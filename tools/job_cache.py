#!/usr/bin/env python3
"""Private job-description and structured-fact cache.

Purpose: stop the same posting being fetched and re-interpreted repeatedly inside
one discovery/ranking cycle. This is a short-lived working cache, not an archive.

Storage is `job_scraper/cache/<key>.json`, keyed from canonical vacancy identity,
and the directory is gitignored.

FOUR SEPARATE CLOCKS. Conflating them lets an unrelated cache write make stale
evidence look fresh, which is a correctness bug rather than an efficiency one:

  cached_at               When the cache FILE was last rewritten. Every write moves
                          it. It never decides whether evidence may be reused.
  description_fetched_at  When the job description was actually fetched. Only a
                          write that genuinely supplies description_text, a
                          --description-file, or an explicit description_fetched_at
                          moves it.
  facts_fetched_at        When the structured facts were actually extracted or
                          refreshed. Only a write that genuinely supplies facts or
                          an explicit facts_fetched_at moves it.
  open_status_checked_at  When the vacancy's open/closed state was actually checked.
                          Only a write that explicitly supplies an open_status
                          observation moves it.

Description and facts are two evidence classes, not one. Re-extracting facts says
nothing about whether the advert body still reads the way it did eight days ago, so
a facts-only write must leave the description exactly as stale as it was, and a
description-only write must leave the facts exactly as stale as they were.

`fetched_at` survives only as a derived compatibility summary: the newer of the two
class clocks, meaning "when evidence was last genuinely fetched at all". It is never
the authority for either class on its own.

So `job_cache.py put --company "Acme"` is metadata maintenance: it updates cached_at
and leaves every evidence age exactly where it was. A facts refresh ages the facts,
not the description and not the vacancy-status observation.

RUN PROVENANCE IS ALSO SPLIT, for the same reason:

  run_id                  Optional metadata: the most recent run that touched this
                          entry at all, including a metadata-only write. It never
                          makes evidence reusable.
  description_run_id      The run that actually fetched this description.
  facts_run_id            The run that actually extracted or refreshed these facts.
  evidence_run_id         The run that actually fetched reusable evidence of either
                          class. Only this may trigger same-run evidence reuse.

All four are derived by this module from what the operation genuinely supplied, not
read from the caller's payload, so a metadata-only or open-status-only write cannot
claim to have fetched anything. A legacy entry carrying only `run_id` is readable,
but its `run_id` is never promoted to evidence provenance: there is no safe evidence
that such an entry represents a genuine fetch.

TTL POLICY (deliberately simple):

  Same run          Description or facts actually fetched by this run stay reusable
                    for that evidence class, whatever their age. `/rank` must not
                    re-extract a JD that `/scrape` already extracted minutes earlier
                    in the same cycle. A write that supplied no evidence, such as a
                    metadata-only or open-status-only put, grants no reuse.
  Description       Reusable for CACHE_TTL_HOURS (72h) from description_fetched_at.
  Facts             Reusable for CACHE_TTL_HOURS (72h) from facts_fetched_at.
  Open/closed state Reusable for OPEN_STATUS_TTL_HOURS (12h) measured from
                    `open_status_checked_at`, and never a substitute for a live
                    check before presenting a high-priority recommendation. Cache
                    reuse must not suppress that check.
  Changed text      A differing description_hash marks the advert as materially
                    rewritten, so a refresh is required rather than optional.
  Pruning           `prune` drops entries whose file is older than PRUNE_AFTER_DAYS
                    (30), which is genuinely a question about the file.

`get` therefore reports each evidence class independently: description_age_hours,
facts_age_hours, description_fresh, facts_fresh, reuse_description and reuse_facts.
Callers must read the two reuse decisions separately. `fresh` remains only as a
conservative summary meaning "every evidence class this entry holds is reusable".

PRIVACY. The cache schema contains no candidate-profile or credential fields, and
a field whitelist is enforced at the write boundary, so such a field name is
refused outright. That is a schema guarantee, not a content guarantee: an allowed
free-text field such as description_text cannot be proven to hold only vacancy
text. Callers must pass vacancy and source content only. The cache never
intentionally receives profile or credential data.

WHAT description_text IS, EXACTLY. It is the SELECTED VACANCY'S OWN JOB-DESCRIPTION
BODY. Nothing else.

That distinction is not pedantry, and a live browser health check showed why. An
authenticated search or results page is not a vacancy: it is a personalised
interface built around the account viewing it. Real examples observed on the
actual sites this workspace searches:

  Indeed results     carried a commute estimate computed from the signed-in
                     user's own saved home address
  Totaljobs results  served a "Suggested based on your CV" recommendation panel,
                     derived from the CV the user uploaded to that site
  any logged-in page carries account chrome, saved searches, message counts and
                     other personalisation belonging to the viewer

Caching a results page verbatim would therefore write the USER'S OWN private data
into a vacancy cache, under a field name the whitelist happily allows, and it
would do so without any component ever intending to. The whitelist cannot catch
that, because `description_text` is a legitimate field and the bytes are just
text.

So the boundary is a WORKFLOW one, enforced by where the text comes from:

    search or results page  ->  extract the card fields and the vacancy URL ONLY
    vacancy URL             ->  tools/url_safety.py
    selected vacancy page   ->  isolate the job-description body
    that body               ->  description_text

Never pass a whole results page, a recommendation panel, a sidebar, a commute
widget, an account page, a search-engine results page, or any other page-level
capture to this cache. If the vacancy body cannot be isolated, cache nothing and
record the description as unavailable. An absent description is a known unknown;
a page-level capture is silent contamination that nothing downstream can detect.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import atomic_write_text, facts_problems, norm_url, source_host  # noqa: E402
from discovery_candidate import description_hash, split_platform_chrome  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / 'job_scraper' / 'cache'
SCHEMA_VERSION = 2

CACHE_TTL_HOURS = 72
OPEN_STATUS_TTL_HOURS = 12
PRUNE_AFTER_DAYS = 30

OPEN_STATUSES = ('unknown', 'open', 'closed')

# Whitelist. Anything outside this set is refused at the write boundary, which is
# what structurally keeps credentials, cookies and candidate data out of the cache.
ALLOWED_FIELDS = frozenset({
    'schema_version', 'key', 'canonical_url', 'source_url', 'source_id',
    'source_family', 'source_type', 'source_host', 'source_job_id', 'requisition_id',
    'company', 'title', 'location', 'posted', 'posted_raw',
    # The search platform's OWN classification of the role, kept apart from the
    # employer's words and always labelled with who asserted it. Never
    # employer-stated evidence, and never an input to a hard blocker on its own.
    'platform_metadata', 'platform_metadata_source',
    'description_text', 'description_hash', 'facts',
    'open_status', 'open_status_checked_at',
    'description_fetched_at', 'facts_fetched_at',
    'fetched_at', 'cached_at',
    'run_id', 'description_run_id', 'facts_run_id', 'evidence_run_id',
})

# Run provenance is derived from what an operation genuinely supplied. A caller
# payload may never assert it, or a metadata-only write could forge same-run reuse.
DERIVED_RUN_FIELDS = ('run_id', 'description_run_id', 'facts_run_id', 'evidence_run_id')


def cache_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def parse_iso(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def age_hours(value, reference=None):
    stamp = parse_iso(value)
    if stamp is None:
        return None
    reference = reference or datetime.now().astimezone()
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return max(0.0, round((reference - stamp).total_seconds() / 3600.0, 3))


def cache_key(url):
    """Stable key from canonical vacancy identity."""
    canonical = norm_url(url)
    if not canonical:
        raise cache_error('A cache key needs a URL.', 'Pass --url with the vacancy URL.')
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:40]


def entry_path(key):
    return CACHE_DIR / f'{key}.json'


def load_entry(key):
    path = entry_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def entry_problems(entry):
    """Whitelist and vocabulary problems in a cache entry."""
    problems = []
    if not isinstance(entry, dict):
        return [{'field': '_root', 'problem': 'not_an_object'}]
    for field in sorted(set(entry) - ALLOWED_FIELDS):
        problems.append({'field': field, 'problem': 'not_an_allowed_cache_field'})
    status = entry.get('open_status')
    if status and str(status).strip().lower() not in OPEN_STATUSES:
        problems.append({'field': 'open_status', 'value': status, 'problem': 'not_in_vocabulary'})
    if entry.get('facts') is not None:
        problems.extend(facts_problems(entry.get('facts')))
    return problems


def scan_problems():
    """Every stored entry that breaks the whitelist or a controlled vocabulary."""
    found = []
    for path in sorted(CACHE_DIR.glob('*.json')) if CACHE_DIR.exists() else []:
        try:
            entry = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            found.append({'file': path.name, 'problems': [{'field': '_file',
                                                           'problem': f'unreadable: {type(exc).__name__}'}]})
            continue
        problems = entry_problems(entry)
        if problems:
            found.append({'file': path.name, 'problems': problems})
    return found


def newest_stamp(*values):
    """The latest of several ISO stamps, comparing real instants rather than text."""
    dated = [(parse_iso(v), v) for v in values if v]
    dated = [(stamp.astimezone() if stamp.tzinfo is None else stamp, raw)
             for stamp, raw in dated if stamp is not None]
    return max(dated)[1] if dated else ''


def evidence_clock(supplied, payload, existing, field, write_time):
    """The fetch time for one evidence class after this write.

    An explicit class timestamp always wins, so a caller who fetched an hour ago and
    is only now caching it stays honest. Otherwise the clock moves to the write time
    only when this operation genuinely supplied that evidence class. When it did
    not, the stored time is kept, falling back to a pre-split `fetched_at` so a
    legacy entry keeps the age it really has.
    """
    explicit = str(payload.get(field) or '').strip()
    if explicit:
        return explicit
    if supplied:
        # A pre-split caller may still pass a bare `fetched_at` alongside the
        # evidence it is writing. That is this write's own observation time.
        return str(payload.get('fetched_at') or '').strip() or write_time
    return str(existing.get(field) or existing.get('fetched_at') or '').strip()


def class_clock(entry, field):
    """The stored fetch time for one evidence class, with legacy fallback.

    A pre-split entry recorded only `fetched_at`, which was a genuine evidence
    fetch time, so it is safe to read for either class. `cached_at` is the last
    resort for an entry that never recorded a fetch time at all. The fallback is
    read-only: it never turns a file rewrite into an evidence observation, because
    a write that supplies no evidence never moves `fetched_at` either.
    """
    return entry.get(field) or entry.get('fetched_at') or entry.get('cached_at') or ''


def freshness(entry, run_id='', ttl_hours=CACHE_TTL_HOURS):
    """How reusable each evidence class in one entry is right now.

    Description and facts are decided independently, each from its own fetch clock
    and its own run provenance. A metadata-only rewrite of the file must not make
    old evidence look fresh, and refreshing one evidence class must not make the
    other look fresh.
    """
    description_at = class_clock(entry, 'description_fetched_at')
    facts_at = class_clock(entry, 'facts_fetched_at')
    description_age = age_hours(description_at)
    facts_age = age_hours(facts_at)
    cached_age = age_hours(entry.get('cached_at'))
    status_age = age_hours(entry.get('open_status_checked_at'))

    # Only a run that ACTUALLY fetched evidence may claim same-run reuse, and only
    # for the class it fetched. `run_id` is metadata and is deliberately not read.
    same_run_description = bool(run_id) and entry.get('description_run_id') == run_id
    same_run_facts = bool(run_id) and entry.get('facts_run_id') == run_id
    same_run = bool(run_id) and entry.get('evidence_run_id') == run_id

    description_fresh = bool(same_run_description
                             or (description_age is not None and description_age <= ttl_hours))
    facts_fresh = bool(same_run_facts or (facts_age is not None and facts_age <= ttl_hours))
    has_description = bool(entry.get('description_text'))
    has_facts = bool(entry.get('facts'))

    # `fresh` is a conservative compatibility summary only: every evidence class
    # this entry actually holds is reusable. Callers decide per class.
    held = ([('description', description_fresh, description_age)] if has_description else []) + \
           ([('facts', facts_fresh, facts_age)] if has_facts else [])
    fresh = bool(held) and all(is_fresh for _, is_fresh, _ in held)
    # The pessimistic evidence age: the oldest class this entry actually holds.
    ages = [age for _, _, age in held if age is not None]
    if not held:
        ages = [age for age in (description_age, facts_age) if age is not None]
    evidence_age = max(ages) if ages else None

    return {
        'age_hours': evidence_age,
        'fetched_age_hours': evidence_age,
        'cache_age_hours': cached_age,
        'description_age_hours': description_age,
        'facts_age_hours': facts_age,
        'description_fetched_at': description_at,
        'facts_fetched_at': facts_at,
        'fetched_at': entry.get('fetched_at', ''),
        'cached_at': entry.get('cached_at', ''),
        'ttl_hours': ttl_hours,
        'run_id': entry.get('run_id', ''),
        'evidence_run_id': entry.get('evidence_run_id', ''),
        'description_run_id': entry.get('description_run_id', ''),
        'facts_run_id': entry.get('facts_run_id', ''),
        'same_run': same_run,
        'same_run_description': same_run_description,
        'same_run_facts': same_run_facts,
        'description_fresh': description_fresh,
        'facts_fresh': facts_fresh,
        'fresh': fresh,
        'has_description': has_description,
        'has_facts': has_facts,
        'reuse_description': bool(description_fresh and has_description),
        'reuse_facts': bool(facts_fresh and has_facts),
        'open_status': entry.get('open_status', 'unknown'),
        'open_status_checked_at': entry.get('open_status_checked_at', ''),
        'open_status_age_hours': status_age,
        'open_status_fresh': bool(status_age is not None and status_age <= OPEN_STATUS_TTL_HOURS),
        'open_status_ttl_hours': OPEN_STATUS_TTL_HOURS,
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def read_json_input(args):
    if getattr(args, 'file', ''):
        path = Path(args.file)
        if not path.exists():
            raise cache_error(f'Input file not found: {path}')
        raw = path.read_text(encoding='utf-8')
    else:
        raw = sys.stdin.read()
    # Windows shells routinely prefix piped text with a byte-order mark.
    raw = raw.lstrip('﻿')
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise cache_error('Malformed JSON input.',
                          f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None


def cmd_put(args):
    payload = read_json_input(args) if (args.file or not sys.stdin.isatty()) else {}
    if not isinstance(payload, dict):
        raise cache_error('Cache payload must be a JSON object.')

    url = args.url or payload.get('source_url') or payload.get('canonical_url') or ''
    key = cache_key(url)
    existing = load_entry(key) or {}

    entry = {field: existing.get(field) for field in existing if field in ALLOWED_FIELDS}
    for field, value in payload.items():
        entry[field] = value

    if args.description_file:
        entry['description_text'] = Path(args.description_file).read_text(encoding='utf-8')
    for flag, field in (('open_status', 'open_status'), ('source_id', 'source_id'),
                        ('company', 'company'), ('title', 'title')):
        value = getattr(args, flag, '')
        if value:
            entry[field] = value

    # What THIS operation actually supplied, decided before the merged entry can
    # blur it. Inheriting a field from the existing entry is not observing it, and
    # the two evidence classes are counted separately because they age separately.
    # An explicit class timestamp counts as supplying that class, because it is an
    # assertion about when that evidence was genuinely fetched.
    supplied_description = bool(args.description_file or 'description_text' in payload
                                or str(payload.get('description_fetched_at') or '').strip())
    supplied_facts = bool('facts' in payload
                          or str(payload.get('facts_fetched_at') or '').strip())
    supplied_open_status = bool(args.open_status or 'open_status' in payload)
    run_id = (getattr(args, 'run_id', '') or '').strip()

    # `fetched_at` is a derived summary now, so a bare one cannot say which evidence
    # class it describes. Silently discarding it would let a caller believe stale
    # evidence had been dated.
    if str(payload.get('fetched_at') or '').strip() and not (supplied_description or supplied_facts):
        raise cache_error(
            'fetched_at was supplied without any evidence to date.',
            'fetched_at is a derived summary of the two evidence clocks, so on its own '
            'it cannot say whether the description or the facts were fetched.',
            'Pass description_fetched_at or facts_fetched_at, or supply the '
            'description_text / facts the timestamp belongs to.',
        )

    entry['schema_version'] = SCHEMA_VERSION
    entry['key'] = key
    entry['canonical_url'] = norm_url(url)
    entry['source_url'] = entry.get('source_url') or url
    entry['source_host'] = source_host(entry.get('source_url', ''))

    # Split the search platform's own page furniture off the employer's text. The
    # description must be the vacancy body alone; LinkedIn's Seniority level and
    # Employment type are the PLATFORM's classification, and storing them inside
    # description_text would attribute to the employer a statement it never made.
    # A last line of defence: the workflow is supposed to isolate the body first,
    # and every LinkedIn entry in the first production cache proved it does not
    # always happen.
    description_unavailable = False
    if supplied_description and entry.get('description_text'):
        split = split_platform_chrome(entry['description_text'],
                                      entry.get('source_id', ''), entry.get('source_host', ''))
        if split['chrome_removed']:
            if split['platform_metadata']:
                entry['platform_metadata'] = split['platform_metadata']
                entry['platform_metadata_source'] = split['provenance']
            else:
                entry.pop('platform_metadata', None)
                entry.pop('platform_metadata_source', None)
            if split['description_unavailable']:
                # The extraction found only the platform's block, so no job
                # description was isolated at all. Treat this write as supplying NO
                # description: any previously fetched body stays exactly as it was,
                # at exactly the age it was, and nothing derived from chrome is
                # stored. A failed isolation must never make description evidence
                # look freshly fetched.
                description_unavailable = True
                supplied_description = False
                if 'description_text' in existing:
                    entry['description_text'] = existing['description_text']
                else:
                    entry.pop('description_text', None)
                    entry.pop('description_hash', None)
                if str(payload.get('description_fetched_at') or '').strip():
                    raise cache_error(
                        'description_fetched_at was supplied, but the text contains no '
                        'job description.',
                        'Only the search platform\'s own block was present, so the '
                        'vacancy body was never isolated.',
                        'Cache nothing for the description and record it as unavailable. '
                        'An absent description is a known unknown; platform chrome '
                        'stored as a vacancy body is silent contamination.',
                    )
            else:
                entry['description_text'] = split['description']

    # cached_at is the file-write clock and moves on every write.
    entry['cached_at'] = now_iso()
    if entry.get('description_text'):
        entry['description_hash'] = description_hash(entry['description_text'])
    else:
        entry.pop('description_hash', None)
    if entry.get('open_status'):
        entry['open_status'] = str(entry['open_status']).strip().lower()

    # Each evidence class keeps its own clock. A metadata-only update such as
    # `put --company "Acme"` moves neither, and a facts-only refresh must leave an
    # eight-day-old description exactly eight days old.
    entry['description_fetched_at'] = evidence_clock(
        supplied_description, payload, existing, 'description_fetched_at', entry['cached_at'])
    entry['facts_fetched_at'] = evidence_clock(
        supplied_facts, payload, existing, 'facts_fetched_at', entry['cached_at'])
    for field in ('description_fetched_at', 'facts_fetched_at'):
        if not entry[field]:
            entry.pop(field)

    # fetched_at survives only as a derived summary: when evidence was last fetched
    # at all. It is never the authority for either class on its own.
    summary_clock = newest_stamp(entry.get('description_fetched_at'), entry.get('facts_fetched_at'))
    if summary_clock:
        entry['fetched_at'] = summary_clock
    else:
        entry.pop('fetched_at', None)

    # open_status_checked_at is the vacancy-status clock. Refreshing facts does not
    # re-observe whether the vacancy is still open, so it must not age the status.
    if supplied_open_status:
        entry['open_status_checked_at'] = (
            str(payload.get('open_status_checked_at') or '').strip() or now_iso())
    elif existing.get('open_status_checked_at'):
        entry['open_status_checked_at'] = existing['open_status_checked_at']
    else:
        entry.pop('open_status_checked_at', None)

    # Run provenance is derived here, never taken from the payload, so a
    # metadata-only or open-status-only write cannot claim to have fetched
    # anything. Only a run that genuinely supplied an evidence class owns it.
    entry['run_id'] = run_id or existing.get('run_id', '')
    entry['description_run_id'] = (run_id if supplied_description
                                   else existing.get('description_run_id', ''))
    entry['facts_run_id'] = run_id if supplied_facts else existing.get('facts_run_id', '')
    entry['evidence_run_id'] = (run_id if (supplied_description or supplied_facts)
                                else existing.get('evidence_run_id', ''))
    for field in DERIVED_RUN_FIELDS:
        if not entry[field]:
            entry.pop(field)

    problems = entry_problems(entry)
    if problems:
        raise cache_error(
            'Refusing to write an invalid cache entry.',
            f'Problems: {json.dumps(problems, ensure_ascii=False)}',
            'The cache schema contains vacancy and source fields only. Credentials, '
            'cookies, browser session data and candidate profile content are not '
            'cacheable field names. Pass vacancy and source content only.',
        )

    previous_hash = existing.get('description_hash', '')
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(entry_path(key), json.dumps(entry, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({
        'cached': True,
        'key': key,
        'canonical_url': entry['canonical_url'],
        'description_hash': entry.get('description_hash', ''),
        'description_changed': bool(previous_hash and entry.get('description_hash')
                                    and previous_hash != entry['description_hash']),
        'previous_description_hash': previous_hash,
        # True when the supplied text held only the platform's own block, so no
        # vacancy description was isolated and none was stored. The description
        # clock did not move: a failed isolation is not a fetch.
        'description_unavailable': description_unavailable,
        'platform_metadata': entry.get('platform_metadata', {}),
        'path': entry_path(key).relative_to(ROOT).as_posix(),
    }, ensure_ascii=False))


def cmd_get(args):
    key = cache_key(args.url)
    entry = load_entry(key)
    if entry is None:
        print(json.dumps({'hit': False, 'key': key, 'canonical_url': norm_url(args.url),
                          'description_fresh': False, 'facts_fresh': False,
                          'reuse_description': False, 'reuse_facts': False}, ensure_ascii=False))
        raise SystemExit(1)
    state = freshness(entry, run_id=args.run_id, ttl_hours=args.ttl_hours)
    body = dict(entry)
    if not args.with_description:
        body.pop('description_text', None)
    print(json.dumps({'hit': True, 'key': key, **state, 'entry': body},
                     indent=2, ensure_ascii=False))


def cmd_stats(args):
    """Cache size and freshness, counted per evidence class.

    A single fresh/stale count would hide the case this cache exists to get right:
    an entry whose facts were refreshed today while its description is eight days
    old is neither fresh nor stale as a whole.
    """
    entries = []
    for path in sorted(CACHE_DIR.glob('*.json')) if CACHE_DIR.exists() else []:
        entry = load_entry(path.stem)
        if entry is None:
            continue
        state = freshness(entry)
        entries.append({
            'key': entry.get('key', path.stem),
            'canonical_url': entry.get('canonical_url', ''),
            'company': entry.get('company', ''),
            'title': entry.get('title', ''),
            'age_hours': state['age_hours'],
            'cache_age_hours': state['cache_age_hours'],
            'description_age_hours': state['description_age_hours'],
            'facts_age_hours': state['facts_age_hours'],
            'description_fresh': state['description_fresh'],
            'facts_fresh': state['facts_fresh'],
            'has_description': state['has_description'],
            'has_facts': state['has_facts'],
            'open_status': entry.get('open_status', 'unknown'),
            'open_status_fresh': state['open_status_fresh'],
        })

    def count(predicate):
        return sum(1 for e in entries if predicate(e))

    fresh_entries = count(lambda e: (e['has_description'] or e['has_facts'])
                          and (e['description_fresh'] or not e['has_description'])
                          and (e['facts_fresh'] or not e['has_facts']))
    print(json.dumps({
        'cache_dir': CACHE_DIR.relative_to(ROOT).as_posix(),
        'entries': len(entries),
        # Entry-level counts stay conservative: fresh means every evidence class
        # this entry holds is reusable.
        'fresh': fresh_entries,
        'stale': len(entries) - fresh_entries,
        'descriptions': count(lambda e: e['has_description']),
        'fresh_descriptions': count(lambda e: e['has_description'] and e['description_fresh']),
        'stale_descriptions': count(lambda e: e['has_description'] and not e['description_fresh']),
        'facts': count(lambda e: e['has_facts']),
        'fresh_facts': count(lambda e: e['has_facts'] and e['facts_fresh']),
        'stale_facts': count(lambda e: e['has_facts'] and not e['facts_fresh']),
        'fresh_open_status': count(lambda e: e['open_status_fresh']),
        'ttl_hours': CACHE_TTL_HOURS,
        'open_status_ttl_hours': OPEN_STATUS_TTL_HOURS,
        'prune_after_days': PRUNE_AFTER_DAYS,
        'schema_problems': scan_problems(),
        'items': entries if args.verbose else [],
    }, indent=2, ensure_ascii=False))


def cmd_prune(args):
    cutoff = datetime.now().astimezone() - timedelta(days=args.max_age_days)
    removed = []
    for path in sorted(CACHE_DIR.glob('*.json')) if CACHE_DIR.exists() else []:
        entry = load_entry(path.stem)
        stamp = parse_iso((entry or {}).get('cached_at')) if entry else None
        if entry is None or stamp is None or stamp.astimezone() < cutoff:
            removed.append(path.name)
            if not args.dry_run:
                path.unlink(missing_ok=True)
    print(json.dumps({'pruned': len(removed), 'dry_run': bool(args.dry_run),
                      'max_age_days': args.max_age_days, 'removed': removed},
                     ensure_ascii=False))


def cmd_scan(args):
    problems = scan_problems()
    print(json.dumps({'entries_with_problems': len(problems), 'problems': problems,
                      'allowed_fields': sorted(ALLOWED_FIELDS)}, indent=2, ensure_ascii=False))
    raise SystemExit(0 if not problems else 1)


def _force_utf8_stdout():
    """Vacancy text is not cp1252, and a Windows console is.

    A real advert title carrying an en-dash or a pound sign made this tool exit
    with UnicodeEncodeError instead of printing, which took `/rank` down on
    Windows the moment a normal role title contained one. The DATA was fine; only
    the console encoding was wrong, so fix the stream rather than the text.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if (getattr(stream, 'encoding', '') or '').lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def main():
    _force_utf8_stdout()
    p = argparse.ArgumentParser(description='Private job-description and fact cache')
    sub = p.add_subparsers(dest='cmd', required=True)

    put = sub.add_parser('put', help='Write or refresh one cache entry (JSON body on stdin or --file).')
    put.add_argument('--url', default='')
    put.add_argument('--file', default='')
    put.add_argument('--description-file', dest='description_file', default='',
                     help='A file holding the job-description body of the SELECTED '
                          'VACANCY only. Never a whole search or results page: an '
                          'authenticated results page carries personalisation '
                          'belonging to the viewer, such as a commute estimate or a '
                          'CV-derived recommendation panel.')
    put.add_argument('--open-status', dest='open_status', default='')
    put.add_argument('--run-id', dest='run_id', default='',
                     help='Run touching this entry. It owns an evidence class only when '
                          'this write actually supplies that class.')
    put.add_argument('--source-id', dest='source_id', default='')
    put.add_argument('--company', default='')
    put.add_argument('--title', default='')
    put.set_defaults(func=cmd_put)

    get = sub.add_parser('get', help='Read one cache entry and its per-class reuse decisions.')
    get.add_argument('--url', required=True)
    get.add_argument('--run-id', dest='run_id', default='')
    get.add_argument('--ttl-hours', dest='ttl_hours', type=float, default=CACHE_TTL_HOURS)
    get.add_argument('--with-description', dest='with_description', action='store_true')
    get.set_defaults(func=cmd_get)

    st = sub.add_parser('stats', help='Summarise cache size and freshness.')
    st.add_argument('--verbose', action='store_true')
    st.set_defaults(func=cmd_stats)

    pr = sub.add_parser('prune', help='Drop entries older than the retention window.')
    pr.add_argument('--max-age-days', dest='max_age_days', type=int, default=PRUNE_AFTER_DAYS)
    pr.add_argument('--dry-run', dest='dry_run', action='store_true')
    pr.set_defaults(func=cmd_prune)

    sc = sub.add_parser('scan', help='Verify every stored entry against the field whitelist.')
    sc.set_defaults(func=cmd_scan)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

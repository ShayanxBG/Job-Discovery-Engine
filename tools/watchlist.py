#!/usr/bin/env python3
"""Private BOUNDED employer watchlist for targeted ATS/careers discovery.

Searching known employer career systems directly is the highest-authority
discovery this workspace can do: no aggregator copy, no agency intermediary, a
real requisition on the employer's own ATS.

The obvious temptation is to do it at scale, because the sponsor register holds
thousands of licensed organisations, so why not check them all? Because that is an
unbounded crawler wearing a job-search costume. It would take enormous budget,
overwhelmingly against employers who have no relevant vacancy, and it would drown
the genuinely promising employers in noise. THIS FILE IS BOUNDED ON PURPOSE.

MAX_ACTIVE is a single documented number rather than a formula, because a limit
nobody can state is a limit nobody enforces. An employer earns a place by evidence:

    strong_match         previously produced a strong or viable ranked match
    sponsor_evidence     discovered with credible sponsorship evidence
    manual               deliberately added by the user
    known_ats            a resolved employer entity with a known ATS tenant
    recurring            appeared repeatedly in relevant searches

Adding every employer ever seen is explicitly not one of those reasons.

`due` implements a simple, honest rotation: an entry is due when it has never been
checked or its check interval has elapsed, ordered by priority then by staleness, so
the bounded budget goes to the most promising employers that have waited longest.
A disabled entry is never due and is never counted against the active maximum.
"""
import argparse
import json
import sys
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import atomic_write_text  # noqa: E402
from employers import ATS_PLATFORMS, employer_key  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / 'job_scraper' / 'watchlist.json'
SCHEMA_VERSION = 1

# The documented bound. One number, chosen for simplicity: 60 active employers is
# enough to cover the employers that actually produce matches for one candidate,
# and small enough that a full rotation stays affordable in a normal run.
MAX_ACTIVE = 60

REASONS = ('strong_match', 'sponsor_evidence', 'manual', 'known_ats', 'recurring')
PRIORITIES = (1, 2, 3)
DEFAULT_CHECK_INTERVAL_DAYS = 7

FIELDS = ('employer_key', 'canonical_name', 'reason', 'priority', 'ats_platform',
          'ats_tenant', 'careers_url', 'added_at', 'last_checked', 'enabled',
          'check_interval_days', 'notes',
          # An entry must be able to say WHY it is here, in words a human can
          # check, or the evidence requirement is a promise rather than a record.
          'evidence', 'next_due', 'consecutive_failures', 'last_failed')

# How strong the reason is, for ordering only. It never decides ADMISSION: a
# reason not in REASONS is refused whatever its rank would have been.
REASON_STRENGTH = {'strong_match': 1, 'manual': 2, 'known_ats': 3,
                   'sponsor_evidence': 4, 'recurring': 5}

# A failed check backs off along this ladder rather than being retried every run.
# Retrying a dead careers page daily spends the employer budget on a URL that has
# already answered, every day, at the cost of an employer that would have answered
# differently. The ladder is read from configuration so the policy has one home.
DEFAULT_FAILURE_BACKOFF_DAYS = (1, 3, 7, 14, 30)
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5


def _ats_policy():
    """Employer ATS policy from the search strategy, with safe fallbacks."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from search_strategy import load_strategy
        block = (load_strategy() or {}).get('employer_ats_policy') or {}
    except Exception:  # noqa: BLE001 - an unreadable strategy is not a crash here
        block = {}
    return {
        'failure_backoff_days': tuple(block.get('failure_backoff_days')
                                      or DEFAULT_FAILURE_BACKOFF_DAYS),
        'max_consecutive_failures': int(
            block.get('max_consecutive_failures_before_disable')
            or DEFAULT_MAX_CONSECUTIVE_FAILURES),
    }


def watchlist_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def today_iso(on=''):
    return (on or '').strip() or date.today().isoformat()


def load_store(path=None):
    path = Path(path) if path else STORE
    if not path.exists():
        return {'schema_version': SCHEMA_VERSION, 'max_active': MAX_ACTIVE, 'entries': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise watchlist_error(
            f'Malformed watchlist: {path}',
            f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}',
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise watchlist_error(f'Watchlist could not be read: {path}',
                              f'{type(exc).__name__}: {exc}') from None
    if not isinstance(data, dict) or not isinstance(data.get('entries'), dict):
        raise watchlist_error(f'Invalid watchlist: {path}',
                              'Expected an object with an "entries" mapping.')
    return data


def save_store(data, path=None):
    path = Path(path) if path else STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    data['schema_version'] = SCHEMA_VERSION
    data['max_active'] = MAX_ACTIVE
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def is_enabled(entry):
    return bool(entry.get('enabled', True))


def active_entries(data):
    return {k: e for k, e in data.get('entries', {}).items() if is_enabled(e)}


def entry_problems(entry):
    problems = []
    if not isinstance(entry, dict):
        return [{'field': '_root', 'problem': 'not_an_object'}]
    for field in sorted(set(entry) - set(FIELDS)):
        problems.append({'field': field, 'problem': 'not_a_watchlist_field'})
    reason = str(entry.get('reason') or '').strip().lower()
    if not reason:
        problems.append({'field': 'reason', 'problem': 'required'})
    elif reason not in REASONS:
        problems.append({'field': 'reason', 'value': reason, 'problem': 'not_in_vocabulary'})
    try:
        priority = int(entry.get('priority', 2))
    except (TypeError, ValueError):
        priority = None
    if priority not in PRIORITIES:
        problems.append({'field': 'priority', 'value': entry.get('priority'),
                         'problem': 'not_in_vocabulary'})
    platform = str(entry.get('ats_platform') or '').strip().lower()
    if platform and platform not in ATS_PLATFORMS:
        problems.append({'field': 'ats_platform', 'value': platform,
                         'problem': 'not_in_vocabulary'})
    # An entry with no stated evidence is an entry nobody can audit. The reason
    # names the CLASS of evidence; this is the evidence itself.
    if not str(entry.get('evidence') or '').strip():
        problems.append({'field': 'evidence', 'problem': 'required',
                         'detail': 'Every entry must record what put it here, in '
                                   'checkable words. A reason alone is a label.'})
    # A known_ats entry that names no tenant and no careers URL is asserting an
    # ATS this workspace cannot reach. Inventing a tenant is exactly the failure
    # the reason vocabulary exists to prevent.
    if str(entry.get('reason') or '').strip().lower() == 'known_ats' and not (
            str(entry.get('ats_tenant') or '').strip()
            or str(entry.get('careers_url') or '').strip()):
        problems.append({'field': 'ats_tenant', 'problem': 'required_for_known_ats',
                         'detail': 'known_ats claims a resolved ATS. Without a tenant '
                                   'or a careers URL there is nothing to check.'})
    failures = entry.get('consecutive_failures', 0)
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        problems.append({'field': 'consecutive_failures',
                         'problem': 'must_be_a_non_negative_whole_number'})
    return problems


def store_problems(data):
    """Every structural problem in the whole store, including the bound."""
    problems = []
    if not isinstance(data, dict) or not isinstance(data.get('entries'), dict):
        return [{'problem': 'not_a_watchlist_store'}]
    active = active_entries(data)
    if len(active) > MAX_ACTIVE:
        problems.append({'problem': 'over_max_active', 'active': len(active),
                         'max_active': MAX_ACTIVE,
                         'detail': 'The bound is the whole point: an unbounded '
                                   'watchlist is a crawler wearing a job-search costume.'})
    for key, entry in sorted(data.get('entries', {}).items()):
        for problem in entry_problems(entry):
            problems.append({'employer_key': key, **problem})
        if entry.get('employer_key') and entry['employer_key'] != key:
            problems.append({'employer_key': key, 'field': 'employer_key',
                             'problem': 'does_not_match_its_store_key'})
    return problems


def add(name, reason, priority=2, ats_platform='', ats_tenant='', careers_url='',
        check_interval_days=DEFAULT_CHECK_INTERVAL_DAYS, notes='', evidence='',
        store=None, path=None):
    """Add or update one watchlist entry, refusing to exceed the documented bound."""
    data = store or load_store(path)
    key = employer_key(name)
    if not key:
        raise watchlist_error('A watchlist entry needs a usable employer name.')

    entries = data.setdefault('entries', {})
    existing = entries.get(key)
    entry = dict(existing or {})
    entry.update({
        'employer_key': key,
        'canonical_name': entry.get('canonical_name') or str(name).strip(),
        'reason': str(reason or '').strip().lower(),
        'priority': int(priority),
        'enabled': True,
        'check_interval_days': int(check_interval_days),
        'evidence': str(evidence or entry.get('evidence') or '').strip(),
        'consecutive_failures': int(entry.get('consecutive_failures', 0) or 0),
    })
    entry['next_due'] = next_due(entry)
    for field, value in (('ats_platform', str(ats_platform or '').strip().lower()),
                         ('ats_tenant', str(ats_tenant or '').strip()),
                         ('careers_url', str(careers_url or '').strip()),
                         ('notes', str(notes or '').strip())):
        if value:
            entry[field] = value
    entry.setdefault('added_at', now_iso())

    problems = entry_problems(entry)
    if problems:
        raise watchlist_error(
            'Refusing to write an invalid watchlist entry.',
            f'Problems: {json.dumps(problems, ensure_ascii=False)}',
            f'An employer earns a place by evidence. Allowed reasons: {", ".join(REASONS)}.',
        )

    reactivating = bool(existing) and not is_enabled(existing)
    if (not existing or reactivating) and len(active_entries(data)) >= MAX_ACTIVE:
        raise watchlist_error(
            f'The watchlist is full: {MAX_ACTIVE} active employers.',
            'This bound is deliberate. A watchlist that grows without limit becomes an '
            'unbounded crawler over employers with no relevant vacancy.',
            'Disable a lower-value employer first: watchlist.py disable <employer>.',
        )

    entries[key] = entry
    save_store(data, path)
    return {'employer_key': key, 'added': not existing, 'reactivated': reactivating,
            'active': len(active_entries(data)), 'max_active': MAX_ACTIVE, 'entry': entry}


def set_enabled(name, enabled, store=None, path=None):
    data = store or load_store(path)
    key = employer_key(name)
    entry = data.get('entries', {}).get(key)
    if entry is None:
        raise watchlist_error(f'Not on the watchlist: {name!r}',
                              'List current entries with: python tools/watchlist.py list')
    if enabled and not is_enabled(entry) and len(active_entries(data)) >= MAX_ACTIVE:
        raise watchlist_error(f'The watchlist is full: {MAX_ACTIVE} active employers.',
                              'Disable a lower-value employer before re-enabling this one.')
    entry['enabled'] = bool(enabled)
    save_store(data, path)
    return {'employer_key': key, 'enabled': entry['enabled'],
            'active': len(active_entries(data)), 'max_active': MAX_ACTIVE}


def remove(name, store=None, path=None):
    data = store or load_store(path)
    key = employer_key(name)
    if key not in data.get('entries', {}):
        raise watchlist_error(f'Not on the watchlist: {name!r}')
    data['entries'].pop(key)
    save_store(data, path)
    return {'employer_key': key, 'removed': True, 'active': len(active_entries(data))}


def days_since(stamp, on=''):
    if not stamp:
        return None
    try:
        checked = date.fromisoformat(str(stamp)[:10])
    except ValueError:
        return None
    return (date.fromisoformat(today_iso(on)) - checked).days


def backoff_days(failures, policy=None):
    """How long to wait after `failures` consecutive failed checks."""
    ladder = (policy or _ats_policy())['failure_backoff_days']
    if failures <= 0 or not ladder:
        return 0
    return int(ladder[min(int(failures), len(ladder)) - 1])


def next_due(entry, on=''):
    """The date this entry next becomes checkable, as a stored, readable fact.

    Derived rather than free text, so a human reading the private file can see
    when an employer comes back round without simulating the rotation.
    """
    interval = int(entry.get('check_interval_days', DEFAULT_CHECK_INTERVAL_DAYS))
    failures = int(entry.get('consecutive_failures', 0) or 0)
    # The backoff is ADDED to the ordinary interval, not compared against it. Taking
    # the larger of the two made the first three failures indistinguishable from a
    # normal wait, so the ladder only began to bite after the fourth. A backoff that
    # does not visibly grow from the first failure is a retry loop with a nicer name.
    wait = interval + backoff_days(failures)
    anchor = str(entry.get('last_checked') or entry.get('last_failed') or '').strip()
    if not anchor:
        return today_iso(on)
    try:
        return (date.fromisoformat(anchor) + timedelta(days=wait)).isoformat()
    except ValueError:
        return today_iso(on)


def record_failure(name, on='', store=None, path=None):
    """One failed employer check: back off, and disable rather than churn forever.

    Disabling keeps the entry and its history. Deleting would lose the evidence
    that put the employer there, and the next promotion pass would cheerfully add
    it back to fail again.
    """
    data = store or load_store(path)
    key = employer_key(name)
    entry = data.get('entries', {}).get(key)
    if entry is None:
        raise watchlist_error(f'Not on the watchlist: {name!r}')
    policy = _ats_policy()
    entry['consecutive_failures'] = int(entry.get('consecutive_failures', 0) or 0) + 1
    entry['last_failed'] = today_iso(on)
    disabled = entry['consecutive_failures'] >= policy['max_consecutive_failures']
    if disabled:
        entry['enabled'] = False
    entry['next_due'] = next_due(entry, on)
    save_store(data, path)
    return {'employer_key': key, 'consecutive_failures': entry['consecutive_failures'],
            'backoff_days': backoff_days(entry['consecutive_failures'], policy),
            'next_due': entry['next_due'], 'disabled': disabled,
            'max_consecutive_failures': policy['max_consecutive_failures']}


def is_due(entry, on=''):
    """Never checked, or the check interval has elapsed. Disabled is never due."""
    if not is_enabled(entry):
        return False
    # A backing-off entry is not due even though its ordinary interval elapsed.
    # Without this the backoff would be advisory and the retry loop would survive.
    try:
        if date.fromisoformat(next_due(entry, on)) > date.fromisoformat(today_iso(on)):
            return False
    except ValueError:
        pass
    elapsed = days_since(entry.get('last_checked'), on)
    if elapsed is None:
        return True
    return elapsed >= int(entry.get('check_interval_days', DEFAULT_CHECK_INTERVAL_DAYS))


def due(limit=0, on='', store=None, path=None):
    """Watchlist entries worth checking now, most promising and most stale first."""
    data = store or load_store(path)
    rows = []
    for key, entry in data.get('entries', {}).items():
        if not is_due(entry, on):
            continue
        elapsed = days_since(entry.get('last_checked'), on)
        rows.append({
            'employer_key': key,
            'canonical_name': entry.get('canonical_name', ''),
            'reason': entry.get('reason', ''),
            'priority': int(entry.get('priority', 2)),
            'ats_platform': entry.get('ats_platform', ''),
            'ats_tenant': entry.get('ats_tenant', ''),
            'careers_url': entry.get('careers_url', ''),
            'last_checked': entry.get('last_checked', ''),
            'days_since_check': elapsed,
            'never_checked': elapsed is None,
            'evidence': entry.get('evidence', ''),
            'evidence_strength': REASON_STRENGTH.get(entry.get('reason', ''), 99),
            'consecutive_failures': int(entry.get('consecutive_failures', 0) or 0),
            'next_due': entry.get('next_due') or next_due(entry, on),
        })
    # Priority, then how strong the evidence class is, then how long it has waited,
    # then the key. Fully deterministic: two runs of the same store produce the
    # same order, so a bounded employer budget always spends on the same entries.
    rows.sort(key=lambda r: (r['priority'], r['evidence_strength'],
                             -(r['days_since_check'] if r['days_since_check']
                               is not None else 10 ** 6), r['employer_key']))
    return rows[:limit] if limit else rows


def mark_checked(name, on='', store=None, path=None):
    data = store or load_store(path)
    key = employer_key(name)
    entry = data.get('entries', {}).get(key)
    if entry is None:
        raise watchlist_error(f'Not on the watchlist: {name!r}')
    entry['last_checked'] = today_iso(on)
    # A successful check clears the streak. Backoff punishes repeated failure, not
    # an employer that failed once six weeks ago and has answered since.
    entry['consecutive_failures'] = 0
    entry['next_due'] = next_due(entry, on)
    save_store(data, path)
    return {'employer_key': key, 'last_checked': entry['last_checked'],
            'next_due': entry['next_due'], 'due': is_due(entry, on)}


# --------------------------------------------------------------------------
# Promotion: which employers have EARNED a place, from stores this workspace
# already holds. Read-only by default, because a private file is not something a
# tool should quietly rewrite.
# --------------------------------------------------------------------------

# The one thing this must never become is an enumeration of the sponsor register
# against ATS providers. That register lists thousands of licensed organisations,
# almost none of which have a relevant vacancy, and checking them all would be an
# unbounded crawler with a job-search costume on. So sponsor-register membership
# is deliberately NOT a qualifying reason on its own: it says an employer COULD
# sponsor somebody, for any role, which is not evidence about this candidate and
# not evidence about a vacancy. It qualifies only alongside something that ties
# the employer to this search, and the reason recorded is then the tie, not the
# licence.
DISQUALIFYING_ALONE = {
    'sponsor_register_only': (
        'A sponsor-register match alone. The register says an employer holds a '
        'licence, not that it has a relevant vacancy or that this candidate is a '
        'fit. Promoting on it would enumerate the register.'),
    'single_sighting': (
        'Seen once. One sighting is a coincidence; the watchlist is for employers '
        'worth returning to, and recurring means more than one.'),
    'no_reachable_ats': (
        'No resolved ATS tenant and no careers URL. There is nothing to check, and '
        'guessing a tenant would be inventing one.'),
}

MIN_SIGHTINGS_FOR_RECURRING = 2


def _employer_store(path=None):
    base = Path(path) if path else STORE.parent
    file = base / 'employers.json'
    if not file.exists():
        return {}
    try:
        return (json.loads(file.read_text(encoding='utf-8')) or {}).get('employers', {}) or {}
    except (OSError, ValueError):
        return {}


def _sponsorship_store(path=None):
    base = Path(path) if path else STORE.parent
    file = base / 'sponsorship_evidence.json'
    if not file.exists():
        return {}
    try:
        return (json.loads(file.read_text(encoding='utf-8')) or {}).get('employers', {}) or {}
    except (OSError, ValueError):
        return {}


def _seen_employers(path=None):
    """Employer sightings and their best ranked outcome, from discovery state.

    Read defensively: an absent or unreadable state file means no sightings, which
    is honestly zero rather than an excuse to lower the bar.
    """
    base = Path(path) if path else STORE.parent
    file = base / 'seen_jobs.json'
    if not file.exists():
        return {}
    try:
        seen = (json.loads(file.read_text(encoding='utf-8')) or {}).get('seen', {}) or {}
    except (OSError, ValueError):
        return {}
    rows = {}
    for record in seen.values():
        if not isinstance(record, dict):
            continue
        key = employer_key(record.get('company') or '')
        if not key:
            continue
        row = rows.setdefault(key, {'sightings': 0, 'best_score': None,
                                    'canonical_name': record.get('company') or ''})
        row['sightings'] += 1
        score = ((record.get('evaluation') or {}).get('total_score')
                 if isinstance(record.get('evaluation'), dict) else None)
        if isinstance(score, int) and (row['best_score'] is None or score > row['best_score']):
            row['best_score'] = score
    return rows


def promotion_candidates(strong_score=70, path=None):
    """Every employer this workspace already knows, judged against the evidence bar.

    Returns qualifying AND rejected employers with the reason for each, because a
    promotion pass that only shows what it accepted cannot be audited. Nothing is
    written: this function has no side effects at all.
    """
    employers = _employer_store(path)
    sponsorship = _sponsorship_store(path)
    sightings = _seen_employers(path)
    keys = sorted(set(employers) | set(sponsorship) | set(sightings))

    qualifying, rejected = [], []
    for key in keys:
        employer = employers.get(key) or {}
        spon = sponsorship.get(key) or {}
        seen = sightings.get(key) or {}
        name = (employer.get('canonical_name') or spon.get('canonical_name')
                or seen.get('canonical_name') or key)
        tenant = str(employer.get('ats_tenant') or '').strip()
        platform = str(employer.get('ats_platform') or '').strip().lower()
        careers = str(employer.get('careers_url') or '').strip()
        best = seen.get('best_score')
        count = int(seen.get('sightings', 0) or 0)
        register = bool(employer.get('sponsor_register_name')) or bool(spon.get('evidence'))

        reason, evidence = '', ''
        if best is not None and int(best) >= int(strong_score):
            reason = 'strong_match'
            evidence = (f'Previously ranked {best}/100, at or above the {strong_score} '
                        f'strong-match bar, from stored discovery state.')
        elif tenant and platform:
            reason = 'known_ats'
            evidence = (f'Resolved ATS tenant {platform}/{tenant} recorded in the '
                        f'employer store, so the requisition feed is directly checkable.')
        elif careers:
            reason = 'known_ats'
            evidence = f'Known careers URL recorded in the employer store: {careers}'
        elif count >= MIN_SIGHTINGS_FOR_RECURRING:
            reason = 'recurring'
            evidence = (f'Appeared in {count} separate relevant discoveries, which is a '
                        f'pattern rather than a coincidence.')

        if reason:
            qualifying.append({
                'employer_key': key, 'canonical_name': name, 'reason': reason,
                'evidence': evidence,
                'priority': 1 if reason == 'strong_match' else 2,
                'ats_platform': platform, 'ats_tenant': tenant, 'careers_url': careers,
                'sponsor_register_match': register, 'sightings': count,
                'best_score': best,
            })
            continue

        if register and not tenant and not careers:
            why = DISQUALIFYING_ALONE['sponsor_register_only']
        elif count == 1:
            why = DISQUALIFYING_ALONE['single_sighting']
        elif not tenant and not careers:
            why = DISQUALIFYING_ALONE['no_reachable_ats']
        else:
            why = 'No qualifying evidence recorded.'
        rejected.append({'employer_key': key, 'canonical_name': name,
                         'sponsor_register_match': register, 'sightings': count,
                         'best_score': best, 'not_promoted_because': why})

    # Deterministic and bounded. Ordered by evidence strength then key, then cut to
    # the remaining headroom, so the same stores always promote the same employers
    # and a large evidence set can never push the active list past MAX_ACTIVE.
    qualifying.sort(key=lambda r: (REASON_STRENGTH.get(r['reason'], 99), r['employer_key']))
    return {'qualifying': qualifying, 'rejected': rejected}


def seed(strong_score=70, apply_changes=False, limit=0, store=None, path=None,
         data_dir=None):
    """Promote qualifying employers. DRY RUN unless apply_changes is true.

    Dry run is the default deliberately: this writes a private file, and a command
    that mutates private state by default is one that gets run by accident.
    """
    data = store or load_store(path)
    found = promotion_candidates(strong_score, data_dir)
    existing = set(data.get('entries', {}))
    headroom = max(0, MAX_ACTIVE - len(active_entries(data)))
    fresh = [row for row in found['qualifying'] if row['employer_key'] not in existing]
    already = [row for row in found['qualifying'] if row['employer_key'] in existing]

    cap = headroom if limit <= 0 else min(headroom, int(limit))
    selected, over_cap = fresh[:cap], fresh[cap:]

    result = {
        'schema_version': SCHEMA_VERSION,
        'dry_run': not apply_changes,
        'strong_match_score': int(strong_score),
        'max_active': MAX_ACTIVE,
        'active_before': len(active_entries(data)),
        'headroom': headroom,
        'examined': len(found['qualifying']) + len(found['rejected']),
        'qualifying': found['qualifying'],
        'already_watched': [r['employer_key'] for r in already],
        'would_add' if not apply_changes else 'added': selected,
        'not_added_over_cap': [r['employer_key'] for r in over_cap],
        'rejected': found['rejected'],
        'backup': '',
        'note': (
            'Dry run. Nothing was written. Re-run with --apply to change the private '
            'watchlist.' if not apply_changes else ''),
    }
    if not selected and not already:
        result['empty_note'] = (
            'No employer currently meets the evidence bar, so the watchlist stays '
            'honestly empty. That is the correct outcome, not a defect: the bar is '
            'not lowered to make the count non-zero. Entries arrive after a run, '
            'when a role ranks at or above the strong-match score, when an ATS '
            'tenant is resolved, or when an employer recurs across searches. Re-run '
            'this command after the next /scrape.')
    if not apply_changes:
        return result

    for row in selected:
        add(row['canonical_name'], row['reason'], priority=row['priority'],
            ats_platform=row['ats_platform'], ats_tenant=row['ats_tenant'],
            careers_url=row['careers_url'], evidence=row['evidence'],
            store=data, path=path)
    result['active_after'] = len(active_entries(data))
    return result


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_add(args):
    print(json.dumps(add(args.employer, args.reason, priority=args.priority,
                         ats_platform=args.ats_platform, ats_tenant=args.ats_tenant,
                         careers_url=args.careers_url,
                         check_interval_days=args.check_interval_days, notes=args.notes,
                         evidence=args.evidence),
                     indent=2, ensure_ascii=False))


def cmd_validate(args):
    problems = store_problems(load_store())
    print(json.dumps({'watchlist': STORE.relative_to(ROOT).as_posix()
                      if STORE.is_relative_to(ROOT) else str(STORE),
                      'valid': not problems, 'problems': problems,
                      'max_active': MAX_ACTIVE}, indent=2, ensure_ascii=False))
    raise SystemExit(1 if problems else 0)


def cmd_seed(args):
    """Promotion pass. Dry run unless --apply, and it backs up before writing."""
    backup = ''
    if args.apply and STORE.exists():
        stamp = datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')
        backup = STORE.with_name(f'{STORE.stem}.backup-{stamp}.json')
        backup.write_text(STORE.read_text(encoding='utf-8'), encoding='utf-8')
    result = seed(strong_score=args.strong_score, apply_changes=args.apply,
                  limit=args.limit)
    if backup:
        result['backup'] = (backup.relative_to(ROOT).as_posix()
                            if backup.is_relative_to(ROOT) else str(backup))
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_record_failure(args):
    print(json.dumps(record_failure(args.employer, on=args.on),
                     indent=2, ensure_ascii=False))


def cmd_list(args):
    data = load_store()
    rows = []
    for key, entry in sorted(data.get('entries', {}).items()):
        if args.active_only and not is_enabled(entry):
            continue
        rows.append({'employer_key': key, **{f: entry.get(f) for f in FIELDS if f in entry},
                     'due': is_due(entry, args.on)})
    print(json.dumps({'count': len(rows), 'active': len(active_entries(data)),
                      'max_active': MAX_ACTIVE, 'entries': rows}, indent=2, ensure_ascii=False))


def cmd_due(args):
    rows = due(limit=args.limit, on=args.on)
    print(json.dumps({'count': len(rows), 'limit': args.limit or 0, 'due': rows},
                     indent=2, ensure_ascii=False))


def cmd_disable(args):
    print(json.dumps(set_enabled(args.employer, False), indent=2, ensure_ascii=False))


def cmd_enable(args):
    print(json.dumps(set_enabled(args.employer, True), indent=2, ensure_ascii=False))


def cmd_remove(args):
    print(json.dumps(remove(args.employer), ensure_ascii=False))


def cmd_mark_checked(args):
    print(json.dumps(mark_checked(args.employer, on=args.on), ensure_ascii=False))


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
    p = argparse.ArgumentParser(description='Private bounded employer watchlist')
    sub = p.add_subparsers(dest='cmd', required=True)

    a = sub.add_parser('add', help='Add or update one watched employer.')
    a.add_argument('employer')
    a.add_argument('--reason', required=True, choices=REASONS)
    a.add_argument('--priority', type=int, default=2, choices=PRIORITIES)
    a.add_argument('--ats-platform', dest='ats_platform', default='')
    a.add_argument('--ats-tenant', dest='ats_tenant', default='')
    a.add_argument('--careers-url', dest='careers_url', default='')
    a.add_argument('--check-interval-days', dest='check_interval_days', type=int,
                   default=DEFAULT_CHECK_INTERVAL_DAYS)
    a.add_argument('--notes', default='')
    a.add_argument('--evidence', default='',
                   help='What put this employer here, in checkable words.')
    a.set_defaults(func=cmd_add)

    l = sub.add_parser('list', help='List watchlist entries.')
    l.add_argument('--active-only', dest='active_only', action='store_true')
    l.add_argument('--on', default='')
    l.set_defaults(func=cmd_list)

    d = sub.add_parser('due', help='Watched employers worth checking now.')
    d.add_argument('--limit', type=int, default=0)
    d.add_argument('--on', default='')
    d.set_defaults(func=cmd_due)

    di = sub.add_parser('disable', help='Disable one entry without losing its history.')
    di.add_argument('employer')
    di.set_defaults(func=cmd_disable)

    en = sub.add_parser('enable', help='Re-enable one disabled entry.')
    en.add_argument('employer')
    en.set_defaults(func=cmd_enable)

    rm = sub.add_parser('remove', help='Remove one entry entirely.')
    rm.add_argument('employer')
    rm.set_defaults(func=cmd_remove)

    mc = sub.add_parser('mark-checked', help='Record that this employer was just checked.')
    mc.add_argument('employer')
    mc.add_argument('--on', default='')
    mc.set_defaults(func=cmd_mark_checked)

    v = sub.add_parser('validate', help='Structural check of the whole watchlist.')
    v.set_defaults(func=cmd_validate)

    sd = sub.add_parser('seed', help='Promote qualifying employers. Dry run by default.')
    sd.add_argument('--apply', action='store_true',
                    help='Write the changes. Without this nothing is written.')
    sd.add_argument('--strong-score', dest='strong_score', type=int, default=70,
                    help='Ranked score at or above which a past match qualifies.')
    sd.add_argument('--limit', type=int, default=0, help='Cap additions this pass.')
    sd.set_defaults(func=cmd_seed)

    rf = sub.add_parser('record-failure', help='Record one failed employer check.')
    rf.add_argument('employer')
    rf.add_argument('--on', default='', help='ISO date to record against.')
    rf.set_defaults(func=cmd_record_failure)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

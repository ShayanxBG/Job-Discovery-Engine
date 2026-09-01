#!/usr/bin/env python3
"""Private per-run discovery metadata.

One run record per `/scrape`, stored under `job_scraper/runs/` and gitignored. It
exists so a collapsed source can never look healthy by accident.

The load-bearing distinction is between a source that genuinely held nothing and a
source that broke. `empty` is market supply. `changed_layout`, `blocked_captcha`,
`timeout` and the rest are missing coverage.

Coverage is then judged by inventory FAMILY, because that is what actually holds
the vacancies. CWJobs and Totaljobs share one StepStone inventory, so CWJobs
succeeding while Totaljobs breaks means the inventory was searched: the run is
COMPLETE_WITH_WARNINGS, the Totaljobs failure stays visible as a source warning,
and normal fresh-first widening is unaffected. A family where nothing completed is
a genuine gap, the run is PARTIAL, and its thin candidate pool must never be read
as a thin market.

Completion is three separate questions, so no single boolean has to answer all of
them and a fully covered run can never look incomplete:

  finished                  Did the discovery cycle close? bool(finished_at).
  family_coverage_complete  Was every attempted inventory family seen? No gaps.
  coverage_status           COMPLETE, COMPLETE_WITH_WARNINGS or PARTIAL.
  complete                  Compatibility answer, aligned with family coverage:
                            finished AND coverage_status is not PARTIAL. So
                            COMPLETE and COMPLETE_WITH_WARNINGS are both complete,
                            and an unfinished run never is, however healthy the
                            families observed so far look.

Run records hold coverage and counts only. They never contain candidate profile
text, CV content, credentials, cookies or browser session data.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import atomic_write_text  # noqa: E402
from sources import (  # noqa: E402
    COMPLETE_OUTCOMES, FAILED_OUTCOMES, SOURCE_OUTCOMES, family_coverage,
    is_known_source, load_registry, source_family,
)
from search_strategy import is_known_family, load_strategy  # noqa: E402
from search_plan import run_progress  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / 'job_scraper' / 'runs'
SCHEMA_VERSION = 1

MODES = ('deep', 'daily', 'initial_catchup', 'catchup', 'exhaustive', 'broad',
         'quick', 'gapfill', 'linkedin', 'browser', 'public', 'window', 'health')

# What each run counter MEANS. These exist because the first real run produced a
# report whose funnel could not add up: `raw` was reported as the canonical unique
# count while `hard_filtered` silently mixed three different rejection stages, so
# raw - hard_filtered no longer equalled anything real. A counter without a
# definition is a counter two readers will define differently.
#
# The pre-deep counters form a PARTITION of `raw`: every canonical candidate
# leaves the cheap pipeline through exactly one of them, so they must sum to it.
COUNT_DEFINITIONS = {
    'raw': 'CANONICAL UNIQUE candidates considered, after intra-source repeat '
           'sightings and source artefacts were removed. Not the number of result '
           'rows scraped; record that in the per-source notes.',
    'duplicates': 'Canonical candidates already present in discovery state.',
    'hard_filtered': 'Canonical candidates rejected by the CHEAP deterministic '
                     'gates before any posting was opened. Rejections that needed '
                     'the posting body are NOT counted here; they are deep-checked '
                     'candidates that did not become leads.',
    'suppressed': 'Canonical candidates skipped because a live suppression record '
                  'still applied at check time.',
    'deep_checked': 'Candidates whose own posting was opened and read.',
    'deferred': 'Candidates that survived the cheap gates but were not deep '
                'checked in this run, because the deep budget or prioritisation '
                'stopped short of them. Lost depth, not rejection.',
    'candidates': 'ACTIVE new leads written to state by this run, after any '
                  'post-write dismissal.',
    'new_direct': 'Active new leads with lead_type direct.',
    'agency': 'Active new leads with lead_type agency.',
    'verification': 'Active new leads with lead_type verification.',
    'updated': 'Previously seen vacancies resurfaced with a material improvement.',
}

# raw is partitioned by these four outcomes.
COUNT_PARTITION = ('hard_filtered', 'duplicates', 'suppressed', 'deep_checked',
                   'deferred')
# The lead-type breakdown must account for the active leads exactly.
COUNT_LEAD_TYPES = ('new_direct', 'agency', 'verification')


def reconcile_counts(counts):
    """Return the list of funnel identities this count set violates.

    Deliberately silent when the counters needed for an identity are absent, so a
    historical run recorded before a counter existed still renders.
    """
    problems = []
    have = lambda *k: all(counts.get(x) is not None for x in k)  # noqa: E731

    if have('raw', *COUNT_PARTITION):
        total = sum(int(counts[k]) for k in COUNT_PARTITION)
        if total != int(counts['raw']):
            parts = ' + '.join(f'{k}={counts[k]}' for k in COUNT_PARTITION)
            problems.append(
                f'raw={counts["raw"]} but the pre-deep partition sums to {total} '
                f'({parts}). Every canonical candidate must leave the cheap '
                f'pipeline through exactly one of those outcomes.')

    if have('candidates', *COUNT_LEAD_TYPES):
        total = sum(int(counts[k]) for k in COUNT_LEAD_TYPES)
        if total != int(counts['candidates']):
            parts = ' + '.join(f'{k}={counts[k]}' for k in COUNT_LEAD_TYPES)
            problems.append(
                f'candidates={counts["candidates"]} but the lead types sum to '
                f'{total} ({parts}).')

    if have('candidates', 'deep_checked') and int(counts['candidates']) > int(counts['deep_checked']):
        problems.append(
            f'candidates={counts["candidates"]} exceeds deep_checked='
            f'{counts["deep_checked"]}. A lead cannot be written without its '
            f'posting having been read.')
    return problems

# RETIRED in Phase 4. These were the fresh-first widening thresholds: fewer than
# six new Direct matches in 24 hours widened to 7 days, fewer than four widened
# again to 14. The rule inferred a coverage failure from a quiet market, and it
# spent three query budgets covering one window three times. Window selection now
# lives in tools/search_window.py and reads RUN HISTORY, never yield.
#
# The table stays for one reason only: a run recorded before this change stored
# the threshold it was judged against, and a historical record must keep meaning
# what it meant when it was written. Nothing NEW is judged against it. `summarise`
# reports it only for a run that recorded one.
LEGACY_WIDENING_THRESHOLDS = {'24h': 6, '7d': 4, '14d': 0}


# --------------------------------------------------------------------------
# One active production parent. The ATS ledger, the run counters and the seen
# state are all safe under single-parent write ownership, and nothing was
# enforcing that a second parent could not start. Two `begin` calls succeeded and
# produced two open run records, each with its own ATS ledger, so two runs could
# together make twice the ceiling in external employer checks while each ledger
# reconciled perfectly. A guarantee nobody enforces is an assumption.
# --------------------------------------------------------------------------

ACTIVE_LOCK = RUN_DIR / '.active-run.json'

# A production run that has been open longer than this is treated as ABANDONED
# rather than active, but it is never discarded silently: taking it over is an
# explicit act that leaves a record. Generous, because a catch-up sweep across
# fourteen days of inventory is legitimately slow and killing it would be worse
# than waiting.
STALE_LOCK_HOURS = 6

# Modes that must hold the lock. `health` searches nothing and writes no state,
# so it stays usable while a production run is in flight; blocking it would make
# the one command you want during a stuck run the one you cannot use.
LOCKED_MODES = tuple(m for m in ('deep', 'daily', 'initial_catchup', 'catchup',
                                 'exhaustive', 'broad',
                                 'gapfill', 'linkedin', 'browser', 'public',
                                 'window', 'quick'))


def read_lock():
    if not ACTIVE_LOCK.exists():
        return None
    try:
        data = json.loads(ACTIVE_LOCK.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        # An unreadable lock is a held lock, not an absent one. Assuming absent
        # would let corruption be the way past the guard.
        return {'run_id': '', 'mode': '', 'started_at': '', 'unreadable': True}
    return data if isinstance(data, dict) else None


def lock_age_hours(lock, now=None):
    stamp = str((lock or {}).get('started_at') or '').strip()
    if not stamp:
        return None
    try:
        started = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if not started.tzinfo:
        started = started.astimezone()
    now = now or datetime.now().astimezone()
    return round((now - started).total_seconds() / 3600.0, 2)


def lock_status(now=None):
    """Whether a production run holds the lock, and whether it looks abandoned.

    A lock pointing at a run that has already FINISHED is not held. `finish`
    releases it, so this only happens after a crash between the two writes, and
    a pointer to a closed run is stale bookkeeping rather than a live claim on
    the workspace. Treating it as held would deadlock on a run that is over.
    """
    lock = read_lock()
    if lock is not None and lock.get('unreadable'):
        # Fail closed. A lock we cannot parse might be a live claim, and
        # treating unparseable as absent would make corruption the way past the
        # guard. It is reported as held and stale so a person can release it.
        return {'held': True, 'stale': True, 'lock': lock, 'age_hours': None,
                'stale_after_hours': STALE_LOCK_HOURS,
                'note': ('The active-run lock exists but could not be read. It is '
                         'treated as HELD: an unparseable lock may be a live claim, '
                         'and assuming otherwise would make corruption a way past '
                         'the guard. Inspect and release it deliberately.')}
    if lock is not None and lock.get('run_id'):
        try:
            if load_run(lock['run_id']).get('finished_at'):
                return {'held': False, 'stale': False, 'lock': None,
                        'age_hours': None,
                        'note': (f'The lock pointed at {lock["run_id"]}, which has '
                                 f'already finished. Treated as released.')}
        except SystemExit:
            # The run record is gone. The lock outlived what it protected, so it
            # is reported as held-and-stale rather than quietly dropped.
            pass
    if lock is None:
        return {'held': False, 'stale': False, 'lock': None, 'age_hours': None,
                'note': 'No production run is active.'}
    age = lock_age_hours(lock, now)
    stale = age is not None and age >= STALE_LOCK_HOURS
    return {
        'held': True, 'stale': stale, 'lock': lock, 'age_hours': age,
        'stale_after_hours': STALE_LOCK_HOURS,
        'note': (
            f'Run {lock.get("run_id", "?")} has held the lock for {age} hours, past '
            f'the {STALE_LOCK_HOURS}-hour staleness threshold. It is probably '
            f'abandoned, but it is NOT discarded automatically: release it '
            f'deliberately with `discovery_run.py release --run-id <id>`, which '
            f'records who released it and when.'
            if stale else
            f'Run {lock.get("run_id", "?")} is active ({age} hours). A second '
            f'production run would share mutable state with it: two ATS ledgers '
            f'could each reconcile while together exceeding the ceiling.'),
    }


def take_lock(run_id, mode, force=False):
    """Acquire the active-run lock ATOMICALLY, or refuse.

    The previous version read the lock and then wrote it. Between those two
    operations any number of processes can also read it and find it absent, so
    ten simultaneous `begin` calls would all have believed themselves the winner
    and ten open run records would exist, each with its own ATS ledger, each
    reconciling perfectly while together spending ten times the ceiling. A guard
    made of a check followed by a write is not a guard.

    `os.open` with O_CREAT|O_EXCL is a single atomic filesystem operation on both
    Windows and POSIX: exactly one caller creates the file and every other gets
    FileExistsError. There is no window between deciding and claiming, because
    they are the same operation.
    """
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    payload = {'run_id': run_id, 'mode': mode, 'started_at': now_iso(),
               'pid': os.getpid()}

    def _claim(extra=None):
        # O_EXCL is the whole mechanism. Written and flushed before the
        # descriptor closes, so no other process can observe a half-created lock
        # and conclude the workspace is free.
        body = json.dumps({**payload, **(extra or {})}, indent=2) + '\n'
        fd = os.open(ACTIVE_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, body.encode('utf-8'))
            os.fsync(fd)
        finally:
            os.close(fd)
        return {**payload, **(extra or {})}

    try:
        return _claim()
    except FileExistsError:
        pass

    # The lock exists. It is only NOT held if it points at a run that already
    # finished, which is stale bookkeeping after a crash between finish's two
    # writes rather than a live claim.
    status = lock_status()
    if not status['held']:
        # Replace it, still atomically: unlink then exclusive-create, so a racing
        # process either wins the create or is refused. It never sees an empty
        # workspace.
        try:
            os.unlink(ACTIVE_LOCK)
        except OSError:
            pass
        try:
            return _claim({'replaced_finished_lock': True})
        except FileExistsError:
            status = lock_status()

    if not force:
        raise run_error(
            'A production discovery run is already active.',
            status.get('note', ''),
            'Finish it, or release it explicitly if it was abandoned:',
            f'  python tools/discovery_run.py release --run-id '
            f'{(status.get("lock") or {}).get("run_id", "<id>")}',
            'Read-only and health commands remain usable while it is held.')

    # A deliberate takeover. Unlink then exclusive-create so two takeovers racing
    # each other still produce exactly one winner.
    previous = (status.get('lock') or {}).get('run_id', '')
    try:
        os.unlink(ACTIVE_LOCK)
    except OSError:
        pass
    try:
        return _claim({'took_over_from': previous, 'took_over_at': now_iso()})
    except FileExistsError:
        raise run_error(
            'Another process took the active-run lock during this takeover.',
            'Exactly one production run may hold it. Re-check with '
            '`discovery_run.py active`.') from None


def release_lock(run_id='', reason=''):
    """Release the lock, leaving an auditable trace of who released what.

    Crash recovery is deliberate rather than automatic. A lock that vanished on
    its own would make the guard unreliable in exactly the situation it exists
    for, and a released run that was actually still running would be worse than
    the deadlock.
    """
    status = lock_status()
    if not status['held']:
        return {'released': False, 'reason': 'no_active_run'}
    held = (status['lock'] or {}).get('run_id', '')
    # Only the OWNER may release during normal completion. A non-owner releasing
    # would let one run hand the workspace to another while still writing to it,
    # which is the state the lock exists to prevent.
    if run_id and held and run_id != held:
        raise run_error(
            f'The active run is {held!r}, not {run_id!r}.',
            'Only the owning run may release its own lock. If the holder is '
            'genuinely abandoned, release it by its own id, deliberately.')
    try:
        ACTIVE_LOCK.unlink()
    except OSError as exc:
        raise run_error(f'The active-run lock could not be released: {exc}') from None
    return {'released': True, 'run_id': held, 'was_stale': status['stale'],
            'age_hours': status['age_hours'], 'reason': reason or 'explicit_release'}


def run_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def safe_run_id(value):
    value = (value or '').strip()
    if not value:
        raise run_error('run_id must not be empty')
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '-', value).strip('-')
    if not cleaned:
        raise run_error('run_id contains no usable characters')
    return cleaned[:120]


def make_run_id(prefix='scrape'):
    return f'{prefix}-{datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")}'


def run_path(run_id):
    return RUN_DIR / f'{safe_run_id(run_id)}.json'


def load_run(run_id):
    path = run_path(run_id)
    if not path.exists():
        raise run_error(
            f'Unknown discovery run: {run_id}',
            f'Expected: {path.relative_to(ROOT).as_posix()}',
            'Start one with: python tools/discovery_run.py begin',
        )
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError) as exc:
        raise run_error(f'Discovery run could not be read: {path}', f'{type(exc).__name__}: {exc}') from None
    except json.JSONDecodeError as exc:
        raise run_error(
            f'Malformed discovery run: {path}',
            f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}',
        ) from None
    if not isinstance(data, dict) or not isinstance(data.get('sources'), list):
        raise run_error(f'Invalid discovery run record: {path}')
    return data


def save_run(data):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(run_path(data['run_id']), json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def run_files():
    if not RUN_DIR.exists():
        return []
    return sorted(RUN_DIR.glob('*.json'))


def latest_run_id():
    files = run_files()
    if not files:
        raise run_error(
            'No discovery runs recorded yet.',
            f'Run records live in {RUN_DIR.relative_to(ROOT).as_posix()}/',
        )
    # run_id embeds a sortable timestamp, so filename order is chronological.
    return files[-1].stem


def query_health(queries):
    """Per-search-family query productivity.

    This is the diagnosis layer the run report exists for. Source coverage answers
    "did we look?"; query coverage answers "did we look for the right things?".
    A run that issued ten variations of one job title has excellent source coverage
    and terrible query coverage, and only this view can tell them apart.
    """
    health = {}
    for entry in queries:
        family = entry.get('search_family') or ''
        if not family:
            continue
        row = health.setdefault(family, {
            'search_family': family, 'queries_attempted': 0, 'queries_completed': 0,
            'queries_failed': 0, 'queries_productive': 0, 'raw_candidates': 0,
            'new_canonical_candidates': 0, 'eligible_after_cheap_filters': 0,
            'deep_checked': 0, 'sources': [], 'outcomes': [],
        })
        outcome = str(entry.get('outcome') or '').strip().lower()
        row['queries_attempted'] += 1
        row['outcomes'].append(outcome)
        if entry.get('source_id'):
            row['sources'].append(entry['source_id'])
        if outcome in FAILED_OUTCOMES:
            row['queries_failed'] += 1
        elif outcome in COMPLETE_OUTCOMES:
            row['queries_completed'] += 1
        for field in ('raw_candidates', 'new_canonical_candidates',
                      'eligible_after_cheap_filters', 'deep_checked'):
            row[field] += max(0, int(entry.get(field, 0) or 0))
        if int(entry.get('new_canonical_candidates', 0) or 0) > 0:
            row['queries_productive'] += 1
    for row in health.values():
        row['sources'] = sorted(set(row['sources']))
        row['outcomes'] = sorted(set(row['outcomes']))
        completed = row['queries_completed']
        row['new_candidates_per_query'] = (
            round(row['new_canonical_candidates'] / completed, 2) if completed else None)
        row['productive'] = row['new_canonical_candidates'] > 0
    return health


def source_health(entries):
    """Split recorded sources into complete coverage and lost coverage."""
    complete, failed = [], []
    for entry in entries:
        row = {'source_id': entry.get('source_id'), 'family': entry.get('source_family'),
               'outcome': entry.get('outcome')}
        if entry.get('outcome') in FAILED_OUTCOMES:
            failed.append(row)
        elif entry.get('outcome') in COMPLETE_OUTCOMES:
            complete.append(row)
    return complete, failed


def family_health(entries):
    """Classify each attempted inventory family by whether its inventory was seen.

    Coverage is a property of the FAMILY, not of every nominal site in it. CWJobs
    and Totaljobs run on one platform and share inventory, so CWJobs succeeding
    while Totaljobs breaks means the StepStone inventory WAS searched. That is a
    source warning, not a coverage gap, and it must not be allowed to look like
    one: treating it as a gap would suppress normal fresh-first widening on the
    strength of a sibling failure that cost no inventory.

    A family with no successful source is a genuine gap. Nothing there was seen.

        covered                every attempted source in the family completed
        covered_with_warnings  at least one completed, at least one failed
        gap                    attempted, nothing completed
    """
    health = {}
    for entry in entries:
        family = entry.get('source_family') or entry.get('source_id') or ''
        if not family:
            continue
        row = health.setdefault(family, {'family': family, 'attempted': [],
                                         'complete': [], 'failed': []})
        source_id = entry.get('source_id')
        row['attempted'].append(source_id)
        if entry.get('outcome') in FAILED_OUTCOMES:
            row['failed'].append(source_id)
        elif entry.get('outcome') in COMPLETE_OUTCOMES:
            row['complete'].append(source_id)
    for row in health.values():
        if not row['complete']:
            row['status'] = 'gap'
        elif row['failed']:
            row['status'] = 'covered_with_warnings'
        else:
            row['status'] = 'covered'
    return health


def _service_view(data):
    """Tier-aware service status for ONE run, from its own recorded queries.

    Kept deliberately narrow: it asks whether THIS run's covering queries
    discharged the critical and rolling obligations the CURRENT policy defines.
    It never re-credits a failed query and never rewrites a recorded outcome; it
    reads the same `coverage_bucket` plus `outcome` pair the ledger reads.
    """
    if not (data.get('queries') or []):
        # No query rows means no bucket evidence to judge. Saying "critical service
        # incomplete" here would be an assertion about work nobody recorded, so the
        # view reports itself inapplicable and callers fall back to the whole-run
        # test rather than inventing a verdict.
        return {'schema_version': 1, 'applicable': False,
                'reason': 'the run recorded no queries, so no bucket evidence exists'}
    try:
        from coverage_ledger import service_report
        report = service_report([data])
        report['applicable'] = True
        return report
    except Exception as exc:                                  # noqa: BLE001
        # A derived view must never take a run record down with it.
        return {'schema_version': 1, 'unavailable': True, 'applicable': False,
                'reason': str(exc)[:200]}


def summarise(data):
    """Derived coverage view. Never stored, always recomputed from source entries."""
    entries = data.get('sources', [])
    complete, failed = source_health(entries)
    attempted_ids = [e.get('source_id') for e in entries if e.get('source_id')]
    complete_ids = [row['source_id'] for row in complete]
    counts = data.get('counts', {})
    windows = data.get('actual_windows_used', []) or []
    window = windows[-1] if windows else data.get('requested_window', '')
    # A run written before Phase 4 carries the window it was judged against and
    # keeps its historical threshold. A run written after it carries none, because
    # no threshold was applied to it.
    legacy = bool(data.get('widening_thresholds_applied'))
    threshold = LEGACY_WIDENING_THRESHOLDS.get(window) if legacy else None
    eligible = int(counts.get('new_direct', 0) or 0)
    health = family_health(entries)
    families_covered = sorted(f for f, row in health.items() if row['status'] != 'gap')
    family_gaps = sorted(f for f, row in health.items() if row['status'] == 'gap')
    warned_families = sorted(f for f, row in health.items()
                             if row['status'] == 'covered_with_warnings')
    forced_partial = bool(data.get('forced_partial'))
    if family_gaps or forced_partial:
        coverage_status = 'PARTIAL'
    elif failed:
        coverage_status = 'COMPLETE_WITH_WARNINGS'
    else:
        coverage_status = 'COMPLETE'
    # A family gap means inventory nobody saw, so a thin pool there is missing
    # coverage rather than a thin market. A failed sibling inside a covered family
    # cost no inventory and must not block ordinary fresh-first widening.
    caveat_gap = bool(family_gaps)
    # Three different questions, kept apart so no single boolean has to answer all
    # of them. `finished` is only about whether the cycle closed. Family coverage is
    # only about whether every attempted inventory was seen. `complete` is the
    # compatibility answer and now means both: the run finished AND its coverage was
    # not partial, so a covered family with a degraded sibling is complete.
    finished = bool(data.get('finished_at'))
    family_coverage_complete = not family_gaps

    # Query coverage is a SEPARATE axis from source coverage. Ten queries across
    # five boards can be one search family, so neither number substitutes for the
    # other and the report always shows both.
    queries = data.get('queries', []) or []
    qhealth = query_health(queries)
    strategy = load_strategy()
    min_families = int(strategy.get('diversity_policy', {}).get('min_families_for_broad_claim', 3))
    search_families_attempted = sorted(qhealth)
    search_families_completed = sorted(f for f, row in qhealth.items()
                                       if row['queries_completed'] > 0)
    search_families_productive = sorted(f for f, row in qhealth.items() if row['productive'])
    progress = run_progress(queries, mode=data.get('mode', 'deep')) if queries else {}
    completed_queries = sum(row['queries_completed'] for row in qhealth.values())
    productive_queries = sum(row['queries_productive'] for row in qhealth.values())
    return {
        'attempted_sources': sorted(set(attempted_ids)),
        'attempted_family_coverage': family_coverage(attempted_ids),
        'complete_family_coverage': family_coverage(complete_ids),
        'families_attempted': len(family_coverage(attempted_ids)),
        'families_complete': len(family_coverage(complete_ids)),
        'family_health': health,
        'families_covered': families_covered,
        'families_covered_with_warnings': warned_families,
        'family_gaps': family_gaps,
        'sources_complete': complete,
        'sources_failed': failed,
        # Every failed source stays individually visible. A covered family never
        # hides the sibling that broke inside it.
        'source_warnings': failed,
        'coverage_status': coverage_status,
        'finished': finished,
        'family_coverage_complete': family_coverage_complete,
        'complete': finished and coverage_status != 'PARTIAL',
        # FOUR separate questions. `coverage_status` above answers only the
        # FULL-INVENTORY one and still goes PARTIAL on any family gap, so an
        # unreachable supplemental site stays visible. The service questions
        # below are tier-aware, so that same site can no longer veto a run whose
        # critical work is complete. See coverage_ledger.service_report.
        'safe_close': {
            'finished': finished,
            'forced_partial': forced_partial,
            'errors': list(data.get('errors') or []),
            'status': ('CLOSED' if finished and not forced_partial
                       else 'NOT_CLOSED'),
        },
        'full_inventory': {
            'status': coverage_status,
            'family_gaps': family_gaps,
            'families_covered': families_covered,
            'families_covered_with_warnings': warned_families,
            'complete': family_coverage_complete,
            'note': ('Every attempted inventory family was seen.'
                     if family_coverage_complete else
                     'Gaps remain and are listed; full inventory is not complete.'),
        },
        'service': _service_view(data),
        'query_coverage': {
            'queries_attempted': len(queries),
            'queries_completed': completed_queries,
            'queries_productive': productive_queries,
            # Audit surface for the bucket-persistence contract. A completed query
            # with no bucket credits nothing, so it must be visible rather than
            # silently absent from the ledger.
            'queries_without_coverage_bucket': sorted(
                q.get('query_id', '') for q in queries
                if not str(q.get('coverage_bucket') or '').strip()),
            'covering_queries_without_coverage_bucket': sorted(
                q.get('query_id', '') for q in queries
                if not str(q.get('coverage_bucket') or '').strip()
                and str(q.get('outcome', '')).strip().lower() in ('ok', 'empty')),
            'queries_failed': sum(row['queries_failed'] for row in qhealth.values()),
            'raw_candidates': sum(row['raw_candidates'] for row in qhealth.values()),
            'new_canonical_candidates': sum(row['new_canonical_candidates']
                                            for row in qhealth.values()),
            'new_candidates_per_query': (
                round(sum(row['new_canonical_candidates'] for row in qhealth.values())
                      / completed_queries, 2) if completed_queries else None),
            'search_families_attempted': search_families_attempted,
            'search_families_completed': search_families_completed,
            'search_families_productive': search_families_productive,
            'search_family_count': len(search_families_attempted),
            'search_families_saturated': progress.get('families_saturated', []),
            'search_families_with_gaps': progress.get('families_with_gaps', []),
            'search_families_budget_exhausted': progress.get('families_budget_exhausted', []),
            'search_family_health': qhealth,
            'stopping_state': progress.get('state', ''),
            # A run may only call its query coverage broad when it genuinely
            # attempted several DISTINCT search families and each completed a query.
            'min_families_for_broad_claim': min_families,
            'broad_query_coverage': len(search_families_completed) >= min_families,
            'diversity_note': (
                f'{len(search_families_completed)} search family/families completed a '
                f'query. Query coverage counts SEARCH families, not sites: several '
                f'variations of one job title remain one family however many boards '
                f'they were run against.'
            ),
        },
        'window_selection': {
            'window': window,
            'decision': (data.get('window_decision') or {}).get('decision', ''),
            'reason': (data.get('window_decision') or {}).get('reason', ''),
            'capped': bool((data.get('window_decision') or {}).get('capped')),
            'selected_from': 'run_history',
            'yield_considered': False,
            'note': (
                'The window was chosen from run history. A low result count is market '
                'supply and never widens a window: re-searching a fortnight cannot '
                'conjure a vacancy nobody posted.'),
        },
        'widening': {
            'window': window,
            'eligible_new_direct': eligible,
            'threshold': threshold,
            'threshold_met': None if threshold is None else eligible >= threshold,
            'retired': not legacy,
            'retired_note': (
                'Yield-based widening is retired. This count is reported so a thin '
                'day is visible, and it changes nothing about the window.'
            ) if not legacy else '',
            'excluded_from_threshold': {
                'agency': int(counts.get('agency', 0) or 0),
                'verification': int(counts.get('verification', 0) or 0),
                'updated': int(counts.get('updated', 0) or 0),
                'suppressed': int(counts.get('suppressed', 0) or 0),
            },
            'source_health_caveat': caveat_gap,
            'family_gaps': family_gaps,
            'caveat': (
                'A source family was attempted and no source in it completed, so a '
                'small eligible pool may be missing coverage rather than a thin '
                f'market. Unseen inventory families: {", ".join(family_gaps)}. '
                'Resolve source health before treating a thin result pool as a '
                'market signal.'
            ) if caveat_gap else '',
            'source_warning_note': (
                'A source failed inside a family that another source covered, so the '
                f'inventory was still searched. Degraded sources: '
                f'{", ".join(r["source_id"] for r in failed)}. This is a source '
                'warning, not a coverage gap, and creates no gap-fill work.'
            ) if (failed and not caveat_gap) else '',
        },
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_begin(args):
    mode = (args.mode or 'deep').strip().lower()
    if mode not in MODES:
        raise run_error(f'Invalid --mode: {args.mode!r}', f'Allowed values: {", ".join(MODES)}')
    run_id = make_run_id()
    # Taken BEFORE anything is written. A refused run therefore leaves no run
    # record, no ATS ledger and no trace: it did not start, so nothing about it
    # should exist for the next reader to interpret.
    lock = take_lock(run_id, mode, force=bool(getattr(args, 'takeover', False))) \
        if mode in LOCKED_MODES else None
    data = {
        'schema_version': SCHEMA_VERSION,
        'run_id': run_id,
        'mode': mode,
        'requested_window': (args.requested_window or '').strip(),
        'actual_windows_used': [],
        # Why this window, in the run's own record, so the choice is auditable
        # after the fact rather than only at the moment it was made.
        'window_decision': {},
        'rotation': {},
        'employer_ats': {},
        'sponsorship_checks': {},
        'started_at': now_iso(),
        'finished_at': '',
        'forced_partial': False,
        'sources': [],
        'queries': [],
        'counts': {},
        'errors': [],
        'warnings': [],
    }
    save_run(data)
    print(json.dumps({
        'run_id': data['run_id'],
        'mode': data['mode'],
        'requested_window': data['requested_window'],
        'path': run_path(data['run_id']).relative_to(ROOT).as_posix(),
        'active_run_lock': bool(lock),
        'took_over_from': (lock or {}).get('took_over_from', ''),
    }, ensure_ascii=False))


def cmd_source(args):
    registry = load_registry()
    source_id = (args.source_id or '').strip()
    if not is_known_source(source_id, registry):
        raise run_error(
            f'Unknown source id: {source_id!r}',
            'Every searched source must be defined in config/sources.json.',
            'List them with: python tools/sources.py list',
        )
    outcome = (args.outcome or '').strip().lower()
    if outcome not in SOURCE_OUTCOMES:
        raise run_error(
            f'Invalid --outcome: {args.outcome!r}',
            f'Allowed values: {", ".join(SOURCE_OUTCOMES)}',
            'Note that "empty" means the source genuinely held nothing. A source that '
            'broke must use its real failure outcome so lost coverage stays visible.',
        )
    data = load_run(args.run_id)
    entry = {
        'source_id': source_id,
        'source_family': source_family(source_id, registry),
        'outcome': outcome,
        'authenticated': bool(args.authenticated),
        'window': (args.window or '').strip(),
        'searched': max(0, int(args.searched or 0)),
        'candidates': max(0, int(args.candidates or 0)),
        'notes': (args.notes or '').strip(),
        'warnings': [w.strip() for w in (args.warning or []) if w.strip()],
        'recorded_at': now_iso(),
    }
    # Re-recording a source replaces its previous entry so one source cannot be
    # counted twice, while the final outcome stays the one that actually happened.
    data['sources'] = [e for e in data['sources'] if e.get('source_id') != source_id]
    data['sources'].append(entry)
    data['sources'].sort(key=lambda e: (e.get('source_family', ''), e.get('source_id', '')))
    save_run(data)
    print(json.dumps({'run_id': data['run_id'], 'recorded': entry,
                      'sources_recorded': len(data['sources'])}, ensure_ascii=False))


_REQUIRED_BUCKET_INDEX = None

# A planner-issued query id is `{search_family}-{hash}`. A hand-written id in a
# test or a manual replay is not, and must not be forced into that shape.
PLANNER_QUERY_ID = re.compile(r'^(?P<family>[a-z][a-z0-9-]*)-[0-9a-f]{6,}$')


def required_bucket_index():
    """{(inventory_family, search_family): {term_cluster, ...}} for owed buckets.

    The authority for whether a query was MANDATORY. Built from the same
    `coverage_ledger.required_universe()` the ledger and the planner use, so a
    bucket cannot be mandatory here and optional there.
    """
    global _REQUIRED_BUCKET_INDEX
    if _REQUIRED_BUCKET_INDEX is None:
        from coverage_ledger import required_universe
        index = {}
        for row in required_universe().values():
            index.setdefault((row['inventory_family'], row['search_family']),
                             set()).add(row['term_cluster'])
        _REQUIRED_BUCKET_INDEX = index
    return _REQUIRED_BUCKET_INDEX


def validate_coverage_bucket(bucket, query_id, source_id, search_family,
                             inventory_family, window):
    """Return the bucket to persist, or raise. Never silently drops one.

    The bucket the planner assigned is the only thing `coverage_ledger` can
    credit, so a query that omits it for a MANDATORY obligation has not recorded
    its coverage at all. That is the defect this gate exists to make impossible:
    it fails closed rather than writing a row that reads as searched and credits
    nothing.

    A bucket is accepted only when it agrees with the query it claims to
    describe: the source's own inventory family, the recorded search family, the
    planner's query id where the id is planner-shaped, a term cluster the
    required universe actually declares, and a stated window.
    """
    bucket = (bucket or '').strip()
    index = required_bucket_index()
    mandatory = (inventory_family, search_family) in index

    if not bucket:
        if mandatory:
            raise run_error(
                f'--coverage-bucket is required for this query: '
                f'{inventory_family}::{search_family} is a MANDATORY obligation.',
                'Pass the coverage_bucket the plan assigned to this query task.',
                'Without it coverage_ledger.checkpoints() can credit nothing, so the '
                'query would read as searched while advancing no checkpoint.',
                'Find it with: python tools/search_plan.py plan --mode <mode> '
                '--window <window>',
            )
        return ''

    parts = bucket.split('::')
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise run_error(
            f'Malformed --coverage-bucket: {bucket!r}',
            'The bucket key is {inventory_family}::{search_family}::{term_cluster}.',
        )
    fam, sfam, cluster = (p.strip() for p in parts)

    if fam != inventory_family:
        raise run_error(
            f'--coverage-bucket names inventory family {fam!r} but source '
            f'{source_id!r} belongs to {inventory_family!r}.',
            'A bucket may only be credited to the inventory its source actually holds.',
        )
    if sfam != search_family:
        raise run_error(
            f'--coverage-bucket names search family {sfam!r} but the query recorded '
            f'--search-family {search_family!r}.',
            'Searching one family never covers another.',
        )
    planner = PLANNER_QUERY_ID.match(query_id or '')
    if planner and planner.group('family') != search_family:
        raise run_error(
            f'--query-id {query_id!r} was issued for search family '
            f'{planner.group("family")!r}, which disagrees with --search-family '
            f'{search_family!r}.',
            'The bucket must describe the query the id names.',
        )
    if mandatory and cluster not in index[(fam, search_family)]:
        raise run_error(
            f'Unknown term cluster {cluster!r} for mandatory bucket '
            f'{fam}::{search_family}.',
            f'Declared clusters: {", ".join(sorted(index[(fam, search_family)]))}',
            'A cluster the required universe does not declare cannot discharge an '
            'obligation it does not name.',
        )
    if not (window or '').strip():
        raise run_error(
            '--window is required whenever --coverage-bucket is supplied.',
            'A checkpoint records WHICH interval was searched. Crediting a bucket '
            'without the window it covered would claim an interval nobody stated.',
        )
    return bucket


def cmd_query(args):
    """Record one executed query's coverage and yield.

    A query row is deliberately counts-only. It never carries candidate profile
    text, candidate rows, or advert bodies: the run log answers how much was
    searched and how much came back, not what any of it said.
    """
    registry = load_registry()
    strategy = load_strategy()
    source_id = (args.source_id or '').strip()
    if not is_known_source(source_id, registry):
        raise run_error(
            f'Unknown source id: {source_id!r}',
            'Every searched source must be defined in config/sources.json.',
            'List them with: python tools/sources.py list',
        )
    family = (args.search_family or '').strip()
    if not is_known_family(family, strategy):
        raise run_error(
            f'Unknown search family: {family!r}',
            'Every recorded query must belong to a family in config/search_strategy.json.',
            'List them with: python tools/search_strategy.py list',
        )
    outcome = (args.outcome or '').strip().lower()
    if outcome not in SOURCE_OUTCOMES:
        raise run_error(
            f'Invalid --outcome: {args.outcome!r}',
            f'Allowed values: {", ".join(SOURCE_OUTCOMES)}',
            'A query whose source broke is lost coverage, never zero yield, so it '
            'must record its real failure outcome rather than "empty".',
        )
    data = load_run(args.run_id)
    inventory_family = source_family(source_id, registry)
    query_id = (args.query_id or '').strip()
    bucket = validate_coverage_bucket(
        args.coverage_bucket, query_id, source_id, family, inventory_family,
        args.window)
    subsumes = sorted({s.strip() for s in (args.subsumes or []) if s.strip()})
    entry = {
        'query_id': query_id,
        'search_family': family,
        'source_id': source_id,
        'source_family': inventory_family,
        'coverage_bucket': bucket,
        'subsumes': subsumes,
        'dedup_key': (args.dedup_key or '').strip(),
        'attempted': True,
        'outcome': outcome,
        'window': (args.window or '').strip(),
        'raw_candidates': max(0, int(args.raw_candidates or 0)),
        'new_canonical_candidates': max(0, int(args.new_canonical or 0)),
        'eligible_after_cheap_filters': max(0, int(args.eligible or 0)),
        'deep_checked': max(0, int(args.deep_checked or 0)),
        'warnings': [w.strip() for w in (args.warning or []) if w.strip()],
        'recorded_at': now_iso(),
    }
    if not entry['query_id']:
        raise run_error('A recorded query needs a --query-id.',
                        'Use the query_id from the plan so coverage stays traceable.')
    data.setdefault('queries', [])
    data['queries'] = [q for q in data['queries'] if q.get('query_id') != entry['query_id']]
    data['queries'].append(entry)
    data['queries'].sort(key=lambda q: (q.get('search_family', ''), q.get('query_id', '')))
    save_run(data)
    print(json.dumps({'run_id': data['run_id'], 'recorded': entry,
                      'queries_recorded': len(data['queries'])}, ensure_ascii=False))


def _json_flag(raw, label):
    text = str(raw or '').strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise run_error(f'--{label} is not valid JSON.',
                        f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None
    if not isinstance(value, dict):
        raise run_error(f'--{label} must be a JSON object.')
    return value


def ats_problems(data):
    """Every reason this run's employer ATS ledger cannot be trusted.

    A run recorded before the ledger existed has none, and is silently fine: it
    is not evidence of a breach, only of an older schema. That is what keeps
    historical runs readable.
    """
    import ats_budget
    if not ats_budget.has_ledger(data):
        return []
    ledger = data['employer_ats']
    problems = list(ats_budget.reconcile(ledger))
    made = int(ledger['counts'].get('attempted', 0) or 0)
    ceiling = int(ledger.get('ceiling', 0) or 0)
    if ceiling and made > ceiling:
        problems.append({
            'problem': 'checks_made_exceeds_ceiling', 'attempted': made,
            'ceiling': ceiling,
            'detail': 'Capacity is reserved BEFORE each external check, so a run '
                      'cannot reach this state without the reservation gate having '
                      'been bypassed.'})
    return problems


def cmd_finish(args):
    data = load_run(args.run_id)
    windows = [w.strip() for w in (args.windows or '').split(',') if w.strip()]
    if windows:
        data['actual_windows_used'] = windows
    for label, field in (('window-decision', 'window_decision'),
                         ('rotation', 'rotation'),
                         ('employer-ats', 'employer_ats'),
                         ('sponsorship-checks', 'sponsorship_checks')):
        value = _json_flag(getattr(args, field, ''), label)
        if value:
            data[field] = value
    counts = dict(data.get('counts', {}))
    for field in ('raw', 'duplicates', 'hard_filtered', 'suppressed', 'deep_checked',
                  'deferred', 'candidates', 'new_direct', 'updated', 'agency',
                  'verification'):
        value = getattr(args, field, None)
        if value is not None:
            counts[field] = max(0, int(value))

    # `candidates` is DERIVED, not optional. It is definitionally the sum of the
    # lead types, so leaving it out cannot be a way to opt out of reconciliation:
    # without it both lead-type identities go unchecked, and a run claiming nine
    # leads written from four postings read would close cleanly and store a funnel
    # that contradicts itself. Omitting one CLI flag must never bypass an invariant.
    lead_counts = [counts.get(field) for field in COUNT_LEAD_TYPES]
    if counts.get('candidates') is None and all(v is not None for v in lead_counts):
        counts['candidates'] = sum(int(v) for v in lead_counts)
        derived_candidates = True
    else:
        derived_candidates = False
    # If it could not be derived either, a run that reports deep work owes the
    # number rather than being excused the check.
    if (counts.get('candidates') is None and counts.get('deep_checked') is not None
            and not args.allow_unreconciled):
        raise run_error(
            'This run reports deep-checked candidates but no lead count, so its '
            'funnel cannot be reconciled.',
            'candidates = new_direct + agency + verification, and candidates must never '
            'exceed deep_checked. Neither identity can be checked without them.',
            'Pass --candidates, or pass --new-direct, --agency and --verification so it '
            'can be derived.',
            'Counter meanings: python tools/discovery_run.py definitions',
        )

    problems = reconcile_counts(counts)
    if problems and not args.allow_unreconciled:
        raise run_error(
            'These counts do not reconcile, so the run report would contradict itself.',
            *problems,
            'Counter meanings: python tools/discovery_run.py definitions',
            'Fix the counts rather than the identity. Pass --allow-unreconciled only '
            'to close a run whose true numbers genuinely cannot be recovered, which '
            'records the discrepancy as a warning instead of hiding it.',
        )
    if problems:
        data['warnings'] = list(data.get('warnings', [])) + [
            'COUNTS DO NOT RECONCILE: ' + p for p in problems]
    data['counts'] = counts
    # The ATS ledger reconciles on the same terms as the funnel: a run that spent
    # more external employer checks than its mode allows may not close cleanly.
    _ats = ats_problems(data)
    if _ats and not args.allow_unreconciled:
        raise run_error(
            'This run cannot close: its employer ATS ledger does not reconcile.',
            *[json.dumps(problem, ensure_ascii=False) for problem in _ats[:4]],
            'The ceiling is an execution boundary, not a reporting field. Reserve '
            'capacity with tools/ats_budget.py before each check.',
            'Pass --allow-unreconciled only to store a run you know is wrong.')
    if _ats:
        data.setdefault('warnings', []).append(
            'employer ATS ledger stored UNRECONCILED: '
            + '; '.join(p.get('problem', '') for p in _ats))
        data['employer_ats']['unreconciled'] = True
    # Re-finishing a run to correct its counters must not duplicate the narrative.
    # Order is preserved so the first time a warning was raised stays first.
    def merge(existing, incoming):
        out, seen = [], set()
        for item in list(existing) + [x.strip() for x in (incoming or []) if x.strip()]:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    data['errors'] = merge(data.get('errors', []), args.error)
    data['warnings'] = merge(data.get('warnings', []), args.warning)
    data['forced_partial'] = bool(args.partial)
    data['finished_at'] = now_iso()
    # A completed run always releases the lock, including one closed with
    # --allow-unreconciled: an unreconciled run is finished, and leaving the lock
    # held would turn a reporting problem into a deadlock.
    try:
        _held = read_lock()
        if _held and _held.get('run_id') == data.get('run_id'):
            release_lock(data['run_id'], reason='run_finished')
    except SystemExit:
        pass
    save_run(data)
    print(json.dumps({'run_id': data['run_id'], 'finished_at': data['finished_at'],
                      'candidates_derived': derived_candidates,
                      'counts': data['counts'],
                      'summary': summarise(data)}, indent=2, ensure_ascii=False))


def render(data):
    summary = summarise(data)
    lines = [
        f"Discovery run: {data.get('run_id', '')}",
        f"Mode: {data.get('mode', '')} | Requested window: {data.get('requested_window') or 'unspecified'}",
        f"Windows used: {', '.join(data.get('actual_windows_used', [])) or 'none recorded'}",
        f"Started: {data.get('started_at', '')} | Finished: {data.get('finished_at') or 'still open'}",
        f"Run coverage: {summary['coverage_status']}"
        + f" | finished: {'yes' if summary['finished'] else 'no'}"
        + f" | every attempted family covered: {'yes' if summary['family_coverage_complete'] else 'no'}",
        '',
        'Sources:',
    ]
    if not data.get('sources'):
        lines.append('  - none recorded')
    for entry in data.get('sources', []):
        flag = ' <- lost coverage' if entry.get('outcome') in FAILED_OUTCOMES else ''
        lines.append(
            f"  - {entry.get('source_id')} [{entry.get('source_family')}] "
            f"{entry.get('outcome')} | searched {entry.get('searched')} "
            f"| candidates {entry.get('candidates')}{flag}")
        if entry.get('notes'):
            lines.append(f"      {entry['notes']}")
    lines.append('')
    lines.append(f"Families attempted: {summary['families_attempted']} "
                 f"| complete: {summary['families_complete']} "
                 f"| covered: {len(summary['families_covered'])} "
                 f"| gaps: {len(summary['family_gaps'])}")
    for family, ids in summary['attempted_family_coverage'].items():
        status = summary['family_health'].get(family, {}).get('status', 'unknown')
        flag = {'gap': ' <- FAMILY GAP: no source in this family completed',
                'covered_with_warnings': ' <- covered, with a degraded sibling source'}.get(status, '')
        lines.append(f"  - {family} [{status}]: {', '.join(ids)}{flag}")
    if summary['source_warnings']:
        lines.append('')
        lines.append('Source warnings (visible even when the family was covered):')
        for row in summary['source_warnings']:
            lines.append(f"  - {row['source_id']} [{row['family']}] {row['outcome']}")
    # Search-productivity report. Low output has several very different causes and
    # they are only distinguishable side by side: a thin market, a narrow query
    # strategy, a collapsed source, over-filtering, or genuine saturation.
    qcov = summary.get('query_coverage', {})
    if qcov.get('queries_attempted'):
        lines.append('')
        lines.append('Queries:')
        lines.append(f"  attempted: {qcov['queries_attempted']} "
                     f"| completed: {qcov['queries_completed']} "
                     f"| productive: {qcov['queries_productive']} "
                     f"| failed: {qcov['queries_failed']}")
        lines.append(f"  new canonical candidates: {qcov['new_canonical_candidates']} "
                     f"| per completed query: {qcov['new_candidates_per_query']}")
        lines.append(f"  stopping state: {qcov.get('stopping_state') or 'n/a'}")
        if qcov.get('search_families_saturated'):
            lines.append(f"  saturated families: {', '.join(qcov['search_families_saturated'])}")
        if qcov.get('search_families_with_gaps'):
            lines.append(f"  families with lost coverage: "
                         f"{', '.join(qcov['search_families_with_gaps'])}")
        lines.append('')
        lines.append('Search-family yield:')
        for family, row in sorted(qcov.get('search_family_health', {}).items()):
            flag = '' if row['productive'] else '  <- no new candidates'
            lines.append(f"  {family:22s} {row['new_canonical_candidates']} new "
                         f"/ {row['queries_attempted']} queries "
                         f"({row['raw_candidates']} raw, {row['deep_checked']} deep){flag}")
        lines.append(f"  Search families completed: {len(qcov.get('search_families_completed', []))} "
                     f"(broad query coverage needs {qcov.get('min_families_for_broad_claim')}): "
                     f"{'BROAD' if qcov.get('broad_query_coverage') else 'NARROW'}")
        lines.append('  ' + qcov.get('diversity_note', ''))

    counts = data.get('counts', {})
    if counts:
        lines.append('')
        lines.append('Counts: ' + ' | '.join(f'{k}={v}' for k, v in sorted(counts.items())))
    selection = summary['window_selection']
    lines.append('')
    lines.append(f"Window ({selection['window'] or 'unspecified'}): "
                 f"{selection['decision'] or 'not recorded'}, chosen from "
                 f"{selection['selected_from']}")
    if selection['reason']:
        lines.append(f"  {selection['reason']}")
    widening = summary['widening']
    lines.append(f"Eligible NEW direct: {widening['eligible_new_direct']}"
                 + (f" / historical threshold {widening['threshold']}"
                    if widening['threshold'] is not None
                    else '  (reported only; yield never widens a window)'))
    excluded = widening['excluded_from_threshold']
    lines.append('  Excluded from the threshold: '
                 + ', '.join(f'{k} {v}' for k, v in sorted(excluded.items())))
    if widening['source_health_caveat']:
        lines.append('  CAVEAT: ' + widening['caveat'])
    elif widening['source_warning_note']:
        lines.append('  NOTE: ' + widening['source_warning_note'])
    if data.get('errors'):
        lines.append('')
        lines.append('Errors:')
        lines.extend(f'  - {e}' for e in data['errors'])
    if data.get('warnings'):
        lines.append('')
        lines.append('Warnings:')
        lines.extend(f'  - {w}' for w in data['warnings'])
    return '\n'.join(lines)


def cmd_definitions(args):
    print(json.dumps({
        'counter_definitions': COUNT_DEFINITIONS,
        'identities': [
            'raw = ' + ' + '.join(COUNT_PARTITION),
            'candidates = ' + ' + '.join(COUNT_LEAD_TYPES),
            'candidates <= deep_checked',
        ],
        'note': 'The pre-deep counters partition `raw`: every canonical candidate '
                'leaves the cheap pipeline through exactly one of them. A rejection '
                'that required reading the posting body is NOT hard_filtered; it is '
                'a deep-checked candidate that did not become a lead.',
    }, indent=2, ensure_ascii=False))


def cmd_active(args):
    print(json.dumps(lock_status(), indent=2, ensure_ascii=False))


def cmd_release(args):
    print(json.dumps(release_lock(args.run_id, args.reason), indent=2,
                     ensure_ascii=False))


def cmd_show(args):
    if args.all:
        rows = []
        for path in run_files():
            data = json.loads(path.read_text(encoding='utf-8'))
            summary = summarise(data)
            rows.append({
                'run_id': data.get('run_id'),
                'mode': data.get('mode'),
                'started_at': data.get('started_at'),
                'finished_at': data.get('finished_at'),
                'finished': summary['finished'],
                'family_coverage_complete': summary['family_coverage_complete'],
                'complete': summary['complete'],
                'coverage_status': summary['coverage_status'],
                'families_attempted': summary['families_attempted'],
                'family_gaps': summary['family_gaps'],
                'sources_failed': [r['source_id'] for r in summary['sources_failed']],
                'counts': data.get('counts', {}),
            })
        print(json.dumps({'count': len(rows), 'runs': rows}, indent=2, ensure_ascii=False))
        return
    run_id = args.run_id or latest_run_id()
    data = load_run(run_id)
    if args.json:
        print(json.dumps({**data, 'summary': summarise(data)}, indent=2, ensure_ascii=False))
    else:
        print(render(data))


def main():
    p = argparse.ArgumentParser(description='Private per-run discovery coverage log')
    sub = p.add_subparsers(dest='cmd', required=True)

    b = sub.add_parser('begin', help='Start a discovery run record.')
    b.add_argument('--takeover', action='store_true',
                   help='Take over an abandoned active run. Records what it replaced.')
    b.add_argument('--mode', default='deep')
    b.add_argument('--requested-window', dest='requested_window', default='')
    b.set_defaults(func=cmd_begin)

    s = sub.add_parser('source', help='Record one source outcome.')
    s.add_argument('--run-id', dest='run_id', required=True)
    s.add_argument('--source-id', dest='source_id', required=True)
    s.add_argument('--outcome', required=True)
    s.add_argument('--searched', type=int, default=0)
    s.add_argument('--candidates', type=int, default=0)
    s.add_argument('--window', default='')
    s.add_argument('--authenticated', action='store_true')
    s.add_argument('--notes', default='')
    s.add_argument('--warning', action='append', default=[])
    s.set_defaults(func=cmd_source)

    f = sub.add_parser('finish', help='Close a run and record its counts.')
    f.add_argument('--window-decision', dest='window_decision', default='',
                   help='JSON from search_window.py select, recorded as evidence.')
    f.add_argument('--rotation', default='', help='JSON rotation block from the plan.')
    f.add_argument('--employer-ats', dest='employer_ats', default='',
                   help='JSON: checks_made, checks_ceiling, employers_due, checks_failed.')
    f.add_argument('--sponsorship-checks', dest='sponsorship_checks', default='',
                   help='JSON: local_lookups, live_fallbacks.')
    f.add_argument('--run-id', dest='run_id', required=True)
    f.add_argument('--windows', default='', help='Comma separated windows actually searched, e.g. 24h,7d')
    for field in ('raw', 'duplicates', 'hard-filtered', 'suppressed', 'deep-checked',
                  'deferred', 'candidates', 'new-direct', 'updated', 'agency',
                  'verification'):
        f.add_argument(f'--{field}', dest=field.replace('-', '_'), type=int,
                       help=COUNT_DEFINITIONS.get(field.replace('-', '_'), ''))
    f.add_argument('--allow-unreconciled', dest='allow_unreconciled',
                   action='store_true',
                   help='Close the run even though its counts do not reconcile, '
                        'recording the discrepancy as a warning.')
    f.add_argument('--partial', action='store_true',
                   help='Force partial even when every recorded source succeeded.')
    f.add_argument('--error', action='append', default=[])
    f.add_argument('--warning', action='append', default=[])
    f.set_defaults(func=cmd_finish)

    q = sub.add_parser('query', help='Record one executed query and its yield.')
    q.add_argument('--run-id', dest='run_id', required=True)
    q.add_argument('--query-id', dest='query_id', required=True)
    q.add_argument('--search-family', dest='search_family', required=True)
    q.add_argument('--source-id', dest='source_id', required=True)
    q.add_argument('--outcome', required=True)
    q.add_argument('--coverage-bucket', dest='coverage_bucket', default='',
                   help='{inventory_family}::{search_family}::{term_cluster} from '
                        'the plan task. REQUIRED for a mandatory obligation, for '
                        'every outcome including failures, so coverage is auditable.')
    q.add_argument('--subsumes', action='append', default=[],
                   help='A narrower bucket this completed query also searched. '
                        'Declared in the task, never re-derived later.')
    q.add_argument('--dedup-key', dest='dedup_key', default='')
    q.add_argument('--window', default='')
    q.add_argument('--raw-candidates', dest='raw_candidates', type=int, default=0)
    q.add_argument('--new-canonical', dest='new_canonical', type=int, default=0,
                   help='NEW canonical candidates this query contributed.')
    q.add_argument('--eligible', type=int, default=0,
                   help='Candidates surviving the cheap deterministic gates.')
    q.add_argument('--deep-checked', dest='deep_checked', type=int, default=0)
    q.add_argument('--warning', action='append', default=[])
    q.set_defaults(func=cmd_query)

    d = sub.add_parser('definitions',
                       help='Print what each run counter means and the funnel '
                            'identities they must satisfy.')
    d.set_defaults(func=cmd_definitions)

    al = sub.add_parser('active', help='Whether a production run holds the lock.')
    al.set_defaults(func=cmd_active)

    rl = sub.add_parser('release', help='Explicitly release an abandoned active run.')
    rl.add_argument('--run-id', dest='run_id', default='')
    rl.add_argument('--reason', default='')
    rl.set_defaults(func=cmd_release)

    w = sub.add_parser('show', help='Show the latest run, one run, or the run index.')
    w.add_argument('--run-id', dest='run_id', default='')
    w.add_argument('--latest', action='store_true', help='Explicitly select the newest run (the default).')
    w.add_argument('--all', action='store_true')
    w.add_argument('--json', action='store_true')
    w.set_defaults(func=cmd_show)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

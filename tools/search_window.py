#!/usr/bin/env python3
"""Which time window a run searches, decided from RUN HISTORY.

THE BUG THIS REPLACES. The old rule was fresh-first widening: search 24 hours,
count new Direct matches, and if fewer than six were found widen to 7 days, then
if fewer than four widen again to 14 days. It reads as caution and behaves as a
false inference. Yield is market supply. A day on which UK employers posted three
matching backend vacancies is a quiet day, not a day on which the previous window
was searched badly, and re-searching the last fortnight cannot conjure a vacancy
that was never posted. Worse, it spent three query budgets to cover one window
three times, and every extra pass was overwhelmingly rediscovery of records the
deduplicator then threw away. A quiet market was made to look like a coverage
failure, and the cure for a coverage failure was applied to it.

WHAT DECIDES A WINDOW INSTEAD. Exactly one thing: the gap between now and the last
SUCCESSFUL COMPLETED production run. That is the only evidence that says inventory
went unseen, because it is the only fact about what this workspace actually did.

    INITIAL_CATCHUP   no successful completed production run exists, so nothing is
                      known to have been covered. Search the initial catch-up
                      window ONCE, directly, with ordinary deduplication. Not 24h
                      then 7d then 14d: that is the same window searched three
                      times at triple cost.
    DAILY             the gap is 24 hours or less, so the 24-hour window covers
                      it exactly.
    RECOVERY          the gap is longer than a day. Take the SMALLEST supported
                      window that COVERS it, capped at max_recovery_window with
                      the uncovered hours reported as a number.
    EXPLICIT          the user named a window. It is honoured exactly and is never
                      widened, in either direction, for any reason.

THE WINDOW MUST COVER THE GAP, AND THERE IS NO GRACE. An earlier version allowed
twelve hours of slack before calling a day missed, so a gap of 30 hours was
searched with a 24-hour window. Six hours of inventory went unsearched and
nothing anywhere reported it. That is the same invisible coverage failure that
yield-based widening was removed for, arriving through the other door. Overlap
costs nothing, because deduplication absorbs it; a shortfall cannot be recovered,
because nobody knows it happened. So DAILY and RECOVERY are now two labels on ONE
ascending ladder with INCLUSIVE upper bounds, and every returned decision carries
`covers_gap` and `uncovered_hours` computed from the window actually selected.

WHAT A SUCCESSFUL COMPLETED PRODUCTION RUN IS. All three words carry weight.
`finished_at` is set, so the cycle closed. Coverage was not PARTIAL, so no
inventory family was attempted and wholly missed. And the mode was a production
discovery mode: a `health` check searches nothing and a `quick` troubleshooting
sample is not a day's coverage, so neither may reset the clock and hide a real
gap. A partial run is deliberately NOT evidence of coverage. It is closer to
evidence of the opposite.

YIELD IS REPORTED, NEVER ACTED ON. `yield_may_widen_window` is false in the
configuration and this module reads no count of any kind. A thin day is reported
as a thin day.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from search_strategy import load_strategy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1

# Modes whose completion proves a day of production coverage. `health` searches
# nothing at all, and `quick` is documented as troubleshooting rather than the
# daily workflow, so neither can stand in for a real run.
PRODUCTION_MODES = ('deep', 'daily', 'catchup', 'exhaustive', 'broad', 'gapfill',
                    'linkedin', 'browser', 'public', 'window')
NON_PRODUCTION_MODES = ('health', 'quick')

DECISIONS = ('INITIAL_CATCHUP', 'DAILY', 'RECOVERY', 'EXPLICIT')

# The mode whose ceilings each decision should run under. Window and budget are
# separate questions, but a catch-up window with a daily budget would be a plan
# that cannot finish what it set out to search.
DECISION_MODES = {
    # The bootstrap gets its OWN budget, derived from the critical obligations it
    # must discharge. Phase 4E ran it on the ordinary 36-query catch-up budget and
    # funded 30 of 45 critical buckets, which is a contract the budget could not
    # keep.
    'INITIAL_CATCHUP': 'initial_catchup',
    'RECOVERY': 'catchup',
    'DAILY': 'daily',
}


def window_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def window_policy(strategy=None):
    strategy = strategy or load_strategy()
    policy = strategy.get('window_policy')
    if not isinstance(policy, dict):
        raise window_error(
            'config/search_strategy.json has no window_policy block.',
            'Window selection is configuration, not a constant in this module.')
    return policy


def supported_windows(strategy=None):
    return tuple(window_policy(strategy).get('supported_windows', ()) or ())


def window_hours(window, strategy=None):
    hours = window_policy(strategy).get('window_hours', {})
    if window not in hours:
        raise window_error(f'Unknown search window: {window!r}',
                           f'Supported: {", ".join(supported_windows(strategy))}')
    return int(hours[window])


def _parse(stamp):
    """An ISO timestamp, or None. A record we cannot date is a record we cannot use."""
    text = str(stamp or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def is_production_mode(mode):
    mode = str(mode or '').strip().lower()
    return bool(mode) and mode not in NON_PRODUCTION_MODES


def run_is_successful(record, summary=None):
    """Finished, a production mode, not forced partial, and CRITICAL SERVICE complete.

    `summary` is `discovery_run.summarise(record)` when the caller already has it.
    Recomputing coverage here would duplicate the run-accounting authority, so this
    function accepts the derived view rather than re-deriving it.

    WHY THIS IS NO LONGER `coverage_status != PARTIAL`. That test collapsed every
    inventory family into one verdict, so a single unreachable SUPPLEMENTAL site
    made the whole run partial. Production run scrape-20260831T102144228455 covered
    all 33 critical buckets, closed cleanly and held no lock, yet four optional
    families (two blocked by a browser permission, two unable to run a query at
    all) made it PARTIAL. This function then rejected it, `select_window` saw no
    successful run, and the workspace returned INITIAL_CATCHUP. Nothing the
    critical tier did could ever end catch-up while any optional website was down.

    So the test is now the tier-aware one: a run is evidence of coverage when its
    CRITICAL service is complete. Nothing is relaxed. `coverage_status` still goes
    PARTIAL on any family gap and is still reported, `forced_partial` still
    disqualifies a run outright, and an INCOMPLETE critical tier still fails here.
    A supplemental gap is reported, not promoted, and never silently absorbed.
    """
    if not isinstance(record, dict):
        return False
    if not _parse(record.get('finished_at')):
        return False
    if not is_production_mode(record.get('mode')):
        return False
    if record.get('forced_partial'):
        return False
    if summary is None:
        return True
    if not summary.get('finished'):
        return False
    service = summary.get('service') or {}
    # ABSENT is not FAILED. A summary with no service view at all (one built before
    # the tier-aware repair, or by a caller that supplies only coverage fields), a
    # run that recorded no queries, and an unreadable policy are all cases with no
    # bucket evidence to judge. Each falls back to the historical whole-run test,
    # so nothing is invented in either direction and an unreadable policy can never
    # upgrade a run.
    if (not service
            or not service.get('applicable', True)
            or service.get('unavailable')
            or 'critical_service_complete' not in service):
        return summary.get('coverage_status') != 'PARTIAL'
    return bool(service.get('critical_service_complete'))


def last_successful_run(records=None, summaries=None):
    """The most recently FINISHED successful production run, or None.

    Ordered by `finished_at`, not by `started_at` and not by filename: a long
    catch-up begun yesterday and closed today covered up to today.
    """
    records = list(records or [])
    summaries = summaries or {}
    best, best_at = None, None
    for record in records:
        summary = summaries.get(record.get('run_id'))
        if not run_is_successful(record, summary):
            continue
        finished = _parse(record.get('finished_at'))
        if finished and (best_at is None or finished > best_at):
            best, best_at = record, finished
    return best


def _ladder_window(gap_hours, strategy=None):
    """Smallest supported window COVERING the gap, and whether the cap was hit.

    Upper bounds are inclusive, so a gap of exactly 24 hours takes the 24-hour
    window and 24 hours and one minute takes the next rung up. Ties go to the
    smaller window because at the boundary it genuinely covers the interval; one
    minute past it, it genuinely does not.
    """
    policy = window_policy(strategy)
    for rung in policy.get('recovery_ladder') or []:
        if gap_hours <= float(rung.get('max_gap_hours', 0)):
            return str(rung.get('window')), False
    return str(policy.get('max_recovery_window', '14d')), True


def _coverage(window, gap_hours, strategy=None):
    """Does this window cover this gap, and by how much does it fall short?

    Returned on EVERY decision, including the ones that cover comfortably, so a
    shortfall can never be the one case nobody printed.
    """
    covered = window_hours(window, strategy)
    shortfall = round(max(0.0, float(gap_hours) - float(covered)), 2)
    return {
        'window_hours': covered,
        'gap_hours': round(float(gap_hours), 2),
        'covers_gap': shortfall == 0,
        'uncovered_hours': shortfall,
        'uncovered_days': round(shortfall / 24.0, 2) if shortfall else 0.0,
    }


def select_window(records=None, summaries=None, explicit='', now=None, strategy=None):
    """The window this run should search, and the run-history evidence for it.

    Returns a decision object. It never reads a yield count, and there is no
    parameter through which one could be supplied.
    """
    strategy = strategy or load_strategy()
    policy = window_policy(strategy)
    now = _parse(now) or datetime.now().astimezone()

    explicit = str(explicit or '').strip().lower()
    if explicit:
        if explicit not in supported_windows(strategy):
            raise window_error(
                f'Unknown explicit window: {explicit!r}',
                f'Supported: {", ".join(supported_windows(strategy))}')
        return {
            'schema_version': SCHEMA_VERSION,
            'decision': 'EXPLICIT',
            'window': explicit,
            'window_hours': window_hours(explicit, strategy),
            'budget_mode': '',
            'capped': False,
            'coverage': {'window_hours': window_hours(explicit, strategy),
                         'gap_hours': None, 'covers_gap': None,
                         'uncovered_hours': None, 'uncovered_days': None},
            'evidence': {
                'requested_window': explicit,
                'last_successful_run_id': '',
                'last_successful_finished_at': '',
                'hours_since_last_successful_run': None,
                'production_runs_recorded': len(list(records or [])),
            },
            'reason': (
                f'The user named {explicit} explicitly. An explicit window is '
                'honoured exactly and is never widened or narrowed, whatever the '
                'run history or the result count turns out to be.'),
            'yield_considered': False,
        }

    last = last_successful_run(records, summaries)
    total = len(list(records or []))
    if last is None:
        window = str(policy.get('initial_catchup_window', '14d'))
        return {
            'schema_version': SCHEMA_VERSION,
            'decision': 'INITIAL_CATCHUP',
            'window': window,
            'window_hours': window_hours(window, strategy),
            'budget_mode': DECISION_MODES['INITIAL_CATCHUP'],
            'capped': False,
            # Nothing is known to have been covered, so there is no gap to measure
            # against. That is honestly None, not a zero that would read as
            # "measured, and it covered everything".
            'coverage': {'window_hours': window_hours(window, strategy),
                         'gap_hours': None, 'covers_gap': None,
                         'uncovered_hours': None, 'uncovered_days': None},
            'evidence': {
                'requested_window': '',
                'last_successful_run_id': '',
                'last_successful_finished_at': '',
                'hours_since_last_successful_run': None,
                'production_runs_recorded': total,
            },
            'reason': (
                f'No successful completed production run exists, so no inventory is '
                f'known to have been covered. Search {window} ONCE, directly, with '
                f'ordinary deduplication. There is no 24h then 7d then 14d ladder: '
                f'that searches one window three times.'),
            'yield_considered': False,
        }

    finished = _parse(last.get('finished_at'))
    gap_hours = round((now - finished).total_seconds() / 3600.0, 2)
    interval = float(policy.get('daily_interval_hours', 24))
    evidence = {
        'requested_window': '',
        'last_successful_run_id': last.get('run_id', ''),
        'last_successful_finished_at': last.get('finished_at', ''),
        'last_successful_mode': last.get('mode', ''),
        'hours_since_last_successful_run': gap_hours,
        'daily_interval_hours': interval,
        'production_runs_recorded': total,
    }

    window, capped = _ladder_window(gap_hours, strategy)
    coverage = _coverage(window, gap_hours, strategy)
    # One ladder, two labels. DAILY is simply the case where the smallest window
    # already covers the gap; it is not a separate branch with its own tolerance,
    # because a separate tolerance is exactly what let a 30-hour gap be searched
    # with a 24-hour window.
    decision = 'DAILY' if window == '24h' else 'RECOVERY'
    if decision == 'DAILY':
        reason = (
            f'The last successful production run closed {gap_hours} hours ago, '
            f'within the {interval}-hour daily interval, so the 24-hour window '
            f'covers the whole gap. A small result count will not change that: '
            f'yield is market supply, not evidence about coverage.')
    else:
        reason = (
            f'The last successful production run closed {gap_hours} hours ago, more '
            f'than the {interval}-hour daily interval, so {window} is the SMALLEST '
            f'supported window that COVERS the gap. A 24-hour window would leave '
            f'{round(gap_hours - 24, 2)} hours unsearched.')
    if capped:
        reason += (
            f' The gap EXCEEDS the {window} cap by {coverage["uncovered_hours"]} '
            f'hours ({coverage["uncovered_days"]} days). This run does NOT achieve '
            f'full historical coverage, and that shortfall is reported rather than '
            f'described away. {policy.get("uncapped_recovery_advice", "")}'.rstrip())
    return {
        'schema_version': SCHEMA_VERSION,
        'decision': decision,
        'window': window,
        'window_hours': window_hours(window, strategy),
        'budget_mode': DECISION_MODES[decision],
        'capped': capped,
        'coverage': coverage,
        'evidence': evidence,
        'reason': reason,
        'recovery_advice': str(policy.get('uncapped_recovery_advice', '')) if capped else '',
        'yield_considered': False,
    }


def gap_fill_targets(summary):
    """Which inventory families a gap-fill run should repair, from ONE run summary.

    A genuine family gap is a family that was attempted and where nothing
    completed: that inventory was never seen. A `covered_with_warnings` family had
    a sibling fail while another source covered the same inventory, so it is a
    source warning and produces no gap-fill work at all. Treating it as a gap would
    re-run a multi-source sweep to recover inventory that was already searched.
    """
    summary = summary or {}
    gaps = sorted(summary.get('family_gaps') or [])
    warned = sorted(summary.get('families_covered_with_warnings') or [])

    # A gap in a family that owes NOTHING and cannot even run a query is not
    # repairable work. Re-running it every gap-fill would retry a source already
    # proven unable to execute the request, forever, while reporting the same gap
    # afterwards. It stays REPORTED as a gap; it is simply not scheduled work.
    unrepairable, reasons = [], {}
    try:
        from coverage_ledger import required_universe
        from sources import load_registry
        universe = required_universe()
        registry = load_registry()
        owed = {row['inventory_family'] for row in universe.values()}
        for fam in gaps:
            if fam in owed:
                continue
            modes = {str(s.get('query_execution', 'verified'))
                     for s in (registry.get('sources') or [])
                     if s.get('family') == fam and s.get('enabled', True)}
            if modes and not (modes & {'verified'}):
                unrepairable.append(fam)
                reasons[fam] = (f'owes no required bucket and no enabled source can '
                                f'execute a query (query_execution: '
                                f'{", ".join(sorted(modes))})')
    except Exception:                                          # noqa: BLE001
        unrepairable, reasons = [], {}

    targets = [f for f in gaps if f not in unrepairable]
    return {
        'schema_version': SCHEMA_VERSION,
        'family_gaps': gaps,
        'families_covered_with_warnings': warned,
        'gapfill_required': bool(targets),
        'target_families': targets,
        'unrepairable_families': sorted(unrepairable),
        'unrepairable_reasons': reasons,
        'reason': (
            f'{len(targets)} inventory family/families were attempted, left unseen '
            f'and can still be repaired: {", ".join(targets)}.'
            if targets else
            'No repairable inventory family was left unseen, so there is nothing to '
            'gap fill.'),
        'unrepairable_note': (
            f'Still reported as a gap, but NOT scheduled: {", ".join(sorted(unrepairable))}. '
            f'Retrying a source that owes nothing and cannot execute the query would '
            f'repeat forever and change nothing.'
            if unrepairable else ''),
        'warning_note': (
            f'A source failed inside a family another source covered, so the '
            f'inventory WAS searched: {", ".join(warned)}. That is a source warning, '
            f'not a missed family, and it creates no gap-fill work.'
            if warned else ''),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _history():
    """Run records and their derived summaries, read through the run authority."""
    import discovery_run as run_mod
    records, summaries = [], {}
    for path in run_mod.run_files():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        records.append(data)
        try:
            summaries[data.get('run_id')] = run_mod.summarise(data)
        except Exception:  # noqa: BLE001 - an unreadable summary is not a crash
            summaries[data.get('run_id')] = None
    return records, summaries


def cmd_select(args):
    records, summaries = _history()
    print(json.dumps(select_window(records, summaries, explicit=args.window,
                                   now=args.now), indent=2, ensure_ascii=False))


def boundary_probe(strategy=None):
    """Every documented boundary of the ladder, computed rather than asserted.

    Exists so the boundaries can be checked from the command line without
    constructing run history, and so a change to the ladder shows up here
    immediately rather than in a test nobody ran.
    """
    strategy = strategy or load_strategy()
    rows = []
    for label, gap in (
            ('23h59m', 23 + 59 / 60), ('exactly 24h', 24.0),
            ('24h and one minute', 24 + 1 / 60), ('30h', 30.0), ('40h', 40.0),
            ('exactly 7d', 168.0), ('7d and one minute', 168 + 1 / 60),
            ('exactly 14d', 336.0), ('14d and one minute', 336 + 1 / 60),
            ('30d', 720.0)):
        window, capped = _ladder_window(gap, strategy)
        cov = _coverage(window, gap, strategy)
        rows.append({'gap': label, 'gap_hours': round(gap, 4), 'window': window,
                     'decision': 'DAILY' if window == '24h' else 'RECOVERY',
                     'capped': capped, **cov})
    return {'schema_version': SCHEMA_VERSION,
            'boundary_rule': window_policy(strategy).get('boundary_rule'),
            'grace_hours': window_policy(strategy).get('daily_grace_hours', 0),
            'probes': rows,
            'every_probe_covers_or_reports': all(
                r['covers_gap'] or r['capped'] for r in rows)}


def cmd_boundaries(args):
    print(json.dumps(boundary_probe(), indent=2, ensure_ascii=False))


def cmd_policy(args):
    print(json.dumps(window_policy(), indent=2, ensure_ascii=False))


def cmd_gapfill(args):
    import discovery_run as run_mod
    run_id = args.run_id or run_mod.latest_run_id()
    print(json.dumps(gap_fill_targets(run_mod.summarise(run_mod.load_run(run_id))),
                     indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description='Run-history based search window selection')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('select', help='Choose the window for the next run.')
    s.add_argument('--window', default='', help='Explicit window: 24h, 7d or 14d.')
    s.add_argument('--now', default='', help='ISO timestamp to evaluate against.')
    s.set_defaults(func=cmd_select)

    sub.add_parser('policy', help='Show the window policy.').set_defaults(func=cmd_policy)
    sub.add_parser('boundaries', help='Probe every documented ladder boundary.'
                   ).set_defaults(func=cmd_boundaries)

    g = sub.add_parser('gapfill', help='Which families a gap-fill run should repair.')
    g.add_argument('--run-id', dest='run_id', default='')
    g.set_defaults(func=cmd_gapfill)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

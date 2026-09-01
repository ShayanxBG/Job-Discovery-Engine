#!/usr/bin/env python3
"""Employer ATS checks, bounded by RESERVATION rather than by good intentions.

WHAT WAS WRONG. The mode's ceiling was declared in configuration, printed in the
plan, and recorded in the run metrics, and nothing anywhere stopped a run from
exceeding it. Exceeding it was visible only afterwards, in a number describing
work that had already been done. A limit nobody can enforce is a suggestion, and
a suggestion is not a budget.

WHAT REPLACES IT. Capacity is taken BEFORE the external check happens, not
counted after. Every attempt passes through `reserve`, which writes the increased
count to disk and only then hands back the tasks. There is no code path that
performs an employer check without first holding a reservation, and `due_tasks`
never returns more than the remaining capacity however large a `--limit` the
caller passes.

FOUR THINGS THIS GETS RIGHT THAT A NAIVE COUNTER GETS WRONG.

1. A FAILED CHECK STILL SPENDS CAPACITY. The ceiling bounds external WORK, not
   successes. A dead careers page cost a fetch, and letting failures be free
   would turn a bad tenant into an unbounded retry loop inside a bounded run.

2. RESERVATION IS DURABLE AND ORDERED. The reservation is written before the
   caller receives the tasks, so a crash mid-check spends the slot rather than
   silently freeing it. Two consecutive requests inside one run cannot hand back
   the same employer, because the first request records the keys it issued.

3. REACHING THE CEILING IS A BOUNDED STOP, NOT A FAILURE. `ceiling_reached` is a
   normal outcome. Deferred employers stay enabled, stay due, and are picked up
   by the next run. Recording it as a source failure would corrupt exactly the
   coverage vocabulary that Phase 4 spent its effort protecting.

4. THE COUNTERS RECONCILE. reserved = attempted + abandoned, attempted =
   succeeded + failed, and deferred is what capacity refused. A ledger that
   cannot be checked is a ledger nobody should trust.

WRITE OWNERSHIP. The ledger lives inside the run record, which the parent
workflow already owns exclusively, so this introduces no second writer and no
parallel write path.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1

# Every counter the ledger tracks. Named here so a caller cannot invent a
# fifth one that nothing reconciles.
COUNTERS = ('due', 'reserved', 'attempted', 'succeeded', 'failed',
            'deferred_by_ceiling', 'abandoned')

STOP_REASONS = ('capacity_available', 'ceiling_reached', 'nothing_due')


def budget_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def ceiling_for(mode, strategy=None):
    """The mode's declared ATS ceiling. Configuration owns it, not this module."""
    from search_strategy import employer_ats_ceiling
    return int(employer_ats_ceiling(mode, strategy))


def empty_ledger(mode='deep', ceiling=None, strategy=None):
    return {
        'schema_version': SCHEMA_VERSION,
        'mode': mode,
        'ceiling': int(ceiling if ceiling is not None else ceiling_for(mode, strategy)),
        'counts': {name: 0 for name in COUNTERS},
        'reserved_keys': [],
        'deferred_keys': [],
    }


def _ledger(data):
    """The run's ATS ledger, created on first use.

    A run recorded before this existed has none, and reading one must not invent
    counters it never had. `has_ledger` is how a caller tells the difference.
    """
    block = data.get('employer_ats')
    if not isinstance(block, dict) or 'counts' not in block:
        block = empty_ledger(data.get('mode', 'deep'),
                             (block or {}).get('checks_ceiling'))
        data['employer_ats'] = block
    for name in COUNTERS:
        block['counts'].setdefault(name, 0)
    return block


def has_ledger(data):
    block = (data or {}).get('employer_ats')
    return isinstance(block, dict) and 'counts' in block


def remaining(ledger):
    """Capacity left. Never negative, whatever a corrupted count claims."""
    return max(0, int(ledger.get('ceiling', 0)) - int(ledger['counts'].get('reserved', 0)))


def reconcile(ledger):
    """Every way this ledger fails to add up. Empty means it is trustworthy."""
    problems = []
    if not isinstance(ledger, dict) or 'counts' not in ledger:
        return [{'problem': 'not_an_ats_ledger'}]
    counts = ledger['counts']

    def value(name):
        try:
            return int(counts.get(name, 0) or 0)
        except (TypeError, ValueError):
            return -1

    for name in COUNTERS:
        if value(name) < 0:
            problems.append({'counter': name, 'problem': 'not_a_whole_number'})
    ceiling = int(ledger.get('ceiling', 0))
    if value('reserved') > ceiling:
        problems.append({'problem': 'reserved_exceeds_ceiling',
                         'reserved': value('reserved'), 'ceiling': ceiling,
                         'detail': 'Capacity is taken before the check, so this can '
                                   'only mean the reservation gate was bypassed.'})
    if value('attempted') > value('reserved'):
        problems.append({'problem': 'attempted_exceeds_reserved',
                         'detail': 'An external check happened without a reservation.'})
    if value('succeeded') + value('failed') != value('attempted'):
        problems.append({'problem': 'outcomes_do_not_account_for_attempts',
                         'succeeded': value('succeeded'), 'failed': value('failed'),
                         'attempted': value('attempted')})
    if value('attempted') + value('abandoned') != value('reserved'):
        problems.append({'problem': 'reservations_unaccounted',
                         'detail': 'reserved = attempted + abandoned. A reservation '
                                   'that was neither used nor released is a lost slot.'})
    if value('deferred_by_ceiling') and remaining(ledger):
        problems.append({'problem': 'deferred_while_capacity_remained',
                         'detail': 'Employers were deferred for a ceiling that had '
                                   'not been reached.'})
    if len(ledger.get('reserved_keys', [])) != len(set(ledger.get('reserved_keys', []))):
        problems.append({'problem': 'duplicate_reservation',
                         'detail': 'One employer was reserved twice in one run.'})
    return problems


def due_tasks(ledger, due_rows, limit=0):
    """The bounded task list: never more than capacity, never a repeat.

    `limit` can only make the list SHORTER. A caller asking for a hundred tasks
    gets the remaining capacity, because the ceiling is the boundary and the
    caller's number is a preference.
    """
    already = set(ledger.get('reserved_keys', []))
    fresh = [row for row in (due_rows or [])
             if str(row.get('employer_key') or '') not in already]
    capacity = remaining(ledger)
    want = capacity if int(limit or 0) <= 0 else min(capacity, int(limit))
    selected = fresh[:want]
    deferred = fresh[want:]
    return {
        'schema_version': SCHEMA_VERSION,
        'ceiling': int(ledger.get('ceiling', 0)),
        'reserved_so_far': int(ledger['counts'].get('reserved', 0)),
        'remaining_before': capacity,
        'requested_limit': int(limit or 0),
        'granted': len(selected),
        'tasks': selected,
        'deferred_by_ceiling': [str(r.get('employer_key') or '') for r in deferred],
        'stop_reason': ('nothing_due' if not fresh else
                        'ceiling_reached' if not selected else 'capacity_available'),
        'note': (
            'Reaching the ceiling is a normal bounded stop, not a source failure. '
            'Deferred employers stay enabled and stay due for the next run.'),
    }


def reserve(data, due_rows, limit=0, save=None):
    """Take capacity, THEN hand back the tasks. Never the other way round.

    The reservation is persisted before the caller sees a single employer, so a
    crash between reserving and checking spends the slot rather than silently
    freeing it for an unbounded retry.
    """
    ledger = _ledger(data)
    plan = due_tasks(ledger, due_rows, limit)
    ledger['counts']['due'] = max(int(ledger['counts'].get('due', 0)), len(due_rows or []))
    ledger['counts']['reserved'] += plan['granted']
    ledger['counts']['deferred_by_ceiling'] = len(
        set(ledger.get('deferred_keys', [])) | set(plan['deferred_by_ceiling']))
    ledger['reserved_keys'] = list(ledger.get('reserved_keys', [])) + [
        str(row.get('employer_key') or '') for row in plan['tasks']]
    ledger['deferred_keys'] = sorted(
        set(ledger.get('deferred_keys', [])) | set(plan['deferred_by_ceiling']))
    if save:
        save(data)
    return {**plan, 'remaining_after': remaining(ledger)}


def record_outcome(data, employer_key, succeeded, save=None):
    """One completed external check. A FAILURE still spends its reserved slot.

    The ceiling bounds external work, not successes. Refunding a failure would
    let one dead careers page consume the whole run one retry at a time.
    """
    ledger = _ledger(data)
    key = str(employer_key or '').strip()
    if key and key not in set(ledger.get('reserved_keys', [])):
        raise budget_error(
            f'No ATS reservation held for {key!r}.',
            'Capacity is taken BEFORE the external check. Call reserve first.',
            'An unreserved check is exactly what the ceiling exists to prevent.')
    ledger['counts']['attempted'] += 1
    ledger['counts']['succeeded' if succeeded else 'failed'] += 1
    if save:
        save(data)
    return {'employer_key': key, 'succeeded': bool(succeeded),
            'attempted': ledger['counts']['attempted'],
            'failed': ledger['counts']['failed'],
            'remaining': remaining(ledger),
            'note': ('A failed check still consumes capacity: the external work was '
                     'performed.' if not succeeded else '')}


def abandon(data, employer_key, save=None):
    """Release a reservation whose check never began, so the ledger still adds up."""
    ledger = _ledger(data)
    key = str(employer_key or '').strip()
    if key not in set(ledger.get('reserved_keys', [])):
        raise budget_error(f'No ATS reservation held for {key!r}.')
    ledger['counts']['abandoned'] += 1
    if save:
        save(data)
    return {'employer_key': key, 'abandoned': ledger['counts']['abandoned']}


def summary(ledger):
    """The metrics view: every counter distinguished, plus the derived capacity."""
    counts = dict(ledger.get('counts', {}))
    return {
        'ceiling': int(ledger.get('ceiling', 0)),
        'checks_due': int(counts.get('due', 0)),
        'checks_reserved': int(counts.get('reserved', 0)),
        'checks_attempted': int(counts.get('attempted', 0)),
        'checks_succeeded': int(counts.get('succeeded', 0)),
        'checks_failed': int(counts.get('failed', 0)),
        'checks_deferred_by_ceiling': int(counts.get('deferred_by_ceiling', 0)),
        'checks_abandoned': int(counts.get('abandoned', 0)),
        'remaining': remaining(ledger),
        'ceiling_reached': remaining(ledger) == 0 and int(counts.get('reserved', 0)) > 0,
        'reconciles': not reconcile(ledger),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _run(run_id):
    import discovery_run as run_mod
    return run_mod, run_mod.load_run(run_id or run_mod.latest_run_id())


def cmd_tasks(args):
    """Bounded due list for the current run. This is what /scrape must call."""
    import watchlist as watch_mod
    run_mod, data = _run(args.run_id)
    rows = watch_mod.due()
    print(json.dumps(reserve(data, rows, limit=args.limit, save=run_mod.save_run),
                     indent=2, ensure_ascii=False))


def cmd_outcome(args):
    run_mod, data = _run(args.run_id)
    print(json.dumps(record_outcome(data, args.employer_key, not args.failed,
                                    save=run_mod.save_run), indent=2, ensure_ascii=False))


def cmd_status(args):
    run_mod, data = _run(args.run_id)
    ledger = _ledger(data)
    print(json.dumps({'run_id': data.get('run_id'), **summary(ledger),
                      'problems': reconcile(ledger)}, indent=2, ensure_ascii=False))


def cmd_ceilings(args):
    from search_strategy import MODES
    print(json.dumps({mode: ceiling_for(mode) for mode in MODES}, indent=2))


def main():
    p = argparse.ArgumentParser(description='Bounded employer ATS check budget')
    sub = p.add_subparsers(dest='cmd', required=True)

    t = sub.add_parser('tasks', help='Reserve and return the bounded due list.')
    t.add_argument('--run-id', dest='run_id', default='')
    t.add_argument('--limit', type=int, default=0,
                   help='Preference only. It can shorten the list, never lengthen it.')
    t.set_defaults(func=cmd_tasks)

    o = sub.add_parser('outcome', help='Record one completed check.')
    o.add_argument('employer_key')
    o.add_argument('--run-id', dest='run_id', default='')
    o.add_argument('--failed', action='store_true',
                   help='The check failed. It still consumes its reserved slot.')
    o.set_defaults(func=cmd_outcome)

    s = sub.add_parser('status', help='Ledger and reconciliation for one run.')
    s.add_argument('--run-id', dest='run_id', default='')
    s.set_defaults(func=cmd_status)

    sub.add_parser('ceilings', help='The declared ceiling for every mode.').set_defaults(
        func=cmd_ceilings)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

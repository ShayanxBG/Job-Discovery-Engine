#!/usr/bin/env python3
"""Deterministic title-to-source rotation across runs.

THE BUG THIS REPLACES. The planner paired terms with sources on a fixed diagonal:
the first title went to the first source, the second to the second, and so on. It
looks like breadth and is not. With five titles and five boards, LinkedIn only
ever saw `Python Developer` and Reed only ever saw the fifth title, run after run
after run. `Python Developer` on Reed was never searched. Not once. The
combinations were not chosen and rejected, they were structurally unreachable, and
because the diagonal was stable the same ones were unreachable every single day.

WHAT REPLACES IT. The same diagonal, OFFSET by a cycle index that advances one
step per successful completed run. Offset 0 pairs title i with source i; offset 1
pairs title i with source i+1. Over `cycle_length` runs every rotating title
reaches every applicable primary inventory family, and the coverage debt is
visible at any moment rather than invisible forever.

FOUR PROPERTIES THIS MUST HAVE, AND WHY EACH ONE IS NOT OPTIONAL.

1. DETERMINISTIC, NOT RANDOM. Random selection would cover the matrix eventually
   in expectation and could not be reproduced, audited, or resumed. Re-planning
   the same state must produce byte-identical output, or no plan can be checked.

2. ADVANCED BY SUCCESS ONLY. The index is the COUNT of successful completed
   production runs. A failed or partial run advances nothing, because the
   combinations it was supposed to cover were not covered, and stepping past them
   would silently retire a coverage debt that was never paid. Re-running a failed
   run therefore retries the same combinations, which is exactly right.

3. AN INDEX, NOT A CURSOR. Nothing is stored. The index is derived from run
   history on demand, so it cannot drift out of step with the history it claims to
   summarise, and there is no cursor file to corrupt.

4. APPLICABILITY IS REAL. A title is only owed to a source family when that family
   is genuinely eligible for the search family. Owing `Python Developer` to a
   sponsor board that the direct-title family never searches would be a debt that
   can never be paid and a coverage report that can never be honest.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from search_strategy import get_family, load_strategy  # noqa: E402
from sources import load_registry, source_family  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1


def rotation_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def rotation_policy(strategy=None):
    strategy = strategy or load_strategy()
    policy = strategy.get('rotation')
    if not isinstance(policy, dict):
        raise rotation_error(
            'config/search_strategy.json has no rotation block.',
            'Rotation is configuration, not a constant in this module.')
    return policy


def cycle_length(strategy=None):
    return max(1, int(rotation_policy(strategy).get('cycle_length', 4)))


def rotating_families(strategy=None):
    return tuple(rotation_policy(strategy).get('rotating_families', ()) or ())


def primary_source_families(strategy=None):
    return tuple(rotation_policy(strategy).get('primary_source_families', ()) or ())


def cycle_index(successful_runs, strategy=None):
    """Where in the cycle the next run sits. Derived, never stored."""
    return int(max(0, int(successful_runs))) % cycle_length(strategy)


def successful_run_count(records=None, summaries=None):
    """How many successful completed production runs the history holds.

    Delegates the definition of `successful` to `search_window`, so there is one
    answer to that question and not two that can disagree.
    """
    from search_window import run_is_successful
    summaries = summaries or {}
    return sum(1 for r in (records or [])
               if run_is_successful(r, summaries.get(r.get('run_id'))))


def _applicable_source_families(family_id, strategy=None, registry=None):
    """Primary inventory families this search family can actually reach."""
    strategy = strategy or load_strategy()
    registry = registry or load_registry()
    family = get_family(family_id, strategy)
    eligible = {source_family(s, registry)
                for s in (family.get('eligible_sources') or [])}
    return [f for f in primary_source_families(strategy) if f in eligible]


def pairings(family_id, terms, index=0, strategy=None, registry=None):
    """Term-to-source-family pairs for ONE run at ONE cycle index.

    The offset walks the source families, so a term that reached LinkedIn last run
    reaches Indeed next. With fewer terms than families, or the reverse, the
    modulus keeps every pair reachable rather than truncating the shorter list.
    """
    strategy = strategy or load_strategy()
    registry = registry or load_registry()
    fams = _applicable_source_families(family_id, strategy, registry)
    terms = list(terms or [])
    if not fams or not terms:
        return []
    return [(term, fams[(position + int(index)) % len(fams)])
            for position, term in enumerate(terms)]


def full_cycle(family_id, terms, strategy=None, registry=None):
    """Every pairing produced across a whole cycle, index by index."""
    return {index: pairings(family_id, terms, index, strategy, registry)
            for index in range(cycle_length(strategy))}


def coverage(family_id, terms, strategy=None, registry=None, upto=None):
    """Which term-source-family combinations the cycle covers, and which remain.

    `upto` restricts the answer to the indices already run, so a mid-cycle run can
    state its outstanding coverage debt honestly rather than claiming the whole
    matrix in advance.
    """
    strategy = strategy or load_strategy()
    registry = registry or load_registry()
    fams = _applicable_source_families(family_id, strategy, registry)
    terms = list(terms or [])
    required = {(t, f) for t in terms for f in fams}
    length = cycle_length(strategy)
    indices = range(length if upto is None else max(0, min(int(upto), length)))
    covered = set()
    for index in indices:
        covered.update(pairings(family_id, terms, index, strategy, registry))
    return {
        'schema_version': SCHEMA_VERSION,
        'search_family': family_id,
        'cycle_length': length,
        'indices_considered': list(indices),
        'terms': terms,
        'applicable_source_families': fams,
        'required_combinations': len(required),
        'covered_combinations': len(covered & required),
        'covered': sorted(f'{t} @ {f}' for t, f in sorted(covered & required)),
        'outstanding': sorted(f'{t} @ {f}' for t, f in sorted(required - covered)),
        'complete': not (required - covered),
    }


def plan_note(index, strategy=None, override=''):
    """One sentence a run can print about where it sits in the rotation."""
    length = cycle_length(strategy)
    if override:
        return (f'Rotation OVERRIDDEN by {override}: this run searches the sources it '
                f'was told to, so it neither follows nor advances the normal '
                f'{length}-run cycle.')
    return (f'Rotation index {index} of {length}. Core titles are paired with primary '
            f'inventory families at this offset; over {length} successful runs every '
            f'rotating title reaches every applicable primary family. A failed or '
            f'partial run does not advance the index.')


# --------------------------------------------------------------------------
# Inventory-family coverage. Which families a run must reach, which rotate,
# and what it left out. An omission must be a DECISION with a reason: a family
# quietly missing from every plan looks exactly like a family nobody thought
# about, and the coverage percentage absorbs both without complaint.
# --------------------------------------------------------------------------

def coverage_policy(registry=None):
    registry = registry or load_registry()
    policy = registry.get('family_coverage_policy')
    if not isinstance(policy, dict):
        raise rotation_error(
            'config/sources.json has no family_coverage_policy block.',
            'Which families an ordinary run must reach is configuration.')
    return policy


def family_classes(registry=None):
    """Every inventory family and its monitoring class, from the registry."""
    registry = registry or load_registry()
    out = {}
    for family_id, block in (registry.get('families') or {}).items():
        block = block if isinstance(block, dict) else {}
        out[family_id] = {
            'monitoring_class': str(block.get('monitoring_class') or 'rotating'),
            'queryable': bool(block.get('queryable', True)),
            'reason': str(block.get('monitoring_reason') or ''),
            'policy_review_after': str(block.get('policy_review_after') or ''),
        }
    return out


def expected_families(registry=None):
    """The exhaustive DENOMINATOR: enabled, queryable, not deliberately excluded.

    A family classed `excluded` leaves the denominator entirely rather than
    counting as coverage exhaustive fails to reach. That is the difference
    between a decision and a defect, and a percentage that cannot tell them apart
    is worse than no percentage.
    """
    registry = registry or load_registry()
    policy = coverage_policy(registry)
    wanted = set(policy.get('exhaustive_must_cover') or ())
    enabled = {s.get('family') for s in (registry.get('sources') or [])
               if isinstance(s, dict) and s.get('enabled', True)}
    rows = family_classes(registry)
    return sorted(f for f, row in rows.items()
                  if f in enabled and row['queryable']
                  and row['monitoring_class'] in wanted)


def excluded_families(registry=None):
    rows = family_classes(registry)
    return sorted(f for f, row in rows.items()
                  if row['monitoring_class'] == 'excluded' or not row['queryable'])


def rotating_due(index, registry=None):
    """Which rotating families this cycle position is responsible for.

    Split evenly across the cycle so every rotating family is reached within it,
    and derived from the index rather than stored, for the same reason the title
    rotation is: a cursor can drift out of step with the history it summarises.
    """
    registry = registry or load_registry()
    policy = coverage_policy(registry)
    length = max(1, int(policy.get('rotating_cycle_length', 3)))
    rows = family_classes(registry)
    enabled = {s.get('family') for s in (registry.get('sources') or [])
               if isinstance(s, dict) and s.get('enabled', True)}
    rotating = sorted(f for f, row in rows.items()
                      if row['monitoring_class'] == 'rotating'
                      and row['queryable'] and f in enabled)
    if not rotating:
        return []
    position = int(index) % length
    return [f for i, f in enumerate(rotating) if i % length == position]


def family_cycle_index(successful_runs, registry=None):
    """Where the FAMILY cycle sits. Its own modulus, not the title cycle's.

    Passing the title index in and taking it modulo again gave
    (successes % 5) % 3, so family position 2 came round every five runs rather
    than every three and the documented three-run cycle was quietly false. Two
    cycles of different lengths must each count from the same source, never
    through one another.
    """
    registry = registry or load_registry()
    length = max(1, int(coverage_policy(registry).get('rotating_cycle_length', 3)))
    return int(max(0, int(successful_runs))) % length


def force_due_families(families, records=None, summaries=None, now=None,
                       registry=None):
    """Rotating families overdue by TIME rather than by cycle position.

    The cycle counts runs; the cap counts hours. A workspace run weekly would let
    a three-run family wait twenty-one days against a fourteen-day cap and lose a
    week of inventory nothing could recover, and the cycle would report itself as
    perfectly on schedule the whole time. This override is what keeps the two
    clocks from disagreeing, and it fires well below the cap so the family is
    pulled forward BEFORE anything is lost.
    """
    registry = registry or load_registry()
    limit = float(coverage_policy(registry).get('force_due_after_hours', 0) or 0)
    if not limit:
        return []
    # A family is overdue when the OLDEST BUCKET on it is overdue. Judging a
    # family by its freshest bucket would let it look current while one of its
    # query intents quietly aged past the cap, which is the whole failure this
    # phase exists to remove.
    #
    # A bucket that has NEVER been searched is not overdue, it is new: the
    # ordinary cycle reaches it within one cycle length, well inside the cap, and
    # it arrives carrying the initial catch-up window. Treating first coverage as
    # overdue made a bootstrap run try to reach every family at once and starved
    # the cycle on exactly the runs where it matters most.
    import coverage_ledger as cl
    universe = cl.required_universe(registry=registry)
    wanted = set(families or ())
    buckets = [b for b, row in universe.items() if row['inventory_family'] in wanted]
    oldest = {}
    for bucket, row in cl.bucket_windows(buckets, records, summaries, now).items():
        elapsed = row.get('elapsed_gap_hours')
        if elapsed is None:
            continue
        family = universe[bucket]['inventory_family']
        oldest[family] = max(oldest.get(family, 0.0), float(elapsed))
    return sorted(f for f, elapsed in oldest.items() if elapsed >= limit)


def family_coverage_plan(mode, index=0, registry=None, successful_runs=None,
                         records=None, summaries=None, now=None):
    """Which families this mode should reach now, and what it omits and why.

    `successful_runs` is preferred: it lets the family cycle keep its own index
    rather than inheriting the title cycle's. `index` remains for fixtures that
    want to pin a position directly.
    """
    registry = registry or load_registry()
    if successful_runs is not None:
        index = family_cycle_index(successful_runs, registry)
    policy = coverage_policy(registry)
    rows = family_classes(registry)
    expected = expected_families(registry)
    length = max(1, int(policy.get('rotating_cycle_length', 3)))

    forced, due = set(), set()
    # An INITIAL CATCH-UP reaches every family. Nothing has been covered, so the
    # rotating cycle has no debt to prioritise between, and the first run is the
    # one where breadth matters most: a family left out of run one waits three
    # runs for inventory nobody has ever looked at.
    first_run = not (records or [])
    if mode == 'exhaustive' or first_run:
        planned, omitted = list(expected), []
        due = {f for f in expected if rows[f]['monitoring_class'] == 'rotating'}
    else:
        due = set(rotating_due(index, registry))
        # Anything overdue by TIME joins the due set regardless of position.
        forced = set(force_due_families(
            [f for f in expected if rows[f]['monitoring_class'] == 'rotating'],
            records, summaries, now, registry))
        due |= forced
        planned = [f for f in expected
                   if rows[f]['monitoring_class'] == 'daily' or f in due]
        omitted = []
        for family in expected:
            if family in planned:
                continue
            # When does this family come round? Stated, so the debt is visible
            # rather than implied by its absence.
            due_at = next((i for i in range(length)
                           if family in rotating_due(index + i, registry)), None)
            omitted.append({
                'family': family,
                'monitoring_class': rows[family]['monitoring_class'],
                'reason': rows[family]['reason'] or 'Rotating family, not due this cycle.',
                'due_in_rolling_cycle': due_at is not None,
                'runs_until_due': due_at,
                'policy_review_after': rows[family]['policy_review_after'],
            })
    return {
        'schema_version': SCHEMA_VERSION,
        'mode': mode,
        'cycle_index': int(index) % length,
        'rotating_cycle_length': length,
        'expected_families': expected,
        'expected_family_count': len(expected),
        'planned_families': sorted(planned),
        'planned_family_count': len(planned),
        # Which rotating families this cycle position OWES. A due family outranks
        # an optional domain query, because a family nobody reached this cycle is
        # a debt while a domain query is a bonus.
        'rotating_forced_due_by_time': sorted(forced) if mode != 'exhaustive' else [],
        'rotating_due_now': sorted(due & {f for f in expected
                                          if rows[f]['monitoring_class'] == 'rotating'})
                            if mode != 'exhaustive'
                            else sorted(f for f in expected
                                        if rows[f]['monitoring_class'] == 'rotating'),
        'omitted_families': omitted,
        'excluded_from_denominator': [
            {'family': f, 'reason': rows[f]['reason'] or 'Declared not queryable.',
             'policy_review_after': rows[f]['policy_review_after']}
            for f in excluded_families(registry)],
        'complete': sorted(planned) == sorted(expected),
        'note': (
            'Coverage is counted by inventory FAMILY. Several sources inside one '
            'family are one family covered, never several, because searching two '
            'sites that share inventory does not search more inventory.'),
    }


def sources_for_families(families, registry=None):
    """Every enabled source id belonging to these inventory families."""
    registry = registry or load_registry()
    wanted = set(families or ())
    return sorted(s.get('id') for s in (registry.get('sources') or [])
                  if isinstance(s, dict) and s.get('enabled', True)
                  and s.get('family') in wanted and s.get('id'))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _terms_for(family_id, strategy=None):
    from search_plan import _family_terms
    from search_profile import load_search_profile
    strategy = strategy or load_strategy()
    family = get_family(family_id, strategy)
    return [text for text, _template in _family_terms(family, load_search_profile(), 4)]


def cmd_index(args):
    from search_window import _history
    records, summaries = _history()
    successful = successful_run_count(records, summaries)
    index = cycle_index(successful, )
    print(json.dumps({
        'schema_version': SCHEMA_VERSION,
        'successful_completed_production_runs': successful,
        'cycle_length': cycle_length(),
        'cycle_index': index,
        'advance_on': rotation_policy().get('advance_on'),
        'note': plan_note(index),
    }, indent=2, ensure_ascii=False))


def cmd_cycle(args):
    terms = ([t.strip() for t in args.terms.split(',') if t.strip()]
             if args.terms else _terms_for(args.family_id))
    rows = {str(i): [f'{t} @ {f}' for t, f in pairs]
            for i, pairs in full_cycle(args.family_id, terms).items()}
    print(json.dumps({'search_family': args.family_id, 'terms': terms,
                      'cycle': rows,
                      'coverage': coverage(args.family_id, terms)},
                     indent=2, ensure_ascii=False))


def cmd_families(args):
    print(json.dumps(family_coverage_plan(args.mode, args.index), indent=2,
                     ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description='Deterministic title-to-source rotation')
    sub = p.add_subparsers(dest='cmd', required=True)

    sub.add_parser('index', help='Where the next run sits in the cycle.').set_defaults(func=cmd_index)

    c = sub.add_parser('cycle', help='Every pairing across a whole cycle.')
    c.add_argument('family_id')
    c.add_argument('--terms', default='', help='Comma separated terms to rotate.')
    c.set_defaults(func=cmd_cycle)

    fc = sub.add_parser('families', help='Inventory-family coverage plan for one mode.')
    fc.add_argument('--mode', default='daily')
    fc.add_argument('--index', type=int, default=0)
    fc.set_defaults(func=cmd_families)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

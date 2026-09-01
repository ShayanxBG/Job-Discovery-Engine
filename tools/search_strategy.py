#!/usr/bin/env python3
"""Deterministic owner of HOW this workspace searches.

`config/search_strategy.json` defines the search FAMILIES, their budgets, the
term slots each family fills, and the deterministic rules for query dedup,
saturation and body-signal gating. It is publishable: it describes search methods
and contains no candidate values.

The separation from `config/sources.json` is deliberate and load bearing:

    sources.json          WHAT a source is: identity, inventory family, the state
                          source_type it maps to, its freshness behaviour.
    search_strategy.json  HOW to search: which query families exist, how much
                          budget each gets, when a family is saturated.

A search family is not a source family. Ten variations of one job title across
five different boards are ONE search family and FIVE source families; a run that
did that has broad source coverage and narrow query coverage. Reporting them
separately is the point, because they diagnose different failures: a thin result
from narrow queries is a strategy problem, and a thin result from a collapsed
source is a coverage problem.

This module validates that every eligible source a family names actually exists
in the source registry, so a search family can never be defined against a source
that has no deterministic identity.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources import is_known_source, load_registry, source_family  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = ROOT / 'config' / 'search_strategy.json'

# `daily` and `catchup` separate the two resource profiles that used to share
# one. A day of new UK inventory is small, so a daily run reads fewer postings
# and asks fewer queries; a catch-up covers up to a fortnight in ONE pass and
# needs the larger ceilings to finish what it started.
MODES = ('quick', 'daily', 'deep', 'initial_catchup', 'catchup', 'exhaustive',
         'gapfill')
# Modes a plain `/scrape` may select for itself. Everything else is chosen by
# the user, so nothing here can silently escalate a run's cost.
# `initial_catchup` is the ONE-TIME bootstrap: selected automatically for the
# first successful production run and never again.
AUTOMATIC_MODES = ('daily', 'catchup', 'initial_catchup')
SATURATION_STATES = ('CONTINUE', 'SATURATED', 'BUDGET_EXHAUSTED', 'GAP_REMAINS')

REQUIRED_FAMILY_FIELDS = (
    'id', 'display_name', 'purpose', 'priority', 'query_budget',
    'candidate_budget', 'requires_body_validation', 'eligible_sources',
    'term_slots', 'query_templates', 'notes',
)
# Families that must never be planned unprompted. gapfill repairs a RECORDED gap,
# so planning it by default would spend budget re-covering a covered family.
NEVER_PLAN_BY_DEFAULT = ('gapfill',)
REQUIRED_MODE_FIELDS = (
    'global_query_budget', 'global_raw_candidate_ceiling',
    'global_deep_jd_ceiling', 'family_budget_multiplier',
    # Employer ATS work is bounded SEPARATELY from web queries, so a busy
    # watchlist cannot quietly eat board coverage and a quiet one cannot donate
    # its budget to more board queries.
    'employer_ats_check_ceiling',
)
# The families the product promises to be able to plan. A strategy file that
# quietly dropped one would narrow discovery without anybody noticing.
REQUIRED_FAMILIES = (
    'direct-title', 'backend-capability', 'adjacent-software', 'early-career',
    'employer-ats', 'sponsorship-oriented', 'gapfill',
)

_CACHE = {}


def strategy_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def load_strategy(path=None):
    """Parse the search strategy, raising an actionable message instead of a traceback."""
    path = Path(path) if path else STRATEGY
    key = str(path)
    if key in _CACHE:
        return _CACHE[key]
    if not path.exists():
        raise strategy_error(
            f'Search strategy not found: {path}',
            'config/search_strategy.json defines the search families and budgets.',
        )
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise strategy_error(f'Malformed search strategy: {path}',
                             f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None
    except (OSError, UnicodeDecodeError) as exc:
        raise strategy_error(f'Search strategy could not be read: {path}',
                             f'{type(exc).__name__}: {exc}') from None
    if not isinstance(data, dict) or not isinstance(data.get('families'), list):
        raise strategy_error(f'Invalid search strategy: {path}',
                             'Expected an object with a "families" list.')
    _CACHE[key] = data
    return data


def families(strategy=None):
    return list((strategy or load_strategy()).get('families', []))


def family_ids(strategy=None):
    return [f.get('id') for f in families(strategy) if f.get('id')]


def get_family(family_id, strategy=None):
    for family in families(strategy):
        if family.get('id') == family_id:
            return family
    raise strategy_error(f'Unknown search family: {family_id!r}',
                         f'Defined families: {", ".join(family_ids(strategy))}')


def is_known_family(family_id, strategy=None):
    return any(f.get('id') == family_id for f in families(strategy))


def mode_budget(mode, strategy=None):
    strategy = strategy or load_strategy()
    modes = strategy.get('modes', {})
    if mode not in modes:
        raise strategy_error(f'Unknown search mode: {mode!r}',
                             f'Defined modes: {", ".join(sorted(modes))}')
    return dict(modes[mode])


def family_query_budget(family_id, mode='deep', strategy=None):
    """Queries this family may plan in this mode.

    A mode declaring `fund_all_mandatory_buckets` gets at least the number of
    buckets that family owes: a per-family cap smaller than the obligation would
    silently defer mandatory work and make the mode's name untrue.
    """
    strategy = strategy or load_strategy()
    family = get_family(family_id, strategy)
    limits = mode_budget(mode, strategy)
    multiplier = float(limits['family_budget_multiplier'])
    budget = max(1, int(int(family['query_budget']) * multiplier))
    if limits.get('fund_all_mandatory_buckets') or limits.get('fund_all_critical_buckets'):
        try:
            import coverage_ledger
            rows = coverage_ledger.bucket_universe(strategy).values()
            if limits.get('fund_all_mandatory_buckets'):
                owed = sum(1 for r in rows if r['owes_interval']
                           and r['search_family'] == family_id)
            else:
                # The bootstrap owes every CRITICAL bucket, plus room for the one
                # rolling route per family it also has to reach.
                owed = sum(1 for r in rows
                           if r['tier'] in ('critical_fresh', 'rolling_recall')
                           and r['search_family'] == family_id)
            budget = max(budget, owed)
        except Exception:  # noqa: BLE001 - an unreadable ledger must not break planning
            pass
    return budget


def min_family_query_reservation(mode='deep', strategy=None):
    """Queries reserved for EVERY applicable family before priority spending.

    Additive and optional: a strategy file without the field reserves nothing and
    plans exactly as it did before, so an older file is never silently migrated.
    A negative value is clamped, because a negative reservation is not a strategy.
    """
    value = mode_budget(mode, strategy).get('min_family_query_reservation', 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def family_candidate_budget(family_id, mode='deep', strategy=None):
    strategy = strategy or load_strategy()
    family = get_family(family_id, strategy)
    multiplier = float(mode_budget(mode, strategy)['family_budget_multiplier'])
    return max(1, int(int(family['candidate_budget']) * multiplier))


def eligible_sources(family_id, strategy=None, registry=None):
    return [s for s in get_family(family_id, strategy).get('eligible_sources', [])]


def requires_body_validation(family_id, strategy=None):
    return bool(get_family(family_id, strategy).get('requires_body_validation'))


def employer_ats_ceiling(mode='deep', strategy=None):
    """How many employer ATS checks this mode may make, outside the query budget."""
    return int(mode_budget(mode, strategy).get('employer_ats_check_ceiling', 0) or 0)


def strategy_problems(strategy=None, registry=None):
    """Every structural problem in the strategy file, including registry drift."""
    try:
        strategy = strategy or load_strategy()
    except SystemExit as exc:
        return [{'problem': 'unloadable', 'detail': str(exc)}]
    registry = registry or load_registry()
    problems = []

    for mode in MODES:
        block = strategy.get('modes', {}).get(mode)
        if not isinstance(block, dict):
            problems.append({'mode': mode, 'problem': 'missing_mode'})
            continue
        for field in REQUIRED_MODE_FIELDS:
            if block.get(field) in (None, ''):
                problems.append({'mode': mode, 'field': field, 'problem': 'required'})
        # Optional and additive, but when present it must be a whole number of
        # queries. A negative or non-numeric reservation is a typo, not a strategy.
        reservation = block.get('min_family_query_reservation')
        if reservation is not None and (isinstance(reservation, bool)
                                        or not isinstance(reservation, int)
                                        or reservation < 0):
            problems.append({'mode': mode, 'field': 'min_family_query_reservation',
                             'problem': 'must_be_a_non_negative_whole_number_of_queries'})

    seen_ids = set()
    for index, family in enumerate(families(strategy)):
        if not isinstance(family, dict):
            problems.append({'index': index, 'problem': 'not_an_object'})
            continue
        fid = family.get('id') or f'#{index}'
        for field in REQUIRED_FAMILY_FIELDS:
            if family.get(field) in (None, ''):
                problems.append({'family': fid, 'field': field, 'problem': 'required'})
        if family.get('id') in seen_ids:
            problems.append({'family': fid, 'problem': 'duplicate_family_id'})
        seen_ids.add(family.get('id'))
        for source_id in family.get('eligible_sources', []) or []:
            if not is_known_source(source_id, registry):
                problems.append({'family': fid, 'source_id': source_id,
                                 'problem': 'not_in_source_registry'})
        slots = set(strategy.get('vocabularies', {}).get('term_slots', []))
        for slot in family.get('term_slots', []) or []:
            if slot not in slots:
                problems.append({'family': fid, 'slot': slot, 'problem': 'not_a_term_slot'})
        for template in family.get('query_templates', []) or []:
            if not isinstance(template, dict) or not template.get('id'):
                problems.append({'family': fid, 'problem': 'malformed_query_template'})
                continue
            for slot in template.get('slots', []) or []:
                if slot not in (family.get('term_slots') or []):
                    problems.append({'family': fid, 'template': template.get('id'),
                                     'slot': slot, 'problem': 'slot_not_declared_by_family'})

    # ---- Window policy. The rule this replaces was yield-based widening, so the
    # single most important assertion is that yield can never widen a window again.
    window = strategy.get('window_policy')
    if not isinstance(window, dict):
        problems.append({'block': 'window_policy', 'problem': 'required'})
    else:
        supported = list(window.get('supported_windows') or [])
        if not supported:
            problems.append({'block': 'window_policy', 'field': 'supported_windows',
                             'problem': 'required'})
        hours = window.get('window_hours') or {}
        for name in supported:
            if not isinstance(hours.get(name), int) or hours.get(name) <= 0:
                problems.append({'block': 'window_policy', 'window': name,
                                 'problem': 'window_hours_must_be_a_positive_whole_number'})
        if window.get('yield_may_widen_window') is not False:
            problems.append({
                'block': 'window_policy', 'field': 'yield_may_widen_window',
                'problem': 'must_be_false',
                'detail': 'A low result count is market supply, never evidence that '
                          'the previous window was missed. Only run history widens.'})
        for field in ('initial_catchup_window', 'max_recovery_window'):
            if window.get(field) not in supported:
                problems.append({'block': 'window_policy', 'field': field,
                                 'problem': 'not_a_supported_window'})
        ladder = window.get('recovery_ladder') or []
        if not ladder:
            problems.append({'block': 'window_policy', 'field': 'recovery_ladder',
                             'problem': 'required'})
        last_gap = 0
        for rung in ladder:
            gap = rung.get('max_gap_hours')
            if not isinstance(gap, (int, float)) or gap <= last_gap:
                problems.append({'block': 'window_policy', 'rung': rung,
                                 'problem': 'recovery_ladder_must_ascend'})
            else:
                last_gap = gap
            if rung.get('window') not in supported:
                problems.append({'block': 'window_policy', 'rung': rung,
                                 'problem': 'not_a_supported_window'})
        if (not isinstance(window.get('daily_interval_hours'), (int, float))
                or window.get('daily_interval_hours') <= 0):
            problems.append({'block': 'window_policy', 'field': 'daily_interval_hours',
                             'problem': 'must_be_a_positive_number_of_hours'})
        # There must be NO timing grace, and the ladder must begin exactly at the
        # daily interval. A grace, or a first rung above the interval, leaves a
        # band of gaps that the daily window is chosen for but cannot cover, and
        # the lost hours are invisible: nothing counts inventory nobody searched.
        if window.get('daily_grace_hours'):
            problems.append({
                'block': 'window_policy', 'field': 'daily_grace_hours',
                'value': window.get('daily_grace_hours'),
                'problem': 'a_timing_grace_creates_invisible_inventory_loss',
                'detail': 'A grace lets a gap longer than the daily window be '
                          'searched with the daily window. Overlap is free; a '
                          'shortfall is not recoverable.'})
        if ladder and window.get('daily_interval_hours') is not None:
            first = ladder[0]
            if (first.get('window') != '24h'
                    or first.get('max_gap_hours') != window.get('daily_interval_hours')):
                problems.append({
                    'block': 'window_policy', 'field': 'recovery_ladder',
                    'problem': 'ladder_must_begin_at_the_daily_interval',
                    'detail': 'The first rung is the daily window and must end '
                              'exactly at daily_interval_hours, so every gap from '
                              'zero upward is covered by some rung.'})
        if window.get('window_must_cover_gap') is not True:
            problems.append({'block': 'window_policy', 'field': 'window_must_cover_gap',
                             'problem': 'must_be_true'})
        # Every rung's window must actually be long enough for the gaps it claims.
        hours = window.get('window_hours') or {}
        for rung in ladder:
            declared = hours.get(rung.get('window'))
            if isinstance(declared, int) and rung.get('max_gap_hours', 0) > declared:
                problems.append({
                    'block': 'window_policy', 'rung': rung,
                    'problem': 'rung_window_shorter_than_the_gap_it_claims',
                    'detail': f'{rung.get("window")} is {declared}h but is offered '
                              f'for gaps up to {rung.get("max_gap_hours")}h.'})

    # ---- Reserved family minimums.
    mins = strategy.get('family_minimums')
    if not isinstance(mins, dict):
        problems.append({'block': 'family_minimums', 'problem': 'required'})
    else:
        classes = mins.get('classes') or {}
        minimums = mins.get('minimums') or {}
        known = set(family_ids(strategy))
        for class_id, want in minimums.items():
            if not isinstance(want, int) or want <= 0:
                problems.append({'block': 'family_minimums', 'class': class_id,
                                 'problem': 'must_be_a_positive_whole_number'})
            if class_id not in classes:
                problems.append({'block': 'family_minimums', 'class': class_id,
                                 'problem': 'has_no_family_class'})
        for class_id, ids in classes.items():
            if class_id not in minimums:
                problems.append({'block': 'family_minimums', 'class': class_id,
                                 'problem': 'has_no_minimum'})
            for fid in ids or []:
                if fid not in known:
                    problems.append({'block': 'family_minimums', 'class': class_id,
                                     'family': fid, 'problem': 'unknown_search_family'})
        if int(mins.get('min_after_scaling', 0) or 0) < 1:
            problems.append({
                'block': 'family_minimums', 'field': 'min_after_scaling',
                'problem': 'must_be_at_least_one',
                'detail': 'A reduced budget may shrink a reserved family but must '
                          'never reduce it to zero: that is the failure the floors exist for.'})
        if int(mins.get('reference_budget', 0) or 0) <= 0:
            problems.append({'block': 'family_minimums', 'field': 'reference_budget',
                             'problem': 'must_be_a_positive_query_budget'})

    # ---- Rotation. A cycle shorter than the number of primary inventory families
    # cannot reach every pairing, which is the exact bug rotation was added to fix.
    rot = strategy.get('rotation')
    if not isinstance(rot, dict):
        problems.append({'block': 'rotation', 'problem': 'required'})
    else:
        primaries = list(rot.get('primary_source_families') or [])
        known_families = {s.get('family') for s in (registry.get('sources') or [])
                          if isinstance(s, dict)}
        for fam in primaries:
            if known_families and fam not in known_families:
                problems.append({'block': 'rotation', 'source_family': fam,
                                 'problem': 'not_an_inventory_family_in_the_registry'})
        if not primaries:
            problems.append({'block': 'rotation', 'field': 'primary_source_families',
                             'problem': 'required'})
        length = rot.get('cycle_length')
        if not isinstance(length, int) or length < 1:
            problems.append({'block': 'rotation', 'field': 'cycle_length',
                             'problem': 'must_be_a_positive_whole_number_of_runs'})
        elif primaries and length < len(primaries):
            problems.append({
                'block': 'rotation', 'field': 'cycle_length', 'value': length,
                'problem': 'cycle_shorter_than_primary_source_families',
                'detail': f'{length} runs cannot reach {len(primaries)} inventory '
                          f'families, so some title-source pairs would be permanently '
                          f'unreachable rather than merely deferred.'})
        if rot.get('advance_on') != 'successful_completed_run':
            problems.append({
                'block': 'rotation', 'field': 'advance_on',
                'problem': 'must_advance_only_on_a_successful_completed_run',
                'detail': 'A failed or partial run did not cover its combinations, so '
                          'advancing past them would retire a debt nobody paid.'})
        for fid in rot.get('rotating_families') or []:
            if fid not in set(family_ids(strategy)):
                problems.append({'block': 'rotation', 'family': fid,
                                 'problem': 'unknown_search_family'})

    # ---- Employer ATS policy.
    ats = strategy.get('employer_ats_policy')
    if not isinstance(ats, dict):
        problems.append({'block': 'employer_ats_policy', 'problem': 'required'})
    else:
        if ats.get('bounded_separately_from_query_budget') is not True:
            problems.append({'block': 'employer_ats_policy',
                             'field': 'bounded_separately_from_query_budget',
                             'problem': 'must_be_true'})
        backoff = list(ats.get('failure_backoff_days') or [])
        if not backoff:
            problems.append({'block': 'employer_ats_policy', 'field': 'failure_backoff_days',
                             'problem': 'required'})
        elif backoff != sorted(backoff) or any(int(v) <= 0 for v in backoff):
            problems.append({
                'block': 'employer_ats_policy', 'field': 'failure_backoff_days',
                'problem': 'must_be_ascending_positive_days',
                'detail': 'A backoff that does not grow is a retry loop with extra steps.'})

    for required in REQUIRED_FAMILIES:
        if required not in seen_ids:
            problems.append({'family': required, 'problem': 'required_family_missing'})
    for family in families(strategy):
        if family.get('id') in NEVER_PLAN_BY_DEFAULT and family.get('plan_by_default', True):
            problems.append({'family': family.get('id'),
                             'problem': 'must_not_be_planned_by_default'})

    signals = strategy.get('body_signals', {})
    if not signals.get('backend_signals'):
        problems.append({'field': 'body_signals.backend_signals', 'problem': 'required'})
    if int(signals.get('min_distinct_signals', 0)) < 2:
        problems.append({'field': 'body_signals.min_distinct_signals',
                         'problem': 'must_require_more_than_one_signal'})
    saturation = strategy.get('saturation_policy', {})
    if int(saturation.get('min_queries_before_saturation', 0)) < 2:
        problems.append({'field': 'saturation_policy.min_queries_before_saturation',
                         'problem': 'one_query_must_not_saturate_a_family'})
    if int(saturation.get('zero_yield_streak_to_saturate', 0)) < 2:
        problems.append({'field': 'saturation_policy.zero_yield_streak_to_saturate',
                         'problem': 'one_empty_query_must_not_saturate_a_family'})
    return problems


def source_family_coverage(source_ids, registry=None):
    """Source families touched by a set of source ids, for diversity reporting."""
    registry = registry or load_registry()
    out = {}
    for source_id in source_ids:
        if is_known_source(source_id, registry):
            out.setdefault(source_family(source_id, registry), []).append(source_id)
    return {family: sorted(set(ids)) for family, ids in sorted(out.items())}


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_list(args):
    strategy = load_strategy()
    rows = []
    for family in families(strategy):
        rows.append({
            'id': family.get('id'),
            'priority': family.get('priority'),
            'query_budget': family.get('query_budget'),
            'candidate_budget': family.get('candidate_budget'),
            'requires_body_validation': family.get('requires_body_validation'),
            'eligible_sources': family.get('eligible_sources', []),
            'purpose': family.get('purpose'),
        })
    rows.sort(key=lambda r: (int(r['priority'] or 9), str(r['id'])))
    print(json.dumps({'count': len(rows), 'families': rows}, indent=2, ensure_ascii=False))


def cmd_get(args):
    print(json.dumps(get_family(args.family_id), indent=2, ensure_ascii=False))


def cmd_budget(args):
    strategy = load_strategy()
    budget = mode_budget(args.mode, strategy)
    budget['min_family_query_reservation'] = min_family_query_reservation(args.mode, strategy)
    budget['families'] = {
        fid: {'query_budget': family_query_budget(fid, args.mode, strategy),
              'candidate_budget': family_candidate_budget(fid, args.mode, strategy)}
        for fid in family_ids(strategy)
    }
    print(json.dumps({'mode': args.mode, **budget}, indent=2, ensure_ascii=False))


def cmd_validate(args):
    problems = strategy_problems()
    print(json.dumps({'strategy': str(STRATEGY.relative_to(ROOT).as_posix()),
                      'valid': not problems, 'problems': problems,
                      'families': family_ids()}, indent=2, ensure_ascii=False))
    raise SystemExit(0 if not problems else 1)


def main():
    p = argparse.ArgumentParser(description='Search-family strategy registry')
    sub = p.add_subparsers(dest='cmd', required=True)

    l = sub.add_parser('list', help='List every search family and its budget.')
    l.set_defaults(func=cmd_list)

    g = sub.add_parser('get', help='Show one search family.')
    g.add_argument('family_id')
    g.set_defaults(func=cmd_get)

    b = sub.add_parser('budget', help='Show resolved budgets for one search mode.')
    b.add_argument('--mode', default='deep', choices=MODES)
    b.set_defaults(func=cmd_budget)

    v = sub.add_parser('validate', help='Validate the strategy against the source registry.')
    v.set_defaults(func=cmd_validate)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

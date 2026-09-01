#!/usr/bin/env python3
"""Productivity metrics for discovery runs, and rolling summaries across them.

WHAT THIS IS FOR. A run already records what it did. What it could not answer was
whether the doing was worth it: which query families actually produced leads,
whether reading seventy job descriptions to find three matches is normal or awful,
and which inventory families contributed anything at all. Without that, tuning the
search is guesswork dressed as judgement.

FOUR RULES THIS MODULE WILL NOT BREAK.

1. IT DERIVES, IT NEVER DECIDES. Nothing here changes a budget, a threshold, a
   window or an allocation. Metrics inform a later human calibration decision.
   A tool that retunes itself from its own output is a feedback loop, and a
   feedback loop over a five-run sample is a random walk.

2. A PARTIAL RUN IS NOT A YIELD OBSERVATION. A run that lost an inventory family
   found fewer roles because it searched less, not because the market was thin.
   Averaging it into a yield summary would teach exactly the wrong lesson, so
   rolling summaries count SUCCESSFUL COMPLETED runs only and say how many they
   found.

3. SMALL SAMPLES SAY SO. Every summary carries its sample size and an explicit
   flag when there is not enough to mean anything. An unqualified average over two
   runs is a lie with a decimal point.

4. NO DIVISION BY ZERO, AND NO ZERO DISGUISED AS A RATE. Every ratio returns None
   when its denominator is zero, never 0.0. A run that read no job descriptions
   has no conversion rate; reporting 0.0 would claim it converted nothing, which
   is a different and false statement.

PRIVACY. Metrics are counts, identifiers and controlled vocabulary. No vacancy
description, no candidate content, no browser or account data, and no query text
beyond the family and source it belonged to. `assert_private` enforces that
mechanically rather than trusting the shape of the code.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1

# The number of successful runs below which a rolling average is not reported as
# a finding. Seven is the rolling window; three is the point at which a mean stops
# being one lucky day.
ROLLING_WINDOW = 7
MIN_SAMPLE_FOR_SIGNAL = 3

# Fields that may never appear in a metrics object. Checked by name AND by shape,
# because the risk is not a developer typing `description_text` on purpose; it is
# a future convenience that carries a whole record through by accident.
FORBIDDEN_FIELDS = (
    'description_text', 'description', 'jd_text', 'body_text', 'raw_html',
    'page_text', 'cv', 'profile', 'candidate', 'email', 'phone', 'address',
    'home_address', 'password', 'cookie', 'session', 'auth', 'token', 'account',
    'username', 'query_text', 'excerpt', 'evidence',
)
MAX_STRING_LENGTH = 400


def metrics_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def _ratio(numerator, denominator, digits=2):
    """A rate, or None. Never 0.0 standing in for an undefined one."""
    try:
        denominator = float(denominator)
    except (TypeError, ValueError):
        return None
    if not denominator:
        return None
    return round(float(numerator or 0) / denominator, digits)


def _per_ten(numerator, denominator):
    return _ratio((numerator or 0) * 10, denominator)


def assert_private(obj, path='metrics'):
    """Every reason this object may not be persisted as metrics.

    Returns a list of problems rather than raising, so a caller can report them
    all at once instead of discovering them one rejection at a time.
    """
    problems = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            lowered = str(key).lower()
            # A number cannot carry a description, so a numeric field is judged by
            # its VALUE rather than by its name. Without this, an honest metric
            # like `new_direct_per_detailed_jd` is refused for its suffix while a
            # real leak in a differently named string field goes through.
            carries_content = isinstance(value, (str, dict, list))
            if carries_content and any(bad == lowered or lowered.endswith('_' + bad)
                                       for bad in FORBIDDEN_FIELDS):
                problems.append({'path': f'{path}.{key}', 'problem': 'forbidden_field',
                                 'detail': 'Metrics are counts and identifiers. This '
                                           'field carries content.'})
            problems.extend(assert_private(value, f'{path}.{key}'))
    elif isinstance(obj, list):
        for index, value in enumerate(obj[:200]):
            problems.extend(assert_private(value, f'{path}[{index}]'))
    elif isinstance(obj, str) and len(obj) > MAX_STRING_LENGTH:
        problems.append({'path': path, 'problem': 'string_too_long_for_a_metric',
                         'length': len(obj), 'max': MAX_STRING_LENGTH,
                         'detail': 'A long string in a metrics object is almost always '
                                   'a description that leaked in.'})
    return problems


def _ats_metrics(block):
    """The employer ATS view, from a ledger or from a pre-ledger run record.

    A run recorded before the ledger existed carries only `checks_made` and
    `checks_ceiling`. Those are mapped onto the fields they meant rather than
    reported as zeros, because a zero here would claim the run made no checks.
    """
    block = block or {}
    if 'counts' in block:
        import ats_budget
        return {**ats_budget.summary(block), 'schema': 'ledger'}
    made = int(block.get('checks_made', 0) or 0)
    failed = int(block.get('checks_failed', 0) or 0)
    return {
        'ceiling': int(block.get('checks_ceiling', 0) or 0),
        'checks_due': int(block.get('employers_due', 0) or 0),
        'checks_reserved': made,
        'checks_attempted': made,
        'checks_succeeded': max(0, made - failed),
        'checks_failed': failed,
        'checks_deferred_by_ceiling': 0,
        'checks_abandoned': 0,
        'remaining': max(0, int(block.get('checks_ceiling', 0) or 0) - made),
        'ceiling_reached': None,
        'reconciles': None,
        # Said plainly, because an older run genuinely cannot answer some of
        # these and a fabricated zero would be worse than an honest gap.
        'schema': 'pre_ledger' if block else 'absent',
    }


def run_metrics(data, summary=None, window_decision=None, plan=None):
    """Every productivity metric for ONE run, derived from its own record.

    `summary` is `discovery_run.summarise(data)`. `window_decision` and `plan` are
    optional: a run that recorded neither still produces valid metrics with those
    sections marked absent, which is what keeps historical runs readable.
    """
    if not isinstance(data, dict):
        raise metrics_error('run_metrics expects one discovery run record.')
    if summary is None:
        import discovery_run as run_mod
        summary = run_mod.summarise(data)

    counts = data.get('counts') or {}
    queries = data.get('queries') or []
    sources = data.get('sources') or []

    def count(field):
        try:
            return int(counts.get(field, 0) or 0)
        except (TypeError, ValueError):
            return 0

    raw = count('raw')
    deep = count('deep_checked')
    new_direct = count('new_direct')
    total_queries = len(queries)

    by_family = Counter()
    by_source = Counter()
    by_source_family = Counter()
    yield_by_family = Counter()
    yield_by_source_family = Counter()
    combinations = set()
    for row in queries:
        if not isinstance(row, dict):
            continue
        family = str(row.get('search_family') or '')
        source = str(row.get('source_id') or '')
        source_family = str(row.get('source_family') or '')
        produced = max(0, int(row.get('new_canonical_candidates', 0) or 0))
        if family:
            by_family[family] += 1
            yield_by_family[family] += produced
        if source:
            by_source[source] += 1
        if source_family:
            by_source_family[source_family] += 1
            yield_by_source_family[source_family] += produced
        # A title-source combination is recorded by FAMILY and inventory family,
        # never by query text: the text can carry nothing private, but it can carry
        # a great deal of noise and it is not what the rotation is measured in.
        if family and source_family:
            combinations.add(f'{family}@{source_family}')

    source_outcomes = Counter(str(s.get('outcome') or '') for s in sources
                              if isinstance(s, dict))
    ats = data.get('employer_ats') or {}
    sponsorship = data.get('sponsorship_checks') or {}

    duration = None
    started, finished = data.get('started_at'), data.get('finished_at')
    if started and finished:
        from datetime import datetime
        try:
            a, b = datetime.fromisoformat(started), datetime.fromisoformat(finished)
            duration = round((b - a).total_seconds() / 60.0, 1)
        except ValueError:
            duration = None

    rotation = data.get('rotation') or (plan or {}).get('rotation') or {}
    decision = window_decision or data.get('window_decision') or {}

    metrics = {
        'schema_version': SCHEMA_VERSION,
        'run_id': data.get('run_id', ''),
        'mode': data.get('mode', ''),
        'successful': bool(summary.get('finished')) and summary.get('coverage_status') != 'PARTIAL',
        'coverage_status': summary.get('coverage_status', ''),
        'window': {
            'selected': (decision.get('window')
                         or (data.get('actual_windows_used') or [''])[-1]
                         or data.get('requested_window', '')),
            'decision': decision.get('decision', ''),
            'reason': str(decision.get('reason', ''))[:MAX_STRING_LENGTH],
            'capped': bool(decision.get('capped')),
            # A run that did not cover its own gap must be visible in the metrics,
            # not only in the sentence explaining the decision.
            'covers_gap': (decision.get('coverage') or {}).get('covers_gap'),
            'uncovered_hours': (decision.get('coverage') or {}).get('uncovered_hours'),
        },
        'rotation': {
            'cycle_index': rotation.get('cycle_index'),
            'cycle_length': rotation.get('cycle_length'),
            'override': rotation.get('override', ''),
        },
        'queries': {
            'total': total_queries,
            'by_search_family': dict(sorted(by_family.items())),
            'by_source': dict(sorted(by_source.items())),
            'by_inventory_family': dict(sorted(by_source_family.items())),
            'title_source_combinations': sorted(combinations),
            'title_source_combination_count': len(combinations),
        },
        'funnel': {
            'raw_candidates': raw,
            'hard_filtered': count('hard_filtered'),
            'duplicates': count('duplicates'),
            'suppressed': count('suppressed'),
            'deep_checked': deep,
            'deferred': count('deferred'),
            'candidates': count('candidates'),
            'new_direct': new_direct,
            'agency': count('agency'),
            'verification': count('verification'),
            'updated': count('updated'),
        },
        'sources': {
            'outcomes': dict(sorted(source_outcomes.items())),
            'families_covered': sorted(summary.get('families_covered') or []),
            'families_covered_with_warnings': sorted(
                summary.get('families_covered_with_warnings') or []),
            'family_gaps': sorted(summary.get('family_gaps') or []),
        },
        # Seven distinguishable outcomes, not one lump. `due` is market supply,
        # `reserved` is what capacity allowed, `attempted` is what actually ran,
        # and `deferred_by_ceiling` is the bounded stop. Collapsing them would
        # make an exhausted ceiling indistinguishable from an empty watchlist.
        'employer_ats': _ats_metrics(ats),
        'sponsorship': {
            'register_lookups_local': int(sponsorship.get('local_lookups', 0) or 0),
            'live_verification_fallbacks': int(sponsorship.get('live_fallbacks', 0) or 0),
        },
        'duration_minutes': duration,
        'derived': {
            # Every one of these is None rather than 0.0 when undefined, so a run
            # that did no work of a given kind cannot be read as having done it
            # badly. `None` means "not measurable here"; 0.0 would mean "measured,
            # and it was nothing", which is a different and usually false claim.
            'new_direct_per_ten_queries': _per_ten(new_direct, total_queries),
            'detailed_read_conversion_rate': _ratio(deep, raw),
            'new_direct_per_detailed_jd': _ratio(new_direct, deep),
            'duplicate_rate': _ratio(count('duplicates'), raw),
            'hard_filter_rate': _ratio(count('hard_filtered'), raw),
            'query_family_contribution': {
                family: _ratio(produced, sum(yield_by_family.values()))
                for family, produced in sorted(yield_by_family.items())},
            'source_family_contribution': {
                family: _ratio(produced, sum(yield_by_source_family.values()))
                for family, produced in sorted(yield_by_source_family.items())},
        },
    }
    return metrics


def rolling(all_metrics, window=ROLLING_WINDOW):
    """Averages across the last `window` SUCCESSFUL runs, sample size stated.

    Ordering is by run id, which is a timestamp, so the newest runs are the ones
    summarised. Partial and failed runs are excluded and counted separately rather
    than silently dropped: knowing that four of the last eleven runs lost coverage
    is itself the most useful thing this summary can say.
    """
    rows = [m for m in (all_metrics or []) if isinstance(m, dict)]
    successful = sorted((m for m in rows if m.get('successful')),
                        key=lambda m: str(m.get('run_id', '')))[-int(window):]
    excluded = [m.get('run_id', '') for m in rows if not m.get('successful')]
    sample = len(successful)

    def mean(path):
        values = []
        for row in successful:
            node = row
            for part in path:
                node = (node or {}).get(part) if isinstance(node, dict) else None
            if isinstance(node, (int, float)) and not isinstance(node, bool):
                values.append(float(node))
        return round(sum(values) / len(values), 2) if values else None

    contribution = Counter()
    for row in successful:
        for family, share in ((row.get('derived') or {})
                              .get('source_family_contribution') or {}).items():
            if isinstance(share, (int, float)):
                contribution[family] += float(share)

    return {
        'schema_version': SCHEMA_VERSION,
        'window': int(window),
        'sample_size': sample,
        'runs_examined': len(rows),
        'excluded_unsuccessful_runs': excluded,
        'sufficient_sample': sample >= MIN_SAMPLE_FOR_SIGNAL,
        'sample_note': (
            f'{sample} successful completed run(s) in the last {window}. That is '
            f'below the {MIN_SAMPLE_FOR_SIGNAL}-run minimum, so these averages '
            f'describe what happened and predict nothing.'
            if sample < MIN_SAMPLE_FOR_SIGNAL else
            f'{sample} successful completed run(s) of the last {window}. Partial and '
            f'failed runs are excluded: they found less because they searched less.'),
        'averages': {
            'queries_per_run': mean(('queries', 'total')),
            'raw_candidates_per_run': mean(('funnel', 'raw_candidates')),
            'deep_checked_per_run': mean(('funnel', 'deep_checked')),
            'new_direct_per_run': mean(('funnel', 'new_direct')),
            'new_direct_per_ten_queries': mean(('derived', 'new_direct_per_ten_queries')),
            'detailed_read_conversion_rate': mean(('derived', 'detailed_read_conversion_rate')),
            'new_direct_per_detailed_jd': mean(('derived', 'new_direct_per_detailed_jd')),
            'duplicate_rate': mean(('derived', 'duplicate_rate')),
            'hard_filter_rate': mean(('derived', 'hard_filter_rate')),
            'duration_minutes': mean(('duration_minutes',)),
        },
        'source_family_contribution': {
            family: round(total / sample, 3) for family, total in
            sorted(contribution.items())} if sample else {},
        'advisory_only': (
            'These figures inform a later human calibration decision. Nothing here '
            'changes a budget, a threshold, a window or an allocation, and no tool '
            'in this workspace rewrites its search strategy from its own output.'),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _all_run_metrics():
    import discovery_run as run_mod
    out = []
    for path in run_mod.run_files():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            out.append(run_metrics(data, run_mod.summarise(data)))
        except SystemExit:
            continue
    return out


def cmd_run(args):
    import discovery_run as run_mod
    run_id = args.run_id or run_mod.latest_run_id()
    data = run_mod.load_run(run_id)
    print(json.dumps(run_metrics(data, run_mod.summarise(data)), indent=2,
                     ensure_ascii=False))


def cmd_rolling(args):
    print(json.dumps(rolling(_all_run_metrics(), window=args.window), indent=2,
                     ensure_ascii=False))


def cmd_check(args):
    """Every stored run's metrics, checked for private content and zero division."""
    rows = _all_run_metrics()
    problems = []
    for row in rows:
        problems.extend({'run_id': row.get('run_id'), **p} for p in assert_private(row))
    print(json.dumps({'runs_checked': len(rows), 'clean': not problems,
                      'problems': problems}, indent=2, ensure_ascii=False))
    raise SystemExit(1 if problems else 0)


def main():
    p = argparse.ArgumentParser(description='Discovery run productivity metrics')
    sub = p.add_subparsers(dest='cmd', required=True)

    r = sub.add_parser('run', help='Metrics for one run.')
    r.add_argument('--run-id', dest='run_id', default='')
    r.set_defaults(func=cmd_run)

    ro = sub.add_parser('rolling', help='Rolling summary of successful runs.')
    ro.add_argument('--window', type=int, default=ROLLING_WINDOW)
    ro.set_defaults(func=cmd_rolling)

    sub.add_parser('check', help='Privacy and safety check of stored metrics.').set_defaults(func=cmd_check)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

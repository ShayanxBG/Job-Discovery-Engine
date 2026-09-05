#!/usr/bin/env python3
"""Bounded query planning and deterministic stopping rules.

A discovery run used to be told to "search broadly", which is not a budget. This
module turns the search strategy, the compact private search profile, and the
source registry into a BOUNDED, reproducible query plan, and then answers the only
question that matters mid-run: should this search family get another query?

TWO PROBLEMS THIS SOLVES.

1. Wasted budget on equivalent queries. "Python Backend Engineer" and "Backend
   Engineer Python" are the same request. So are "Backend Developer Python jobs"
   and "Python Backend Developer". They are deduplicated by the normalised SET of
   significant terms plus the source and the search mode, so word order and filler
   words can never buy extra budget. Deduplication happens BEFORE budget is spent,
   not after results come back.

2. Searching forever at zero yield, or stopping too early. Both are failures. The
   stopping rules are deliberately conservative and evidence backed:

     CONTINUE          more useful work is available in this family
     SATURATED         minimum query coverage met AND the last two consecutive
                       distinct completed queries produced zero NEW canonical
                       candidates
     BUDGET_EXHAUSTED  the family or the run has spent its query budget
     GAP_REMAINS       a query in this family lost coverage to a failed source, so
                       the family cannot honestly be called finished

   One empty query never saturates a family: an empty result is market supply, and
   a single sample of it proves nothing. A FAILED source is not zero yield at all;
   it is missing coverage, so it is excluded from the zero-yield streak entirely
   and leaves the family GAP_REMAINS. Reporting a broken source as a saturated
   family would be the same error as reporting it as `0 results`.

   A productive query resets the streak, so one good query pulls a family back from
   the edge of saturation rather than leaving it condemned by earlier misses.

BUDGETS ARE COUNTED, NEVER TIMED. Query count, candidate count and new canonical
yield are all mechanically checkable from a run record. A prompt cannot reliably
stop a worker at minute fifteen, so no budget here pretends otherwise.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from search_strategy import (  # noqa: E402
    MODES, SATURATION_STATES, families, family_candidate_budget,
    family_query_budget, get_family, is_known_family, load_strategy,
    min_family_query_reservation, mode_budget,
)
from search_profile import load_search_profile  # noqa: E402
from search_rotation import (  # noqa: E402
    cycle_index, cycle_length, expected_families as registry_expected_families,
    family_coverage_plan, plan_note, rotating_families, sources_for_families,
)
import coverage_ledger  # noqa: E402
from sources import (  # noqa: E402
    COMPLETE_OUTCOMES, FAILED_OUTCOMES, get_source, is_known_source, load_registry,
    source_family,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1

WINDOWS = ('24h', '7d', '14d')

# Multi-slot templates anchor on the leading slot's strongest terms. Pairing a
# fourth-listed language with a framework produces a query nobody would run.
ANCHOR_LIMIT = 2


def plan_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def significant_terms(text, strategy=None):
    """The normalised significant tokens of a query, order and filler removed."""
    strategy = strategy or load_strategy()
    ignore = {t.lower() for t in strategy.get('query_dedup', {}).get('ignore_tokens', [])}
    tokens = re.findall(r'[a-z0-9+#.]+', str(text or '').lower())
    return frozenset(t for t in tokens if t and t not in ignore)


def dedup_key(query_text, source_id, mode, strategy=None):
    """Identity of a query as a REQUEST, not as a string.

    Two constructions describing the same term set, against the same source in the
    same mode, are one query. This is what stops a planner spending eight slots on
    eight orderings of three words.
    """
    terms = '|'.join(sorted(significant_terms(query_text, strategy)))
    return f'{mode}::{source_id}::{terms}'


def query_id(dedup, family_id):
    return f'{family_id}-' + hashlib.sha256(dedup.encode('utf-8')).hexdigest()[:10]


def _slot_values(profile, slot, limit):
    values = list(profile.get(slot, []) or [])
    return values[:limit] if limit else values


def _render(template, combo):
    text = template.get('shape', '')
    for slot, value in combo.items():
        text = text.replace('{' + slot + '}', value)
    return re.sub(r'\s+', ' ', text).strip()


def _combinations(profile, template, per_slot, anchor_limit=ANCHOR_LIMIT):
    """Ordered term combinations for one template, anchored on the leading slot.

    The first slot is the ANCHOR and the profile lists it strongest-evidence first,
    so a language/framework template varies frameworks against the primary language
    before it ever reaches the second language. Free cross-product would instead
    spend budget on pairings nobody would search, like a secondary language beside a
    framework the candidate only uses with the primary one.

    The anchor itself is capped, because the third and fourth entries of a skills
    line are rarely what a candidate is actually hired for. That cap is about
    PAIRING and applies only where there is something to pair with: a single-slot
    template such as `{target_titles}` renders one term on its own, so capping it at
    two turned eight target titles into two and left `Python Developer` and
    `Backend Developer` as the family's entire search vocabulary.
    """
    slots = list(template.get('slots', []))
    if not slots:
        return []
    anchor, rest = slots[0], slots[1:]
    anchor_cap = min(per_slot, anchor_limit) if rest else per_slot
    anchor_values = _slot_values(profile, anchor, anchor_cap)
    rest_values = [_slot_values(profile, slot, per_slot) for slot in rest]
    if not anchor_values or any(not v for v in rest_values):
        return []

    combos, seen = [], set()
    for anchor_value in anchor_values:
        depth = max((len(v) for v in rest_values), default=1)
        for index in range(depth):
            combo = {anchor: anchor_value}
            for slot_index, slot in enumerate(rest):
                column = rest_values[slot_index]
                combo[slot] = column[min(index, len(column) - 1)]
            key = tuple(sorted(combo.items()))
            if key in seen:
                continue
            seen.add(key)
            combos.append(combo)
    return combos


def _family_terms(family, profile, per_slot):
    """Every renderable query text this family's templates produce, in order.

    Templates are flattened into ONE ordered term list rather than being consumed
    one at a time, because a family budget spent template by template is spent on
    the first template. A backend-capability budget of ten reached only the
    language/framework pair and never asked about REST APIs or PostgreSQL at all.
    """
    out, seen = [], set()
    for template in family.get('query_templates', []) or []:
        for combo in _combinations(profile, template, per_slot):
            text = _render(template, combo)
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            out.append((text, template.get('id')))
    return out


def _term_source_pairs(terms, targets, offset=0):
    """(term, source) pairs ordered so a bounded budget varies BOTH axes.

    Every pair is produced exactly once, so nothing is lost; only the ORDER changes,
    and the order is the whole point. Sequentially, term one was offered to every
    source before term two was tried at all, so a direct-title budget of eight
    became `Python Developer` on eight boards: broad SOURCE coverage, one search
    term, and every well-fitting vacancy whose advert uses another title missed.

    Pass p offers term i the source at (i + p), so one pass spends each term on a
    different source. The passes are then ordered so that consecutive ones start a
    whole term-list further along the source list instead of sliding one place,
    which is what keeps source coverage from collapsing as term coverage grows:
    four terms over twelve sources at a budget of eight gives four terms across
    EIGHT sources, not four across five.

    `offset` is the rotation cycle index. It shifts the whole matrix by a whole
    number of source positions, so a budget that can only afford the first pass
    still lands on DIFFERENT source pairings than it did last run. Without it the
    first pass was identical every day and the pairs it could not reach were
    unreachable forever rather than merely deferred.
    """
    if not terms or not targets:
        return []
    count, width = len(terms), len(targets)
    passes = sorted(range(width), key=lambda p: (p % count, p // count))
    return [(term, template_id, targets[(index + step + int(offset)) % width])
            for step in passes
            for index, (term, template_id) in enumerate(terms)]


# Which coverage buckets already hold a query in the plan being built, and which
# TERM anchors that bucket. A bucket is (inventory family, search family, term
# cluster): all three change what comes back, so all three are part of its
# identity. Module level because plan_family runs once per search family and a
# bucket is a property of the PLAN.
_BUCKETED = {}
# Inventory families this plan has already reached, so source diversity can
# outrank a second phrasing into a board already covered.
_ANCHORED_FAMILIES = set()
# How many buckets each tier has taken in the plan being built, so a tier quota
# can be enforced across families rather than within one.
_TIER_TAKEN = {}


def _bucket_field(bucket, bucket_windows, field, default=None):
    return (bucket_windows or {}).get(bucket, {}).get(field, default)


def _rolling_share(family_id, quotas, all_buckets):
    """This family's share of the run's rolling quota, proportional to its debt.

    Enforced as a per-family SPLIT rather than a mid-loop yield. A global
    counter looked simpler and behaved badly: families are planned in priority
    order, so the first two exhausted the quota logic and the later ones were
    squeezed by a rule that was never about them.
    """
    want = int((quotas or {}).get('rolling_recall', 0) or 0)
    if want <= 0 or not all_buckets:
        return 0
    mine = sum(1 for r in all_buckets.values()
               if r['tier'] == 'rolling_recall' and r['search_family'] == family_id)
    total = sum(1 for r in all_buckets.values() if r['tier'] == 'rolling_recall')
    if not total or not mine:
        return 0
    return max(1, round(want * mine / total))


def _critical_outstanding(required_buckets, bucket_tiers, bucketed):
    """Any critical bucket in scope that this plan has not funded yet."""
    return any((bucket_tiers or {}).get(b, {}).get('tier') == 'critical_fresh'
               and b not in bucketed
               for b in (required_buckets or ()))


def _unfunded_mandatory(family_id, required_buckets, bucketed):
    """Are there mandatory buckets in this search family nobody has funded yet?"""
    return any(b.split('::')[1] == family_id and b not in bucketed
               for b in (required_buckets or ()))


def _source_candidate_budget(family_budget, source_id, registry):
    """The deeper of the family default and what this SOURCE can actually supply.

    Two numbers described the same thing and disagreed. `candidate_budget` is a
    per-FAMILY default - 40 for direct-title - written when every board returned a
    screen or two. `inspect_cards_per_query` in the source registry is the
    per-SOURCE depth, and for LinkedIn it is 400, set from a 2026-09-03
    measurement: one guest query on a 24-hour window paged to exhaustion at 332
    unique dated vacancies. A plan carrying 40 next to a registry saying 400 asks
    the run to guess, and the smaller number wins by default, which is exactly the
    behaviour that left LinkedIn contributing ten cards.

    The deeper number wins because these are a floor and a capability, not two
    competing limits: the family default says how much is normally worth taking,
    the source says how much is there. Taking the max never reduces any existing
    budget, so a source that declares nothing, or declares less, is unaffected.
    """
    declared = (get_source(source_id, registry) or {}).get(
        'inspect_cards_per_query')
    try:
        declared = int(declared)
    except (TypeError, ValueError):
        return int(family_budget)
    return max(int(family_budget), declared)


def plan_family(family_id, profile, mode='deep', sources=(), window='24h',
                strategy=None, registry=None, seen_keys=None, per_slot=4,
                rotation_index=0, bucket_windows=None, required_buckets=None,
                bucket_tiers=None, tier_quotas=None, deadlines=None,
                at_risk=None, critical_first=False, candidate_pool=False):
    """Bounded, deduplicated queries for one search family."""
    strategy = strategy or load_strategy()
    registry = registry or load_registry()
    family = get_family(family_id, strategy)
    budget = family_query_budget(family_id, mode, strategy)
    # A family's budget is a SOFT preference now, not a wall. It still bounds how
    # many candidates a family bothers to build, generously, and the global
    # earliest-deadline pass decides which of them actually run.
    if candidate_pool:
        budget = 10 ** 6
    candidate_budget = family_candidate_budget(family_id, mode, strategy)
    eligible = [s for s in family.get('eligible_sources', []) if is_known_source(s, registry)]
    targets = [s for s in (sources or eligible) if s in eligible]

    seen_keys = seen_keys if seen_keys is not None else set()
    queries, skipped = [], []
    if not targets:
        return queries, skipped

    terms = _family_terms(family, profile, per_slot)
    # Terms whose token sets nest share a cluster: `Python Developer` subsumes
    # `Python Backend Developer` under conjunctive board matching, so they are one
    # bucket with the shorter term as its anchor. Terms that are not substitutes
    # stay separate, because running one proves nothing about the other.
    clusters = coverage_ledger.cluster_terms([t for t, _tpl in terms], strategy)
    required = coverage_ledger.is_required(family_id, strategy)
    # CARRIER CAPABILITY. A bucket that owes an interval is only advanced by its
    # own query's outcome, and a source DECLARED to ignore the query can only
    # ever record `partial`, so a query planned there can never discharge the
    # bucket: the slot is spent and the checkpoint does not move.
    # `family_capability` already applies this declared fact to the FAMILY's
    # obligation; this applies the same fact to which SOURCE carries the bucket's
    # required query, so an obligation is never assigned to a carrier that
    # cannot discharge it. `unverified` sources still carry and still rotate:
    # unknown is not negative evidence, and the run's own outcome settles it.
    _cannot_execute = {s['id'] for s in (registry.get('sources') or [])
                       if isinstance(s, dict) and s.get('id')
                       and str(s.get('query_execution', '')) == 'ignores_query'}
    # Only the families the rotation policy names actually rotate. Rotating a
    # family whose sources are not primary inventory would shuffle pairings for no
    # coverage gain and make two runs of the same state look different.
    offset = int(rotation_index) if family_id in rotating_families(strategy) else 0
    # PRIORITY: tier, then how overdue, then diversity. Age alone is not value:
    # spending a bounded budget on the oldest bucket regardless of what it is
    # would let a speculative phrasing outrank the candidate's core route simply
    # by having waited longer. Within a tier, debt and rotation break the tie.
    _crit_here = 0
    _rolling_reserved = _rolling_share(family_id, tier_quotas, bucket_tiers)
    _rolling_left = any(
        (bucket_tiers or {}).get(b, {}).get('tier') == 'rolling_recall'
        and b.split('::')[1] == family_id
        for b in (required_buckets or ()))
    _pairs = list(_term_source_pairs(terms, targets, offset))
    if bucket_windows or bucket_tiers:
        _tier_rank = {t: i for i, t in enumerate(coverage_ledger.TIERS)}
        _seen_families = []

        # How many earlier pairs share this pair's term. Interleaving by it
        # makes the ordering take term1/src1, term2/src1, term3/src1 before
        # term1/src2, so an equal-debt tie spreads across TERMS instead of
        # collapsing onto whichever one sorts first alphabetically.
        _term_seen, _occurrence = {}, {}
        for _idx, (_pt, _ptpl, _psid) in enumerate(_pairs):
            _occurrence[_idx] = _term_seen.get(_pt, 0)
            _term_seen[_pt] = _term_seen.get(_pt, 0) + 1
        _order_of = {id(pair): i for i, pair in enumerate(_pairs)}
        _term_rank = {t: i for i, (t, _tpl) in enumerate(terms)}

        def _priority(pair):
            _t, _tpl, _sid = pair
            _c = clusters.get(_t, (_t, _t, ''))[0]
            _fam = source_family(_sid, registry)
            _b = coverage_ledger.bucket_key(_fam, family_id, _c)
            _row = (bucket_tiers or {}).get(_b) or {}
            _tier = _row.get('tier', 'exploratory')
            _win = (bucket_windows or {}).get(_b) or {}
            # Overdue past the cap outranks everything, including a fresher
            # critical bucket: a bucket beyond fourteen days is losing inventory
            # now, and nothing else on the list is.
            _overdue = 0 if _win.get('capped') else 1
            # SLACK, not age, decides which critical bucket is about to breach.
            # Ordering by raw debt let a bucket with plenty of slack outrank one
            # hours from its deadline, purely for having waited longer.
            _dl = (deadlines or {}).get(_b) or {}
            _slack = _dl.get('slack_hours')
            _slack_rank = float(_slack) if isinstance(_slack, (int, float)) else 10 ** 6
            # Never searched owes the entire catch-up window, so it sorts ahead
            # of any elapsed gap.
            _debt = (float('-inf') if _win.get('basis') == 'first_coverage'
                     else -float(_win.get('elapsed_gap_hours') or 0))
            # Diversity: an inventory family not yet touched by this plan
            # outranks a second query into one already reached.
            _fresh_family = 0 if _fam not in _ANCHORED_FAMILIES else 1
            # Debt outranks diversity, and diversity breaks ties. Putting
            # diversity first let a fresh family's newly-searched bucket outrank
            # a bucket that had waited nine days, so specific rolling buckets
            # starved while the tier as a whole looked well served.
            return (_overdue, _tier_rank.get(_tier, 9), _slack_rank, _debt,
                    _occurrence.get(_order_of.get(id(pair), 0), 0),
                    _fresh_family, _term_rank.get(_t, 99), _sid)
        _pairs.sort(key=_priority)
    for text, template_id, source_id in _pairs:
        if len(queries) >= budget:
            break
        # A second query into a bucket this plan already funded buys nothing for
        # COVERAGE: six sponsor boards share one inventory family, CWJobs and
        # Totaljobs share a platform, and a narrower term in the same cluster is
        # subsumed by its anchor. While any mandatory bucket this family can
        # reach is still unfunded, spending a query on a repeat is spending it on
        # inventory already searched instead of inventory nobody has touched.
        _pair_fam = source_family(source_id, registry)
        _pair_cluster = clusters.get(text, (text, text, ''))[0]
        _pair_bucket = coverage_ledger.bucket_key(_pair_fam, family_id, _pair_cluster)
        # A source DECLARED unable to execute the query can never produce the
        # completed-query outcome that advances a checkpoint, so it may not carry
        # a bucket that owes an interval. Skipped with its reason, exactly like a
        # repeat: the capable sibling on the same inventory claims the bucket,
        # and where a restricted run permits no capable sibling the bucket is
        # honestly unfunded rather than fake-covered.
        if (source_id in _cannot_execute
                and ((bucket_tiers or {}).get(_pair_bucket) or {}).get('owes_interval')):
            skipped.append({'query_text': text, 'source_id': source_id,
                            'reason': 'mandatory_bucket_needs_a_query_capable_carrier',
                            'coverage_bucket': _pair_bucket})
            continue
        # In the bootstrap a repeat is always waste: it owes every critical
        # bucket AND one route per family, so a second query into a bucket
        # already funded is a family or an ATS check nobody got.
        # In candidate-pool mode the pool keeps repeats: the GLOBAL pass decides,
        # and a family floor counted in query slots needs a repeat available when
        # the family owns fewer buckets than its guaranteed slots. Suppressing
        # them here left sponsorship with three candidates against a promise of
        # four, which no later stage could repair.
        if _pair_bucket in _BUCKETED and (
                critical_first or candidate_pool
                or _unfunded_mandatory(family_id, required_buckets, _BUCKETED)):
            skipped.append({'query_text': text, 'source_id': source_id,
                            'reason': 'bucket_already_funded_while_mandatory_work_remains',
                            'coverage_bucket': _pair_bucket})
            continue
        key = dedup_key(text, source_id, mode, strategy)
        if key in seen_keys:
            skipped.append({'query_text': text, 'source_id': source_id,
                            'reason': 'equivalent_query_already_planned',
                            'dedup_key': key})
            continue
        seen_keys.add(key)
        _fam = source_family(source_id, registry)
        # A tier that has taken its share yields to a tier still owed one. Without
        # this the critical tier consumed every slot forever and rolling_recall
        # was never searched at all, while its policy advertised a seven-day
        # target: a promise the schedule had no way to keep.
        _pair_tier = ((bucket_tiers or {}).get(
            coverage_ledger.bucket_key(_fam, family_id,
                                       clusters.get(text, (text, text, ''))[0])) or {}
        ).get('tier', 'exploratory')
        # Hold back this family's share of the rolling quota. Without it the
        # critical tier consumed every slot forever and rolling_recall was never
        # searched even once in thirty simulated days, while its policy
        # advertised a seven-day target the schedule could not keep.
        # Counted PER FAMILY. The global counter was the bug: by the time
        # backend-capability planned, direct-title's picks had already exceeded
        # a cap that was never about them, so every capability bucket on DWP was
        # skipped on every run and twenty mandatory buckets went unsearched.
        # A quota is a floor on opportunity and was never meant to cause an
        # avoidable deadline breach. Critical work already at risk takes the slot;
        # the rolling floor is preserved only after that work is funded.
        # The one-time bootstrap owes EVERY critical bucket. Until they are all
        # funded, nothing else may take a slot: Phase 4E let exploratory routes
        # bid against critical work on the very first run and funded 30 of 45,
        # which is a contract the run could not keep.
        # Only when this family is choosing for itself. In candidate-pool mode
        # the GLOBAL pass ranks by deadline, and filtering here starved the pool:
        # direct-title plans first, so its rolling buckets were dropped while
        # other families' critical work was still outstanding, and they never
        # reached the ranking at all.
        if (critical_first and not candidate_pool and _pair_tier != 'critical_fresh'
                and _critical_outstanding(required_buckets, bucket_tiers, _BUCKETED)):
            skipped.append({'query_text': text, 'source_id': source_id,
                            'reason': 'bootstrap_funds_every_critical_bucket_first',
                            'coverage_bucket': _pair_bucket})
            continue
        _blocks_at_risk = _pair_tier == 'critical_fresh' and _pair_bucket in set(at_risk or ())
        if (not candidate_pool and _pair_tier == 'critical_fresh'
                and _rolling_reserved and not _blocks_at_risk
                and _crit_here >= budget - _rolling_reserved and _rolling_left):
            skipped.append({'query_text': text, 'source_id': source_id,
                            'reason': 'held_back_for_the_rolling_recall_quota'})
            continue
        _TIER_TAKEN[_pair_tier] = _TIER_TAKEN.get(_pair_tier, 0) + 1
        if _pair_tier == 'critical_fresh':
            _crit_here += 1
        _ANCHORED_FAMILIES.add(_fam)
        _cluster, _anchor_term, _parent = clusters.get(text, (text, text, ''))
        _bucket = coverage_ledger.bucket_key(_fam, family_id, _cluster)
        # A query is REQUIRED coverage when its search family owes an interval and
        # this is the query actually carrying that bucket's window. A second term
        # in the same cluster is supplemental, and it says WHY: its anchor
        # subsumes it under a named, mechanically checked rule, not because it
        # happens to be on the same website.
        _first_in_bucket = _bucket not in _BUCKETED
        _row = (bucket_tiers or {}).get(_bucket) or {}
        _tier = _row.get('tier', 'exploratory')
        # A task is required coverage only when its BUCKET owes an interval and
        # this is the query carrying it. An exploratory route is still executed
        # and still recorded; it simply cannot advance a checkpoint it does not
        # own, so a cheap adjacent query can never be mistaken for the coverage
        # it did not provide.
        _is_required = (required and _first_in_bucket
                        and _bucket in set(required_buckets or ()))
        _subsumed_by = ''
        if required and not _first_in_bucket and _parent:
            _subsumed_by = _BUCKETED.get(_bucket, '')
        _BUCKETED.setdefault(_bucket, text)
        queries.append({
            'query_id': query_id(key, family_id),
            'search_family': family_id,
            'source_id': source_id,
            'source_family': source_family(source_id, registry),
            'query_text': text,
            'query_terms': sorted(significant_terms(text, strategy)),
            'template_id': template_id,
            'priority': int(family.get('priority', 5)),
            'window': window,
            # The GLOBAL window is what the run as a whole searches. The EFFECTIVE
            # window is what THIS BUCKET needs, from its own last successful
            # search. A returning bucket carrying the global window searches the
            # wrong interval and nothing downstream would report the difference.
            'inventory_family': _fam,
            'coverage_bucket': _bucket,
            'term_cluster': _cluster,
            'query_intent': f'{family_id}:{_cluster}',
            'effective_window': _bucket_field(_bucket, bucket_windows,
                                              'effective_window', window),
            'coverage_basis': _bucket_field(_bucket, bucket_windows, 'basis'),
            'last_successful_coverage': _bucket_field(
                _bucket, bucket_windows, 'last_successful_coverage', ''),
            'elapsed_gap_hours': _bucket_field(_bucket, bucket_windows,
                                               'elapsed_gap_hours'),
            'covers_gap': _bucket_field(_bucket, bucket_windows, 'covers_gap'),
            'uncovered_hours': _bucket_field(_bucket, bucket_windows,
                                             'uncovered_hours'),
            'task_role': 'required_coverage' if _is_required else 'supplemental_recall',
            'required_or_supplemental': 'required' if _is_required else 'supplemental',
            'coverage_tier': _tier,
            'tier_rationale': _row.get('tier_rationale', ''),
            'target_revisit_hours': _row.get('target_revisit_hours'),
            'slack_hours': ((deadlines or {}).get(_bucket) or {}).get('slack_hours'),
            'urgency': ((deadlines or {}).get(_bucket) or {}).get('urgency', ''),
            'deadline_at': ((deadlines or {}).get(_bucket) or {}).get('deadline_at', ''),
            'broader_anchor': _subsumed_by,
            'subsumption_rule': (
                coverage_ledger.coverage_policy(strategy)['subsumption']['rule']
                if _subsumed_by else ''),
            'candidate_budget': _source_candidate_budget(
                candidate_budget, source_id, registry),
            'requires_body_validation': bool(family.get('requires_body_validation')),
            'dedup_key': key,
            'rotation_offset': offset,
            'reason': family.get('purpose', ''),
        })
    return queries, skipped


def family_minimums(mode='deep', strategy=None):
    """Reserved query floors per family for this mode's budget.

    The floors are declared against a reference budget and scale PROPORTIONALLY
    below it, floored at `min_after_scaling`. Scaling rather than a fixed number
    matters because a `quick` 12-query troubleshooting run must still touch the
    early-career and sponsorship families: reducing a required family to zero is
    exactly the failure these floors exist to prevent, and a smaller budget is not
    a reason to stop looking where the vacancy most likely is.
    """
    strategy = strategy or load_strategy()
    block = strategy.get('family_minimums') or {}
    classes = block.get('classes') or {}
    minimums = block.get('minimums') or {}
    reference = max(1, int(block.get('reference_budget', 30)))
    floor = int(block.get('min_after_scaling', 1))
    limits = mode_budget(mode, strategy)
    budget = int(limits['global_query_budget'])
    out = {}
    # A mode that promises to execute every mandatory obligation takes its floors
    # from the obligations themselves. Deriving them is what stops `exhaustive`
    # from meaning less than its name as the bucket set changes underneath it.
    if limits.get(block.get('mandatory_mode_key', 'fund_all_mandatory_buckets')):
        import coverage_ledger
        for bucket, row in coverage_ledger.bucket_universe(strategy).items():
            if row['owes_interval']:
                out[row['search_family']] = out.get(row['search_family'], 0) + 1
        # A derived floor may never fall BELOW a class minimum an earlier phase
        # guarantees: sponsorship owes three buckets but is promised four
        # queries, and a derivation that quietly weakened that promise would be
        # the wrong kind of clever.
        for class_id, family_ids in (block.get('classes') or {}).items():
            want = int((block.get('minimums') or {}).get(class_id, 0) or 0)
            ids = list(family_ids)
            if not ids or want <= 0:
                continue
            base, extra = divmod(want, len(ids))
            for position, fid in enumerate(ids):
                out[fid] = max(out.get(fid, 0), base + (1 if position < extra else 0))
        for fid, want in (block.get('per_family') or {}).items():
            out[fid] = max(out.get(fid, 0), int(want))
        return out
    for class_id, family_ids in classes.items():
        want = int(minimums.get(class_id, 0) or 0)
        if want <= 0:
            continue
        scaled = want if budget >= reference else max(
            floor, int(want * budget / reference))
        # A class covering several families shares its floor between them, with
        # the remainder going to the earliest listed, so the class total is what
        # was promised rather than a per-family multiple of it.
        ids = list(family_ids)
        base, extra = divmod(scaled, len(ids)) if ids else (0, 0)
        for position, fid in enumerate(ids):
            out[fid] = out.get(fid, 0) + base + (1 if position < extra else 0)
    return out


TIER_RANK = {'critical_fresh': 0, 'rolling_recall': 1,
             'watchlist_or_event_driven': 2, 'exploratory': 3}


def global_deadline_order(queries, deadline_rows, bucket_tiers, families_needed,
                          cap_hours=336):
    """Earliest-deadline-first across EVERY family, in the documented order.

    The rank is the whole correction. Phase 4F ordered inside each family and
    then let per-family budgets decide, so a globally urgent bucket lost to a
    locally comfortable one purely because they belonged to different families.
    Deadlines are a property of the workspace, not of a search family.

        0  past the 14-day cap: losing inventory now
        1  breached, most overdue first
        2  will breach before the next run, earliest deadline first
        3  remaining critical, smallest slack
        4  remaining rolling, smallest slack
        5  a route into an inventory family nothing else has reached
        6  event-driven work holding its own reservation
        7  exploratory and supplemental
        8+ deterministic term and source tie-breaks

    Diversity and rotation survive as TIE-BREAKS. They were never supposed to be
    barriers to a deadline.
    """
    seen_families = set()
    ranked = []
    for index, q in enumerate(queries):
        bucket = q['coverage_bucket']
        row = (bucket_tiers or {}).get(bucket) or {}
        dl = (deadline_rows or {}).get(bucket) or {}
        tier = row.get('tier', 'exploratory')
        owes = bool(row.get('owes_interval'))
        slack = dl.get('slack_hours')
        slack = float(slack) if isinstance(slack, (int, float)) else float('inf')
        overdue = dl.get('overdue_hours')
        overdue = float(overdue) if isinstance(overdue, (int, float)) else 0.0
        age = dl.get('current_age_hours')
        beyond_cap = isinstance(age, (int, float)) and float(age) > cap_hours

        if owes and beyond_cap:
            band = 0
        elif owes and dl.get('urgency') == 'breached':
            band = 1
        elif owes and dl.get('urgency') == 'at_risk':
            band = 2
        elif tier == 'critical_fresh':
            band = 3
        elif tier == 'rolling_recall':
            band = 4
        elif q.get('inventory_family') in families_needed:
            band = 5
        elif tier == 'watchlist_or_event_driven':
            band = 6
        else:
            band = 7
        # Within a band: most overdue first, then earliest deadline, then least
        # slack, then a stable tie-break that still varies term and source.
        ranked.append(((band, -overdue, dl.get('deadline_at') or 'zzzz', slack,
                        TIER_RANK.get(tier, 9), q['query_text'], q['source_id'],
                        index), q))
    ranked.sort(key=lambda r: r[0])
    return [q for _rank, q in ranked]


def select_globally(candidates, global_budget, deadline_rows, bucket_tiers,
                    expected_families, reserved_event_driven=2,
                    exploratory_allowance=None, require_family_coverage=False,
                    rolling_cap=None, family_floors=None, service_minimums=None,
                    fund_all_mandatory=False, fund_all_critical=False,
                    exploratory_reservation=0, gate_report=None,
                    term_cluster_minimums=None, diversity_report=None):
    """Take the highest-priority unique buckets the global budget can afford.

    ORDER OF CLAIM, and the order is the correction. Phase 4G ranked everything
    together and trusted the ranking, and the authoritative run showed 31
    exploratory queries funded in exhaustive while six mandatory rolling buckets
    went unsearched. A ranking that CAN be right is not the same as a structure
    that cannot be wrong, so the mandatory work is now claimed in its own passes
    before anything optional is considered at all.

        1. event-driven reservation, which answers to its own ceiling
        2. MANDATORY SERVICE RESERVATION: the deadline-safe minimum number of
           UNIQUE buckets per tier, in earliest-deadline order. Everything when
           the mode declares it funds all mandatory work.
        2b. CRITICAL TERM-CLUSTER BREADTH: a critical family must reach
            several distinct term clusters before any optional work is funded
        3. one route into each inventory family nothing has reached
        4. reserved family floors
        5. remaining mandatory work by deadline
        6. exploratory, and ONLY once THIS RUN'S mandatory obligation is funded

    One bucket, one slot. A repeat buys no deadline capacity and may never
    consume a mandatory service slot.

    Step 6 says THIS RUN'S obligation, not the workspace's. Phase 4H blocked
    exploratory work whenever any of the 73 mandatory buckets was unfunded, and
    on a cold workspace every bucket reads as breached, so in daily and deep the
    condition never cleared and adjacent-software was planned out of every run
    that was not a bootstrap. A bucket the next run's service reservation will
    reach is scheduled, not starved.
    """
    families_needed = set(expected_families or ())
    ordered = global_deadline_order(candidates, deadline_rows, bucket_tiers,
                                    families_needed)
    mandatory = {b for b, r in (bucket_tiers or {}).items() if r.get('owes_interval')}
    in_pool_mandatory = {q['coverage_bucket'] for q in ordered} & mandatory

    chosen, taken, reached, deferred = [], set(), set(), []
    service_counts = {}

    # ---- Three sets, and keeping them apart is the whole Phase 4I correction.
    #
    #   mandatory_universe             every critical and rolling bucket
    #   mandatory_required_this_run    what this run must fund before anything
    #                                  optional: its per-tier service reservation,
    #                                  a route into each required inventory family,
    #                                  and as much breached and at-risk repair as
    #                                  the run can actually afford
    #   mandatory_scheduled_for_future the rest, which a later run's reservation
    #                                  reaches on schedule
    #
    # The repair clause is bounded by capacity on purpose. On a cold workspace
    # all 73 buckets are breached, so an unbounded reading would make every run
    # permanently in deficit and would leave the recall widener at zero forever.
    # What the run cannot fund now it owes later, and the simulations prove the
    # later run collects it: zero skipped intervals, zero never searched.
    _minimums_in = dict(service_minimums or {})
    if fund_all_mandatory:
        _minimums_in = {'critical_fresh': 10 ** 6, 'rolling_recall': 10 ** 6}
    elif fund_all_critical:
        _minimums_in = {**_minimums_in, 'critical_fresh': 10 ** 6}

    _mand_order, _seen_mand = [], set()
    for q in ordered:
        b = q['coverage_bucket']
        if b in mandatory and b not in _seen_mand:
            _seen_mand.add(b)
            _mand_order.append(b)

    def _tier_of_bucket(b):
        return (bucket_tiers or {}).get(b, {}).get('tier', 'exploratory')

    # (a) the per-tier service reservation, in unique BUCKETS, bounded by what
    # this run can actually reach.
    _service_target = {}
    for _tier in ('critical_fresh', 'rolling_recall'):
        _want = int(_minimums_in.get(_tier, 0) or 0)
        _avail = sum(1 for b in in_pool_mandatory if _tier_of_bucket(b) == _tier)
        _service_target[_tier] = min(_want, _avail)

    # (b) how many inventory families this run must route into.
    _families_required = (set(families_needed) if require_family_coverage else set())

    # (c) gap repair: breached and at-risk work, up to what the run can fund
    # without spending the exploratory reservation it declared.
    _urgent = [b for b in _mand_order
               if (deadline_rows or {}).get(b, {}).get('urgency')
               in ('breached', 'at_risk')]
    _allowance = int(global_budget) - int(reserved_event_driven) \
        - int(exploratory_reservation or 0)
    _allowance = max(_allowance, sum(_service_target.values()))
    _mandatory_owed = min(_allowance, len(in_pool_mandatory),
                          max(sum(_service_target.values()), len(_urgent)))

    # The canonical earliest-deadline reading of the same obligation, kept for
    # diagnosis. The allocator may satisfy a family floor with a different but
    # equally valid bucket, so the GATE tests the specification above and this
    # list explains it rather than constraining it.
    required_preview = _mand_order[:_mandatory_owed]
    scheduled_for_future = sorted(mandatory - set(required_preview))

    def _tier_of(q):
        return (bucket_tiers or {}).get(q['coverage_bucket'], {}).get(
            'tier', 'exploratory')

    def _claim(q):
        chosen.append(q)
        taken.add(q['coverage_bucket'])
        reached.add(q.get('inventory_family', ''))
        tier = _tier_of(q)
        service_counts[tier] = service_counts.get(tier, 0) + 1

    def _room():
        return len(chosen) < int(global_budget)

    # 1. Event-driven, off the top and capped.
    for q in ordered:
        if not _room() or service_counts.get('watchlist_or_event_driven', 0) >= \
                int(reserved_event_driven):
            break
        if _tier_of(q) == 'watchlist_or_event_driven' and q['coverage_bucket'] not in taken:
            _claim(q)

    # 2. Mandatory service reservation. These are UNIQUE BUCKETS SERVICED, not
    # query slots: a repeat or a supplemental query can never satisfy one. The
    # bootstrap owes every critical bucket and nothing less; leaving it on the
    # ordinary deadline-safe minimum dropped it to 42 of 45.
    minimums = _minimums_in
    for tier in ('critical_fresh', 'rolling_recall'):
        want = int(minimums.get(tier, 0) or 0)
        for q in ordered:
            if not _room() or service_counts.get(tier, 0) >= want:
                break
            if (_tier_of(q) == tier and q['coverage_bucket'] in mandatory
                    and q['coverage_bucket'] not in taken):
                _claim(q)

    # 2b. Critical term-cluster breadth. A family that searches one phrase on
    # seven boards has searched one thing. The minimum is bounded by the clusters
    # that actually exist, so it can never ask for a duplicate, and it is claimed
    # HERE, before the family floors and long before the exploratory reservation,
    # because breadth in the primary title family outranks recall widening.
    _cluster_of = {}
    for _b, _row in (bucket_tiers or {}).items():
        _cluster_of[_b] = _row.get('term_cluster', '')
    _diversity_rows = {}
    for fid, want in sorted((term_cluster_minimums or {}).items()):
        _available = {_cluster_of.get(q['coverage_bucket'], '') for q in ordered
                      if q['search_family'] == fid}
        _available.discard('')
        _target = min(int(want), len(_available))
        _have = {_cluster_of.get(c['coverage_bucket'], '') for c in chosen
                 if c['search_family'] == fid}
        _have.discard('')
        _claimed_here = 0
        for q in ordered:
            if len(_have) >= _target or not _room():
                break
            if q['search_family'] != fid or q['coverage_bucket'] in taken:
                continue
            _cluster = _cluster_of.get(q['coverage_bucket'], '')
            if not _cluster or _cluster in _have:
                continue
            _claim(q)
            _have.add(_cluster)
            _claimed_here += 1
        _diversity_rows[fid] = {
            'configured_minimum': int(want),
            'available_term_clusters': len(_available),
            'effective_minimum': _target,
            'claimed_by_this_pass': _claimed_here,
        }

    # 3. One route into every inventory family nothing has reached yet.
    if require_family_coverage:
        for q in ordered:
            if not _room():
                break
            fam = q.get('inventory_family', '')
            if (fam in families_needed and fam not in reached
                    and q['coverage_bucket'] not in taken):
                _claim(q)

    def _mandatory_funded():
        return len(taken & mandatory)

    def _obligation_met():
        """Has this run funded what IT owes? Not what the workspace owes.

        Four conditions, all structural:
          the critical service reservation is serviced in unique buckets
          the rolling service reservation is serviced in unique buckets
          every required inventory family has a route
          the run has spent its gap-repair allowance, or exhausted the pool
        """
        for _t, _n in _service_target.items():
            if sum(1 for b in taken if b in mandatory and _tier_of_bucket(b) == _t) < _n:
                return False
        if _families_required - reached:
            return False
        return _mandatory_funded() >= _mandatory_owed

    def _room_for(tier):
        """Mandatory work stops at the exploratory reservation, once owed work
        is funded. Before that the reservation yields, so a breached bucket the
        run is obliged to repair always outranks the widener."""
        if not _room():
            return False
        if (tier != 'exploratory' and int(exploratory_reservation or 0)
                and _obligation_met()
                and len(chosen) >= int(global_budget) - int(exploratory_reservation)):
            return False
        return True

    # 4. Reserved family floors, preferring a term and a source this family has
    # not used yet: a floor buys DIVERSITY, not the same phrase twice.
    for fid, want in sorted((family_floors or {}).items()):
        # A family that owns no mandatory bucket has no floor while this run's
        # own mandatory obligation is unfunded. Honouring it there is optional
        # work displacing mandatory work under a different name.
        _fid_mandatory = any(r.get('search_family') == fid and r.get('owes_interval')
                             for r in (bucket_tiers or {}).values())
        if not _fid_mandatory and not _obligation_met():
            continue
        for q in ordered:
            have = [c for c in chosen if c['search_family'] == fid]
            if len(have) >= int(want) or not _room():
                break
            if q['search_family'] != fid or q['coverage_bucket'] in taken:
                continue
            fresh = (not any(c['query_text'] == q['query_text'] for c in have)
                     and not any(c['source_id'] == q['source_id'] for c in have))
            if not fresh and any(
                    o['search_family'] == fid and o['coverage_bucket'] not in taken
                    and not any(c['query_text'] == o['query_text'] for c in have)
                    and not any(c['source_id'] == o['source_id'] for c in have)
                    for o in ordered):
                continue
            _claim(q)

    # 5. Mandatory and event-driven work in deadline order. Exploratory
    # candidates are set aside for their own passes below, because a diversity
    # preference inside this single forward pass cannot be undone: a query
    # postponed here never came back, and exhaustive fell from twelve
    # adjacent-software queries to two. Measured, then fixed.
    # What the earlier passes actually spent on NON-mandatory work comes off the
    # mandatory allowance before pass 5 begins. A family floor met with an
    # exploratory bucket is legitimate, but it costs a slot, and leaving the
    # allowance at its opening figure let mandatory work run one slot past the
    # reservation and deliver a declared two as a one.
    _nonmandatory_claimed = sum(1 for c in chosen
                                if c['coverage_bucket'] not in mandatory)
    _mandatory_owed = min(_mandatory_owed,
                          max(0, int(global_budget) - _nonmandatory_claimed
                              - int(exploratory_reservation or 0)))

    explor_pool = []
    for q in ordered:
        bucket = q['coverage_bucket']
        tier = _tier_of(q)
        if bucket in taken:
            deferred.append({'query_id': q['query_id'],
                             'search_family': q['search_family'],
                             'source_id': q['source_id'], 'coverage_bucket': bucket,
                             'reason': 'bucket_already_funded_this_run'})
            continue
        if tier == 'exploratory':
            explor_pool.append(q)
            continue
        if not _room_for(tier):
            deferred.append({'query_id': q['query_id'],
                             'search_family': q['search_family'],
                             'source_id': q['source_id'], 'coverage_bucket': bucket,
                             'reason': 'exploratory_reservation_held' if _room()
                             else 'global_query_budget_reached'})
            continue
        if tier == 'watchlist_or_event_driven':
            if not _obligation_met():
                deferred.append({
                    'query_id': q['query_id'], 'search_family': q['search_family'],
                    'source_id': q['source_id'], 'coverage_bucket': bucket,
                    'reason':
                    'optional_work_may_not_run_while_mandatory_work_is_unfunded'})
                continue
            deferred.append({'query_id': q['query_id'],
                             'search_family': q['search_family'],
                             'source_id': q['source_id'], 'coverage_bucket': bucket,
                             'reason': 'event_driven_reservation_filled'})
            continue
        _claim(q)

    # 6. Exploratory work, and ONLY once this run's own obligation is funded.
    #
    #    a. the mode's declared reservation, spent on DIVERSITY: a term and a
    #       source this family has not used yet, wherever one exists
    #    b. a second sweep, in case the reservation could not be filled freshly
    #    c. whatever capacity genuinely remains, in deadline order
    #
    # Preferring a fresh term is free here: what (a) declines, (c) spends.
    _explor_claimed = set()

    def _short_of_floor(fid):
        """Is this family still below its EFFECTIVE unique reservation?"""
        want = int((family_floors or {}).get(fid, 0) or 0)
        if not want:
            return False
        have = len({c['coverage_bucket'] for c in chosen
                    if c['search_family'] == fid})
        return have < want

    _reservation_spent = []

    def _spend_exploratory(limit, prefer_fresh, only_short=False, reservation=False):
        for q in explor_pool:
            if not _room():
                return
            # The RESERVATION counts only what the reservation itself spends. It
            # used to read the global exploratory count, which already included
            # whatever the family-floor pass had claimed on an exploratory
            # bucket, so a reservation of two could deliver one.
            if reservation:
                if len(_reservation_spent) >= limit:
                    return
            elif service_counts.get('exploratory', 0) >= limit:
                return
            if (exploratory_allowance is not None
                    and service_counts.get('exploratory', 0)
                    >= int(exploratory_allowance)):
                return
            if q['coverage_bucket'] in taken:
                continue
            if only_short and not _short_of_floor(q['search_family']):
                continue
            if prefer_fresh:
                have = [c for c in chosen if c['search_family'] == q['search_family']]
                if (any(c['query_text'] == q['query_text'] for c in have)
                        or any(c['source_id'] == q['source_id'] for c in have)):
                    continue
            _explor_claimed.add(q['query_id'])
            if reservation:
                _reservation_spent.append(q['query_id'])
            _claim(q)

    if _obligation_met():
        _reserved = int(exploratory_reservation or 0)
        if _reserved:
            # The reservation belongs to whichever family is still short of its
            # effective floor. Spending it on a family already funded past its
            # floor makes the reservation a number rather than a guarantee.
            _spend_exploratory(_reserved, prefer_fresh=True, only_short=True,
                               reservation=True)
            _spend_exploratory(_reserved, prefer_fresh=False, only_short=True,
                               reservation=True)
            _spend_exploratory(_reserved, prefer_fresh=True, reservation=True)
            _spend_exploratory(_reserved, prefer_fresh=False, reservation=True)
        _spend_exploratory(10 ** 9, prefer_fresh=False)

    for q in explor_pool:
        if q['query_id'] in _explor_claimed:
            continue
        if q['coverage_bucket'] in taken:
            _reason = 'bucket_already_funded_this_run'
        elif not _obligation_met():
            _reason = 'optional_work_may_not_run_while_mandatory_work_is_unfunded'
        elif (exploratory_allowance is not None
              and service_counts.get('exploratory', 0) >= int(exploratory_allowance)):
            _reason = 'exploratory_allowance_filled'
        else:
            _reason = 'global_query_budget_reached'
        deferred.append({'query_id': q['query_id'],
                         'search_family': q['search_family'],
                         'source_id': q['source_id'],
                         'coverage_bucket': q['coverage_bucket'],
                         'reason': _reason})

    # Which family gave up the marginal slot. The first query the budget
    # refused is the lowest-priority funded surplus by construction: the ordered
    # pass funds by deadline, so what falls off the end is what was worth least.
    _marginal = next((row for row in deferred
                      if row.get('reason') == 'global_query_budget_reached'), None)
    if diversity_report is not None:
        for fid, row in _diversity_rows.items():
            _final = {_cluster_of.get(c['coverage_bucket'], '') for c in chosen
                      if c['search_family'] == fid}
            _final.discard('')
            _same_family_depth = max(
                0, sum(1 for c in chosen if c['search_family'] == fid) - len(_final))
            row.update({
                'term_clusters_covered': len(_final),
                'satisfied': len(_final) >= row['effective_minimum'],
                'surplus_same_family_depth': _same_family_depth,
                'donor_family': (fid if row['claimed_by_this_pass']
                                 and _same_family_depth else
                                 (_marginal or {}).get('search_family', '')),
                'donor_reason': (
                    f"{fid} funded {_same_family_depth} extra querie(s) on term "
                    f"clusters it had already reached, which is funded surplus "
                    f"above its effective obligation. Breadth was taken from "
                    f"that depth before any other family was touched."
                    if row['claimed_by_this_pass'] and _same_family_depth else
                    'no reallocation was needed: the breadth minimum was already '
                    'satisfied by higher-priority passes'),
            })
        diversity_report.update(_diversity_rows)

    _explored = service_counts.get('exploratory', 0)
    _met = bool(_obligation_met())
    if gate_report is not None:
        gate_report.update({
            'mandatory_universe': len(mandatory),
            'mandatory_reachable_this_run': len(in_pool_mandatory),
            'mandatory_required_this_run': _mandatory_owed,
            'mandatory_required_preview': required_preview,
            'mandatory_funded': _mandatory_funded(),
            'mandatory_scheduled_for_future': len(scheduled_for_future),
            'service_reservation_target': dict(_service_target),
            'service_reservation_serviced': {
                _t: sum(1 for b in taken
                        if b in mandatory and _tier_of_bucket(b) == _t)
                for _t in _service_target},
            'inventory_families_required': len(_families_required),
            'inventory_families_reached': len(_families_required & reached),
            'urgent_mandatory_in_pool': len(_urgent),
            'mandatory_allowance': _allowance,
            'exploratory_reservation': int(exploratory_reservation or 0),
            'exploratory_planned': _explored,
            'exploratory_permitted': _met,
            'exploratory_permitted_because': ([
                'all current-run mandatory obligations funded',
                'remaining globally mandatory buckets are scheduled for future service',
                'mode permits exploratory work',
                'query capacity remains',
            ] if _met else []),
            'exploratory_blocked_because': ([] if _met else [
                'a mandatory bucket this run owes is still unfunded']),
        })

    unfunded = sorted(mandatory - taken)
    return chosen, deferred, unfunded


def allocate(order, planned_by_family, global_budget, reservation, minimums=None,
             exploratory_only=()):
    """How many of each family's planned queries the global budget can afford.

    Two passes, both in priority order. Every APPLICABLE family - one that actually
    produced queries - is reserved a small minimum first; what is left is then spent
    by priority, so the direct and backend families still take most of it.

    Priority-only spending starved the tail. The six default families are budgeted
    at 48 queries against a deep budget of 36, so the first four consumed all of it
    and early-career and sponsorship-oriented planned nothing at all. For a profile
    that is early-career and will need sponsorship, those are two of the families
    most likely to hold the vacancy, and zero coverage of them is missing coverage
    rather than a decision anybody made.

    A family that produced no queries - unsupported by the profile, or with no
    eligible source in this run - is reserved nothing, because reserving budget for
    a family that cannot use it would only take it from one that can.

    THREE PASSES, and the first one is new. Reserved class minimums are taken off
    the top, then the flat per-family reservation, then priority spends the rest.
    Two passes were not enough: with a 36-query budget and six families budgeted at
    48, priority reached early-career and sponsorship-oriented with two queries
    each, which is the flat reservation and nothing more. For a candidate who is
    early-career and will need sponsorship those are the two families most likely
    to hold the vacancy. A lower-priority family can no longer consume a reserved
    allocation, because the reservation is gone before priority is consulted.
    """
    minimums = minimums or {}
    take = {fid: 0 for fid in order}
    remaining = int(global_budget)
    # A family whose buckets are ALL exploratory owes no interval, so it gets its
    # flat reservation and no more while any mandatory bucket is unfunded.
    # Without this, adjacent-software took eight queries on the very first run by
    # priority alone, while forty-one mandatory buckets went unsearched: a recall
    # widener outbidding the coverage it is supposed to widen.
    capped_to_reservation = set(exploratory_only or ())
    for pass_want in (minimums, {fid: reservation for fid in order}):
        for fid in order:
            available = len(planned_by_family.get(fid) or []) - take[fid]
            want = min(max(0, int(pass_want.get(fid, 0)) - take[fid]), available, remaining)
            take[fid], remaining = take[fid] + want, remaining - want
    for fid in order:
        if fid in capped_to_reservation:
            continue
        want = min(len(planned_by_family.get(fid) or []) - take[fid], remaining)
        take[fid], remaining = take[fid] + want, remaining - want
    # Anything still unspent may go to the exploratory families: spare budget is
    # exactly what exploratory work is for.
    for fid in order:
        want = min(len(planned_by_family.get(fid) or []) - take[fid], remaining)
        take[fid], remaining = take[fid] + want, remaining - want
    return take


def build_plan(profile, mode='deep', window='24h', family_ids=(), sources=(),
               strategy=None, registry=None, rotation_index=0, rotation_override='',
               records=None, summaries=None, now='', successful_runs=None):
    """A whole bounded run plan, families in priority order.

    Priority order is the point: the highest-signal direct queries are planned
    first, so a plan truncated by the global budget keeps the queries most likely
    to yield rather than whichever family happened to be enumerated first. It is
    not the ONLY point, which is why `allocate` reserves a minimum for every
    applicable family before priority spends the rest.
    """
    strategy = strategy or load_strategy()
    registry = registry or load_registry()
    if mode not in MODES:
        raise plan_error(f'Unknown search mode: {mode!r}', f'Allowed: {", ".join(MODES)}')
    if window not in WINDOWS:
        raise plan_error(f'Unknown window: {window!r}', f'Allowed: {", ".join(WINDOWS)}')

    limits = mode_budget(mode, strategy)
    global_budget = int(limits['global_query_budget'])
    # A family may opt out of default planning. gapfill does: it exists to repair a
    # RECORDED coverage gap, so planning it unprompted would spend budget re-running
    # a family that is already covered.
    wanted = list(family_ids) or [f['id'] for f in families(strategy)
                                  if f.get('plan_by_default', True)]
    for fid in wanted:
        if not is_known_family(fid, strategy):
            raise plan_error(f'Unknown search family: {fid!r}')

    ordered = sorted(
        (get_family(fid, strategy) for fid in wanted),
        key=lambda f: (int(f.get('priority', 5)), str(f.get('id'))),
    )
    if mode_budget(mode, strategy).get('fund_all_critical_buckets'):
        # In the bootstrap, every family carrying critical work plans BEFORE the
        # supplemental ones. Priority order alone put adjacent-software and
        # employer-ats ahead of early-career and sponsorship, so the critical-first
        # rule blocked them out entirely and the run reached six families instead
        # of thirteen: the right rule applied in the wrong order.
        _crit_families = {r['search_family']
                          for r in coverage_ledger.bucket_universe(strategy).values()
                          if r['tier'] == 'critical_fresh'}
        ordered = sorted(ordered, key=lambda f: (
            0 if f['id'] in _crit_families else 1,
            int(f.get('priority', 5)), str(f.get('id'))))

    # Which inventory families this mode is responsible for right now. Sources
    # outside them are not planned at all, so a daily run's omissions are a
    # recorded decision rather than whatever the budget happened to reach.
    # The family cycle counts successful runs directly. Deriving it from the
    # title index would give (successes % 5) % 3 and a false three-run cycle.
    if records is None:
        try:
            from search_window import _history
            records, summaries = _history()
        except Exception:  # noqa: BLE001 - unreadable history means first coverage
            records, summaries = [], {}
    coverage = family_coverage_plan(
        mode, rotation_index, registry,
        successful_runs=(successful_runs if successful_runs is not None
                         else rotation_index),
        records=records, summaries=summaries, now=now or None)
    # A run restricted to a subset of sources cannot be REQUIRED to reach an
    # inventory family that no permitted source serves. Policy still says the
    # family is required; this run simply has no route to it, which is a
    # different fact and is recorded as one.
    _restricted = bool(sources)
    _policy_required = set(coverage['expected_families']
                           or coverage['planned_families'])
    _reach = _reachable_families(sources, registry)
    _source_reachable = _reach['reachable']
    if not sources:
        # Sources for every expected family, so a mandatory bucket is always
        # reachable. Which of them a run actually spends on is decided by the
        # global deadline pass, not by the rotation.
        sources = sources_for_families(
            coverage['expected_families'] or coverage['planned_families'], registry)

    # Each family's own effective window, from its own last successful coverage.
    # Read from run history when the caller did not supply it, so an ordinary
    # `search_plan.py plan` is correct without the caller knowing to ask.
    # Every REQUIRED bucket reachable from the families in scope, with its own
    # window measured from its own last successful search.
    _tier_quotas = ((coverage_ledger.tier_policy(strategy).get('run_quotas') or {})
                    .get(mode) or {})
    # The one-time bootstrap owes every critical bucket, so it reserves no
    # rolling floor that could displace one.
    if mode_budget(mode, strategy).get('fund_all_critical_buckets'):
        _tier_quotas = {}
    _all_buckets = coverage_ledger.bucket_universe(strategy, registry, profile)
    _universe = {b: r for b, r in _all_buckets.items() if r['owes_interval']}
    # MANDATORY scope is every expected inventory family, never only the ones
    # this cycle position happens to plan. The family rotation is a preference
    # about where to spend spare effort; letting it bound the mandatory set put
    # rolling buckets on non-due families out of reach for whole cycles and
    # pushed rolling to 192 hours against a 168-hour target. No inventory-family
    # rotation may prevent a feasible global mandatory plan.
    # MANDATORY scope is every expected inventory family this run can actually
    # reach. A bucket on a family no permitted source serves is not deferred by
    # a budget, it is out of this run's reach, and calling it deferred would
    # invent a coverage debt the next unrestricted run does not owe.
    _scope_families = _policy_required & _source_reachable
    _unreachable_families = sorted(_policy_required - _source_reachable)
    _in_scope = {b for b, row in _universe.items()
                 if row['inventory_family'] in _scope_families}
    _unreachable_buckets = sorted(
        b for b, row in _universe.items()
        if row['inventory_family'] in set(_unreachable_families))
    bucket_windows = coverage_ledger.bucket_windows(
        _in_scope, records, summaries, now or None, strategy, window)
    # Deadlines, not just ages. A bucket comfortable now and breached by the next
    # run has to be searched NOW, and nothing measurable from its current age
    # alone would have said so.
    # The at-risk horizon must be the OBSERVED interval to the next run, not a
    # fixed 24 hours. At a 30-hour cadence a bucket with 25 hours of slack is
    # comfortable by a 24-hour horizon and breached by the time anything can
    # search it again, so the pull-forward never fired and critical drifted to
    # 90 hours while capacity sat unused.
    _cadence = _observed_cadence_hours(records, summaries)
    _deadlines = coverage_ledger.deadlines(
        {b: _all_buckets[b] for b in _in_scope}, bucket_windows, now or None,
        strategy, next_run_in_hours=_cadence)
    _at_risk = set(coverage_ledger.at_risk_buckets(_deadlines))
    _BUCKETED.clear()
    _ANCHORED_FAMILIES.clear()
    _TIER_TAKEN.clear()

    seen_keys, planned_by_family, skipped, deferred = set(), {}, [], []
    for family in ordered:
        fid = family['id']
        # A family with no usable terms is not planned at all. An early-career
        # family is never forced onto a profile whose seniority band excludes it.
        family_queries, family_skipped = plan_family(
            fid, profile, mode=mode, sources=sources, window=window,
            strategy=strategy, registry=registry, seen_keys=seen_keys,
            rotation_index=0 if rotation_override else rotation_index,
            bucket_windows=bucket_windows, required_buckets=_in_scope,
            bucket_tiers=_all_buckets, tier_quotas=_tier_quotas,
            deadlines=_deadlines, at_risk=_at_risk,
            critical_first=bool(limits.get('fund_all_critical_buckets')),
            candidate_pool=True)
        planned_by_family[fid] = family_queries
        skipped.extend(family_skipped)

    order = [f['id'] for f in ordered]
    reservation = min_family_query_reservation(mode, strategy)
    minimums = family_minimums(mode, strategy)
    _mandatory_families = {r['search_family'] for r in _universe.values()}
    _exploratory_only = [fid for fid in order if fid not in _mandatory_families]

    # GLOBAL earliest-deadline-first. Per-family budgets are soft preferences
    # from here on: a deadline belongs to the workspace, not to a search family,
    # and letting a family cap block a globally urgent bucket was the whole
    # defect. Hard limits survive: the global query budget, the event-driven
    # reservation, and the ATS ceiling enforced elsewhere.
    _candidates = []
    for fid in order:
        _candidates.extend(planned_by_family.get(fid) or [])
    _gate, _diversity = {}, {}
    _explore_allow = None
    if limits.get('fund_all_critical_buckets'):
        _explore_allow = int((limits.get('budget_derivation') or {}).get(
            'bounded_exploratory_allowance', 4))
    # Reachability refined by the pool itself: a family with a permitted source
    # but no applicable search-family task is equally out of reach this run.
    _pool_reachable = {q.get('inventory_family', '') for q in _candidates
                       if q.get('inventory_family')}

    # ---- Reservations bounded by the unique work that actually exists.
    #
    #   effective = min(configured, available_unique_executable_tasks,
    #                   remaining_mode_capacity)
    #
    # Available tasks are counted in unique COVERAGE BUCKETS, because the floor
    # pass claims a bucket at most once: two queries against one bucket are one
    # unique task, and counting them as two would promise capacity that cannot
    # be spent on anything new.
    _configured_floors = {**{fid: reservation for fid in order
                             if planned_by_family.get(fid)},
                          **minimums}
    _avail_unique = {
        fid: len({q['coverage_bucket'] for q in (planned_by_family.get(fid) or [])})
        for fid in _configured_floors}
    # The floor the allocator AIMS at: bounded by unique work and the budget.
    _effective_floors = {
        fid: min(int(want), _avail_unique.get(fid, 0), int(global_budget))
        for fid, want in _configured_floors.items()}
    # Which families can be funded out of mandatory service at all. A family
    # owning no mandatory bucket can only be paid from what is left after the
    # run's service obligation, so ITS remaining capacity is that remainder and
    # not the whole budget. Reporting the whole budget there would call a
    # correctly prioritised daily run a floor violation.
    _owns_mandatory = {row['search_family'] for row in _all_buckets.values()
                       if row.get('owes_interval')}
    _required_families_this_run = sorted(
        set(coverage['planned_families']) & _source_reachable & _pool_reachable)
    _unreachable_rows = [
        {'inventory_family': _fam,
         'reason': _unreachable_reason(_fam, sources, registry, _restricted,
                                       _source_reachable, _pool_reachable),
         'counted_as_deferred': False, 'counted_as_unfunded': False,
         'counted_as_failed': False}
        for _fam in sorted(_policy_required
                           - (_source_reachable & _pool_reachable))]
    queries, deferred, _unfunded_mandatory_global = select_globally(
        _candidates, global_budget, _deadlines, _all_buckets,
        _required_families_this_run, exploratory_allowance=_explore_allow,
        require_family_coverage=bool(
            limits.get('fund_all_critical_buckets')
            or limits.get('fund_all_mandatory_buckets')),
        rolling_cap=None,
        family_floors=_effective_floors,
        # The deadline-safe MINIMUM UNIQUE BUCKETS this run must service, derived
        # from the observed cadence rather than assumed. This is what turns a
        # feasibility calculation into a schedule that actually delivers it.
        service_minimums={
            tier: coverage_ledger.deadline_safe_slots(tier, _cadence, strategy)
            for tier in ('critical_fresh', 'rolling_recall')},
        fund_all_mandatory=bool(limits.get('fund_all_mandatory_buckets')),
        fund_all_critical=bool(limits.get('fund_all_critical_buckets')),
        # Slots the mode holds back for recall widening, spendable ONLY once this
        # run's own mandatory obligation is funded. Zero for daily, which may
        # legitimately spend everything on service, and zero for exhaustive and
        # the bootstrap, which owe every bucket they are asked for.
        exploratory_reservation=int(limits.get('exploratory_reservation', 0) or 0),
        gate_report=_gate,
        # Critical breadth, claimed before the exploratory reservation so the
        # recall widener can never cost the primary title family a target title.
        term_cluster_minimums=(
            (strategy.get('allocation_policy') or {})
            .get('critical_term_cluster_minimums') or {}),
        diversity_report=_diversity)

    planned_families = sorted({q['search_family'] for q in queries})
    planned_sources = sorted({q['source_id'] for q in queries})
    return {
        'schema_version': SCHEMA_VERSION,
        'mode': mode,
        'window': window,
        'global_query_budget': global_budget,
        'global_raw_candidate_ceiling': int(limits['global_raw_candidate_ceiling']),
        'global_deep_jd_ceiling': int(limits['global_deep_jd_ceiling']),
        'min_family_query_reservation': reservation,
        'employer_ats_check_ceiling': int(limits.get('employer_ats_check_ceiling', 0) or 0),
        'family_minimums': minimums,
        # A floor is met when the family reaches it, OR when it has spent every
        # unique bucket it owns. Demanding more would demand a duplicate query,
        # which a stronger invariant forbids and which searches nothing new.
        'family_minimums_met': {
            fid: (sum(1 for q in queries if q['search_family'] == fid) >= want
                  or sum(1 for q in queries if q['search_family'] == fid)
                  >= len({b for b, r in _all_buckets.items()
                          if r['search_family'] == fid}))
            for fid, want in sorted(minimums.items())},
        'family_coverage': _settle_coverage(coverage, queries, bucket_windows,
                                            mode, registry),
        'exploratory_gate': _gate,
        'critical_term_diversity': _diversity,
        'family_reservations': _family_reservations(
            _configured_floors, _avail_unique, _owns_mandatory, queries,
            int(global_budget), _gate),
        # Requirement and reachability are separate facts, and a reader can see
        # both rather than inferring one from a short plan.
        'inventory_family_reachability': {
            'run_restricted_to_sources': sorted(sources) if _restricted else [],
            'policy_required_inventory_families': sorted(_policy_required),
            'reachable_inventory_families': sorted(_source_reachable
                                                   & _pool_reachable),
            'required_inventory_families_this_run': _required_families_this_run,
            'unreachable_due_to_run_constraints': [
                _r for _r in _unreachable_rows
                if _r['reason']['controlling_reason']
                == 'no_permitted_source_serves_this_family'],
            'unreachable_for_other_reasons': [
                _r for _r in _unreachable_rows
                if _r['reason']['controlling_reason']
                != 'no_permitted_source_serves_this_family'],
            'buckets_out_of_reach_this_run': _unreachable_buckets,
            'note': ('An unreachable family is not deferred, unfunded or failed. '
                     'It is out of this run\'s reach, its checkpoint does not '
                     'advance, and removing the restriction restores the normal '
                     'obligation. A source that FAILS at execution time is a '
                     'failure, never an unreachable family: reachability is '
                     'decided from the registry and the candidate pool before '
                     'anything is searched.'),
        },
        'bucket_coverage': _settle_buckets(_universe, _in_scope, bucket_windows,
                                           queries, mode, _all_buckets,
                                           _tier_quotas, _deadlines),
        'rotation': {
            'cycle_index': 0 if rotation_override else int(rotation_index),
            'cycle_length': cycle_length(strategy),
            'rotating_families': list(rotating_families(strategy)),
            'override': rotation_override,
            'note': plan_note(0 if rotation_override else int(rotation_index),
                              strategy, rotation_override),
        },
        'queries_planned': len(queries),
        'queries_deferred': len(deferred),
        'queries_deduplicated': len(skipped),
        'search_families_planned': planned_families,
        'search_family_count': len(planned_families),
        'sources_planned': planned_sources,
        'source_family_coverage': sorted({source_family(s, registry) for s in planned_sources}),
        'family_budgets': {f['id']: {
            'query_budget': family_query_budget(f['id'], mode, strategy),
            'candidate_budget': family_candidate_budget(f['id'], mode, strategy),
            'planned': sum(1 for q in queries if q['search_family'] == f['id']),
            'reserved_minimum': minimums.get(f['id'], 0),
        } for f in ordered},
        'deferred_by_family': {fid: sum(1 for row in deferred if row['search_family'] == fid)
                               for fid in order},
        'queries': queries,
        'deduplicated': skipped,
        'deferred': deferred,
    }


def _observed_cadence_hours(records, summaries, default=24.0):
    """How long this workspace actually waits between successful runs.

    Measured from the last few successful completions rather than assumed. A
    schedule that assumes a daily cadence while running every thirty hours will
    keep judging buckets comfortable that are already out of time.
    """
    from search_window import _parse, run_is_successful
    summaries = summaries or {}
    stamps = sorted(
        _parse(r.get('finished_at')) for r in (records or [])
        if run_is_successful(r, summaries.get(r.get('run_id')))
        and _parse(r.get('finished_at')))
    if len(stamps) < 2:
        return float(default)
    gaps = [(b - a).total_seconds() / 3600.0 for a, b in zip(stamps[-6:], stamps[-5:])]
    # The LARGEST recent gap, not the mean: planning for the average interval
    # under-protects exactly the runs that arrive late.
    return max(float(default), round(max(gaps), 2))


def _family_reservations(configured, available, owns_mandatory, queries, budget,
                         gate, event_reservation=2):
    """Configured, available, effective and funded, per family, with the reason.

    `remaining_mode_capacity` differs by family on purpose. A family that owns
    mandatory buckets is paid out of the run's service obligation, so its
    capacity is the budget. A family that owns none can only be paid from what
    is left once that obligation is funded, and holding it to the whole budget
    would report a correctly prioritised daily run as a floor violation.
    """
    owed = int((gate or {}).get('mandatory_required_this_run') or 0)
    remainder = max(0, int(budget) - int(event_reservation) - owed)
    # Did the run finish with a slot it chose not to spend? If it did not, a
    # family below its floor lost to work of equal or higher priority, which is
    # the allocation doing its job. If it DID, the floor was missed while
    # capacity sat unused, and that is a defect.
    spare = max(0, int(budget) - len(queries))
    out = {}
    for fid in sorted(configured):
        want = int(configured[fid])
        avail = int(available.get(fid, 0))
        capacity = int(budget) if fid in owns_mandatory else remainder
        effective = min(want, avail, capacity)
        funded = len({q['coverage_bucket'] for q in queries
                      if q['search_family'] == fid})
        out[fid] = {
            'configured_reservation': want,
            'available_unique_tasks': avail,
            'remaining_mode_capacity': capacity,
            'effective_unique_reservation': effective,
            'funded_unique_tasks': funded,
            'unspent_budget_slots': spare,
            'shortfall_reason': _reservation_shortfall(
                want, avail, effective, funded, capacity, spare),
        }
    return out


def _reservation_shortfall(configured, available, effective, funded, budget,
                           spare=0):
    """Why a family received fewer unique tasks than its configured reservation.

    A reservation is a floor on OPPORTUNITY, not a quota to be padded. When the
    unique work does not exist, the honest answer is the smaller number and the
    reason for it, never a duplicate query issued to make a figure look right.
    """
    if funded >= configured:
        return 'none: the configured reservation was met in full'
    if available < configured:
        return (f'bounded by unique executable capacity: only {available} unique '
                f'task(s) exist for this family after source restriction, '
                f'applicability, query construction, controlled subsumption and '
                f'duplicate suppression, against a configured {configured}. A '
                f'further query would repeat a bucket and search nothing new.')
    if effective < configured:
        return (f'bounded by remaining mode capacity: {budget} slot(s) remained '
                f'for this family after the run funded its mandatory service '
                f'obligation, against a configured {configured}.')
    if funded < effective and not spare:
        return (f'funded {funded} of an effective {effective}: the run spent its '
                f'whole budget on work of equal or higher priority, so no slot '
                f'remained for this floor. The floor is an opportunity, not a '
                f'claim that outranks a deadline.')
    if funded < effective:
        return (f'DEFECT: funded {funded} of an effective {effective} while '
                f'{spare} budget slot(s) went unspent.')
    return 'none'


def _reachable_families(sources, registry):
    """Which inventory families a run could reach, and which sources serve them.

    Applies the source restriction, the enabled state and queryability. Search
    family applicability and candidate construction are applied later, against
    the pool itself, because only the pool knows whether a query could be built.
    """
    rows = (registry or {}).get('sources') or []
    allowed = set(sources or ())
    by_family, enabled_by_family = {}, {}
    for row in rows:
        if not isinstance(row, dict) or not row.get('id'):
            continue
        fam = row.get('family')
        by_family.setdefault(fam, set()).add(row['id'])
        if row.get('enabled', True):
            enabled_by_family.setdefault(fam, set()).add(row['id'])
    queryable = set(registry_expected_families(registry))
    reachable = set()
    for fam, ids in enabled_by_family.items():
        if fam not in queryable:
            continue
        if allowed and not (ids & allowed):
            continue
        reachable.add(fam)
    return {'reachable': reachable, 'sources_by_family': by_family,
            'enabled_by_family': enabled_by_family, 'queryable': queryable}


def _unreachable_reason(family, sources, registry, restricted, source_reachable,
                        pool_reachable):
    """The ONE controlling reason a required family is out of reach this run."""
    reach = _reachable_families(sources, registry)
    all_ids = reach['sources_by_family'].get(family) or set()
    enabled_ids = reach['enabled_by_family'].get(family) or set()
    allowed = set(sources or ())
    if family not in reach['queryable']:
        return {'controlling_reason': 'family_is_not_queryable_by_policy',
                'detail': 'The source registry classes this family as excluded '
                          'or not queryable, independently of this run.'}
    if not enabled_ids:
        return {'controlling_reason': 'every_source_in_this_family_is_disabled',
                'detail': f'{len(all_ids)} source(s) in the registry, none enabled.'}
    if restricted and not (enabled_ids & allowed):
        return {'controlling_reason': 'no_permitted_source_serves_this_family',
                'detail': (f'This run is restricted to {sorted(allowed)}. The '
                           f'family is served by {sorted(enabled_ids)}, none of '
                           f'which is permitted. Policy still requires it; this '
                           f'run has no route to it.')}
    if family not in pool_reachable:
        return {'controlling_reason': 'no_applicable_search_family_task',
                'detail': ('A permitted source serves this family, but no '
                           'applicable search family could build a query for it '
                           'from the current profile.')}
    return {'controlling_reason': 'reachable',
            'detail': 'This family is reachable this run.'}


def _settle_buckets(universe, in_scope, bucket_windows, queries, mode,
                    all_buckets=None, quotas=None, deadline_rows=None):
    """Which REQUIRED buckets this plan searches, and what it owes the rest.

    A bucket the plan does not fund is DEFERRED with its debt stated. It is never
    quietly absent, and it never advances: only a completed query advances a
    bucket, so a deferral costs nothing but time and that time is visible here.
    """
    funded = {q['coverage_bucket'] for q in queries
              if q['required_or_supplemental'] == 'required'}
    deferred = []
    for bucket in sorted(in_scope - funded):
        row = bucket_windows.get(bucket, {})
        deferred.append({
            'coverage_bucket': bucket,
            'inventory_family': universe[bucket]['inventory_family'],
            'search_family': universe[bucket]['search_family'],
            'term_cluster': universe[bucket]['term_cluster'],
            'tier': universe[bucket]['tier'],
            'tier_rationale': universe[bucket]['tier_rationale'],
            'target_revisit_hours': universe[bucket]['target_revisit_hours'],
            'deferral_reason': (
                f'The {mode} query budget did not reach this bucket on this run. It '
                f'is DEFERRED, not covered: its checkpoint does not advance, so the '
                f'next run that funds it searches the whole interval.'),
            'priority': 1 if row.get('capped') else 2,
            'elapsed_gap_hours': row.get('elapsed_gap_hours'),
            'last_successful_coverage': row.get('last_successful_coverage', ''),
            'capped': bool(row.get('capped')),
        })
    out_of_scope = sorted(set(universe) - set(in_scope))
    all_buckets = all_buckets or universe
    _by_tier = {}
    for tier in coverage_ledger.TIERS:
        _in_tier = {b for b, r in all_buckets.items() if r['tier'] == tier}
        _hit = {q['coverage_bucket'] for q in queries} & _in_tier
        _by_tier[tier] = {
            'total': len(_in_tier),
            'owes_interval': coverage_ledger.owes_interval(tier),
            'target_revisit_hours': coverage_ledger.target_revisit_hours(tier),
            'searched_this_run': len(_hit),
            'deferred_this_run': len(_in_tier & set(in_scope) - _hit),
        }
    _mandatory = {b for b, r in all_buckets.items() if r['owes_interval']}
    # Query SLOTS and unique BUCKETS are different counts and were reported as
    # if they were one: eight rolling slots against five rolling buckets read as
    # a discrepancy when it was two facts wearing one label. A slot is a query
    # this run may spend; a bucket is an obligation it discharged.
    _slots = {}
    for tier in coverage_ledger.TIERS:
        _tier_queries = [q for q in queries if q['coverage_tier'] == tier]
        _target = int((quotas or {}).get(tier, 0) or 0)
        _used = len(_tier_queries)
        _slots[tier] = {
            'query_slots_used': _used,
            'unique_buckets_covered': len({q['coverage_bucket'] for q in _tier_queries}),
            'slots_satisfying_an_already_covered_bucket': (
                _used - len({q['coverage_bucket'] for q in _tier_queries})),
            # NOT a floor. Phase 4F called this `quota_floor` and reported it as 8
            # beside 0 slots used, which is not what the word floor means. It is a
            # normal target that more urgent work may borrow, so it is named for
            # what it is and the difference is always explained.
            'normal_target_slots': _target,
            'slots_borrowed_by_more_urgent_work': max(0, _target - _used),
            'slots_unused': max(0, _target - _used) if _used < _target else 0,
            'difference_reason': (
                '' if _used >= _target else
                'more urgent mandatory work took these slots: a target is '
                'borrowable, and a deadline outranks it'),
        }
    _accounting = {
        'total_queries': len(queries),
        'by_tier': _slots,
        'reconciles': sum(v['query_slots_used'] for v in _slots.values()) == len(queries),
        'note': (
            'normal_target_slots is a borrowable TARGET, never an unconditional '
            'floor: urgent mandatory work takes it and the difference is stated. '
            'query_slots_used counts QUERIES; unique_buckets_covered counts the '
            'obligations they discharged. They differ when two slots land in one '
            'bucket through controlled subsumption, and reporting either number '
            'as the other would misstate both coverage and cost.'),
    }
    return {
        'schema_version': 3,
        'slot_accounting': _accounting,
        'tiers': _by_tier,
        'mandatory_total': len(_mandatory),
        'mandatory_funded': len(funded & _mandatory),
        'mandatory_deferred': sorted(_mandatory & set(in_scope) - funded),
        'tier_of': {q['coverage_bucket']: q['coverage_tier'] for q in queries},
        'deadlines': {b: r for b, r in sorted((deadline_rows or {}).items())
                      if r['urgency'] != 'comfortable'},
        'at_risk_total': len([r for r in (deadline_rows or {}).values()
                              if r['urgency'] in ('breached', 'at_risk')]),
        'at_risk_funded': len([b for b, r in (deadline_rows or {}).items()
                               if r['urgency'] in ('breached', 'at_risk')
                               and b in funded]),
        'at_risk_unfunded': sorted(b for b, r in (deadline_rows or {}).items()
                                   if r['urgency'] in ('breached', 'at_risk')
                                   and b not in funded and b in in_scope),
        'bucket_key': coverage_ledger.coverage_policy()['bucket_key'],
        'required_bucket_total': len(universe),
        'required_in_scope': len(in_scope),
        'required_funded': sorted(funded),
        'required_funded_count': len(funded),
        'required_deferred': deferred,
        'required_deferred_count': len(deferred),
        'out_of_scope_this_run': out_of_scope,
        'capped_buckets': sorted(b for b in funded
                                 if (bucket_windows.get(b) or {}).get('capped')),
        'effective_windows': {b: bucket_windows[b] for b in sorted(funded)
                              if b in bucket_windows},
        'supplemental_tasks': sum(1 for q in queries
                                  if q['required_or_supplemental'] == 'supplemental'),
        'note': (
            'A coverage bucket is inventory family, search family and term cluster '
            'together. Sharing a website is not evidence that one query searched '
            'another\'s interval. Coverage here is the planned SEARCH INTERVAL '
            'that was queried, never a guarantee of complete external retrieval.'),
    }


def _settle_coverage(coverage, queries, family_windows, mode, registry):
    """Reclassify any family the budget could not fund from PLANNED to DEFERRED.

    A plan that lists a family it never funds is worse than a plan that admits it
    could not reach it: the listing looks like coverage, so nothing downstream
    knows to catch up, and the family's clock is never advanced by a query that
    never ran. Deferral is the honest form of the same fact, and it carries what
    a later run needs in order to act on it.
    """
    funded = {q['source_family'] for q in queries}
    planned = list(coverage['planned_families'])
    unfunded = sorted(set(planned) - funded)
    deferred = list(coverage.get('omitted_families') or [])
    for family in unfunded:
        row = next((r for b, r in family_windows.items()
                    if b.split('::')[0] == family), {})
        deferred.append({
            'family': family,
            'monitoring_class': 'deferred_by_budget',
            'reason': (f'The {mode} query budget could not fund a query for '
                       f'{family}. It is DEFERRED, not covered, and its coverage '
                       f'checkpoint is not advanced.'),
            'priority': 1 if row.get('capped') else 2,
            'coverage_debt_hours': row.get('elapsed_gap_hours'),
            'last_successful_coverage': row.get('last_successful_coverage', ''),
            'due_in_rolling_cycle': True,
            'runs_until_due': 0,
            'next_opportunity': 'the next run of any mode that funds this family',
        })
    settled = sorted(funded & set(planned))
    return {
        **coverage,
        'planned_families': settled,
        'planned_family_count': len(settled),
        'families_funded': sorted(funded),
        # Kept as a field, and always empty by construction: a reader should be
        # able to check the invariant rather than trust that it holds.
        'families_planned_but_unfunded': [],
        'deferred_families': deferred,
        'omitted_families': deferred,
        'complete': settled == sorted(coverage['expected_families']),
        'due_rotating_funded': not (set(coverage.get('rotating_due_now') or []) - funded),
        # The widest window any bucket on this family needs, so a family-level
        # reader sees the real lookback rather than the narrowest one.
        'effective_windows': {f: {
            'effective_window': max(
                (r['effective_window'] for b, r in family_windows.items()
                 if b.split('::')[0] == f),
                key=lambda w: {'24h': 1, '7d': 2, '14d': 3}.get(w, 0), default='24h'),
        } for f in sorted(funded)},
        'capped_families': sorted({b.split('::')[0] for b, r in family_windows.items()
                                   if r.get('capped') and b.split('::')[0] in funded}),
    }


# --------------------------------------------------------------------------
# Saturation
# --------------------------------------------------------------------------

def _completed(record):
    return str(record.get('outcome', '')).strip().lower() in COMPLETE_OUTCOMES


def _failed(record):
    return str(record.get('outcome', '')).strip().lower() in FAILED_OUTCOMES


def family_progress(family_id, records, mode='deep', strategy=None):
    """Should this search family get another query? Deterministic, evidence backed.

    `records` are the query outcomes recorded for this family so far, oldest first,
    each carrying at least `outcome` and `new_canonical_candidates`.
    """
    strategy = strategy or load_strategy()
    policy = strategy.get('saturation_policy', {})
    min_queries = int(policy.get('min_queries_before_saturation', 2))
    streak_needed = int(policy.get('zero_yield_streak_to_saturate', 2))
    budget = family_query_budget(family_id, mode, strategy)

    rows = [r for r in records if r.get('search_family') == family_id] or list(records)
    attempted = len(rows)
    completed = [r for r in rows if _completed(r)]
    failed = [r for r in rows if _failed(r)]
    new_total = sum(max(0, int(r.get('new_canonical_candidates', 0) or 0)) for r in completed)

    # Distinct completed queries only. Re-running the same dedup key proves nothing
    # about the family, so it can neither extend nor break a zero-yield streak.
    distinct, seen = [], set()
    for row in completed:
        key = row.get('dedup_key') or row.get('query_id') or json.dumps(row, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(row)

    streak = 0
    for row in reversed(distinct):
        if int(row.get('new_canonical_candidates', 0) or 0) > 0:
            break
        streak += 1

    if attempted >= budget:
        state, reason = 'BUDGET_EXHAUSTED', (
            f'This family has spent its {budget}-query budget for {mode} mode.')
    elif failed:
        # Lost coverage is never saturation. A family whose source broke has not
        # been searched, so it must stay visibly unfinished.
        state, reason = 'GAP_REMAINS', (
            f'{len(failed)} query/queries lost coverage to a failed source '
            f'({", ".join(sorted({str(r.get("outcome")) for r in failed}))}), which is '
            'missing coverage rather than zero yield.')
    elif len(distinct) >= min_queries and streak >= streak_needed:
        state, reason = 'SATURATED', (
            f'{streak} consecutive distinct completed queries produced no new canonical '
            f'candidates after the {min_queries}-query minimum coverage.')
    else:
        needed = max(0, min_queries - len(distinct))
        state, reason = 'CONTINUE', (
            f'{needed} more distinct queries needed for minimum coverage.' if needed else
            f'Zero-yield streak is {streak} of the {streak_needed} needed to saturate.')

    return {
        'search_family': family_id,
        'state': state,
        'reason': reason,
        'queries_attempted': attempted,
        'queries_completed': len(completed),
        'queries_distinct': len(distinct),
        'queries_failed': len(failed),
        'new_canonical_candidates': new_total,
        'zero_yield_streak': streak,
        'query_budget': budget,
        'budget_remaining': max(0, budget - attempted),
        'min_queries_before_saturation': min_queries,
        'zero_yield_streak_to_saturate': streak_needed,
        'productive': new_total > 0,
    }


def run_progress(records, mode='deep', strategy=None, family_ids=()):
    """Per-family stopping decisions plus the run-level budget picture."""
    strategy = strategy or load_strategy()
    limits = mode_budget(mode, strategy)
    seen_families = list(family_ids) or sorted({r.get('search_family') for r in records
                                                if r.get('search_family')})
    per_family = {fid: family_progress(fid, records, mode, strategy) for fid in seen_families}
    attempted = len(records)
    global_budget = int(limits['global_query_budget'])
    continuing = [fid for fid, row in per_family.items() if row['state'] == 'CONTINUE']
    gaps = [fid for fid, row in per_family.items() if row['state'] == 'GAP_REMAINS']

    if attempted >= global_budget:
        state = 'BUDGET_EXHAUSTED'
    elif gaps:
        state = 'GAP_REMAINS'
    elif continuing:
        state = 'CONTINUE'
    elif per_family:
        state = 'SATURATED'
    else:
        state = 'CONTINUE'

    return {
        'mode': mode,
        'state': state,
        'queries_attempted': attempted,
        'global_query_budget': global_budget,
        'global_budget_remaining': max(0, global_budget - attempted),
        'families': per_family,
        'families_continuing': sorted(continuing),
        'families_saturated': sorted(fid for fid, r in per_family.items() if r['state'] == 'SATURATED'),
        'families_with_gaps': sorted(gaps),
        'families_budget_exhausted': sorted(fid for fid, r in per_family.items()
                                            if r['state'] == 'BUDGET_EXHAUSTED'),
        'families_productive': sorted(fid for fid, r in per_family.items() if r['productive']),
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def read_json_input(args):
    if getattr(args, 'file', ''):
        path = Path(args.file)
        if not path.exists():
            raise plan_error(f'Input file not found: {path}')
        raw = path.read_text(encoding='utf-8')
    else:
        raw = sys.stdin.read()
    raw = raw.lstrip('﻿')
    if not raw.strip():
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise plan_error('Malformed JSON input.',
                         f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None


def _profile_for(args):
    if getattr(args, 'search_profile', ''):
        path = Path(args.search_profile)
        if not path.exists():
            raise plan_error(f'Search profile not found: {path}')
        return json.loads(path.read_text(encoding='utf-8'))
    return load_search_profile(getattr(args, 'profile', '') or None)


def _rotation_index_for(args):
    """The cycle index, derived from run history unless explicitly supplied.

    Derivation is the default because a stored cursor can drift out of step with
    the history it claims to summarise. The flag exists for fixtures and for
    reproducing a past plan.
    """
    if getattr(args, 'rotation_index', None) is not None:
        return max(0, int(args.rotation_index))
    try:
        from search_rotation import successful_run_count
        from search_window import _history
        records, summaries = _history()
        return cycle_index(successful_run_count(records, summaries))
    except Exception:  # noqa: BLE001 - no readable history is index zero, not a crash
        return 0


def cmd_plan(args):
    plan = build_plan(
        _profile_for(args), mode=args.mode, window=args.window,
        family_ids=[f.strip() for f in args.families.split(',') if f.strip()],
        sources=[s.strip() for s in args.sources.split(',') if s.strip()],
        rotation_index=_rotation_index_for(args),
        rotation_override=args.rotation_override)
    if not args.verbose:
        plan.pop('deduplicated', None)
        plan.pop('deferred', None)
    print(json.dumps(plan, indent=2, ensure_ascii=False))


def cmd_progress(args):
    records = read_json_input(args)
    if not isinstance(records, list):
        raise plan_error('progress expects a JSON array of query outcome records.')
    print(json.dumps(run_progress(records, mode=args.mode), indent=2, ensure_ascii=False))


def cmd_family(args):
    records = read_json_input(args)
    if not isinstance(records, list):
        raise plan_error('family expects a JSON array of query outcome records.')
    print(json.dumps(family_progress(args.family_id, records, mode=args.mode),
                     indent=2, ensure_ascii=False))


def cmd_dedup_key(args):
    key = dedup_key(args.query, args.source_id, args.mode)
    print(json.dumps({'query': args.query, 'source_id': args.source_id, 'mode': args.mode,
                      'dedup_key': key, 'query_terms': sorted(significant_terms(args.query))},
                     ensure_ascii=False))


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
    p = argparse.ArgumentParser(description='Bounded query planning and stopping rules')
    sub = p.add_subparsers(dest='cmd', required=True)

    pl = sub.add_parser('plan', help='Generate a bounded, deduplicated query plan.')
    pl.add_argument('--mode', default='deep', choices=MODES)
    pl.add_argument('--window', default='24h', choices=WINDOWS)
    pl.add_argument('--rotation-index', dest='rotation_index', type=int, default=None,
                    help='Cycle index. Default: derived from run history.')
    pl.add_argument('--rotation-override', dest='rotation_override', default='',
                    help='Name the focused mode that overrides normal rotation.')
    pl.add_argument('--families', default='', help='Comma separated search families.')
    pl.add_argument('--sources', default='', help='Comma separated source ids.')
    pl.add_argument('--profile', default='', help='Alternative candidate profile path.')
    pl.add_argument('--search-profile', dest='search_profile', default='',
                    help='Pre-built compact search profile JSON.')
    pl.add_argument('--verbose', action='store_true', help='Include deduplicated/deferred queries.')
    pl.set_defaults(func=cmd_plan)

    pr = sub.add_parser('progress', help='Stopping decision for a whole run.')
    pr.add_argument('--file', default='')
    pr.add_argument('--mode', default='deep', choices=MODES)
    pr.set_defaults(func=cmd_progress)

    fa = sub.add_parser('family', help='Stopping decision for one search family.')
    fa.add_argument('family_id')
    fa.add_argument('--file', default='')
    fa.add_argument('--mode', default='deep', choices=MODES)
    fa.set_defaults(func=cmd_family)

    dk = sub.add_parser('dedup-key', help='Show the dedup identity of one query.')
    dk.add_argument('--query', required=True)
    dk.add_argument('--source-id', dest='source_id', required=True)
    dk.add_argument('--mode', default='deep', choices=MODES)
    dk.set_defaults(func=cmd_dedup_key)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

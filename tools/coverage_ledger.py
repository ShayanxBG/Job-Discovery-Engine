#!/usr/bin/env python3
"""Per-BUCKET coverage checkpoints, and the effective window each bucket needs.

WHAT PHASE 4C GOT WRONG. It used the inventory family as the coverage unit and
justified it like this: a board holds one inventory, so a second title against
the same board cannot cover an interval the first did not. The premise is true
and the conclusion does not follow. A board holds one inventory; its RESULTS are
filtered by query text. Searching `Integration Developer` on LinkedIn returns
adverts matching that phrase, and proves nothing whatever about whether the same
interval was searched for `Python Django`, or `Graduate Software Engineer`, or
`visa sponsorship Python`. The trace was plain: on one run LinkedIn's declared
anchor was `Integration Developer`, and `Python PostgreSQL` on LinkedIn was
filed as supplemental recall. Nothing had searched the capability intent that
day, and the ledger said LinkedIn was covered.

Calling a different query intent "supplemental" because it shares a website is
how the gap got hidden. Sharing an inventory is not evidence of subsumption.

THE UNIT INSTEAD. A coverage bucket is

    {inventory_family}::{search_family}::{term_cluster}

Three things, because all three change what comes back. The family decides which
inventory is searched at all. The search family is the INTENT: title, capability,
early career, sponsorship. And within an intent, terms that are not substitutes
find different adverts, so they are different buckets.

CLUSTERING, SO THIS STAYS AFFORDABLE. Not every wording variation earns a bucket.
Board search is conjunctive over significant tokens, so the results of `Python
Backend Developer` are a SUBSET of the results of `Python Developer`: running the
shorter query searches the longer one's interval too. Terms whose token sets nest
that way share a cluster, and the SHORTEST token set is its anchor. The
relationship is computed from the token sets themselves, never from a hand
written table that could drift, and it is confined to one search family, where
terms differ by a token rather than by intent.

The assumption underneath it is stated rather than buried: a broad query subsumes
a narrow one only if its result list was not truncated before reaching the narrow
matches. Boards do cap result lists. That is why the rule never crosses a search
family, and why a query that reports truncation leaves its cluster uncovered.

WHAT ADVANCES A CHECKPOINT. One thing: a query task that COMPLETED against its
declared interval, inside a successful completed production run. Not a source
outcome, because a board answering one query says nothing about another. Not a
planned task, because planning is not searching. Not a failed query, even when a
sibling query on the same board succeeded. Not a deferred or unfunded bucket,
because the plan naming a bucket it never ran must never be what advances it.

WHAT THIS DOES NOT CLAIM. Coverage here is the planned SEARCH INTERVAL that was
actually queried. It is not a guarantee that the external site returned every
vacancy it held for that interval, and no mechanism in this workspace could
honestly promise that.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 2

TASK_ROLES = ('required_coverage', 'supplemental_recall')

# Query outcomes that prove the declared interval was actually searched. A
# truncated or partial result list did not reach the end of its window, so it
# cannot claim the window.
COVERING_OUTCOMES = ('ok', 'empty')


def ledger_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def coverage_policy(strategy=None):
    from search_strategy import load_strategy
    strategy = strategy or load_strategy()
    policy = strategy.get('coverage_policy')
    if not isinstance(policy, dict):
        raise ledger_error(
            'config/search_strategy.json has no coverage_policy block.',
            'What a coverage bucket is, is configuration.')
    return policy


def required_search_families(strategy=None):
    return tuple(coverage_policy(strategy).get('required_search_families', ()) or ())


def _tokens(text, strategy=None):
    from search_plan import significant_terms
    return frozenset(significant_terms(text, strategy))


def cluster_terms(terms, strategy=None):
    """Group terms whose token sets nest, and name each group by its anchor.

    Returns {term: (cluster_id, anchor_term, subsumed_by_or_empty)}. The anchor is
    the term with the smallest token set in the group: it is the broad query whose
    results contain the others'.
    """
    rows = [(t, _tokens(t, strategy)) for t in dict.fromkeys(terms or ())]
    rows = [(t, s) for t, s in rows if s]
    assigned, out = {}, {}
    # Broadest first, so an anchor is chosen before the terms it subsumes.
    for term, toks in sorted(rows, key=lambda r: (len(r[1]), r[0])):
        anchor = next((a for a, atoks in assigned.items() if atoks < toks), None)
        if anchor is None:
            assigned[term] = toks
            out[term] = (_slug(term), term, '')
        else:
            out[term] = (out[anchor][0], anchor, anchor)
    return out


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', str(text or '').lower()).strip('-') or 'term'


def bucket_key(inventory_family, search_family, term_cluster):
    return f'{inventory_family}::{search_family}::{term_cluster}'


def is_required(search_family, strategy=None):
    return search_family in set(required_search_families(strategy))


def subsumes(anchor_text, child_text, anchor_family, child_family,
             anchor_search_family, child_search_family, strategy=None):
    """Whether running `anchor_text` also searched `child_text`'s interval.

    Every condition must hold, and the third is the one Phase 4C skipped: two
    queries sharing a website is not a reason to believe either covered the
    other.
    """
    policy = coverage_policy(strategy).get('subsumption', {})
    if policy.get('requires_same_inventory_family', True) and anchor_family != child_family:
        return False
    if policy.get('requires_same_search_family', True) and anchor_search_family != child_search_family:
        return False
    a, c = _tokens(anchor_text, strategy), _tokens(child_text, strategy)
    return bool(a) and bool(c) and a < c


# --------------------------------------------------------------------------
# Checkpoints, derived from recorded QUERY outcomes
# --------------------------------------------------------------------------

def run_is_creditable(record, summary=None):
    """May this run's own QUERY rows advance a bucket checkpoint?

    Finished, a production mode, and not declared partial by its operator. All
    three, or nothing in it counts.

    Deliberately NOT `run_is_successful`, which additionally refuses a run whose
    run-level coverage_status is PARTIAL. That is the right test for "is this the
    last successful run" in window selection, and the wrong one here: coverage is
    keyed per BUCKET precisely because a board holds one inventory and filters it
    by query text. A broken LinkedIn cannot un-search the ten Indeed queries that
    returned `ok` beside it, and gating per-bucket evidence on a whole-run verdict
    made one failing family erase every other family's checkpoints, which is how
    production run scrape-20260831T083115570281 left 30 genuinely searched buckets
    uncredited and the workspace permanently stuck in INITIAL_CATCHUP.

    Nothing here relaxes what counts as covered: the per-query outcome still has
    to be in COVERING_OUTCOMES, so a failed, partial or changed_layout query
    credits exactly nothing.
    """
    from search_window import _parse, is_production_mode
    if not isinstance(record, dict):
        return False
    if not _parse(record.get('finished_at')):
        return False
    if not is_production_mode(record.get('mode')):
        return False
    if record.get('forced_partial'):
        return False
    if summary is not None and not summary.get('finished', True):
        return False
    return True


def checkpoints(records=None, summaries=None):
    """Last successful coverage per bucket: {bucket: {run_id, finished_at}}.

    Read from the recorded QUERY tasks, not the source outcomes. A source outcome
    says a board answered; it does not say which query text was asked, and a run
    that asked one question must not be credited with having asked the others.
    """
    from search_window import _parse
    summaries = summaries or {}
    best = {}
    for record in records or []:
        if not run_is_creditable(record, summaries.get(record.get('run_id'))):
            continue
        finished = _parse(record.get('finished_at'))
        if not finished:
            continue
        for task in record.get('queries') or []:
            if not isinstance(task, dict):
                continue
            bucket = str(task.get('coverage_bucket') or '')
            outcome = str(task.get('outcome', '')).strip().lower()
            if not bucket or outcome not in COVERING_OUTCOMES:
                continue
            if bucket not in best or finished > best[bucket][1]:
                best[bucket] = (record.get('run_id', ''), finished)
            # A completed broad query also searched the interval of every bucket
            # it declares itself the anchor of. Declared IN THE TASK, so the claim
            # travels with the evidence rather than being re-derived later from a
            # rule that may have changed.
            for child in task.get('subsumes') or []:
                child = str(child)
                if child and (child not in best or finished > best[child][1]):
                    best[child] = (record.get('run_id', ''), finished)
    return {bucket: {'run_id': run_id, 'finished_at': stamp.isoformat()}
            for bucket, (run_id, stamp) in best.items()}


def bucket_window(bucket, checkpoint=None, now=None, strategy=None,
                  global_window='24h'):
    """The window this bucket must search, from ITS OWN last coverage."""
    from datetime import datetime
    from search_window import (_coverage, _ladder_window, _parse, window_hours,
                               window_policy)
    policy = window_policy(strategy)
    now = _parse(now) or datetime.now().astimezone()
    stamp = _parse((checkpoint or {}).get('finished_at'))

    if stamp is None:
        window = str(policy.get('initial_catchup_window', '14d'))
        return {
            'coverage_bucket': bucket, 'effective_window': window,
            'basis': 'first_coverage', 'last_successful_coverage': '',
            'elapsed_gap_hours': None, 'covers_gap': None, 'uncovered_hours': None,
            'capped': False,
            'reason': (f'{bucket} has never been successfully searched, so it uses '
                       f'the initial catch-up window {window}.'),
        }

    elapsed = round((now - stamp).total_seconds() / 3600.0, 2)
    window, capped = _ladder_window(elapsed, strategy)
    if window_hours(window, strategy) < window_hours(global_window, strategy):
        window, capped = global_window, False
    cov = _coverage(window, elapsed, strategy)
    reason = (f'{bucket} was last successfully searched {elapsed} hours ago, so its '
              f'effective window is {window} rather than the global {global_window}.')
    if capped:
        reason += (f' The gap EXCEEDS the {window} cap by {cov["uncovered_hours"]} '
                   f'hours ({cov["uncovered_days"]} days), which this run does not '
                   f'recover.')
    return {
        'coverage_bucket': bucket, 'effective_window': window,
        'basis': 'bucket_checkpoint',
        'last_successful_coverage': stamp.isoformat(), 'elapsed_gap_hours': elapsed,
        'covers_gap': cov['covers_gap'], 'uncovered_hours': cov['uncovered_hours'],
        'capped': capped, 'reason': reason,
    }


def bucket_windows(buckets, records=None, summaries=None, now=None, strategy=None,
                   global_window='24h'):
    marks = checkpoints(records, summaries)
    return {b: bucket_window(b, marks.get(b), now, strategy, global_window)
            for b in sorted(set(buckets or ()))}


def overdue_buckets(buckets, records=None, summaries=None, now=None, strategy=None):
    rows = bucket_windows(buckets, records, summaries, now, strategy)
    return sorted((r for r in rows.values() if r['capped']),
                  key=lambda r: (-(r['uncovered_hours'] or 0), r['coverage_bucket']))


# --------------------------------------------------------------------------
# Service tiers. A bucket EXISTING is not a promise to search it daily.
#
# Phase 4D produced 173 buckets and called every one required, so a daily run
# funded 24 and deferred 81, and `exhaustive` funded 33 while deferring 140 it
# had labelled mandatory. That is a Cartesian product wearing a strategy's
# clothes. The identity was right; the obligation was not.
#
# Each bucket now carries exactly one tier and the reason it has it, derived
# from structure the workspace already declares: whether the source says this
# intent is productive on it, which query template the term came from, and
# whether the inventory family is primary or secondary. Nothing here invents a
# candidate preference, and nothing is deleted: an exploratory bucket keeps its
# identity, its checkpoint and its window. What changes is what the schedule
# owes it.
# --------------------------------------------------------------------------

TIERS = ('critical_fresh', 'rolling_recall', 'exploratory',
         'watchlist_or_event_driven')


def tier_policy(strategy=None):
    policy = coverage_policy(strategy).get('tiers')
    if not isinstance(policy, dict):
        raise ledger_error('config/search_strategy.json has no coverage_policy.tiers.')
    return policy


def tier_definition(tier, strategy=None):
    return (tier_policy(strategy).get('definitions') or {}).get(tier, {})


def target_revisit_hours(tier, strategy=None):
    return tier_definition(tier, strategy).get('target_revisit_hours')


def owes_interval(tier, strategy=None):
    return bool(tier_definition(tier, strategy).get('owes_interval'))


def _productive(inventory_family, search_family, registry):
    for source in registry.get('sources') or []:
        if (isinstance(source, dict) and source.get('family') == inventory_family
                and search_family in (source.get('productive_families') or [])):
            return True
    return False


def family_capability(inventory_family, search_family, registry, rules):
    """Can ANY source in this family actually discharge a query-specific obligation?

    A bucket names a QUERY on an INVENTORY over a WINDOW, so owing one requires two
    capabilities that are properties of the SOURCE, never of the vacancies:

      - the requested query is reproducibly executed and displayed (`query_execution`)
      - the window can be proven from the item itself (`freshness_support`)

    Production run scrape-20260831T102144228455 is why this is enforced rather than
    assumed. The sponsor-board family held three critical buckets through a
    `critical_inventory_overrides` entry, while every source in it declared
    freshness `unknown`, and gradsponsor and findsponsorjobs were then observed
    returning their whole unfiltered inventory whatever was typed. Two of those
    three critical buckets could never be covered by anything. An obligation that
    no source can discharge is not a standard; it is a guaranteed permanent
    failure that makes catch-up unfinishable.

    The two failures are NOT the same failure, so they do not carry the same
    penalty:

      - CANNOT EXECUTE THE QUERY -> `exploratory`. It cannot search what was
        asked, so it can discharge nothing. Still planned, still searched, still
        recorded; owes no interval.
      - CANNOT VERIFY FRESHNESS -> capped at `rolling_recall`. It genuinely
        searches its inventory, so deleting the obligation would lose real
        coverage; it simply cannot be held to a 72-hour promise it can never
        evidence. A seven-day recall target it can keep is honest, where a
        72-hour one it cannot check is not.
      - CANNOT PAGE ITS OWN RESULTS -> `exploratory`. Added 2026-09-04 for
        indeed, which executes the query and dates every card yet serves only
        &start=0 to an unauthenticated client: the first &start=10 drew an
        immediate reCAPTCHA across repeated measurements. A bucket asserts that
        an INTERVAL WAS SEARCHED, and one relevance-ordered page of 16 results
        cannot assert that however fresh and faithful those 16 are. This is a
        third, genuinely distinct failure: the query runs, the window is
        provable, and the inventory still cannot be reached to its end. Treating
        it as capable put 12 unreachable buckets into the mandatory denominator
        and the cadence simulation stopped covering every mandatory bucket.

    Returns (ceiling, reason): the STRONGEST tier this family may hold, one of
    'critical_fresh' (no restriction), 'rolling_recall', or 'exploratory'.
    """
    bad_fresh = set(rules.get('unverifiable_freshness_support') or ())
    good_query = set(rules.get('verified_query_execution_values') or ('verified',))
    need_fresh = bool(rules.get('critical_requires_verifiable_freshness', True))
    need_query = bool(rules.get('required_requires_verified_query_execution', True))

    members = [s for s in (registry.get('sources') or [])
               if isinstance(s, dict) and s.get('family') == inventory_family
               and s.get('enabled', True)
               and search_family in (s.get('productive_families') or [])]
    if not members:
        return 'critical_fresh', ''

    if need_query and not any(str(s.get('query_execution', 'verified')) in good_query
                              for s in members):
        modes = sorted({str(s.get('query_execution', 'verified')) for s in members})
        return 'exploratory', (
            f'no enabled {inventory_family} source can be shown to execute a '
            f'{search_family} query (query_execution: {", ".join(modes)}), so it '
            f'cannot discharge a query-specific obligation')

    # A source that cannot paginate cannot reach the end of its own interval, so
    # the family owes no interval at all. Checked BEFORE freshness, because a
    # family that cannot be exhausted is not rescued by dating what it does show.
    if not any(bool(s.get('paginate', True)) for s in members):
        return 'exploratory', (
            f'no enabled {inventory_family} source can paginate its own results '
            f'(paginate: false), so a query reaches only the first page and cannot '
            f'assert that the interval was searched; it is planned and recorded '
            f'but owes no interval')

    if need_fresh and all(str(s.get('freshness_support', '')) in bad_fresh
                          for s in members):
        modes = sorted({str(s.get('freshness_support', '')) for s in members})
        return 'rolling_recall', (
            f'no enabled {inventory_family} source can verify per-item freshness '
            f'(freshness_support: {", ".join(modes)}), so it may not owe a '
            f'72-hour critical interval it could never evidence; it keeps a '
            f'seven-day recall obligation it can actually keep')
    return 'critical_fresh', ''


def assign_tier(inventory_family, search_family, template_id, cluster_rank,
                strategy=None, registry=None):
    """The tier for one bucket, and the controlled reason it got it.

    Returns (tier, rationale). Every branch names the declared fact it rests on,
    because a reclassification with no stated reason is indistinguishable from a
    number somebody wanted to be smaller.
    """
    from sources import load_registry
    registry = registry or load_registry()
    rules = tier_policy(strategy).get('assignment') or {}
    primary = set(rules.get('primary_inventory_families') or ())
    crit_templates = rules.get('critical_query_templates') or {}
    max_terms = int(rules.get('critical_capability_terms_max', 4))

    if search_family == 'employer-ats':
        return ('watchlist_or_event_driven',
                'Employer ATS work answers to the bounded watchlist and its own '
                'ceiling, not to a clock.')

    # Capability CEILING before any tier that owes an interval. A family that
    # cannot run the query may still be searched but is owed nothing; a family
    # that runs the query but cannot evidence per-item dates keeps a seven-day
    # recall obligation instead of a 72-hour one it could never check.
    ceiling, why = family_capability(inventory_family, search_family, registry, rules)
    if ceiling == 'exploratory':
        return ('exploratory', why)
    _capability_ceiling, _capability_why = ceiling, why

    if search_family in set(rules.get('always_exploratory_search_families') or ()):
        return ('exploratory',
                f'{search_family} is a recall widener over roles the direct-title '
                f'and capability intents already reach by other wording. It must '
                f'never displace a stronger route by owing the same interval.')

    # Some intents live only on families that are not general primary boards.
    # Naming those families keeps the intent critical without pretending they
    # are something they are not.
    overrides = set((rules.get('critical_inventory_overrides') or {}).get(
        search_family) or ())

    if rules.get('requires_declared_productive_family', True) and not _productive(
            inventory_family, search_family, registry):
        return ('exploratory',
                f'{inventory_family} does not declare {search_family} productive in '
                f'the source registry, so requiring it there would be an obligation '
                f'nobody expects to pay off.')

    allowed = crit_templates.get(search_family) or []
    template_is_critical = '*' in allowed or template_id in allowed

    if (inventory_family in (primary | overrides) and template_is_critical
            and cluster_rank < max_terms):
        if _capability_ceiling == 'rolling_recall':
            return ('rolling_recall', _capability_why)
        return ('critical_fresh',
                f'{search_family} on the primary inventory family '
                f'{inventory_family}, from the {template_id} template: a '
                f'highest-value route for this profile.')

    if inventory_family in (primary | overrides):
        if cluster_rank < max_terms + int(
                rules.get('rolling_secondary_capability_clusters_max', 2)):
            return ('rolling_recall',
                    f'{search_family} on a primary family, from the {template_id} '
                    f'template beyond the core clusters: a refinement of an intent '
                    f'already covered critically, so it is recall rather than '
                    f'obligation.')
        return ('exploratory',
                f'A {template_id} phrasing ranked {cluster_rank} within '
                f'{search_family}: by this point the wording restates an intent '
                f'already guaranteed rather than adding one.')

    if template_is_critical and cluster_rank < int(
            rules.get('rolling_representatives_per_secondary_family', 3)):
        return ('rolling_recall',
                f'A representative {search_family} route into the independent '
                f'{inventory_family} inventory. One representative per applicable '
                f'intent reaches the inventory; the rest of its matrix does not.')

    return ('exploratory',
            f'A secondary phrasing of {search_family} on the secondary family '
            f'{inventory_family}. Independent enough to keep auditable, not '
            f'independent enough to owe an interval.')


def bucket_universe(strategy=None, registry=None, profile=None, per_slot=4,
                    include_supplemental=True):
    """Every coverage bucket, with its tier and the reason it has it.

    Includes the supplemental search families when asked, because an exploratory
    route still needs a stable identity: it has to be recordable, and it has to
    be impossible for it to advance a checkpoint it does not own.
    """
    from search_plan import _family_terms
    from search_profile import load_search_profile
    from search_rotation import expected_families
    from search_strategy import families, load_strategy
    from sources import load_registry, source_family
    strategy = strategy or load_strategy()
    registry = registry or load_registry()
    profile = profile or load_search_profile()
    ordinary = set(expected_families(registry))

    out = {}
    for family in families(strategy):
        fid = family['id']
        if not is_required(fid, strategy) and not include_supplemental:
            continue
        # `gapfill` exists to repair a recorded gap and is never planned
        # unprompted, so it owes no interval and is not part of any universe.
        if not family.get('plan_by_default', True):
            continue
        pairs = _family_terms(family, profile, per_slot)
        terms = [t for t, _tpl in pairs]
        template_of = {t: tpl for t, tpl in pairs}
        clusters = cluster_terms(terms, strategy)
        # Rank within the search family, so "the first four capability clusters"
        # is a deterministic statement rather than whichever order a dict gave.
        rank_of, seen_cluster = {}, []
        for term in terms:
            cid = clusters[term][0]
            if cid not in seen_cluster:
                seen_cluster.append(cid)
            rank_of[cid] = seen_cluster.index(cid)
        # Only inventory families that hold their OWN inventory owe an interval.
        # A search engine indexes other people's boards, so giving it a bucket
        # would count the same vacancies twice as coverage; employer and ATS
        # sources answer to the watchlist ceiling rather than to a clock.
        kinds = set(coverage_policy(strategy).get('required_inventory_kinds') or ())
        owning = {s.get('family') for s in (registry.get('sources') or [])
                  if isinstance(s, dict) and s.get('kind') in kinds}
        reachable = {source_family(s, registry)
                     for s in family.get('eligible_sources') or []} & ordinary
        # Only an inventory-owning family can OWE an interval; a search engine
        # indexes other people's boards. It can still hold exploratory buckets,
        # which is what exploratory is for.
        inv = sorted(reachable if not is_required(fid, strategy)
                     else reachable & owning)
        for inventory in inv:
            for term, (cluster, anchor, parent) in clusters.items():
                key = bucket_key(inventory, fid, cluster)
                if key not in out:
                    tier, why = assign_tier(inventory, fid, template_of.get(term, ''),
                                            rank_of.get(cluster, 99), strategy, registry)
                    out[key] = {
                        'coverage_bucket': key, 'inventory_family': inventory,
                        'search_family': fid, 'term_cluster': cluster,
                        'anchor_term': anchor, 'terms': [],
                        'tier': tier, 'tier_rationale': why,
                        'owes_interval': owes_interval(tier, strategy),
                        'target_revisit_hours': target_revisit_hours(tier, strategy),
                        'template_id': template_of.get(term, ''),
                        'cluster_rank': rank_of.get(cluster, 99),
                    }
                out[key]['terms'].append(term)
    for row in out.values():
        row['terms'] = sorted(row['terms'])
    return out


def required_universe(strategy=None, registry=None, profile=None, per_slot=4):
    """Only the buckets that OWE an interval: the critical and rolling tiers.

    The denominator for any honest coverage claim. An exploratory bucket is a
    real, auditable task identity and it is deliberately not in here, because a
    schedule that promises the same interval to a speculative pairing as to the
    candidate's core route is not a schedule.
    """
    return {b: row for b, row in bucket_universe(strategy, registry, profile,
                                                 per_slot).items()
            if row['owes_interval']}


def universe_by_tier(strategy=None, registry=None, profile=None, per_slot=4):
    rows = bucket_universe(strategy, registry, profile, per_slot)
    out = {tier: sorted(b for b, r in rows.items() if r['tier'] == tier)
           for tier in TIERS}
    return out


# --------------------------------------------------------------------------
# Deadlines. A target expressed in HOURS has to be scheduled in hours.
#
# Phase 4E ordered critical work by raw debt and held a fixed rolling quota, so
# at a steady 30-hour cadence a critical bucket reached 120 hours against a
# 72-hour target while the same run still spent slots on rolling work nowhere
# near its own deadline. Age says which bucket has waited longest. SLACK says
# which one is about to breach. Those are different questions, and only the
# second one is a deadline.
# --------------------------------------------------------------------------

URGENCY = ('breached', 'at_risk', 'due_soon', 'comfortable')


def deadline_policy(strategy=None):
    policy = coverage_policy(strategy).get('deadlines')
    if not isinstance(policy, dict):
        raise ledger_error(
            'config/search_strategy.json has no coverage_policy.deadlines.')
    return policy


def deadline_fields(bucket, row, window_row, now=None, strategy=None,
                    next_run_in_hours=None):
    """Every temporal fact a scheduler needs about one bucket.

    `predicted_age_at_next_normal_run` is the field that changes decisions: a
    bucket comfortable now and breached by the next run has to be searched NOW,
    and nothing measurable from its current age alone would say so.
    """
    from datetime import datetime, timedelta
    from search_window import _parse
    policy = deadline_policy(strategy)
    now = _parse(now) or datetime.now().astimezone()
    target = row.get('target_revisit_hours')
    stamp = _parse((window_row or {}).get('last_successful_coverage'))
    horizon = float(next_run_in_hours if next_run_in_hours is not None
                    else policy.get('at_risk_within_hours', 24))

    if target is None:
        return {'coverage_bucket': bucket, 'target_revisit_hours': None,
                'deadline_at': '', 'current_age_hours': None, 'slack_hours': None,
                'overdue_hours': 0.0, 'predicted_age_at_next_normal_run': None,
                'urgency': 'comfortable', 'owes_interval': False}
    if stamp is None:
        # Never searched. It owes the whole initial window, so it sorts with the
        # breached work rather than looking comfortable for lack of a number.
        return {'coverage_bucket': bucket, 'target_revisit_hours': int(target),
                'deadline_at': '', 'current_age_hours': None,
                'slack_hours': float('-inf'), 'overdue_hours': None,
                'predicted_age_at_next_normal_run': None,
                'urgency': 'breached', 'owes_interval': True,
                'note': 'never searched'}

    age = round((now - stamp).total_seconds() / 3600.0, 2)
    deadline = stamp + timedelta(hours=int(target))
    slack = round((deadline - now).total_seconds() / 3600.0, 2)
    predicted = round(age + horizon, 2)
    if slack < 0:
        urgency = 'breached'
    elif predicted > int(target):
        urgency = 'at_risk'
    elif slack <= horizon * 2:
        urgency = 'due_soon'
    else:
        urgency = 'comfortable'
    return {
        'coverage_bucket': bucket, 'target_revisit_hours': int(target),
        'deadline_at': deadline.isoformat(), 'current_age_hours': age,
        'slack_hours': slack, 'overdue_hours': round(max(0.0, -slack), 2),
        'predicted_age_at_next_normal_run': predicted,
        'urgency': urgency, 'owes_interval': True,
    }


def deadlines(bucket_rows, window_rows, now=None, strategy=None,
              next_run_in_hours=None):
    return {b: deadline_fields(b, row, (window_rows or {}).get(b), now, strategy,
                               next_run_in_hours)
            for b, row in (bucket_rows or {}).items() if row.get('owes_interval')}


def at_risk_buckets(deadline_rows):
    """Buckets that will breach before anything can search them again."""
    return sorted(b for b, r in (deadline_rows or {}).items()
                  if r['urgency'] in ('breached', 'at_risk'))


# --------------------------------------------------------------------------
# Service status. FOUR questions, four answers, never one boolean for all of
# them.
#
# Production run scrape-20260831T102144228455 is why these are separated. It
# covered every critical bucket it could, closed cleanly, held no lock and
# carried `errors: []`. Four SUPPLEMENTAL families failed: two blocked by a
# browser extension permission, two unable to execute a query at all. Because
# `coverage_status` collapsed every inventory family into one verdict, those
# four made the whole run PARTIAL, `run_is_successful` then rejected it, and
# `select_window` saw no successful run and returned INITIAL_CATCHUP again. A
# workspace could therefore never leave catch-up while any optional website was
# unreachable, no matter how complete the critical work was.
#
# Splitting the question does NOT relax any standard. Critical service still
# demands every critical bucket. Full inventory still reports every gap. What
# changes is that an optional site can no longer veto a complete critical run.
# --------------------------------------------------------------------------

def critical_service(records=None, summaries=None, strategy=None, registry=None,
                     universe=None):
    """Are all critical_fresh buckets covered by creditable, covering queries?"""
    universe = universe if universe is not None else required_universe(
        strategy=strategy, registry=registry)
    critical = {b for b, row in universe.items() if row.get('tier') == 'critical_fresh'}
    marks = checkpoints(records, summaries)
    covered = sorted(critical & set(marks))
    outstanding = sorted(critical - set(marks))
    return {
        'tier': 'critical_fresh',
        'required': len(critical),
        'covered': len(covered),
        'outstanding': outstanding,
        'status': 'COMPLETE' if not outstanding else 'INCOMPLETE',
        'note': ('Every critical bucket has a covering checkpoint.' if not outstanding
                 else f'{len(outstanding)} critical bucket(s) still owe coverage.'),
    }


def rolling_service(records=None, summaries=None, now=None, strategy=None,
                    registry=None, universe=None):
    """Rolling work, split into covered, awaiting first coverage, and OVERDUE.

    A rolling bucket that is deferred inside its own seven-day target is being
    serviced correctly, not failing. Only a bucket whose OWN last coverage is
    older than its target has broken a promise. A bucket never searched yet is
    reported separately and prioritised, but it cannot fail rolling service,
    because no interval has started for it to have missed.
    """
    universe = universe if universe is not None else required_universe(
        strategy=strategy, registry=registry)
    rolling = {b: row for b, row in universe.items()
               if row.get('tier') == 'rolling_recall'}
    marks = checkpoints(records, summaries)
    wins = bucket_windows(rolling, records, summaries, now, strategy)
    dl = deadlines(rolling, wins, now, strategy)

    covered, awaiting, overdue = [], [], []
    for b in sorted(rolling):
        if b in marks:
            if dl.get(b, {}).get('urgency') == 'breached':
                overdue.append(b)
            else:
                covered.append(b)
        else:
            awaiting.append(b)
    return {
        'tier': 'rolling_recall',
        'required': len(rolling),
        'covered': len(covered),
        'awaiting_first_coverage': awaiting,
        'overdue': overdue,
        'status': 'ON_SCHEDULE' if not overdue else 'OVERDUE',
        'note': ('No rolling bucket has passed its own target interval.'
                 if not overdue else
                 f'{len(overdue)} rolling bucket(s) are past their target interval.'),
    }


def service_report(records=None, summaries=None, now=None, strategy=None,
                   registry=None):
    """The four separate answers, plus what may depend on each.

    `full_inventory` is deliberately NOT computed here: it belongs to
    discovery_run.summarise, which owns source and family health. This function
    answers only the bucket-schedule questions.
    """
    universe = required_universe(strategy=strategy, registry=registry)
    crit = critical_service(records, summaries, strategy, registry, universe)
    roll = rolling_service(records, summaries, now, strategy, registry, universe)
    return {
        'schema_version': 1,
        'required_total': len(universe),
        'critical': crit,
        'rolling': roll,
        'critical_service_complete': crit['status'] == 'COMPLETE',
        'rolling_on_schedule': roll['status'] == 'ON_SCHEDULE',
        'note': ('Critical and rolling service are separate questions, and both are '
                 'separate from full-inventory coverage, which stays PARTIAL while '
                 'any attempted family has a gap.'),
    }


def deadline_safe_slots(tier, cadence_hours, strategy=None, registry=None,
                        profile=None):
    """Unique buckets this tier must search per run to hold its target.

        max_intervals    = floor(target / cadence)
        required_uniques = ceil(bucket_count / max_intervals)

    Average capacity is necessary and not sufficient. With discrete runs a tier
    needing 4.0 slots on average still breaches if the schedule ever gives it 3
    twice in a row, and the ceiling of the discrete division is what a maximum
    revisit guarantee actually costs.
    """
    import math
    count = len(universe_by_tier(strategy, registry, profile)[tier])
    target = target_revisit_hours(tier, strategy)
    if not count or not target or float(cadence_hours) <= 0:
        return None
    intervals = math.floor(float(target) / float(cadence_hours))
    if intervals < 1:
        return None
    return math.ceil(count / intervals)


def capacity_feasibility(strategy=None, registry=None, profile=None):
    """Can the declared capacity actually meet the declared targets?

        critical_buckets * cadence / target <= critical_slots_per_run

    Validation fails on a policy that promises a freshness its own budget cannot
    deliver. Claiming 72 hours at a cadence where the arithmetic needs 19 slots
    and 15 exist is not a target, it is a wish, and the difference only shows up
    weeks later as inventory nobody searched.
    """
    from search_strategy import mode_budget
    policy = deadline_policy(strategy)
    tiers = universe_by_tier(strategy, registry, profile)
    rows = []
    for cadence in policy.get('supported_cadences_hours') or (24,):
        # Which mode a run at this cadence actually gets. Inside the daily
        # interval it is `daily`; past it the run is a recovery and carries more
        # budget, which is where the extra critical slots come from.
        mode = 'daily' if float(cadence) <= 24 else 'catchup'
        budget = int(mode_budget(mode, strategy)['global_query_budget'])
        quota = int(((tier_policy(strategy).get('run_quotas') or {}).get(mode) or {})
                    .get('rolling_recall', 0))
        # Slots a run can give critical work: the query budget, less the rolling
        # floor and the two reserved supplemental pairs.
        slots = budget - quota - 4
        for tier in ('critical_fresh', 'rolling_recall'):
            count = len(tiers[tier])
            target = target_revisit_hours(tier, strategy)
            if not target or not count:
                continue
            available = slots if tier == 'critical_fresh' else max(quota, 1)
            needed = round(count * float(cadence) / float(target), 2)
            # Average capacity is necessary and NOT sufficient with discrete
            # runs: a tier needing 4.0 slots on average still breaches if the
            # schedule ever hands it 3 twice in a row. The deadline-safe figure
            # is what a maximum-revisit guarantee actually costs.
            safe = deadline_safe_slots(tier, cadence, strategy, registry, profile)
            rows.append({
                'cadence_hours': cadence, 'mode_at_this_cadence': mode,
                'tier': tier, 'buckets': count, 'target_revisit_hours': target,
                'slots_needed_per_run': needed,
                'deadline_safe_slots_per_run': safe,
                'max_intervals': (None if not safe else
                                  int(float(target) // float(cadence))),
                'slots_available_per_run': available,
                'feasible': needed <= available,
                'deadline_safe_feasible': bool(safe) and safe <= available,
                'headroom': round(available - needed, 2),
                'deadline_safe_headroom': (None if not safe else available - safe),
            })
    return {'schema_version': SCHEMA_VERSION,
            'formula': policy.get('feasibility_formula'),
            'rows': rows,
            'all_feasible': all(r['feasible'] for r in rows),
            'all_deadline_safe': all(r['deadline_safe_feasible'] for r in rows),
            'infeasible': [r for r in rows if not r['feasible']],
            'not_deadline_safe': [r for r in rows if not r['deadline_safe_feasible']],
            'note': ('A target may claim a hard maximum revisit only when '
                     'deadline_safe_feasible holds. Average feasibility alone '
                     'permits a schedule that meets the mean and misses the '
                     'maximum, which is the only number a guarantee is about.')}


# --------------------------------------------------------------------------
# Bootstrap. Derived, like everything else here, so it cannot drift.
# --------------------------------------------------------------------------

def bootstrap_status(records=None, summaries=None, strategy=None, registry=None,
                     profile=None):
    """Has the one-time initial catch-up actually discharged its obligations?

    Complete only when EVERY critical bucket carries a successful checkpoint.
    That falls out of the checkpoint rules rather than needing rules of its own:
    a partial run advances nothing, a failed critical query advances nothing, an
    unfunded bucket advances nothing, and a bucket first searched three days
    later gets a checkpoint dated three days later, so it can never retroactively
    claim the oldest interval the first run was supposed to cover.
    """
    tiers = universe_by_tier(strategy, registry, profile)
    critical = set(tiers['critical_fresh'])
    marks = checkpoints(records, summaries)
    covered = critical & set(marks)
    started = sorted(r.get('started_at', '') for r in (records or [])
                     if r.get('started_at'))
    complete = bool(critical) and covered == critical
    return {
        'schema_version': SCHEMA_VERSION,
        'critical_total': len(critical),
        'critical_covered': len(covered),
        'critical_outstanding': sorted(critical - covered),
        'complete': complete,
        'started_at': started[0] if started else '',
        'completed_at': max((marks[b]['finished_at'] for b in covered), default=''),
        'runs_examined': len(records or []),
        'note': (
            'Initial catch-up is COMPLETE: every critical bucket carries a '
            'successful checkpoint.' if complete else
            f'Initial catch-up is INCOMPLETE: {len(critical) - len(covered)} '
            f'critical bucket(s) have never been successfully searched. A partial '
            f'run, a failed critical query and an unfunded bucket each leave it '
            f'incomplete, which is the point.'),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_buckets(args):
    universe = required_universe()
    print(json.dumps({
        'schema_version': SCHEMA_VERSION,
        'bucket_key': coverage_policy()['bucket_key'],
        'required_search_families': list(required_search_families()),
        'required_bucket_count': len(universe),
        'buckets': universe if args.verbose else sorted(universe),
    }, indent=2, ensure_ascii=False))


def cmd_checkpoints(args):
    from search_window import _history
    records, summaries = _history()
    marks = checkpoints(records, summaries)
    universe = required_universe()
    print(json.dumps({
        'schema_version': SCHEMA_VERSION, 'runs_examined': len(records),
        'required_buckets': len(universe),
        'required_with_coverage': len([b for b in universe if b in marks]),
        'checkpoints': marks,
    }, indent=2, ensure_ascii=False))


def cmd_windows(args):
    from search_window import _history
    records, summaries = _history()
    print(json.dumps(bucket_windows(sorted(required_universe()), records, summaries,
                                    global_window=args.window),
                     indent=2, ensure_ascii=False))


def cmd_feasibility(args):
    print(json.dumps(capacity_feasibility(), indent=2, ensure_ascii=False))


def cmd_bootstrap(args):
    from search_window import _history
    records, summaries = _history()
    print(json.dumps(bootstrap_status(records, summaries), indent=2,
                     ensure_ascii=False))


def cmd_service(args):
    from search_window import _history
    records, summaries = _history()
    print(json.dumps(service_report(records, summaries), indent=2,
                     ensure_ascii=False))


def cmd_denominators(args):
    """The AUTHORITATIVE current tier counts, for anything that would otherwise
    hard-code them into prose. Never restate these numbers in documentation."""
    import collections
    universe = required_universe()
    counts = collections.Counter(row['tier'] for row in universe.values())
    print(json.dumps({
        'schema_version': 2,
        'required_total': len(universe),
        'critical_fresh': counts.get('critical_fresh', 0),
        'rolling_recall': counts.get('rolling_recall', 0),
        'note': ('Derived from config/search_strategy.json and config/sources.json at '
                 'runtime. A source-policy change moves these numbers, so no document '
                 'may quote them as current authority.'),
    }, indent=2, ensure_ascii=False))


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
    p = argparse.ArgumentParser(description='Per-bucket coverage checkpoints')
    sub = p.add_subparsers(dest='cmd', required=True)

    b = sub.add_parser('buckets', help='Every required coverage bucket.')
    b.add_argument('--verbose', action='store_true')
    b.set_defaults(func=cmd_buckets)

    sub.add_parser('checkpoints', help='Last successful coverage per bucket.'
                   ).set_defaults(func=cmd_checkpoints)

    sub.add_parser('feasibility', help='Can capacity meet the declared targets?'
                   ).set_defaults(func=cmd_feasibility)
    sub.add_parser('bootstrap', help='Initial catch-up completion state.'
                   ).set_defaults(func=cmd_bootstrap)
    sub.add_parser('service', help='Tier-aware critical and rolling service status.'
                   ).set_defaults(func=cmd_service)
    sub.add_parser('denominators', help='Current tier counts, derived at runtime.'
                   ).set_defaults(func=cmd_denominators)

    w = sub.add_parser('windows', help='Effective window each bucket needs now.')
    w.add_argument('--window', default='24h')
    w.set_defaults(func=cmd_windows)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

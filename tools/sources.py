#!/usr/bin/env python3
"""Deterministic owner of discovery source identity and family.

`config/sources.json` is the single definition of what a discovery source is and
which inventory family it belongs to. Prose rules may explain how to search a
source, but they must not redefine its identity, its family, or the state
`source_type` it maps to.

Coverage diversity is measured by FAMILY, not by nominal site count, so searching
CWJobs and Totaljobs is one StepStone family rather than two independent boards.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import SOURCE_CONFIDENCES, SOURCE_TYPES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'config' / 'sources.json'

KINDS = (
    'authenticated-board', 'board', 'aggregator', 'employer', 'ats',
    'search-engine', 'sponsor-board',
)
FRESHNESS_SUPPORT = (
    'reliable-filter', 'unreliable-filter', 'per-item-date', 'none', 'unknown',
)

# Controlled source-outcome vocabulary. `empty` and the failure outcomes mean very
# different things: a source that genuinely held nothing is market supply, while a
# source that broke is missing coverage. Collapsing them would let a failed source
# masquerade as a thin market.
SOURCE_OUTCOMES = (
    'ok',
    'empty',
    'partial',
    'blocked_captcha',
    'blocked_permission',
    'changed_layout',
    'timeout',
    'unavailable',
    'error',
)
# Outcomes where the source did not deliver the inventory it was asked for.
FAILED_OUTCOMES = (
    'partial', 'blocked_captcha', 'blocked_permission', 'changed_layout',
    'timeout', 'unavailable', 'error',
)
# Outcomes that count as a source having been genuinely searched end to end.
COMPLETE_OUTCOMES = ('ok', 'empty')

# Sources that discovery rules are allowed to name. Each token must resolve to a
# registry entry through `rule_aliases` or an ATS `platforms` entry, so a source
# can never be described in prose while having no deterministic definition.
DOCUMENTED_SOURCE_TOKENS = (
    'LinkedIn', 'Indeed', 'CWJobs', 'Totaljobs', 'Reed', 'DWP', 'Find a Job',
    'Built In', 'Welcome to the Jungle', 'JobServe', 'Technojobs', 'Adzuna',
    'Greenhouse', 'Lever', 'Ashby', 'Workable', 'SmartRecruiters', 'Workday',
    'Oracle Recruiting', 'Hunt UK Visa Sponsors', 'SkilledJobs', 'SponsoredJobs',
    'FindSponsorJobs', 'GradSponsor', 'Jobsponsor',
)

RULE_FILES = (
    'CLAUDE.md',
    '.claude/skills/scrape/SKILL.md',
    '.claude/skills/scrape/search-queries.md',
    # Phase 4 moved which families pay off on which source out of prose and into
    # the structured search authority, which is where a planner can read it. A
    # source named by a search family's eligible_sources IS referenced by the
    # rules; it is referenced by the strongest form of them. Leaving this file
    # out would have made deleting a duplicated prose list look like orphaning
    # every source it used to name.
    'config/search_strategy.json',
)


def registry_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def load_registry(path=None):
    """Parse the source registry, raising an actionable message instead of a traceback."""
    path = Path(path or REGISTRY)
    if not path.exists():
        raise registry_error(
            f'Missing source registry: {path}',
            'config/sources.json is the deterministic owner of source identity and family.',
        )
    try:
        raw = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise registry_error(f'Source registry could not be read: {path}', f'{type(exc).__name__}: {exc}') from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise registry_error(
            f'Malformed source registry: {path}',
            f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}',
        ) from None
    if not isinstance(data, dict) or not isinstance(data.get('sources'), list):
        raise registry_error(
            f'Invalid source registry: {path}',
            'Expected an object with a "sources" array and a "families" object.',
        )
    return data


def all_sources(registry=None):
    return list((registry or load_registry()).get('sources', []))


def families(registry=None):
    return dict((registry or load_registry()).get('families', {}))


def by_id(registry=None):
    return {s.get('id'): s for s in all_sources(registry) if s.get('id')}


def get_source(source_id, registry=None):
    found = by_id(registry).get((source_id or '').strip())
    if found is None:
        raise registry_error(
            f'Unknown source id: {source_id!r}',
            'Every discovery source must be defined in config/sources.json.',
            f'Known ids: {", ".join(sorted(by_id(registry)))}',
        )
    return found


def is_known_source(source_id, registry=None):
    return (source_id or '').strip() in by_id(registry)


def source_family(source_id, registry=None):
    return get_source(source_id, registry).get('family', '')


def state_source_type_for(source_id, authenticated=False, registry=None):
    """The state `source_type` this source maps to, or None when the target decides.

    A null `state_source_type` (public web search) means the resolved target owns
    the value, so the candidate must supply an explicit source_type instead.
    """
    source = get_source(source_id, registry)
    if authenticated and source.get('authenticated_source_type'):
        return source['authenticated_source_type']
    return source.get('state_source_type')


def source_confidence_for(source_id, authenticated=False, registry=None):
    source = get_source(source_id, registry)
    if authenticated and source.get('authenticated_source_confidence'):
        return source['authenticated_source_confidence']
    return source.get('default_source_confidence', 'low')


def family_coverage(source_ids, registry=None):
    """Group attempted source ids by inventory family.

    This is the honest diversity measure: two StepStone sites are one family.
    """
    registry = registry or load_registry()
    grouped = {}
    for source_id in source_ids:
        if not is_known_source(source_id, registry):
            grouped.setdefault('unknown', []).append(source_id)
            continue
        grouped.setdefault(source_family(source_id, registry), []).append(source_id)
    return {family: sorted(set(ids)) for family, ids in sorted(grouped.items())}


def forbidden_panel_markers(source_id, registry=None):
    """Wording that proves an extracted block is not this source's result list."""
    if not is_known_source(source_id, registry):
        return []
    return list(get_source(source_id, registry).get('forbidden_panel_markers', []) or [])


def forbidden_panel_hits(source_id, text, registry=None):
    """Which forbidden-panel markers appear in an extracted block.

    Totaljobs can serve a personalised recommendation panel in place of the
    filtered result list. It looks like results, ignores the requested filter, and
    must never be ingested as discovery inventory. Any hit here means the extracted
    text is not search results.
    """
    body = str(text or '')
    return [m for m in forbidden_panel_markers(source_id, registry)
            if re.search(re.escape(m), body, re.I)]


def promoted_card_markers(source_id, registry=None):
    """Badges marking a card that is served regardless of the posted-within filter."""
    if not is_known_source(source_id, registry):
        return []
    return list(get_source(source_id, registry).get('promoted_card_markers', []) or [])


def filter_is_trustworthy(source_id, registry=None):
    """Whether this source's posted-within filter can be trusted on its own.

    False for CWJobs and Totaljobs, where promoted slots ignore the filter, so each
    candidate's own posted date or visible age must decide its freshness.
    """
    if not is_known_source(source_id, registry):
        return False
    return get_source(source_id, registry).get('freshness_support') == 'reliable-filter'


def registry_problems(registry=None):
    """Every structural problem in the registry itself."""
    registry = registry or load_registry()
    problems = []
    declared_families = families(registry)
    seen_ids = {}
    seen_aliases = {}

    for index, source in enumerate(registry.get('sources', [])):
        where = f'sources[{index}]'
        if not isinstance(source, dict):
            problems.append({'where': where, 'problem': 'not_an_object'})
            continue
        source_id = (source.get('id') or '').strip()
        if not source_id:
            problems.append({'where': where, 'problem': 'missing_id'})
            continue
        if source_id in seen_ids:
            problems.append({'where': where, 'id': source_id, 'problem': 'duplicate_id'})
        seen_ids[source_id] = index

        for field in ('display_name', 'family', 'kind', 'freshness_support', 'notes'):
            if not str(source.get(field) or '').strip():
                problems.append({'id': source_id, 'field': field, 'problem': 'missing'})
        for field in ('requires_authenticated_browser', 'enabled'):
            if not isinstance(source.get(field), bool):
                problems.append({'id': source_id, 'field': field, 'problem': 'not_a_boolean'})

        family = (source.get('family') or '').strip()
        if family and family not in declared_families:
            problems.append({'id': source_id, 'field': 'family', 'value': family, 'problem': 'undeclared_family'})
        if source.get('kind') and source['kind'] not in KINDS:
            problems.append({'id': source_id, 'field': 'kind', 'value': source['kind'], 'problem': 'unknown_value'})
        if source.get('freshness_support') and source['freshness_support'] not in FRESHNESS_SUPPORT:
            problems.append({'id': source_id, 'field': 'freshness_support',
                             'value': source['freshness_support'], 'problem': 'unknown_value'})

        # The registry may only name state vocabularies job_state actually accepts.
        for field in ('state_source_type', 'authenticated_source_type'):
            value = source.get(field)
            if value is None:
                continue
            if value not in SOURCE_TYPES:
                problems.append({'id': source_id, 'field': field, 'value': value,
                                 'problem': 'not_a_state_source_type'})
        for field in ('default_source_confidence', 'authenticated_source_confidence'):
            value = source.get(field)
            if value is None:
                continue
            if value not in SOURCE_CONFIDENCES:
                problems.append({'id': source_id, 'field': field, 'value': value,
                                 'problem': 'not_a_state_source_confidence'})

        for alias in source.get('rule_aliases', []) or []:
            token = str(alias).strip().lower()
            if not token:
                problems.append({'id': source_id, 'field': 'rule_aliases', 'problem': 'empty_alias'})
                continue
            if token in seen_aliases and seen_aliases[token] != source_id:
                problems.append({'id': source_id, 'field': 'rule_aliases', 'value': alias,
                                 'problem': f'alias_also_claimed_by_{seen_aliases[token]}'})
            seen_aliases[token] = source_id

    # CWJobs and Totaljobs share one StepStone inventory. This is a load-bearing
    # diversity fact, not a preference, so the registry must keep asserting it.
    ids = seen_ids
    if 'cwjobs' in ids and 'totaljobs' in ids:
        cw = by_id(registry)['cwjobs'].get('family')
        tj = by_id(registry)['totaljobs'].get('family')
        if cw != tj or cw != 'stepstone':
            problems.append({'id': 'cwjobs/totaljobs', 'field': 'family',
                             'value': f'{cw}/{tj}', 'problem': 'stepstone_family_broken'})
    for required in ('linkedin', 'indeed', 'cwjobs', 'totaljobs', 'public-web',
                     'employer-direct', 'employer-ats'):
        if required not in ids:
            problems.append({'id': required, 'problem': 'required_source_missing'})
    return problems


def alias_terms(registry=None):
    """Every prose token that resolves to a registry entry."""
    registry = registry or load_registry()
    terms = {}
    for source in all_sources(registry):
        source_id = source.get('id', '')
        for alias in list(source.get('rule_aliases', []) or []) + list(source.get('platforms', []) or []):
            token = str(alias).strip().lower()
            if token:
                terms.setdefault(token, source_id)
    return terms


def rule_reference_problems(registry=None, root=None):
    """Source names used by discovery rules that no registry entry defines.

    Also reports registry entries that no rule file mentions, so the registry
    cannot quietly accumulate sources the workflow never searches. A source named
    by a search family in config/search_strategy.json counts as referenced: that
    is a machine-readable declaration that the workflow searches it, which is
    stronger evidence than a sentence naming it in passing.
    """
    registry = registry or load_registry()
    root = Path(root or ROOT)
    corpus = []
    for rel in RULE_FILES:
        path = root / rel
        if path.exists():
            corpus.append(path.read_text(encoding='utf-8'))
    blob = '\n'.join(corpus)
    problems = []
    terms = alias_terms(registry)

    for token in DOCUMENTED_SOURCE_TOKENS:
        if not re.search(r'\b' + re.escape(token) + r'\b', blob, re.I):
            continue
        if token.strip().lower() not in terms:
            problems.append({'token': token, 'problem': 'referenced_by_rules_but_not_registered'})

    for source in all_sources(registry):
        if not source.get('enabled', True):
            continue
        aliases = list(source.get('rule_aliases', []) or [])
        if not aliases:
            problems.append({'id': source.get('id'), 'problem': 'no_rule_aliases'})
            continue
        # The source ID counts as a reference too. Prose names a source by its
        # display alias ("Built In"); the structured search authority names it by
        # its id ("built-in"). Both are the workflow declaring that it searches
        # this source, and accepting only the prose form would reward duplication.
        aliases = [a for a in aliases + [str(source.get('id') or '')] if a]
        if not any(re.search(r'\b' + re.escape(a) + r'\b', blob, re.I) for a in aliases):
            problems.append({'id': source.get('id'), 'problem': 'registered_but_never_referenced_by_rules'})
    return problems


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_list(args):
    registry = load_registry()
    rows = all_sources(registry)
    if args.family:
        rows = [s for s in rows if s.get('family') == args.family]
    if args.enabled_only:
        rows = [s for s in rows if s.get('enabled', True)]
    print(json.dumps({
        'count': len(rows),
        'sources': [{
            'id': s.get('id'),
            'display_name': s.get('display_name'),
            'family': s.get('family'),
            'kind': s.get('kind'),
            'enabled': s.get('enabled'),
            'requires_authenticated_browser': s.get('requires_authenticated_browser'),
            'freshness_support': s.get('freshness_support'),
            'state_source_type': s.get('state_source_type'),
        } for s in rows],
    }, indent=2, ensure_ascii=False))


def cmd_get(args):
    print(json.dumps(get_source(args.id), indent=2, ensure_ascii=False))


def cmd_families(args):
    registry = load_registry()
    declared = families(registry)
    grouped = {}
    for source in all_sources(registry):
        grouped.setdefault(source.get('family', ''), []).append(source.get('id'))
    print(json.dumps({
        'count': len(declared),
        'families': {
            name: {
                'display_name': meta.get('display_name', ''),
                'notes': meta.get('notes', ''),
                'sources': sorted(grouped.get(name, [])),
            }
            for name, meta in sorted(declared.items())
        },
    }, indent=2, ensure_ascii=False))


def cmd_validate(args):
    registry = load_registry()
    problems = registry_problems(registry)
    reference_problems = [] if args.skip_rule_references else rule_reference_problems(registry)
    ok = not problems and not reference_problems
    print(json.dumps({
        'registry': str(REGISTRY.relative_to(ROOT)).replace('\\', '/'),
        'sources': len(all_sources(registry)),
        'families': len(families(registry)),
        'problems': problems,
        'rule_reference_problems': reference_problems,
        'valid': ok,
    }, indent=2, ensure_ascii=False))
    raise SystemExit(0 if ok else 1)


def main():
    p = argparse.ArgumentParser(description='Discovery source registry helper')
    sub = p.add_subparsers(dest='cmd', required=True)

    l = sub.add_parser('list', help='List registered sources.')
    l.add_argument('--family', default='')
    l.add_argument('--enabled-only', action='store_true')
    l.set_defaults(func=cmd_list)

    g = sub.add_parser('get', help='Show one source definition.')
    g.add_argument('id')
    g.set_defaults(func=cmd_get)

    f = sub.add_parser('families', help='Show inventory families and their sources.')
    f.set_defaults(func=cmd_families)

    v = sub.add_parser('validate', help='Validate registry structure and rule references.')
    v.add_argument('--skip-rule-references', action='store_true')
    v.set_defaults(func=cmd_validate)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

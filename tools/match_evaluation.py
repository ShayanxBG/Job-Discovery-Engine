#!/usr/bin/env python3
"""Deterministic validation and calculation of one match evaluation.

THE MODEL PROPOSES. PYTHON DECIDES.

A language model is good at reading a job description and judging whether the
work described is the work this candidate does. It is not a calculator, and it
should never be the authority on any of these:

    the arithmetic          adding five component scores
    the band                whether 79 is Viable or Strong
    the maxima              whether tech_fit can be worth 45 today
    eligibility             whether a blocker outranks a high score

Each of those has exactly one correct answer, derivable from
`config/matching_policy.json` and the private candidate calibration. So the model
returns a structured PROPOSAL of component scores with evidence, and this module
validates every number against policy and computes the total, the band and
eligibility itself. Where a proposal disagrees with the arithmetic, Python wins;
where it proposes an out-of-range score or a component the policy does not define,
the evaluation is rejected rather than silently corrected, because a silent
correction hides a model that has misunderstood the model.

WHAT A BLOCKER DOES, AND DOES NOT DO. A hard blocker sets `eligible: false` and
overrides the total. It does NOT zero the component scores. Destroying the
diagnostic score would throw away the reason the role was interesting: a role
blocked today on an explicit no-sponsorship statement, scoring 86 on everything
else, is a completely different record from a genuinely poor 41, and next month
the blocker may be gone.

VERIFY FIRST IS AN ACTION. A role can score well and still need verification.
`verification_needed` never changes the score, the band or the lead type. A Direct
Match needing sponsorship confirmation is still a Direct Match.

THE AGENCY MODEL IS A DIFFERENT MODEL, not a discount. An agency advert whose
employing client is unknown has no employer to check for sponsorship, so the
25-point component is EXCLUDED rather than scored zero, and the total is out of 75.
Rendering that as `/100` would be a false claim, so the denominator travels with
the score and the result is always marked provisional.

A NUMBER IS NOT A JUDGEMENT UNTIL SOMETHING SUPPORTS IT. Validating the
arithmetic, the ranges, the bands and the blocker spelling proves an evaluation is
well FORMED, and says nothing at all about whether it is TRUE. That gap accepted,
in order:

    a 100/100 Exceptional Match whose every component said "no vacancy evidence
    was available", because the evidence field was only length-checked;

    an `experience_requirement` blocker whose own evidence said the advert asked
    for two years, against a calibrated hard threshold of four, because a blocker
    was checked for SPELLING and for whether the calibration enabled it, never for
    whether the vacancy's facts supported it;

    an `explicit_no_sponsorship` blocker carrying empty evidence, because blocker
    evidence was read with `or ''` and never required.

So this module now also decides GROUNDING, deterministically and from
configuration rather than from magic numbers:

    uncertainty ceilings   a component can never score above floor(max x ratio)
                           for its declared uncertainty, so an all-unknown
                           evaluation cannot reach a qualifying band at all
    evidence quality       evidence that says only that nothing is known drops the
                           ceiling to the unknown ratio, without ever rejecting the
                           component or reading silence as a negative fact
    full-marks anchors     the exact maximum requires the strongest anchor the
                           policy documents for that component
    blocker preconditions  each blocker names the factual precondition it has to
                           satisfy, checked against STRUCTURED vacancy facts and
                           the private calibration, with the quote, the source and
                           who stated it all required

Uncertainty is not hidden by any of this: the applied ceiling travels on each
component and the run's uncertainty profile travels on the result.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidate_config import CONFIG as CANDIDATE_CONFIG_PATH, load_config  # noqa: E402
# job_state owns the structured-fact vocabulary and the source-type vocabulary,
# and imports nothing from here, so reusing them costs no cycle and keeps one
# definition of what a vacancy fact is.
from job_state import FACT_FIELDS, SOURCE_TYPES, facts_problems, normalise_facts  # noqa: E402
# The read-only VIEW of what this workspace canonically knows about a vacancy.
# It owns no data: it assembles the discovery record and the cached employer
# description that already exist, so a blocker can be checked against them
# rather than against whatever the model that proposed it also wrote down.
import canonical_vacancy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / 'config' / 'matching_policy.json'
# Schema 2 adds evidence grounding: a per-component `ceiling`, the run's
# `uncertainty_summary`, the `facts_used` the calculation actually consumed, and
# structured blocker evidence carrying its precondition and what was compared.
SCHEMA_VERSION = 2

LEAD_TYPES = ('direct', 'agency', 'verification')
_POLICY_CACHE = {}

# Every deterministic factual precondition a hard blocker may be required to
# satisfy. The policy names one per blocker; a blocker naming a precondition that
# is not implemented here makes the policy invalid, so a new blocker can never be
# added with no factual test at all. Implementations live in PRECONDITIONS below.
PRECONDITION_IDS = (
    'structured_minimum_years_at_or_above_hard_maximum',
    'vacancy_level_sponsorship_refusal',
    'structured_salary_below_configured_floor',
    'employer_stated_excluded_employment_type',
    'excluded_value_named',
    'excluded_level_is_the_role_level',
    'excluded_specialism_is_the_primary_specialism',
    'foreign_primary_language_named',
    'security_clearance_required',
    'canonical_country_outside_market',
)

# Who asserted a blocker's supporting excerpt. `platform` is the search platform's
# own classification of the role (LinkedIn's `Employment type` block, a results
# card label) and is never the employer speaking; `inference` is the model's own
# reading and can never support a decided rejection.
STATED_BY_NEVER_BLOCKS = ('inference',)


def evaluation_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def load_policy(path=None):
    """Parse the matching policy, refusing an invalid one outright.

    An invalid policy must never be used: a ranking run against a broken weighting
    would produce scores that look ordinary and mean nothing.
    """
    path = Path(path) if path else POLICY_PATH
    key = str(path)
    if key in _POLICY_CACHE:
        return _POLICY_CACHE[key]
    if not path.exists():
        raise evaluation_error(f'Matching policy not found: {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise evaluation_error(f'Malformed matching policy: {path}',
                               f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None
    problems = policy_problems(data)
    if problems:
        raise evaluation_error('The matching policy is not valid.',
                               f'Problems: {json.dumps(problems, ensure_ascii=False)}',
                               'Ranking must not run against a broken scoring model.')
    _POLICY_CACHE[key] = data
    return data


def policy_problems(policy):
    """Every structural problem in the matching policy."""
    problems = []
    if not isinstance(policy, dict):
        return [{'field': '_root', 'problem': 'not_an_object'}]

    direct = policy.get('direct_model') or {}
    components = direct.get('components') or {}
    if not components:
        problems.append({'field': 'direct_model.components', 'problem': 'required'})
    total_max = direct.get('total_max')
    summed = 0
    for name, block in components.items():
        value = (block or {}).get('max_score')
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            problems.append({'field': f'direct_model.components.{name}.max_score',
                             'value': value, 'problem': 'not_a_non_negative_integer'})
            continue
        summed += value
    if components and summed != total_max:
        # The single most important invariant. A model whose parts no longer sum to
        # its whole produces scores that are not on the scale they claim to be.
        problems.append({'field': 'direct_model', 'problem': 'component_maxima_do_not_sum_to_total',
                         'sum_of_components': summed, 'total_max': total_max})
    if components and summed != 100:
        problems.append({'field': 'direct_model', 'problem': 'direct_model_must_total_100',
                         'sum_of_components': summed})

    bands = direct.get('bands') or []
    if not bands:
        problems.append({'field': 'direct_model.bands', 'problem': 'required'})
    previous_min = None
    covered = []
    for band in bands:
        low, high = (band or {}).get('min_score'), (band or {}).get('max_score')
        if not isinstance(low, int) or not isinstance(high, int) or low > high:
            problems.append({'field': f"direct_model.bands.{(band or {}).get('id')}",
                             'problem': 'malformed_band_range'})
            continue
        if previous_min is not None and low >= previous_min:
            problems.append({'field': f"direct_model.bands.{band.get('id')}",
                             'problem': 'bands_must_descend'})
        previous_min = low
        covered.append((low, high))
    if covered:
        ordered = sorted(covered)
        if ordered[0][0] != 0 or ordered[-1][1] != total_max:
            problems.append({'field': 'direct_model.bands', 'problem': 'bands_must_cover_the_whole_range'})
        for (_, high), (low, _) in zip(ordered, ordered[1:]):
            if low != high + 1:
                problems.append({'field': 'direct_model.bands',
                                 'problem': 'bands_must_be_contiguous_without_overlap',
                                 'gap_between': [high, low]})

    location = policy.get('location_policy') or {}
    if location.get('score_weight') != 0:
        # Location carrying any weight at all, positive or negative, contradicts a
        # product promise that a UK relocation costs nothing.
        problems.append({'field': 'location_policy.score_weight',
                         'value': location.get('score_weight'),
                         'problem': 'location_must_carry_exactly_zero_score_weight'})
    if location.get('contributes_to_components'):
        problems.append({'field': 'location_policy.contributes_to_components',
                         'problem': 'location_must_not_contribute_to_any_component'})

    agency = policy.get('agency_model') or {}
    agency_components = agency.get('components') or {}
    agency_sum = sum((block or {}).get('max_score', 0) for block in agency_components.values())
    if agency_sum != agency.get('total_max'):
        problems.append({'field': 'agency_model', 'problem': 'agency_maxima_do_not_sum_to_total',
                         'sum_of_components': agency_sum, 'total_max': agency.get('total_max')})
    if 'sponsorship' in agency_components:
        problems.append({'field': 'agency_model.components.sponsorship',
                         'problem': 'agency_model_must_exclude_sponsorship'})
    if 'sponsorship' not in (agency.get('excluded_components') or []):
        problems.append({'field': 'agency_model.excluded_components',
                         'problem': 'agency_model_must_declare_sponsorship_excluded'})

    if not (policy.get('uncertainty') or {}).get('vocabulary'):
        problems.append({'field': 'uncertainty.vocabulary', 'problem': 'required'})
    if not (policy.get('hard_blockers') or {}).get('vocabulary'):
        problems.append({'field': 'hard_blockers.vocabulary', 'problem': 'required'})
    if not policy.get('verification_reasons'):
        problems.append({'field': 'verification_reasons', 'problem': 'required'})
    problems.extend(grounding_problems(policy, components, total_max))
    return problems


def grounding_problems(policy, components, total_max):
    """Problems in the rules that decide whether a score is SUPPORTED.

    A policy that has lost these is not merely incomplete: it is a policy under
    which an evaluation with no evidence at all could score 100, which is exactly
    the defect these rules exist to close. So they are required, not optional.
    """
    problems = []
    evidence = policy.get('evidence_policy') or {}
    uncertainties = list((policy.get('uncertainty') or {}).get('vocabulary') or [])
    ceilings = evidence.get('uncertainty_ceilings')
    if not isinstance(ceilings, dict) or not ceilings:
        problems.append({'field': 'evidence_policy.uncertainty_ceilings', 'problem': 'required'})
        ceilings = {}
    for token in uncertainties:
        ratio = ceilings.get(token)
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 <= ratio <= 1:
            problems.append({'field': f'evidence_policy.uncertainty_ceilings.{token}',
                             'value': ratio, 'problem': 'not_a_ratio_between_zero_and_one'})
    for extra in sorted(set(ceilings) - set(uncertainties)):
        problems.append({'field': f'evidence_policy.uncertainty_ceilings.{extra}',
                         'problem': 'not_in_the_uncertainty_vocabulary'})
    ordered = [ceilings.get(t) for t in ('known', 'partial', 'unknown') if t in ceilings]
    if len(ordered) == 3 and not ordered[0] >= ordered[1] >= ordered[2]:
        problems.append({'field': 'evidence_policy.uncertainty_ceilings',
                         'problem': 'ceilings_must_not_rise_as_certainty_falls',
                         'value': ordered})
    if ceilings.get('known') != 1:
        # `known` is what a fully evidenced component means, so it is the one
        # uncertainty that must be able to reach its own maximum.
        problems.append({'field': 'evidence_policy.uncertainty_ceilings.known',
                         'value': ceilings.get('known'),
                         'problem': 'known_evidence_must_be_able_to_reach_the_component_maximum'})

    # THE INVARIANT THAT MATTERS MOST: an evaluation that establishes nothing must
    # not be able to reach a qualifying band. Without this, a proposal could claim
    # every component was unknown and still be recommended.
    if components and isinstance(ceilings.get('unknown'), (int, float)):
        blind = sum(_ceiling_from_ratio((block or {}).get('max_score') or 0, ceilings['unknown'])
                    for block in components.values())
        qualifying = [b.get('min_score') for b in (policy.get('direct_model') or {}).get('bands') or []
                      if isinstance(b, dict) and b.get('id') != 'below_threshold'
                      and isinstance(b.get('min_score'), int)]
        floor = min(qualifying) if qualifying else total_max
        if blind >= floor:
            problems.append({'field': 'evidence_policy.uncertainty_ceilings.unknown',
                             'problem': 'an_evaluation_with_no_evidence_could_reach_a_qualifying_band',
                             'all_unknown_ceiling_total': blind, 'lowest_qualifying_band': floor})

    non_informative = evidence.get('non_informative_evidence')
    if not isinstance(non_informative, dict):
        problems.append({'field': 'evidence_policy.non_informative_evidence', 'problem': 'required'})
    else:
        if not non_informative.get('phrases'):
            problems.append({'field': 'evidence_policy.non_informative_evidence.phrases',
                             'problem': 'required'})
        token = non_informative.get('ceiling_uncertainty')
        if token not in uncertainties:
            problems.append({'field': 'evidence_policy.non_informative_evidence.ceiling_uncertainty',
                             'value': token, 'problem': 'not_in_the_uncertainty_vocabulary'})

    reasons = set(policy.get('verification_reasons') or [])
    for name in components:
        mapped = (evidence.get('unknown_requires_verification') or {}).get(name)
        if not mapped:
            problems.append({'field': f'evidence_policy.unknown_requires_verification.{name}',
                             'problem': 'required'})
            continue
        for reason in mapped:
            if reason not in reasons:
                problems.append({'field': f'evidence_policy.unknown_requires_verification.{name}',
                                 'value': reason, 'problem': 'not_a_verification_reason'})
        anchor = (evidence.get('full_marks_anchors') or {}).get(name)
        if not isinstance(anchor, dict):
            problems.append({'field': f'evidence_policy.full_marks_anchors.{name}',
                             'problem': 'required'})
            continue
        if anchor.get('requires_uncertainty') not in uncertainties:
            problems.append({'field': f'evidence_policy.full_marks_anchors.{name}.requires_uncertainty',
                             'value': anchor.get('requires_uncertainty'), 'problem': 'not_in_the_uncertainty_vocabulary'})

    identity = policy.get('specialism_identity')
    if not isinstance(identity, dict) or not (identity.get('aliases') or {}):
        problems.append({'field': 'specialism_identity.aliases', 'problem': 'required'})
    else:
        minimum = identity.get('min_alias_chars')
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 4:
            problems.append({'field': 'specialism_identity.min_alias_chars', 'value': minimum,
                             'problem': 'must_be_an_integer_of_at_least_four'})
            minimum = 4
        for name, aliases in (identity.get('aliases') or {}).items():
            if not aliases or not isinstance(aliases, list):
                problems.append({'field': f'specialism_identity.aliases.{name}',
                                 'problem': 'must_be_a_non_empty_list'})
                continue
            for alias in aliases:
                # A short alias is how a controlled vocabulary quietly becomes a
                # substring match: `ml` or `qa` would eliminate vacancies on a
                # two-letter coincidence.
                if not isinstance(alias, str) or len(alias.strip()) < minimum:
                    problems.append({'field': f'specialism_identity.aliases.{name}',
                                     'value': alias, 'problem': 'alias_is_too_short_to_identify_a_role',
                                     'min_alias_chars': minimum})

    market_codes = (policy.get('location_policy') or {}).get('market_country_codes')
    if not isinstance(market_codes, dict) or not market_codes:
        problems.append({'field': 'location_policy.market_country_codes', 'problem': 'required'})
    else:
        for market, codes in market_codes.items():
            if not codes or not all(isinstance(c, str) and len(c) == 2 and c.isupper()
                                    for c in codes):
                problems.append({'field': f'location_policy.market_country_codes.{market}',
                                 'value': codes,
                                 'problem': 'not_a_list_of_iso_3166_alpha_2_codes'})
    if not (policy.get('reproducibility') or {}).get('evaluation_fingerprint_files'):
        problems.append({'field': 'reproducibility.evaluation_fingerprint_files',
                         'problem': 'required'})

    blockers = policy.get('hard_blockers') or {}
    requirements = blockers.get('evidence_requirements')
    if not isinstance(requirements, dict):
        problems.append({'field': 'hard_blockers.evidence_requirements', 'problem': 'required'})
        requirements = {}
    stated_by = list(requirements.get('stated_by_vocabulary') or [])
    if not stated_by:
        problems.append({'field': 'hard_blockers.evidence_requirements.stated_by_vocabulary',
                         'problem': 'required'})
    for entry in blockers.get('vocabulary') or []:
        bid = (entry or {}).get('id')
        precondition = (entry or {}).get('precondition')
        if precondition not in PRECONDITION_IDS:
            problems.append({'field': f'hard_blockers.vocabulary.{bid}.precondition',
                             'value': precondition, 'problem': 'no_implemented_factual_precondition',
                             'implemented': list(PRECONDITION_IDS)})
        allowed = (entry or {}).get('requires_stated_by') or []
        if not allowed:
            problems.append({'field': f'hard_blockers.vocabulary.{bid}.requires_stated_by',
                             'problem': 'required'})
        if entry.get('precondition') == 'excluded_level_is_the_role_level':
            # Without the accepted levels a mixed title cannot be recognised, and a
            # role the candidate is actually targeting could be eliminated by the
            # other half of its own title.
            if not entry.get('accepted_levels_from'):
                problems.append({
                    'field': f'hard_blockers.vocabulary.{bid}.accepted_levels_from',
                    'problem': 'required'})
            if list(entry.get('permitted_bases') or []) != ['canonical_title']:
                problems.append({
                    'field': f'hard_blockers.vocabulary.{bid}.permitted_bases',
                    'value': entry.get('permitted_bases'),
                    'problem': 'a_role_level_blocker_is_proved_by_the_canonical_title_only'})
        if entry.get('precondition') == 'excluded_specialism_is_the_primary_specialism':
            # A mention is not a specialism, and neither is a COUNT of mentions. An
            # automated rejection that never looks at the employer text, or that
            # proves identity by frequency, are the two defects this closed, so both
            # are refused by configuration rather than left to a future editor.
            if not entry.get('matched_value_must_appear_in_text'):
                problems.append({
                    'field': f'hard_blockers.vocabulary.{bid}.matched_value_must_appear_in_text',
                    'value': entry.get('matched_value_must_appear_in_text'),
                    'problem': 'a_primary_specialism_blocker_must_be_proved_from_employer_text'})
            for key in SPECIALISM_FREQUENCY_KEYS:
                if key in entry:
                    problems.append({
                        'field': f'hard_blockers.vocabulary.{bid}.{key}', 'value': entry.get(key),
                        'problem': 'term_frequency_can_never_prove_a_primary_specialism'})
            bases = entry.get('permitted_bases') or []
            if not bases:
                problems.append({
                    'field': f'hard_blockers.vocabulary.{bid}.permitted_bases',
                    'problem': 'required'})
            for basis in bases:
                if basis not in SPECIALISM_IDENTITY_BASES:
                    problems.append({
                        'field': f'hard_blockers.vocabulary.{bid}.permitted_bases', 'value': basis,
                        'problem': 'not_an_implemented_role_identity_basis',
                        'implemented': list(SPECIALISM_IDENTITY_BASES)})
            if not entry.get('accepted_identities_from'):
                # Without these a mixed title cannot be recognised, and a role the
                # candidate is actually targeting could be eliminated by its own
                # secondary discipline.
                problems.append({
                    'field': f'hard_blockers.vocabulary.{bid}.accepted_identities_from',
                    'problem': 'required'})
        for token in allowed:
            if token not in stated_by:
                problems.append({'field': f'hard_blockers.vocabulary.{bid}.requires_stated_by',
                                 'value': token, 'problem': 'not_in_the_stated_by_vocabulary'})
            if token in STATED_BY_NEVER_BLOCKS:
                problems.append({'field': f'hard_blockers.vocabulary.{bid}.requires_stated_by',
                                 'value': token,
                                 'problem': 'an_inferred_claim_can_never_support_a_hard_blocker'})
    return problems


def component_maxima(policy, lead_type='direct'):
    model = policy['agency_model'] if lead_type == 'agency' else policy['direct_model']
    return {name: block['max_score'] for name, block in (model.get('components') or {}).items()}


def blocker_vocabulary(policy):
    return tuple(b['id'] for b in policy['hard_blockers']['vocabulary'])


def never_blockers(policy):
    return {b['id']: b['reason'] for b in policy['hard_blockers'].get('never_blockers', [])}


def uncertainty_vocabulary(policy):
    return tuple(policy['uncertainty']['vocabulary'])


def band_for(score, policy):
    """The band one total falls in. Owned here, never taken from a proposal."""
    for band in policy['direct_model']['bands']:
        if band['min_score'] <= score <= band['max_score']:
            return band
    return {'id': 'below_threshold', 'display_name': 'Below Threshold',
            'min_score': 0, 'max_score': 0}


def applicable_blockers(candidate_config, policy):
    """Which blockers this candidate's calibration actually enables.

    A blocker whose calibration field is null cannot fire. An unknown salary floor
    is not a floor of zero, and an unknown clearance constraint is not a refusal.
    """
    config = candidate_config or {}
    enabled, disabled = [], {}
    for entry in policy['hard_blockers']['vocabulary']:
        bid = entry['id']
        path = entry.get('applies_when', '')
        value = config
        for part in path.replace('candidate_config.', '').split('.'):
            value = (value or {}).get(part) if isinstance(value, dict) else None
        # An empty list or a null threshold means the calibration never enabled it.
        active = value not in (None, '', [], {})
        if bid == 'security_clearance':
            # Explicitly false means "cannot obtain", which is what enables it.
            active = config.get('constraints', {}).get('security_clearance_obtainable') is False
        if bid in ('explicit_no_sponsorship',):
            active = bool(config.get('sponsorship', {}).get('eventual_sponsorship_required'))
        if active:
            enabled.append(bid)
        else:
            disabled[bid] = f'candidate calibration does not set {path}'
    return sorted(enabled), disabled


# --------------------------------------------------------------------------
# Evidence grounding: what a score is allowed to claim.
#
# None of this decides how good a vacancy is. It decides only whether the
# proposal has SUPPORT for what it claims, which is a different question and the
# one that was previously unasked.
# --------------------------------------------------------------------------

# floor() with a tolerance, because a ratio expressed in decimal cannot be held
# exactly in binary: 10 x 0.3 is 2.9999999999999996, and flooring that to 2 would
# silently make a documented ceiling one point tighter than the policy states.
_CEILING_EPSILON = 1e-9


def _ceiling_from_ratio(max_score, ratio):
    return int(max_score * ratio + _CEILING_EPSILON)


def _words(value):
    return [w for w in re.split(r'[^a-z0-9]+', str(value or '').lower()) if w]


def evidence_quality(evidence, policy):
    """Whether an evidence claim actually says anything about the vacancy.

    A phrase match is NOT enough on its own, and that restraint is the whole
    design. `Permanent and hybrid; salary not stated` contains a non-informative
    phrase and is still a real claim about a real advert, so treating every phrase
    hit as empty evidence would reject good evaluations. The claim counts as
    non-informative only when removing the matched phrases and ordinary filler
    leaves almost nothing behind, which is what `no vacancy evidence was available
    for this component` reduces to.

    Returns `{'non_informative': bool, 'matched_phrases': [...], 'substantive_tokens': int}`.
    """
    rules = (policy.get('evidence_policy') or {}).get('non_informative_evidence') or {}
    phrases = [str(p).strip().lower() for p in (rules.get('phrases') or []) if str(p).strip()]
    filler = {str(t).strip().lower() for t in (rules.get('filler_tokens') or [])}
    minimum = int(rules.get('min_substantive_tokens', 2))

    normalised = ' '.join(_words(evidence))
    matched = [p for p in phrases if p and p in normalised]
    if not matched:
        return {'non_informative': False, 'matched_phrases': [], 'substantive_tokens': None}
    remaining = normalised
    for phrase in sorted(matched, key=len, reverse=True):
        remaining = remaining.replace(phrase, ' ')
    tokens = [t for t in remaining.split() if len(t) > 1 and t not in filler]
    return {'non_informative': len(tokens) < minimum,
            'matched_phrases': matched[:4], 'substantive_tokens': len(tokens)}


def component_ceiling(policy, max_score, uncertainty, quality=None):
    """The most a component may score given how well its evidence supports it.

    Non-informative evidence is treated as `unknown` however the proposal labelled
    it, because a claim that establishes nothing is unknown whatever word is
    attached to it. The result is a CEILING and never a floor: nothing here lowers
    a score the proposal set honestly low, and nothing here turns silence into a
    negative fact.
    """
    rules = policy.get('evidence_policy') or {}
    ratios = rules.get('uncertainty_ceilings') or {}
    token = str(uncertainty or '').strip().lower()
    if quality and quality.get('non_informative'):
        token = (rules.get('non_informative_evidence') or {}).get('ceiling_uncertainty', token)
    ratio = ratios.get(token)
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        # An uncertainty the policy priced no ceiling for cannot be scored at all.
        return 0, token
    return _ceiling_from_ratio(max_score, ratio), token


def full_marks_problems(name, block, policy, facts, quality):
    """Why this component may not take its exact maximum.

    Full marks are the strongest claim the model can make about a component, so
    the policy names the strongest anchor available for each one and the maximum
    requires it. Returns a list of problems; empty means the anchor is satisfied.
    """
    anchor = ((policy.get('evidence_policy') or {}).get('full_marks_anchors') or {}).get(name)
    if not isinstance(anchor, dict):
        return [{'field': f'components.{name}.score', 'problem': 'no_full_marks_anchor_is_defined',
                 'hint': f'The policy defines no full-marks anchor for {name}, so its exact '
                         'maximum cannot be justified.'}]
    problems = []
    field = f'components.{name}'
    required_uncertainty = anchor.get('requires_uncertainty')
    if required_uncertainty and str(block.get('uncertainty') or '').lower() != required_uncertainty:
        problems.append({'field': f'{field}.uncertainty', 'problem': 'full_marks_require_stronger_certainty',
                         'value': block.get('uncertainty'), 'required': required_uncertainty,
                         'anchor': anchor.get('description', '')})
    if quality.get('non_informative'):
        problems.append({'field': f'{field}.evidence', 'problem': 'full_marks_require_informative_evidence',
                         'matched_phrases': quality.get('matched_phrases', [])})
    minimum = anchor.get('min_evidence_chars')
    evidence = str(block.get('evidence') or '').strip()
    if isinstance(minimum, int) and len(evidence) < minimum:
        problems.append({'field': f'{field}.evidence', 'problem': 'full_marks_require_a_substantive_claim',
                         'chars': len(evidence), 'required_chars': minimum,
                         'anchor': anchor.get('description', '')})
    missing = [f for f in (anchor.get('requires_facts') or [])
               if (facts or {}).get(f) in (None, '', [])]
    if missing:
        problems.append({'field': f'{field}.score', 'problem': 'full_marks_require_structured_facts',
                         'missing_facts': missing, 'anchor': anchor.get('description', ''),
                         'hint': 'Pass the vacancy facts on the proposal as `facts`, or score '
                                 'below the maximum.'})
    wanted_signal = anchor.get('requires_sponsorship_signal')
    if wanted_signal:
        observed = sponsorship_label(evidence)
        if observed != wanted_signal:
            problems.append({'field': f'{field}.evidence',
                             'problem': 'full_marks_require_vacancy_level_sponsorship_evidence',
                             'required_signal': wanted_signal, 'observed_signal': observed,
                             'anchor': anchor.get('description', '')})
    return problems


def _experience_minimum(text):
    """The stated experience floor in a piece of employer wording, or None."""
    from discovery_candidate import experience_minimum  # noqa: E402
    return experience_minimum(text)


# Bases this module can actually prove. A policy naming any other one is invalid,
# which is what stops a future edit reinstating frequency proof by configuration.
SPECIALISM_IDENTITY_BASES = ('canonical_title', 'explicit_role_identity_statement')
# Configuration keys that would reintroduce counting. Refused by name, because the
# defect they caused was not a wrong threshold but the wrong KIND of evidence.
SPECIALISM_FREQUENCY_KEYS = ('min_primary_mentions', 'min_mentions', 'dominance_threshold',
                             'description_dominance', 'term_frequency', 'preferred_specialisms_from')


def _specialism_role_identity(aliases, title, quotation, accepted):
    """Whether employer evidence establishes an excluded specialism as the ROLE."""
    from discovery_candidate import specialism_role_identity  # noqa: E402
    return specialism_role_identity(aliases, title, quotation, accepted)


def sponsorship_label(text):
    """The deterministic sponsorship reading of a piece of vacancy wording.

    Imported at call time from the module that OWNS that judgement, so the wording
    rules exist in exactly one place. A refusal is what blocks and an offer is what
    earns full marks, and neither may be decided by a second set of patterns kept
    here.
    """
    from discovery_candidate import sponsorship_signal  # noqa: E402
    return sponsorship_signal(text or '').get('label', 'unknown')


# --------------------------------------------------------------------------
# Hard-blocker grounding.
#
# A blocker is a DECIDED factual rejection. Membership of the controlled
# vocabulary says only that the word exists; the calibration check says only that
# this candidate enabled the rule. Neither says the vacancy actually did the
# thing. That third question is what these functions answer.
# --------------------------------------------------------------------------

def _config_value(config, dotted):
    """Walk a `candidate_config.a.b` path. Missing means the profile never set it."""
    value = config or {}
    for part in str(dotted or '').replace('candidate_config.', '').split('.'):
        value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            return None
    return value


def normalise_blocker_evidence(raw):
    """Accept the structured form, and read a bare string as an excerpt only.

    A plain string used to be the whole contract, so it is still parsed rather
    than rejected outright: it becomes an excerpt with no source and no attributed
    speaker, and then fails on the fields it is actually missing. That produces a
    message naming what to add instead of a type error.
    """
    if isinstance(raw, dict):
        return {'excerpt': str(raw.get('excerpt') or '').strip(),
                'source_url': str(raw.get('source_url') or '').strip(),
                'source_type': str(raw.get('source_type') or '').strip().lower(),
                'stated_by': str(raw.get('stated_by') or '').strip().lower(),
                'matched_value': str(raw.get('matched_value') or '').strip()}
    return {'excerpt': str(raw or '').strip(), 'source_url': '', 'source_type': '',
            'stated_by': '', 'matched_value': ''}


def blocker_evidence_problems(bid, evidence, spec, policy):
    """Whether a blocker's supporting evidence is traceable employer-stated fact."""
    rules = (policy.get('hard_blockers') or {}).get('evidence_requirements') or {}
    vocabulary = list(rules.get('stated_by_vocabulary') or [])
    minimum = int(rules.get('min_excerpt_chars', 12))
    maximum = int(rules.get('max_excerpt_chars', 400))
    allowed = list(spec.get('requires_stated_by') or [])
    problems = []
    excerpt = evidence.get('excerpt', '')
    if not excerpt:
        problems.append({'field': 'hard_blockers', 'value': bid, 'problem': 'blocker_evidence_required',
                         'hint': 'A hard blocker is a decided rejection, so it must quote what the '
                                 'vacancy actually said. An unresolved question belongs in '
                                 'verification_needed instead.'})
    elif len(excerpt) < minimum:
        problems.append({'field': 'hard_blockers', 'value': bid, 'problem': 'blocker_excerpt_too_short',
                         'chars': len(excerpt), 'required_chars': minimum})
    elif len(excerpt) > maximum:
        problems.append({'field': 'hard_blockers', 'value': bid, 'problem': 'blocker_excerpt_too_long',
                         'chars': len(excerpt), 'max_chars': maximum})
    url = evidence.get('source_url', '')
    if not url:
        problems.append({'field': 'hard_blockers', 'value': bid, 'problem': 'blocker_source_url_required',
                         'hint': 'Name the page the excerpt was read from, so the rejection is traceable.'})
    elif not url.lower().startswith(('https://', 'http://')):
        problems.append({'field': 'hard_blockers', 'value': bid, 'problem': 'blocker_source_url_not_a_web_url',
                         'value_url': url[:80]})
    source_type = evidence.get('source_type', '')
    if source_type and source_type not in SOURCE_TYPES:
        problems.append({'field': 'hard_blockers', 'value': bid, 'problem': 'blocker_source_type_not_in_vocabulary',
                         'value_source_type': source_type, 'allowed': list(SOURCE_TYPES)})
    stated_by = evidence.get('stated_by', '')
    if not stated_by:
        problems.append({'field': 'hard_blockers', 'value': bid, 'problem': 'blocker_stated_by_required',
                         'allowed': vocabulary})
    elif stated_by not in vocabulary:
        problems.append({'field': 'hard_blockers', 'value': bid, 'problem': 'blocker_stated_by_not_in_vocabulary',
                         'value_stated_by': stated_by, 'allowed': vocabulary})
    elif stated_by not in allowed:
        problems.append({
            'field': 'hard_blockers', 'value': bid, 'problem': 'blocker_evidence_not_stated_by_a_permitted_source',
            'value_stated_by': stated_by, 'allowed': allowed,
            'hint': 'A search platform\'s own classification of a role, such as a LinkedIn '
                    '`Employment type` block or a results-card label, is not the employer '
                    'speaking and can never decide an employer fact on its own.'})
    return problems


def canonical_fact(bid, field, ctx):
    """Read one blocker-supporting fact from the CANONICAL record.

    The proposal's own `facts` never decide anything here. They are compared, so a
    model that submitted `years_required_min: 5` for a vacancy the workspace
    recorded as asking for two fails VISIBLY rather than quietly winning, and they
    are never copied into the record: evaluation reads state, it does not write it.

    Returns `(value, problem)`. Three separate refusals, deliberately distinct:
    absence is not proof of the blocker, contradiction is a fault worth naming, and
    a fact that came from a search card is not the employer speaking.
    """
    stored = (ctx['canonical'].get('facts') or {}).get(field)
    proposed = (ctx['facts'] or {}).get(field)
    if stored in (None, '', []):
        return None, {
            'field': 'hard_blockers', 'value': bid,
            'problem': 'canonical_record_does_not_establish_this_fact', 'fact': field,
            'hint': f'The stored vacancy record has no {field}. A missing fact is not '
                    'evidence of the blocker: extract it from the employer page and '
                    'persist it first, or raise a verification need.'}
    if proposed not in (None, '', []) and proposed != stored:
        return None, {
            'field': 'hard_blockers', 'value': bid,
            'problem': 'proposal_fact_contradicts_the_canonical_record', 'fact': field,
            'proposed': proposed, 'canonical': stored,
            'hint': 'The evaluation and the stored vacancy disagree about what the advert '
                    'said. Resolve that against the employer page before rejecting anybody.'}
    reason = canonical_vacancy.fact_provenance_problem(ctx['canonical'], field)
    if reason:
        return None, {'field': 'hard_blockers', 'value': bid, 'problem': reason, 'fact': field,
                      'hint': 'A hard blocker needs an employer-stated fact. A search card, a '
                              'results row and an unattributed value are none of them that.'}
    return stored, None


def canonical_evidence_problems(bid, evidence, spec, policy, canonical):
    """Whether a blocker's quotation and citation survive contact with the record.

    The excerpt, the URL and the facts on a proposal are all written by the same
    model that proposed the score, so none of them is evidence of anything until it
    is checked against what this workspace actually holds. Three checks, each
    failing CLOSED:

    the record      a blocker needs a canonical vacancy to be checked against
    the text        the quotation must appear in the stored EMPLOYER description,
                    which is the body with the platform chrome already split off,
                    so a search card or a `Seniority level` block cannot satisfy it
    the citation    the source URL must be one the record itself names
    """
    rules = (policy.get('hard_blockers') or {}).get('evidence_requirements') or {}
    problems = []
    if rules.get('require_canonical_record') and not (canonical or {}).get('resolved'):
        return [{
            'field': 'hard_blockers', 'value': bid,
            'problem': 'canonical_vacancy_record_required_for_a_hard_blocker',
            'hint': 'A hard blocker is a decided rejection, so it is checked against the stored '
                    'vacancy rather than against the proposal. Evaluate with the vacancy key, or '
                    'propose the concern as a verification need.'}]
    if rules.get('require_authoritative_source_url') and not canonical_vacancy.url_is_authoritative(
            evidence.get('source_url', ''), canonical):
        problems.append({
            'field': 'hard_blockers', 'value': bid,
            'problem': 'blocker_source_url_is_not_recorded_for_this_vacancy',
            'cited': evidence.get('source_url', '')[:120],
            'authoritative': list(canonical.get('authoritative_urls') or [])[:4],
            'hint': 'Cite the vacancy page this workspace recorded, not a plausible-looking URL.'})
    if rules.get('require_quotation_in_canonical_employer_text'):
        if not canonical.get('description_available'):
            problems.append({
                'field': 'hard_blockers', 'value': bid,
                'problem': 'canonical_employer_text_is_unavailable',
                'hint': 'No employer job-description body is cached for this vacancy, so a '
                        'quotation cannot be verified and the blocker fails closed. Fetch and '
                        'cache the vacancy body, or raise a verification need for human review.'})
        elif not canonical_vacancy.quote_is_in(evidence.get('excerpt', ''),
                                               canonical.get('description_text', '')):
            problems.append({
                'field': 'hard_blockers', 'value': bid,
                'problem': 'blocker_quotation_is_not_in_the_canonical_employer_text',
                'excerpt': evidence.get('excerpt', '')[:120],
                'hint': 'The quotation must appear in the stored employer description after '
                        'whitespace and Unicode normalisation. A search card, a recommendation '
                        'panel and a platform classification are not employer text.'})
    return problems


def _precondition_experience(bid, evidence, spec, ctx):
    hard = _config_value(ctx['config'], spec.get('threshold_from'))
    if not isinstance(hard, (int, float)) or isinstance(hard, bool):
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'blocker_threshold_is_not_calibrated'}], {}
    years, problem = canonical_fact(bid, 'years_required_min', ctx)
    if problem:
        if problem['problem'] == 'canonical_record_does_not_establish_this_fact':
            problem['problem'] = 'facts_do_not_establish_a_minimum_experience_requirement'
        problem.setdefault('hint', '')
        problem['hint'] = (problem['hint'] + ' Ambiguous wording, and a preference such as '
                           '"3 years preferred", stay uncertain and raise an '
                           'experience_requirement verification need rather than becoming a '
                           'decided rejection.').strip()
        return [problem], {}
    if isinstance(years, bool) or not isinstance(years, (int, float)):
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'facts_years_required_min_is_not_a_number', 'value_years': years}], {}
    if years < hard:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'stated_minimum_is_below_the_calibrated_hard_threshold',
                 'years_required_min': years, 'hard_block_at_or_above_years': hard,
                 'hint': f'The advert asks for {years} years and the calibration only drops at '
                         f'{hard}. That is a seniority_experience score, not a rejection.'}], {}
    if spec.get('quotation_must_state_the_same_minimum'):
        # The quotation is already known to appear in the employer text. It must
        # also SAY the thing: a preference, a ceiling, or a number that happens to
        # describe how old a system is are none of them a stated floor.
        reading = _experience_minimum(evidence.get('excerpt', ''))
        if reading['years'] is None:
            return [{'field': 'hard_blockers', 'value': bid,
                     'problem': 'quotation_does_not_state_a_hard_minimum',
                     'reading': reading['reason'],
                     'hint': 'The quoted sentence has to state a floor. A preference, an "up to" '
                             'ceiling, or a number not tied to experience cannot become one.'}], {}
        if reading['years'] != years:
            return [{'field': 'hard_blockers', 'value': bid,
                     'problem': 'quotation_states_a_different_minimum_from_the_canonical_fact',
                     'quoted_years': reading['years'], 'canonical_years': years}], {}
    return [], {'years_required_min': years, 'hard_block_at_or_above_years': hard}


def _precondition_no_sponsorship(bid, evidence, spec, ctx):
    excerpt = evidence.get('excerpt', '')
    label = sponsorship_label(excerpt)
    if label != 'blocked':
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'excerpt_does_not_refuse_sponsorship', 'observed_signal': label,
                 'hint': 'This blocker needs vacancy wording that actually refuses sponsorship or '
                         'requires unrestricted UK work rights. Employer silence, an unknown '
                         'status, absence from a register snapshot and a weak sponsorship history '
                         'are none of them a refusal.'}], {}
    return [], {'sponsorship_signal': 'blocked'}


def _precondition_salary_floor(bid, evidence, spec, ctx):
    floor = _config_value(ctx['config'], spec.get('threshold_from'))
    if not isinstance(floor, (int, float)) or isinstance(floor, bool):
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'no_salary_floor_is_configured'}], {}
    facts = ctx['canonical'].get('facts') or {}
    # A range is read in the direction that keeps the candidate in play, so the
    # top of the band is the bound the employer has said it will pay.
    bound_field = 'salary_max' if facts.get('salary_max') is not None else 'salary_min'
    bound, problem = canonical_fact(bid, bound_field, ctx)
    if problem:
        if problem['problem'] == 'canonical_record_does_not_establish_this_fact':
            problem['problem'] = 'facts_do_not_establish_a_salary'
            problem['hint'] = ('An unstated or ambiguous salary is missing information, never a '
                               'low salary. Persist the figure as facts.salary_min/salary_max, or '
                               'raise a salary verification need.')
        return [problem], {}
    if isinstance(bound, bool) or not isinstance(bound, (int, float)):
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'facts_salary_is_not_a_number', 'value_salary': bound}], {}
    currency = str(facts.get('salary_currency') or '').upper()
    configured = str((ctx['config'] or {}).get('salary', {}).get('currency') or '').upper()
    if currency and configured and currency != configured:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'salary_currency_does_not_match_the_calibration',
                 'value_currency': currency, 'configured_currency': configured}], {}
    if bound >= floor:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'stated_salary_is_not_below_the_configured_floor',
                 bound_field: bound, 'hard_floor': floor}], {}
    return [], {bound_field: bound, 'hard_floor': floor}


def _independent_arrangement(text):
    """Whether employer wording NAMES a non-employment engagement."""
    from discovery_candidate import names_independent_contracting  # noqa: E402
    return names_independent_contracting(text)


def _precondition_employment_type(bid, evidence, spec, ctx):
    matches = [str(t).lower() for t in (spec.get('matches_employment_types') or [bid])]
    excluded = [str(t).lower() for t in
                ((ctx['config'] or {}).get('employment', {}).get('excluded_types') or [])]
    value, problem = canonical_fact(bid, 'employment_type', ctx)
    stated = str(value or '').strip().lower()
    if problem or not stated or stated == 'unknown':
        if problem and problem['problem'] != 'canonical_record_does_not_establish_this_fact':
            return [problem], {}
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'facts_do_not_establish_the_employment_type',
                 'hint': 'Persist the EMPLOYER\'s stated employment type as '
                         'facts.employment_type. A platform classification such as LinkedIn\'s '
                         '`Employment type` block is that platform\'s reading of the role, not '
                         'the employer\'s words, and can never fire this blocker on its own.'}], {}
    if stated not in matches:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'stated_employment_type_does_not_match_this_blocker',
                 'employment_type': stated, 'blocker_matches': matches}], {}
    if stated not in excluded:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'employment_type_is_not_excluded_by_the_calibration',
                 'employment_type': stated, 'excluded_types': excluded}], {}
    if spec.get('requires_independent_arrangement_wording'):
        # The structured fact says `contract`; the ADVERT has to say what kind.
        # `Software Engineer, 12-month contract` is a fixed-term PAYE job as often
        # as a contractor one, and a fact recorded before that distinction existed
        # must not eliminate a vacancy on wording that never established it.
        if not _independent_arrangement(evidence.get('excerpt', '')):
            return [{'field': 'hard_blockers', 'value': bid,
                     'problem': 'quotation_does_not_name_an_independent_arrangement',
                     'employment_type': stated,
                     'hint': 'The quoted employer wording has to name the arrangement: a day '
                             'rate, an IR35 status, an umbrella arrangement, freelance or '
                             'self-employed work, or labour supplied to a third party. The bare '
                             'word `contract` is compatible with direct fixed-term employment, '
                             'so it raises an employment_type verification need instead.'}], {}
    return [], {'employment_type': stated}


def _precondition_excluded_value(bid, evidence, spec, ctx):
    values = _config_value(ctx['config'], spec.get('matched_value_from')) or []
    matched = evidence.get('matched_value', '')
    known = {str(v).strip().lower() for v in values if str(v).strip()}
    if not known:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'the_calibration_lists_no_excluded_value_for_this_blocker'}], {}
    if not matched:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'blocker_must_name_the_calibration_value_it_matched',
                 'allowed': sorted(known),
                 'hint': 'Set evidence.matched_value to the configured exclusion this vacancy '
                         'actually meets, so the rejection names a rule the candidate set rather '
                         'than a judgement nobody can audit.'}], {}
    if matched.lower() not in known:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'matched_value_is_not_in_the_candidate_calibration',
                 'value_matched': matched, 'allowed': sorted(known)}], {}
    if spec.get('matched_value_must_appear_in_text'):
        haystack = set(_words(evidence.get('excerpt', ''))) | set(_words(ctx.get('title', '')))
        if not set(_words(matched)) <= haystack:
            return [{'field': 'hard_blockers', 'value': bid,
                     'problem': 'matched_value_does_not_appear_in_the_vacancy_text',
                     'value_matched': matched,
                     'hint': 'The excerpt or the title has to contain the level being rejected. '
                             'A generic title is never on its own a seniority signal.'}], {}
    return [], {'matched_value': matched.lower()}


def _precondition_role_level(bid, evidence, spec, ctx):
    """The excluded LEVEL has to be the role's own, stated in the employer's title.

    The earlier rule accepted the level word appearing in the excerpt OR the title,
    which meant a sourced sentence naming somebody else settled it: "you will report
    to the Senior Engineering Manager", "our Lead Architect owns the roadmap", "there
    is a path towards a Principal role" are all employer-stated, all quotable, and
    none of them is the level of the vacancy.

    So the title decides, and a title naming an accepted level alongside an excluded
    one is MIXED. `Mid to Senior Software Engineer` is a role to look at, and a role
    is never rejected merely for calling itself mid-level.
    """
    values = _config_value(ctx['config'], spec.get('matched_value_from')) or []
    known = {str(v).strip().lower() for v in values if str(v).strip()}
    matched = evidence.get('matched_value', '')
    if not known:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'the_calibration_lists_no_excluded_level_for_this_blocker'}], {}
    if not matched:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'blocker_must_name_the_calibration_value_it_matched',
                 'allowed': sorted(known)}], {}
    if matched.lower() not in known:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'matched_value_is_not_in_the_candidate_calibration',
                 'value_matched': matched, 'allowed': sorted(known)}], {}

    title = ctx['canonical'].get('title') or ctx.get('title') or ''
    accepted = []
    for path in (spec.get('accepted_levels_from') or []):
        accepted.extend(str(v) for v in (_config_value(ctx['config'], path) or []))
    level = _specialism_role_identity([matched], title, '', accepted)
    if level['basis'] == 'mixed_title':
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'the_canonical_title_names_both_an_accepted_and_an_excluded_level',
                 'value_matched': matched, 'accepted_in_title': level['accepted_in_title'],
                 'hint': 'A mixed-level title is reviewable rather than rejected. Raise a '
                         'verification need instead.'}], {}
    if level['basis'] != 'canonical_title':
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'the_canonical_title_does_not_state_this_level',
                 'value_matched': matched, 'title': title[:80],
                 'hint': 'A reporting line, a stakeholder\'s job title, a team description and a '
                         'note about future progression all contain level words without being '
                         'the level of the vacancy. If the responsibilities look over-levelled '
                         'but the title does not say so, raise a verification need.'}], {}
    return [], {'matched_value': matched.lower(), 'level_basis': 'canonical_title'}


def _precondition_specialism_identity(bid, evidence, spec, ctx):
    """The excluded specialism must be the role's own IDENTITY, stated by the employer.

    Frequency is deliberately absent. Counting mentions measured subject matter and
    called it identity, so a backend advert that discussed data, machine learning or
    a React front end at length could be eliminated for doing its job. Only two
    things now qualify, and both are the employer saying what the ROLE IS: its own
    title naming an excluded identity from the controlled alias vocabulary, or a
    quoted sentence that states the vacancy IS that role.

    A title naming an accepted identity alongside an excluded one is MIXED, and a
    mixed role is reviewable rather than rejected.
    """
    values = _config_value(ctx['config'], spec.get('matched_value_from')) or []
    known = {str(v).strip().lower() for v in values if str(v).strip()}
    matched = evidence.get('matched_value', '')
    if not known:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'the_calibration_lists_no_excluded_value_for_this_blocker'}], {}
    if not matched:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'blocker_must_name_the_calibration_value_it_matched',
                 'allowed': sorted(known)}], {}
    if matched.lower() not in known:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'matched_value_is_not_in_the_candidate_calibration',
                 'value_matched': matched, 'allowed': sorted(known)}], {}

    catalogue = (ctx['policy'].get('specialism_identity') or {}).get('aliases') or {}
    aliases = catalogue.get(matched.lower()) or []
    if not aliases:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'no_controlled_role_identity_vocabulary_for_this_specialism',
                 'value_matched': matched,
                 'hint': 'This exclusion has no controlled identity aliases, so no title or '
                         'statement can prove it automatically. Raise a verification need for '
                         'human review.'}], {}

    accepted = []
    for path in (spec.get('accepted_identities_from') or []):
        accepted.extend(str(v) for v in (_config_value(ctx['config'], path) or []))
    identity = _specialism_role_identity(
        aliases, ctx['canonical'].get('title') or ctx.get('title') or '',
        evidence.get('excerpt', ''), accepted)

    if identity['basis'] == 'mixed_title':
        return [{
            'field': 'hard_blockers', 'value': bid,
            'problem': 'the_canonical_title_names_both_an_accepted_and_an_excluded_identity',
            'value_matched': matched, 'matched_alias': identity['matched_alias'],
            'accepted_in_title': identity['accepted_in_title'],
            'hint': 'A mixed or ambiguous role stays eligible and reviewable. Raise a '
                    'verification need rather than eliminating it automatically.'}], {}
    if not identity['established']:
        return [{
            'field': 'hard_blockers', 'value': bid,
            'problem': 'canonical_evidence_does_not_establish_the_role_identity',
            'value_matched': matched, 'permitted_bases': list(SPECIALISM_IDENTITY_BASES),
            'hint': 'Repeated terminology proves subject matter, not role identity. A '
                    'technology, a responsibility, a department, a stakeholder team, a '
                    'desirable skill and an adjacent discipline are none of them the role. '
                    'Either the employer title names the excluded identity, or the quoted '
                    'sentence says the vacancy IS that role. Otherwise raise a verification '
                    'need for human review.'}], {}

    verified = {'matched_value': matched.lower(), 'identity_basis': identity['basis'],
                'matched_alias': identity['matched_alias']}
    if identity['statement']:
        verified['identity_statement'] = identity['statement']
    return [], verified


def _precondition_primary_language(bid, evidence, spec, ctx):
    from discovery_candidate import candidate_ecosystems, named_language_ecosystems  # noqa: E402
    owned = candidate_ecosystems(ctx['config'])
    if not owned:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'the_calibration_names_no_primary_language'}], {}
    named = named_language_ecosystems(
        f"{evidence.get('excerpt', '')} {ctx.get('title', '')}")
    if not named:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'vacancy_text_names_no_language_ecosystem',
                 'hint': 'Quote the wording that names the stack. A role is not rejected for a '
                         'language it never mentioned.'}], {}
    if named & owned:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'vacancy_text_names_the_candidate_s_own_stack',
                 'named': sorted(named), 'candidate_ecosystems': sorted(owned)}], {}
    return [], {'foreign_ecosystems': sorted(named - owned),
                'candidate_ecosystems': sorted(owned)}


def _precondition_security_clearance(bid, evidence, spec, ctx):
    from discovery_candidate import names_security_clearance  # noqa: E402
    if not names_security_clearance(f"{evidence.get('excerpt', '')} {ctx.get('title', '')}"):
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'vacancy_text_states_no_security_clearance_requirement'}], {}
    return [], {'clearance_stated': True}


def _precondition_country_outside_market(bid, evidence, spec, ctx):
    """A hard blocker must PROVE its condition, so this one needs a country.

    The earlier version could only refuse the damaging direction: it rejected a
    blocker on a vacancy that NAMED the accepted market, and let anything else
    through. That is not proof. An unfamiliar city, a remote-working line, an
    aggregator location and a model's own reading are all incapable of establishing
    that a role sits outside the market, and this workspace has no gazetteer that
    could settle it from a place name.

    So the blocker now requires a controlled country code recorded as an
    employer-grade fact on the canonical record, compared with the codes the policy
    says the market accepts. Everything else is a verification need.
    """
    market = _config_value(ctx['config'], spec.get('market_from'))
    if not market:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'no_market_is_configured'}], {}
    accepted = ((ctx['policy'].get('location_policy') or {}).get('market_country_codes')
                or {}).get(str(market).strip().lower())
    if not accepted:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'policy_defines_no_country_codes_for_this_market',
                 'market': market}], {}
    country, problem = canonical_fact(bid, 'country', ctx)
    if problem:
        if problem['problem'] == 'canonical_record_does_not_establish_this_fact':
            problem['problem'] = 'canonical_record_states_no_country_for_this_vacancy'
            problem['hint'] = (
                'An unfamiliar city, remote wording, an aggregator location line and a missing '
                'location cannot establish that a role is outside the market. Persist an '
                'employer-stated ISO country code as facts.country, or raise a verification '
                'need for human review.')
        return [problem], {}
    code = str(country).strip().upper()
    if code in {str(c).strip().upper() for c in accepted}:
        return [{'field': 'hard_blockers', 'value': bid,
                 'problem': 'the_canonical_country_is_inside_the_accepted_market',
                 'country': code, 'market': market,
                 'hint': 'A location inside the accepted market carries no penalty at all, and a '
                         'non-preferred city inside it is a tie-breaker rather than a rejection.'}], {}
    return [], {'country': code, 'market': market, 'accepted_country_codes': list(accepted)}


PRECONDITIONS = {
    'structured_minimum_years_at_or_above_hard_maximum': _precondition_experience,
    'vacancy_level_sponsorship_refusal': _precondition_no_sponsorship,
    'structured_salary_below_configured_floor': _precondition_salary_floor,
    'employer_stated_excluded_employment_type': _precondition_employment_type,
    'excluded_value_named': _precondition_excluded_value,
    'excluded_level_is_the_role_level': _precondition_role_level,
    'excluded_specialism_is_the_primary_specialism': _precondition_specialism_identity,
    'foreign_primary_language_named': _precondition_primary_language,
    'security_clearance_required': _precondition_security_clearance,
    'canonical_country_outside_market': _precondition_country_outside_market,
}
assert set(PRECONDITIONS) == set(PRECONDITION_IDS)


def blocker_specs(policy):
    return {entry['id']: entry for entry in policy['hard_blockers']['vocabulary']}


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def _verification_result(proposal, identity, policy, errors):
    """The machine record of an UNSCORED verification lead.

    Structured and readable like any other evaluation, but carrying no score, no
    denominator and no band, because the decision it would express is not yet
    knowable. `eligible` is None rather than True or False for the same reason:
    the Direct decision has not been made, so claiming either would be a false
    statement about a vacancy nobody has been able to assess.

    A component proposal is REFUSED rather than ignored, because sending one means
    the caller believed it was scoring this lead, and silently discarding numbers
    somebody intended to store would hide that misunderstanding.
    """
    if proposal.get('components'):
        errors.append({
            'field': 'components', 'problem': 'verification_lead_is_not_scored',
            'hint': 'A Verification Lead exists because a decision-critical fact is '
                    'unresolved, so it carries no score, band or eligibility. Score it '
                    'as `direct` once the gate is resolved, or leave `components` out.'})
    allowed_reasons = policy.get('verification_reasons', [])
    verification = []
    for entry in proposal.get('verification_needed', []) or []:
        row = entry if isinstance(entry, dict) else {'reason': entry}
        reason = str(row.get('reason') or '').strip().lower()
        if reason not in allowed_reasons:
            errors.append({'field': 'verification_needed', 'value': reason,
                           'problem': 'not_in_vocabulary', 'allowed': list(allowed_reasons)})
            continue
        verification.append({'reason': reason, 'detail': str(row.get('detail') or '').strip()})
    if not verification:
        errors.append({
            'field': 'verification_needed', 'problem': 'required',
            'hint': 'A Verification Lead must name the unresolved gate that is holding '
                    f'it back. Allowed reasons: {", ".join(allowed_reasons)}'})
    if proposal.get('hard_blockers'):
        errors.append({
            'field': 'hard_blockers', 'problem': 'verification_lead_is_not_blocked',
            'hint': 'A hard blocker is a decided rejection; an unresolved gate is not. '
                    'Record the gate in verification_needed instead.'})
    if errors:
        return None, errors
    return {
        'schema_version': SCHEMA_VERSION,
        **identity,
        'lead_type': 'verification',
        'components': {},
        'evaluation_fingerprints': evaluation_fingerprints(),
        'total_score': None,
        'max_score': None,
        'score_display': '',
        'score_band': None,
        'band_display': 'Verification Lead - unscored while a decision-critical gate '
                        'is unresolved',
        'eligible': None,
        'provisional': False,
        'hard_blockers': [],
        'verification_needed': verification,
        'warnings': [str(w).strip() for w in (proposal.get('warnings') or []) if str(w).strip()],
        'computed_by': 'tools/match_evaluation.py',
        'note': 'A Verification Lead carries no score, denominator or band. It is not a '
                'low score and not a rejection: the decision cannot honestly be made '
                'until the named gate is resolved, at which point it is reclassified '
                'and scored as a Direct or an Agency lead.',
    }, []


def evaluate(proposal, policy=None, candidate_config=None, strict_blockers=True,
             canonical=None):
    """Validate a proposed evaluation and compute its authoritative result.

    Returns `(result, errors)`. A non-empty `errors` means the proposal is not fit
    to be recorded: it is reported rather than repaired, because quietly fixing an
    out-of-range component would hide a model that has misread the scoring model.

    `canonical` is the read-only view from `canonical_vacancy.resolve()`. Hard
    blockers REQUIRE it, because a decided rejection has to be checkable against
    the stored vacancy rather than against the proposal that asserts it. Scoring
    does not require it, since a score is decision support rather than a rejection;
    where it is absent the result says so in `canonical_grounding`, and a component
    can then only reach its exact maximum on facts nobody has corroborated.
    """
    canonical = canonical if isinstance(canonical, dict) else {}
    policy = policy or load_policy()
    errors = []
    if not isinstance(proposal, dict):
        return None, [{'field': '_root', 'problem': 'not_an_object',
                       'value': type(proposal).__name__}]

    lead_type = str(proposal.get('lead_type') or 'direct').strip().lower()
    if lead_type not in LEAD_TYPES:
        errors.append({'field': 'lead_type', 'value': proposal.get('lead_type'),
                       'problem': 'not_in_vocabulary', 'allowed': list(LEAD_TYPES)})
        lead_type = 'direct'

    maxima = component_maxima(policy, lead_type)
    uncertainties = uncertainty_vocabulary(policy)
    evidence_policy = policy.get('evidence_policy', {})
    min_evidence = int(evidence_policy.get('min_evidence_chars', 0))
    max_evidence = int(evidence_policy.get('max_evidence_chars', 400))

    identity = {field: str(proposal.get(field) or '').strip()
                for field in ('company', 'title', 'url', 'location', 'key')}
    if not identity['company'] or not identity['title']:
        errors.append({'field': 'company/title', 'problem': 'required',
                       'hint': 'An evaluation must say which vacancy it evaluated.'})

    # A VERIFICATION LEAD IS UNSCORED, BY DEFINITION.
    #
    # It exists precisely because a decision-critical fact about the employer or the
    # vacancy is unresolved, so there is nothing to score honestly yet. The evaluator
    # previously accepted `lead_type: verification` and then applied the full Direct
    # 100-point model to it, demanding all five components. That is a contradiction:
    # it could only be satisfied by inventing the very numbers the lead type exists
    # to withhold, and a fabricated Direct score is exactly what the category was
    # created to prevent.
    #
    # There is no third numeric scale here. Direct is /100, Agency is a provisional
    # /75, and Verification carries no number at all until the gate is resolved and
    # the lead is reclassified.
    if lead_type == 'verification':
        return _verification_result(proposal, identity, policy, errors)

    # Verification needs are parsed FIRST, because a component that establishes
    # nothing is only allowed to be scored at all when the gap is visible as a
    # verification action, and that can only be checked once they are known.
    verification = []
    allowed_reasons = policy.get('verification_reasons', [])
    for entry in proposal.get('verification_needed', []) or []:
        row = entry if isinstance(entry, dict) else {'reason': entry}
        reason = str(row.get('reason') or '').strip().lower()
        if reason not in allowed_reasons:
            errors.append({'field': 'verification_needed', 'value': reason,
                           'problem': 'not_in_vocabulary', 'allowed': list(allowed_reasons)})
            continue
        verification.append({'reason': reason, 'detail': str(row.get('detail') or '').strip()})
    raised = {row['reason'] for row in verification}

    # STRUCTURED VACANCY FACTS. Optional, because most evaluations need none, and
    # validated against the same vocabulary the state boundary persists, so the
    # evaluator and the record can never disagree about what a fact is. Facts are
    # read ONLY to decide whether a claim is supported; they never contribute a
    # point to any score.
    facts = proposal.get('facts')
    if facts is None:
        facts = {}
    elif not isinstance(facts, dict):
        errors.append({'field': 'facts', 'value': type(facts).__name__,
                       'problem': 'required_object'})
        facts = {}
    else:
        fact_faults = facts_problems(facts)
        if fact_faults:
            errors.append({'field': 'facts', 'problem': 'invalid_vacancy_facts',
                           'problems': fact_faults[:5],
                           'hint': 'Facts hold what the vacancy actually stated. Leave a fact out '
                                   'rather than filling it with a guess.'})
            facts = {}
        else:
            facts = normalise_facts(facts)
    facts_used = set()

    proposed_components = proposal.get('components')
    if not isinstance(proposed_components, dict):
        errors.append({'field': 'components', 'problem': 'required_object'})
        proposed_components = {}

    # A component the model invented, or one the policy defines that the model
    # skipped, both mean the evaluation is not the model this policy describes.
    for name in sorted(set(proposed_components) - set(maxima)):
        errors.append({'field': f'components.{name}', 'problem': 'not_a_policy_component',
                       'allowed': sorted(maxima)})
    for name in sorted(set(maxima) - set(proposed_components)):
        errors.append({'field': f'components.{name}', 'problem': 'required_component_missing'})

    unknown_verification = evidence_policy.get('unknown_requires_verification') or {}
    components, total = {}, 0
    for name, allowed_max in sorted(maxima.items()):
        block = proposed_components.get(name)
        if not isinstance(block, dict):
            continue
        score = block.get('score')
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            errors.append({'field': f'components.{name}.score', 'value': score,
                           'problem': 'not_a_number'})
            continue
        if score != int(score):
            errors.append({'field': f'components.{name}.score', 'value': score,
                           'problem': 'must_be_a_whole_number'})
            continue
        score = int(score)
        if score < 0 or score > allowed_max:
            errors.append({'field': f'components.{name}.score', 'value': score,
                           'problem': 'outside_allowed_range', 'max_score': allowed_max})
            continue
        # A proposal may state the maximum it believed it was scoring against. If it
        # disagrees with policy the proposal was built on the wrong model.
        stated_max = block.get('max_score')
        if stated_max is not None and stated_max != allowed_max:
            errors.append({'field': f'components.{name}.max_score', 'value': stated_max,
                           'problem': 'disagrees_with_policy', 'policy_max_score': allowed_max})
            continue
        evidence = str(block.get('evidence') or '').strip()
        if len(evidence) < min_evidence:
            errors.append({'field': f'components.{name}.evidence',
                           'problem': 'evidence_required',
                           'hint': 'A component cannot receive points without a short '
                                   'claim naming what the vacancy actually said.'})
            continue
        if len(evidence) > max_evidence:
            errors.append({'field': f'components.{name}.evidence',
                           'problem': 'evidence_too_long', 'max_chars': max_evidence})
            continue
        uncertainty = str(block.get('uncertainty') or '').strip().lower()
        if uncertainty not in uncertainties:
            errors.append({'field': f'components.{name}.uncertainty',
                           'value': block.get('uncertainty'), 'problem': 'not_in_vocabulary',
                           'allowed': list(uncertainties)})
            continue

        quality = evidence_quality(evidence, policy)
        ceiling, effective = component_ceiling(policy, allowed_max, uncertainty, quality)
        if score > ceiling:
            errors.append({
                'field': f'components.{name}.score', 'value': score,
                'problem': 'above_the_uncertainty_ceiling', 'ceiling': ceiling,
                'max_score': allowed_max, 'declared_uncertainty': uncertainty,
                'effective_uncertainty': effective,
                'matched_non_informative_phrases': quality.get('matched_phrases', []),
                'hint': ('Evidence that says only that nothing is known cannot support this '
                         'score.' if quality.get('non_informative') else
                         f'{uncertainty} evidence is capped at {ceiling} of {allowed_max}. '
                         'Score it inside the ceiling, or supply evidence that establishes '
                         'the fact.')})
            continue
        if score == allowed_max:
            # Anchors read the CANONICAL facts wherever a record was resolved, so
            # full marks cannot be bought with facts the model wrote itself.
            anchor_facts = canonical.get('facts') if canonical.get('resolved') else facts
            anchor_faults = full_marks_problems(name, block, policy, anchor_facts, quality)
            if anchor_faults:
                errors.extend(anchor_faults)
                continue
            anchor = (evidence_policy.get('full_marks_anchors') or {}).get(name) or {}
            facts_used.update(anchor.get('requires_facts') or [])
        if effective == 'unknown':
            wanted = unknown_verification.get(name) or []
            if wanted and not (raised & set(wanted)):
                errors.append({
                    'field': f'components.{name}.uncertainty', 'value': effective,
                    'problem': 'unknown_evidence_must_raise_a_verification_need',
                    'accepted_reasons': list(wanted),
                    'hint': 'A component that establishes nothing may still be scored '
                            'conservatively, but the gap has to stay visible as a verification '
                            'action rather than disappearing into a number.'})
                continue

        components[name] = {'score': score, 'max_score': allowed_max, 'ceiling': ceiling,
                            'evidence': evidence, 'uncertainty': uncertainty}
        total += score

    blockers, applicable, disabled = [], None, {}
    if candidate_config is not None:
        applicable, disabled = applicable_blockers(candidate_config, policy)
    vocabulary = blocker_vocabulary(policy)
    never = never_blockers(policy)
    specs = blocker_specs(policy)
    for entry in proposal.get('hard_blockers', []) or []:
        row = entry if isinstance(entry, dict) else {'id': entry}
        bid = str(row.get('id') or '').strip().lower()
        if bid in never:
            errors.append({'field': 'hard_blockers', 'value': bid,
                           'problem': 'never_a_blocker', 'reason': never[bid]})
            continue
        if bid not in vocabulary:
            errors.append({'field': 'hard_blockers', 'value': bid,
                           'problem': 'not_in_vocabulary', 'allowed': list(vocabulary)})
            continue
        if strict_blockers and applicable is not None and bid not in applicable:
            # The candidate's calibration never enabled this blocker, so applying it
            # would reject a vacancy on a rule this candidate did not ask for.
            errors.append({'field': 'hard_blockers', 'value': bid,
                           'problem': 'not_enabled_by_candidate_calibration',
                           'reason': disabled.get(bid, '')})
            continue
        if candidate_config is None:
            # Every precondition is measured against the private calibration, so
            # without one there is nothing to decide a rejection against. Refusing
            # is the fail-closed answer; believing the proposal would not be.
            errors.append({
                'field': 'hard_blockers', 'value': bid,
                'problem': 'blocker_preconditions_require_the_candidate_calibration',
                'hint': 'A hard blocker cannot be verified without the calibration that '
                        'defines it. Run with the candidate config, or propose the concern '
                        'as a verification need.'})
            continue
        spec = specs[bid]
        blocker_evidence = normalise_blocker_evidence(row.get('evidence'))
        evidence_faults = blocker_evidence_problems(bid, blocker_evidence, spec, policy)
        evidence_faults.extend(
            canonical_evidence_problems(bid, blocker_evidence, spec, policy, canonical))
        if evidence_faults:
            errors.extend(evidence_faults)
            continue
        precondition = spec.get('precondition')
        checker = PRECONDITIONS.get(precondition)
        if checker is None:
            errors.append({'field': 'hard_blockers', 'value': bid,
                           'problem': 'no_implemented_factual_precondition',
                           'precondition': precondition})
            continue
        context = {'config': candidate_config, 'facts': facts, 'policy': policy,
                   'canonical': canonical, 'title': identity['title'],
                   'location': identity['location']}
        faults, verified = checker(bid, blocker_evidence, spec, context)
        if faults:
            errors.extend(faults)
            continue
        facts_used.update(k for k in verified if k in FACT_FIELDS)
        blockers.append({'id': bid, 'evidence': blocker_evidence,
                         'precondition': precondition, 'verified_against': verified})

    if errors:
        return None, errors

    model = policy['agency_model'] if lead_type == 'agency' else policy['direct_model']
    denominator = model['total_max']
    profile = {token: 0 for token in uncertainties}
    for block in components.values():
        profile[block['uncertainty']] = profile.get(block['uncertainty'], 0) + 1
    result = {
        'schema_version': SCHEMA_VERSION,
        **identity,
        'lead_type': lead_type,
        'components': components,
        # Computed here, never read from the proposal.
        'total_score': total,
        'max_score': denominator,
        'score_display': f'{total}/{denominator}',
        'uncertainty_summary': {
            'counts': profile,
            # What this evaluation COULD have scored on the evidence it actually
            # had. A total sitting well under its own ceiling is a weak vacancy; a
            # low ceiling is weak evidence, and the two are not the same finding.
            'max_possible_score': sum(b['ceiling'] for b in components.values()),
            'capped_components': sorted(n for n, b in components.items()
                                        if b['ceiling'] < b['max_score']),
        },
        # What the CALCULATION consumed, read from the canonical record wherever one
        # was resolved, so an anchor or a precondition can be re-checked from the
        # stored object alone without trusting the proposal that produced it.
        'facts_used': {f: (canonical.get('facts') or facts).get(f)
                       for f in sorted(facts_used)
                       if (canonical.get('facts') or facts).get(f) not in (None, '', [])},
        'canonical_grounding': bool(canonical.get('resolved')),
        'canonical_key': str(canonical.get('key') or ''),
        'evaluation_fingerprints': evaluation_fingerprints(),
        'hard_blockers': blockers,
        'verification_needed': verification,
        'warnings': [str(w).strip() for w in (proposal.get('warnings') or []) if str(w).strip()],
        'computed_by': 'tools/match_evaluation.py',
    }

    if lead_type == 'agency':
        # A provisional score on a different scale. It has no band, because the
        # bands are defined for the 100-point model and borrowing them would imply
        # a comparability that does not exist.
        result.update({
            'provisional': True,
            'excluded_components': list(policy['agency_model'].get('excluded_components', [])),
            'score_band': None,
            'band_display': f'Provisional {total}/{denominator} excl. sponsorship',
            'eligible': not blockers,
            'note': ('Agency lead: the employing client is not identified, so sponsorship '
                     'cannot be assessed and its 25 points are EXCLUDED rather than scored '
                     'zero. This score is out of 75 and must never be shown against 100.'),
        })
    else:
        band = band_for(total, policy)
        result.update({
            'provisional': False,
            'score_band': band['id'],
            'band_display': band['display_name'],
            # A blocker overrides the total. The component scores stay intact so the
            # diagnostic picture survives for when the evidence changes.
            'eligible': not blockers,
            'note': ('A hard blocker overrides the numeric total and sets eligible false '
                     'without destroying the component scores.' if blockers else
                     'Verification needed does not change the score, the band or the lead '
                     'type.' if verification else ''),
        })
    return result, []


# --------------------------------------------------------------------------
# Re-validating a STORED evaluation.
#
# The state boundary re-validates rather than trusts, and it must not have to
# reimplement any of the rules above to do it, or the two definitions drift and
# the weaker one wins. So the grounding rules live here, once, and the boundary
# calls them.
#
# Everything re-checked here is SELF-CONTAINED in the evaluation plus the
# publishable policy. That is deliberate: the private calibration and the
# vacancy's facts are the evaluator's inputs, and the boundary should not need to
# read either to tell whether a stored object is internally coherent. What the
# evaluator therefore records is not its conclusion alone but the comparison it
# made, so the comparison itself can be re-run.
# --------------------------------------------------------------------------

def _stored_recheck_years(verified, evidence):
    years, hard = (verified.get('years_required_min'),
                   verified.get('hard_block_at_or_above_years'))
    if not isinstance(years, (int, float)) or not isinstance(hard, (int, float)):
        return 'blocker_did_not_record_the_experience_comparison'
    if years < hard:
        return 'stated_minimum_is_below_the_calibrated_hard_threshold'
    return None


def _stored_recheck_salary(verified, evidence):
    floor = verified.get('hard_floor')
    bound = verified.get('salary_max', verified.get('salary_min'))
    if not isinstance(floor, (int, float)) or not isinstance(bound, (int, float)):
        return 'blocker_did_not_record_the_salary_comparison'
    if bound >= floor:
        return 'stated_salary_is_not_below_the_configured_floor'
    return None


def _stored_recheck_sponsorship(verified, evidence):
    if sponsorship_label(evidence.get('excerpt', '')) != 'blocked':
        return 'excerpt_does_not_refuse_sponsorship'
    return None


def _stored_recheck_specialism(verified, evidence):
    basis = verified.get('identity_basis')
    if basis not in SPECIALISM_IDENTITY_BASES:
        return 'blocker_did_not_record_a_permitted_role_identity_basis'
    if basis == 'explicit_role_identity_statement' and not verified.get('identity_statement'):
        return 'an_explicit_identity_basis_must_record_the_statement_it_matched'
    return None


def _stored_recheck_country(verified, evidence):
    accepted = {str(c).upper() for c in (verified.get('accepted_country_codes') or [])}
    country = str(verified.get('country') or '').upper()
    if not country or not accepted:
        return 'blocker_did_not_record_the_country_comparison'
    if country in accepted:
        return 'the_canonical_country_is_inside_the_accepted_market'
    return None


def _stored_recheck_level(verified, evidence):
    if verified.get('level_basis') != 'canonical_title':
        return 'blocker_did_not_record_a_canonical_title_level_basis'
    return None


_STORED_RECHECKS = {
    'excluded_level_is_the_role_level': _stored_recheck_level,
    'structured_minimum_years_at_or_above_hard_maximum': _stored_recheck_years,
    'structured_salary_below_configured_floor': _stored_recheck_salary,
    'vacancy_level_sponsorship_refusal': _stored_recheck_sponsorship,
    'excluded_specialism_is_the_primary_specialism': _stored_recheck_specialism,
    'canonical_country_outside_market': _stored_recheck_country,
}


def stored_evaluation_problems(evaluation, policy=None):
    """Grounding problems in an evaluation that is about to be persisted.

    Structural validity is the state boundary's own job; this answers the separate
    question of whether the stored numbers are still SUPPORTED by what the object
    itself says supported them.
    """
    problems = []
    if not isinstance(evaluation, dict):
        return [{'field': 'evaluation', 'value': type(evaluation).__name__,
                 'reason': 'not_an_object'}]
    policy = policy or load_policy()
    lead_type = str(evaluation.get('lead_type') or '').strip().lower()
    if lead_type == 'verification':
        # Unscored by construction, so there is nothing here to ground.
        return problems

    evidence_policy = policy.get('evidence_policy') or {}
    unknown_verification = evidence_policy.get('unknown_requires_verification') or {}
    raised = {str(r.get('reason') or '').strip().lower()
              for r in (evaluation.get('verification_needed') or []) if isinstance(r, dict)}
    facts_used = evaluation.get('facts_used')
    if facts_used is not None and not isinstance(facts_used, dict):
        problems.append({'field': 'facts_used', 'value': type(facts_used).__name__,
                         'reason': 'not_an_object'})
        facts_used = {}
    facts_used = facts_used or {}

    for name in sorted((evaluation.get('components') or {})):
        block = (evaluation.get('components') or {})[name]
        if not isinstance(block, dict):
            continue
        score, max_score = block.get('score'), block.get('max_score')
        uncertainty = str(block.get('uncertainty') or '').strip().lower()
        evidence = str(block.get('evidence') or '').strip()
        if not isinstance(score, int) or not isinstance(max_score, int):
            continue  # Structural validation already reported this.
        quality = evidence_quality(evidence, policy)
        expected, effective = component_ceiling(policy, max_score, uncertainty, quality)
        ceiling = block.get('ceiling')
        if isinstance(ceiling, bool) or not isinstance(ceiling, int):
            problems.append({'field': f'components.{name}.ceiling', 'value': ceiling,
                             'reason': 'uncertainty_ceiling_missing'})
        elif ceiling != expected:
            problems.append({'field': f'components.{name}.ceiling', 'value': ceiling,
                             'reason': 'uncertainty_ceiling_disagrees_with_policy',
                             'policy_ceiling': expected})
        if score > expected:
            problems.append({'field': f'components.{name}.score', 'value': score,
                             'reason': 'above_the_uncertainty_ceiling', 'policy_ceiling': expected,
                             'effective_uncertainty': effective})
            continue
        if score == max_score:
            for fault in full_marks_problems(name, block, policy, facts_used, quality):
                problems.append({'field': fault.get('field'), 'value': fault.get('value'),
                                 'reason': fault.get('problem')})
        if effective == 'unknown':
            wanted = set(unknown_verification.get(name) or [])
            if wanted and not (raised & wanted):
                problems.append({'field': f'components.{name}.uncertainty', 'value': effective,
                                 'reason': 'unknown_evidence_must_raise_a_verification_need'})

    specs = blocker_specs(policy)
    for index, row in enumerate(evaluation.get('hard_blockers') or []):
        if not isinstance(row, dict):
            continue
        bid = str(row.get('id') or '').strip().lower()
        field = f'hard_blockers[{index}]'
        spec = specs.get(bid)
        if spec is None:
            continue  # Structural validation already reported the unknown id.
        evidence = row.get('evidence')
        if not isinstance(evidence, dict):
            problems.append({'field': f'{field}.evidence', 'value': type(evidence).__name__,
                             'reason': 'blocker_evidence_must_be_structured'})
            continue
        evidence = normalise_blocker_evidence(evidence)
        for fault in blocker_evidence_problems(bid, evidence, spec, policy):
            problems.append({'field': field, 'value': bid, 'reason': fault.get('problem')})
        precondition = str(row.get('precondition') or '')
        if precondition != spec.get('precondition'):
            problems.append({'field': f'{field}.precondition', 'value': precondition,
                             'reason': 'precondition_is_not_the_one_policy_defines',
                             'policy_precondition': spec.get('precondition')})
            continue
        verified = row.get('verified_against')
        if not isinstance(verified, dict) or not verified:
            problems.append({'field': f'{field}.verified_against', 'value': verified,
                             'reason': 'blocker_must_record_what_it_verified'})
            continue
        recheck = _STORED_RECHECKS.get(precondition)
        reason = recheck(verified, evidence) if recheck else None
        if reason:
            problems.append({'field': field, 'value': bid, 'reason': reason})
    return problems


def evaluation_fingerprints(root=None):
    """sha256 of the two files that actually decide a score.

    Narrower than the snapshot-level `config_fingerprints` on purpose. A ranking
    snapshot records everything that produced a run; an individual evaluation only
    needs to say which CALIBRATION and which POLICY it was calculated against, so
    that a later write can be refused when either has moved. Including the search
    strategy or the sponsor snapshot here would reject perfectly good evaluations
    for changes that cannot alter a single number.
    """
    policy = load_policy()
    wanted = (policy.get('reproducibility', {}).get('evaluation_fingerprint_files')
              or ['candidate/config.json', 'config/matching_policy.json'])
    return config_fingerprints(wanted, root)


def proposal_from_evaluation(evaluation):
    """Reduce a stored evaluation back to the PROPOSAL that would produce it.

    Only the model's own contributions survive: the identity, each component's
    score, evidence and uncertainty, each blocker's id and evidence, and the
    verification needs. Every calculated field is dropped, because the whole point
    of recomputing is to derive them again rather than believe them.
    """
    components = {}
    for name, block in (evaluation.get('components') or {}).items():
        if isinstance(block, dict):
            components[name] = {'score': block.get('score'),
                                'evidence': block.get('evidence'),
                                'uncertainty': block.get('uncertainty')}
    return {
        'company': evaluation.get('company', ''), 'title': evaluation.get('title', ''),
        'url': evaluation.get('url', ''), 'location': evaluation.get('location', ''),
        'key': evaluation.get('key', ''),
        'lead_type': evaluation.get('lead_type', 'direct'),
        'components': components,
        'hard_blockers': [{'id': r.get('id'), 'evidence': r.get('evidence')}
                          for r in (evaluation.get('hard_blockers') or [])
                          if isinstance(r, dict)],
        'verification_needed': [{'reason': r.get('reason'), 'detail': r.get('detail', '')}
                                for r in (evaluation.get('verification_needed') or [])
                                if isinstance(r, dict)],
        'warnings': list(evaluation.get('warnings') or []),
    }


# Fields the EVALUATOR calculates. A stored object may not differ from a fresh
# calculation on any of them, because each is derivable and none is the caller's
# to assert.
RECOMPUTED_FIELDS = ('lead_type', 'total_score', 'max_score', 'score_display',
                     'score_band', 'eligible', 'provisional', 'canonical_grounding')


def recompute_stored_evaluation(evaluation, canonical=None, policy=None,
                                candidate_config=None):
    """Re-derive a stored evaluation from LIVE configuration and canonical data.

    This is the answer to `computed_by`. That field is a string any caller can
    write, so it proves nothing at all about where an object came from; what does
    prove something is that the deterministic evaluator, run NOW, against the
    calibration and policy currently on disk and the vacancy this workspace
    actually stored, produces the same numbers. An object that cannot be
    reproduced is refused however it describes itself.

    Returns `(recomputed, problems)`.
    """
    policy = policy or load_policy()
    if candidate_config is None:
        candidate_config = load_config(required=False)
    proposal = proposal_from_evaluation(evaluation)
    if canonical is None and evaluation.get('hard_blockers'):
        identity = (evaluation.get('canonical_key') or evaluation.get('key')
                    or evaluation.get('url') or '')
        canonical = canonical_vacancy.resolve(identity)
    recomputed, errors = evaluate(proposal, policy, candidate_config, True, canonical)
    if errors:
        return None, [{'field': p.get('field'), 'value': p.get('value'),
                       'reason': p.get('problem'), 'detail': p.get('hint', '')}
                      for p in errors]

    problems = []
    for field in RECOMPUTED_FIELDS:
        if evaluation.get(field) != recomputed.get(field):
            problems.append({'field': field, 'value': evaluation.get(field),
                             'reason': 'does_not_match_a_fresh_deterministic_calculation',
                             'recomputed': recomputed.get(field)})
    for name, block in sorted((recomputed.get('components') or {}).items()):
        stored = (evaluation.get('components') or {}).get(name)
        if not isinstance(stored, dict):
            problems.append({'field': f'components.{name}', 'value': None,
                             'reason': 'missing_from_the_stored_evaluation'})
            continue
        for field in ('score', 'max_score', 'ceiling'):
            if stored.get(field) != block.get(field):
                problems.append({'field': f'components.{name}.{field}',
                                 'value': stored.get(field),
                                 'reason': 'does_not_match_a_fresh_deterministic_calculation',
                                 'recomputed': block.get(field)})
    stored_blockers = {str(r.get('id')): r for r in (evaluation.get('hard_blockers') or [])
                       if isinstance(r, dict)}
    fresh_blockers = {str(r.get('id')): r for r in (recomputed.get('hard_blockers') or [])}
    for bid, fresh in sorted(fresh_blockers.items()):
        stored = stored_blockers.get(bid) or {}
        for field in ('precondition', 'verified_against'):
            if stored.get(field) != fresh.get(field):
                problems.append({'field': f'hard_blockers.{bid}.{field}',
                                 'value': stored.get(field),
                                 'reason': 'does_not_match_a_fresh_deterministic_calculation',
                                 'recomputed': fresh.get(field)})
    for bid in sorted(set(stored_blockers) - set(fresh_blockers)):
        problems.append({'field': f'hard_blockers.{bid}', 'value': bid,
                         'reason': 'not_reproduced_by_a_fresh_deterministic_calculation'})
    return recomputed, problems


def fingerprint_problems(evaluation, root=None):
    """Whether an evaluation was calculated against the configuration in force.

    A score is only meaningful under one calibration and one policy. An object
    carrying different fingerprints was calculated against something else, so
    writing it now would file a decision nobody could reproduce.
    """
    stored = evaluation.get('evaluation_fingerprints')
    if not isinstance(stored, dict) or not stored:
        return [{'field': 'evaluation_fingerprints', 'value': stored,
                 'reason': 'evaluation_does_not_record_the_calibration_it_was_calculated_against'}]
    live = evaluation_fingerprints(root)
    problems = []
    for key, value in sorted(live.items()):
        if stored.get(key) != value:
            problems.append({'field': f'evaluation_fingerprints.{key}',
                             'value': stored.get(key),
                             'reason': 'does_not_match_the_live_configuration',
                             'live': value})
    return problems


def config_fingerprints(paths=None, root=None):
    """sha256 of each configuration that produced a ranking, for reproducibility."""
    root = Path(root) if root else ROOT
    policy = load_policy(root / 'config' / 'matching_policy.json'
                         if (root / 'config' / 'matching_policy.json').exists() else None)
    wanted = paths or policy.get('reproducibility', {}).get('fingerprint_files', [])
    out = {}
    for rel in wanted:
        path = root / rel
        name = Path(rel).stem.replace('-', '_').replace('.', '_')
        key = {
            'config': 'candidate_config_sha256',
            'matching_policy': 'matching_policy_sha256',
            'search_strategy': 'search_strategy_sha256',
            'sources': 'source_registry_sha256',
            'sponsor_register_meta': 'sponsor_snapshot_sha256',
        }.get(name, f'{name}_sha256')
        if not path.is_file():
            out[key] = None
            continue
        if key == 'sponsor_snapshot_sha256':
            # Fingerprint the SNAPSHOT the lookups used, not the metadata wrapper,
            # so the recorded value identifies the register data itself.
            try:
                out[key] = json.loads(path.read_text(encoding='utf-8')).get('sha256')
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                out[key] = None
            continue
        out[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def read_json_input(args):
    if getattr(args, 'file', ''):
        path = Path(args.file)
        if not path.exists():
            raise evaluation_error(f'Input file not found: {path}')
        raw = path.read_text(encoding='utf-8')
    else:
        raw = sys.stdin.read()
    raw = raw.lstrip('﻿')
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise evaluation_error('Malformed JSON input.',
                               f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None


def cmd_schema(args):
    policy = load_policy()
    evidence_policy = policy.get('evidence_policy', {})
    maxima = component_maxima(policy, 'direct')
    ratios = evidence_policy.get('uncertainty_ceilings', {})
    print(json.dumps({
        'schema_version': SCHEMA_VERSION,
        'lead_types': list(LEAD_TYPES),
        'direct_components': maxima,
        'agency_components': component_maxima(policy, 'agency'),
        'agency_excluded': policy['agency_model'].get('excluded_components', []),
        'bands': policy['direct_model']['bands'],
        'uncertainty': list(uncertainty_vocabulary(policy)),
        'uncertainty_ceilings': {
            token: {name: _ceiling_from_ratio(value, ratio) for name, value in maxima.items()}
            for token, ratio in ratios.items()
            if isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
        },
        'full_marks_anchors': evidence_policy.get('full_marks_anchors', {}),
        'unknown_requires_verification': evidence_policy.get('unknown_requires_verification', {}),
        'fact_fields': list(FACT_FIELDS),
        'hard_blockers': [
            {'id': entry['id'], 'precondition': entry.get('precondition'),
             'requires_stated_by': entry.get('requires_stated_by', []),
             'requires_facts': entry.get('requires_facts', entry.get('requires_any_facts', []))}
            for entry in policy['hard_blockers']['vocabulary']
        ],
        'blocker_evidence_requirements': policy['hard_blockers'].get('evidence_requirements', {}),
        'never_blockers': never_blockers(policy),
        'verification_reasons': policy.get('verification_reasons', []),
        'location_score_weight': policy['location_policy']['score_weight'],
        'example': {
            'company': 'Example Ltd', 'title': 'Backend Python Engineer',
            'url': 'https://boards.greenhouse.io/example/jobs/1',
            'location': 'Manchester', 'lead_type': 'direct',
            'facts': {'employment_type': 'permanent', 'work_pattern': 'hybrid',
                      'salary_min': 45000, 'salary_max': 55000, 'salary_currency': 'GBP',
                      'years_required_min': 2,
                      'skills': ['Python', 'Django', 'REST', 'PostgreSQL']},
            'components': {
                'tech_fit': {'score': 34, 'evidence': 'Python, Django and REST APIs are central',
                             'uncertainty': 'known'},
                'seniority_experience': {'score': 12, 'evidence': '2+ years commercial required',
                                         'uncertainty': 'known'},
                'sponsorship': {'score': 14,
                                'evidence': 'Employer on current Worker register; vacancy silent',
                                'uncertainty': 'partial'},
                'employment_conditions': {'score': 8, 'evidence': 'Permanent, GBP 45-55k, hybrid',
                                          'uncertainty': 'known'},
                'company_environment': {'score': 7, 'evidence': 'Product team owning backend services',
                                        'uncertainty': 'partial'},
            },
            'hard_blockers': [],
            'verification_needed': [{'reason': 'sponsorship',
                                     'detail': 'vacancy does not state its position'}],
            'warnings': [],
        },
        'blocker_example': {
            'id': 'experience_requirement',
            'evidence': {
                'excerpt': 'You will need a minimum of 5 years commercial Python experience.',
                'source_url': 'https://boards.greenhouse.io/example/jobs/1',
                'source_type': 'employer-ats', 'stated_by': 'employer'},
        },
        'notes': [
            'A component can never score above its uncertainty ceiling, and evidence that '
            'says only that nothing is known is treated as unknown however it was labelled.',
            'The exact component maximum additionally requires the full-marks anchor the '
            'policy documents for that component, which usually means structured vacancy facts.',
            'A component whose evidence establishes nothing must raise one of its listed '
            'verification reasons, so the gap stays a visible action rather than a number.',
            'A hard blocker must quote the vacancy, name where the quote was read, say who '
            'stated it, and satisfy its own factual precondition against `facts`. Vocabulary '
            'membership is not permission.',
        ],
    }, indent=2, ensure_ascii=False))


def cmd_evaluate(args):
    proposal = read_json_input(args)
    config = None
    if not args.no_candidate_config:
        config = load_config(args.candidate_config or None,
                             required=not args.allow_missing_config)
    canonical = None
    identity = args.canonical_key or proposal.get('key') or proposal.get('url') or ''
    if identity and not args.no_canonical:
        canonical = canonical_vacancy.resolve(identity)
    result, errors = evaluate(proposal, candidate_config=config,
                              strict_blockers=not args.no_candidate_config,
                              canonical=canonical)
    if errors:
        print(json.dumps({'valid': False, 'errors': errors,
                          'note': 'The proposal is rejected rather than repaired. A silent '
                                  'correction would hide a misunderstanding of the model.'},
                         indent=2, ensure_ascii=False))
        raise SystemExit(1)
    payload = {'valid': True, 'evaluation': result}
    if args.fingerprints:
        payload['config_fingerprints'] = config_fingerprints()
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_validate_policy(args):
    path = Path(args.policy) if args.policy else POLICY_PATH
    data = json.loads(path.read_text(encoding='utf-8'))
    problems = policy_problems(data)
    direct = data.get('direct_model', {})
    print(json.dumps({
        'policy': str(path).replace('\\', '/'),
        'valid': not problems,
        'component_maxima': {n: b.get('max_score') for n, b in
                             (direct.get('components') or {}).items()},
        'sum_of_component_maxima': sum((b or {}).get('max_score', 0) for b in
                                       (direct.get('components') or {}).values()),
        'location_score_weight': (data.get('location_policy') or {}).get('score_weight'),
        'agency_total_max': (data.get('agency_model') or {}).get('total_max'),
        'problems': problems,
    }, indent=2, ensure_ascii=False))
    raise SystemExit(0 if not problems else 1)


def cmd_fingerprints(args):
    print(json.dumps(config_fingerprints(), indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description='Deterministic match evaluation')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('schema', help='Print the evaluation contract and an example.')
    s.set_defaults(func=cmd_schema)

    e = sub.add_parser('evaluate', help='Validate a proposal and compute its result.')
    e.add_argument('--file', default='')
    e.add_argument('--candidate-config', dest='candidate_config', default='')
    e.add_argument('--no-candidate-config', dest='no_candidate_config', action='store_true',
                   help='Score without the private calibration. A hard blocker is then '
                        'REFUSED rather than unverified, because every blocker precondition '
                        'is measured against that calibration.')
    e.add_argument('--allow-missing-config', dest='allow_missing_config', action='store_true')
    e.add_argument('--fingerprints', action='store_true',
                   help='Include the full run configuration fingerprints as well. The '
                        'calibration and policy fingerprints always travel inside the '
                        'evaluation itself.')
    e.add_argument('--canonical-key', dest='canonical_key', default='',
                   help='State key or URL of the stored vacancy to check blocker evidence '
                        'against. Defaults to the proposal\'s own key or url.')
    e.add_argument('--no-canonical', dest='no_canonical', action='store_true',
                   help='Score without resolving the stored vacancy. A hard blocker is then '
                        'REFUSED, because its evidence cannot be checked against anything.')
    e.set_defaults(func=cmd_evaluate)

    v = sub.add_parser('validate-policy', help='Validate config/matching_policy.json.')
    v.add_argument('--policy', default='')
    v.set_defaults(func=cmd_validate_policy)

    f = sub.add_parser('fingerprints', help='sha256 of every configuration in force.')
    f.set_defaults(func=cmd_fingerprints)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

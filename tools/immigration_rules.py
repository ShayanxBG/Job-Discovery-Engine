#!/usr/bin/env python3
"""The one owner of the UK immigration figures this workspace reasons about.

WHY THIS IS A FILE AND NOT PROSE. Salary thresholds and occupation going rates
were previously written into `.claude/commands/rank.md` and
`.claude/skills/job-matcher/job-screening.md` as numbers in sentences, in two
places, already disagreeing by ten pounds. Prose cannot say when it was checked,
cannot say which official page it came from, and cannot go stale out loud. These
figures change by statement of changes rather than by anything this project
controls, so a number that keeps being applied long after it moved is the failure
that actually costs a vacancy.

So `config/immigration_rules.json` holds the PUBLISHED values with their sources
and an observation date, and this module reads them. Nothing here is legal advice:
it is a dated reading of official pages, and the case-specific parts stay listed
as verification rather than being computed.

NOTHING IS RECALCULATED. Appendix Skilled Occupations publishes an annual and an
hourly figure for every percentage column, so a percentage is a column name and
never a multiplier. An earlier version computed 70 per cent of the full going rate
and produced GBP 38,290 and GBP 36,610 where the Rules publish GBP 38,300 and
GBP 36,600. Close enough to look right, and wrong enough to be a legal figure this
project invented. Those two values are now refused by name at the validation
boundary, and the only arithmetic left is the comparison the requirement itself
states: the higher of the general threshold and the published occupation figure,
both sides of which are published.

STALENESS IS REPORTED, NEVER SILENT. The reference is fresh up to and including
`review_after`, which is exactly `observed_at` plus `review_interval_days`, and
stale from the day after; the validator refuses any other `review_after` so the
boundary cannot drift into opinion. Thirty days, because this is a live
sponsorship-sensitive search and a threshold that moved three months ago would
have been shaping recommendations the whole time. Stale WARNS: it never deletes a
vacancy and never becomes a hard blocker, because a stale reference still beats no
reference and a run that stopped because a review date passed would be worse than
one that says so.
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / 'config' / 'immigration_rules.json'

STATUSES = ('fresh', 'stale', 'unavailable')
# The percentage column an applicant on a reduced rate is read against. It is a
# COLUMN NAME in the published table, not a multiplier.
NEW_ENTRANT_COLUMN = '70'
_CACHE = {}


def rules_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def load(path=None):
    """Parse the immigration reference, refusing an unusable one outright."""
    path = Path(path) if path else RULES_PATH
    key = str(path)
    if key in _CACHE:
        return _CACHE[key]
    if not path.is_file():
        raise rules_error(f'Immigration reference not found: {path}')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise rules_error(f'Malformed immigration reference: {path}',
                          f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None
    problems = reference_problems(data)
    if problems:
        raise rules_error('The immigration reference is not valid.',
                          f'Problems: {json.dumps(problems, ensure_ascii=False)}',
                          'A salary judgement must not run against a broken legal reference.')
    _CACHE[key] = data
    return data


def _iso_date(value):
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def reference_problems(data):
    """Every structural problem in the immigration reference."""
    problems = []
    if not isinstance(data, dict):
        return [{'field': '_root', 'problem': 'not_an_object'}]
    for field in ('observed_at', 'review_after'):
        if _iso_date(data.get(field)) is None:
            problems.append({'field': field, 'value': data.get(field),
                             'problem': 'not_an_iso_date'})
    observed, review = _iso_date(data.get('observed_at')), _iso_date(data.get('review_after'))
    if observed and review and review <= observed:
        problems.append({'field': 'review_after', 'problem': 'must_be_after_the_observation_date'})

    interval = data.get('review_interval_days')
    if not isinstance(interval, int) or isinstance(interval, bool) or not 1 <= interval <= 180:
        problems.append({'field': 'review_interval_days', 'value': interval,
                         'problem': 'not_a_review_interval_in_days'})
    elif observed and review and review != observed + timedelta(days=interval):
        # The boundary must be arithmetic, not editorial, or `fresh` and `stale`
        # mean whatever the last person to touch the file thought they meant.
        problems.append({'field': 'review_after', 'value': data.get('review_after'),
                         'problem': 'must_equal_observed_at_plus_review_interval_days',
                         'expected': (observed + timedelta(days=interval)).isoformat()})

    salary = data.get('salary_thresholds') or {}
    for field in ('general_standard', 'general_new_entrant_floor'):
        value = salary.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            problems.append({'field': f'salary_thresholds.{field}', 'value': value,
                             'problem': 'not_a_positive_integer'})
    if salary.get('percentage_is_explanatory_only') is not True:
        problems.append({'field': 'salary_thresholds.percentage_is_explanatory_only',
                         'value': salary.get('percentage_is_explanatory_only'),
                         'problem': 'the_percentage_must_be_explanatory_because_the_rules_publish_the_value'})

    policy = data.get('derivation_policy') or {}
    if policy.get('recalculating_a_published_annual_value_is_forbidden') is not True:
        problems.append({'field': 'derivation_policy.recalculating_a_published_annual_value_is_forbidden',
                         'value': policy.get('recalculating_a_published_annual_value_is_forbidden'),
                         'problem': 'required'})
    forbidden = {v for v in (policy.get('forbidden_derived_annual_values') or [])
                 if isinstance(v, int)}
    if not forbidden:
        problems.append({'field': 'derivation_policy.forbidden_derived_annual_values',
                         'problem': 'required'})

    rates = data.get('going_rates') or {}
    if not rates:
        problems.append({'field': 'going_rates', 'problem': 'required'})
    for code, entry in sorted(rates.items()):
        published = (entry or {}).get('published_rates')
        if not isinstance(published, dict) or NEW_ENTRANT_COLUMN not in published:
            problems.append({'field': f'going_rates.{code}.published_rates',
                             'problem': 'the_published_percentage_table_is_required',
                             'required_column': NEW_ENTRANT_COLUMN})
            continue
        for column, row in sorted(published.items()):
            annual = (row or {}).get('annual')
            hourly = (row or {}).get('hourly')
            if not isinstance(annual, int) or isinstance(annual, bool) or annual <= 0:
                problems.append({'field': f'going_rates.{code}.published_rates.{column}.annual',
                                 'value': annual, 'problem': 'not_a_whole_number_of_pounds'})
            elif annual in forbidden:
                # The exact values an earlier version produced by multiplying. Named
                # so the regression cannot return quietly under a different author.
                problems.append({
                    'field': f'going_rates.{code}.published_rates.{column}.annual',
                    'value': annual,
                    'problem': 'this_is_an_independently_calculated_value_not_the_published_one',
                    'hint': 'Appendix Skilled Occupations publishes the annual figure for each '
                            'percentage column. Copy it; never compute it.'})
            if not isinstance(hourly, (int, float)) or isinstance(hourly, bool) or hourly <= 0:
                problems.append({'field': f'going_rates.{code}.published_rates.{column}.hourly',
                                 'value': hourly, 'problem': 'not_a_published_hourly_rate'})

    entrant = data.get('new_entrant') or {}
    if entrant.get('student_permission_is_enumerated') is not False:
        # The Rules enumerate Skilled Worker, Graduate and Tier 2. Recording
        # anything else here would reinstate the misreading this file exists to fix.
        problems.append({'field': 'new_entrant.student_permission_is_enumerated',
                         'value': entrant.get('student_permission_is_enumerated'),
                         'problem': 'sw_12_3_enumerates_skilled_worker_graduate_and_tier_2_only'})
    if not entrant.get('combined_permission_condition_quote'):
        problems.append({'field': 'new_entrant.combined_permission_condition_quote',
                         'problem': 'required'})
    if not entrant.get('requires_case_specific_confirmation'):
        problems.append({'field': 'new_entrant.requires_case_specific_confirmation',
                         'problem': 'required'})

    sources = data.get('sources') or []
    if len(sources) < 3:
        problems.append({'field': 'sources', 'problem': 'at_least_three_official_sources_required'})
    for index, source in enumerate(sources):
        url = str((source or {}).get('url') or '')
        if not url.startswith('https://www.gov.uk/'):
            problems.append({'field': f'sources[{index}].url', 'value': url[:80],
                             'problem': 'not_an_official_gov_uk_url'})
        if _iso_date((source or {}).get('checked_on')) is None:
            problems.append({'field': f'sources[{index}].checked_on',
                             'problem': 'not_an_iso_date'})
        if not str((source or {}).get('conclusion') or '').strip():
            problems.append({'field': f'sources[{index}].conclusion', 'problem': 'required'})
    if not data.get('standing_limitations'):
        problems.append({'field': 'standing_limitations', 'problem': 'required'})
    return problems


def status(today=None, path=None):
    """Whether the legal reference is still inside its review window."""
    today = today or date.today()
    try:
        data = load(path)
    except SystemExit as exc:
        return {'status': 'unavailable', 'detail': str(exc).splitlines()[0],
                'observed_at': None, 'review_after': None, 'days_until_review': None}
    observed = _iso_date(data.get('observed_at'))
    review = _iso_date(data.get('review_after'))
    days = (review - today).days if review else None
    age = (today - observed).days if observed else None
    # FRESH up to and including review_after, STALE from the day after. Stated
    # rather than implied, because a boundary nobody wrote down is a boundary two
    # readers will disagree about.
    fresh = days is not None and days >= 0
    return {
        'status': 'fresh' if fresh else 'stale',
        'observed_at': data.get('observed_at'),
        'review_after': data.get('review_after'),
        'review_interval_days': data.get('review_interval_days'),
        'age_days': age,
        'days_until_review': days,
        'boundary': 'fresh while today <= review_after; stale from the following day',
        'source_count': len(data.get('sources') or []),
        'is_a_hard_blocker': False,
        'detail': ('Immigration reference is inside its review window.' if fresh else
                   'Immigration reference is past its review date. Re-verify every figure '
                   'against the official GOV.UK pages before it decides anything. This is a '
                   'review warning: it never blocks a vacancy and never becomes a hard blocker.'),
    }


def salary_bands(code, path=None):
    """The PUBLISHED salary picture for one occupation code.

    Nothing here multiplies. Appendix Skilled Occupations publishes an annual and
    an hourly figure for every percentage column, so the occupation figure is READ
    from that table. An earlier version computed 70 per cent of the full going rate
    and produced GBP 38,290 and GBP 36,610 where the Rules publish GBP 38,300 and
    GBP 36,600: close enough to look right, wrong enough to be a legal figure this
    project invented.

    The only arithmetic left is the one the requirement itself states: the
    applicable minimum is the HIGHER of the general threshold and the published
    occupation figure. Both sides of that comparison are published values.
    """
    data = load(path)
    entry = (data.get('going_rates') or {}).get(str(code))
    if not entry:
        return None
    published = entry.get('published_rates') or {}
    full = (published.get('100') or {})
    reduced = (published.get(NEW_ENTRANT_COLUMN) or {})
    if not full.get('annual') or not reduced.get('annual'):
        # No published row means no answer. Computing one is exactly the defect.
        return None
    salary = data['salary_thresholds']
    return {
        'code': str(code),
        'title': entry.get('title', ''),
        'currency': salary.get('currency', 'GBP'),
        'going_rate': full['annual'],
        'going_rate_hourly': full.get('hourly'),
        'standard_threshold': max(salary['general_standard'], full['annual']),
        'new_entrant_column': NEW_ENTRANT_COLUMN,
        'new_entrant_going_rate': reduced['annual'],
        'new_entrant_going_rate_hourly': reduced.get('hourly'),
        'new_entrant_threshold': max(salary['general_new_entrant_floor'], reduced['annual']),
        'published_rates': dict(published),
        'general_standard': salary['general_standard'],
        'general_new_entrant_floor': salary['general_new_entrant_floor'],
        'values_are_published': True,
        'derivation': 'Every occupation figure is READ from the published Appendix Skilled '
                      'Occupations table. The only computed step is max(general threshold, '
                      'published occupation figure), which is how the requirement is stated '
                      'and whose inputs are both published.',
        'observed_at': data.get('observed_at'),
    }


def viability_note(amount, path=None):
    """How a stated salary reads against every occupation code we hold.

    Deliberately returns a PICTURE rather than a verdict. The occupation code is
    the sponsor's choice and the advert almost never states it, so a figure that
    clears one code and not another is undecided, and saying so is the honest
    answer rather than picking the code that settles it.
    """
    data = load(path)
    codes = sorted((data.get('going_rates') or {}))
    bands = [b for b in (salary_bands(code, path) for code in codes) if b]
    if amount is None:
        return {'salary': None, 'verdict': 'unknown', 'uncertainty': 'unknown',
                'codes': {b['code']: b['new_entrant_threshold'] for b in bands},
                'note': 'No salary stated. That is missing information, never a low salary, '
                        'and it can never count against a role.'}
    clears = [b['code'] for b in bands if amount >= b['new_entrant_threshold']]
    floor = data['salary_thresholds']['general_new_entrant_floor']
    if amount < floor:
        verdict, uncertainty = 'below_every_threshold', 'known'
    elif len(clears) == len(bands):
        verdict, uncertainty = 'clears_every_code_we_hold', 'known'
    else:
        verdict, uncertainty = 'depends_on_the_occupation_code', 'partial'
    return {
        'salary': amount, 'verdict': verdict, 'uncertainty': uncertainty,
        'clears_codes': clears,
        'codes': {b['code']: b['new_entrant_threshold'] for b in bands},
        'general_new_entrant_floor': floor,
        'observed_at': data.get('observed_at'),
        'values_are_published': True,
        'note': 'Published reduced-rate figures, read from Appendix Skilled Occupations. They '
                'apply only if a reduced-rate limb actually applies to the applicant, which '
                'depends on dates this project does not hold, and the occupation code is the '
                'sponsor\'s choice rather than anything a title reveals. A figure between two '
                'codes stays a verification need. Salary viability is never on its own evidence '
                'that a vacancy will be sponsored.',
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_status(args):
    print(json.dumps(status(), indent=2, ensure_ascii=False))
    raise SystemExit(0 if status()['status'] != 'unavailable' else 1)


def cmd_show(args):
    data = load()
    payload = {
        'observed_at': data['observed_at'], 'review_after': data['review_after'],
        'status': status()['status'],
        'salary_thresholds': data['salary_thresholds'],
        'derived_bands': {code: salary_bands(code) for code in sorted(data['going_rates'])},
        'new_entrant': data['new_entrant'],
        'employment_relationship': data['employment_relationship'],
        'sources': data['sources'],
        'standing_limitations': data['standing_limitations'],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_salary(args):
    print(json.dumps(viability_note(args.amount), indent=2, ensure_ascii=False))


def cmd_validate(args):
    data = json.loads(Path(args.path or RULES_PATH).read_text(encoding='utf-8'))
    problems = reference_problems(data)
    print(json.dumps({'valid': not problems, 'problems': problems,
                      'observed_at': data.get('observed_at'),
                      'review_after': data.get('review_after')},
                     indent=2, ensure_ascii=False))
    raise SystemExit(0 if not problems else 1)


def main():
    p = argparse.ArgumentParser(description='Dated official UK immigration reference')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status', help='Is the legal reference inside its review window?').set_defaults(func=cmd_status)
    sub.add_parser('show', help='The reference and everything derived from it.').set_defaults(func=cmd_show)
    s = sub.add_parser('salary', help='How one stated salary reads against every code.')
    s.add_argument('--amount', type=int)
    s.set_defaults(func=cmd_salary)
    v = sub.add_parser('validate', help='Structural validation of the reference.')
    v.add_argument('--path', default='')
    v.set_defaults(func=cmd_validate)
    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

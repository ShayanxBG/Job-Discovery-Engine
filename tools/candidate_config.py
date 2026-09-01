#!/usr/bin/env python3
"""Private machine-readable candidate matching configuration.

`candidate/profile.md` is the authority on what is true about the candidate, and it
stays that way. But it is prose, and prose has to be re-interpreted on every run:
"junior on the way to mid-level" is a sentence a human understands and a matcher
has to guess at, differently each time. That is exactly the kind of drift a scoring
model must not have.

So this module derives a COMPACT MACHINE CONFIGURATION of the matching constraints:
which levels are acceptable, which experience minimum is a stretch and which is a
blocker, which specialisms are excluded, whether sponsorship is required, whether a
salary floor is configured. Deterministic code then reads those fields instead of
re-reading a paragraph.

WHAT IT IS NOT. It is not a copy of the profile, and it is not identity. It holds
matching constraints only: no name, email, phone, address, account details, visa
document facts or CV prose. The privacy boundary is enforced twice, the same way
`search_profile.py` does it:

  1. Extraction is WHITELISTED. Nothing is copied out of the profile unless this
     module went looking for it in a named section, so a new private paragraph
     cannot leak by default. That is the opposite of copying everything and trying
     to redact the sensitive parts afterwards.
  2. The RESULT is checked. `privacy_problems()` refuses a config carrying an
     identity label, a contact detail, a date-shaped token, a document number or
     free prose, so a profile that put a phone number under a heading this module
     reads is caught rather than shipped.

UNKNOWN STAYS UNKNOWN. Where the profile is ambiguous the field is null. Null means
"the profile does not establish this", which is different from false, and the
evaluator treats it that way: a null salary floor cannot produce a salary blocker,
and a null clearance constraint cannot produce a clearance blocker. Nothing here
invents experience, salary expectations, sponsorship status, skills or willingness
to relocate.

BUILD IS NOT DESTRUCTIVE. `build` refuses to overwrite an existing config, because
a config may have been hand-corrected after generation and silently regenerating it
would discard that. Without `--overwrite` it writes a `.proposed.json` beside the
real file and tells you to diff it.
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import atomic_write_text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'candidate' / 'profile.md'
CONFIG = ROOT / 'candidate' / 'config.json'
EXAMPLE = ROOT / 'candidate' / 'config.example.json'
SCHEMA_VERSION = 1

# The complete field surface. Anything outside it is refused at the boundary, which
# is what structurally keeps identity out of a file that gets read on every run.
TOP_LEVEL_FIELDS = (
    'schema_version', '_comment', 'target_roles', 'adjacent_roles', 'seniority',
    'skills', 'specialisms', 'employment', 'location', 'sponsorship', 'salary',
    'working_pattern', 'constraints', 'derived_from',
)
SECTION_FIELDS = {
    'seniority': ('target_level', 'acceptable_levels', 'excluded_levels',
                  'commercial_experience', 'review_from_years',
                  'hard_block_at_or_above_years', '_comment'),
    'skills': ('primary_languages', 'frameworks', 'backend_capabilities', 'databases',
               'secondary_skills', '_comment'),
    'specialisms': ('preferred', 'excluded', '_comment'),
    'employment': ('acceptable_types', 'excluded_types', '_comment'),
    'location': ('market', 'preferred_locations', 'relocation_within_market',
                 'score_weight', '_comment'),
    'sponsorship': ('eventual_sponsorship_required', 'current_status_category',
                    'licence_evidence_required', 'vacancy_specific_confirmation_required',
                    '_comment'),
    'salary': ('hard_floor', 'currency', '_comment'),
    'working_pattern': ('acceptable', 'preferred', '_comment'),
    'constraints': ('security_clearance_obtainable', '_comment'),
    # `experience_observed_at` lives here because `derived_from` is the one
    # section exempt from the no-dates privacy rule, and a dated observation is
    # exactly what provenance is for. The range itself carries no date.
    'derived_from': ('profile_sha256', 'built_at', 'tool', 'experience_observed_at'),
}

SENIORITY_LEVELS = ('junior', 'junior-to-mid', 'mid', 'mid-to-senior', 'senior')
EXCLUDED_LEVEL_WORDS = ('senior', 'staff', 'principal', 'lead', 'head', 'director',
                        'architect', 'manager')
# Right-to-work is expressed as a CATEGORY. A visa type tied to an individual, a
# document number or an expiry date is identity and belongs only in the profile.
STATUS_CATEGORIES = ('unrestricted', 'time-limited-work-authorisation',
                     'sponsorship-required-now', 'unknown')

DEFAULT_EXCLUDED_SPECIALISMS = (
    'data science', 'machine learning research', 'quantitative research', 'embedded',
    'game development', 'frontend only', 'technical support',
)
DEFAULT_PREFERRED_SPECIALISMS = (
    'backend', 'api', 'integrations', 'platform', 'application engineering',
)
# `contract` here means INDEPENDENT CONTRACTING: day rate, outside IR35, self
# employment, and labour supplied to a third party. It deliberately does NOT mean
# a directly employed fixed-term role, which the official sponsor guidance does
# not prohibit and which can be a genuine sponsored job. The two used to collapse
# into one blocker and one of them was being discarded for the other's reasons.
DEFAULT_EXCLUDED_EMPLOYMENT = ('contract', 'freelance', 'temporary', 'apprenticeship',
                               'internship', 'day-rate', 'outside-ir35')
# `contract-unspecified` is acceptable in the sense that it is not a reason to
# walk away. It is the honest label for an advert that said `contract` without
# saying whose, and what it earns is an employment_type verification need.
DEFAULT_ACCEPTABLE_EMPLOYMENT = ('permanent', 'full-time', 'fixed-term',
                                'contract-unspecified')

# Where the experience thresholds come from. They are NOT guessed: the matching
# rules state them explicitly ("4+ years hard minimum: drop", "3+ years hard
# minimum: retain only for human review"). Reading them from those files keeps one
# definition, and leaving them null instead would silently disable an experience
# blocker the product already promises to apply.
#
# The hard figure is INCLUSIVE. `4+ years hard minimum: drop` means a stated hard
# minimum OF FOUR blocks, which is why the config field is named
# `hard_block_at_or_above_years` rather than a "maximum" that could be read either
# way at the only value where the two readings differ.
EXPERIENCE_POLICY_FILES = (
    '.claude/skills/job-matcher/job-screening.md',
    '.claude/skills/scrape/SKILL.md',
)
HARD_YEARS_PATTERN = re.compile(r'(\d+)\+\s*years?\s+hard\s+minimum\s*:\s*(?:always\s+)?drop', re.I)
BORDERLINE_YEARS_PATTERN = re.compile(
    r'(\d+)\+\s*years?\s+hard\s+minimum\s*:\s*(?:retain|keep)\s+only', re.I)


def derive_experience_thresholds(root=None):
    """The documented hard and borderline experience minimums, or (None, None).

    Returns the LOWEST stated drop threshold and the highest stated
    retain-selectively threshold, so a rules file listing 5+ drop and 4+ drop
    yields 4 rather than 5.
    """
    root = Path(root) if root else ROOT
    hard, borderline = [], []
    for rel in EXPERIENCE_POLICY_FILES:
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        hard += [int(m) for m in HARD_YEARS_PATTERN.findall(text)]
        borderline += [int(m) for m in BORDERLINE_YEARS_PATTERN.findall(text)]
    return (min(hard) if hard else None, max(borderline) if borderline else None)

# The privacy boundary, mirroring tools/search_profile.py.
IDENTITY_LABELS = ('name', 'email', 'e-mail', 'phone', 'mobile', 'telephone', 'address',
                   'postcode', 'date of birth', 'dob', 'nationality', 'passport',
                   'national insurance', 'brp', 'share code')
CONTACT_PATTERNS = (
    re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+'),
    re.compile(r'(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)'),
    re.compile(r'\b\d{4}-\d{2}-\d{2}\b'),
    re.compile(r'\b\d{1,2}\s+\w+\s+(?:19|20)\d{2}\b'),
    re.compile(r'\bhttps?://\S+', re.I),
)
MAX_TERM_CHARS = 60


def config_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def display_path(path):
    """A workspace-relative path where possible, absolute otherwise."""
    path = Path(path)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def sha256_of_file(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ''


def read_profile(path=None):
    path = Path(path) if path else PROFILE
    if not path.exists():
        raise config_error(
            f'Candidate profile not found: {path}',
            'Copy candidate/profile.example.md to candidate/profile.md and fill it in.',
        )
    return path.read_text(encoding='utf-8')


def section(text, heading_pattern):
    """The body of one markdown section, by heading regex."""
    out, capturing, level = [], False, 0
    for line in text.splitlines():
        match = re.match(r'^(#{1,6})\s+(.*)$', line)
        if match:
            if capturing and len(match.group(1)) <= level:
                break
            if not capturing and re.search(heading_pattern, match.group(2), re.I):
                capturing, level = True, len(match.group(1))
                continue
        if capturing:
            out.append(line)
    return '\n'.join(out)


def labelled_value(text, label):
    match = re.search(rf'^\s*[-*]\s*{re.escape(label)}\s*:\s*(.+)$', text, re.I | re.M)
    return re.sub(r'\s+', ' ', match.group(1)).strip() if match else ''


def split_terms(value):
    value = re.sub(r'\([^)]*\)', ' ', str(value or ''))
    out = []
    # Commas and semicolons separate skills. '/' does not: 'CI/CD' and
    # 'GitHub Actions CI/CD' are single terms, not two half-terms.
    for part in re.split(r'[,;]| and ', value):
        term = re.sub(r'\s+', ' ', part).strip(' .·-')
        if term and len(term) <= 40:
            out.append(term)
    return out


def dedupe(values):
    seen, out = set(), []
    for value in values:
        token = re.sub(r'\s+', ' ', str(value or '')).strip()
        if token and token.lower() not in seen:
            seen.add(token.lower())
            out.append(token)
    return out


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------

def derive_seniority(identity_text):
    """Seniority calibration from the profile's own seniority sentence.

    Every field stays null unless the profile actually says it. A missing
    `hard_block_at_or_above_years` means no experience blocker can ever fire, which
    is the correct behaviour for a profile that never stated a limit.
    """
    line = labelled_value(identity_text, 'Seniority')
    lowered = line.lower()
    out = {
        'target_level': None,
        'acceptable_levels': [],
        'excluded_levels': list(EXCLUDED_LEVEL_WORDS),
        'commercial_experience': None,
        'review_from_years': None,
        'hard_block_at_or_above_years': None,
    }
    if not lowered:
        return out

    junior = 'junior' in lowered or 'graduate' in lowered or 'entry' in lowered
    mid = 'mid' in lowered
    if junior and mid:
        out['target_level'] = 'junior-to-mid'
        out['acceptable_levels'] = ['junior', 'junior-to-mid', 'mid']
    elif junior:
        out['target_level'] = 'junior'
        out['acceptable_levels'] = ['junior', 'junior-to-mid']
    elif mid:
        out['target_level'] = 'mid'
        out['acceptable_levels'] = ['junior-to-mid', 'mid']

    # "Senior roles are not." is an explicit exclusion, and the default exclusion
    # list already covers it. Only widen acceptance when the profile says so.
    if re.search(r'senior\s+roles?\s+are\s+(in\s+scope|acceptable)', lowered):
        out['excluded_levels'] = [w for w in EXCLUDED_LEVEL_WORDS if w != 'senior']
        out['acceptable_levels'] = dedupe(out['acceptable_levels'] + ['senior'])

    out['commercial_experience'] = derive_commercial_experience(identity_text)
    hard, review = derive_experience_thresholds()
    out['hard_block_at_or_above_years'] = hard
    out['review_from_years'] = review
    return out


def derive_commercial_experience(identity_text):
    """The candidate's own confirmed commercial-experience total, as a dated RANGE.

    A scalar was the wrong shape and became wrong with time. The profile states
    month-granularity role dates and the current role is ONGOING, so any single
    number is both falsely precise and quietly decaying: 21 months was true on
    29 August 2026 and is not true a quarter later.

    So the range is stored as it was stated, with `ongoing_role` recorded, and the
    observation date is written separately into `derived_from`. Nothing here
    invents a day for a month-granularity date, and nothing advances the figure by
    guessing elapsed time; the calibration is instead REPORTED as stale once the
    observation is old enough, which is the honest answer to a number that has to
    be re-confirmed rather than extrapolated.

    Bounds are never reversed, and the LOWER bound is what any conservative
    comparison uses. That is the opposite of how a vacancy's stated range is read,
    and deliberately so: a range in an advert is the employer's own acceptable
    limit, so reading it generously keeps the candidate in play, while a range
    about the candidate read generously would overstate them.
    """
    line = labelled_value(identity_text, 'Commercial experience')
    if not line:
        return None
    lowered = line.lower()
    span = re.search(r'(\d{1,3})\s*(?:to|-|\u2013)\s*(\d{1,3})\s*months', lowered)
    if span:
        low, high = int(span.group(1)), int(span.group(2))
    else:
        single = re.search(r'(\d{1,3})\s*months', lowered)
        if not single:
            return None
        low = high = int(single.group(1))
    if low > high:
        low, high = high, low
    return {
        'minimum_months': low,
        'maximum_months': high,
        'ongoing_role': bool(re.search(r'\bongoing\b', lowered)),
        'is_a_lower_bound_for_comparison': True,
    }


def derive_experience_observed_at(identity_text):
    """The date the profile itself states the experience total was true.

    Read from the profile rather than stamped from the clock, so repeated builds
    on different days produce the same value and `diff` stays quiet until the
    evidence actually changes.
    """
    line = labelled_value(identity_text, 'Commercial experience')
    found = re.search(r'as\s+of\s+(\d{1,2})\s+([A-Za-z]+)\s+((?:19|20)\d{2})', line or '')
    if not found:
        return None
    months = {m: i for i, m in enumerate(
        ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
         'september', 'october', 'november', 'december'), start=1)}
    month = months.get(found.group(2).lower())
    if not month:
        return None
    return f'{int(found.group(3)):04d}-{month:02d}-{int(found.group(1)):02d}'


EXPERIENCE_REVIEW_DAYS = 31


def experience_staleness(config, today=None):
    """Whether the recorded commercial-experience range still describes today.

    A WARNING, never a rejection. The hard experience blocker turns on the
    VACANCY's own explicit four-year minimum and never on subtracting two
    approximate figures, so a stale range cannot reject anybody; what it can do is
    quietly misdescribe the candidate in prose, and that is worth saying out loud.
    """
    today = today or date.today()
    observed = ((config or {}).get('derived_from') or {}).get('experience_observed_at')
    try:
        stamp = datetime.strptime(str(observed)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return {'status': 'unknown', 'age_days': None, 'observed_at': observed,
                'review_after_days': EXPERIENCE_REVIEW_DAYS, 'is_a_hard_blocker': False,
                'detail': 'No recorded observation date for the commercial-experience range.'}
    age = (today - stamp).days
    fresh = age <= EXPERIENCE_REVIEW_DAYS
    return {
        'status': 'fresh' if fresh else 'stale',
        'observed_at': observed,
        'age_days': age,
        'review_after_days': EXPERIENCE_REVIEW_DAYS,
        'is_a_hard_blocker': False,
        'detail': ('The commercial-experience range is inside its review window.' if fresh else
                   'The commercial-experience range was observed more than '
                   f'{EXPERIENCE_REVIEW_DAYS} days ago and the role is ongoing, so it '
                   'understates the candidate. Re-state it in candidate/profile.md and rebuild '
                   'the calibration. This is a maintenance warning: it changes no blocker and '
                   'rejects no vacancy.'),
    }


def derive_skills(skills_text):
    """Skills from the profile's labelled technical-skills lines only."""
    slots = {'primary_languages': [], 'frameworks': [], 'databases': [], 'secondary_skills': []}
    label_map = {
        'languages': 'primary_languages',
        'backend': 'frameworks',
        'database': 'databases',
        'data': 'databases',
        'apis and integration': 'secondary_skills',
        'api and integration': 'secondary_skills',
        'devops and environment': 'secondary_skills',
        'testing': 'secondary_skills',
        'frontend': 'secondary_skills',
        'tools': 'secondary_skills',
    }
    # Markup is not a queryable or matchable language.
    non_languages = {'html', 'css'}
    for line in skills_text.splitlines():
        match = re.match(r'^\s*[-*]\s*([^:]{1,40}):\s*(.+)$', line)
        if not match:
            continue
        slot = label_map.get(re.sub(r'\s+', ' ', match.group(1)).strip().lower())
        if not slot:
            continue
        for term in split_terms(match.group(2)):
            if slot == 'primary_languages' and term.lower() in non_languages:
                continue
            slots[slot].append(term)

    languages = dedupe(slots['primary_languages'])
    # The PRIMARY language drives a hard blocker, so it must be the language the
    # profile leads with rather than every language ever listed. Naming four here
    # would make "wrong primary language" almost unable to fire.
    primary = languages[:1]
    secondary = dedupe(slots['secondary_skills'] + languages[1:])
    return {
        'primary_languages': primary,
        'frameworks': dedupe(slots['frameworks']),
        'backend_capabilities': ['REST API', 'backend services', 'integrations', 'server-side'],
        'databases': dedupe(slots['databases']),
        'secondary_skills': secondary,
    }


def derive_location(identity_text):
    line = labelled_value(identity_text, 'Location preference') or labelled_value(
        identity_text, 'Location preferences')
    market_line = labelled_value(identity_text, 'Active market')
    lowered = line.lower()
    preferred = []
    for city in ('London', 'Cambridge', 'Oxford', 'Manchester', 'Bristol', 'Edinburgh',
                 'Leeds', 'Birmingham', 'Glasgow', 'Cardiff', 'Belfast'):
        if city.lower() in lowered:
            preferred.append(city)
    market = None
    if market_line:
        market = re.sub(r'\s*only\.?\s*$', '', market_line, flags=re.I).strip(' .')
    relocation = None
    if re.search(r'willing to relocate|happy to relocate|relocate anywhere', lowered):
        relocation = True
    elif re.search(r'not willing to relocate|no relocation', lowered):
        relocation = False
    return {
        'market': market,
        'preferred_locations': preferred,
        'relocation_within_market': relocation,
        # Never derived, never configurable upward. Location carries no score.
        'score_weight': 0,
    }


def derive_sponsorship(identity_text, profile_text):
    """Sponsorship REQUIREMENT as a category, never as an individual's document facts."""
    blob = f'{identity_text}\n{profile_text}'.lower()
    required = None
    category = 'unknown'
    # A time-limited work authorisation that the profile says will need sponsoring
    # later is the requirement; the expiry date itself stays in the profile.
    if re.search(r'visa expiry|expiry:|time-limited|graduate visa', blob):
        category = 'time-limited-work-authorisation'
        required = True
    if re.search(r'does not (?:currently )?(?:need|require) sponsorship|'
                 r'no sponsorship (?:needed|required)|unrestricted right to work', blob):
        category = 'unrestricted'
        required = False
    if re.search(r'sponsorship (?:is )?(?:needed|required) now', blob):
        category = 'sponsorship-required-now'
        required = True
    return {
        'eventual_sponsorship_required': required,
        'current_status_category': category,
        'licence_evidence_required': True if required else None,
        'vacancy_specific_confirmation_required': True if required else None,
    }


def derive_target_roles(identity_text):
    line = labelled_value(identity_text, 'Target') or labelled_value(identity_text, 'Target roles')
    lowered = line.lower()
    roles = []
    if 'python' in lowered:
        roles.append('Python Developer')
    if 'backend' in lowered or 'back-end' in lowered:
        roles.append('Backend Developer')
        if 'python' in lowered:
            roles.append('Python Backend Developer')
    if 'integration' in lowered:
        roles.append('Integration Developer')
    if 'software' in lowered and not roles:
        roles.append('Software Developer')
    return dedupe(roles)


def build_config(profile_text, profile_path=None):
    """The whole config, built only from whitelisted profile sections."""
    identity = section(profile_text, r'identity and target') or profile_text
    skills_text = section(profile_text, r'technical skills')

    config = {
        'schema_version': SCHEMA_VERSION,
        '_comment': ('Private candidate MATCHING CONSTRAINTS derived from '
                     'candidate/profile.md. Constraints only: never identity, contact '
                     'details or CV prose. A null value means the profile does not '
                     'establish it, which is not the same as false.'),
        'target_roles': derive_target_roles(identity),
        'adjacent_roles': ['Software Engineer', 'Software Developer', 'Integration Engineer',
                           'API Engineer', 'Platform Engineer'],
        'seniority': derive_seniority(identity),
        'skills': derive_skills(skills_text),
        'specialisms': {
            'preferred': list(DEFAULT_PREFERRED_SPECIALISMS),
            'excluded': list(DEFAULT_EXCLUDED_SPECIALISMS),
        },
        'employment': {
            'acceptable_types': list(DEFAULT_ACCEPTABLE_EMPLOYMENT),
            'excluded_types': list(DEFAULT_EXCLUDED_EMPLOYMENT),
        },
        'location': derive_location(identity),
        'sponsorship': derive_sponsorship(identity, profile_text),
        # Never derived from prose. A salary floor is a deliberate decision, and
        # guessing one would silently start rejecting real vacancies.
        'salary': {'hard_floor': None, 'currency': 'GBP'},
        'working_pattern': {'acceptable': [], 'preferred': None},
        # Unknown, not false: an unknown clearance constraint must not block.
        'constraints': {'security_clearance_obtainable': None},
        'derived_from': {
            'profile_sha256': sha256_of_file(profile_path or PROFILE),
            'built_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'tool': 'tools/candidate_config.py',
            # Read from the profile's own words, never stamped from the clock, so
            # two builds on different days produce the same value and `diff` stays
            # quiet until the evidence itself changes.
            'experience_observed_at': derive_experience_observed_at(identity),
        },
    }

    pattern_line = labelled_value(identity, 'Location preference').lower()
    acceptable = [word for word in ('remote', 'hybrid', 'on-site') if word in pattern_line]
    config['working_pattern']['acceptable'] = acceptable

    # A profile that states a target level but no hard experience limit gets none.
    # The blocker simply cannot fire, which is correct: nothing said it should.
    return config


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def privacy_problems(config):
    """Anything in a candidate config that is not a matching constraint."""
    problems = []
    if not isinstance(config, dict):
        return [{'field': '_root', 'problem': 'not_an_object'}]
    for field in sorted(set(config) - set(TOP_LEVEL_FIELDS)):
        problems.append({'field': field, 'problem': 'not_a_matching_constraint_field'})
    for name, allowed in SECTION_FIELDS.items():
        block = config.get(name)
        if isinstance(block, dict):
            for field in sorted(set(block) - set(allowed)):
                problems.append({'field': f'{name}.{field}', 'problem': 'not_a_config_field'})

    def scan(value, path):
        if isinstance(value, dict):
            for key, sub in value.items():
                if any(f'{label}' == str(key).lower() for label in IDENTITY_LABELS):
                    problems.append({'field': f'{path}.{key}', 'problem': 'identity_field'})
                scan(sub, f'{path}.{key}')
            return
        if isinstance(value, list):
            for index, sub in enumerate(value):
                scan(sub, f'{path}[{index}]')
            return
        if not isinstance(value, str):
            return
        # `_comment` and `derived_from` carry deliberate explanatory text and a
        # profile digest, so they are exempt from the term-shape rules.
        if path.endswith('_comment') or '.derived_from' in path or path == 'derived_from':
            return
        for pattern in CONTACT_PATTERNS:
            if pattern.search(value):
                problems.append({'field': path, 'value': value[:40],
                                 'problem': 'contains_contact_or_date_detail'})
                return
        lowered = value.lower()
        if any(f'{label}:' in lowered for label in IDENTITY_LABELS):
            problems.append({'field': path, 'value': value[:40], 'problem': 'contains_identity_label'})
        elif len(value) > MAX_TERM_CHARS:
            problems.append({'field': path, 'value': value[:40] + '...',
                             'problem': 'too_long_to_be_a_constraint'})

    scan({k: v for k, v in config.items() if k != '_comment'}, 'config')
    return problems


def structure_problems(config):
    """Structural and vocabulary problems, independent of privacy."""
    problems = []
    if not isinstance(config, dict):
        return [{'field': '_root', 'problem': 'not_an_object'}]
    if config.get('schema_version') != SCHEMA_VERSION:
        problems.append({'field': 'schema_version', 'value': config.get('schema_version'),
                         'problem': 'unsupported_schema_version'})

    location = config.get('location') or {}
    weight = location.get('score_weight')
    if weight != 0:
        # The single most important invariant in this file. A non-zero weight would
        # silently reintroduce a location penalty the product promises not to have.
        problems.append({'field': 'location.score_weight', 'value': weight,
                         'problem': 'location_must_carry_zero_score_weight'})

    seniority = config.get('seniority') or {}
    experience = seniority.get('commercial_experience')
    if experience is not None:
        if not isinstance(experience, dict):
            problems.append({'field': 'seniority.commercial_experience',
                             'value': type(experience).__name__, 'problem': 'not_an_object'})
        else:
            low = experience.get('minimum_months')
            high = experience.get('maximum_months')
            for name, value in (('minimum_months', low), ('maximum_months', high)):
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    problems.append({'field': f'seniority.commercial_experience.{name}',
                                     'value': value, 'problem': 'not_a_non_negative_integer'})
            if isinstance(low, int) and isinstance(high, int) and low > high:
                problems.append({'field': 'seniority.commercial_experience',
                                 'value': [low, high],
                                 'problem': 'minimum_months_exceeds_maximum_months'})
            if not isinstance(experience.get('ongoing_role'), bool):
                problems.append({'field': 'seniority.commercial_experience.ongoing_role',
                                 'value': experience.get('ongoing_role'),
                                 'problem': 'not_a_boolean'})
            for extra in sorted(set(experience) - {'minimum_months', 'maximum_months',
                                                   'ongoing_role',
                                                   'is_a_lower_bound_for_comparison'}):
                problems.append({'field': f'seniority.commercial_experience.{extra}',
                                 'problem': 'not_a_commercial_experience_field'})
    for field in ('review_from_years', 'hard_block_at_or_above_years'):
        value = seniority.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            problems.append({'field': f'seniority.{field}', 'value': value,
                             'problem': 'not_a_non_negative_integer_or_null'})
    review = seniority.get('review_from_years')
    hard = seniority.get('hard_block_at_or_above_years')
    # `hard_block_at_or_above_years` is INCLUSIVE and the name now says so: a stated
    # minimum EQUAL to it blocks. `review_from_years` must therefore be strictly
    # below it, or the same requirement would be both reviewable and blocked.
    if isinstance(review, int) and isinstance(hard, int) and review >= hard:
        problems.append({'field': 'seniority.review_from_years', 'value': review,
                         'problem': 'review_threshold_must_be_below_the_inclusive_hard_threshold',
                         'hard_block_at_or_above_years': hard})
    for level in seniority.get('acceptable_levels') or []:
        if str(level).lower() not in SENIORITY_LEVELS:
            problems.append({'field': 'seniority.acceptable_levels', 'value': level,
                             'problem': 'not_in_vocabulary'})
    overlap = {str(x).lower() for x in seniority.get('acceptable_levels') or []} & \
              {str(x).lower() for x in seniority.get('excluded_levels') or []}
    if overlap:
        problems.append({'field': 'seniority', 'value': sorted(overlap),
                         'problem': 'level_both_acceptable_and_excluded'})

    sponsorship = config.get('sponsorship') or {}
    category = sponsorship.get('current_status_category')
    if category is not None and category not in STATUS_CATEGORIES:
        problems.append({'field': 'sponsorship.current_status_category', 'value': category,
                         'problem': 'not_in_vocabulary'})

    salary = config.get('salary') or {}
    floor = salary.get('hard_floor')
    if floor is not None and (not isinstance(floor, (int, float)) or isinstance(floor, bool)
                              or floor < 0):
        problems.append({'field': 'salary.hard_floor', 'value': floor,
                         'problem': 'not_a_non_negative_number_or_null'})

    employment = config.get('employment') or {}
    clash = {str(x).lower() for x in employment.get('acceptable_types') or []} & \
            {str(x).lower() for x in employment.get('excluded_types') or []}
    if clash:
        problems.append({'field': 'employment', 'value': sorted(clash),
                         'problem': 'type_both_acceptable_and_excluded'})
    return problems


def config_problems(config):
    return structure_problems(config) + privacy_problems(config)


def load_config(path=None, required=True):
    path = Path(path) if path else CONFIG
    if not path.exists():
        if not required:
            return None
        raise config_error(
            f'Candidate config not found: {path}',
            'Generate one with: python tools/candidate_config.py build',
            'Or copy candidate/config.example.json and edit it.',
        )
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise config_error(f'Malformed candidate config: {path}',
                           f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None
    problems = config_problems(data)
    if problems:
        raise config_error('The candidate config is not valid.',
                           f'Problems: {json.dumps(problems, ensure_ascii=False)}',
                           'Ranking must not run against an invalid calibration.')
    return data


def compact(config):
    """The minimal calibration an evaluation prompt needs. No comments, no provenance."""
    seniority = config.get('seniority') or {}
    skills = config.get('skills') or {}
    return {
        'target_roles': config.get('target_roles', []),
        'adjacent_roles': config.get('adjacent_roles', []),
        'seniority': {k: seniority.get(k) for k in
                      ('target_level', 'acceptable_levels', 'excluded_levels',
                       'commercial_experience', 'review_from_years',
                       'hard_block_at_or_above_years')},
        'skills': {k: skills.get(k, []) for k in
                   ('primary_languages', 'frameworks', 'backend_capabilities',
                    'databases', 'secondary_skills')},
        'specialisms': config.get('specialisms', {}),
        'employment': {k: (config.get('employment') or {}).get(k, [])
                       for k in ('acceptable_types', 'excluded_types')},
        'location': {k: (config.get('location') or {}).get(k)
                     for k in ('market', 'preferred_locations', 'relocation_within_market',
                               'score_weight')},
        'sponsorship': {k: (config.get('sponsorship') or {}).get(k) for k in
                        ('eventual_sponsorship_required', 'current_status_category',
                         'licence_evidence_required', 'vacancy_specific_confirmation_required')},
        'salary': {k: (config.get('salary') or {}).get(k) for k in ('hard_floor', 'currency')},
        'working_pattern': {k: (config.get('working_pattern') or {}).get(k)
                            for k in ('acceptable', 'preferred')},
        'constraints': {'security_clearance_obtainable':
                        (config.get('constraints') or {}).get('security_clearance_obtainable')},
    }


def diff_configs(current, proposed):
    """Field-level differences between two configs, ignoring provenance and comments."""
    def flatten(node, path=''):
        out = {}
        if isinstance(node, dict):
            for key, value in node.items():
                if key == '_comment' or key == 'derived_from':
                    continue
                out.update(flatten(value, f'{path}.{key}' if path else key))
        elif isinstance(node, list):
            out[path] = list(node)
        else:
            out[path] = node
        return out

    a, b = flatten(current or {}), flatten(proposed or {})
    changes = []
    for field in sorted(set(a) | set(b)):
        before, after = a.get(field, '<absent>'), b.get(field, '<absent>')
        if before != after:
            changes.append({'field': field, 'current': before, 'proposed': after})
    return changes


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_build(args):
    profile_path = Path(args.profile) if args.profile else PROFILE
    config = build_config(read_profile(profile_path), profile_path)
    problems = config_problems(config)
    if problems:
        raise config_error('Refusing to write a candidate config that is not valid.',
                           f'Problems: {json.dumps(problems, ensure_ascii=False)}')

    target = Path(args.out) if args.out else CONFIG
    body = json.dumps(config, indent=2, ensure_ascii=False) + '\n'
    if target.exists() and not args.overwrite:
        # A config may have been hand-corrected after generation. Silently
        # regenerating it would discard that correction with no trace.
        proposed = target.with_suffix('.proposed.json')
        atomic_write_text(proposed, body)
        current = json.loads(target.read_text(encoding='utf-8'))
        print(json.dumps({
            'written': False,
            'proposed': display_path(proposed),
            'reason': 'a candidate config already exists',
            'changes': diff_configs(current, config),
            'hint': ('Review the proposal, then rerun with --overwrite to replace the '
                     'existing config. A hand-corrected calibration is never replaced '
                     'silently.'),
        }, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    atomic_write_text(target, body)
    print(json.dumps({
        'written': True,
        'config': display_path(target),
        'overwrote_existing': bool(args.overwrite and target.exists()),
        'profile_sha256': config['derived_from']['profile_sha256'],
        'unknown_fields': sorted(f for f, v in _flat_unknowns(config)),
    }, indent=2, ensure_ascii=False))


def _flat_unknowns(node, path=''):
    """Every field the profile did not establish, so the gaps are visible."""
    out = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ('_comment', 'derived_from'):
                continue
            out.extend(_flat_unknowns(value, f'{path}.{key}' if path else key))
    elif node is None:
        out.append((path, None))
    return out


def cmd_validate(args):
    path = Path(args.config) if args.config else CONFIG
    if not path.exists():
        print(json.dumps({'config': str(path), 'exists': False, 'valid': False,
                          'hint': 'Generate one with: python tools/candidate_config.py build'},
                         indent=2, ensure_ascii=False))
        raise SystemExit(1)
    data = json.loads(path.read_text(encoding='utf-8'))
    structure = structure_problems(data)
    privacy = privacy_problems(data)
    print(json.dumps({
        'config': str(path).replace('\\', '/'),
        'exists': True,
        'valid': not structure and not privacy,
        'structure_problems': structure,
        'privacy_problems': privacy,
        'unknown_fields': sorted(f for f, _ in _flat_unknowns(data)),
        # Reported beside validity, never folded into it. A range observed a while
        # ago is still a VALID calibration; it is simply one that now understates an
        # ongoing role and wants re-stating.
        'experience_staleness': experience_staleness(data),
    }, indent=2, ensure_ascii=False))
    raise SystemExit(0 if not structure and not privacy else 1)


def cmd_show(args):
    config = load_config(args.config)
    print(json.dumps(compact(config) if args.compact else config, indent=2, ensure_ascii=False))


def cmd_diff(args):
    path = Path(args.config) if args.config else CONFIG
    current = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    profile_path = Path(args.profile) if args.profile else PROFILE
    proposed = build_config(read_profile(profile_path), profile_path)
    changes = diff_configs(current, proposed)
    print(json.dumps({
        'config_exists': path.exists(),
        'changed_fields': len(changes),
        'changes': changes,
        'note': 'diff never writes. Use build --overwrite to apply.',
    }, indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description='Private candidate matching configuration')
    sub = p.add_subparsers(dest='cmd', required=True)

    b = sub.add_parser('build', help='Derive the config from the private profile.')
    b.add_argument('--profile', default='')
    b.add_argument('--out', default='')
    b.add_argument('--overwrite', action='store_true',
                   help='Replace an existing config. Without this a proposal is written instead.')
    b.set_defaults(func=cmd_build)

    v = sub.add_parser('validate', help='Validate structure, vocabulary and privacy.')
    v.add_argument('--config', default='')
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser('show', help='Show the config, or its compact matching form.')
    s.add_argument('--config', default='')
    s.add_argument('--compact', action='store_true')
    s.set_defaults(func=cmd_show)

    d = sub.add_parser('diff', help='Compare the stored config with a fresh derivation.')
    d.add_argument('--config', default='')
    d.add_argument('--profile', default='')
    d.set_defaults(func=cmd_diff)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

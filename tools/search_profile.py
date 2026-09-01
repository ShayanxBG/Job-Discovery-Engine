#!/usr/bin/env python3
"""Compact, discovery-only search terms derived from the private candidate profile.

`candidate/profile.md` is the private evidence file. It holds far more than a
search needs: a real name, a visa expiry, employment history, prose. A worker
never needs any of that to run a query, and copying the whole profile into a
worker prompt would be both wasteful and a privacy leak.

This module extracts a COMPACT SEARCH PROFILE: the handful of term lists a query
planner and a bounded worker actually use.

    target_titles        titles the candidate is actually aiming at
    adjacent_titles      generic engineering titles worth a body-validated search
    early_career_titles  junior/graduate/associate forms, only when the seniority
                         band supports them
    primary_languages    the language(s) the CALIBRATION calls primary
    frameworks           backend frameworks with real evidence behind them
    backend_capabilities the work itself: REST API, backend services, microservices
    database_terms       datastores with real evidence behind them
    integration_terms    integration/API-surface terms
    excluded_seniority   seniority words that are a hard mismatch
    excluded_specialisms primary specialisms this candidate is not applying for
    body_signals         cheap-gate vocabulary for body validation

EXTRACTION IS WHITELISTED, NOT REDACTED. Nothing is copied out of the profile
unless this module went looking for it in a named section, so a new private
paragraph cannot leak by default. That is the opposite of scanning the whole file
and trying to remove the sensitive parts afterwards.

ONE EXCEPTION, AND IT IS DELIBERATE: which language is PRIMARY comes from
`candidate/config.json`, the calibration, not from the profile's skills line. The
skills line is EVIDENCE and lists every language the candidate has ever used, so
searching all of them spends discovery budget on stacks the candidate is not
applying for, and it contradicts the `wrong_primary_language` blocker, which reads
the calibration. Two answers to "what is this candidate's primary language" is one
too many. The calibration wins; the rest of the skills line stays exactly where it
already lives, as `skills.secondary_skills` in the calibration and as prose in the
profile. The calibration is itself a term-only file with its own privacy boundary,
and `privacy_problems()` below still checks whatever comes out of it.

A second privacy boundary then checks the RESULT: `privacy_problems()` refuses an
output carrying an identity line, a contact detail, a date-shaped token or a
right-to-work phrase. Both `show` and `emit` run it, so a malformed profile that
put a phone number under `Languages:` is caught rather than shipped to a worker.

The extraction is deterministic. The same profile always produces the same terms
in the same order, so a query plan built from it is reproducible.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from search_strategy import load_strategy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'candidate' / 'profile.md'
# The calibration derived from that profile. It lives beside it, so an alternative
# profile path carries its own calibration and a test cannot silently read the real one.
CONFIG_NAME = 'config.json'
SCHEMA_VERSION = 1

# Term lists this module is allowed to emit. Anything else is not a search term.
TERM_FIELDS = (
    'target_titles', 'adjacent_titles', 'early_career_titles',
    'primary_languages', 'frameworks', 'backend_capabilities',
    'database_terms', 'integration_terms',
    'excluded_seniority', 'excluded_specialisms', 'body_signals',
)

# Skill lines worth reading, mapped to the slot they feed. The profile writes these
# as `- Languages: Python, SQL`, so the label is the contract, not the prose.
SKILL_LINE_SLOTS = {
    'languages': 'primary_languages',
    'backend': 'frameworks',
    'apis and integration': 'integration_terms',
    'api and integration': 'integration_terms',
    'database': 'database_terms',
    'data': 'database_terms',
}

# Terms that are real languages rather than markup, so a query never asks a board
# for "HTML developer" on the strength of a skills line.
NON_QUERYABLE_LANGUAGES = frozenset({'html', 'css'})

# Generic engineering titles worth an adjacent, body-validated search. These are
# search METHOD vocabulary, not candidate facts, so they belong in code beside the
# strategy rather than in the private profile.
ADJACENT_TITLES = (
    'Software Engineer', 'Software Developer', 'Integration Engineer',
    'API Engineer', 'Platform Engineer', 'Application Engineer',
    'Product Engineer',
)
EARLY_CAREER_TITLES = (
    'Junior Software Engineer', 'Junior Backend Developer',
    'Graduate Software Engineer', 'Associate Software Engineer',
    'Software Engineer I', 'Software Engineer II',
    'Entry Level Software Engineer',
)
BACKEND_CAPABILITIES = (
    'REST API', 'backend services', 'microservices', 'server-side',
)
SENIORITY_WORDS = ('senior', 'staff', 'principal', 'lead', 'head of', 'director',
                   'architect', 'manager')
SPECIALISM_WORDS = ('data scientist', 'data engineer', 'machine learning',
                    'ml engineer', 'quant', 'devops engineer', 'site reliability',
                    'frontend developer', 'front-end developer', 'mobile developer',
                    'ios developer', 'android developer', 'qa engineer',
                    'support engineer')

# The privacy boundary. A compact search profile is term lists only.
IDENTITY_LABELS = ('name', 'email', 'e-mail', 'phone', 'mobile', 'telephone',
                   'address', 'postcode', 'date of birth', 'dob', 'nationality',
                   'passport', 'national insurance')
RIGHT_TO_WORK_PHRASES = ('visa', 'graduate visa', 'right to work', 'sponsorship needed',
                         'expiry', 'skilled worker')
CONTACT_PATTERNS = (
    re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+'),                       # email
    re.compile(r'(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)'),          # phone-shaped
    re.compile(r'\b\d{4}-\d{2}-\d{2}\b'),                         # ISO date
    re.compile(r'\b\d{1,2}\s+\w+\s+(?:19|20)\d{2}\b'),            # 14 March 2028
    re.compile(r'\bhttps?://\S+', re.I),                          # any URL
)


def profile_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def read_profile(path=None):
    path = Path(path) if path else PROFILE
    if not path.exists():
        raise profile_error(
            f'Candidate profile not found: {path}',
            'Copy candidate/profile.example.md to candidate/profile.md and fill it in.',
        )
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise profile_error(f'Candidate profile could not be read: {path}',
                            f'{type(exc).__name__}: {exc}') from None


def config_beside(profile_path=None):
    """The calibration belonging to one profile: `config.json` in its directory."""
    return (Path(profile_path) if profile_path else PROFILE).parent / CONFIG_NAME


def calibrated_primary_languages(config_path=None):
    """The language(s) the calibration calls primary, or None when there is none.

    None means UNKNOWN, not "no languages": a workspace whose calibration has not
    been built yet must still be able to plan a search, so the caller falls back to
    the profile's leading language rather than to nothing at all.

    An unreadable or malformed calibration is treated the same way. This module is
    not a second validator for `candidate/config.json`; `candidate_config.py
    validate` and `preflight.py` already refuse a broken one, and failing the search
    profile here would only turn one clear error into two.
    """
    path = Path(config_path) if config_path else config_beside()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    values = ((data or {}).get('skills') or {}).get('primary_languages')
    if not isinstance(values, list):
        return None
    return dedupe(values) or None


def split_terms(value):
    """Split one skills line into trimmed terms, dropping parenthetical asides."""
    value = re.sub(r'\([^)]*\)', ' ', str(value or ''))
    parts = re.split(r'[,;/]| and ', value)
    out = []
    for part in parts:
        term = re.sub(r'\s+', ' ', part).strip(' .·-')
        if term and len(term) <= 40:
            out.append(term)
    return out


def dedupe(values):
    """Order-preserving case-insensitive dedupe, so extraction stays deterministic."""
    seen, out = set(), []
    for value in values:
        token = re.sub(r'\s+', ' ', str(value or '')).strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def section(text, heading_pattern):
    """The body of one markdown section, by heading regex."""
    lines = text.splitlines()
    out, capturing, level = [], False, 0
    for line in lines:
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
    """The value of a `- Label: value` bullet, or ''."""
    match = re.search(rf'^\s*[-*]\s*{re.escape(label)}\s*:\s*(.+)$', text, re.I | re.M)
    return re.sub(r'\s+', ' ', match.group(1)).strip() if match else ''


def titles_from_target(value):
    """Target job titles from the profile's target line.

    The line is prose ("Python backend / software developer with a backend and
    integrations specialism"), so this builds titles from the ROLE WORDS present
    rather than copying the sentence. Nothing outside the recognised vocabulary
    becomes a query.
    """
    lowered = (value or '').lower()
    languages = [name for token, name in (('python', 'Python'),) if token in lowered]
    roles = []
    for token, name in (('backend', 'Backend'), ('back-end', 'Backend'),
                        ('full stack', 'Full Stack'), ('full-stack', 'Full Stack'),
                        ('platform', 'Platform'), ('integration', 'Integration')):
        if token in lowered and name not in roles:
            roles.append(name)
    nouns = []
    for token, name in (('developer', 'Developer'), ('engineer', 'Engineer'),
                        ('software', 'Software')):
        if token in lowered and name not in nouns:
            nouns.append(name)
    if not nouns:
        nouns = ['Developer']

    titles = []
    for language in languages:
        for noun in nouns:
            if noun == 'Software':
                continue
            titles.append(f'{language} {noun}')
    for role in roles:
        for noun in nouns:
            if noun == 'Software':
                continue
            titles.append(f'{role} {noun}')
    for language in languages:
        for role in roles:
            for noun in nouns:
                if noun == 'Software':
                    continue
                titles.append(f'{language} {role} {noun}')
    if 'software' in lowered:
        for noun in ('Engineer', 'Developer'):
            if noun.lower() in lowered or not nouns:
                titles.append(f'Software {noun}')
        for language in languages:
            titles.append(f'Software Engineer {language}')
            titles.append(f'Software Developer {language}')
    return dedupe(titles)


def seniority_supports_early_career(value):
    """Whether the profile's seniority band genuinely supports early-career queries."""
    lowered = (value or '').lower()
    if any(word in lowered for word in ('junior', 'graduate', 'entry', 'associate', 'early career')):
        return True
    # "junior on the way to mid-level" is early-career; a mid-or-senior-only profile
    # must never be given graduate queries just because the family exists.
    return 'mid' in lowered and 'senior' not in lowered.split('senior roles are not')[0]


def build_search_profile(text, strategy=None, primary_languages=None):
    """The compact search profile, built only from whitelisted profile sections.

    `primary_languages` is the calibration's answer. None means it could not be
    read, and the fallback is the language the profile LEADS with, which is the
    same rule tools/candidate_config.py applies when deriving the calibration.
    """
    strategy = strategy or load_strategy()
    identity = section(text, r'identity and target') or text
    skills = section(text, r'technical skills')

    target_line = labelled_value(identity, 'Target') or labelled_value(identity, 'Target roles')
    seniority_line = labelled_value(identity, 'Seniority')

    slots = {field: [] for field in TERM_FIELDS}
    slots['target_titles'] = titles_from_target(target_line)

    for line in skills.splitlines():
        match = re.match(r'^\s*[-*]\s*([^:]{1,40}):\s*(.+)$', line)
        if not match:
            continue
        label = re.sub(r'\s+', ' ', match.group(1)).strip().lower()
        slot = SKILL_LINE_SLOTS.get(label)
        if not slot:
            continue
        for term in split_terms(match.group(2)):
            if slot == 'primary_languages' and term.lower() in NON_QUERYABLE_LANGUAGES:
                continue
            slots[slot].append(term)

    # Search on the language the candidate is actually applying for. The skills line
    # is evidence of everything they know; the calibration is the decision about
    # which of it is primary, and the same decision already drives the
    # wrong_primary_language blocker.
    slots['primary_languages'] = list(primary_languages or dedupe(slots['primary_languages'])[:1])

    slots['adjacent_titles'] = list(ADJACENT_TITLES)
    slots['backend_capabilities'] = list(BACKEND_CAPABILITIES)
    if seniority_supports_early_career(seniority_line):
        slots['early_career_titles'] = list(EARLY_CAREER_TITLES)
    slots['excluded_seniority'] = list(SENIORITY_WORDS)
    slots['excluded_specialisms'] = list(SPECIALISM_WORDS)
    slots['body_signals'] = list(strategy['body_signals']['backend_signals'])

    profile = {field: dedupe(slots[field]) for field in TERM_FIELDS}
    return {
        'schema_version': SCHEMA_VERSION,
        'seniority_band': 'early-career' if slots['early_career_titles'] else 'mid',
        **profile,
    }


def privacy_problems(profile):
    """Anything in a compact search profile that is not a search term.

    The extraction is already whitelisted, so this is the second boundary: it
    catches a profile that put private content under a heading this module reads,
    and it refuses to hand such an output to a worker.
    """
    problems = []
    if not isinstance(profile, dict):
        return [{'field': '_root', 'problem': 'not_an_object'}]
    allowed = set(TERM_FIELDS) | {'schema_version', 'seniority_band'}
    for field in sorted(set(profile) - allowed):
        problems.append({'field': field, 'problem': 'not_a_search_term_field'})
    for field in TERM_FIELDS:
        values = profile.get(field) or []
        if not isinstance(values, list):
            problems.append({'field': field, 'problem': 'not_a_list'})
            continue
        for value in values:
            token = str(value)
            lowered = token.lower()
            for pattern in CONTACT_PATTERNS:
                if pattern.search(token):
                    problems.append({'field': field, 'value': token,
                                     'problem': 'contains_contact_or_date_detail'})
                    break
            else:
                if any(f'{label}:' in lowered for label in IDENTITY_LABELS):
                    problems.append({'field': field, 'value': token,
                                     'problem': 'contains_identity_label'})
                elif any(phrase in lowered for phrase in RIGHT_TO_WORK_PHRASES):
                    problems.append({'field': field, 'value': token,
                                     'problem': 'contains_right_to_work_detail'})
                elif len(token) > 60:
                    problems.append({'field': field, 'value': token[:40] + '...',
                                     'problem': 'too_long_to_be_a_search_term'})
    return problems


def build_from_path(path=None, strategy=None):
    """The compact profile for one profile path, using the calibration beside it."""
    return build_search_profile(
        read_profile(path), strategy=strategy,
        primary_languages=calibrated_primary_languages(config_beside(path)))


def load_search_profile(path=None, strategy=None):
    """Build and privacy-check one compact search profile."""
    profile = build_from_path(path, strategy=strategy)
    problems = privacy_problems(profile)
    if problems:
        raise profile_error(
            'Refusing to emit a compact search profile containing non-search content.',
            f'Problems: {json.dumps(problems, ensure_ascii=False)}',
            'A compact search profile is term lists only. Identity, contact details, '
            'dates and right-to-work facts must never reach a discovery worker.',
        )
    return profile


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_show(args):
    print(json.dumps(load_search_profile(args.profile), indent=2, ensure_ascii=False))


def cmd_emit(args):
    """The minimal form a bounded worker receives: terms it needs, nothing else."""
    profile = load_search_profile(args.profile)
    fields = [f.strip() for f in args.fields.split(',') if f.strip()] if args.fields else [
        'target_titles', 'adjacent_titles', 'early_career_titles', 'primary_languages',
        'frameworks', 'backend_capabilities', 'database_terms', 'integration_terms',
        'excluded_seniority', 'excluded_specialisms',
    ]
    unknown = [f for f in fields if f not in TERM_FIELDS]
    if unknown:
        raise profile_error(f'Unknown search-term field(s): {", ".join(unknown)}',
                            f'Allowed: {", ".join(TERM_FIELDS)}')
    print(json.dumps({field: profile.get(field, []) for field in fields},
                     indent=2, ensure_ascii=False))


def cmd_check(args):
    profile = build_from_path(args.profile)
    problems = privacy_problems(profile)
    print(json.dumps({
        'profile': str(Path(args.profile) if args.profile else PROFILE),
        'clean': not problems,
        'term_counts': {field: len(profile.get(field, [])) for field in TERM_FIELDS},
        'problems': problems,
    }, indent=2, ensure_ascii=False))
    raise SystemExit(0 if not problems else 1)


def main():
    p = argparse.ArgumentParser(description='Compact discovery search terms from the private profile')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('show', help='Show the full compact search profile.')
    s.add_argument('--profile', default='')
    s.set_defaults(func=cmd_show)

    e = sub.add_parser('emit', help='Emit only the term fields a worker needs.')
    e.add_argument('--profile', default='')
    e.add_argument('--fields', default='', help='Comma separated search-term fields.')
    e.set_defaults(func=cmd_emit)

    c = sub.add_parser('check', help='Verify no private content reaches the search profile.')
    c.add_argument('--profile', default='')
    c.set_defaults(func=cmd_check)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Structured discovery candidate schema and worker-output contract.

A discovered vacancy is a structured record owned by deterministic Python, not a
paragraph the parent agent has to reinterpret. Controlled fields are validated at
the boundary and obvious values are normalised.

Two rules govern normalisation:

1. Unknown stays unknown. Nothing here guesses a salary, a date, a currency, a
   work pattern or an experience requirement that the source did not state.
2. A page-level date filter is not evidence. Freshness is decided per candidate
   from its own posted date or its own visible posted age, because promoted cards
   on some boards ignore the active posted-within filter.

This module also owns three deterministic gates that run BEFORE any expensive
reasoning, because broad adjacent-title searching only pays off if the cheap work
happens first:

  body_signal_gate  A generic "Software Engineer" advert is interesting only when
                    its body shows backend/application/API work is genuinely
                    central. One incidental keyword is never enough, so the gate
                    demands several DISTINCT signals and weighs counter-signals
                    from a different specialism. It returns KEEP_FOR_DEEP_CHECK or
                    LOW_SIGNAL, and HARD_REJECT only where an existing
                    deterministic blocker already applies. It is a gate, not a
                    score.
  consolidate       LinkedIn, Indeed, public web and an employer careers page all
                    find the same vacancy. Fetching it four times is pure waste, so
                    sightings that PROVABLY share an identifier are merged and the
                    most authoritative source becomes the primary, with secondary
                    identities kept as evidence. Merging requires published
                    identifier evidence, never a resemblance: candidates that merely
                    share a company, title and location are reported as possible
                    duplicates and both stay in the run, because a false merge
                    removes a real vacancy before anybody examines it and nothing
                    downstream can detect the loss.
  validate_query_task  A worker receives a BOUNDED query task and returns. It does
                    not decide its own search expansion, which is what stops one
                    background worker burning an enormous budget.
"""
import argparse
import hashlib
import html
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import (  # noqa: E402
    EMPLOYMENT_TYPES, FACT_FIELDS, FIT_BANDS, LEAD_TYPES, SOURCE_CONFIDENCES,
    SOURCE_TYPES, SPONSORSHIP_LABELS, WORK_PATTERNS, canon_host, facts_problems,
    norm_url, source_host,
)
from sources import (  # noqa: E402
    SOURCE_OUTCOMES, filter_is_trustworthy, forbidden_panel_hits, is_known_source,
    load_registry, promoted_card_markers, source_family, state_source_type_for,
)
from search_strategy import is_known_family, load_strategy  # noqa: E402
from employers import employer_key  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

SCHEMA_VERSION = 1

# Cheap body-gate verdicts. HARD_REJECT is reserved for an existing deterministic
# blocker and is never inferred from body signals alone.
BODY_VERDICTS = ('KEEP_FOR_DEEP_CHECK', 'LOW_SIGNAL', 'HARD_REJECT')

# Source authority for cross-source consolidation, strongest first. The winner
# becomes the primary record; the others survive as secondary evidence.
SOURCE_TYPE_AUTHORITY = ('employer-ats', 'employer-direct', 'authenticated-board',
                         'uk-board', 'aggregator', 'sponsor-board', 'public-web', 'unknown')

RESOLUTION_OUTCOMES = ('resolved_official', 'resolved_ats', 'unresolved', 'agency',
                       'employer_unknown')

# Evidence strong enough to merge two sightings automatically, before either has
# been deep fetched. Every entry is an identifier a source actually published.
# `company_title_location` is deliberately absent: it is a resemblance, not an id.
SAFE_MERGE_EVIDENCE = ('canonical_url', 'requisition_id', 'source_job_id', 'resolution_link')

# Coarse locality normalisation used ONLY to decide whether two candidates are
# worth SHOWING to a human as a possible duplicate. It is deliberately not part of
# SAFE_MERGE_EVIDENCE and nothing in the merge path calls it.
#
# The first real run made the need obvious: one Mustard Systems vacancy appeared as
# "London" on Built In and "London, England" on Workable, and one Lyst vacancy as
# "London, England" and "London, United Kingdom". Both pairs were genuinely the same
# vacancy, and neither was even flagged for review, because the raw location strings
# differed. Widening the HINT costs a human one glance; widening the MERGE would
# silently delete real vacancies, which is why these stay on opposite sides.
_LOC_NATION = {'united kingdom', 'uk', 'u.k.', 'gb', 'great britain', 'england',
               'scotland', 'wales', 'northern ireland'}
_LOC_LEAD = re.compile(r'^(?:hybrid\s+work\s+in|remote\s+in|on[- ]site\s+in|'
                       r'work\s+from|based\s+in)\s+')
_LOC_NOISE = re.compile(r'\b(?:area|region|metropolitan\s+area|and\s+surrounding\s+\w+)\b')
_LOC_PARENS = re.compile(r'\s*\([^)]*\)')
_LOC_POSTCODE = re.compile(r'\b[a-z]{1,2}\d[a-z\d]*\b')


BODY_SIGNAL_EXIT = {'KEEP_FOR_DEEP_CHECK': 0, 'LOW_SIGNAL': 1, 'HARD_REJECT': 2}

# --------------------------------------------------------------------------
# Sponsorship wording. Negation is checked across the WHOLE statement before any
# positive form is considered.
#
# The first real run classified "No sponsorship available" as STRONG positive
# evidence, because the negative pattern required the words "is not available"
# while the positive pattern matched the bare pair "sponsorship ... available".
# A vacancy that refuses sponsorship was therefore reported as one that offers it,
# which is the single most damaging mistake this workspace can make about a
# sponsorship-dependent candidate.
# --------------------------------------------------------------------------
_SPON_SENTENCE = re.compile(
    r'[^.\n]*\b(?:visa|sponsor\w*|right to work|skilled worker|work permit)\b[^.\n]*',
    re.I)
_SPON_NEGATIVE = re.compile(
    r'\bno\s+(?:visa\s+)?sponsorship\b'
    r'|\bsponsorship\s+(?:is\s+)?(?:not|un)available\b'
    r'|\bsponsorship\s+is\s+not\b'
    r'|\bnot\s+(?:be\s+)?(?:able|in\s+a\s+position)\s+to\s+sponsor\b'
    r'|\b(?:cannot|can\s?not|can\'t|unable\s+to|won\'?t|will\s+not|do\s+not|does\s+not'
    r'|not\s+able\s+to)\s+(?:currently\s+)?(?:offer|provide|support|consider|sponsor)\b'
    r'|\bwithout\s+(?:the\s+need\s+for\s+)?sponsorship\b'
    r'|\bmust\s+(?:already\s+)?(?:have|hold)\s+(?:the\s+)?(?:full\s+|unrestricted\s+)?'
    r'right\s+to\s+work\b'
    r'|\buk\s+applicants\s+only\b'
    r'|\bwill\s+not\s+be\s+considered\b'
    r'|\bwill\s+need\s+(?:the\s+)?right\s+to\s+work\b'
    r'|\bno\s+visa\b',
    re.I)
# Route and permission qualifiers that may sit between the verb and the noun.
# Deliberately a CLOSED list rather than a wildcard bridge: `we provide details on
# sponsorship` must not read as an offer, and a wildcard of any useful width does
# exactly that.
_SPON_QUALIFIER = (r'(?:uk|full|visa|work|working|employment|immigration'
                   r'|skilled[\s-]?worker|tier\s*2)')
# A positive claim must be an actual OFFER TO SPONSOR THIS ROLE, never the
# incidental co-occurrence of "sponsorship" and "available" in one sentence, and
# never an organisation-level licence claim.
_SPON_POSITIVE = re.compile(
    r'\bwe\s+(?:can|do|will|would|are\s+able\s+to|are\s+happy\s+to|are\s+willing\s+to'
    r'|are\s+prepared\s+to|are\s+open\s+to)\s+sponsor\b'
    rf'|\bwe\s+(?:can\s+|will\s+|do\s+|would\s+|are\s+able\s+to\s+|are\s+happy\s+to\s+'
    rf'|are\s+willing\s+to\s+)?(?:offer|provide)\s+(?:{_SPON_QUALIFIER}\s+){{0,3}}sponsorship\b'
    rf'|\b(?:{_SPON_QUALIFIER}\s+){{0,3}}sponsorship\s+'
    r'(?:is\s+|will\s+be\s+|can\s+be\s+|are\s+)?(?:available|offered|provided|supported)\b',
    re.I)
# An organisation LICENCE claim. A licence means the EMPLOYER holds one; it is not
# a statement that this vacancy will be sponsored, that the role meets the going
# rate or skill level, or that the licence is still valid today. The official
# GOV.UK register grants exactly this fact only `moderate` support and always sets
# `requires_live_check`, so the same fact asserted in an advert cannot outrank it.
#
# The first version of this module put `we are a licensed sponsor` inside the
# POSITIVE pattern and returned `strong` with the note "the vacancy explicitly
# offers sponsorship", which the advert had not said. Worse, it was inconsistent:
# `we hold a sponsor licence` fell through to `unknown`, so the label depended on
# phrasing rather than on what was claimed.
_SPON_LICENCE = re.compile(
    r'\blicen[cs]ed\s+sponsor\b'
    r'|\bsponsor(?:ship)?\s+licen[cs]e\b'
    r'|\b(?:approved|registered|accredited)\s+sponsor\b'
    r'|\bregister\s+of\s+licen[cs]ed\s+sponsors\b'
    r'|\bhome\s+office\s+(?:approved|registered|licen[cs]ed)\b',
    re.I)


def sponsorship_signal(text):
    """Read sponsorship wording out of a vacancy body.

    Returns `unknown`, `blocked`, `moderate` or `strong` with the sentences that
    decided it, in that order of precedence:

      blocked   the vacancy refuses sponsorship. Negation ALWAYS wins, even when
                the same sentence carries words a positive pattern would match.
      strong    the vacancy offers to sponsor. Vacancy-level evidence.
      moderate  the employer claims a sponsor LICENCE. Organisation-level
                capability only, so it carries `requires_live_check` and can never
                reach `strong` on its own.
      unknown   sponsorship is mentioned but neither offered nor refused, or not
                mentioned at all. Silence is not refusal.
    """
    sentences = [s.strip() for s in _SPON_SENTENCE.findall(text or '')]
    sentences = [s for s in sentences if 0 < len(s) < 400]
    if not sentences:
        return {'label': 'unknown', 'evidence': [], 'negated': [], 'positive': [],
                'licence': [], 'requires_live_check': False,
                'note': 'No sponsorship wording found. Unknown is not a negative: '
                        'silence is not refusal.'}
    negated = [s for s in sentences if _SPON_NEGATIVE.search(s)]
    if negated:
        return {'label': 'blocked', 'evidence': negated[:3], 'negated': negated[:3],
                'positive': [], 'licence': [], 'requires_live_check': False,
                'note': 'The vacancy states it does not offer sponsorship.'}
    positive = [s for s in sentences if _SPON_POSITIVE.search(s)]
    licence = [s for s in sentences if _SPON_LICENCE.search(s)]
    if positive:
        # An explicit offer outranks a licence claim in the same advert, because it
        # is evidence about THIS vacancy rather than about the organisation.
        return {'label': 'strong', 'evidence': positive[:3], 'negated': [],
                'positive': positive[:3], 'licence': licence[:3],
                'requires_live_check': False,
                'note': 'The vacancy explicitly offers sponsorship. This is vacancy '
                        'evidence and still says nothing about the going rate.'}
    if licence:
        return {'label': 'moderate', 'evidence': licence[:3], 'negated': [], 'positive': [],
                'licence': licence[:3], 'requires_live_check': True,
                'note': 'The employer claims a sponsor LICENCE. That is organisation-level '
                        'capability, not a statement that this vacancy will be sponsored, '
                        'that the role meets the going rate or skill level, or that the '
                        'licence is still valid today. Verify before decision-critical use.'}
    return {'label': 'unknown', 'evidence': sentences[:3], 'negated': [], 'positive': [],
            'licence': [], 'requires_live_check': False,
            'note': 'Sponsorship is mentioned but neither offered nor refused.'}


# --------------------------------------------------------------------------
# Search-platform chrome around a vacancy body.
#
# `description_text` is the SELECTED VACANCY'S OWN JOB-DESCRIPTION BODY and
# nothing else. A LinkedIn job page appends its own furniture after the advert:
#
#     ...end of the employer's text
#     Show more / Show less
#     -  Seniority level    Mid-Senior level
#     -  Employment type    Contract
#     -  Job function       Information Technology
#     -  Industries         Legal Services and Law Practice
#     Referrals increase your chances of interviewing at <Company> by 2x
#     See who you know
#
# All 22 LinkedIn-sourced entries in the first production cache carried it. That
# matters beyond tidiness: `Seniority level` and `Employment type` are LinkedIn's
# OWN classification of the role, not the employer's words, and they are exactly
# the fields that drive the `seniority` and `contract` hard blockers. Reading
# "Employment type: Contract" out of a cached body attributes to the employer a
# statement it never made. The block also enters `description_hash`, so a change
# in LinkedIn's furniture would read as a changed advert.
#
# The chrome is therefore SPLIT OFF rather than deleted: the platform's own
# classification is kept, but under its own key with its own provenance, so a
# later reader can never mistake it for employer-stated evidence.
#
# Only sources whose pages are known to carry this furniture are trimmed. A
# Lever advert legitimately contains the word "Industries", so a blanket rule
# would cut real job descriptions short.
#
# AND WHEN THERE IS NOTHING BUT CHROME, THE DESCRIPTION IS UNAVAILABLE.
#
# An extraction that returned only LinkedIn's own block did not find the advert;
# it failed. The documented rule for that case has always been to cache nothing
# and record the description as unavailable, because an absent description is a
# known unknown while a page-level capture is silent contamination. Keeping the
# chrome "so an over-eager cut cannot destroy the vacancy" inverted exactly that
# rule: there was no vacancy text to protect, and what got stored as the
# vacancy's description was `Seniority level / Mid-Senior level` written by
# LinkedIn. Preserving that is the contamination the rule exists to prevent.
# --------------------------------------------------------------------------
PLATFORM_CHROME_SOURCES = ('linkedin', 'authenticated-linkedin')
PLATFORM_CHROME_HOSTS = ('linkedin.com',)

# LinkedIn's own classification of the role, label then value.
_LI_PLATFORM_FIELDS = (
    ('seniority_level', r'Seniority\s+level'),
    ('employment_type', r'Employment\s+type'),
    ('job_function', r'Job\s+function'),
    ('industries', r'Industries'),
)
# A metadata label standing alone on its own line. On LinkedIn the employer's text
# is prose; this label-only form is the platform's.
_LI_LABEL_LINE = re.compile(
    r'^[ \t]*(?:Seniority\s+level|Employment\s+type|Job\s+function|Industries)[ \t]*$',
    re.I)
# Interface furniture that can never begin a paragraph an employer wrote.
_LI_MARKER_LINE = re.compile(
    r'^[ \t]*(?:Show\s+more|Show\s+less|See\s+who\s+you\s+know)[ \t]*$'
    r'|^[ \t]*Referrals\s+increase\s+your\s+chances\s+of\s+interviewing\b'
    r'|^[ \t]*Get\s+notified\s+about\s+new\b'
    r'|^[ \t]*(?:Similar\s+jobs|People\s+also\s+viewed|More\s+jobs\s+from)\b',
    re.I)
# Separators LinkedIn renders between metadata rows.
_LI_SEPARATOR_LINE = re.compile(r'^[ \t]*(?:[-–—·•]|)[ \t]*$')


def carries_platform_chrome(source_id='', source_host=''):
    """Whether this source's vacancy pages are known to append platform furniture."""
    sid = collapse(source_id).lower()
    host = collapse(source_host).lower()
    return (sid in PLATFORM_CHROME_SOURCES
            or any(host == h or host.endswith('.' + h) for h in PLATFORM_CHROME_HOSTS))


def _chrome_start(lines):
    """Index of the first line from which EVERYTHING that follows is chrome.

    Structural rather than keyword-based, and that distinction is load bearing in
    both directions. A trailing block is only chrome if the whole tail parses as
    chrome, so an employer paragraph after a marker means the marker was the
    employer's word and nothing is cut. Equally, a bare `Employment type` block
    with nothing after it IS the platform's, even with no `Show more` above it.

    Returns None when no such point exists.
    """
    def tail_is_pure_chrome(start):
        i = start
        saw_chrome = False
        while i < len(lines):
            line = lines[i]
            if _LI_SEPARATOR_LINE.match(line):
                i += 1
                continue
            if _LI_MARKER_LINE.match(line):
                saw_chrome = True
                i += 1
                continue
            if _LI_LABEL_LINE.match(line):
                saw_chrome = True
                i += 1
                # Consume blank lines then exactly one value line.
                while i < len(lines) and not lines[i].strip():
                    i += 1
                if i < len(lines) and not _LI_LABEL_LINE.match(lines[i]) \
                        and not _LI_MARKER_LINE.match(lines[i]) \
                        and not _LI_SEPARATOR_LINE.match(lines[i]):
                    i += 1
                continue
            return False
        return saw_chrome

    for index, line in enumerate(lines):
        if _LI_MARKER_LINE.match(line) or _LI_LABEL_LINE.match(line):
            if tail_is_pure_chrome(index):
                return index
    return None


def split_platform_chrome(text, source_id='', source_host=''):
    """Separate a vacancy body from the search platform's own page furniture.

    Returns `{'description', 'description_unavailable', 'platform_metadata',
    'chrome_removed', 'cut_at', 'provenance'}`.

    `description` is the employer's text ONLY. When the extraction contained
    nothing but the platform's own block, `description` is empty and
    `description_unavailable` is True: the job description was not isolated, and
    an absent description is a known unknown while platform chrome stored as a
    vacancy body is silent contamination.

    The platform's classification is still preserved under `platform_metadata`,
    labelled with the source that asserted it, and is NEVER employer-stated
    evidence.

    A source not known to append chrome is returned untouched, because guessing
    would truncate real job descriptions.
    """
    body = text or ''
    result = {'description': body, 'description_unavailable': False,
              'platform_metadata': {}, 'chrome_removed': False, 'cut_at': '',
              'provenance': ''}
    if not body.strip() or not carries_platform_chrome(source_id, source_host):
        return result

    lines = body.splitlines()
    start = _chrome_start(lines)
    if start is None:
        return result

    head = '\n'.join(lines[:start]).rstrip()
    tail = '\n'.join(lines[start:])

    metadata = {}
    for field, label in _LI_PLATFORM_FIELDS:
        found = re.search(rf'^[ \t]*{label}[ \t]*$\s*\n+[ \t]*(?P<value>[^\n]+)',
                          tail, re.I | re.M)
        if found:
            value = collapse(found.group('value'))
            if value and not _LI_SEPARATOR_LINE.match(value) \
                    and not _LI_LABEL_LINE.match(value) and not _LI_MARKER_LINE.match(value):
                metadata[field] = value

    provenance = f'{collapse(source_id).lower() or "linkedin"} platform metadata'
    cut_at = collapse(lines[start])[:60]

    if not head.strip():
        # Nothing but chrome. The isolation FAILED; it did not succeed with an
        # unusual result. Returning the block as the description would store
        # LinkedIn's own words as the employer's.
        result.update({
            'description': '', 'description_unavailable': True,
            'platform_metadata': metadata, 'chrome_removed': True,
            'cut_at': cut_at, 'provenance': provenance,
            'reason': 'no employer job-description body was found before the platform '
                      'block, so the vacancy description was never isolated',
        })
        return result

    result.update({
        'description': head + '\n',
        'description_unavailable': False,
        'platform_metadata': metadata,
        'chrome_removed': True,
        'cut_at': cut_at,
        'provenance': provenance,
    })
    return result


# --------------------------------------------------------------------------
# HTML to text. Entities are unescaped BEFORE tags are stripped, then once more
# afterwards for entities that were themselves encoded.
#
# The first real run cached a Greenhouse body still containing literal <p> tags,
# because the converter stripped tags first and unescaped entities second, so
# `&lt;p&gt;` became `<p>` only after the tag stripper had already run.
# --------------------------------------------------------------------------
_HTML_DROP = re.compile(r'<(script|style|noscript)[^>]*>[\s\S]*?</\1>', re.I)
_HTML_BREAK = re.compile(r'<br\s*/?>', re.I)
_HTML_BLOCK = re.compile(r'</(p|div|li|h[1-6]|tr|section|article)>', re.I)
_HTML_LI = re.compile(r'<li[^>]*>', re.I)
_HTML_TAG = re.compile(r'<[^>]+>')


def html_to_text(raw):
    """Convert a vacancy-description fragment to plain text.

    Order is the whole point: unescape, strip, unescape again, and only then
    verify that nothing tag-shaped survived. Deliberately not a general-purpose
    parser; it handles the description fragments ATS APIs and job pages return.
    """
    if not raw:
        return ''
    text = html.unescape(str(raw))
    text = _HTML_DROP.sub(' ', text)
    text = _HTML_BREAK.sub('\n', text)
    text = _HTML_BLOCK.sub('\n', text)
    text = _HTML_LI.sub('\n- ', text)
    text = _HTML_TAG.sub(' ', text)
    # A second pass catches entities that were themselves entity-encoded, e.g.
    # `&amp;lt;p&amp;gt;`, which the first unescape turns into `&lt;p&gt;`.
    text = html.unescape(text)
    text = _HTML_TAG.sub(' ', text)
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = '\n'.join(line.strip() for line in text.split('\n'))
    return re.sub(r'\n{3,}', '\n\n', text).strip()


# --------------------------------------------------------------------------
# Title-level deterministic blockers.
#
# Every pattern here was a live miss in the first real run. Each is anchored on a
# word boundary rather than a substring, because the cheap gates run before anyone
# reads the posting and a false positive here deletes a real vacancy unseen.
# --------------------------------------------------------------------------
# `quant`, `quants`, `quantitative` - but never `quantum`, which is a different
# industry entirely and was present in this very run (IonQ, Octric Semiconductors).
_T_QUANT = re.compile(r'\bquant(?:itative|s)?\b', re.I)
_T_SENIORITY = re.compile(
    r'\b(?:senior|snr|sr|staff|principal|lead|head\s+of|director|architect|manager'
    r'|vice\s+president|vp|svp|evp|chief|distinguished|fellow)\b', re.I)
# --------------------------------------------------------------------------
# Primary-language identification, driven by the candidate's own configuration.
#
# The first version hard-coded a single `node.js` pattern, so `Golang Engineer`,
# `C# .NET Developer` and `Java Backend Engineer` all survived the cheap gate and
# each cost a deep fetch. The fix is NOT broader substring matching: the gate runs
# before anyone reads the posting, so a false positive here deletes a real vacancy
# unseen, and that is a far worse failure than a wasted fetch.
#
# So the rule stays narrow and is stated positively. A title is blocked only when
# it NAMES a language ecosystem, and NONE of the candidate's own configured
# primary languages or frameworks is named alongside it. `Python / Node.js
# Developer` names both, so it is not blocked; `Software Engineer` names neither,
# so it goes to the body gate where it belongs.
#
# The candidate's side comes from candidate_config.skills, never from a constant
# here, so a calibration change moves this gate with it.
# --------------------------------------------------------------------------
LANGUAGE_ECOSYSTEMS = {
    'python': r'python|django|fastapi|flask|pyramid|pytest',
    'javascript': r'javascript|node\.?js|node|typescript|react|angular|vue|express|nest\.?js|deno',
    'java': r'java|spring(?:\s*boot)?|kotlin|jvm|hibernate|quarkus',
    'csharp': r'c#|c\s?sharp|\.net|dotnet|asp\.net|blazor',
    # Bare `go` is an ordinary English word, so it only counts when a role noun
    # follows it. `Go Developer` is a Golang role; `Go-To-Market Engineer` is not,
    # and this gate drops vacancies before anyone reads them.
    'go': r'golang|go(?=\s+(?:developer|engineer|dev|programmer|backend|microservices?))',
    'ruby': r'ruby|rails',
    'php': r'php|laravel|symfony|drupal',
    'rust': r'rust\b|rustlang',
    'scala': r'scala|akka',
    'elixir': r'elixir|phoenix',
    'cpp': r'c\+\+|cpp\b',
    'perl': r'perl\b',
    'swift': r'swift\b|swiftui',
    'salesforce': r'salesforce|apex\b|visualforce',
    'abap': r'\babap\b|sap\s+abap',
}
# The alternation MUST be grouped. Ungrouped, `(?<!...)golang|go\b(?!...)` splits
# into two whole-regex alternatives, so the word-boundary guard stops protecting
# `go` and it matches inside `Django` - which quietly put Go in the candidate's own
# ecosystem set and made every Go title unblockable.
_ECOSYSTEM_PATTERNS = {
    name: re.compile(rf'(?<![\w#+.])(?:{pattern})(?![\w#+])', re.I)
    for name, pattern in LANGUAGE_ECOSYSTEMS.items()
}
# Terms that name a language ecosystem but not as the role's own stack.
_T_PYTHON = _ECOSYSTEM_PATTERNS['python']


def _ecosystems_named(text):
    """Every configured language ecosystem this text names."""
    return {name for name, pattern in _ECOSYSTEM_PATTERNS.items() if pattern.search(text or '')}


def named_language_ecosystems(text):
    """Public reading of which language ecosystems a piece of vacancy text names.

    The cheap title gate and the deterministic `wrong_primary_language` blocker
    check ask the same question of different text, so they ask it of the same
    patterns. A second set kept elsewhere would drift.
    """
    return _ecosystems_named(text)


def candidate_ecosystems(config):
    """The language ecosystems the candidate's OWN configured stack belongs to.

    Derived from `skills.primary_languages` and `skills.frameworks`, so this gate
    follows the calibration rather than restating it. Returns an empty set when the
    profile never established a primary language, and an empty set can never block:
    unknown is not a refusal.
    """
    skills = (config or {}).get('skills') or {}
    terms = []
    for field in ('primary_languages', 'frameworks'):
        values = skills.get(field) or []
        if isinstance(values, list):
            terms.extend(str(v) for v in values)
    owned = set()
    for term in terms:
        owned |= _ecosystems_named(term)
    return owned
# UNAMBIGUOUS independent contracting only.
#
# Two kinds of wording used to sit in here that should never have. `FTC` and
# `fixed term` name a directly employed role with an end date, and the bare word
# `contract` names nothing at all: `Software Engineer, 12-month contract` is a
# fixed-term PAYE job about as often as it is a contractor one. This gate runs
# before anyone reads the advert, so deleting either on a title deletes a genuine
# sponsored job for a contractor's reasons.
#
# What stays is only wording that cannot mean direct employment: an engagement
# priced by the day, an IR35 status, an umbrella arrangement, a person supplying
# their own labour. Official sponsor guidance refuses a Certificate of Sponsorship
# for supplying a worker to a third party, which is what these describe, and
# prohibits nothing about employing someone for a fixed period.
_T_CONTRACT = re.compile(
    r'\bday\s*[- ]?\s*rate\b|\bdaily\s+rate\b'
    r'|\b(?:inside|outside)\s+ir35\b'
    r'|\bcontractor\b|\bsub[- ]?contractor\b'
    r'|\bcontract(?:ing)?\s+engagement\b'
    r'|\bfreelanc(?:e|er|ing)\b'
    r'|\bself[- ]employed\b|\bsole\s+trader\b'
    r'|\bumbrella\s+(?:company|contract)\b'
    r'|\bconsultancy\s+engagement\b|\bconsulting\s+engagement\b', re.I)

# Recognised, reported, and deliberately NOT a gate.
_T_FIXED_TERM = re.compile(r'\bftc\b|\bfixed[- ]term\b|\bfixed\s+term\b', re.I)
# AMBIGUOUS. `contract` on its own, a duration plus `contract`, an interim posting
# and a secondment can each describe direct employment or independent work. None
# decides anything, and each raises an employment_type verification need.
_T_AMBIGUOUS_CONTRACT = re.compile(
    r'\bcontract\b|\binterim\b|\bsecondment\b', re.I)


def names_fixed_term(text):
    """Whether text describes a fixed-term engagement.

    Public so discovery can LABEL a role without blocking it. A fixed-term direct
    role is scored conservatively on duration, stability, salary and employer
    identity; it is not eliminated, and it must never be read as contracting.
    """
    return bool(_T_FIXED_TERM.search(text or ''))


def names_independent_contracting(text):
    """Whether text unambiguously describes a non-employment engagement.

    This is what a `contract` hard blocker has to be able to point at. The bare
    word `contract` is deliberately not enough, here or anywhere else.
    """
    return bool(_T_CONTRACT.search(text or ''))


def contract_wording(text):
    """How a piece of text describes the engagement, without deciding it.

    Returns `independent`, `fixed_term`, `ambiguous` or `''`. `ambiguous` is a real
    answer and the most common one: the wording is compatible with direct
    fixed-term employment AND with contracting, so the employment type is a
    verification need rather than a filter. Nothing here infers permanence,
    fixed-term status or contracting from the word `contract` alone.
    """
    body = text or ''
    if _T_CONTRACT.search(body):
        return 'independent'
    if _T_FIXED_TERM.search(body):
        return 'fixed_term'
    if _T_AMBIGUOUS_CONTRACT.search(body):
        return 'ambiguous'
    return ''
_T_CLEARANCE = re.compile(
    r'\b(?:dv|sc|ctc|nppv\d?)[\s-]*(?:security[\s-]*)?clear(?:ed|ance)\b'
    r'|\bsecurity\s+clearance\b|\bdeveloped\s+vetting\b', re.I)
_T_APPRENTICE = re.compile(r'\bapprentice(?:ship)?\b|\bplacement\s+programme\b', re.I)


def names_security_clearance(text):
    """Whether text states a security-clearance requirement.

    Same pattern the cheap title gate uses, exposed so the `security_clearance`
    hard-blocker precondition can check the employer's own wording against one
    definition rather than a second copy of it.
    """
    return bool(_T_CLEARANCE.search(text or ''))


def _config_enables(config, dotted, truthy_only=False):
    """Walk a dotted candidate-config path. Null/empty means the profile never
    established it, which is UNKNOWN and therefore never a blocker."""
    value = config or {}
    for part in dotted.split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    if truthy_only:
        return value is not None and value is not False and value != []
    return value not in (None, '', [], {})


def title_blockers(title, candidate_config=None):
    """Deterministic blockers decidable from a job TITLE alone.

    Returns `{'blocked': bool, 'reason_code': str|'', 'evidence': str, 'checked': [...]}`.
    Calibration-gated where the candidate config governs the constraint, so a
    blocker the profile never established can never fire here either.
    """
    config = candidate_config
    if config is None:
        path = ROOT / 'candidate' / 'config.json'
        config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    text = collapse(title)
    checked = ['seniority', 'quant_specialism', 'apprenticeship', 'contract',
               'security_clearance', 'wrong_primary_language']
    if not text:
        return {'blocked': False, 'reason_code': '', 'evidence': '', 'checked': checked,
                'verification': '', 'employment_wording': ''}

    m = _T_SENIORITY.search(text)
    if m:
        return {'blocked': True, 'reason_code': 'seniority', 'evidence': m.group(0),
                'checked': checked}

    m = _T_QUANT.search(text)
    if m:
        return {'blocked': True, 'reason_code': 'wrong_specialism',
                'evidence': m.group(0), 'checked': checked,
                'note': 'Quantitative finance. Deliberately does not match "quantum".'}

    m = _T_APPRENTICE.search(text)
    if m:
        return {'blocked': True, 'reason_code': 'apprenticeship',
                'evidence': m.group(0), 'checked': checked}

    # Employment type is governed by the candidate's own excluded types.
    excluded_types = {str(t).lower() for t in
                      ((config.get('employment') or {}).get('excluded_types') or [])}
    if 'contract' in excluded_types:
        m = _T_CONTRACT.search(text)
        if m:
            return {'blocked': True, 'reason_code': 'contract', 'evidence': m.group(0),
                    'checked': checked}

    # Clearance only blocks when the calibration says the candidate cannot obtain
    # it. Null means unknown, and unknown is not a refusal.
    if (config.get('constraints') or {}).get('security_clearance_obtainable') is False:
        m = _T_CLEARANCE.search(text)
        if m:
            return {'blocked': True, 'reason_code': 'security_clearance',
                    'evidence': m.group(0), 'checked': checked}

    # A different primary language blocks only when the title names a language
    # ecosystem AND names none of the candidate's own. Driven by the calibration:
    # with no configured primary language the set is empty and this can never fire,
    # because unknown is not a refusal.
    owned = candidate_ecosystems(config)
    if owned:
        named = _ecosystems_named(text)
        foreign = named - owned
        if foreign and not (named & owned):
            m = _ECOSYSTEM_PATTERNS[sorted(foreign)[0]].search(text)
            return {'blocked': True, 'reason_code': 'wrong_primary_language',
                    'evidence': m.group(0) if m else sorted(foreign)[0],
                    'checked': checked,
                    'note': f'Title names {sorted(foreign)} and none of the candidate\'s '
                            f'own {sorted(owned)}.'}

    # Ambiguous engagement wording is REPORTED, never gated. Discovery carries it
    # forward as an employment_type verification need so the deep check can read
    # the advert and decide what the word `contract` actually meant here.
    wording = contract_wording(text)
    if wording == 'ambiguous':
        return {'blocked': False, 'reason_code': '', 'evidence': '', 'checked': checked,
                'verification': 'employment_type', 'employment_wording': 'ambiguous',
                'note': 'The advert says `contract` without saying whose. That is compatible '
                        'with direct fixed-term employment and with independent contracting, '
                        'so it is a question for the deep check rather than a filter.'}
    return {'blocked': False, 'reason_code': '', 'evidence': '', 'checked': checked,
            'verification': '', 'employment_wording': wording}


# --------------------------------------------------------------------------
# Reading a STATED experience minimum out of employer wording.
#
# A hard experience blocker is a decided rejection, so the sentence it quotes has
# to say the thing. Three sentences can each contain a number and only one of them
# is a minimum:
#
#     "You will need a minimum of 5 years of commercial Python experience"  minimum
#     "Ideally 5 years of commercial experience"                            preference
#     "Our platform has been in production for 5 years"                     neither
#
# The reader is therefore positive and narrow. A preference marker anywhere in the
# sentence disqualifies it, a ceiling form disqualifies it, and the number has to
# sit near an experience noun rather than merely appear in the text.
# --------------------------------------------------------------------------
_EXP_PREFERENCE = re.compile(
    r'\bideal(?:ly)?\b|\bprefer(?:red|ably|ence)?\b|\bdesirable\b|\bdesired\b'
    r'|\bnice\s+to\s+have\b|\badvantageous\b|\ba\s+plus\b|\bbonus\b'
    r'|\bwould\s+be\s+(?:great|good|nice)\b|\bnot\s+essential\b|\bwelcome\b'
    # Approximation is a wishlist too. "Approximately 4+ years" is an employer
    # sketching a level, not stating a floor, and a floor is what this blocker needs.
    r'|\bapprox(?:imately)?\b|\baround\b|\bcirca\b|\broughly\b|\bor\s+so\b', re.I)
_EXP_CEILING = re.compile(
    r'\bup\s+to\b|\bno\s+more\s+than\b|\bat\s+most\b|\bfewer\s+than\b'
    r'|\bless\s+than\b|\bunder\s+\d', re.I)
# The number, in one of the forms that actually state a floor.
_EXP_MINIMUM = (
    re.compile(r'\b(?:minimum|min\.?)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*years?\b', re.I),
    re.compile(r'\bat\s+least\s+(\d{1,2})\s*\+?\s*years?\b', re.I),
    re.compile(r'\b(\d{1,2})\s*\+\s*years?\b', re.I),
    re.compile(r'\b(\d{1,2})\s*years?\s*(?:\+|or\s+more|minimum|min\.?|and\s+above)\b', re.I),
    re.compile(r'\b(?:requires?|required|must\s+have|need)\s+(?:a\s+)?(\d{1,2})\s*\+?\s*years?\b', re.I),
)
# An experience noun has to sit close to the number, or a sentence about how old a
# technology is reads as a hiring requirement.
_EXP_CONTEXT = re.compile(
    r'\b(?:experience|experienced|commercial|professional|industry|hands[\s-]?on|'
    r'working|development|engineering|building|career)\b', re.I)
_EXP_CONTEXT_WINDOW = 60


def experience_minimum(text):
    """The hard minimum years a piece of employer wording actually states.

    Returns `{'years': int|None, 'reason': str, 'sentence': str}`. `years` is None
    whenever the wording is a preference, a ceiling, or a number with no experience
    meaning, because none of those is a stated floor and treating one as a floor is
    how a good vacancy gets deleted unseen.
    """
    body = collapse(text)
    if not body:
        return {'years': None, 'reason': 'no_text', 'sentence': ''}
    for sentence in re.split(r'(?<=[.;!?])\s+', body):
        sentence = sentence.strip()
        if not sentence:
            continue
        for pattern in _EXP_MINIMUM:
            found = pattern.search(sentence)
            if not found:
                continue
            if _EXP_PREFERENCE.search(sentence):
                return {'years': None, 'reason': 'stated_as_a_preference_not_a_minimum',
                        'sentence': sentence[:200]}
            if _EXP_CEILING.search(sentence):
                return {'years': None, 'reason': 'stated_as_a_ceiling_not_a_minimum',
                        'sentence': sentence[:200]}
            start, end = found.span()
            window = sentence[max(0, start - _EXP_CONTEXT_WINDOW):end + _EXP_CONTEXT_WINDOW]
            if not _EXP_CONTEXT.search(window):
                return {'years': None, 'reason': 'the_number_is_not_tied_to_experience',
                        'sentence': sentence[:200]}
            return {'years': int(found.group(1)), 'reason': 'stated_minimum',
                    'sentence': sentence[:200]}
    return {'years': None, 'reason': 'no_stated_minimum_found', 'sentence': ''}


# --------------------------------------------------------------------------
# Is an excluded specialism the role's OWN IDENTITY?
#
# The first version of this asked how OFTEN an excluded specialism was mentioned,
# and treated three mentions outnumbering the candidate's own specialisms as
# proof. That measures subject matter, not identity. A Python backend or platform
# advert legitimately talks at length about data, frontend, testing, DevOps,
# machine learning or mobile:
#
#     "you will build the platform our machine learning teams deploy on"
#     "you will work closely with the data science group and expose their models"
#     "our React front end consumes these APIs"
#
# Every one of those can out-mention `backend` in a genuinely backend advert, and
# this blocker DELETES a vacancy rather than ranking it down. Frequency is
# therefore gone, and only two things count, both of them the employer stating
# what the ROLE IS:
#
#   title                the employer's own title names an excluded identity from
#                        the controlled alias vocabulary, and does NOT also name an
#                        identity the candidate is targeting
#   explicit statement   the quoted employer sentence says the vacancy IS that role
#                        ("We are hiring a Data Scientist", "As a Data Scientist,
#                        you will"), rather than mentioning the discipline
#
# A technology, a responsibility, a department, a stakeholder team, a desirable
# skill, a project domain and an adjacent discipline are none of them identity. A
# title carrying both an accepted and an excluded identity is MIXED, which is
# reviewable rather than blocked. Anything else is UNDECIDED, which is a
# verification need.
# --------------------------------------------------------------------------

def normalise_identity(value):
    """Fold a title, a phrase or a sentence onto comparable tokens.

    Punctuation and hyphenation become spaces, so `front-end developer` and
    `Front End Developer` are one phrase, and matching stays whole-token rather
    than substring: `data science` can never match inside another word.
    """
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9+#]+', ' ', str(value or '').lower())).strip()


def _phrase_in(phrase, text):
    """Whole-phrase containment over normalised tokens."""
    phrase, text = normalise_identity(phrase), normalise_identity(text)
    return bool(phrase) and bool(text) and f' {phrase} ' in f' {text} '


# Constructions in which an employer states what the role IS. Each is anchored on
# a hiring or role verb, so a sentence merely NAMING a discipline cannot match:
# "you will support our data scientists" has no construction, and neither has
# "experience with data science is desirable".
_ROLE_IDENTITY_TEMPLATES = (
    r'\bwe\s+(?:are|re)\s+(?:currently\s+)?(?:hiring|looking\s+for|seeking|recruiting|after)\s+'
    r'(?:an?|our\s+next|a\s+new)\s+{identity}\b',
    r'\bwe\s+(?:have|are\s+advertising)\s+an?\s+(?:opening|vacancy|role|position)\s+for\s+'
    r'(?:an?\s+)?{identity}\b',
    r'\bas\s+(?:an?|our)\s+{identity}\s+you\s+(?:will|ll|would)\b',
    r'\bthis\s+(?:is|role\s+is|position\s+is|vacancy\s+is)\s+(?:an?)\s+{identity}\b',
    r'\bthe\s+(?:role|position|vacancy)\s+(?:is|of)\s+(?:an?\s+)?{identity}\b',
    r'\b(?:join|joining)\s+(?:us|our\s+team|the\s+team)\s+as\s+(?:an?)\s+{identity}\b',
    r'\byou\s+(?:will|ll)\s+(?:be\s+)?(?:joining|working)\s+(?:us\s+)?as\s+(?:an?)\s+{identity}\b',
)


def role_identity_statement(quotation, aliases):
    """The employer sentence that says the vacancy IS one of these identities.

    Returns the matched span of the normalised quotation, or '' when the wording
    only mentions the discipline. The alternation is built from the controlled
    alias vocabulary, so nothing outside it can ever match.
    """
    text = normalise_identity(quotation)
    names = [normalise_identity(a) for a in (aliases or [])]
    names = sorted({n for n in names if n}, key=len, reverse=True)
    if not text or not names:
        return ''
    identity = '(?:' + '|'.join(re.escape(n).replace(r'\ ', r'\s+') for n in names) + ')'
    for template in _ROLE_IDENTITY_TEMPLATES:
        found = re.search(template.replace('{identity}', identity), text)
        if found:
            return found.group(0)[:200]
    return ''


def specialism_role_identity(aliases, title='', quotation='', accepted_identities=()):
    """Whether employer evidence establishes an excluded specialism as the ROLE.

    Returns `{'established': bool, 'basis': str, 'matched_alias': str,
    'accepted_in_title': [...], 'statement': str}`. `basis` is `canonical_title`,
    `explicit_role_identity_statement`, `mixed_title` or `undecided`.

    A title naming BOTH an accepted identity and an excluded one is mixed, and a
    mixed title is never overridden by a statement in the body: two contradictory
    claims about what a role is are a question for a person, not a rejection to
    automate.
    """
    result = {'established': False, 'basis': 'undecided', 'matched_alias': '',
              'accepted_in_title': [], 'statement': ''}
    names = [str(a) for a in (aliases or []) if str(a).strip()]
    if not names:
        return result

    excluded_hit = next((n for n in sorted(names, key=len, reverse=True)
                         if _phrase_in(n, title)), '')
    accepted_hits = sorted({str(a) for a in (accepted_identities or [])
                            if _phrase_in(a, title)})
    result['accepted_in_title'] = accepted_hits

    if excluded_hit and accepted_hits:
        result.update({'basis': 'mixed_title', 'matched_alias': excluded_hit})
        return result
    if excluded_hit:
        result.update({'established': True, 'basis': 'canonical_title',
                       'matched_alias': excluded_hit})
        return result
    if accepted_hits:
        # The employer has titled this as work the candidate targets. A discipline
        # named in the body is then subject matter, not identity.
        return result

    statement = role_identity_statement(quotation, names)
    if statement:
        matched = next((n for n in sorted(names, key=len, reverse=True)
                        if _phrase_in(n, statement)), '')
        result.update({'established': True, 'basis': 'explicit_role_identity_statement',
                       'matched_alias': matched, 'statement': statement})
    return result


def query_was_executed(observed):
    """Did the source actually RUN the query, or return its unfiltered inventory?

    GradSponsor, FindSponsorJobs and JobServe all accepted a query in production
    run `scrape-20260831T102144228455` and returned their whole listing anyway:
    3,460 jobs, 11,678 roles and 20,004 jobs respectively, with top results
    unrelated to the request. Recording those as `ok` would have claimed coverage
    for an interval nobody searched, and recording them as `empty` would have
    claimed the market held nothing.

    `observed` is a plain dict:
        {'query', 'heading', 'echoed_url', 'result_total', 'baseline_total',
         'top_titles'}

    Returns (executed, outcome, reason). `outcome` is the controlled source
    outcome to record when the query was NOT executed: never `ok`, never `empty`.
    """
    observed = observed or {}
    query = str(observed.get('query') or '').strip()
    if not query:
        return False, 'error', 'no query text was supplied to verify against'

    tokens = [t for t in re.split(r'[^A-Za-z0-9+#.]+', query.lower()) if len(t) > 2]
    haystack = ' '.join(str(observed.get(k) or '').lower()
                        for k in ('heading', 'echoed_url'))
    echoed = bool(tokens) and any(t in haystack for t in tokens)

    total = observed.get('result_total')
    baseline = observed.get('baseline_total')
    unchanged = (total is not None and baseline is not None
                 and int(total) == int(baseline))

    titles = ' '.join(str(t).lower() for t in (observed.get('top_titles') or []))
    relevant = bool(tokens) and any(t in titles for t in tokens)

    if unchanged and not relevant:
        return False, 'partial', (
            f'the result set was unchanged at {total} after submitting '
            f'{query!r} and no top result matched it, so the query was ignored '
            f'and its inventory was never searched for this request')
    if not echoed and not relevant:
        return False, 'partial', (
            f'neither the rendered heading, the echoed URL nor any top result '
            f'reflects {query!r}, so the query cannot be attributed to this page')
    return True, 'ok', ''


def browser_card_ownership(card):
    """Does this browser result card OWN the id it is about to be stored under?

    A board's result list can contain HIDDEN placeholder elements that carry a
    real-looking job id. Verified on Indeed in production run
    `scrape-20260831T102144228455` (2026-08-31): a zero-height placeholder with
    `data-jk` resolved, via `closest()`, to the NEIGHBOURING visible card, and so
    inherited that card's title, employer and location. The same placeholder id
    was attributed to two entirely different vacancies on two different queries.
    Persisting either would have created a phantom vacancy under a plausible id,
    which no later check could detect.

    So a card is trustworthy only when BOTH hold:

      - it is actually visible (rendered, non-zero box, no hidden ancestor), and
      - its id and its displayed fields came from the SAME card element.

    `card` is a plain dict so this is testable without a browser:
        {'job_id', 'visible', 'width', 'height', 'hidden_ancestor',
         'id_owner', 'field_owner'}

    Returns (ok, reason). Never guesses: an unproven card is dropped, not repaired.
    """
    card = card or {}
    job_id = str(card.get('job_id') or '').strip()
    if not job_id:
        return False, 'no job id on the card'
    if not bool(card.get('visible', True)):
        return False, f'hidden element: {job_id} is not rendered, so it owns no card'
    if bool(card.get('hidden_ancestor')):
        return False, f'hidden ancestor: {job_id} is inside a non-rendered subtree'
    try:
        w = float(card.get('width', 1) or 0)
        h = float(card.get('height', 1) or 0)
    except (TypeError, ValueError):
        return False, f'unreadable box for {job_id}'
    if w <= 0 or h <= 0:
        return False, f'zero-size element: {job_id} has no rendered box ({w}x{h})'
    id_owner = card.get('id_owner')
    field_owner = card.get('field_owner')
    if id_owner is not None and field_owner is not None and id_owner != field_owner:
        return False, (f'ownership mismatch: id {job_id} came from {id_owner!r} but '
                       f'its fields came from {field_owner!r}, so the id belongs to '
                       f'a different card')
    return True, ''


def trustworthy_browser_cards(cards):
    """Filter a result list to cards that own their ids, THEN deduplicate.

    Order matters. Deduplicating first would let a hidden placeholder collapse
    into, or evict, the genuine card that shares its id.
    """
    kept, rejected, seen = [], [], set()
    for card in cards or []:
        ok, why = browser_card_ownership(card)
        if not ok:
            rejected.append({'job_id': str((card or {}).get('job_id') or ''),
                             'reason': why})
            continue
        jid = str(card.get('job_id')).strip()
        if jid in seen:
            continue
        seen.add(jid)
        kept.append(card)
    return kept, rejected


def hint_location(value):
    """Return a coarse locality key for possible-duplicate HINTS only.

    Never an input to automatic merging. Word-based rather than substring-based, so
    'Londonderry' never collapses onto 'London'.
    """
    text = collapse(value).lower()
    if not text:
        return ''
    parts = [p.strip() for p in text.split(',') if p.strip()]
    # Drop trailing nation qualifiers, but never drop the only segment: a candidate
    # located simply at "United Kingdom" must keep that as its locality.
    while len(parts) > 1 and parts[-1] in _LOC_NATION:
        parts.pop()
    head = _LOC_LEAD.sub('', parts[0])
    head = _LOC_PARENS.sub(' ', head)
    head = _LOC_NOISE.sub(' ', head)
    head = re.sub(r'^greater\s+', '', head)
    head = _LOC_POSTCODE.sub(' ', head)
    head = re.sub(r'[^a-z\s\-]', ' ', head)
    return re.sub(r'\s+', ' ', head).strip()

# Identity and classification a candidate cannot be useful without.
REQUIRED_FIELDS = ('source_id', 'source_url', 'company', 'title', 'lead_type', 'source_confidence')

TEXT_FIELDS = (
    'source_url', 'canonical_url', 'source_job_id', 'requisition_id', 'company',
    'title', 'location', 'posted', 'posted_raw', 'closing_date', 'salary_raw',
    'description_text', 'description_hash', 'sponsorship_evidence', 'filter_reason',
    'discovered_at', 'fetched_at',
)
NUMBER_FIELDS = ('salary_min', 'salary_max')
INT_FIELDS = ('years_required_min', 'years_required_max')

# FACT_FIELDS is owned by job_state, the write boundary that persists them, and is
# imported above so the candidate schema and the stored facts object cannot drift.
# `extracted_at` is stamped by facts_from_candidate rather than carried on a
# candidate, so it is excluded from the projected subset.
PROJECTED_FACT_FIELDS = tuple(f for f in FACT_FIELDS if f != 'extracted_at')

RELATIVE_AGE_PATTERNS = (
    (re.compile(r'(\d+)\s*\+?\s*(?:second|sec|minute|min|hour|hr)s?\s*ago', re.I), 0),
    (re.compile(r'(\d+)\s*\+?\s*days?\s*ago', re.I), 1),
    (re.compile(r'(\d+)\s*\+?\s*weeks?\s*ago', re.I), 7),
    (re.compile(r'(\d+)\s*\+?\s*months?\s*ago', re.I), 30),
    (re.compile(r'(\d+)\s*\+?\s*years?\s*ago', re.I), 365),
)
TODAY_MARKERS = re.compile(r'\b(just\s*(now|posted)|today|new)\b', re.I)
YESTERDAY_MARKERS = re.compile(r'\byesterday\b', re.I)


def candidate_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


# --------------------------------------------------------------------------
# Deterministic normalisation helpers
# --------------------------------------------------------------------------

def collapse(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def description_hash(text):
    """Stable hash of a job description.

    Whitespace is collapsed first so a re-wrapped copy of the same advert does not
    look like changed text, while any real wording change does.
    """
    body = collapse(text)
    if not body:
        return ''
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def parse_iso_date(value):
    value = collapse(value)
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def age_days_from_raw(text):
    """Days old implied by a visible posted-age string, or None when unreadable.

    This reads what the card itself says. It never infers an age from the search
    filter that produced the card.
    """
    body = collapse(text)
    if not body:
        return None
    for pattern, multiplier in RELATIVE_AGE_PATTERNS:
        match = pattern.search(body)
        if match:
            return int(match.group(1)) * multiplier
    if YESTERDAY_MARKERS.search(body):
        return 1
    if TODAY_MARKERS.search(body):
        return 0
    return None


def window_eligibility(posted='', posted_raw='', window_days=1, today=None):
    """Whether one candidate belongs inside a freshness window.

    Returns `inside`, `outside` or `unknown`. A verified posted date wins. Failing
    that, the candidate's own visible posted age is used. An active page filter is
    never consulted, because promoted slots on CWJobs and Totaljobs are served
    regardless of the selected posted-within filter.
    """
    today = today or date.today()
    if isinstance(today, str):
        today = parse_iso_date(today) or date.today()
    posted_date = parse_iso_date(posted)
    if posted_date is not None:
        age = (today - posted_date).days
        return 'inside' if age <= window_days else 'outside'
    age = age_days_from_raw(posted_raw)
    if age is not None:
        return 'inside' if age <= window_days else 'outside'
    return 'unknown'


# --------------------------------------------------------------------------
# Post-verification run-window gate.
#
# A candidate can enter discovery with no known posted date and only acquire an
# authoritative one later, when the parent opens the official ATS page. Freshness
# must therefore be re-decided at that point, not just at discovery time.
#
# The first production run proved the gap: it searched 24h only, and Letly's
# "Software Engineer (Backend) - Junior/Mid" turned out to be posted 2026-08-13,
# fifteen days before the run. Because its date was unknown when discovered, it
# stayed an active Direct opportunity and was proposed for ranking, which is a
# vacancy the run never actually asked for.
# --------------------------------------------------------------------------
# The window vocabulary shared with discovery_run.WIDENING_THRESHOLDS. Keeping the
# day values here, next to the date logic, means there is one definition of how
# long "7d" is.
WINDOW_DAYS = {'24h': 1, '7d': 7, '14d': 14}

RUN_WINDOW_VERDICTS = ('IN_WINDOW', 'OUT_OF_WINDOW', 'UNKNOWN_FRESHNESS')


def widest_window_days(windows_used):
    """Widest window a run actually activated, in days, or None if none is known.

    A run that widened to 7d judges its candidates against 7 days, not 1: the
    widening genuinely asked for that inventory.
    """
    best = None
    for token in (windows_used or []):
        days = WINDOW_DAYS.get(str(token).strip().lower())
        if days is not None and (best is None or days > best):
            best = days
    return best


def run_window_gate(posted='', posted_raw='', windows_used=None, today=None):
    """Judge one candidate against the WIDEST window its run actually activated.

    Returns IN_WINDOW, OUT_OF_WINDOW or UNKNOWN_FRESHNESS with the evidence used.
    Date reasoning is delegated to window_eligibility so there is exactly one
    implementation of "how old is this posting".

    UNKNOWN_FRESHNESS is not a failure and not staleness. An open vacancy whose
    source publishes no posted date stays eligible, because unknown is unknown.
    Nothing here invents a date, and a generic "last updated" stamp is never
    treated as the original posting date.
    """
    days = widest_window_days(windows_used)
    if days is None:
        return {'verdict': 'UNKNOWN_FRESHNESS', 'window_days': None,
                'windows_used': list(windows_used or []), 'posted': posted or '',
                'posted_raw': posted_raw or '', 'age_days': None,
                'reason': 'The run recorded no window, so no candidate can be judged '
                          'against one.'}

    eligibility = window_eligibility(posted, posted_raw, days, today)
    reference = today or date.today()
    if isinstance(reference, str):
        reference = parse_iso_date(reference) or date.today()
    posted_date = parse_iso_date(posted)
    age = (reference - posted_date).days if posted_date else age_days_from_raw(posted_raw)

    verdict = {'inside': 'IN_WINDOW', 'outside': 'OUT_OF_WINDOW',
               'unknown': 'UNKNOWN_FRESHNESS'}[eligibility]
    if verdict == 'OUT_OF_WINDOW':
        reason = (f'Authoritative posted evidence puts this vacancy {age} days old, '
                  f'outside the widest window this run activated ({days} days from '
                  f'{"/".join(windows_used or [])}).')
    elif verdict == 'IN_WINDOW':
        reason = (f'Posted {age} days before the reference date, inside the widest '
                  f'window this run activated ({days} days).')
    else:
        reason = ('No authoritative posted date or visible posted age, so freshness '
                  'is genuinely unknown. Unknown is not stale: an open vacancy stays '
                  'eligible and keeps its uncertainty visible.')
    return {'verdict': verdict, 'window_days': days,
            'windows_used': list(windows_used or []), 'posted': posted or '',
            'posted_raw': posted_raw or '', 'age_days': age, 'reason': reason}


def coerce_number(value, field, errors):
    if value in (None, ''):
        return None
    if isinstance(value, bool):
        errors.append({'field': field, 'value': value, 'problem': 'not_a_number'})
        return None
    if isinstance(value, (int, float)):
        return value
    cleaned = re.sub(r'[,\s£$€]', '', str(value))
    try:
        number = float(cleaned)
    except ValueError:
        errors.append({'field': field, 'value': value, 'problem': 'not_a_number'})
        return None
    return int(number) if number.is_integer() else number


def coerce_int(value, field, errors):
    number = coerce_number(value, field, errors)
    if number is None:
        return None
    if float(number) != int(number):
        errors.append({'field': field, 'value': value, 'problem': 'not_an_integer'})
        return None
    return int(number)


def choice(value, field, allowed, errors, default=''):
    raw = collapse(value).lower()
    if not raw:
        return default
    if raw not in allowed:
        errors.append({'field': field, 'value': value, 'problem': 'not_in_vocabulary',
                       'allowed': list(allowed)})
        return default
    return raw


def normalise_skills(value, errors):
    if value in (None, ''):
        return []
    if isinstance(value, str):
        items = [part for part in re.split(r'[,;]', value)]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        errors.append({'field': 'skills', 'value': value, 'problem': 'not_a_list'})
        return []
    seen, out = set(), []
    for item in items:
        token = collapse(item)
        if not token:
            continue
        if token.lower() in seen:
            continue
        seen.add(token.lower())
        out.append(token)
    return out


def normalise_currency(value, errors):
    token = collapse(value).upper()
    if not token:
        return ''
    if not re.fullmatch(r'[A-Z]{3}', token):
        errors.append({'field': 'salary_currency', 'value': value,
                       'problem': 'not_an_iso_4217_code'})
        return ''
    return token


# --------------------------------------------------------------------------
# Candidate schema
# --------------------------------------------------------------------------

def normalise_candidate(raw, registry=None, now=None):
    """Validate and normalise one discovery candidate.

    Returns `(candidate, errors)`. When `errors` is non-empty the candidate is not
    fit to enter discovery state.
    """
    errors = []
    if not isinstance(raw, dict):
        return None, [{'field': '_root', 'problem': 'not_an_object',
                       'value': type(raw).__name__}]
    registry = registry or load_registry()
    now = now or datetime.now().astimezone().isoformat(timespec='seconds')

    item = {field: collapse(raw.get(field)) for field in TEXT_FIELDS}

    source_id = collapse(raw.get('source_id'))
    item['source_id'] = source_id
    if source_id and not is_known_source(source_id, registry):
        errors.append({'field': 'source_id', 'value': source_id,
                       'problem': 'not_in_source_registry'})
        item['source_family'] = ''
    elif source_id:
        # The registry owns the family. A candidate may not declare a different one.
        family = source_family(source_id, registry)
        declared = collapse(raw.get('source_family'))
        if declared and declared != family:
            errors.append({'field': 'source_family', 'value': declared,
                           'problem': 'contradicts_registry', 'expected': family})
        item['source_family'] = family
    else:
        item['source_family'] = ''

    item['lead_type'] = choice(raw.get('lead_type'), 'lead_type', LEAD_TYPES, errors)
    item['source_confidence'] = choice(
        raw.get('source_confidence'), 'source_confidence', SOURCE_CONFIDENCES, errors)
    item['fit_band'] = choice(raw.get('fit_band'), 'fit_band', FIT_BANDS, errors, default='unknown')
    item['sponsorship_label'] = choice(
        raw.get('sponsorship_label'), 'sponsorship_label', SPONSORSHIP_LABELS, errors,
        default='unknown')
    item['employment_type'] = choice(
        raw.get('employment_type'), 'employment_type', EMPLOYMENT_TYPES, errors, default='unknown')
    item['work_pattern'] = choice(
        raw.get('work_pattern'), 'work_pattern', WORK_PATTERNS, errors, default='unknown')

    declared_type = choice(raw.get('source_type'), 'source_type', SOURCE_TYPES, errors)
    registry_type = state_source_type_for(source_id, registry=registry) if item['source_family'] else None
    if declared_type:
        item['source_type'] = declared_type
    elif registry_type:
        item['source_type'] = registry_type
    else:
        item['source_type'] = ''
        if item['source_family']:
            errors.append({'field': 'source_type', 'problem': 'required_for_this_source',
                           'value': source_id,
                           'reason': 'this source has no default state_source_type, '
                                     'so the resolved target must supply one'})

    for field in NUMBER_FIELDS:
        item[field] = coerce_number(raw.get(field), field, errors)
    for field in INT_FIELDS:
        item[field] = coerce_int(raw.get(field), field, errors)
    if item['salary_min'] is not None and item['salary_max'] is not None:
        if item['salary_min'] > item['salary_max']:
            errors.append({'field': 'salary_min', 'problem': 'greater_than_salary_max',
                           'value': item['salary_min']})
    if item['years_required_min'] is not None and item['years_required_max'] is not None:
        if item['years_required_min'] > item['years_required_max']:
            errors.append({'field': 'years_required_min', 'problem': 'greater_than_years_required_max',
                           'value': item['years_required_min']})

    item['salary_currency'] = normalise_currency(raw.get('salary_currency'), errors)
    item['skills'] = normalise_skills(raw.get('skills'), errors)

    for field in ('posted', 'closing_date'):
        value = item[field]
        if value and parse_iso_date(value) is None:
            errors.append({'field': field, 'value': value,
                           'problem': 'not_an_iso_date',
                           'hint': 'a relative age such as "3 hours ago" belongs in posted_raw'})
            item[field] = ''

    item['canonical_url'] = norm_url(item['source_url']) if item['source_url'] else ''
    item['source_host'] = source_host(item['source_url'])
    if item['description_text']:
        computed = description_hash(item['description_text'])
        if item['description_hash'] and item['description_hash'] != computed:
            errors.append({'field': 'description_hash', 'value': item['description_hash'],
                           'problem': 'does_not_match_description_text'})
        item['description_hash'] = computed
    item['discovered_at'] = item['discovered_at'] or now

    for field in REQUIRED_FIELDS:
        if not item.get(field):
            errors.append({'field': field, 'problem': 'required'})

    item['schema_version'] = SCHEMA_VERSION
    return item, errors


def facts_from_candidate(candidate, extracted_at=None):
    """Project the structured-fact subset of a candidate for persistence.

    Only fields the source actually stated survive. Absent values are preserved as
    null/empty rather than filled in.
    """
    facts = {field: candidate.get(field) for field in PROJECTED_FACT_FIELDS}
    facts['extracted_at'] = extracted_at or candidate.get('fetched_at') \
        or candidate.get('discovered_at') \
        or datetime.now().astimezone().isoformat(timespec='seconds')
    # Unknown stays unknown: a fact the source never stated is dropped rather than
    # persisted as a null the reader could mistake for a checked value.
    return {k: v for k, v in facts.items() if v not in (None, '', [])}


# --------------------------------------------------------------------------
# Worker output contract
# --------------------------------------------------------------------------

def body_signal_gate(text, title='', strategy=None, extra_signals=(), hard_blocker=''):
    """Cheap deterministic decision on whether a body is worth deep checking.

    Broad adjacent-title searching is only affordable when the obviously wrong
    results are dropped before any model reads them. But a keyword gate that
    promotes anything mentioning "Python" is worse than no gate: a React role that
    lists Python once under "nice to have" would sail through and consume a deep
    check.

    So promotion requires SEVERAL DISTINCT signals, incidental terms are discounted,
    and counter-signals from a different specialism pull against it. HARD_REJECT is
    only ever returned when the caller passes an existing deterministic blocker;
    this gate never invents one from body text.
    """
    strategy = strategy or load_strategy()
    config = strategy.get('body_signals', {})
    minimum = int(config.get('min_distinct_signals', 2))
    signals = [s.lower() for s in list(config.get('backend_signals', [])) + list(extra_signals)]
    incidental = {s.lower() for s in config.get('incidental_only_signals', [])}
    counters = [s.lower() for s in config.get('counter_signals', [])]

    haystack = f'{title} {text}'.lower()
    matched = sorted({s for s in signals if s and s in haystack})
    strong = sorted(s for s in matched if s not in incidental)
    counter_hits = sorted({c for c in counters if c and c in haystack})

    if hard_blocker:
        verdict = 'HARD_REJECT'
        reason = f'existing deterministic blocker: {hard_blocker}'
    elif len(matched) < minimum:
        verdict = 'LOW_SIGNAL'
        reason = (f'{len(matched)} distinct backend signal(s), '
                  f'{minimum} required: {", ".join(matched) or "none"}')
    elif not strong:
        # Everything matched was a term common enough to appear in almost any
        # advert. That is not evidence that backend work is central.
        verdict = 'LOW_SIGNAL'
        reason = (f'only incidental signals present ({", ".join(matched)}), '
                  'none specific enough to show backend work is central')
    elif counter_hits and len(strong) <= len(counter_hits):
        # A different specialism is at least as well evidenced as this one.
        verdict = 'LOW_SIGNAL'
        reason = (f'{len(counter_hits)} counter-signal(s) for another specialism '
                  f'({", ".join(counter_hits)}) against {len(strong)} specific backend '
                  f'signal(s) ({", ".join(strong)})')
    else:
        verdict = 'KEEP_FOR_DEEP_CHECK'
        reason = (f'{len(strong)} specific backend signal(s): {", ".join(strong)}'
                  + (f'; counter-signals present but outweighed: {", ".join(counter_hits)}'
                     if counter_hits else ''))

    return {
        'verdict': verdict,
        'reason': reason,
        'signals_matched': matched,
        'specific_signals': strong,
        'incidental_signals': sorted(s for s in matched if s in incidental),
        'counter_signals': counter_hits,
        'min_distinct_signals': minimum,
        'note': 'This is a cheap gate, not a score. KEEP_FOR_DEEP_CHECK only means the '
                'vacancy earned a full read; it is never a match judgement.',
    }


def _authority_rank(candidate):
    source_type = str(candidate.get('source_type') or 'unknown').strip().lower()
    try:
        return SOURCE_TYPE_AUTHORITY.index(source_type)
    except ValueError:
        return len(SOURCE_TYPE_AUTHORITY)


def _ensure_normalised(candidate, registry=None):
    """Return a candidate guaranteed to carry the fields authority ranking needs.

    Consolidation compares `source_type` and `source_confidence` to decide which
    sighting becomes primary, and both are DERIVED from the registry during
    normalisation. A raw candidate carrying only `source_id` therefore ranked as
    zero against every other, which made the first sighting win by accident.

    Rows that already carry a derived `source_type` are returned untouched, so a
    caller that did normalise pays nothing. A row that cannot be normalised at all
    is returned as-is: consolidation is not the validation boundary, and rejecting
    here would silently drop a candidate the caller intends to validate elsewhere.
    """
    if not isinstance(candidate, dict):
        return candidate
    if collapse(candidate.get('source_type')) and collapse(candidate.get('canonical_url')):
        return candidate
    normalised, errors = normalise_candidate(candidate, registry)
    if errors or not normalised:
        # Fill only what authority ranking needs, without inventing anything the
        # registry does not already state about this source.
        patched = dict(candidate)
        registry = registry or load_registry()
        source_id = collapse(candidate.get('source_id'))
        if not collapse(patched.get('source_type')) and source_id:
            derived = state_source_type_for(source_id, registry=registry)
            if derived:
                patched['source_type'] = derived
        if not collapse(patched.get('canonical_url')):
            patched['canonical_url'] = norm_url(candidate.get('source_url') or '')
        if not collapse(patched.get('source_host')):
            patched['source_host'] = source_host(
                patched.get('source_url') or patched.get('canonical_url') or '')
        return patched
    return normalised


def _candidate_identity(candidate):
    """The identity evidence one candidate carries, normalised for comparison."""
    canonical = norm_url(candidate.get('canonical_url') or candidate.get('source_url') or '')
    return {
        'canonical_url': canonical,
        'host': canon_host(candidate.get('source_host') or source_host(
            candidate.get('source_url') or canonical)),
        'source_id': collapse(candidate.get('source_id')),
        'company': collapse(candidate.get('company')).lower(),
        'title': collapse(candidate.get('title')).lower(),
        'location': collapse(candidate.get('location')).lower(),
        'employer_key': collapse(candidate.get('employer_key')).lower(),
        'requisition_id': collapse(candidate.get('requisition_id')),
        'source_job_id': collapse(candidate.get('source_job_id')),
        'resolved_from_url': norm_url(candidate.get('resolved_from_url') or ''),
        'resolved_to_url': norm_url(candidate.get('resolved_to_url') or ''),
    }


def _employer_identity(identity):
    """A comparable employer identity: the resolved key when known, else the name.

    The name falls through `employers.employer_key`, the single definition of
    employer identity in this workspace, so `Acme Ltd` and `Acme Limited` are one
    employer here exactly as they are in the employer cache. Two modules disagreeing
    about that would make a requisition match depend on which one asked.
    """
    return identity['employer_key'] or employer_key(identity['company'])


def safe_merge_evidence(left, right):
    """Evidence strong enough to merge two sightings BEFORE either is deep fetched.

    Only these four count, and each is an identifier that a source actually
    published rather than an inference drawn from how two adverts look:

        canonical_url    the same URL. Unarguable.
        requisition      the same non-empty requisition id at a compatible
                         employer. A requisition id is the employer's own handle
                         for one vacancy.
        source_job_id    the same non-empty source-local job id on the same host.
                         Source-local means it is only meaningful within that host,
                         so the host must match too.
        resolution_link  an explicit board -> employer/ATS resolution recorded on
                         one of the candidates, which is direct evidence that both
                         sightings are the same vacancy.

    Company + title + location is deliberately NOT here. One employer routinely
    advertises several genuinely different vacancies with the same title in the
    same city, on different teams and under different requisitions. Merging those
    before a deep fetch silently discards a real vacancy, and a recall loss is
    invisible: nothing downstream can tell that a vacancy was never examined.
    """
    if left['canonical_url'] and left['canonical_url'] == right['canonical_url']:
        return 'canonical_url'

    # An explicit resolution linkage is direct evidence, so it is checked before
    # the id rules: a board sighting resolved to an ATS page states the connection.
    for a, b in ((left, right), (right, left)):
        if a['resolved_to_url'] and a['resolved_to_url'] == b['canonical_url']:
            return 'resolution_link'
        if a['resolved_from_url'] and a['resolved_from_url'] == b['canonical_url']:
            return 'resolution_link'
    if (left['resolved_to_url'] and left['resolved_to_url'] == right['resolved_to_url']) or (
            left['resolved_from_url'] and left['resolved_from_url'] == right['resolved_from_url']):
        return 'resolution_link'

    if left['requisition_id'] and left['requisition_id'] == right['requisition_id']:
        # A requisition id is employer-scoped, so it only identifies a vacancy once
        # the employer matches. Two employers can both use "REQ-1".
        a, b = _employer_identity(left), _employer_identity(right)
        if a and b and a == b:
            return 'requisition_id'

    if left['source_job_id'] and left['source_job_id'] == right['source_job_id']:
        # A source-local job id means nothing across hosts, so the host or the
        # registered source must match before it identifies anything.
        if (left['host'] and left['host'] == right['host']) or (
                left['source_id'] and left['source_id'] == right['source_id']):
            return 'source_job_id'

    return ''


def consolidate(candidates, registry=None):
    """Merge candidates that are PROVABLY the same vacancy, before any deep fetch.

    Four sources finding one vacancy is four raw candidates and ONE piece of work.
    Deep-fetching it once per source would multiply the most expensive step in the
    pipeline by the number of places that happened to list it.

    But an over-eager merge is worse than a missed one. A false merge here removes
    a real vacancy from the run before anybody looks at it, and nothing downstream
    can detect the loss. So automatic merging requires published identifier
    evidence (see `safe_merge_evidence`), never a resemblance between two adverts.

    Company + title + location no longer merges. It is reported as a
    POSSIBLE DUPLICATE instead: both candidates stay in the run and are resolved by
    the ordinary deep check, which can see the requisition, the team and the body
    text that distinguish two genuinely different vacancies.

    The most authoritative source still becomes the primary record of a merged
    group, so a board listing resolved to an employer ATS page is a provenance
    upgrade rather than a duplicate, and weaker sightings survive in
    `secondary_sources` as evidence that the vacancy is real and widely listed.

    IT NORMALISES ITS OWN INPUT. Source authority is decided from `source_type`,
    which is derived from the registry during normalisation. Given raw candidates
    that carry only a `source_id`, every row ranked equally, so the FIRST sighting
    stayed primary and a stronger employer/ATS one was silently demoted to a
    secondary source. That is the provenance upgrade failing quietly, and it
    depended on nothing but undocumented caller hygiene. Any row missing a derived
    field is normalised here rather than trusted, so calling this out of order can
    no longer produce the wrong authority ordering.
    """
    registry = registry or load_registry()
    rows = [(index, _ensure_normalised(candidate, registry))
            for index, candidate in enumerate(candidates or [])
            if isinstance(candidate, dict)]
    identities = {index: _candidate_identity(candidate) for index, candidate in rows}

    # Union-find over safe evidence only. Transitivity is intended: if A and B share
    # a requisition and B and C share a URL, all three are one vacancy.
    parent = {index: index for index, _ in rows}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    merge_reasons = {}
    for position, (index, _) in enumerate(rows):
        for other_index, _ in rows[position + 1:]:
            reason = safe_merge_evidence(identities[index], identities[other_index])
            if not reason:
                continue
            root_a, root_b = find(index), find(other_index)
            if root_a != root_b:
                parent[root_b] = root_a
            merge_reasons.setdefault(find(index), set()).add(reason)

    groups, order = {}, []
    for index, candidate in rows:
        root = find(index)
        if root not in groups:
            groups[root] = []
            order.append(root)
        groups[root].append((index, candidate))

    consolidated, merges = [], []
    index_to_group = {}
    for root in order:
        members = sorted(groups[root], key=lambda pair: (_authority_rank(pair[1]), pair[0]))
        group_position = len(consolidated)
        for member_index, _ in members:
            index_to_group[member_index] = group_position
        _, primary = members[0]
        merged = dict(primary)
        secondaries = []
        for _, other in members[1:]:
            secondaries.append({
                'source_id': other.get('source_id', ''),
                'source_type': other.get('source_type', ''),
                'source_url': other.get('source_url', ''),
                'source_confidence': other.get('source_confidence', ''),
            })
            # A weaker sighting may fill a fact the stronger source did not state,
            # exactly as the state merge does. It never overwrites one.
            for field, value in other.items():
                if field in ('source_id', 'source_type', 'source_url', 'source_family',
                             'source_confidence', 'canonical_url'):
                    continue
                if value in (None, '', []) or merged.get(field) not in (None, '', []):
                    continue
                merged[field] = value
        if secondaries:
            merged['secondary_sources'] = secondaries
            merged['merge_evidence'] = sorted(merge_reasons.get(root, set()))
            merges.append({
                'kept': merged.get('source_id', ''),
                'kept_index': members[0][0],
                'merged_away': [s['source_id'] for s in secondaries],
                'merged_indices': [i for i, _ in members[1:]],
                'evidence': sorted(merge_reasons.get(root, set())),
            })
        merged['sighting_count'] = len(members)
        consolidated.append(merged)

    # Candidates that LOOK like the same vacancy but carry no safe evidence. They
    # are reported compactly and kept separate, never merged away.
    by_cti = {}
    for index, _ in rows:
        identity = identities[index]
        if not (identity['company'] and identity['title'] and identity['location']):
            continue
        key = (identity['company'], identity['title'],
               hint_location(identity['location']))
        by_cti.setdefault(key, []).append(index)

    possible_duplicates = []
    for (company, title, location), indices in by_cti.items():
        distinct_groups = sorted({index_to_group[i] for i in indices})
        if len(distinct_groups) < 2:
            continue
        possible_duplicates.append({
            'reason': 'company_title_location',
            'company': collapse(candidates[indices[0]].get('company')),
            'title': collapse(candidates[indices[0]].get('title')),
            'location': collapse(candidates[indices[0]].get('location')),
            'location_key': location,
            'location_variants': sorted({collapse(candidates[i].get('location'))
                                         for i in indices}),
            'candidate_count': len(indices),
            'consolidated_indices': distinct_groups,
            'candidates': [{
                'index': i,
                'source_id': identities[i]['source_id'],
                'canonical_url': identities[i]['canonical_url'],
                'requisition_id': identities[i]['requisition_id'],
                'source_job_id': identities[i]['source_job_id'],
            } for i in indices],
            'note': 'Same company, same title and the same coarse locality, but no '
                    'shared URL, requisition, source job id or resolution link. '
                    'Locality is normalised for this HINT only, so "London" and '
                    '"London, England" are shown together; it is never evidence for '
                    'an automatic merge. One employer can advertise several genuinely '
                    'different vacancies this way, so both are kept and the deep check '
                    'decides.',
        })
    possible_duplicates.sort(key=lambda row: (row['company'], row['title'], row['location']))

    return {
        'input_count': len(candidates or []),
        'consolidated_count': len(consolidated),
        'duplicates_merged': len(rows) - len(consolidated),
        'possible_duplicates': possible_duplicates,
        'possible_duplicate_count': len(possible_duplicates),
        'deep_fetches_saved': len(rows) - len(consolidated),
        'candidates': consolidated,
        'merges': merges,
        'safe_merge_evidence': list(SAFE_MERGE_EVIDENCE),
        'note': 'Consolidation runs BEFORE deep fetching, so one vacancy costs one '
                'fetch however many sources listed it. Merging requires published '
                'identifier evidence: company, title and location alone are reported '
                'as possible duplicates and both candidates stay in the run, because '
                'a false merge silently removes a real vacancy nobody then examines.',
    }


def validate_query_task(raw, registry=None, strategy=None):
    """Validate one bounded query task before it is handed to a worker.

    A worker that chooses its own scope is how a background agent quietly consumes
    an enormous budget. So a task must name exactly one query, one source, one
    search family and one candidate budget. An unbounded or missing budget is
    rejected rather than defaulted, because a silent default is how "bounded"
    becomes decorative.
    """
    registry = registry or load_registry()
    strategy = strategy or load_strategy()
    result = {'valid': False, 'errors': [], 'task': {}}
    if not isinstance(raw, dict):
        result['errors'].append({'field': '_root', 'problem': 'not_an_object',
                                 'value': type(raw).__name__})
        return result

    task = {}
    for field in ('query_id', 'search_family', 'source_id', 'query_text', 'window'):
        value = collapse(raw.get(field))
        task[field] = value
        if not value:
            result['errors'].append({'field': field, 'problem': 'required'})

    if task['search_family'] and not is_known_family(task['search_family'], strategy):
        result['errors'].append({'field': 'search_family', 'value': task['search_family'],
                                 'problem': 'not_in_search_strategy'})
    if task['source_id'] and not is_known_source(task['source_id'], registry):
        result['errors'].append({'field': 'source_id', 'value': task['source_id'],
                                 'problem': 'not_in_source_registry'})
    else:
        task['source_family'] = source_family(task['source_id'], registry) if task['source_id'] else ''

    budget = raw.get('candidate_budget')
    if isinstance(budget, bool) or not isinstance(budget, int):
        result['errors'].append({'field': 'candidate_budget', 'value': budget,
                                 'problem': 'required_positive_integer',
                                 'hint': 'A worker without a candidate budget is unbounded.'})
    elif budget <= 0:
        result['errors'].append({'field': 'candidate_budget', 'value': budget,
                                 'problem': 'must_be_positive'})
    else:
        task['candidate_budget'] = budget

    terms = raw.get('profile_terms', {})
    if terms in (None, ''):
        terms = {}
    if not isinstance(terms, dict):
        result['errors'].append({'field': 'profile_terms', 'problem': 'not_an_object'})
    else:
        # A worker gets term LISTS, never profile prose. Anything else here would be
        # candidate data crossing into a subagent prompt for no search benefit.
        for field, values in terms.items():
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                result['errors'].append({'field': f'profile_terms.{field}',
                                         'problem': 'not_a_list_of_terms'})
        task['profile_terms'] = terms

    if raw.get('expand_search') or raw.get('unbounded'):
        result['errors'].append({'field': 'expand_search', 'problem': 'worker_may_not_expand_scope',
                                 'hint': 'The parent decides whether another query is '
                                         'warranted, using saturation and budget logic.'})

    task['requires_body_validation'] = bool(raw.get('requires_body_validation'))
    result['task'] = task
    result['valid'] = not result['errors']
    return result


def validate_worker_output(raw, registry=None):
    """Validate one public-job-researcher return envelope.

    Envelope problems invalidate the whole return. Candidate problems reject only
    that candidate, so one malformed row never silently discards a good batch and
    never enters discovery state either.
    """
    registry = registry or load_registry()
    result = {
        'valid': False,
        'source_id': '',
        'source_family': '',
        'outcome': '',
        'searched': 0,
        'queries': [],
        'accepted': [],
        'rejected': [],
        'warnings': [],
        'errors': [],
        'query_id': '',
        'search_family': '',
        'queries_executed': 0,
        'candidate_count': 0,
        'new_candidate_estimate': None,
        'coverage_notes': [],
    }
    if not isinstance(raw, dict):
        result['errors'].append({'field': '_root', 'problem': 'not_an_object',
                                 'value': type(raw).__name__})
        return result

    source_id = collapse(raw.get('source_id'))
    result['source_id'] = source_id
    if not source_id:
        result['errors'].append({'field': 'source_id', 'problem': 'required'})
    elif not is_known_source(source_id, registry):
        result['errors'].append({'field': 'source_id', 'value': source_id,
                                 'problem': 'not_in_source_registry'})
    else:
        family = source_family(source_id, registry)
        result['source_family'] = family
        declared = collapse(raw.get('source_family'))
        if declared and declared != family:
            result['errors'].append({'field': 'source_family', 'value': declared,
                                     'problem': 'contradicts_registry', 'expected': family})

    outcome = collapse(raw.get('outcome')).lower()
    result['outcome'] = outcome
    if not outcome:
        result['errors'].append({'field': 'outcome', 'problem': 'required'})
    elif outcome not in SOURCE_OUTCOMES:
        result['errors'].append({'field': 'outcome', 'value': raw.get('outcome'),
                                 'problem': 'not_in_vocabulary',
                                 'allowed': list(SOURCE_OUTCOMES)})

    searched = raw.get('searched')
    if isinstance(searched, bool):
        result['errors'].append({'field': 'searched', 'value': searched, 'problem': 'not_a_count'})
    elif isinstance(searched, int):
        result['searched'] = searched
    elif isinstance(searched, (list, tuple)):
        result['queries'] = [collapse(q) for q in searched if collapse(q)]
        result['searched'] = len(result['queries'])
    elif searched in (None, ''):
        result['errors'].append({'field': 'searched', 'problem': 'required',
                                 'hint': 'a search count, or the list of queries run'})
    else:
        result['errors'].append({'field': 'searched', 'value': searched,
                                 'problem': 'not_a_count_or_query_list'})

    warnings = raw.get('warnings', [])
    if warnings in (None, ''):
        warnings = []
    if not isinstance(warnings, (list, tuple)):
        result['errors'].append({'field': 'warnings', 'value': warnings, 'problem': 'not_a_list'})
    else:
        result['warnings'] = [collapse(w) for w in warnings if collapse(w)]

    # Optional query-task echo. A worker that was given a bounded task reports back
    # which one it ran and what it cost, so the parent can record query-level
    # coverage without guessing. These are additive: an older envelope without them
    # stays valid.
    result['query_id'] = collapse(raw.get('query_id'))
    result['search_family'] = collapse(raw.get('search_family'))
    executed = raw.get('queries_executed')
    if isinstance(executed, bool):
        result['errors'].append({'field': 'queries_executed', 'value': executed,
                                 'problem': 'not_a_count'})
    elif isinstance(executed, int):
        result['queries_executed'] = max(0, executed)
    elif isinstance(executed, (list, tuple)):
        result['queries_executed'] = len(executed)
    elif executed not in (None, ''):
        result['errors'].append({'field': 'queries_executed', 'value': executed,
                                 'problem': 'not_a_count_or_query_list'})
    for field in ('candidate_count', 'new_candidate_estimate'):
        value = raw.get(field)
        if isinstance(value, bool) or (value not in (None, '') and not isinstance(value, int)):
            result['errors'].append({'field': field, 'value': value, 'problem': 'not_a_count'})
        elif isinstance(value, int):
            result[field] = max(0, value)
    notes = raw.get('coverage_notes', [])
    if notes in (None, ''):
        notes = []
    if isinstance(notes, str):
        notes = [notes]
    if not isinstance(notes, (list, tuple)):
        result['errors'].append({'field': 'coverage_notes', 'problem': 'not_a_list'})
    else:
        result['coverage_notes'] = [collapse(n) for n in notes if collapse(n)]

    candidates = raw.get('candidates', [])
    if candidates in (None, ''):
        candidates = []
    if not isinstance(candidates, (list, tuple)):
        result['errors'].append({'field': 'candidates', 'value': type(candidates).__name__,
                                 'problem': 'not_a_list'})
        return result

    envelope_ok = not result['errors']
    if not envelope_ok:
        # An envelope-level problem taints every row in it: a wrong source_id
        # misattributes each candidate, and an unreadable outcome means the source's
        # health is unknown. Nothing here is ingestible.
        result['rejected'] = [{
            'index': index,
            'errors': [{'field': '_envelope', 'problem': 'envelope_invalid'}],
            'title': collapse(entry.get('title')) if isinstance(entry, dict) else '',
            'source_url': collapse(entry.get('source_url')) if isinstance(entry, dict) else '',
        } for index, entry in enumerate(candidates)]
        result['valid'] = False
        return result

    for index, entry in enumerate(candidates):
        payload = dict(entry) if isinstance(entry, dict) else entry
        if isinstance(payload, dict) and source_id and not collapse(payload.get('source_id')):
            payload['source_id'] = source_id
        candidate, errors = normalise_candidate(payload, registry=registry)
        if errors:
            result['rejected'].append({'index': index, 'errors': errors,
                                       'title': collapse((entry or {}).get('title'))
                                       if isinstance(entry, dict) else '',
                                       'source_url': collapse((entry or {}).get('source_url'))
                                       if isinstance(entry, dict) else ''})
            continue
        result['accepted'].append(candidate)

    # An envelope only counts as valid when it is structurally sound AND every
    # candidate in it validated. `accepted` never contains a rejected row either way.
    result['valid'] = not result['rejected']
    return result


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def read_json_input(args):
    if getattr(args, 'file', ''):
        path = Path(args.file)
        if not path.exists():
            raise candidate_error(f'Input file not found: {path}')
        raw = path.read_text(encoding='utf-8')
    else:
        raw = sys.stdin.read()
    # Windows shells routinely prefix piped text with a byte-order mark.
    raw = raw.lstrip('﻿')
    if not raw.strip():
        raise candidate_error(
            'No JSON input received.',
            'Pass --file <path> or pipe a JSON document on stdin.')
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise candidate_error(
            'Malformed JSON input.',
            f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None


def cmd_schema(args):
    print(json.dumps({
        'schema_version': SCHEMA_VERSION,
        'required_fields': list(REQUIRED_FIELDS),
        'text_fields': list(TEXT_FIELDS),
        'number_fields': list(NUMBER_FIELDS),
        'int_fields': list(INT_FIELDS),
        'fact_fields': list(PROJECTED_FACT_FIELDS),
        'vocabularies': {
            'lead_type': list(LEAD_TYPES),
            'source_confidence': list(SOURCE_CONFIDENCES),
            'source_type': list(SOURCE_TYPES),
            'fit_band': list(FIT_BANDS),
            'sponsorship_label': list(SPONSORSHIP_LABELS),
            'employment_type': list(EMPLOYMENT_TYPES),
            'work_pattern': list(WORK_PATTERNS),
            'outcome': list(SOURCE_OUTCOMES),
        },
    }, indent=2, ensure_ascii=False))


def cmd_normalize(args):
    payload = read_json_input(args)
    items = payload if isinstance(payload, list) else [payload]
    out, problems = [], []
    for index, entry in enumerate(items):
        candidate, errors = normalise_candidate(entry)
        if errors:
            problems.append({'index': index, 'errors': errors})
        else:
            out.append(candidate)
    print(json.dumps({
        'count': len(out),
        'rejected': len(problems),
        'candidates': out,
        'problems': problems,
    }, indent=2, ensure_ascii=False))
    raise SystemExit(0 if not problems else 1)


def cmd_validate_worker(args):
    result = validate_worker_output(read_json_input(args))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result['valid'] else 1)


def cmd_split_chrome(args):
    """Report the vacancy body and the platform metadata separately."""
    if args.file:
        body = Path(args.file).read_text(encoding='utf-8', errors='replace')
    else:
        body = sys.stdin.read()
    split = split_platform_chrome(body, args.source_id, args.source_host)
    print(json.dumps({
        'source_id': args.source_id,
        'source_host': args.source_host,
        'carries_platform_chrome': carries_platform_chrome(args.source_id, args.source_host),
        'chrome_removed': split['chrome_removed'],
        'description_unavailable': split['description_unavailable'],
        'cut_at': split['cut_at'],
        'description_chars': len(split['description']),
        'original_chars': len(body),
        'platform_metadata': split['platform_metadata'],
        'platform_metadata_source': split['provenance'],
        'description': split['description'],
        'reason': split.get('reason', ''),
        'note': ('The platform metadata is the SEARCH PLATFORM\'s own classification of '
                 'the role, not the employer\'s words. It is never employer-stated '
                 'evidence and never a hard blocker on its own. When '
                 'description_unavailable is true the vacancy body was never isolated: '
                 'cache nothing for the description rather than storing the block.'),
    }, indent=2, ensure_ascii=False))


def cmd_check_panel(args):
    """Refuse an extracted block that is a recommendation panel, not a result list."""
    if args.file:
        body = Path(args.file).read_text(encoding='utf-8', errors='replace')
    else:
        body = sys.stdin.read()
    hits = forbidden_panel_hits(args.source_id, body)
    print(json.dumps({
        'source_id': args.source_id,
        'is_forbidden_panel': bool(hits),
        'markers_found': hits,
        'verdict': 'do_not_ingest' if hits else 'looks_like_a_result_list',
        'reason': (
            'This block matches this source\'s known recommendation-panel wording. It is '
            'not the filtered result list, it ignores the requested posted-within '
            'filter, and it must never be ingested as discovery inventory. Classify the '
            'source as changed_layout or partial instead.'
        ) if hits else '',
        'filter_is_trustworthy': filter_is_trustworthy(args.source_id),
        'promoted_card_markers': promoted_card_markers(args.source_id),
    }, indent=2, ensure_ascii=False))
    raise SystemExit(1 if hits else 0)


def cmd_window(args):
    print(json.dumps({
        'posted': args.posted,
        'posted_raw': args.posted_raw,
        'window_days': args.window_days,
        'today': args.today or date.today().isoformat(),
        'age_days_from_raw': age_days_from_raw(args.posted_raw),
        'eligibility': window_eligibility(args.posted, args.posted_raw, args.window_days, args.today),
    }, ensure_ascii=False))


def cmd_run_window(args):
    windows = [w.strip() for w in (args.windows or '').split(',') if w.strip()]
    print(json.dumps(run_window_gate(args.posted, args.posted_raw, windows, args.today),
                     indent=2, ensure_ascii=False))


def cmd_body_signal(args):
    """Print the verdict, then signal it through the exit code.

    INTEGRATION CONTRACT, learned the hard way in the first real run: a non-zero
    exit here means LOW_SIGNAL, NOT tool failure. The verdict is always valid JSON
    on stdout. A caller that treats `returncode != 0` as an error will silently
    discard ten legitimate LOW_SIGNAL verdicts, which is exactly what happened.

    Callers MUST parse stdout first and only treat the call as failed when stdout
    does not contain a JSON object with a `verdict` key.
    """
    text = Path(args.file).read_text(encoding='utf-8') if args.file else sys.stdin.read()
    verdict = body_signal_gate(text, title=args.title, hard_blocker=args.hard_blocker)
    verdict['exit_code_contract'] = (
        'Exit 0 = KEEP_FOR_DEEP_CHECK, exit 1 = LOW_SIGNAL, exit 2 = HARD_REJECT. '
        'A non-zero exit is a VERDICT, not a failure: parse stdout.')
    print(json.dumps(verdict, indent=2, ensure_ascii=False))
    raise SystemExit(BODY_SIGNAL_EXIT.get(verdict['verdict'], 1))


def cmd_sponsorship_signal(args):
    text = Path(args.file).read_text(encoding='utf-8') if args.file else sys.stdin.read()
    print(json.dumps(sponsorship_signal(text), indent=2, ensure_ascii=False))


def cmd_title_blockers(args):
    config = None
    if args.config:
        config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    print(json.dumps(title_blockers(args.title, candidate_config=config),
                     indent=2, ensure_ascii=False))


def cmd_consolidate(args):
    rows = read_json_input(args)
    if not isinstance(rows, list):
        raise candidate_error('consolidate expects a JSON array of candidates.')
    result = consolidate(rows)
    if not args.verbose:
        result.pop('candidates', None)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_validate_query_task(args):
    task = read_json_input(args)
    result = validate_query_task(task)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result['valid'] else 1)


def main():
    p = argparse.ArgumentParser(description='Structured discovery candidate helper')
    sub = p.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('schema', help='Print the candidate schema and vocabularies.')
    s.set_defaults(func=cmd_schema)

    n = sub.add_parser('normalize', help='Validate and normalise candidates from stdin or --file.')
    n.add_argument('--file', default='')
    n.set_defaults(func=cmd_normalize)

    w = sub.add_parser('validate-worker', help='Validate a public-job-researcher return envelope.')
    w.add_argument('--file', default='')
    w.set_defaults(func=cmd_validate_worker)

    cp = sub.add_parser('check-panel', help='Refuse extracted text that is a recommendation panel.')
    cp.add_argument('--source-id', dest='source_id', required=True)
    cp.add_argument('--file', default='')
    cp.set_defaults(func=cmd_check_panel)

    pc = sub.add_parser('split-chrome',
                        help='Separate a vacancy body from the search platform\'s own '
                             'page furniture.')
    pc.add_argument('--source-id', dest='source_id', default='')
    pc.add_argument('--source-host', dest='source_host', default='')
    pc.add_argument('--file', default='')
    pc.set_defaults(func=cmd_split_chrome)

    rw = sub.add_parser('run-window',
                        help='Judge one candidate against the widest window its run '
                             'actually activated.')
    rw.add_argument('--posted', default='')
    rw.add_argument('--posted-raw', dest='posted_raw', default='')
    rw.add_argument('--windows', default='',
                    help="Comma separated windows the run used, e.g. '24h,7d'.")
    rw.add_argument('--today', default='')
    rw.set_defaults(func=cmd_run_window)

    ss = sub.add_parser('sponsorship-signal',
                        help='Read sponsorship wording out of a vacancy body. '
                             'Negation always beats a positive form.')
    ss.add_argument('--file', default='')
    ss.set_defaults(func=cmd_sponsorship_signal)

    tb = sub.add_parser('title-blockers',
                        help='Deterministic blockers decidable from a title alone.')
    tb.add_argument('--title', required=True)
    tb.add_argument('--config', default='',
                    help='Candidate config path. Defaults to candidate/config.json.')
    tb.set_defaults(func=cmd_title_blockers)

    bs = sub.add_parser('body-signal', help='Cheap gate: is this body worth a deep check?')
    bs.add_argument('--file', default='')
    bs.add_argument('--title', default='')
    bs.add_argument('--hard-blocker', dest='hard_blocker', default='',
                    help='An existing deterministic blocker, if one already applies.')
    bs.set_defaults(func=cmd_body_signal)

    co = sub.add_parser('consolidate', help='Merge the same vacancy found across sources.')
    co.add_argument('--file', default='')
    co.add_argument('--verbose', action='store_true', help='Include the merged candidates.')
    co.set_defaults(func=cmd_consolidate)

    qt = sub.add_parser('validate-query-task', help='Validate one bounded worker query task.')
    qt.add_argument('--file', default='')
    qt.set_defaults(func=cmd_validate_query_task)

    f = sub.add_parser('window', help='Decide one candidate freshness window from its own date/age.')
    f.add_argument('--posted', default='')
    f.add_argument('--posted-raw', dest='posted_raw', default='')
    f.add_argument('--window-days', dest='window_days', type=int, default=1)
    f.add_argument('--today', default='')
    f.set_defaults(func=cmd_window)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

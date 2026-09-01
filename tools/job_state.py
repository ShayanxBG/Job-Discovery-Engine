#!/usr/bin/env python3
"""Deduplicated job discovery state helper.

Identity, controlled machine vocabularies, durable writes, and read-only diagnosis
for `job_scraper/seen_jobs.json`.
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'job_scraper' / 'seen_jobs.json'
BACKUP_DIR = ROOT / 'backups' / 'discovery-state'
DAILY_BACKUP_DIR = BACKUP_DIR / 'daily'
DAMAGED_DIR = BACKUP_DIR / 'damaged'
LAST_KNOWN_GOOD = BACKUP_DIR / 'seen_jobs-last-known-good.json'
DAILY_BACKUP_KEEP = 14

# Schema 2 adds the additive machine-readable fields sponsorship_label and fit_band
# alongside the existing human-readable sponsorship/quick_fit evidence.
SCHEMA_VERSION = 2

# Controlled machine vocabularies. Human-readable evidence stays in `sponsorship`
# and `quick_fit`; these are the only fields state logic is allowed to rank.
SPONSORSHIP_LABELS = ('unknown', 'blocked', 'weak', 'moderate', 'strong')
FIT_BANDS = ('unknown', 'low', 'medium', 'high')
LEAD_TYPES = ('direct', 'agency', 'verification')
SOURCE_CONFIDENCES = ('low', 'medium', 'high')
# Statuses legitimately used by this project: `new` and `updated` are written by
# discovery, `ranked` by /rank, and `dismissed`/`expired` by triage.
STATUSES = ('new', 'updated', 'ranked', 'dismissed', 'expired')

# Run-window eligibility. This is deliberately SEPARATE from `status` and from
# `fit_band`, because it answers a different question: not "is this vacancy any
# good" and not "where is it in its lifecycle", but "did the run that produced it
# actually ask for a vacancy this old".
#
# It exists because a candidate can be discovered with no posted date and only
# acquire an authoritative one later, when the parent opens the official ATS page.
# In the first production run, Letly's role proved to be 15 days old in a run that
# searched 24h only, yet remained an active Direct opportunity because its date was
# unknown at discovery time.
#
#   in_window          judged against the widest window the run activated, and inside it
#   out_of_window      authoritative date proves it is older than the run asked for
#   unknown_freshness  no authoritative date; NOT stale, and still eligible when open
#
# Absent is legitimate and means "never assessed": records written before this field
# existed stay readable and stay eligible.
RUN_WINDOWS = ('in_window', 'out_of_window', 'unknown_freshness')

# Structured-fact vocabularies. `facts` is an additive object holding what a
# vacancy actually stated, kept strictly separate from the human evidence prose in
# `quick_fit` and `sponsorship`. Existing records without `facts` stay valid: the
# object is written only when a source actually supplied facts, never backfilled
# empty.
# SIX distinct engagement facts, because the word `contract` means at least three
# different things in a UK job advert and only one of them is a reason to walk away.
#
#   permanent             a permanent employee
#   fixed-term            a DIRECTLY EMPLOYED employee with an end date
#   temporary             a temporary agency placement
#   contract              independent contracting, or labour supplied to a third party
#   freelance             freelance or self-employed, the same category of fact
#   contract-unspecified  the advert said `contract` and did not say whose
#
# Official sponsor guidance refuses a Certificate of Sponsorship for supplying a
# worker to a third party and prohibits nothing about employing one for a fixed
# period, so a vocabulary that cannot tell those apart discards genuine sponsored
# jobs for a contractor's reasons. `contract-unspecified` exists because the honest
# answer to ambiguous wording is not one of the other five: it maps to no blocker
# at all and raises an employment_type verification need instead.
#
# MIGRATION. `contract` keeps its value and its meaning, so every historical record
# stays readable and none is rewritten. What changed is that the blocker now also
# requires employer wording that names an independent arrangement, so a `contract`
# fact recorded from ambiguous wording before this distinction existed can no
# longer eliminate a vacancy on its own. See config/immigration_rules.json.
EMPLOYMENT_TYPES = ('unknown', 'permanent', 'fixed-term', 'temporary',
                    'contract', 'freelance', 'contract-unspecified',
                    'apprenticeship', 'internship')
WORK_PATTERNS = ('unknown', 'remote', 'hybrid', 'on-site')
FACT_FIELDS = (
    'salary_min', 'salary_max', 'salary_currency', 'salary_raw',
    'employment_type', 'work_pattern', 'country',
    'years_required_min', 'years_required_max',
    'skills', 'posted_raw', 'closing_date', 'description_hash', 'extracted_at',
)
FACT_NUMBER_FIELDS = ('salary_min', 'salary_max', 'years_required_min', 'years_required_max')

# Field-level fact provenance. `/rank` reuses stored facts, so the authority
# behind each individual field has to stay visible: an aggregator-filled salary
# must never read as if the employer ATS stated it. One provenance object for the
# whole facts block cannot express that, because different fields legitimately
# come from different sources. Provenance is derived by state code from the
# incoming source metadata, never written as free-form model prose, and is
# additive: records with no facts get no provenance.
PROVENANCE_FIELDS = ('source_type', 'source_url', 'source_host', 'observed_at')
# Transient key on an incoming record. merge_item consumes it to stamp provenance
# and never stores it.
FACTS_SOURCE_FIELD = 'facts_source'

FIT_RANK = {'': 0, 'unknown': 0, 'low': 1, 'medium': 2, 'high': 3}
SPONSOR_RANK = {'': 0, 'unknown': 0, 'blocked': 0, 'weak': 1, 'moderate': 2, 'strong': 3}
CONF_RANK = {'': 0, 'low': 1, 'medium': 2, 'high': 3}
LEAD_RANK = {'': 0, 'agency': 1, 'verification': 1, 'direct': 2}
# Unknown tokens fall back to 0, which would let a weak aggregator overwrite a
# stronger employer URL, so every token written by discovery must appear here.
# validate_workspace.py --deep asserts that seen_jobs.json contains no unknown token.
SOURCE_RANK = {
    '': 0, 'aggregator': 1, 'sponsor-board': 1, 'agency-board': 1,
    # Bare 'linkedin' does not say whether the page was authenticated, so it is
    # ranked at board level: above an aggregator, below any employer source.
    'major-board': 2, 'uk-board': 2, 'linkedin': 2,
    'authenticated-linkedin': 3, 'authenticated-indeed': 3, 'authenticated-board': 3,
    # 'employer-ats' and 'ats' are the same thing and must rank identically.
    'employer-direct': 4, 'ats': 4, 'employer-ats': 4, 'official': 4,
}
SOURCE_TYPES = tuple(x for x in SOURCE_RANK if x)

# Some boards identify a vacancy in the query string instead of the path.
# Preserve only identity-bearing parameters and discard tracking parameters.
ID_PARAMS = (
    'jk', 'vjk', 'currentjobid', 'jobid', 'job_id',
    'requisitionid', 'requisition_id', 'reqid', 'gh_jid', 'id',
)

# Verified Greenhouse host family. Same tenant + same job ID on any of these hosts
# is one vacancy. Tenant stays in the canonical path so tenant A job 123 and
# tenant B job 123 remain different jobs. Do not widen this set without evidence.
GREENHOUSE_HOSTS = frozenset({
    'boards.greenhouse.io',
    'job-boards.greenhouse.io',
    'job-boards.eu.greenhouse.io',
})
GREENHOUSE_CANONICAL_HOST = 'boards.greenhouse.io'
LINKEDIN_VIEW_PATH = re.compile(r'^/jobs/view/(\d+)$')

# Source-owned identity fields. A weaker board/aggregator copy may fill one of
# these when it is empty, but must never overwrite the value already owned by a
# stronger employer/ATS source.
SOURCE_OWNED_FIELDS = frozenset({
    'url', 'source', 'source_type', 'source_confidence', 'source_host',
    'job_id', 'requisition_id',
})

# Match/evidence fields derived from a vacancy source. A weaker source may fill
# an empty value but must not downgrade or replace evidence already derived from
# a stronger preferred source.
SOURCE_DERIVED_MATCH_FIELDS = frozenset({
    'quick_fit', 'fit_band', 'sponsorship', 'sponsorship_label',
    'lead_type', 'filter_reason',
})

# These machine fields are required on every persisted schema-v2 job record.
REQUIRED_MACHINE_FIELDS = (
    'fit_band', 'sponsorship_label', 'lead_type', 'status',
    'source_type', 'source_confidence',
)


# --------------------------------------------------------------------------
# Durable persistence
# --------------------------------------------------------------------------

def atomic_write_text(path, text):
    """Write via a same-directory temporary file, fsync, then os.replace.

    The original file stays intact and readable until the replacement is complete.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f'.{path.name}.', suffix='.tmp')
    tmp_path = Path(tmp_name)
    try:
        # Default newline translation keeps the workspace's existing line endings.
        with os.fdopen(handle_fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


_backup_taken = False


def recovery_backups():
    """Every recoverable copy of discovery state, most useful first."""
    found = []
    if LAST_KNOWN_GOOD.exists():
        found.append(LAST_KNOWN_GOOD)
    if DAILY_BACKUP_DIR.exists():
        found.extend(sorted(DAILY_BACKUP_DIR.glob('seen_jobs-*.json'), reverse=True))
    if BACKUP_DIR.exists():
        found.extend(sorted(BACKUP_DIR.glob('seen_jobs-pre-reset-*.json'), reverse=True))
    return found


def ensure_backup():
    """Bounded backup policy, applied once per process before the first replacement.

    1. `backups/discovery-state/seen_jobs-last-known-good.json` is refreshed from the
       current healthy file before this process replaces it.
    2. `backups/discovery-state/daily/seen_jobs-<date>.json` is written once per day.
    3. Daily copies are pruned to the newest DAILY_BACKUP_KEEP.

    A run that saves sixty discovered jobs therefore creates at most two files, not
    one per job, while a recoverable recent copy always exists.
    """
    global _backup_taken
    if _backup_taken or not STATE.exists():
        return
    _backup_taken = True
    try:
        raw = STATE.read_text(encoding='utf-8')
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get('seen'), dict):
            return  # Never overwrite a good backup with a damaged file.
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(LAST_KNOWN_GOOD, raw)
    DAILY_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    daily = DAILY_BACKUP_DIR / f'seen_jobs-{date.today().isoformat()}.json'
    if not daily.exists():
        atomic_write_text(daily, raw)
    keep = sorted(DAILY_BACKUP_DIR.glob('seen_jobs-*.json'), reverse=True)[DAILY_BACKUP_KEEP:]
    for stale in keep:
        stale.unlink(missing_ok=True)


def state_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def parse_state(raw, origin):
    """Parse discovery state, raising an actionable message instead of a traceback."""
    if not raw.strip():
        raise state_error(
            f'Discovery state is empty: {origin}',
            'The file exists but contains no JSON, which usually means a truncated write.',
            'Run: python tools/job_state.py doctor',
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise state_error(
            f'Malformed discovery state: {origin}',
            f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}',
            'Run: python tools/job_state.py doctor           (read-only report)',
            'Then: python tools/job_state.py doctor --repair (restore a validated backup)',
        ) from None
    if not isinstance(data, dict):
        raise state_error(
            f'Invalid discovery state: {origin}',
            f'Expected a JSON object of the form {{"seen": {{...}}}}, found {type(data).__name__}.',
            'Run: python tools/job_state.py doctor',
        )
    if not isinstance(data.get('seen'), dict):
        raise state_error(
            f'Invalid discovery state: {origin}',
            'Expected a top-level "seen" object mapping state keys to job records.',
            'Run: python tools/job_state.py doctor',
        )
    schema_problems = state_schema_violations(data)
    vocab_problems = vocabulary_violations(data['seen'])
    if schema_problems or vocab_problems:
        raise state_error(
            f'Discovery state does not satisfy schema version {SCHEMA_VERSION}: {origin}',
            f'Schema problems: {len(schema_problems)}; vocabulary problems: {len(vocab_problems)}.',
            'Run: python tools/job_state.py doctor',
        )
    return data


def load_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE.exists():
        backups = recovery_backups()
        if backups:
            # Case B: a previously populated workspace whose state has disappeared.
            # Never silently initialise empty history over recoverable discovery.
            raise state_error(
                f'Discovery state is missing but recovery backups exist: {STATE}',
                f'Most recent recoverable copy: {backups[0].relative_to(ROOT).as_posix()}',
                f'Backups available: {len(backups)}',
                'Refusing to start empty history over recoverable discovery.',
                'Run: python tools/job_state.py doctor           (read-only report)',
                'Then: python tools/job_state.py doctor --repair (restore a validated backup)',
            )
        # Case A: genuine first-run workspace.
        save_state({'seen': {}})
    try:
        raw = STATE.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise state_error(
            f'Discovery state could not be read: {STATE}',
            f'{type(exc).__name__}: {exc}',
            'Run: python tools/job_state.py doctor',
        ) from None
    return parse_state(raw, STATE)


def save_state(data):
    ensure_backup()
    payload = {'schema_version': SCHEMA_VERSION, 'seen': data.get('seen', {})}
    for key, value in data.items():
        if key not in payload:
            payload[key] = value
    atomic_write_text(STATE, json.dumps(payload, indent=2, ensure_ascii=False) + '\n')


# --------------------------------------------------------------------------
# Normalisation and identity
# --------------------------------------------------------------------------

def norm_text(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def norm_location(s):
    value = norm_text(s)
    value = re.sub(r'\b(united kingdom|great britain)\b', '', value)
    value = re.sub(r'\buk\b', '', value)
    return re.sub(r'\s+', ' ', value).strip()


def canon_host(host):
    """Canonical host: lowercase, no leading www., verified Greenhouse aliases folded."""
    host = (host or '').strip().lower()
    if host.startswith('www.'):
        host = host[4:]
    if host in GREENHOUSE_HOSTS:
        return GREENHOUSE_CANONICAL_HOST
    return host


def is_linkedin(host):
    return host == 'linkedin.com' or host.endswith('.linkedin.com')


def is_indeed(host):
    return host == 'indeed.com' or host.endswith('.indeed.com')


def source_host(url):
    if not url:
        return ''
    try:
        return canon_host(urlsplit(url.strip()).netloc)
    except Exception:
        return ''


def hosts_compatible(first, second):
    """True when two hosts can describe the same source family.

    Equal canonical hosts match, and so does a registrable-domain generalisation
    such as greenhouse.io for boards.greenhouse.io. Unrelated hosts do not.
    """
    first, second = canon_host(first), canon_host(second)
    if not first or not second or first == second:
        return True
    return first.endswith('.' + second) or second.endswith('.' + first)


def resolve_source_host(url, provided=''):
    """Derive source_host from the canonical URL, rejecting an inconsistent override."""
    derived = source_host(url)
    provided = canon_host(provided)
    if not provided:
        return derived
    if not derived:
        return provided
    if not hosts_compatible(derived, provided):
        raise state_error(
            f'Inconsistent --source-host {provided!r} for URL host {derived!r}.',
            'source_host must belong to the same canonical host family as the URL.',
            'Fix the URL or the --source-host value rather than storing an impossible pair.',
        )
    return derived


def norm_url(url):
    """Canonical vacancy identity for a URL.

    General: http becomes https, a leading www. is dropped, a trailing slash is
    dropped, and tracking parameters are discarded while identity-bearing
    parameters are preserved.

    Host-specific, verified equivalences only:
    - LinkedIn /jobs/view/<id>, /jobs/search?currentJobId=<id> and
      /jobs/collections/...?currentJobId=<id> resolve to one identity.
    - Indeed /viewjob?jk=<id> and /jobs?vjk=<id> resolve to one identity, while
      distinct jk/vjk IDs stay distinct.
    - The verified Greenhouse host family folds to one host, keeping the tenant.
    """
    if not url:
        return ''
    try:
        parts = urlsplit(url.strip())
        host = canon_host(parts.netloc)
        path = parts.path.rstrip('/')
        ids = []
        for key, value in parse_qsl(parts.query, keep_blank_values=False):
            token = key.strip().lower()
            value = value.strip()
            if token in ID_PARAMS and value:
                ids.append((token, value))
        lookup = dict(ids)

        if is_linkedin(host):
            match = LINKEDIN_VIEW_PATH.match(path)
            job_id = match.group(1) if match else lookup.get('currentjobid', '')
            if job_id:
                path = f'/jobs/view/{job_id}'
                ids = []
        elif is_indeed(host):
            job_id = lookup.get('jk') or lookup.get('vjk')
            if job_id:
                path = '/viewjob'
                ids = [('jk', job_id)]

        return urlunsplit(('https', host, path, urlencode(sorted(ids)), ''))
    except Exception:
        return url.strip().lower().rstrip('/')


def canonical_key_index(seen):
    """Map every existing state key to its canonical URL identity.

    Historical keys were stored before canonicalisation, so a rediscovered origin
    URL has to be compared canonically rather than by raw string equality.
    """
    index = {}
    for key in seen:
        index.setdefault(norm_url(key), key)
    return index


def iso_date(value):
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def rank_value(mapping, value):
    return mapping.get((value or '').strip().lower(), 0)


def exact_duplicate(seen, url, company, requisition_id='', job_id='', incoming_host=''):
    nu = norm_url(url)
    nc = norm_text(company)
    incoming_host = canon_host(incoming_host or source_host(url))
    req = norm_text(requisition_id)
    jid = norm_text(job_id)

    # An aggregator advert that was later resolved to an employer/ATS page keeps the
    # aggregator URL as its state key. Rediscovering that origin must return the same
    # record instead of minting a second one.
    if nu:
        origin_key = canonical_key_index(seen).get(nu)
        if origin_key is not None:
            return origin_key, 'origin_url'

    for key, item in seen.items():
        if nu and norm_url(item.get('url', '')) == nu:
            return key, 'url'

        old_company = norm_text(item.get('company', ''))
        if req and nc and old_company == nc and norm_text(item.get('requisition_id', '')) == req:
            return key, 'company_requisition_id'

        old_jid = norm_text(item.get('job_id', ''))
        old_host = canon_host(item.get('source_host') or source_host(item.get('url', '')))
        if jid and incoming_host and old_jid == jid and old_host == incoming_host:
            return key, 'source_job_id'

    return None, None


def possible_duplicates(seen, company, title, location=''):
    nc, nt, nl = norm_text(company), norm_text(title), norm_location(location)
    matches = []
    if not nc or not nt:
        return matches

    for key, item in seen.items():
        if norm_text(item.get('company', '')) != nc or norm_text(item.get('title', '')) != nt:
            continue
        old_loc = norm_location(item.get('location', ''))
        if nl and old_loc and nl == old_loc:
            reason = 'company_title_location'
        else:
            reason = 'company_title_possible'
        matches.append({
            'key': key,
            'reason': reason,
            'title': item.get('title', ''),
            'company': item.get('company', ''),
            'location': item.get('location', ''),
            'url': item.get('url', ''),
            'posted': item.get('posted', ''),
            'requisition_id': item.get('requisition_id', ''),
            'job_id': item.get('job_id', ''),
        })
    return matches


def source_rank(item):
    return SOURCE_RANK.get((item.get('source_type') or '').strip().lower(), 0)


# --------------------------------------------------------------------------
# Controlled-vocabulary write boundary
# --------------------------------------------------------------------------

def validate_choice(field, value, allowed, normalise=True):
    """Reject a value outside its controlled vocabulary at the write boundary."""
    raw = (value or '').strip()
    if not raw:
        return ''
    token = raw.lower()
    if token not in allowed:
        raise state_error(
            f'Invalid {field}: {raw!r}',
            f'Allowed values: {", ".join(allowed)}',
        )
    return token if normalise else raw


def state_schema_violations(data):
    """Schema-v2 requirements that must hold for current state and recovery copies."""
    problems = []
    if data.get('schema_version') != SCHEMA_VERSION:
        problems.append({
            'field': 'schema_version',
            'value': data.get('schema_version'),
            'expected': SCHEMA_VERSION,
        })
    seen = data.get('seen')
    if not isinstance(seen, dict):
        problems.append({'field': 'seen', 'value': type(seen).__name__, 'expected': 'object'})
        return problems
    for key, item in seen.items():
        if not isinstance(item, dict):
            problems.append({'key': key, 'field': 'record', 'value': type(item).__name__, 'expected': 'object'})
            continue
        for field in REQUIRED_MACHINE_FIELDS:
            value = str(item.get(field, '')).strip()
            if not value:
                problems.append({'key': key, 'field': field, 'value': item.get(field), 'expected': 'non-empty'})
    return problems


# --------------------------------------------------------------------------
# Structured ranking evaluation.
#
# ADDITIVE. `rank_score` and `rank_verdict` remain exactly what they were; this
# stores the MACHINE form of the same decision beside them, so a stored ranking
# can be re-audited without parsing English. Before it existed, a hard blocker
# survived only inside prose like "Skip - hard blocker: wrong_primary_language
# (40/100)": nothing stored said the role was ineligible, and nothing stored
# could confirm that 40 was the sum of its components.
#
# It is written ONLY from the output of tools/match_evaluation.py, which remains
# the sole authority on arithmetic, bands and eligibility. This boundary
# re-validates rather than trusts, and REJECTS rather than repairs, for the same
# reason the evaluator does: silently correcting a component would hide a caller
# that has misread the scoring model.
#
# A record without an `evaluation` is valid and always will be. Historical
# rankings are never backfilled: an absent evaluation means the score was
# recorded before this field existed, which is a knowable unknown.
# --------------------------------------------------------------------------
#
# SCHEMA 2 adds EVIDENCE GROUNDING: a per-component `ceiling`, the run's
# `uncertainty_summary`, the `facts_used` the calculation consumed, and hard
# blockers carrying structured evidence, the factual precondition they had to
# satisfy and what was actually compared. Schema 1 objects stay READABLE for ever,
# because a ranking recorded before this existed is history rather than a fault;
# but a schema 1 object may no longer be WRITTEN, because accepting one now would
# be an open downgrade path around every rule below.
EVALUATION_SCHEMA_VERSION = 2
SUPPORTED_EVALUATION_SCHEMA_VERSIONS = (1, 2)
EVALUATION_UNCERTAINTY = ('known', 'partial', 'unknown')
EVALUATION_MAX_SCORES = (75, 100)
EVALUATION_MIN_EVIDENCE_CHARS = 8
EVALUATION_MAX_EVIDENCE_CHARS = 400
_POLICY_MAXIMA_CACHE = {}


def _evaluator():
    """The module that OWNS the evaluation rules, imported at call time.

    Failure to import is FATAL to the caller rather than skipped: a boundary that
    silently stops checking is worse than one that says it cannot check.
    """
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import match_evaluation
    return match_evaluation


def _recompute_problems(evaluation, seen):
    """Re-derive a submitted evaluation from LIVE configuration and canonical data.

    `computed_by` is descriptive metadata. Any caller can write the string
    `tools/match_evaluation.py`, so it establishes nothing about provenance and is
    never treated as proof. What IS proof is reproduction: the deterministic
    evaluator, run now, against the calibration and policy currently on disk and
    the vacancy this workspace actually stored, has to produce the same numbers.
    Totals, ceilings, bands, eligibility and every blocker threshold are therefore
    RECALCULATED here rather than read from the object, and a submitted field that
    disagrees with the fresh calculation is refused.
    """
    try:
        evaluator = _evaluator()
    except Exception as exc:  # pragma: no cover - a stripped tools directory
        return None, [{'field': 'evaluation', 'value': f'{type(exc).__name__}: {exc}',
                       'reason': 'evaluation_rules_are_unavailable'}]
    try:
        import canonical_vacancy
    except Exception as exc:  # pragma: no cover
        return None, [{'field': 'evaluation', 'value': f'{type(exc).__name__}: {exc}',
                       'reason': 'canonical_vacancy_resolver_is_unavailable'}]
    try:
        problems = list(evaluator.fingerprint_problems(evaluation))
        identity = (evaluation.get('canonical_key') or evaluation.get('key')
                    or evaluation.get('url') or '')
        canonical = canonical_vacancy.resolve(identity, seen=seen)
        recomputed, mismatches = evaluator.recompute_stored_evaluation(evaluation, canonical)
    except SystemExit as exc:
        return None, [{'field': 'evaluation', 'value': str(exc),
                       'reason': 'evaluation_could_not_be_recalculated'}]
    problems.extend(mismatches)
    return (recomputed if not problems else None), problems


def _grounding_problems(evaluation):
    """Re-run the evaluator's own grounding rules on an object about to be stored.

    Imported at call time from the module that OWNS those rules, so there is one
    definition of an uncertainty ceiling, a full-marks anchor and a blocker
    precondition rather than a copy here that can quietly fall behind. Failure to
    import is FATAL rather than skipped: a boundary that silently stops checking
    is worse than one that says it cannot.
    """
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import match_evaluation
    except Exception as exc:  # pragma: no cover - a stripped tools directory
        return [{'field': 'evaluation', 'value': f'{type(exc).__name__}: {exc}',
                 'reason': 'evaluation_grounding_rules_are_unavailable'}]
    try:
        return match_evaluation.stored_evaluation_problems(evaluation)
    except SystemExit as exc:
        return [{'field': 'evaluation', 'value': str(exc),
                 'reason': 'evaluation_grounding_rules_could_not_run'}]


def _policy_component_maxima():
    """Component maxima per lead model from the publishable policy, or {} if absent.

    Read lazily so this module stays usable on a workspace that has no policy
    file; when the policy IS present it is authoritative and a component claiming
    a different maximum is refused here as well as in the evaluator.
    """
    if _POLICY_MAXIMA_CACHE:
        return _POLICY_MAXIMA_CACHE
    path = ROOT / 'config' / 'matching_policy.json'
    try:
        policy = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    for model, key in (('direct_model', 'direct'), ('agency_model', 'agency')):
        components = (policy.get(model) or {}).get('components') or {}
        _POLICY_MAXIMA_CACHE[key] = {
            name: block.get('max_score') for name, block in components.items()
            if isinstance(block, dict)
        }
        _POLICY_MAXIMA_CACHE[f'{key}_total'] = (policy.get(model) or {}).get('total_max')
    bands = ((policy.get('direct_model') or {}).get('bands') or [])
    _POLICY_MAXIMA_CACHE['bands'] = tuple(b.get('id') for b in bands if isinstance(b, dict))
    return _POLICY_MAXIMA_CACHE


def evaluation_problems(evaluation, accept_legacy=False, ground=True):
    """Structural problems in a stored ranking evaluation.

    An absent evaluation is not a problem. A present one must be the complete,
    internally consistent machine record of a deterministic evaluation: the
    components must sum to the stored total, every component must sit inside its
    own declared maximum AND inside the policy maximum for its model, the blocker
    and uncertainty vocabularies must hold, and the grounding rules that decide
    whether a score is SUPPORTED must still hold on the stored object.

    `accept_legacy` separates the two questions a version check has to answer. A
    schema 1 object already in state is history and stays readable, so reading
    state passes True. Writing one now is a downgrade around every grounding rule
    added since, so the write path passes False.

    `ground` is off while merely READING state, because the grounding rules need
    `config/matching_policy.json` and opening a discovery record must not start
    depending on a second file. The write boundary and `doctor` both leave it on.
    """
    problems = []
    if evaluation is None:
        return problems
    if not isinstance(evaluation, dict):
        return [{'field': 'evaluation', 'value': type(evaluation).__name__,
                 'reason': 'not_an_object'}]

    version = evaluation.get('schema_version')
    allowed = SUPPORTED_EVALUATION_SCHEMA_VERSIONS if accept_legacy else (EVALUATION_SCHEMA_VERSION,)
    if version not in allowed:
        problems.append({'field': 'schema_version', 'value': version,
                         'reason': 'unsupported_evaluation_schema',
                         'supported': list(allowed)})
    grounded = version == EVALUATION_SCHEMA_VERSION

    lead_type = str(evaluation.get('lead_type') or '').strip().lower()
    if lead_type not in LEAD_TYPES:
        problems.append({'field': 'lead_type', 'value': evaluation.get('lead_type'),
                         'reason': 'unknown_token'})

    # A VERIFICATION LEAD IS UNSCORED. It has no components, no total, no
    # denominator, no band and no decided eligibility, because the fact that would
    # settle those is exactly what is unresolved. It must still name the gate.
    if lead_type == 'verification':
        for field in ('total_score', 'max_score', 'score_band'):
            if evaluation.get(field) is not None:
                problems.append({'field': field, 'value': evaluation.get(field),
                                 'reason': 'verification_lead_is_not_scored'})
        if evaluation.get('components'):
            problems.append({'field': 'components', 'value': 'present',
                             'reason': 'verification_lead_is_not_scored'})
        if evaluation.get('eligible') is not None:
            problems.append({'field': 'eligible', 'value': evaluation.get('eligible'),
                             'reason': 'verification_lead_eligibility_is_undecided'})
        if evaluation.get('hard_blockers'):
            problems.append({'field': 'hard_blockers', 'value': 'present',
                             'reason': 'verification_lead_is_not_blocked'})
        if not evaluation.get('verification_needed'):
            problems.append({'field': 'verification_needed', 'value': None,
                             'reason': 'verification_lead_must_name_its_gate'})
        for index, row in enumerate(evaluation.get('verification_needed') or []):
            if not isinstance(row, dict) or not str(row.get('reason') or '').strip():
                problems.append({'field': f'verification_needed[{index}]', 'value': row,
                                 'reason': 'verification_needs_a_reason'})
        if str(evaluation.get('computed_by') or '').strip() != 'tools/match_evaluation.py':
            problems.append({'field': 'computed_by', 'value': evaluation.get('computed_by'),
                             'reason': 'evaluation_must_come_from_the_deterministic_evaluator'})
        return problems

    max_score = evaluation.get('max_score')
    if max_score not in EVALUATION_MAX_SCORES:
        problems.append({'field': 'max_score', 'value': max_score, 'reason': 'not_a_policy_denominator'})
    # An agency evaluation is a different model, not a discounted one. Rendering it
    # against 100 would be a different and false claim.
    if lead_type == 'agency' and max_score != 75:
        problems.append({'field': 'max_score', 'value': max_score,
                         'reason': 'agency_evaluation_must_be_out_of_75'})
    if lead_type == 'direct' and max_score != 100:
        problems.append({'field': 'max_score', 'value': max_score,
                         'reason': 'direct_evaluation_must_be_out_of_100'})

    total = evaluation.get('total_score')
    if isinstance(total, bool) or not isinstance(total, int):
        problems.append({'field': 'total_score', 'value': total, 'reason': 'not_a_whole_number'})
        total = None
    elif max_score in EVALUATION_MAX_SCORES and not 0 <= total <= max_score:
        problems.append({'field': 'total_score', 'value': total, 'reason': 'outside_allowed_range'})

    if not isinstance(evaluation.get('eligible'), bool):
        problems.append({'field': 'eligible', 'value': evaluation.get('eligible'),
                         'reason': 'not_a_boolean'})

    maxima = _policy_component_maxima()
    model_maxima = maxima.get('agency' if lead_type == 'agency' else 'direct') or {}
    components = evaluation.get('components')
    if not isinstance(components, dict) or not components:
        problems.append({'field': 'components', 'value': type(components).__name__,
                         'reason': 'required_object'})
        components = {}
    summed = 0
    for name in sorted(components):
        block = components[name]
        field = f'components.{name}'
        if not isinstance(block, dict):
            problems.append({'field': field, 'value': type(block).__name__, 'reason': 'not_an_object'})
            continue
        if model_maxima and name not in model_maxima:
            problems.append({'field': field, 'value': name, 'reason': 'not_a_policy_component'})
            continue
        score = block.get('score')
        declared_max = block.get('max_score')
        if isinstance(score, bool) or not isinstance(score, int):
            problems.append({'field': f'{field}.score', 'value': score, 'reason': 'not_a_whole_number'})
            continue
        if isinstance(declared_max, bool) or not isinstance(declared_max, int):
            problems.append({'field': f'{field}.max_score', 'value': declared_max,
                             'reason': 'not_a_whole_number'})
            continue
        if model_maxima and declared_max != model_maxima.get(name):
            problems.append({'field': f'{field}.max_score', 'value': declared_max,
                             'reason': 'disagrees_with_policy',
                             'policy_max_score': model_maxima.get(name)})
            continue
        if not 0 <= score <= declared_max:
            problems.append({'field': f'{field}.score', 'value': score,
                             'reason': 'outside_allowed_range', 'max_score': declared_max})
            continue
        evidence = str(block.get('evidence') or '').strip()
        if len(evidence) < EVALUATION_MIN_EVIDENCE_CHARS:
            problems.append({'field': f'{field}.evidence', 'value': block.get('evidence'),
                             'reason': 'evidence_required'})
            continue
        if len(evidence) > EVALUATION_MAX_EVIDENCE_CHARS:
            problems.append({'field': f'{field}.evidence', 'reason': 'evidence_too_long',
                             'value': len(evidence)})
            continue
        uncertainty = str(block.get('uncertainty') or '').strip().lower()
        if uncertainty not in EVALUATION_UNCERTAINTY:
            problems.append({'field': f'{field}.uncertainty', 'value': block.get('uncertainty'),
                             'reason': 'unknown_token'})
            continue
        if grounded:
            # The ceiling is the most this component could have scored on the
            # evidence it had. Its VALUE is checked against policy by the grounding
            # pass; here it only has to be present and internally coherent, so a
            # score above it cannot be stored even where the policy is unreadable.
            ceiling = block.get('ceiling')
            if isinstance(ceiling, bool) or not isinstance(ceiling, int):
                problems.append({'field': f'{field}.ceiling', 'value': ceiling,
                                 'reason': 'uncertainty_ceiling_missing'})
                continue
            if not 0 <= ceiling <= declared_max:
                problems.append({'field': f'{field}.ceiling', 'value': ceiling,
                                 'reason': 'outside_allowed_range', 'max_score': declared_max})
                continue
            if score > ceiling:
                problems.append({'field': f'{field}.score', 'value': score,
                                 'reason': 'above_the_uncertainty_ceiling', 'ceiling': ceiling})
                continue
        summed += score
    if model_maxima and components:
        missing = sorted(set(model_maxima) - set(components))
        if missing:
            problems.append({'field': 'components', 'value': missing,
                             'reason': 'required_component_missing'})
    # An agency evaluation EXCLUDES sponsorship rather than scoring it zero.
    if lead_type == 'agency' and 'sponsorship' in components:
        problems.append({'field': 'components.sponsorship', 'value': 'present',
                         'reason': 'agency_evaluation_excludes_sponsorship'})
    if total is not None and not problems and summed != total:
        problems.append({'field': 'total_score', 'value': total, 'reason': 'components_do_not_sum',
                         'sum_of_components': summed})

    band = evaluation.get('score_band')
    known_bands = maxima.get('bands') or ()
    if lead_type == 'agency':
        # The bands are defined for the 100-point model; borrowing one would imply a
        # comparability that does not exist.
        if band is not None:
            problems.append({'field': 'score_band', 'value': band,
                             'reason': 'agency_evaluation_has_no_direct_band'})
    elif known_bands and band not in known_bands:
        problems.append({'field': 'score_band', 'value': band, 'reason': 'unknown_token'})

    blockers = evaluation.get('hard_blockers', [])
    if blockers in (None, ''):
        blockers = []
    if not isinstance(blockers, list):
        problems.append({'field': 'hard_blockers', 'value': type(blockers).__name__,
                         'reason': 'not_a_list'})
        blockers = []
    for index, row in enumerate(blockers):
        if not isinstance(row, dict) or not str(row.get('id') or '').strip():
            problems.append({'field': f'hard_blockers[{index}]', 'value': row,
                             'reason': 'blocker_needs_an_id'})
    # Eligibility is DERIVED from the blockers, so the two can never disagree.
    if isinstance(evaluation.get('eligible'), bool):
        if bool(blockers) == evaluation['eligible']:
            problems.append({'field': 'eligible', 'value': evaluation['eligible'],
                             'reason': 'contradicts_hard_blockers',
                             'hard_blockers': [r.get('id') for r in blockers if isinstance(r, dict)]})

    verification = evaluation.get('verification_needed', [])
    if verification in (None, ''):
        verification = []
    if not isinstance(verification, list):
        problems.append({'field': 'verification_needed', 'value': type(verification).__name__,
                         'reason': 'not_a_list'})
    else:
        for index, row in enumerate(verification):
            if not isinstance(row, dict) or not str(row.get('reason') or '').strip():
                problems.append({'field': f'verification_needed[{index}]', 'value': row,
                                 'reason': 'verification_needs_a_reason'})

    computed_by = str(evaluation.get('computed_by') or '').strip()
    if computed_by != 'tools/match_evaluation.py':
        problems.append({'field': 'computed_by', 'value': evaluation.get('computed_by'),
                         'reason': 'evaluation_must_come_from_the_deterministic_evaluator'})
    if grounded and ground:
        # Whether the numbers are SUPPORTED, which is a different question from
        # whether they add up, and the one an arithmetic check cannot reach.
        problems.extend(_grounding_problems(evaluation))
    return problems


def _normalise_blocker_evidence(raw):
    """Keep a blocker's structured evidence, dropping anything not in the schema.

    A bare string is preserved as an excerpt so a legacy object stays readable
    when one is being inspected; only the write path decides whether that is
    enough, and it is not.
    """
    if not isinstance(raw, dict):
        return {'excerpt': str(raw or '').strip()}
    out = {}
    for field in ('excerpt', 'source_url', 'source_type', 'stated_by', 'matched_value'):
        value = str(raw.get(field) or '').strip()
        if value:
            out[field] = value.lower() if field in ('source_type', 'stated_by') else value
    return out


def normalise_evaluation(evaluation, rank_run_id=''):
    """Reduce a validated evaluator result to the fields state stores.

    Only known fields survive, so an evaluator payload can never smuggle an
    arbitrary key into discovery state.
    """
    if not isinstance(evaluation, dict):
        return None
    lead_type = str(evaluation.get('lead_type') or '').strip().lower()
    components = {}
    if lead_type != 'verification':
        for name, block in (evaluation.get('components') or {}).items():
            if not isinstance(block, dict):
                continue
            components[name] = {
                'score': block.get('score'),
                'max_score': block.get('max_score'),
                # The evidence ceiling travels with the score, because a total
                # sitting under its own ceiling and a low ceiling are different
                # findings and only one of them is about the vacancy.
                'ceiling': block.get('ceiling'),
                'evidence': str(block.get('evidence') or '').strip(),
                'uncertainty': str(block.get('uncertainty') or '').strip().lower(),
            }
    stored = {
        'schema_version': EVALUATION_SCHEMA_VERSION,
        'lead_type': lead_type,
        'total_score': evaluation.get('total_score'),
        'max_score': evaluation.get('max_score'),
        'score_display': str(evaluation.get('score_display') or '').strip(),
        'score_band': evaluation.get('score_band'),
        'band_display': str(evaluation.get('band_display') or '').strip(),
        'eligible': evaluation.get('eligible'),
        'provisional': bool(evaluation.get('provisional')),
        'components': components,
        'uncertainty_summary': evaluation.get('uncertainty_summary') or {},
        # WHICH calibration and policy calculated this, and whether it was checked
        # against the stored vacancy. Both have to survive the write or a stored
        # evaluation can never be recognised as stale, and `doctor` would report a
        # clean bill of health for a ranking nobody could reproduce today.
        'evaluation_fingerprints': evaluation.get('evaluation_fingerprints') or {},
        'canonical_grounding': bool(evaluation.get('canonical_grounding')),
        'canonical_key': str(evaluation.get('canonical_key') or ''),
        # The vacancy facts the CALCULATION consumed, so an anchor or a blocker
        # precondition can be re-checked from the stored object alone. Deliberately
        # only what was used: the record's own `facts` block stays the place a
        # vacancy's facts live, and this never becomes a second copy of it.
        'facts_used': evaluation.get('facts_used') or {},
        'hard_blockers': [
            {'id': str(r.get('id') or '').strip().lower(),
             'evidence': _normalise_blocker_evidence(r.get('evidence')),
             'precondition': str(r.get('precondition') or '').strip(),
             'verified_against': r.get('verified_against')
             if isinstance(r.get('verified_against'), dict) else {}}
            for r in (evaluation.get('hard_blockers') or []) if isinstance(r, dict)
        ],
        'verification_needed': [
            {'reason': str(r.get('reason') or '').strip().lower(),
             'detail': str(r.get('detail') or '').strip()}
            for r in (evaluation.get('verification_needed') or []) if isinstance(r, dict)
        ],
        'computed_by': str(evaluation.get('computed_by') or '').strip(),
        'evaluated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
    }
    if rank_run_id:
        stored['rank_run_id'] = rank_run_id
    return stored


def blocker_ids(item):
    """The machine-readable hard-blocker ids on a record, without reading prose."""
    evaluation = (item or {}).get('evaluation')
    if not isinstance(evaluation, dict):
        return []
    return [str(r.get('id') or '').strip().lower()
            for r in (evaluation.get('hard_blockers') or []) if isinstance(r, dict)]


def facts_problems(facts):
    """Controlled-vocabulary and type problems inside a structured facts object.

    An absent facts object is not a problem. Unknown must be allowed to stay
    unknown, so a null or omitted fact is always acceptable; only a stated value
    outside its vocabulary or of the wrong type is rejected.
    """
    if facts is None:
        return []
    if not isinstance(facts, dict):
        return [{'field': 'facts', 'value': type(facts).__name__, 'reason': 'not_an_object'}]
    problems = []
    for field in sorted(set(facts) - set(FACT_FIELDS)):
        problems.append({'field': field, 'reason': 'not_a_fact_field'})
    for field, allowed in (('employment_type', EMPLOYMENT_TYPES), ('work_pattern', WORK_PATTERNS)):
        value = facts.get(field)
        if value in (None, ''):
            continue
        if str(value).strip().lower() not in allowed:
            problems.append({'field': field, 'value': value, 'reason': 'unknown_token'})
    for field in FACT_NUMBER_FIELDS:
        value = facts.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append({'field': field, 'value': value, 'reason': 'not_a_number'})
    skills = facts.get('skills')
    if skills is not None and not isinstance(skills, list):
        problems.append({'field': 'skills', 'value': type(skills).__name__, 'reason': 'not_a_list'})
    currency = facts.get('salary_currency')
    if currency and not re.fullmatch(r'[A-Z]{3}', str(currency).strip()):
        problems.append({'field': 'salary_currency', 'value': currency,
                         'reason': 'not_an_iso_4217_code'})
    # A controlled country code, not a place name. It is the only thing that can
    # establish that a vacancy sits outside the accepted market, so it has to be a
    # value with exactly one reading rather than prose somebody has to interpret.
    country = facts.get('country')
    if country and not re.fullmatch(r'[A-Z]{2}', str(country).strip().upper()):
        problems.append({'field': 'country', 'value': country,
                         'reason': 'not_an_iso_3166_alpha_2_code'})
    return problems


def normalise_facts(facts):
    """Drop empty values so an unknown fact is never stored as a fabricated one."""
    if not isinstance(facts, dict):
        return {}
    out = {}
    for field in FACT_FIELDS:
        value = facts.get(field)
        if value in (None, '', []):
            continue
        if field in ('employment_type', 'work_pattern'):
            value = str(value).strip().lower()
        elif field in ('salary_currency', 'country'):
            value = str(value).strip().upper()
        elif field == 'skills':
            value = [str(s).strip() for s in value if str(s).strip()]
            if not value:
                continue
        out[field] = value
    return out


def facts_provenance_problems(provenance, facts=None):
    """Structural problems inside a field-level fact-provenance mapping.

    An absent provenance object is not a problem: provenance is additive and old
    records legitimately have none.
    """
    if provenance is None:
        return []
    if not isinstance(provenance, dict):
        return [{'field': 'facts_provenance', 'value': type(provenance).__name__,
                 'reason': 'not_an_object'}]
    problems = []
    for field in sorted(provenance):
        entry = provenance[field]
        if field not in FACT_FIELDS:
            problems.append({'field': field, 'reason': 'not_a_fact_field'})
            continue
        if facts is not None and field not in (facts or {}):
            problems.append({'field': field, 'reason': 'provenance_without_fact'})
        if not isinstance(entry, dict):
            problems.append({'field': field, 'value': type(entry).__name__,
                             'reason': 'not_an_object'})
            continue
        for extra in sorted(set(entry) - set(PROVENANCE_FIELDS)):
            problems.append({'field': f'{field}.{extra}', 'reason': 'not_a_provenance_field'})
        token = str(entry.get('source_type') or '').strip().lower()
        if token and token not in SOURCE_TYPES:
            problems.append({'field': f'{field}.source_type', 'value': entry.get('source_type'),
                             'reason': 'unknown_token'})
    return problems


def provenance_stamp(source_type='', url='', host='', observed_at=''):
    """One deterministic provenance entry for whatever facts this write supplied.

    `observed_at` prefers the verification/extraction timestamp the caller
    actually supplied and falls back to the state write time. Empty values are
    dropped rather than stored as blanks.
    """
    entry = {
        'source_type': (source_type or '').strip().lower(),
        'source_url': (url or '').strip(),
        'source_host': canon_host(host or source_host(url)),
        'observed_at': (observed_at or '').strip()
        or datetime.now().astimezone().isoformat(timespec='seconds'),
    }
    return {field: entry[field] for field in PROVENANCE_FIELDS if entry.get(field)}


def facts_source_override(source_type, source_url):
    """Validate a caller-supplied fact-provenance source, all or nothing.

    A provenance tuple describes ONE source context. Accepting half an override
    silently completes it from the record's preferred source, which produces a
    tuple nobody observed: an employer-ATS source_type carrying an aggregator URL
    and aggregator host, or an aggregator source_type stamped on the employer URL.
    Both misreport who actually stated the fact, so a half override is refused.

    Returns None when neither was supplied, meaning "use the record's own preferred
    source". Otherwise returns the validated source, whose host always follows the
    supplied URL rather than the record's.
    """
    source_type = (source_type or '').strip()
    source_url = (source_url or '').strip()
    if not source_type and not source_url:
        return None
    if bool(source_type) != bool(source_url):
        supplied, missing = (('--facts-source-url', '--facts-source-type') if source_url
                             else ('--facts-source-type', '--facts-source-url'))
        raise state_error(
            f'{supplied} was supplied without {missing}.',
            'A fact-provenance override describes one source context, so the source '
            'type and the source URL must be given together.',
            'Completing the missing half from the record would record a source type '
            'and a source URL that never came from the same place.',
            "Either pass both, or omit both to use the record's preferred source.",
        )
    return {'source_type': validate_choice('source_type', source_type, SOURCE_TYPES),
            'source_url': source_url}


def merge_facts_provenance(existing_facts, existing_provenance, incoming_facts,
                           stamp, incoming_is_weaker):
    """Field-level facts merge that keeps each field's authority truthful.

    A stronger or equal source may correct a fact and then owns that fact's
    provenance. A weaker rediscovery may only fill a fact that is currently
    absent, exactly as it may only fill an empty source-owned field, and a weaker
    source forbidden from overwriting a value is equally forbidden from
    overwriting that value's provenance. A fact whose value did not change keeps
    the provenance it already had.
    """
    facts = dict(existing_facts or {})
    provenance = {field: dict(entry)
                  for field, entry in (existing_provenance or {}).items()
                  if isinstance(entry, dict)}
    for field, value in (incoming_facts or {}).items():
        if value in (None, '', []):
            continue
        known = facts.get(field) not in (None, '', [])
        if incoming_is_weaker and known:
            continue
        if known and facts.get(field) == value:
            continue
        facts[field] = value
        if stamp:
            provenance[field] = dict(stamp)
    provenance = {field: entry for field, entry in provenance.items() if field in facts}
    return facts, provenance


def merge_facts(existing, incoming, incoming_is_weaker):
    """Field-level facts merge, values only.

    A stronger or equal source may correct a fact. A weaker rediscovery may only
    fill a fact that is currently absent, exactly as it may only fill an empty
    source-owned field.
    """
    merged, _ = merge_facts_provenance(existing, None, incoming, None, incoming_is_weaker)
    return merged


def vocabulary_violations(seen, ground=False):
    """Every machine-controlled value in state that is missing or outside its vocabulary.

    Stored evaluations are read with legacy schemas accepted, because a ranking
    recorded before evidence grounding existed is history and stays valid. Their
    grounding is NOT re-derived while merely reading state, since that would make
    opening a discovery record depend on the matching policy as well; `doctor`
    turns it on deliberately.
    """
    checks = (
        ('sponsorship_label', SPONSORSHIP_LABELS),
        ('fit_band', FIT_BANDS),
        ('lead_type', LEAD_TYPES),
        ('status', STATUSES),
        ('source_type', SOURCE_TYPES),
        ('source_confidence', SOURCE_CONFIDENCES),
    )
    # Optional: absent means never assessed, which is legitimate for records
    # written before the run-window gate existed.
    optional_checks = (('run_window', RUN_WINDOWS),)
    problems = []
    for key, item in seen.items():
        for field, allowed in checks:
            raw = item.get(field)
            token = str(raw or '').strip().lower()
            if not token:
                if field in REQUIRED_MACHINE_FIELDS:
                    problems.append({'key': key, 'field': field, 'value': raw, 'reason': 'missing'})
                continue
            if token not in allowed:
                problems.append({'key': key, 'field': field, 'value': raw, 'reason': 'unknown_token'})
        for field, allowed in optional_checks:
            raw = item.get(field)
            token = str(raw or '').strip().lower()
            if token and token not in allowed:
                problems.append({'key': key, 'field': field, 'value': raw, 'reason': 'unknown_token'})
        # `facts` is optional. When present it must still be well formed.
        if 'facts' in item:
            for problem in facts_problems(item.get('facts')):
                problems.append({'key': key, 'field': f"facts.{problem.get('field')}",
                                 'value': problem.get('value'), 'reason': problem.get('reason')})
        # `facts_provenance` is optional too, and only meaningful field by field.
        if 'facts_provenance' in item:
            for problem in facts_provenance_problems(item.get('facts_provenance'), item.get('facts')):
                problems.append({'key': key, 'field': f"facts_provenance.{problem.get('field')}",
                                 'value': problem.get('value'), 'reason': problem.get('reason')})
        # `evaluation` is optional and additive. Records ranked before it existed stay
        # valid; a present one must be a complete, self-consistent machine record.
        if 'evaluation' in item:
            for problem in evaluation_problems(item.get('evaluation'),
                                               accept_legacy=True, ground=ground):
                problems.append({'key': key, 'field': f"evaluation.{problem.get('field')}",
                                 'value': problem.get('value'), 'reason': problem.get('reason')})
    return problems


def materially_improved(old, new):
    """Compare machine-readable fields only. Evidence prose is never ranked."""
    reasons = []
    if source_rank(new) > source_rank(old):
        reasons.append('source_type')
    if rank_value(CONF_RANK, new.get('source_confidence')) > rank_value(CONF_RANK, old.get('source_confidence')):
        reasons.append('source_confidence')
    if rank_value(FIT_RANK, new.get('fit_band')) > rank_value(FIT_RANK, old.get('fit_band')):
        reasons.append('fit_band')
    if rank_value(SPONSOR_RANK, new.get('sponsorship_label')) > rank_value(SPONSOR_RANK, old.get('sponsorship_label')):
        reasons.append('sponsorship_label')
    if rank_value(LEAD_RANK, new.get('lead_type')) > rank_value(LEAD_RANK, old.get('lead_type')):
        reasons.append('lead_type')

    old_date, new_date = iso_date(old.get('posted')), iso_date(new.get('posted'))
    if old_date and new_date and new_date > old_date:
        reasons.append('newer_posted_date')
    elif not old_date and new_date:
        reasons.append('verified_posted_date')

    if old.get('url') and new.get('url') and norm_url(old.get('url')) != norm_url(new.get('url')):
        if source_rank(new) > source_rank(old) or rank_value(CONF_RANK, new.get('source_confidence')) > rank_value(CONF_RANK, old.get('source_confidence')):
            reasons.append('better_source_url')

    if not old.get('requisition_id') and new.get('requisition_id'):
        reasons.append('requisition_id')

    return sorted(set(reasons))


def merge_item(item, incoming):
    before_conf = rank_value(CONF_RANK, item.get('source_confidence'))
    incoming_conf = rank_value(CONF_RANK, incoming.get('source_confidence'))
    before_source = source_rank(item)
    incoming_source = source_rank(incoming)
    old_host = canon_host(item.get('source_host') or source_host(item.get('url', '')))
    new_host = canon_host(incoming.get('source_host') or source_host(incoming.get('url', '')))
    incoming_is_weaker = (
        before_source > incoming_source
        or (before_source == incoming_source and before_conf > incoming_conf)
    )
    source_owner_changes = (
        not incoming_is_weaker
        and incoming_source > before_source
        and old_host and new_host and old_host != new_host
    )

    # job_id is source-local. When authority moves to a stronger host and that
    # source supplies no job_id, clear the weaker host's ID rather than keeping an
    # impossible (new host, old source-local ID) tuple. requisition_id is preserved
    # when absent because it can be an employer-wide identifier copied by boards.
    if source_owner_changes and not incoming.get('job_id'):
        item['job_id'] = ''

    stamp = incoming.get(FACTS_SOURCE_FIELD) or {}
    for field, value in incoming.items():
        if not value:
            continue
        if field == FACTS_SOURCE_FIELD:
            # Transient provenance carrier. It stamps the facts it accompanied and
            # is never persisted as a record field of its own.
            continue
        if field == 'facts':
            # Facts merge per field rather than wholesale, so a source that knew
            # the salary does not erase a source that knew the work pattern, and
            # each surviving field keeps the provenance of whoever supplied it.
            merged, provenance = merge_facts_provenance(
                item.get('facts'), item.get('facts_provenance'), value, stamp, incoming_is_weaker)
            if merged:
                item['facts'] = merged
            if provenance:
                item['facts_provenance'] = provenance
            continue
        if field in SOURCE_OWNED_FIELDS and incoming_is_weaker:
            # A weaker source may fill an empty source-owned field only when it
            # belongs to the same canonical source host. Never attach a board ID
            # or requisition to the preferred employer host.
            if item.get(field) or (old_host and new_host and old_host != new_host):
                continue
        if field in SOURCE_DERIVED_MATCH_FIELDS and item.get(field) and incoming_is_weaker:
            continue
        if field == 'posted':
            old_date, new_date = iso_date(item.get('posted')), iso_date(value)
            if old_date and new_date and new_date < old_date:
                continue
        item[field] = value


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_normalize_url(args):
    print(norm_url(args.url))


def check_one(seen, url='', company='', title='', location='',
              requisition_id='', job_id='', source_host_hint=''):
    """One duplicate decision.

    `check` and `check-batch` both route through this, so batching a candidate can
    never produce a different answer from checking it on its own.
    """
    key, reason = exact_duplicate(seen, url, company, requisition_id, job_id, source_host_hint)
    possibles = possible_duplicates(seen, company, title, location)
    if key:
        possibles = [p for p in possibles if p['key'] != key]
    return {
        'duplicate': bool(key),
        'key': key,
        'reason': reason,
        'item': seen.get(key) if key else None,
        'possible_duplicates': possibles,
    }


def cmd_check(args):
    data = load_state()
    print(json.dumps(check_one(
        data['seen'], args.url, args.company, args.title, args.location,
        args.requisition_id, args.job_id, args.source_host,
    ), ensure_ascii=False))


def read_json_input(path):
    """Read a JSON document from a file, or from stdin when no path is given."""
    if path:
        target = Path(path)
        if not target.exists():
            raise state_error(f'Input file not found: {target}')
        raw = target.read_text(encoding='utf-8')
    else:
        raw = sys.stdin.read()
    # Windows shells routinely prefix piped text with a byte-order mark.
    raw = raw.lstrip('﻿')
    if not raw.strip():
        raise state_error(
            'No JSON input received.',
            'Pass --file <path> or pipe a JSON array on stdin.')
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise state_error(
            'Malformed JSON input.',
            f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None


def cmd_check_batch(args):
    """Check a whole worker batch in one process instead of one process per row.

    The default row carries only what discovery gating actually needs. Returning
    the whole historical record for every duplicate would defeat the point of
    batching, so the full record is opt-in through --include-item.
    """
    payload = read_json_input(args.file)
    if not isinstance(payload, list):
        raise state_error(
            'check-batch expects a JSON array of candidate objects.',
            f'Received: {type(payload).__name__}')
    seen = load_state()['seen']
    include_item = bool(getattr(args, 'include_item', False))
    results = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            row = {'index': index, 'error': 'not_an_object',
                   'duplicate': False, 'key': None, 'reason': None,
                   'possible_duplicates': []}
            if include_item:
                row['item'] = None
            results.append(row)
            continue
        url = entry.get('url') or entry.get('source_url') or ''
        result = check_one(
            seen, url, entry.get('company', ''), entry.get('title', ''),
            entry.get('location', ''), entry.get('requisition_id', ''),
            entry.get('job_id', '') or entry.get('source_job_id', ''),
            entry.get('source_host', ''),
        )
        row = {
            'index': index,
            'url': url,
            'canonical_url': norm_url(url),
            'duplicate': result['duplicate'],
            'key': result['key'],
            'reason': result['reason'],
        }
        if include_item:
            row['item'] = result['item']
            row['possible_duplicates'] = result['possible_duplicates']
        else:
            # Enough to identify a possible duplicate, not enough to re-narrate it.
            row['possible_duplicates'] = [
                {'key': p['key'], 'reason': p['reason']}
                for p in result['possible_duplicates']
            ]
        results.append(row)
    duplicates = [r for r in results if r.get('duplicate')]
    print(json.dumps({
        'count': len(results),
        'duplicate_count': len(duplicates),
        'new_count': len(results) - len(duplicates),
        'duplicate_keys': [r['key'] for r in duplicates],
        'include_item': include_item,
        'results': results,
    }, indent=2, ensure_ascii=False))


def merge_existing(data, key, incoming, args, duplicate_reason):
    item = data['seen'][key]
    before = deepcopy(item)
    item['last_seen'] = date.today().isoformat()
    merge_item(item, incoming)
    upgrade_reasons = materially_improved(before, item)

    if args.reopen_on_upgrade and upgrade_reasons:
        item['status'] = 'updated'
    # --status is an initial-record field for `add`. Existing records keep their
    # lifecycle state unless a material upgrade reopens them. Intentional status
    # changes use the explicit `mark` command.

    save_state(data)
    print(json.dumps({
        'added': False,
        'updated': True,
        'key': key,
        'duplicate_reason': duplicate_reason,
        'material_upgrade': bool(upgrade_reasons),
        'upgrade_reasons': upgrade_reasons,
        'status': item.get('status', ''),
    }, ensure_ascii=False))


def evaluation_argument(raw_json, file_path):
    """Parse an optional structured ranking evaluation.

    Returns None when none was supplied, so an existing record is never given an
    empty evaluation merely because the flag was absent. Accepts the evaluator's
    full output (`{"valid": true, "evaluation": {...}}`) or the inner object, so a
    caller can pipe `match_evaluation.py evaluate` through unchanged.
    """
    if file_path:
        payload = read_json_input(file_path)
    elif raw_json:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise state_error(
                'Malformed --evaluation JSON.',
                f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None
    else:
        return None
    if not isinstance(payload, dict):
        raise state_error('An evaluation must be a JSON object.',
                          f'Got {type(payload).__name__}.')
    if isinstance(payload.get('evaluation'), dict):
        # The evaluator's own envelope. A rejected proposal must never be stored.
        if payload.get('valid') is False:
            raise state_error(
                'Refusing to store an evaluation the evaluator itself rejected.',
                f"Errors: {json.dumps(payload.get('errors', []), ensure_ascii=False)}")
        payload = payload['evaluation']
    return payload


def facts_argument(raw_json, file_path):
    """Parse and validate an optional structured-facts object.

    Returns {} when no facts were supplied, so an existing record is never given an
    empty facts object merely because the flag was absent.
    """
    if file_path:
        payload = read_json_input(file_path)
    elif raw_json:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise state_error(
                'Malformed --facts JSON.',
                f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None
    else:
        return {}
    problems = facts_problems(payload)
    if problems:
        raise state_error(
            'Invalid facts object.',
            f'Problems: {json.dumps(problems, ensure_ascii=False)}',
            f'Allowed fields: {", ".join(FACT_FIELDS)}',
            'Facts hold what the vacancy actually stated. Leave a fact out rather '
            'than filling it with a guess.')
    return normalise_facts(payload)


def cmd_add(args):
    data = load_state()
    seen = data['seen']
    today = date.today().isoformat()

    # A vacancy needs an identity before anything else is worth checking. Without a
    # URL, or at minimum a company and a title, there is nothing to deduplicate
    # against, nothing to re-verify, and nothing a human could ever open: the record
    # would key as `::::` and every later identity-less write would collide with it.
    # Refusing here is the fail-closed boundary. Writing a placeholder would be the
    # exact "guess the missing fields and save anyway" behaviour the pipeline
    # forbids everywhere else.
    if not norm_url(args.url) and not (norm_text(args.company) and norm_text(args.title)):
        raise state_error(
            'Refusing to save a vacancy with no usable identity.',
            'A record needs a URL, or at minimum both a company and a title.',
            f'Received: url={args.url!r} company={args.company!r} title={args.title!r}',
            'A candidate that failed validation must be rejected and reported, never '
            'saved with the missing fields guessed or left blank.',
        )

    lead_type = validate_choice('lead_type', args.lead_type, LEAD_TYPES)
    status = validate_choice('status', args.status, STATUSES)
    source_type = validate_choice('source_type', args.source_type, SOURCE_TYPES)
    confidence = validate_choice('source_confidence', args.source_confidence, SOURCE_CONFIDENCES, normalise=False)
    sponsorship_label = validate_choice(
        'sponsorship_label', args.sponsorship_label or 'unknown', SPONSORSHIP_LABELS)
    fit_band = validate_choice('fit_band', args.fit_band or 'unknown', FIT_BANDS)
    for field, value in (
        ('lead_type', lead_type),
        ('source_type', source_type),
        ('source_confidence', confidence),
    ):
        if not value:
            raise state_error(
                f'Missing required --{field.replace("_", "-")}.',
                'Every new discovery record must carry complete machine-readable identity/classification fields.',
            )
    args.status = status

    incoming_host = resolve_source_host(args.url, args.source_host)
    exact_key, exact_reason = exact_duplicate(
        seen, args.url, args.company, args.requisition_id, args.job_id, incoming_host
    )

    if args.merge_key and args.merge_key not in seen:
        raise state_error(f'Unknown --merge-key: {args.merge_key}')
    merge_key = args.merge_key or exact_key
    duplicate_reason = 'verified_merge_key' if args.merge_key else exact_reason

    incoming = {
        'title': args.title,
        'company': args.company,
        'url': args.url,
        'location': args.location or '',
        'posted': args.posted or '',
        'last_verified': args.last_verified or today,
        'quick_fit': args.quick_fit or '',
        'fit_band': fit_band,
        'lead_type': lead_type,
        'sponsorship': args.sponsorship or '',
        'sponsorship_label': sponsorship_label,
        'source': args.source or '',
        'source_type': source_type,
        'source_confidence': confidence,
        'source_host': incoming_host,
        'job_id': args.job_id or '',
        'requisition_id': args.requisition_id or '',
        'filter_reason': args.filter_reason or '',
    }
    facts = facts_argument(args.facts, args.facts_file)
    if facts:
        incoming['facts'] = facts
        # Provenance is derived from this record's own source metadata, so the
        # authority behind each fact is recorded by state code rather than prose.
        incoming[FACTS_SOURCE_FIELD] = provenance_stamp(
            source_type, args.url, incoming_host,
            str(facts.get('extracted_at') or args.last_verified or ''))

    if merge_key:
        merge_existing(data, merge_key, incoming, args, duplicate_reason)
        return

    possibles = possible_duplicates(seen, args.company, args.title, args.location)
    base = norm_url(args.url)
    if base:
        # exact_duplicate already resolves a colliding URL identity to its origin key,
        # so a URL identity can never mint a synthetic ::N record.
        if base in seen:
            merge_existing(data, base, incoming, args, 'origin_url')
            return
        key = base
    else:
        base = f'{norm_text(args.company)}::{norm_text(args.title)}::{norm_location(args.location)}'
        key = base
        suffix = 2
        while key in seen:
            key = f'{base}::{suffix}'
            suffix += 1

    stamp = incoming.pop(FACTS_SOURCE_FIELD, {})
    record = {
        **incoming,
        'first_seen': today,
        'last_seen': today,
        'status': status or 'new',
        'possible_duplicate_keys': [p['key'] for p in possibles],
    }
    if facts and stamp:
        record['facts_provenance'] = {field: dict(stamp) for field in facts}
    seen[key] = record
    save_state(data)
    print(json.dumps({
        'added': True,
        'key': key,
        'material_upgrade': False,
        'upgrade_reasons': [],
        'possible_duplicates': possibles,
    }, ensure_ascii=False))


def cmd_list(args):
    data = load_state()
    rows = []
    statuses = {s.strip() for s in args.status.split(',') if s.strip()} if args.status else set()
    excluded_out_of_window = 0
    for key, item in data['seen'].items():
        if statuses and item.get('status') not in statuses:
            continue
        if args.lead_type and item.get('lead_type') != args.lead_type:
            continue
        # A vacancy the producing run never asked for must not reach /rank just
        # because its generic status is still `new`. Absent means never assessed,
        # which stays eligible, so historical records are unaffected.
        if item.get('run_window') == 'out_of_window' and not args.include_out_of_window:
            excluded_out_of_window += 1
            continue
        rows.append({'key': key, **item})
    rows.sort(
        key=lambda x: (x.get('first_seen', ''), x.get('posted', ''), x.get('company', ''), x.get('title', '')),
        reverse=True,
    )
    # Count the full matching set before slicing so a partial run is never silent.
    total_matching = len(rows)
    if args.limit:
        rows = rows[:args.limit]
    print(json.dumps({
        'total_matching': total_matching,
        'returned': len(rows),
        'truncated': len(rows) < total_matching,
        'deferred': total_matching - len(rows),
        'limit': args.limit or 0,
        'excluded_out_of_window': excluded_out_of_window,
        'out_of_window_note': (
            'Records proved older than the widest window their discovery run '
            'activated are withheld. They are preserved in state, not suppressed; '
            'pass --include-out-of-window to see them.'),
        'count': len(rows),
        'results': rows,
    }, indent=2, ensure_ascii=False))


def cmd_mark(args):
    data = load_state()
    seen = data['seen']
    if args.key not in seen:
        raise state_error(f'Unknown key: {args.key}')
    item = seen[args.key]

    status = validate_choice('status', args.status, STATUSES)
    lead_type = validate_choice('lead_type', args.lead_type, LEAD_TYPES)
    sponsorship_label = validate_choice('sponsorship_label', args.sponsorship_label, SPONSORSHIP_LABELS)
    fit_band = validate_choice('fit_band', args.fit_band, FIT_BANDS)
    run_window = validate_choice('run_window', getattr(args, 'run_window', ''), RUN_WINDOWS)
    facts_source = facts_source_override(args.facts_source_type, args.facts_source_url)

    if status:
        item['status'] = status
    if lead_type:
        item['lead_type'] = lead_type
    if args.quick_fit:
        item['quick_fit'] = args.quick_fit
    if fit_band:
        item['fit_band'] = fit_band
    if args.sponsorship:
        item['sponsorship'] = args.sponsorship
    if sponsorship_label:
        item['sponsorship_label'] = sponsorship_label
    if run_window:
        item['run_window'] = run_window
    if getattr(args, 'run_window_reason', ''):
        item['run_window_reason'] = args.run_window_reason
    facts = facts_argument(args.facts, args.facts_file)
    if facts:
        # A deliberate mark is authoritative, so refreshed facts may correct
        # existing ones rather than only filling the gaps. Provenance defaults to
        # the record's own preferred source; a COMPLETE --facts-source-type /
        # --facts-source-url pair says so explicitly when the refreshed fact
        # actually came from somewhere else, and a weaker named source is held to
        # the ordinary fill-only rule. The host always follows the URL, so a
        # provenance tuple is never assembled from two different source contexts.
        if facts_source:
            override_type = facts_source['source_type']
            stamp = provenance_stamp(
                override_type, facts_source['source_url'], '',
                str(args.facts_observed_at or facts.get('extracted_at') or ''))
        else:
            override_type = ''
            stamp = provenance_stamp(
                item.get('source_type', ''), item.get('url', ''), item.get('source_host', ''),
                str(args.facts_observed_at or facts.get('extracted_at') or ''))
        weaker = bool(override_type) and (
            rank_value(SOURCE_RANK, override_type) < rank_value(SOURCE_RANK, item.get('source_type'))
        )
        merged, provenance = merge_facts_provenance(
            item.get('facts'), item.get('facts_provenance'), facts, stamp, weaker)
        if merged:
            item['facts'] = merged
        if provenance:
            item['facts_provenance'] = provenance
    item['last_verified'] = date.today().isoformat()
    if args.rank_score is not None:
        item['rank_score'] = args.rank_score
        item['rank_date'] = date.today().isoformat()
    if args.rank_run_id:
        item['rank_run_id'] = args.rank_run_id
        item['ranked_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
        if not item.get('rank_date'):
            item['rank_date'] = date.today().isoformat()
    if args.rank_verdict:
        item['rank_verdict'] = args.rank_verdict
    evaluation = evaluation_argument(getattr(args, 'evaluation', ''),
                                     getattr(args, 'evaluation_file', ''))
    if evaluation is not None:
        problems = evaluation_problems(evaluation)
        if not problems:
            # The submitted object is well formed and internally grounded. Whether
            # it is REPRODUCIBLE is the separate question, and the only one that
            # cannot be answered by reading the object itself.
            evaluation.setdefault('canonical_key', args.key)
            recomputed, problems = _recompute_problems(evaluation, seen)
            if not problems:
                # Store the evaluator's own fresh output. It is identical to the
                # submitted object by construction, and storing the calculated one
                # means what lands in state is never the caller's assertion.
                evaluation = recomputed
        if problems:
            raise state_error(
                'Refusing to store an invalid ranking evaluation.',
                f'Problems: {json.dumps(problems, ensure_ascii=False)}',
                'The evaluation is the machine record of a deterministic calculation. '
                'It is reported rather than repaired, because silently correcting a '
                'component would hide a caller that has misread the scoring model.',
                'A score must also be SUPPORTED, not merely well formed: components stay '
                'inside their uncertainty ceilings, the exact maximum needs its documented '
                'anchor, and a hard blocker needs a quote, a source, a speaker and a '
                'satisfied factual precondition.',
                'Produce it with: python tools/match_evaluation.py evaluate --file <proposal>',
            )
        # The stored total is the evaluator's, never the caller's separate --rank-score.
        if args.rank_score is not None and args.rank_score != evaluation.get('total_score'):
            raise state_error(
                f"--rank-score {args.rank_score} contradicts the evaluation total "
                f"{evaluation.get('total_score')}.",
                'One ranking cannot hold two different scores. Pass the evaluation and '
                'let its own total stand, or omit --rank-score.',
            )
        item['evaluation'] = normalise_evaluation(evaluation, args.rank_run_id)
        if evaluation.get('total_score') is None:
            # An unscored verification lead. Any score left over from an earlier
            # classification would now be a claim the evidence no longer supports.
            item.pop('rank_score', None)
        else:
            item['rank_score'] = evaluation.get('total_score')
        item['rank_date'] = item.get('rank_date') or date.today().isoformat()
    save_state(data)
    stored_eval = item.get('evaluation') or {}
    print(json.dumps({
        'updated': True,
        'key': args.key,
        'status': item.get('status', ''),
        'lead_type': item.get('lead_type', ''),
        'quick_fit': item.get('quick_fit', ''),
        'fit_band': item.get('fit_band', ''),
        'sponsorship_label': item.get('sponsorship_label', ''),
        'evaluation_stored': bool(stored_eval),
        'eligible': stored_eval.get('eligible'),
        'hard_blockers': [r.get('id') for r in stored_eval.get('hard_blockers', [])],
    }, ensure_ascii=False))


def cmd_reset(args):
    data = load_state()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = BACKUP_DIR / f'seen_jobs-pre-reset-{stamp}.json'
    atomic_write_text(backup_path, json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    count = len(data.get('seen', {}))
    save_state({'seen': {}})
    print(json.dumps({
        'reset': True,
        'removed_seen_jobs': count,
        'backup': str(backup_path.relative_to(ROOT)).replace('\\', '/'),
        'shortlists_untouched': True,
        'candidate_profile_untouched': True,
        'master_cv_untouched': True,
    }, ensure_ascii=False))


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def identity_problems(seen):
    """Duplicate and origin-key issues that identity logic would trip over."""
    problems = []
    by_canonical_key = {}
    for key in seen:
        by_canonical_key.setdefault(norm_url(key), []).append(key)
    for canonical, keys in by_canonical_key.items():
        if len(keys) > 1:
            problems.append({'issue': 'keys_share_one_canonical_identity',
                             'canonical': canonical, 'keys': sorted(keys)})

    by_url = {}
    for key, item in seen.items():
        url = norm_url(item.get('url', ''))
        if url:
            by_url.setdefault(url, []).append(key)
    for url, keys in by_url.items():
        if len(keys) > 1:
            problems.append({'issue': 'records_share_one_preferred_url',
                             'url': url, 'keys': sorted(keys)})

    for key in seen:
        base = key.rsplit('::', 1)[0]
        if re.search(r'::\d+$', key) and base in seen:
            problems.append({'issue': 'synthetic_suffix_over_existing_key',
                             'key': key, 'base': base})
    return problems


def stale_evaluations(seen):
    """Stored evaluations calculated against a calibration no longer in force.

    Reported, never repaired. A historical ranking is a record of a decision made
    under the configuration of the day; silently recalculating it would rewrite
    history, and deleting it would lose the audit trail. Callers are told which
    records would need re-ranking to reflect the current calibration.
    """
    try:
        evaluator = _evaluator()
        live = evaluator.evaluation_fingerprints()
    except (Exception, SystemExit):
        # Staleness is a REPORT, not a gate. A workspace with no readable policy
        # cannot answer the question, and a diagnosis tool must not fail because a
        # secondary report could not be produced.
        return []
    stale = []
    for key, item in (seen or {}).items():
        evaluation = (item or {}).get('evaluation')
        if not isinstance(evaluation, dict):
            continue
        stored = evaluation.get('evaluation_fingerprints')
        if not isinstance(stored, dict) or not stored:
            # Ranked before evaluations recorded their calibration. Knowable
            # unknown, not staleness, and never backfilled.
            continue
        differing = sorted(k for k, v in live.items() if stored.get(k) != v)
        if differing:
            stale.append({'key': key, 'differing': differing,
                          'ranked_at': evaluation.get('evaluated_at', ''),
                          'note': 'Calculated under a different calibration or policy. '
                                  'Re-rank to refresh it; history is never rewritten in place.'})
    return stale


def state_report():
    report = {
        'state_path': STATE.relative_to(ROOT).as_posix(),
        'exists': STATE.exists(),
        'parse_ok': False,
        'schema_ok': False,
        'schema_version': None,
        'record_count': 0,
        'errors': [],
        'schema_violations': [],
        'vocabulary_violations': [],
        'identity_problems': [],
        'stale_evaluations': [],
        'backups': [],
    }
    for path in recovery_backups():
        stat = path.stat()
        report['backups'].append({
            'path': path.relative_to(ROOT).as_posix(),
            'bytes': stat.st_size,
            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
            'valid': backup_is_valid(path),
        })

    if not STATE.exists():
        if report['backups']:
            report['errors'].append(
                'State file is missing while recovery backups exist. This is a lost-state '
                'workspace, not a first run. Restore with: doctor --repair')
        else:
            report['errors'].append(
                'State file is missing and no recovery backup exists. This looks like a '
                'genuine first-run workspace; the next write will create it.')
        return report

    try:
        raw = STATE.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        report['errors'].append(f'Unreadable state file: {type(exc).__name__}: {exc}')
        return report

    if not raw.strip():
        report['errors'].append('State file is empty, which usually means a truncated write.')
        return report

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        report['errors'].append(
            f'Malformed JSON at line {exc.lineno} column {exc.colno}: {exc.msg}')
        return report

    report['parse_ok'] = True
    if not isinstance(data, dict):
        report['errors'].append(f'Expected a JSON object, found {type(data).__name__}.')
        return report
    if not isinstance(data.get('seen'), dict):
        report['errors'].append('Missing or invalid top-level "seen" object.')
        return report

    report['schema_version'] = data.get('schema_version')
    seen = data['seen']
    report['record_count'] = len(seen)

    non_dict = [k for k, v in seen.items() if not isinstance(v, dict)]
    if non_dict:
        report['errors'].append(f'Non-object records: {sorted(non_dict)[:5]}')
        return report

    schema_problems = state_schema_violations(data)
    report['schema_violations'] = schema_problems
    report['schema_ok'] = not schema_problems
    if schema_problems:
        report['errors'].append(
            f'State does not satisfy schema version {SCHEMA_VERSION}: {len(schema_problems)} problem(s).')
    # doctor is the read-only DIAGNOSIS, so it re-derives evidence grounding too.
    report['vocabulary_violations'] = vocabulary_violations(seen, ground=True)
    report['identity_problems'] = identity_problems(seen)
    # Stale calibration is a REPORT, not a violation: a historical ranking made
    # under an older calibration is valid history, and rewriting it would destroy
    # the audit trail the evaluation object exists to provide.
    report['stale_evaluations'] = stale_evaluations(seen)
    return report


def backup_is_valid(path):
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get('seen'), dict):
        return False
    if state_schema_violations(payload):
        return False
    if vocabulary_violations(payload['seen']):
        return False
    return True


def report_is_healthy(report):
    """Whether a state report describes a usable discovery state."""
    return bool(
        report['exists'] and report['schema_ok']
        and not report['errors']
        and not report['vocabulary_violations']
        and not report['identity_problems']
    )


def cmd_doctor(args):
    """Report discovery-state health, and optionally restore a validated backup.

    REPAIR REPORTS TWO SEPARATE FACTS, because one boolean cannot answer both and
    the first version made that confusing in the operationally worst way: after a
    SUCCESSFUL repair it printed `healthy: false` beside `repaired: true`, which
    describes the state before the restore while reading as though the workspace
    were still broken. `healthy_before` is the diagnosis that justified repairing,
    and `healthy_after` is the only field that describes the state you now have.
    `healthy` stays as an alias of the current state so existing callers keep
    working, and it is never claimed unless the restored file actually validates.
    """
    report = state_report()
    healthy = report_is_healthy(report)
    report['healthy'] = healthy
    report['repaired'] = False

    if not args.repair:
        report['mode'] = 'read-only'
        report['repair_attempted'] = False
        report['healthy_before'] = healthy
        report['healthy_after'] = healthy
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(0 if healthy else 1)

    report['mode'] = 'repair'
    report['repair_attempted'] = True
    report['healthy_before'] = healthy

    def refuse(reason):
        report['repair_result'] = reason
        # Nothing was written, so the state is exactly as diagnosed.
        report['healthy_after'] = healthy
        report['healthy'] = healthy
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(0 if healthy else 1)

    if report['exists'] and report['schema_ok']:
        refuse('Refused: discovery state parses and has a valid shape. Repair only restores '
               'a damaged or missing state file; it never rewrites healthy discovery data.')

    source = next((Path(ROOT / b['path']) for b in report['backups'] if b['valid']), None)
    if source is None:
        refuse('Refused: no validated good backup is available to restore from.')

    preserved = None
    if STATE.exists():
        DAMAGED_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        preserved = DAMAGED_DIR / f'seen_jobs-damaged-{stamp}.json'
        shutil.copy2(STATE, preserved)

    atomic_write_text(STATE, source.read_text(encoding='utf-8'))
    # Re-diagnose the file that is actually on disk now. A restore is not assumed
    # to have worked: health is measured, never inferred from having tried.
    restored = state_report()
    healthy_after = report_is_healthy(restored)
    report['repaired'] = True
    report['restored_from'] = source.relative_to(ROOT).as_posix()
    report['damaged_file_preserved'] = preserved.relative_to(ROOT).as_posix() if preserved else None
    report['record_count'] = restored['record_count']
    report['schema_version'] = restored['schema_version']
    report['errors'] = restored['errors']
    report['schema_violations'] = restored['schema_violations']
    report['vocabulary_violations'] = restored['vocabulary_violations']
    report['identity_problems'] = restored['identity_problems']
    report['healthy_after'] = healthy_after
    report['healthy'] = healthy_after
    report['repair_result'] = (
        f'Restored {restored["record_count"]} records from '
        f'{source.relative_to(ROOT).as_posix()}. Discovery state is now '
        f'{"healthy" if healthy_after else "STILL NOT HEALTHY"}.')
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(0 if healthy_after else 1)


def main():
    p = argparse.ArgumentParser(description='Deduplicated job discovery state helper')
    sub = p.add_subparsers(dest='cmd', required=True)

    n = sub.add_parser('normalize-url')
    n.add_argument('--url', required=True)
    n.set_defaults(func=cmd_normalize_url)

    c = sub.add_parser('check')
    c.add_argument('--url', default='')
    c.add_argument('--company', required=True)
    c.add_argument('--title', required=True)
    c.add_argument('--location', default='')
    c.add_argument('--job-id', default='')
    c.add_argument('--requisition-id', default='')
    c.add_argument('--source-host', default='')
    c.set_defaults(func=cmd_check)

    cb = sub.add_parser('check-batch', help='Check a JSON array of candidates in one process.')
    cb.add_argument('--file', default='', help='JSON array file. Reads stdin when omitted.')
    cb.add_argument('--include-item', dest='include_item', action='store_true',
                    help='Also return the full historical record for each duplicate. '
                         'Off by default: batching exists to keep the gate cheap.')
    cb.set_defaults(func=cmd_check_batch)

    a = sub.add_parser('add')
    for x in ('company', 'title', 'url'):
        a.add_argument(f'--{x}', required=True)
    for x in (
        'location', 'posted', 'last-verified', 'quick-fit', 'fit-band', 'lead-type',
        'sponsorship', 'sponsorship-label', 'source', 'source-type', 'source-confidence',
        'source-host', 'job-id', 'requisition-id', 'filter-reason', 'status',
    ):
        a.add_argument(f'--{x}', dest=x.replace('-', '_'), default='')
    a.add_argument('--merge-key', default='')
    a.add_argument('--reopen-on-upgrade', action='store_true')
    a.add_argument('--facts', default='', help='Structured facts as a JSON object.')
    a.add_argument('--facts-file', dest='facts_file', default='')
    a.set_defaults(func=cmd_add)

    l = sub.add_parser('list')
    l.add_argument('--status', default='')
    l.add_argument('--lead-type', default='')
    l.add_argument('--limit', type=int, default=0)
    l.add_argument('--include-out-of-window', dest='include_out_of_window',
                   action='store_true',
                   help='Also return records proved older than the widest window '
                        'their discovery run activated. Withheld by default so a '
                        'normal /rank cannot pick one up.')
    l.set_defaults(func=cmd_list)

    m = sub.add_parser('mark')
    m.add_argument('--key', required=True)
    m.add_argument('--status', default='')
    m.add_argument('--lead-type', default='')
    m.add_argument('--quick-fit', default='')
    m.add_argument('--fit-band', default='')
    m.add_argument('--run-window', dest='run_window', default='',
                   help='Run-window eligibility: ' + ', '.join(RUN_WINDOWS) + '.')
    m.add_argument('--run-window-reason', dest='run_window_reason', default='',
                   help='Why this run-window verdict was reached.')
    m.add_argument('--sponsorship', default='')
    m.add_argument('--sponsorship-label', default='')
    m.add_argument('--rank-score', type=int)
    m.add_argument('--rank-verdict', default='')
    m.add_argument('--rank-run-id', default='')
    m.add_argument('--facts', default='', help='Structured facts as a JSON object.')
    m.add_argument('--facts-file', dest='facts_file', default='')
    m.add_argument('--facts-source-type', dest='facts_source_type', default='',
                   help='Source type the refreshed facts actually came from. Must be '
                        'given together with --facts-source-url. Omit both to default '
                        'to the record\'s own preferred source.')
    m.add_argument('--facts-source-url', dest='facts_source_url', default='',
                   help='Source URL the refreshed facts actually came from. Must be '
                        'given together with --facts-source-type.')
    m.add_argument('--facts-observed-at', dest='facts_observed_at', default='',
                   help='When the refreshed facts were actually observed.')
    m.add_argument('--evaluation', default='',
                   help='The structured result of tools/match_evaluation.py evaluate, '
                        'as a JSON object. Stored beside rank_score so the ranking can '
                        'be re-audited without parsing the verdict prose.')
    m.add_argument('--evaluation-file', dest='evaluation_file', default='',
                   help='File holding that JSON object. Accepts the evaluator output '
                        'whole, or just its `evaluation` member.')
    m.set_defaults(func=cmd_mark)

    r = sub.add_parser('reset')
    r.set_defaults(func=cmd_reset)

    d = sub.add_parser('doctor', help='Report discovery-state health. Read-only unless --repair.')
    d.add_argument('--repair', action='store_true',
                   help='Restore a validated backup after preserving the damaged file.')
    d.set_defaults(func=cmd_doctor)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

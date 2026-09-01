#!/usr/bin/env python3
"""Compact deterministic rejection store.

This is NOT ranked/dismissed job state and NOT preference learning. It records only
cheap, deterministic rejections that do not deserve repeated model work, so a
rediscovered vacancy that was already ruled out on a hard, objective ground can
skip the expensive fetch-and-reason stage.

The controlled reason-code vocabulary is the enforcement mechanism. There is
deliberately no code for uncertain sponsorship, unstated salary, one missing skill,
or a subjective dislike, because none of those are deterministic rejections and all
of them can change with one more piece of evidence.

Suppression always expires, and how fast depends on how mutable the blocker is.
A role's seniority is a property of the role; a salary band and a no-sponsorship
line are single editable sentences in a mutable advert, so they expire far sooner.

Expiry is not the only way back. A stored rejection blocks deep work only while
the advert still looks like the advert that was rejected. A newer posted date, a
changed requisition or source job id, or a materially changed title marks a
repost or rewrite, and the vacancy is RECONSIDERED rather than skipped. The old
record is not deleted: if the role still fails deterministically, a fresh `add`
replaces it.

`job_scraper/suppression.json` is private and gitignored.
"""
import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import atomic_write_text, norm_location, norm_text, norm_url  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / 'job_scraper' / 'suppression.json'
SCHEMA_VERSION = 1

# Fallback only. Every reason code below has its own default, because a blanket
# expiry is wrong in both directions: a role's seniority does not change, while a
# stated salary or a sponsorship line is an editable sentence in a mutable advert.
DEFAULT_EXPIRY_DAYS = 30

# Deterministic, objective grounds only. Every code here can be decided from the
# advert text without judgement, and none of them turn on missing evidence.
REASON_CODES = (
    'seniority',
    'wrong_specialism',
    'contract',
    'temporary',
    'apprenticeship',
    'security_clearance',
    'salary_below_hard_floor',
    'explicit_no_sponsorship',
    'wrong_primary_language',
)

# Reason-specific defaults, ordered by how mutable the blocker actually is.
#
#   30 days  A property of the role itself. A Staff Engineer vacancy does not
#            quietly become a mid-level one.
#   14 days  A property of the engagement. Contract and temporary postings are
#            genuinely re-advertised as permanent often enough to re-check.
#    7 days  A single editable line in the advert. A salary band and a
#            no-sponsorship sentence are exactly the sort of text an employer
#            revises, so suppressing either for a month is too aggressive.
REASON_EXPIRY_DAYS = {
    'seniority': 30,
    'wrong_specialism': 30,
    'apprenticeship': 30,
    'security_clearance': 30,
    'wrong_primary_language': 30,
    'contract': 14,
    'temporary': 14,
    'salary_below_hard_floor': 7,
    'explicit_no_sponsorship': 7,
}


def expiry_days_for(reason_code):
    return REASON_EXPIRY_DAYS.get((reason_code or '').strip().lower(), DEFAULT_EXPIRY_DAYS)

# Named so the refusal message can explain why these are not suppressible.
NON_DETERMINISTIC_EXAMPLES = (
    'uncertain sponsorship', 'unstated salary', 'one missing skill',
    'subjective preference',
)


def suppression_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def today():
    return date.today()


def parse_date(value):
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def norm_title(value):
    """Title reduced to its words.

    Punctuation and whitespace differences between two renderings of the same
    advert are not a change. A different word is.
    """
    return ' '.join(re.sub(r'[^0-9a-z]+', ' ', (value or '').lower()).split())


def change_evidence(url='', posted='', source_job_id='', requisition_id='', title=''):
    """The lightweight identity/change facts worth storing alongside a rejection."""
    return {
        'canonical_url': norm_url(url),
        'posted': (posted or '').strip()[:10],
        'source_job_id': (source_job_id or '').strip(),
        'requisition_id': (requisition_id or '').strip(),
        'title_normalised': norm_title(title),
    }


def reconsider_reasons(record, incoming):
    """Why an otherwise suppressed advert deserves another look.

    Only credible evidence that this is a materially changed or reposted vacancy
    counts. A missing incoming value proves nothing, and neither does a stored
    blank, so both sides must be present before a difference means anything.
    """
    reasons = []
    stored_posted = parse_date(record.get('posted', ''))
    incoming_posted = parse_date(incoming.get('posted', ''))
    if stored_posted and incoming_posted and incoming_posted > stored_posted:
        reasons.append('newer_posted_date')
    for field, reason in (('requisition_id', 'changed_requisition_id'),
                          ('source_job_id', 'changed_source_job_id')):
        stored, incoming_value = record.get(field, ''), incoming.get(field, '')
        if stored and incoming_value and stored != incoming_value:
            reasons.append(reason)
    stored_title = record.get('title_normalised', '') or norm_title(record.get('title', ''))
    incoming_title = incoming.get('title_normalised', '')
    if stored_title and incoming_title and stored_title != incoming_title:
        reasons.append('changed_title')
    return reasons


def identity_key(url='', company='', title='', location=''):
    """Canonical identity, matching the discovery-state key convention."""
    canonical = norm_url(url)
    if canonical:
        return canonical
    company, title = norm_text(company), norm_text(title)
    if not company or not title:
        raise suppression_error(
            'A suppression record needs either a URL or both a company and a title.',
        )
    return f'{company}::{title}::{norm_location(location)}'


def load_store():
    if not STORE.exists():
        return {'schema_version': SCHEMA_VERSION, 'suppressed': {}}
    try:
        raw = STORE.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise suppression_error(f'Suppression store could not be read: {STORE}',
                                f'{type(exc).__name__}: {exc}') from None
    if not raw.strip():
        return {'schema_version': SCHEMA_VERSION, 'suppressed': {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise suppression_error(
            f'Malformed suppression store: {STORE}',
            f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}',
            'This store is a cheap cache of deterministic rejections. Deleting it is '
            'safe: it only costs one extra pass of cheap filtering.',
        ) from None
    if not isinstance(data, dict) or not isinstance(data.get('suppressed'), dict):
        raise suppression_error(f'Invalid suppression store: {STORE}',
                                'Expected an object with a "suppressed" map.')
    return data


def save_store(data):
    payload = {'schema_version': SCHEMA_VERSION, 'suppressed': data.get('suppressed', {})}
    atomic_write_text(STORE, json.dumps(payload, indent=2, ensure_ascii=False) + '\n')


def is_expired(record, on=None):
    expires = parse_date(record.get('expires_at'))
    if expires is None:
        return True
    return expires < (on or today())


def decide(store, url='', company='', title='', location='', on=None,
           posted='', source_job_id='', requisition_id=''):
    """The single source of truth for one suppression decision.

    `check` and `check-batch` both route through this, so a batch can never
    disagree with an individual check.

    A stored rejection blocks deep work only while the advert still looks like the
    advert that was rejected. Credible evidence of a repost or a material rewrite
    produces `suppressed: false` with `reconsider: true`, which is a different
    outcome from an expiry and from never having been seen.
    """
    key = identity_key(url, company, title, location)
    record = store.get('suppressed', {}).get(key)
    if record is None:
        return {'key': key, 'suppressed': False, 'expired': False,
                'reconsider': False, 'reconsider_reason': '', 'reconsider_reasons': [],
                'reason_code': '', 'record': None}
    expired = is_expired(record, on)
    incoming = change_evidence(url, posted, source_job_id, requisition_id, title)
    reasons = [] if expired else reconsider_reasons(record, incoming)
    return {
        'key': key,
        'suppressed': not expired and not reasons,
        'expired': expired,
        'reconsider': bool(reasons),
        'reconsider_reason': reasons[0] if reasons else '',
        'reconsider_reasons': reasons,
        'reason_code': record.get('reason_code', ''),
        'expires_at': record.get('expires_at', ''),
        'record': record,
    }


def touch(store, key, on=None):
    record = store.get('suppressed', {}).get(key)
    if record is None:
        return None
    record['last_seen'] = (on or today()).isoformat()
    record['hits'] = int(record.get('hits', 0)) + 1
    return record


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def read_json_input(args):
    if getattr(args, 'file', ''):
        path = Path(args.file)
        if not path.exists():
            raise suppression_error(f'Input file not found: {path}')
        raw = path.read_text(encoding='utf-8')
    else:
        raw = sys.stdin.read()
    # Windows shells routinely prefix piped text with a byte-order mark.
    raw = raw.lstrip('﻿')
    if not raw.strip():
        raise suppression_error('No JSON input received.',
                                'Pass --file <path> or pipe a JSON array on stdin.')
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise suppression_error('Malformed JSON input.',
                                f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None


def calibration_disabled(reason):
    """Return why the candidate's calibration forbids this reason, or ''.

    A blocker whose calibration field is null cannot fire. The evaluator already
    refuses such a blocker in a ranking proposal; suppression had no equivalent
    gate, so the first real run suppressed a vacancy on `security_clearance` while
    `constraints.security_clearance_obtainable` was null. Suppression is the more
    dangerous of the two, because it removes the vacancy from FUTURE runs silently.
    Both now read the same calibration through the same function.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import match_evaluation
        policy = match_evaluation.load_policy()
        config_path = ROOT / 'candidate' / 'config.json'
        if not config_path.exists():
            return ''
        config = json.loads(config_path.read_text(encoding='utf-8'))
        enabled, disabled = match_evaluation.applicable_blockers(config, policy)
        vocabulary = {e['id'] for e in policy['hard_blockers']['vocabulary']}
    except Exception:  # noqa: BLE001
        # A missing or unreadable policy must not block ordinary suppression.
        return ''
    if reason not in vocabulary:
        # Not a hard-blocker reason at all (contract, temporary, ...), so the
        # calibration gate does not apply to it.
        return ''
    if reason in enabled:
        return ''
    return disabled.get(reason, 'the candidate calibration does not enable it')


def cmd_add(args):
    reason = (args.reason_code or '').strip().lower()
    if reason not in REASON_CODES:
        raise suppression_error(
            f'Invalid --reason-code: {args.reason_code!r}',
            f'Allowed values: {", ".join(REASON_CODES)}',
            'Deterministic rejections only. ' + ', '.join(NON_DETERMINISTIC_EXAMPLES)
            + ' are not suppressible, because each can change with one more piece of '
              'evidence and none of them is an objective disqualification.',
        )
    why_disabled = calibration_disabled(reason)
    if why_disabled:
        raise suppression_error(
            f'The candidate calibration never enabled the {reason!r} blocker, so it '
            f'cannot be a reason to suppress a vacancy.',
            f'Reason: {why_disabled}.',
            'A null calibration value means UNKNOWN, not false. An unknown salary '
            'floor is not a floor of zero, and an unknown clearance constraint is not '
            'a refusal. Suppressing on one hides the vacancy from future runs too.',
            'Fix the calibration if the constraint is real, rather than the suppression.',
        )
    store = load_store()
    key = identity_key(args.url, args.company, args.title, args.location)
    stamp = today()
    default_days = expiry_days_for(reason)
    explicit = args.expiry_days is not None
    expiry_days = max(1, int(args.expiry_days)) if explicit else default_days
    existing = store['suppressed'].get(key)
    record = {
        'key': key,
        'company': (args.company or '').strip(),
        'title': (args.title or '').strip(),
        'reason_code': reason,
        'first_seen': (existing or {}).get('first_seen', stamp.isoformat()),
        'last_seen': stamp.isoformat(),
        'expires_at': (stamp + timedelta(days=expiry_days)).isoformat(),
        'hits': int((existing or {}).get('hits', 0)),
        # Lightweight identity/change evidence, so a later rediscovery can tell a
        # reposted or rewritten advert from the one that was actually rejected.
        **change_evidence(args.url, args.posted, args.source_job_id,
                          args.requisition_id, args.title),
    }
    store['suppressed'][key] = record
    save_store(store)
    print(json.dumps({'suppressed': True, 'key': key, 'reason_code': reason,
                      'expires_at': record['expires_at'],
                      'expiry_days': expiry_days,
                      'default_expiry_days': default_days,
                      'expiry_source': 'explicit' if explicit else 'reason_default',
                      'replaced_existing': existing is not None}, ensure_ascii=False))


def cmd_check(args):
    store = load_store()
    on = parse_date(args.on) or today()
    result = decide(store, args.url, args.company, args.title, args.location, on,
                    args.posted, args.source_job_id, args.requisition_id)
    # A reconsidered vacancy is not a suppression hit. Only a candidate that
    # actually skipped deep work counts toward the run's suppression accounting.
    if args.touch and result['suppressed']:
        touch(store, result['key'], on)
        save_store(store)
        result['record'] = store['suppressed'][result['key']]
    print(json.dumps(result, ensure_ascii=False))


def cmd_check_batch(args):
    payload = read_json_input(args)
    if not isinstance(payload, list):
        raise suppression_error('check-batch expects a JSON array of candidate objects.')
    store = load_store()
    on = parse_date(args.on) or today()
    results, mutated = [], False
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            results.append({'index': index, 'error': 'not_an_object', 'suppressed': False})
            continue
        result = decide(store, entry.get('url', '') or entry.get('source_url', ''),
                        entry.get('company', ''), entry.get('title', ''),
                        entry.get('location', ''), on,
                        entry.get('posted', ''),
                        entry.get('source_job_id', '') or entry.get('job_id', ''),
                        entry.get('requisition_id', ''))
        if args.touch and result['suppressed']:
            touch(store, result['key'], on)
            result['record'] = store['suppressed'][result['key']]
            mutated = True
        results.append({'index': index, **result})
    if mutated:
        save_store(store)
    suppressed = [r for r in results if r.get('suppressed')]
    reconsider = [r for r in results if r.get('reconsider')]
    print(json.dumps({
        'count': len(results),
        'suppressed_count': len(suppressed),
        'reconsider_count': len(reconsider),
        'skip_deep_work_for': [r['key'] for r in suppressed],
        'reconsider_keys': [r['key'] for r in reconsider],
        'results': results,
    }, indent=2, ensure_ascii=False))


def cmd_list(args):
    store = load_store()
    on = parse_date(args.on) or today()
    rows = []
    for key, record in store.get('suppressed', {}).items():
        expired = is_expired(record, on)
        if args.active_only and expired:
            continue
        rows.append({**record, 'expired': expired})
    rows.sort(key=lambda r: (r.get('expires_at', ''), r.get('company', '')))
    print(json.dumps({
        'store': STORE.relative_to(ROOT).as_posix(),
        'total': len(store.get('suppressed', {})),
        'returned': len(rows),
        'reason_codes': list(REASON_CODES),
        'records': rows,
    }, indent=2, ensure_ascii=False))


def cmd_prune(args):
    store = load_store()
    on = parse_date(args.on) or today()
    expired = [k for k, r in store.get('suppressed', {}).items() if is_expired(r, on)]
    if not args.dry_run:
        for key in expired:
            store['suppressed'].pop(key, None)
        save_store(store)
    print(json.dumps({'pruned': len(expired), 'dry_run': bool(args.dry_run),
                      'remaining': len(store.get('suppressed', {}))}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description='Deterministic rejection suppression store')
    sub = p.add_subparsers(dest='cmd', required=True)

    a = sub.add_parser('add', help='Suppress one deterministically rejected vacancy.')
    a.add_argument('--url', default='')
    a.add_argument('--company', default='')
    a.add_argument('--title', default='')
    a.add_argument('--location', default='')
    a.add_argument('--reason-code', dest='reason_code', required=True)
    a.add_argument('--expiry-days', dest='expiry_days', type=int, default=None,
                   help='Override the reason-specific default expiry.')
    a.add_argument('--posted', default='', help='Verified posted date of the advert.')
    a.add_argument('--source-job-id', dest='source_job_id', default='')
    a.add_argument('--requisition-id', dest='requisition_id', default='')
    a.set_defaults(func=cmd_add)

    c = sub.add_parser('check', help='Check one candidate against the store.')
    c.add_argument('--url', default='')
    c.add_argument('--company', default='')
    c.add_argument('--title', default='')
    c.add_argument('--location', default='')
    c.add_argument('--posted', default='', help='Verified posted date now advertised.')
    c.add_argument('--source-job-id', dest='source_job_id', default='')
    c.add_argument('--requisition-id', dest='requisition_id', default='')
    c.add_argument('--touch', action='store_true',
                   help='Refresh last_seen and count the hit. Only an actively '
                        'suppressed candidate is a hit.')
    c.add_argument('--on', default='', help='Evaluate expiry as at this ISO date.')
    c.set_defaults(func=cmd_check)

    cb = sub.add_parser('check-batch', help='Check a JSON array of candidates in one process.')
    cb.add_argument('--file', default='')
    cb.add_argument('--touch', action='store_true')
    cb.add_argument('--on', default='')
    cb.set_defaults(func=cmd_check_batch)

    l = sub.add_parser('list', help='List suppression records.')
    l.add_argument('--active-only', dest='active_only', action='store_true')
    l.add_argument('--on', default='')
    l.set_defaults(func=cmd_list)

    pr = sub.add_parser('prune', help='Remove expired suppression records.')
    pr.add_argument('--dry-run', dest='dry_run', action='store_true')
    pr.add_argument('--on', default='')
    pr.set_defaults(func=cmd_prune)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Private sponsorship evidence cache, keyed by employer entity.

Sponsorship research is the most expensive verification this workspace does, and
it is repeated constantly: the same employer appears across LinkedIn, Indeed, two
boards and its own careers page inside one run. Caching it is worth real budget.

WHAT THIS DELIBERATELY IS NOT: a boolean. The single most damaging error available
here is collapsing evidence into `sponsors: true`. A Skilled Worker licence on the
register means the ORGANISATION holds a licence. It does not mean:

  - this vacancy will be sponsored
  - this role meets the going rate or skill level
  - the licence is still valid today
  - the register's organisation name is the employer you actually found

So each employer stores a STATUS plus the EVIDENCE it rests on, with per-item
provenance and per-kind expiry. `sponsorship_status()` derives the status from the
unexpired evidence rather than trusting a stored conclusion, so evidence ageing out
downgrades the answer automatically instead of leaving a stale verdict behind.

THE EVIDENCE LADDER IS UNCHANGED. It is expressed here as data:

  employer_statement    the employer's own current words about sponsorship
  vacancy_statement     this specific advert's words
  sponsor_register      presence on a DATED register snapshot. A lead, never proof.
  ats_signal            an ATS question or field implying a sponsorship route
  press_or_thirdparty   weakest. Never sufficient on its own.
  absence_statement     an explicit "we do not sponsor". Strong and blocking.

`sponsor_register` evidence normally comes from `tools/sponsor_register.py`, which
looks an employer up in an installed snapshot of the official GOV.UK register and
hands back the matched organisation, its rating, the routes the file actually
lists, the snapshot digest and the match quality. Those are recorded verbatim.
There is no field anywhere in this schema that could assert a vacancy will be
sponsored, because a register entry cannot support that claim.

TTLs DIFFER BY EVIDENCE KIND because the kinds decay differently. A dated register
snapshot is the clearest case: it was true on its extract date and becomes less
trustworthy every week, so it must never harden into permanent truth. An explicit
employer statement lasts longer than a third-party mention.

REGISTER EVIDENCE EXPIRES FROM THE SNAPSHOT DATE, not from the day somebody looked
it up. Otherwise a lookup against a three-week-old snapshot would reset the clock
and dress three-week-old data as current, which is exactly the failure the dated
extract is recorded to prevent.

A decision-critical conclusion may still require live verification. This cache
records `requires_live_check`, and a high-priority recommendation must honour it.
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import atomic_write_text  # noqa: E402
from employers import employer_key  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / 'job_scraper' / 'sponsorship_evidence.json'
SCHEMA_VERSION = 1

# Machine vocabulary shared with the discovery record's sponsorship_label.
STATUSES = ('unknown', 'blocked', 'weak', 'moderate', 'strong')
CONFIDENCES = ('low', 'medium', 'high')

EVIDENCE_KINDS = ('employer_statement', 'vacancy_statement', 'sponsor_register',
                  'ats_signal', 'press_or_thirdparty', 'absence_statement')

# How long one piece of evidence stays usable, in days. A dated register subset
# decays fastest of the positive signals because it is a snapshot by construction.
EVIDENCE_TTL_DAYS = {
    'employer_statement': 90,
    'vacancy_statement': 30,
    'sponsor_register': 30,
    'ats_signal': 60,
    'press_or_thirdparty': 30,
    'absence_statement': 90,
}

# What each evidence kind can support ON ITS OWN. This is the ladder as data, and it
# is the mechanism that stops a register hit becoming a sponsorship promise.
EVIDENCE_CEILING = {
    'employer_statement': 'strong',
    'vacancy_statement': 'strong',
    'sponsor_register': 'moderate',
    'ats_signal': 'weak',
    'press_or_thirdparty': 'weak',
    'absence_statement': 'blocked',
}
STATUS_RANK = {status: index for index, status in enumerate(STATUSES)}

EVIDENCE_FIELDS = ('kind', 'source', 'url', 'observed_at', 'expires_at', 'detail',
                   'register_extract_date',
                   # Official-register provenance, written by tools/sponsor_register.py
                   # from an installed GOV.UK snapshot. `routes` and `rating` are
                   # quoted from the official file, never inferred, and there is
                   # deliberately no field that could assert a vacancy will sponsor.
                   'organisation', 'snapshot_sha256', 'rating', 'routes', 'match_quality')


def evidence_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def today_iso(on=''):
    return (on or '').strip() or date.today().isoformat()


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def ttl_days_for(kind):
    return EVIDENCE_TTL_DAYS.get(kind, 30)


def load_store(path=None):
    path = Path(path) if path else STORE
    if not path.exists():
        return {'schema_version': SCHEMA_VERSION, 'employers': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise evidence_error(
            f'Malformed sponsorship evidence cache: {path}',
            f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}',
            'Fix or remove the file. It is a rebuildable cache, not primary evidence.',
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise evidence_error(f'Sponsorship evidence cache could not be read: {path}',
                             f'{type(exc).__name__}: {exc}') from None
    if not isinstance(data, dict) or not isinstance(data.get('employers'), dict):
        raise evidence_error(f'Invalid sponsorship evidence cache: {path}',
                             'Expected an object with an "employers" mapping.')
    return data


def save_store(data, path=None):
    path = Path(path) if path else STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    data['schema_version'] = SCHEMA_VERSION
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def evidence_problems(item):
    problems = []
    if not isinstance(item, dict):
        return [{'field': '_root', 'problem': 'not_an_object'}]
    for field in sorted(set(item) - set(EVIDENCE_FIELDS)):
        problems.append({'field': field, 'problem': 'not_an_evidence_field'})
    kind = str(item.get('kind') or '').strip().lower()
    if not kind:
        problems.append({'field': 'kind', 'problem': 'required'})
    elif kind not in EVIDENCE_KINDS:
        problems.append({'field': 'kind', 'value': kind, 'problem': 'not_in_vocabulary'})
    # Provenance is not optional. Evidence nobody can trace is not evidence.
    if not str(item.get('source') or '').strip():
        problems.append({'field': 'source', 'problem': 'required'})
    if not str(item.get('observed_at') or '').strip():
        problems.append({'field': 'observed_at', 'problem': 'required'})
    if kind == 'sponsor_register' and not str(item.get('register_extract_date') or '').strip():
        # A register hit without its extract date cannot be aged, and an unageable
        # register hit is exactly how a snapshot becomes permanent truth.
        problems.append({'field': 'register_extract_date', 'problem': 'required_for_sponsor_register'})
    return problems


def is_expired(item, on=''):
    expires = str(item.get('expires_at') or '').strip()
    return bool(expires) and expires < today_iso(on)


def sponsorship_status(record, on=''):
    """Derive the current status from UNEXPIRED evidence only.

    Deriving rather than reading a stored verdict is the point: when the last
    supporting item expires the answer falls back to `unknown` by itself, instead
    of leaving a confident conclusion nothing supports.
    """
    items = [i for i in (record or {}).get('evidence', []) if isinstance(i, dict)]
    live = [i for i in items if not is_expired(i, on)]
    expired = [i for i in items if is_expired(i, on)]

    blocked = [i for i in live if str(i.get('kind')).lower() == 'absence_statement']
    if blocked:
        status, ceiling_kind = 'blocked', 'absence_statement'
    else:
        status, ceiling_kind = 'unknown', ''
        for item in live:
            kind = str(item.get('kind') or '').lower()
            ceiling = EVIDENCE_CEILING.get(kind, 'unknown')
            if STATUS_RANK.get(ceiling, 0) > STATUS_RANK.get(status, 0):
                status, ceiling_kind = ceiling, kind

    kinds = sorted({str(i.get('kind') or '').lower() for i in live})
    # Confidence rises with corroboration, never with repetition of one weak source.
    if status in ('unknown',):
        confidence = 'low'
    elif len(kinds) >= 2 and status in ('strong', 'blocked'):
        confidence = 'high'
    elif len(kinds) >= 2:
        confidence = 'medium'
    elif status in ('strong', 'blocked'):
        confidence = 'medium'
    else:
        confidence = 'low'

    # A register licence is an organisation fact, never a vacancy promise, so a
    # conclusion resting on it alone always demands a live check before it is used
    # to justify a high-priority recommendation.
    register_only = kinds == ['sponsor_register']
    requires_live_check = bool(register_only or status == 'unknown'
                               or (expired and not live))
    return {
        'status': status,
        'confidence': confidence,
        'evidence_kinds': kinds,
        'live_evidence': len(live),
        'expired_evidence': len(expired),
        'derived_from': ceiling_kind,
        'register_only': register_only,
        'requires_live_check': requires_live_check,
        'caveat': (
            'Presence on the sponsor register means the ORGANISATION holds a licence. '
            'It is not evidence that this vacancy will be sponsored, that the role '
            'meets the going rate or skill level, or that the licence is still valid '
            'today. Verify live before any decision-critical use.'
            if register_only else
            'Sponsorship status is derived from unexpired evidence only. Re-verify '
            'live before a decision-critical recommendation.'
        ),
    }


def expiry_basis(kind, register_extract_date='', observed_at=''):
    """The date an evidence item's expiry is measured FROM.

    Register evidence expires from the SNAPSHOT date, not from the day it happened
    to be looked up. A register snapshot downloaded three weeks ago describes the
    register as it stood three weeks ago, so caching a lookup of it today must not
    reset the clock and dress three-week-old data as current. Every other kind
    expires from when it was actually observed.
    """
    if kind == 'sponsor_register' and str(register_extract_date or '').strip():
        basis = str(register_extract_date).strip()[:10]
        try:
            return date.fromisoformat(basis)
        except ValueError:
            pass
    stamp = str(observed_at or '').strip()[:10]
    try:
        return date.fromisoformat(stamp)
    except ValueError:
        return date.today()


def add_evidence(name, kind, source, detail='', url='', observed_at='',
                 register_extract_date='', expiry_days=None, store=None, path=None,
                 organisation='', snapshot_sha256='', rating='', routes='',
                 match_quality=''):
    """Record one piece of sponsorship evidence against an employer entity."""
    kind = str(kind or '').strip().lower()
    if kind not in EVIDENCE_KINDS:
        raise evidence_error(
            f'Invalid evidence kind: {kind!r}',
            f'Allowed: {", ".join(EVIDENCE_KINDS)}',
            'Sponsorship is recorded as traceable evidence, never as a bare boolean.',
        )
    data = store or load_store(path)
    key = employer_key(name)
    if not key:
        raise evidence_error('Sponsorship evidence needs a usable employer name.')

    observed = (observed_at or '').strip() or now_iso()
    days = int(expiry_days) if expiry_days is not None else ttl_days_for(kind)
    basis = expiry_basis(kind, register_extract_date, observed)
    item = {
        'kind': kind,
        'source': str(source or '').strip(),
        'url': str(url or '').strip(),
        'observed_at': observed,
        'expires_at': (basis + timedelta(days=days)).isoformat(),
        'detail': str(detail or '').strip(),
    }
    if register_extract_date:
        item['register_extract_date'] = str(register_extract_date).strip()
    # Official-register provenance. Recorded verbatim so a later reader can see
    # exactly which snapshot and which matched organisation the licence rests on.
    for field, value in (('organisation', organisation), ('snapshot_sha256', snapshot_sha256),
                         ('rating', rating), ('routes', routes),
                         ('match_quality', match_quality)):
        if str(value or '').strip():
            item[field] = str(value).strip()
    item = {f: v for f, v in item.items() if v}

    problems = evidence_problems(item)
    if problems:
        raise evidence_error('Refusing to store sponsorship evidence without provenance.',
                             f'Problems: {json.dumps(problems, ensure_ascii=False)}',
                             'Every evidence item needs its kind, its source and when it '
                             'was observed. A register hit also needs its extract date.')

    record = data.setdefault('employers', {}).setdefault(
        key, {'employer_key': key, 'canonical_name': str(name).strip(), 'evidence': []})
    record['evidence'] = [i for i in record.get('evidence', [])
                          if not (i.get('kind') == kind and i.get('url', '') == item.get('url', ''))]
    record['evidence'].append(item)
    record['checked_at'] = now_iso()
    record['expiry_days_used'] = days
    save_store(data, path)
    derived = sponsorship_status(record)
    return {'employer_key': key, 'evidence_count': len(record['evidence']),
            'expiry_days': days, **derived}


def get_record(name, store=None, path=None, on=''):
    data = store or load_store(path)
    key = employer_key(name)
    record = data.get('employers', {}).get(key)
    if record is None:
        return {'found': False, 'employer_key': key, 'status': 'unknown',
                'confidence': 'low', 'requires_live_check': True,
                'caveat': 'No stored sponsorship evidence. Verify live before use.'}
    return {'found': True, 'employer_key': key,
            'canonical_name': record.get('canonical_name', ''),
            'checked_at': record.get('checked_at', ''),
            **sponsorship_status(record, on),
            'evidence': record.get('evidence', [])}


def prune(on='', store=None, path=None):
    data = store or load_store(path)
    removed, kept = 0, 0
    for record in data.get('employers', {}).values():
        before = len(record.get('evidence', []))
        record['evidence'] = [i for i in record.get('evidence', []) if not is_expired(i, on)]
        removed += before - len(record['evidence'])
        kept += len(record['evidence'])
    save_store(data, path)
    return {'pruned': removed, 'remaining': kept}


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_add(args):
    print(json.dumps(add_evidence(
        args.employer, args.kind, args.source, detail=args.detail, url=args.url,
        observed_at=args.observed_at, register_extract_date=args.register_extract_date,
        expiry_days=args.expiry_days, organisation=args.organisation,
        snapshot_sha256=args.snapshot_sha256, rating=args.rating, routes=args.routes,
        match_quality=args.match_quality), indent=2, ensure_ascii=False))


def cmd_add_register(args):
    """Record a licence hit from the installed official GOV.UK register snapshot.

    The payload is built by `sponsor_register.py` from a real snapshot, so nothing
    here is typed by hand and nothing can assert more than a licence. A miss is a
    miss in that snapshot and produces no evidence at all rather than a negative.
    """
    from sponsor_register import evidence_payload, search  # noqa: E402
    result = search(args.employer, on=args.on)
    if result['status'] != 'FOUND':
        print(json.dumps({
            'stored': False, 'employer': args.employer, 'register_status': result['status'],
            'requires_live_check': True,
            'reason': result.get('meaning') or result.get('reason', ''),
            'note': 'No evidence was stored. A register miss, an ambiguous entity or an '
                    'unavailable snapshot is not negative evidence about an employer.',
        }, indent=2, ensure_ascii=False))
        raise SystemExit(1)
    payload = evidence_payload(result)
    stored = add_evidence(
        args.employer, payload['kind'], payload['source'], detail=payload['detail'],
        url=payload['url'], observed_at=payload['observed_at'],
        register_extract_date=payload['register_extract_date'],
        organisation=payload['organisation'], snapshot_sha256=payload['snapshot_sha256'],
        rating=payload['rating'], routes=payload['routes'],
        match_quality=payload['match_quality'])
    print(json.dumps({'stored': True, 'snapshot_fresh': result.get('snapshot_fresh', False),
                      'has_skilled_worker_route': result.get('has_skilled_worker_route', False),
                      **stored}, indent=2, ensure_ascii=False))


def cmd_get(args):
    record = get_record(args.employer, on=args.on)
    print(json.dumps(record, indent=2, ensure_ascii=False))
    raise SystemExit(0 if record.get('found') else 1)


def cmd_list(args):
    data = load_store()
    rows = []
    for key, record in sorted(data.get('employers', {}).items()):
        rows.append({'employer_key': key, 'canonical_name': record.get('canonical_name', ''),
                     'checked_at': record.get('checked_at', ''),
                     **{k: v for k, v in sponsorship_status(record, args.on).items()
                        if k in ('status', 'confidence', 'live_evidence', 'expired_evidence',
                                 'register_only', 'requires_live_check')}})
    print(json.dumps({'count': len(rows), 'employers': rows}, indent=2, ensure_ascii=False))


def cmd_prune(args):
    print(json.dumps(prune(on=args.on), ensure_ascii=False))


def cmd_kinds(args):
    print(json.dumps({
        'kinds': [{'kind': k, 'ttl_days': EVIDENCE_TTL_DAYS[k],
                   'supports_at_most': EVIDENCE_CEILING[k]} for k in EVIDENCE_KINDS],
        'statuses': list(STATUSES),
        'note': 'A sponsor-register hit supports at most "moderate" and always requires '
                'a live check before a decision-critical recommendation. A licence is '
                'not a promise that this vacancy will be sponsored.',
    }, indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description='Private sponsorship evidence cache')
    sub = p.add_subparsers(dest='cmd', required=True)

    a = sub.add_parser('add', help='Record one piece of sponsorship evidence.')
    a.add_argument('--employer', required=True)
    a.add_argument('--kind', required=True)
    a.add_argument('--source', required=True, help='Where the evidence came from.')
    a.add_argument('--detail', default='')
    a.add_argument('--url', default='')
    a.add_argument('--observed-at', dest='observed_at', default='')
    a.add_argument('--register-extract-date', dest='register_extract_date', default='')
    a.add_argument('--expiry-days', dest='expiry_days', type=int)
    a.add_argument('--organisation', default='', help='Matched registered organisation name.')
    a.add_argument('--snapshot-sha256', dest='snapshot_sha256', default='')
    a.add_argument('--rating', default='', help='Type and rating quoted from the register.')
    a.add_argument('--routes', default='', help='Routes quoted from the register.')
    a.add_argument('--match-quality', dest='match_quality', default='')
    a.set_defaults(func=cmd_add)

    ar = sub.add_parser('add-register',
                        help='Record a licence hit from the installed GOV.UK snapshot.')
    ar.add_argument('--employer', required=True)
    ar.add_argument('--on', default='')
    ar.set_defaults(func=cmd_add_register)

    g = sub.add_parser('get', help='Derive current sponsorship status for one employer.')
    g.add_argument('--employer', required=True)
    g.add_argument('--on', default='', help='Evaluate as of this date (YYYY-MM-DD).')
    g.set_defaults(func=cmd_get)

    l = sub.add_parser('list', help='Summarise stored sponsorship evidence.')
    l.add_argument('--on', default='')
    l.set_defaults(func=cmd_list)

    pr = sub.add_parser('prune', help='Drop expired evidence items.')
    pr.add_argument('--on', default='')
    pr.set_defaults(func=cmd_prune)

    k = sub.add_parser('kinds', help='Show the evidence ladder, TTLs and ceilings.')
    k.set_defaults(func=cmd_kinds)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

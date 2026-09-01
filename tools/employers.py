#!/usr/bin/env python3
"""Private employer entity cache.

The same employer is met repeatedly: as a LinkedIn company name, an Indeed
company name, a board advert, a careers page, an ATS tenant, and a sponsor-register
organisation. Re-resolving that identity on every encounter is expensive and, worse,
inconsistent: one run decides `Xelix` is `GSPV Limited` and the next does not.

This cache normalises that work once. It is NOT preference learning and holds no
candidate data: it records what an EMPLOYER is, never what the candidate thinks of
one.

    employer_key         stable normalised identity key
    canonical_name       the name to display
    aliases              explicitly confirmed alternative names
    website_domain       the employer's own domain, when known
    careers_url          the employer careers page, when known
    ats_platform         greenhouse / lever / ashby / workable / smartrecruiters /
                         workday / oracle, when known
    ats_tenant           the tenant slug inside that platform, when known
    sponsor_register_name the exact organisation name on the sponsor register, which
                         is frequently NOT the trading name
    last_verified        when this entity's facts were last confirmed
    source_confidence    how good the evidence behind those facts is

MATCHING IS CONSERVATIVE, BY DESIGN. Naive substring matching on short company
names is actively dangerous: `One` substring-matches `AXONE`, and `Sky` matches
`Kaspersky`. Merging two employers is far more damaging than failing to merge them,
because a wrong merge silently attaches one company's sponsorship evidence and ATS
identity to another. So resolution is graded and only strong evidence resolves:

    exact                normalised names are identical
    legal_suffix         identical once Ltd/Limited/PLC/etc are removed
    alias                an explicitly recorded alias, entered deliberately
    domain               the same employer website domain
    weak_substring       NEVER resolves. Reported as a suggestion only.

The graded matcher is shared with `check_sponsor.py` so the sponsor register and the
employer cache cannot disagree about what counts as a credible name match.
"""
import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import atomic_write_text, canon_host, source_host  # noqa: E402
from check_sponsor import LEGAL_SUFFIXES, normalise as sponsor_normalise, without_legal_suffix  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / 'job_scraper' / 'employers.json'
SCHEMA_VERSION = 1

ATS_PLATFORMS = ('greenhouse', 'lever', 'ashby', 'workable', 'smartrecruiters',
                 'workday', 'oracle', 'teamtailor', 'recruitee', 'personio', 'other')
SOURCE_CONFIDENCES = ('low', 'medium', 'high')

# Resolution qualities that may actually merge an identity. `weak_substring` is
# deliberately absent: it is a suggestion for a human, never an automatic merge.
RESOLVING_QUALITIES = ('exact', 'legal_suffix', 'alias', 'domain')
ALL_QUALITIES = RESOLVING_QUALITIES + ('weak_substring', 'none')

# Below this length a name is too generic for anything but an exact or explicitly
# recorded match. "One", "Sky" and "Rise" are real company names AND common words.
SHORT_NAME_CHARS = 5

FIELDS = ('employer_key', 'canonical_name', 'aliases', 'website_domain', 'careers_url',
          'ats_platform', 'ats_tenant', 'sponsor_register_name', 'last_verified',
          'source_confidence', 'first_seen', 'notes')


def employer_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def norm_name(value):
    """Normalised employer name: case, punctuation and spacing removed."""
    return sponsor_normalise(value)


def employer_key(value):
    """Stable identity key for one employer name.

    The legal suffix is dropped, because `Acme Ltd` and `Acme Limited` are one
    employer and a cache that disagreed would defeat its own purpose.
    """
    stripped = without_legal_suffix(norm_name(value))
    return re.sub(r'\s+', '-', stripped).strip('-')


def is_short_name(value):
    return len(without_legal_suffix(norm_name(value)).replace(' ', '')) < SHORT_NAME_CHARS


def as_domain(value):
    """A canonical hostname from either a bare domain or a full URL.

    `canon_host` canonicalises a hostname and `source_host` extracts one from a URL,
    so a careers URL and a bare domain must both end up as the same comparable host
    or domain evidence would silently never match.
    """
    text = str(value or '').strip()
    if not text:
        return ''
    return canon_host(source_host(text)) or canon_host(text)


def match_quality(query, entity):
    """How strong the evidence linking a queried name to a stored entity is.

    Canonical-name and alias hits are reported separately because they are
    different kinds of evidence: one is the employer's own name, the other is a
    link somebody deliberately recorded.
    """
    q_norm, q_bare = norm_name(query), without_legal_suffix(norm_name(query))
    canonical = entity.get('canonical_name', '')
    aliases = [a for a in (entity.get('aliases', []) or []) if a]
    if q_norm and q_norm == norm_name(canonical):
        return 'exact'
    if q_norm and any(q_norm == norm_name(a) for a in aliases):
        return 'alias'
    if q_bare and any(q_bare == without_legal_suffix(norm_name(a)) for a in aliases):
        return 'alias'
    if q_bare and canonical and q_bare == without_legal_suffix(norm_name(canonical)):
        return 'legal_suffix'
    names = [canonical] + aliases
    if q_bare and any(q_bare in without_legal_suffix(norm_name(n)) or
                      without_legal_suffix(norm_name(n)) in q_bare for n in names if n):
        return 'weak_substring'
    return 'none'


def load_store(path=None):
    path = Path(path) if path else STORE
    if not path.exists():
        return {'schema_version': SCHEMA_VERSION, 'employers': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise employer_error(
            f'Malformed employer cache: {path}',
            f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}',
            'Fix or remove the file. It is a rebuildable cache, not primary evidence.',
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise employer_error(f'Employer cache could not be read: {path}',
                             f'{type(exc).__name__}: {exc}') from None
    if not isinstance(data, dict) or not isinstance(data.get('employers'), dict):
        raise employer_error(f'Invalid employer cache: {path}',
                             'Expected an object with an "employers" mapping.')
    return data


def save_store(data, path=None):
    path = Path(path) if path else STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    data['schema_version'] = SCHEMA_VERSION
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def entity_problems(entity):
    problems = []
    if not isinstance(entity, dict):
        return [{'field': '_root', 'problem': 'not_an_object'}]
    for field in sorted(set(entity) - set(FIELDS)):
        problems.append({'field': field, 'problem': 'not_an_employer_field'})
    if not entity.get('canonical_name'):
        problems.append({'field': 'canonical_name', 'problem': 'required'})
    platform = str(entity.get('ats_platform') or '').strip().lower()
    if platform and platform not in ATS_PLATFORMS:
        problems.append({'field': 'ats_platform', 'value': platform, 'problem': 'not_in_vocabulary'})
    confidence = str(entity.get('source_confidence') or '').strip().lower()
    if confidence and confidence not in SOURCE_CONFIDENCES:
        problems.append({'field': 'source_confidence', 'value': confidence,
                         'problem': 'not_in_vocabulary'})
    if entity.get('aliases') is not None and not isinstance(entity.get('aliases'), list):
        problems.append({'field': 'aliases', 'problem': 'not_a_list'})
    return problems


def resolve(name, domain='', store=None, path=None):
    """Resolve one employer name, or explain why it did not resolve.

    A weak substring hit never resolves. It is returned as a suggestion so a human
    can record an explicit alias if the two really are the same employer.
    """
    data = store or load_store(path)
    employers = data.get('employers', {})
    key = employer_key(name)
    wanted_domain = as_domain(domain)

    if wanted_domain:
        for stored_key, entity in employers.items():
            if as_domain(entity.get('website_domain', '')) == wanted_domain:
                return {'resolved': True, 'employer_key': stored_key, 'quality': 'domain',
                        'entity': entity, 'suggestions': []}

    best, suggestions = None, []
    for stored_key, entity in employers.items():
        quality = match_quality(name, entity)
        if quality in RESOLVING_QUALITIES:
            rank = RESOLVING_QUALITIES.index(quality)
            if best is None or rank < best[0]:
                best = (rank, stored_key, quality, entity)
        elif quality == 'weak_substring':
            suggestions.append({'employer_key': stored_key,
                                'canonical_name': entity.get('canonical_name', ''),
                                'quality': 'weak_substring',
                                'why_not_resolved': 'a weak substring match is never an '
                                                    'automatic merge; record an explicit '
                                                    'alias if they are the same employer'})

    if best is not None:
        return {'resolved': True, 'employer_key': best[1], 'quality': best[2],
                'entity': best[3], 'suggestions': suggestions}
    return {'resolved': False, 'employer_key': key, 'quality': 'none', 'entity': None,
            'short_name': is_short_name(name), 'suggestions': suggestions}


def upsert(name, store=None, path=None, **fields):
    """Create or update one employer entity, conservatively.

    An existing entity is only extended: a field already known is not overwritten
    by a weaker later sighting unless the caller explicitly says the evidence is
    at least as strong.
    """
    data = store or load_store(path)
    employers = data.setdefault('employers', {})
    found = resolve(name, domain=fields.get('website_domain', ''), store=data)
    key = found['employer_key'] if found['resolved'] else employer_key(name)
    if not key:
        raise employer_error('An employer needs a usable name.',
                             f'{name!r} normalises to an empty identity key.')

    entity = dict(employers.get(key) or {})
    created = not entity
    entity.setdefault('employer_key', key)
    entity.setdefault('canonical_name', re.sub(r'\s+', ' ', str(name)).strip())
    entity.setdefault('aliases', [])
    entity.setdefault('first_seen', now_iso())

    incoming_confidence = str(fields.get('source_confidence') or '').strip().lower()
    existing_confidence = str(entity.get('source_confidence') or '').strip().lower()
    stronger = (SOURCE_CONFIDENCES.index(incoming_confidence)
                >= SOURCE_CONFIDENCES.index(existing_confidence)) if (
        incoming_confidence in SOURCE_CONFIDENCES and existing_confidence in SOURCE_CONFIDENCES
    ) else bool(incoming_confidence)

    for field, value in fields.items():
        if field not in FIELDS or value in (None, '', []):
            continue
        if field == 'aliases':
            merged = list(entity.get('aliases') or [])
            for alias in value:
                alias = re.sub(r'\s+', ' ', str(alias)).strip()
                if alias and not any(norm_name(alias) == norm_name(a) for a in merged):
                    merged.append(alias)
            entity['aliases'] = merged
            continue
        if field == 'website_domain':
            value = as_domain(value)
        if field == 'ats_platform':
            value = str(value).strip().lower()
        if entity.get(field) and not stronger and field != 'last_verified':
            continue
        entity[field] = value

    entity['last_verified'] = fields.get('last_verified') or date.today().isoformat()
    problems = entity_problems(entity)
    if problems:
        raise employer_error('Refusing to write an invalid employer entity.',
                             f'Problems: {json.dumps(problems, ensure_ascii=False)}')
    employers[key] = entity
    save_store(data, path)
    return {'employer_key': key, 'created': created, 'entity': entity,
            'matched_quality': found.get('quality', 'none')}


def ats_search_target(entity):
    """The targeted discovery task a known ATS identity makes possible.

    Without a tenant there is nothing targeted to search, so this returns None
    rather than inventing a URL. An unresolved employer stays perfectly usable
    through ordinary board discovery; it simply gets no shortcut.
    """
    if not isinstance(entity, dict):
        return None
    platform = str(entity.get('ats_platform') or '').strip().lower()
    tenant = str(entity.get('ats_tenant') or '').strip()
    careers = str(entity.get('careers_url') or '').strip()
    if platform and tenant:
        return {
            'strategy': 'ats_tenant',
            'source_id': 'employer-ats',
            'ats_platform': platform,
            'ats_tenant': tenant,
            'employer_key': entity.get('employer_key', ''),
            'reason': f'known {platform} tenant {tenant}',
        }
    if careers:
        return {
            'strategy': 'careers_page',
            'source_id': 'employer-direct',
            'careers_url': careers,
            'employer_key': entity.get('employer_key', ''),
            'reason': 'known employer careers page',
        }
    return None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def read_json_input(args):
    if getattr(args, 'file', ''):
        path = Path(args.file)
        if not path.exists():
            raise employer_error(f'Input file not found: {path}')
        raw = path.read_text(encoding='utf-8')
    else:
        raw = sys.stdin.read()
    raw = raw.lstrip('﻿')
    if not raw.strip():
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise employer_error('Malformed JSON input.',
                             f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None


def cmd_get(args):
    data = load_store()
    key = args.key if args.key in data.get('employers', {}) else employer_key(args.key)
    entity = data.get('employers', {}).get(key)
    if entity is None:
        print(json.dumps({'found': False, 'employer_key': key}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps({'found': True, 'employer_key': key, 'entity': entity,
                      'ats_search_target': ats_search_target(entity)},
                     indent=2, ensure_ascii=False))


def cmd_resolve(args):
    found = resolve(args.name, domain=args.domain)
    if found['resolved']:
        found['ats_search_target'] = ats_search_target(found['entity'])
    print(json.dumps(found, indent=2, ensure_ascii=False))
    raise SystemExit(0 if found['resolved'] else 1)


def cmd_upsert(args):
    result = upsert(
        args.name,
        canonical_name=args.canonical_name or '',
        aliases=[a for a in (args.alias or []) if a.strip()],
        website_domain=args.website_domain or '',
        careers_url=args.careers_url or '',
        ats_platform=args.ats_platform or '',
        ats_tenant=args.ats_tenant or '',
        sponsor_register_name=args.sponsor_register_name or '',
        source_confidence=args.source_confidence or '',
        notes=args.notes or '',
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_aliases(args):
    result = upsert(args.name, aliases=[a for a in (args.alias or []) if a.strip()])
    print(json.dumps({'employer_key': result['employer_key'],
                      'aliases': result['entity'].get('aliases', [])}, ensure_ascii=False))


def cmd_list(args):
    data = load_store()
    rows = []
    for key, entity in sorted(data.get('employers', {}).items()):
        if args.ats_only and not (entity.get('ats_platform') and entity.get('ats_tenant')):
            continue
        rows.append({'employer_key': key, 'canonical_name': entity.get('canonical_name', ''),
                     'ats_platform': entity.get('ats_platform', ''),
                     'ats_tenant': entity.get('ats_tenant', ''),
                     'website_domain': entity.get('website_domain', ''),
                     'aliases': entity.get('aliases', []),
                     'last_verified': entity.get('last_verified', '')})
    print(json.dumps({'count': len(rows), 'employers': rows}, indent=2, ensure_ascii=False))


def cmd_check_batch(args):
    rows = read_json_input(args)
    if not isinstance(rows, list):
        raise employer_error('check-batch expects a JSON array of {name, domain} rows.')
    data = load_store()
    results = []
    for index, row in enumerate(rows):
        row = row if isinstance(row, dict) else {'name': row}
        found = resolve(row.get('name', ''), domain=row.get('domain', ''), store=data)
        results.append({
            'index': index,
            'name': row.get('name', ''),
            'resolved': found['resolved'],
            'employer_key': found['employer_key'],
            'quality': found['quality'],
            'ats_search_target': ats_search_target(found['entity']) if found['resolved'] else None,
            'suggestions': found.get('suggestions', []),
        })
    print(json.dumps({'count': len(results),
                      'resolved_count': sum(1 for r in results if r['resolved']),
                      'results': results}, indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description='Private employer entity cache')
    sub = p.add_subparsers(dest='cmd', required=True)

    g = sub.add_parser('get', help='Read one employer entity by key or name.')
    g.add_argument('key')
    g.set_defaults(func=cmd_get)

    r = sub.add_parser('resolve', help='Resolve a company name to a stored entity.')
    r.add_argument('name')
    r.add_argument('--domain', default='')
    r.set_defaults(func=cmd_resolve)

    u = sub.add_parser('upsert', help='Create or extend one employer entity.')
    u.add_argument('name')
    u.add_argument('--canonical-name', dest='canonical_name', default='')
    u.add_argument('--alias', action='append', default=[])
    u.add_argument('--website-domain', dest='website_domain', default='')
    u.add_argument('--careers-url', dest='careers_url', default='')
    u.add_argument('--ats-platform', dest='ats_platform', default='')
    u.add_argument('--ats-tenant', dest='ats_tenant', default='')
    u.add_argument('--sponsor-register-name', dest='sponsor_register_name', default='')
    u.add_argument('--source-confidence', dest='source_confidence', default='')
    u.add_argument('--notes', default='')
    u.set_defaults(func=cmd_upsert)

    a = sub.add_parser('aliases', help='Record explicit alternative names.')
    a.add_argument('name')
    a.add_argument('--alias', action='append', default=[])
    a.set_defaults(func=cmd_aliases)

    l = sub.add_parser('list', help='List stored employer entities.')
    l.add_argument('--ats-only', dest='ats_only', action='store_true')
    l.set_defaults(func=cmd_list)

    b = sub.add_parser('check-batch', help='Resolve many company names in one process.')
    b.add_argument('--file', default='')
    b.set_defaults(func=cmd_check_batch)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

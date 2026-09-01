#!/usr/bin/env python3
"""Graded lookup against the local UK sponsor-register TECH SUBSET.

`data/uksponsorregistertechsubset20260812.csv` is a SUPPLEMENTARY dataset, and
every word of that description is load bearing:

  dated       Extracted 2026-08-12. It does not change, and it ages.
  filtered    A tech/consultancy subset, not the whole register. Thousands of
              licensed organisations are simply not in it, including most
              employers outside the sector filter.
  no routes   It has no visa-route column, so it cannot distinguish a Skilled
              Worker licence from any other.

So it is useful for DISCOVERY and employer leads: a cheap way to notice that a
company is plausibly licensed. It is not the official register, and ABSENCE FROM
IT PROVES NOTHING WHATSOEVER.

The official lookup is `tools/sponsor_register.py`, which maintains a validated
snapshot of the current GOV.UK register of licensed sponsors (workers), preserves
its route and rating columns, and reports UNAVAILABLE rather than a false negative
when no snapshot is installed. Prefer it for any licence question. This helper
remains as the cheap supplementary lead check.
"""
import argparse
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / 'data/uksponsorregistertechsubset20260812.csv'
NAME_FIELD = 'Organisation Name'

# Match qualities, strongest first. Only the first three are credible entity
# evidence. A weak substring is a candidate to look at by hand, never a match.
QUALITY_ORDER = ('exact', 'legal_suffix_exact', 'token', 'weak_substring')
CREDIBLE = frozenset({'exact', 'legal_suffix_exact', 'token'})

LEGAL_SUFFIXES = frozenset({
    'limited', 'ltd', 'plc', 'llp', 'llc', 'inc', 'incorporated', 'corporation',
    'corp', 'company', 'co', 'group', 'holding', 'holdings', 'uk',
})

CAVEAT = (
    'This is the SUPPLEMENTARY tech subset dated 2026-08-12, not the official '
    'register: it is filtered to tech/consultancy and has no visa-route column. '
    'Presence alone does not prove this vacancy will sponsor, and a miss proves '
    'nothing at all, because most licensed organisations are outside this subset. '
    'Use tools/sponsor_register.py for the official GOV.UK register snapshot, and '
    'verify legal-entity variants and current employer evidence live before any '
    'decision that depends on a licence.'
)


def normalise(value):
    value = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', ' ', value).strip()


def tokens(value):
    return [t for t in normalise(value).split() if t]


def without_legal_suffix(value):
    kept = [t for t in tokens(value) if t not in LEGAL_SUFFIXES]
    return ' '.join(kept)


def match_quality(query, organisation):
    """Grade how strongly one organisation name matches the query."""
    nq, no = normalise(query), normalise(organisation)
    if not nq or not no:
        return None
    if nq == no:
        return 'exact'
    sq, so = without_legal_suffix(query), without_legal_suffix(organisation)
    if sq and so and sq == so:
        return 'legal_suffix_exact'
    query_tokens = [t for t in tokens(query) if t not in LEGAL_SUFFIXES] or tokens(query)
    org_tokens = set(tokens(organisation))
    if query_tokens and all(t in org_tokens for t in query_tokens):
        return 'token'
    if nq in no:
        # 'Sky' inside 'Kaspersky', 'One' inside 'AXONE'. Never a confirmed entity.
        return 'weak_substring'
    return None


def search(query, limit):
    try:
        with CSV_PATH.open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        raise SystemExit(f'Sponsor subset unreadable: {CSV_PATH}\n  {type(exc).__name__}: {exc}')

    graded = []
    for row in rows:
        quality = match_quality(query, row.get(NAME_FIELD, ''))
        if quality:
            graded.append((QUALITY_ORDER.index(quality), quality, row))
    graded.sort(key=lambda item: (item[0], (item[2].get(NAME_FIELD) or '').lower()))

    matches = [{
        'quality': quality,
        'credible': quality in CREDIBLE,
        'organisation': row.get(NAME_FIELD, ''),
        'record': {k: (v or '') for k, v in row.items()},
    } for _rank, quality, row in graded]

    credible_hits = sum(1 for m in matches if m['credible'])
    shown = matches[:limit] if limit else matches
    return {
        'query': query,
        'dataset': CSV_PATH.relative_to(ROOT).as_posix(),
        'dataset_rows': len(rows),
        'dataset_has_visa_route_column': False,
        'total_hits': len(matches),
        'credible_hits': credible_hits,
        'weak_substring_hits': len(matches) - credible_hits,
        'shown': len(shown),
        'truncated': len(shown) < len(matches),
        'best_match_quality': matches[0]['quality'] if matches else None,
        'matches': shown,
        'caveat': CAVEAT,
    }


def render_text(result):
    lines = [f"Query: {result['query']}"]
    if not result['total_hits']:
        lines.append('No local subset match.')
    else:
        lines.append(
            f"Hits: {result['total_hits']} total "
            f"({result['credible_hits']} credible, {result['weak_substring_hits']} weak substring); "
            f"showing {result['shown']}"
            + (' (truncated)' if result['truncated'] else ''))
        for match in result['matches']:
            flag = 'CREDIBLE' if match['credible'] else 'NOT A MATCH'
            values = ' | '.join(str(v) for v in match['record'].values())
            lines.append(f"  [{match['quality']:<18} {flag:<11}] {values}")
    lines.append('')
    lines.append(result['caveat'])
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('company', nargs='+', help='Company or legal entity name')
    parser.add_argument('--limit', type=int, default=15, help='Max matches to show (0 = all)')
    parser.add_argument('--text', action='store_true', help='Human-readable output instead of JSON')
    args = parser.parse_args()

    result = search(' '.join(args.company), args.limit)
    print(render_text(result) if args.text else json.dumps(result, indent=2, ensure_ascii=False))
    # A searched-but-unmatched company is a normal, successful result.
    return 0


if __name__ == '__main__':
    sys.exit(main())

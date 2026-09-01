#!/usr/bin/env python3
"""One read-only VIEW of what this workspace canonically knows about a vacancy.

THIS MODULE OWNS NOTHING. It is a resolver, deliberately not a third store, and
that restraint is the point: a second vacancy-facts authority would be free to
disagree with the first, and the disagreement would be invisible.

Two stores already exist and each answers a different question:

    job_scraper/seen_jobs.json    the RECORD: canonical URL, company, title,
                                  location, the structured `facts` the vacancy
                                  stated, and the per-field `facts_provenance`
                                  saying who stated each of them
    job_scraper/cache/<key>.json  the EMPLOYER TEXT: `description_text`, which is
                                  the vacancy's own job-description body with the
                                  search platform's furniture already split off
                                  into `platform_metadata`

A hard blocker is a decided factual rejection, so it has to be checkable against
those, not against whatever the model that proposed it also wrote down. This
module assembles the pair and reports honestly when either half is missing, so
the caller can fail closed rather than guess.

WHAT IS AND IS NOT EMPLOYER EVIDENCE. `description_text` is the employer's own
words by construction: the chrome split at the cache boundary removes the
platform's `Seniority level` / `Employment type` block and stores it separately
under `platform_metadata`, which is not a `FACT_FIELDS` member and can never
reach a fact. A search or results page is never cached at all. So requiring a
blocker's quotation to appear in `description_text` structurally excludes search
cards, recommendation panels and platform classifications, rather than trying to
recognise them.
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import (  # noqa: E402
    STATE, canon_host, norm_url, parse_state, source_host,
)


def _cache():
    """The job-description cache, imported when a description is actually wanted.

    Deliberately not a module-level import. Everything that merely resolves a
    RECORD works without the cache, and importing it up here would make the
    evaluator un-importable in any context that does not ship the cache module,
    turning a missing description into a crash instead of the fail-closed
    `description_available: False` the callers already handle.
    """
    from job_cache import cache_key, load_entry  # noqa: E402
    return cache_key, load_entry

# Provenance that describes a SEARCH CARD rather than the vacancy's own page.
# An aggregator row, a sponsor-board tile and an agency-board listing are all
# summaries written by a third party, so a fact carrying one of these is not the
# employer speaking and can never be the deciding fact under a hard blocker.
CARD_LEVEL_SOURCE_TYPES = frozenset({'aggregator', 'sponsor-board', 'agency-board'})


def normalise_quote(text):
    """Fold a quotation and a job description onto one comparable form.

    Unicode first, because an employer's page and a model's transcription of it
    routinely differ only in a curly apostrophe, a non-breaking space or a soft
    hyphen. NFKC folds the compatibility forms; the explicit table below folds the
    punctuation NFKC deliberately leaves alone, since a right single quotation
    mark and an apostrophe are different characters that mean the same thing here.

    Case and run-length of whitespace are then discarded. Nothing else is: this
    must not become so lenient that an invented sentence starts matching.
    """
    value = unicodedata.normalize('NFKC', str(text or ''))
    for source, target in (
        ('‘', "'"), ('’', "'"), ('‚', "'"), ('‛', "'"),
        ('“', '"'), ('”', '"'), ('„', '"'), ('″', '"'),
        ('‐', '-'), ('‑', '-'), ('‒', '-'), ('–', '-'),
        ('—', '-'), ('―', '-'), ('−', '-'),
        (' ', ' '), (' ', ' '), (' ', ' '), ('​', ''),
        ('­', ''), ('﻿', ''),
    ):
        value = value.replace(source, target)
    return re.sub(r'\s+', ' ', value).strip().lower()


def quote_is_in(quote, text):
    """Whether a quotation genuinely appears in a body of employer text."""
    needle, haystack = normalise_quote(quote), normalise_quote(text)
    return bool(needle) and bool(haystack) and needle in haystack


def load_seen(state_path=None):
    """The discovery records, or {} when there is no readable state.

    Read tolerantly on purpose. A caller resolving a canonical record wants to
    hear `unresolved`, not inherit a state-file traceback from a helper that is
    only trying to look something up.
    """
    path = Path(state_path) if state_path else STATE
    if not path.is_file():
        return {}
    try:
        return parse_state(path.read_text(encoding='utf-8'), path).get('seen', {})
    except (OSError, UnicodeDecodeError, SystemExit):
        return {}


def find_record(identity, seen):
    """The one stored record an identity names, by state key then by canonical URL."""
    if not identity:
        return None, None
    if identity in seen:
        return identity, seen[identity]
    wanted = norm_url(identity)
    if not wanted:
        return None, None
    for key, item in seen.items():
        if norm_url(key) == wanted or norm_url((item or {}).get('url', '')) == wanted:
            return key, item
    return None, None


def authoritative_urls(record):
    """Every URL this workspace has recorded as naming THIS vacancy.

    The record's own preferred URL, plus its origin state key when discovery
    resolved an aggregator sighting to an employer page and kept the origin as the
    key. Nothing else: a URL that merely looks plausible is not evidence.
    """
    urls = set()
    for value in (record.get('url', ''), record.get('canonical_url', '')):
        canonical = norm_url(value)
        if canonical:
            urls.add(canonical)
    return urls


def resolve(identity, seen=None, state_path=None):
    """Assemble the canonical view of one vacancy.

    `seen` lets a caller that has already loaded discovery state pass it in, so a
    write path does not read the same file twice and cannot act on two different
    snapshots of it.

    Returns a mapping that always has `resolved`; when False, `problems` says why.
    """
    seen = load_seen(state_path) if seen is None else seen
    key, record = find_record(str(identity or '').strip(), seen)
    if record is None:
        return {
            'resolved': False, 'identity': identity, 'key': None,
            'problems': [{'field': 'canonical_record', 'reason': 'no_stored_vacancy_matches_this_identity',
                          'value': identity}],
            'facts': {}, 'facts_provenance': {}, 'authoritative_urls': [],
            'description_text': '', 'description_available': False,
            'platform_metadata': {}, 'title': '', 'company': '', 'location': '',
        }

    urls = authoritative_urls(record)
    origin = norm_url(key)
    if origin:
        urls.add(origin)

    entry, description, platform_metadata, cache_url = None, '', {}, ''
    try:
        cache_key, load_entry = _cache()
    except Exception:
        cache_key, load_entry = None, None
    for candidate_url in sorted(urls) if load_entry else ():
        found = load_entry(cache_key(candidate_url))
        if found:
            entry = found
            break
    if entry:
        description = str(entry.get('description_text') or '')
        platform_metadata = entry.get('platform_metadata') or {}
        cache_url = norm_url(entry.get('canonical_url') or entry.get('source_url') or '')
        if cache_url:
            urls.add(cache_url)

    return {
        'resolved': True,
        'identity': identity,
        'key': key,
        'canonical_url': norm_url(record.get('url', '')) or origin,
        'authoritative_urls': sorted(urls),
        'authoritative_hosts': sorted({canon_host(source_host(u)) for u in urls if u}),
        'company': str(record.get('company') or ''),
        'title': str(record.get('title') or ''),
        'location': str(record.get('location') or ''),
        'source_type': str(record.get('source_type') or '').strip().lower(),
        # The RECORD is the fact authority. The cache's own `facts` copy is not
        # consulted here, because two answers to one question is one too many.
        'facts': dict(record.get('facts') or {}),
        'facts_provenance': dict(record.get('facts_provenance') or {}),
        'description_text': description,
        'description_available': bool(description.strip()),
        'description_source_url': cache_url,
        # Kept so a caller can SHOW what the platform claimed. It is never
        # employer evidence and never satisfies a quotation check.
        'platform_metadata': dict(platform_metadata),
        'problems': [],
    }


def fact_provenance_problem(canonical, field):
    """Why a stored fact may not be the deciding fact under a hard blocker.

    Returns a reason string, or None when the fact is employer grade. Absent
    provenance fails closed: a fact nobody attributed is not evidence that an
    employer stated anything, and a decided rejection is exactly where an
    unattributed value must not be trusted.
    """
    entry = (canonical.get('facts_provenance') or {}).get(field)
    if not isinstance(entry, dict) or not entry:
        return 'canonical_fact_has_no_recorded_provenance'
    source_type = str(entry.get('source_type') or '').strip().lower()
    if not source_type:
        return 'canonical_fact_provenance_names_no_source_type'
    if source_type in CARD_LEVEL_SOURCE_TYPES:
        return 'canonical_fact_came_from_a_search_card_rather_than_the_employer'
    return None


def url_is_authoritative(url, canonical):
    """Whether a cited source URL is one this workspace records for this vacancy."""
    wanted = norm_url(str(url or ''))
    return bool(wanted) and wanted in set(canonical.get('authoritative_urls') or [])


# --------------------------------------------------------------------------
# CLI, for inspection and for the deep validator.
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Read-only canonical vacancy view')
    sub = parser.add_subparsers(dest='cmd', required=True)

    show = sub.add_parser('show', help='Resolve one vacancy identity.')
    show.add_argument('--key', required=True, help='State key or vacancy URL.')
    show.add_argument('--full-text', action='store_true',
                      help='Include the whole employer description rather than its length.')

    quote = sub.add_parser('quote', help='Check whether a quotation is in the employer text.')
    quote.add_argument('--key', required=True)
    quote.add_argument('--excerpt', required=True)

    args = parser.parse_args()
    canonical = resolve(args.key)
    if args.cmd == 'quote':
        print(json.dumps({
            'resolved': canonical['resolved'],
            'description_available': canonical['description_available'],
            'found': quote_is_in(args.excerpt, canonical.get('description_text', '')),
        }, indent=2, ensure_ascii=False))
        raise SystemExit(0 if canonical['resolved'] else 1)

    payload = dict(canonical)
    if not args.full_text:
        payload['description_text'] = f"<{len(canonical.get('description_text') or '')} chars>"
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    raise SystemExit(0 if canonical['resolved'] else 1)


if __name__ == '__main__':
    main()

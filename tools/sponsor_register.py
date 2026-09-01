#!/usr/bin/env python3
"""Local cache of the official GOV.UK register of licensed sponsors (workers).

WHY THIS EXISTS. Checking whether an employer holds a Skilled Worker licence is the
single most repeated verification in this workspace, and doing it by web search
costs a request and a model read every time. A local snapshot of the official
register turns almost all of those into a dictionary lookup, leaving live
verification for the cases that genuinely need it.

WHAT IT IS NOT. This is a dated SNAPSHOT of a published register, not an oracle.
Three separate limits apply and none of them may be quietly dropped:

  1. A licence is an ORGANISATION fact. It is never evidence that a particular
     vacancy will be sponsored, that the role meets the going rate or skill level,
     or that the employer will sponsor this candidate.
  2. A miss is a miss in THIS SNAPSHOT under the legal-entity names we know. It is
     never proof that the employer cannot sponsor. Trading names differ from
     registered names constantly, which is exactly why the employer cache records
     `sponsor_register_name` separately.
  3. A snapshot ages. Licences are granted and revoked, so a stale snapshot is
     weaker evidence than a fresh one, and the code says so rather than letting the
     data quietly harden into permanent truth.

RELATIONSHIP TO data/uksponsorregistertechsubset20260812.csv. That file is a DATED
(2026-08-12), FILTERED tech/consultancy subset. It stays, and `check_sponsor.py`
keeps querying it as a cheap supplementary lead helper, but it is not the official
register and absence from it proves nothing at all. Once a snapshot exists here,
this module is the primary official lookup.

NETWORK BOUNDARY. Fetching and installing are deliberately separate functions:

    discover_official_csv(fetch)   find the current CSV from the GOV.UK publication
    download_snapshot(fetch)       fetch its bytes
    install_snapshot(raw, ...)     validate and atomically install. NO NETWORK.
    refresh(fetch=...)             orchestrates the three

so every validation, parsing and installation rule is testable offline with
injected bytes, and the test suite never touches the internet. Only official GOV.UK
hosts are accepted; third-party mirrors are refused by the host allowlist rather
than by convention.

COMPLETENESS IS CHECKED SEPARATELY FROM STRUCTURE, because a truncated download is
invisible to a schema check: the first 40,000 rows of the register parse perfectly,
carry every expected column and look entirely healthy. The GOV.UK Content API
publishes each attachment's byte size, so when that is available the downloaded
length is compared against it exactly and any difference in either direction is
refused. When it is absent (it is optional upstream) an existing validated snapshot
becomes the reference instead: a register that suddenly lost most of its rows or
bytes was truncated, not deregistered. MIN_ROWS remains as defence in depth for the
case with neither a published size nor a previous snapshot.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import atomic_write_text  # noqa: E402
from check_sponsor import normalise as norm_name, without_legal_suffix  # noqa: E402
from employers import employer_key, load_store as load_employer_store, resolve as resolve_employer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = ROOT / 'job_scraper' / 'reference'
SNAPSHOT = REFERENCE_DIR / 'sponsor-register.csv'
META = REFERENCE_DIR / 'sponsor-register-meta.json'
SCHEMA_VERSION = 1

# The GOV.UK publication that carries the current register. The CURRENT attachment
# is discovered from the publication rather than hard-coded, because GOV.UK
# republishes it under a new dated asset URL constantly and a pinned URL would
# quietly serve last month's register forever.
PUBLICATION_PATH = '/government/publications/register-of-licensed-sponsors-workers'
PUBLICATION_URL = f'https://www.gov.uk{PUBLICATION_PATH}'
CONTENT_API_URL = f'https://www.gov.uk/api/content{PUBLICATION_PATH}'

# Only official GOV.UK hosts. A third-party mirror may be stale, filtered or
# altered, and there is no way to tell from the bytes.
ALLOWED_HOSTS = ('www.gov.uk', 'gov.uk', 'assets.publishing.service.gov.uk')

FRESH_HOURS = 24
# Well below the real register (roughly a hundred thousand organisation/route rows)
# and far above any truncated download or error page. A single exact count is never
# asserted, because the register legitimately changes every week.
MIN_ROWS = 1000
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

# Fallback completeness bound, used only when GOV.UK publishes no attachment size.
# A register that kept less than this fraction of the previous snapshot's rows or
# bytes was truncated: a week of deregistrations does not remove most of it. It is
# deliberately loose, because the register genuinely changes every week.
MIN_RETAINED_FRACTION = 0.5

# Column names the official CSV has used for the organisation. Matching is
# case-insensitive and punctuation-tolerant so a header tweak does not break
# discovery, but an unrecognised header is reported rather than guessed at.
ORGANISATION_COLUMNS = ('organisation name', 'organisation', 'organisation_name',
                        'sponsor name', 'name')
TOWN_COLUMNS = ('town/city', 'town city', 'town', 'city')
COUNTY_COLUMNS = ('county',)
RATING_COLUMNS = ('type & rating', 'type and rating', 'type rating', 'rating',
                  'type & rating ')
ROUTE_COLUMNS = ('route', 'routes', 'sub tier', 'tier & rating', 'category')

STATUSES = ('FOUND', 'NOT_FOUND', 'AMBIGUOUS', 'UNAVAILABLE')
MATCH_QUALITIES = ('exact', 'legal_suffix', 'alias', 'sponsor_register_name')

# The wording that must survive every refactor. A miss here is a miss in one dated
# snapshot under the names we knew, and saying anything stronger would be false.
NOT_FOUND_MEANING = (
    'No credible match was found in this official register snapshot under the known '
    'legal-entity identities. This does NOT mean the employer cannot sponsor: '
    'registered legal names routinely differ from trading names, and the snapshot '
    'has a date. Record the registered legal name on the employer entity, or verify '
    'live, before treating this as a negative.'
)
LICENCE_CAVEAT = (
    'A current register entry is employer LICENCE evidence only. It is not evidence '
    'that this vacancy will be sponsored, that the role meets the going rate or '
    'skill level, or that the licence is still valid today.'
)


def register_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def parse_iso(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def age_hours(value, reference=None):
    stamp = parse_iso(value)
    if stamp is None:
        return None
    reference = reference or datetime.now().astimezone()
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return max(0.0, round((reference - stamp).total_seconds() / 3600.0, 3))


def _as_int(value):
    """A non-negative integer from optional upstream metadata, or None."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def completeness_problems(raw, expected_bytes=None, previous_meta=None,
                          min_retained_fraction=MIN_RETAINED_FRACTION):
    """Whether a download is COMPLETE, as distinct from merely well formed.

    A truncated download is the failure mode a schema check cannot see: the first
    40,000 rows of the register parse perfectly, carry the right columns and look
    entirely healthy. Only size tells you the rest is missing.

    So completeness is checked in two ways, strongest first:

      expected_bytes   GOV.UK publishes each attachment's byte size. When it is
                       available this is exact: any difference in either direction
                       means the bytes on disk are not the bytes that were
                       published, so the download is refused rather than installed.
      previous snapshot When the size is absent, an existing validated snapshot is
                       the next best reference. A register that suddenly lost most
                       of its rows or most of its bytes is a truncation, not a week
                       of deregistrations. An ordinary modest change passes.

    Neither depends on a hard-coded register size, because the official register
    legitimately changes every week. MIN_ROWS stays as defence in depth for the
    case where there is no size and no previous snapshot at all.
    """
    problems = []
    actual = len(raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode('utf-8'))
    expected = _as_int(expected_bytes)

    if expected:
        if actual != expected:
            problems.append({
                'problem': 'size_mismatch',
                'expected_bytes': expected,
                'actual_bytes': actual,
                'difference': actual - expected,
                'detail': ('The download does not match the byte size GOV.UK published '
                           'for this attachment. A short read parses perfectly and looks '
                           'healthy, so size is the only check that can see it.'),
            })
        return problems

    # No published size. Fall back on the previous validated snapshot rather than
    # failing outright, because this metadata is optional upstream.
    previous_rows = _as_int((previous_meta or {}).get('row_count'))
    previous_bytes = _as_int((previous_meta or {}).get('file_bytes'))
    if not previous_rows and not previous_bytes:
        return problems

    try:
        rows, _, _ = parse_register(raw)
        row_count = len(rows)
    except SystemExit:
        return problems

    if previous_rows and row_count < previous_rows * min_retained_fraction:
        problems.append({
            'problem': 'row_count_collapse',
            'rows': row_count,
            'previous_rows': previous_rows,
            'retained_fraction': round(row_count / previous_rows, 4),
            'minimum_retained_fraction': min_retained_fraction,
            'detail': ('The download holds far fewer rows than the snapshot it would '
                       'replace. A week of deregistrations does not remove most of the '
                       'register, so this is a truncated or filtered file.'),
        })
    if previous_bytes and actual < previous_bytes * min_retained_fraction:
        problems.append({
            'problem': 'file_size_collapse',
            'actual_bytes': actual,
            'previous_bytes': previous_bytes,
            'retained_fraction': round(actual / previous_bytes, 4),
            'minimum_retained_fraction': min_retained_fraction,
            'detail': 'The download is far smaller than the snapshot it would replace.',
        })
    return problems


def host_of(url):
    match = re.match(r'^https://([^/]+)', str(url or '').strip(), re.I)
    return match.group(1).lower() if match else ''


def assert_official(url):
    """Refuse any URL that is not an official GOV.UK host."""
    host = host_of(url)
    if not host:
        raise register_error(f'Refusing a non-HTTPS or malformed register URL: {url!r}')
    if host not in ALLOWED_HOSTS:
        raise register_error(
            f'Refusing a non-official register source: {host}',
            f'Only official GOV.UK hosts are trusted: {", ".join(ALLOWED_HOSTS)}.',
            'A third-party mirror may be stale, filtered or altered, and the bytes '
            'cannot show which.',
        )
    return url


# --------------------------------------------------------------------------
# Fetch and discover (network). Never exercised by the test suite.
# --------------------------------------------------------------------------

def default_fetch(url, timeout=60):
    """Fetch one official GOV.UK URL. The only function here that uses the network."""
    assert_official(url)
    request = urllib.request.Request(url, headers={
        'User-Agent': 'uk-job-discovery-workspace/1.0 (private personal job search)',
        'Accept': '*/*',
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_DOWNLOAD_BYTES + 1)


def discover_official_csv(fetch=None):
    """Find the CURRENT register CSV from the GOV.UK publication.

    The publication's content API lists its attachments, so the current CSV is read
    from the publication itself. Hard-coding one dated attachment URL would keep
    serving whichever register was current on the day the code was written.
    """
    fetch = fetch or default_fetch
    raw = fetch(CONTENT_API_URL)
    try:
        document = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise register_error('The GOV.UK publication did not return readable JSON.',
                             f'{type(exc).__name__}: {exc}',
                             f'Publication: {PUBLICATION_URL}') from None

    attachments = []
    details = document.get('details') or {}
    for block in (details.get('attachments') or []):
        if isinstance(block, dict):
            attachments.append(block)
    for edition in (document.get('links') or {}).get('documents', []) or []:
        for block in ((edition.get('details') or {}).get('attachments') or []):
            if isinstance(block, dict):
                attachments.append(block)

    candidates = []
    for block in attachments:
        url = str(block.get('url') or '').strip()
        if not url or host_of(url) not in ALLOWED_HOSTS:
            continue
        content_type = str(block.get('content_type') or '').lower()
        title = str(block.get('title') or '')
        if url.lower().endswith('.csv') or 'csv' in content_type:
            candidates.append({
                'url': url,
                'title': title,
                'content_type': content_type,
                'filename': str(block.get('filename') or '').strip() or url.rsplit('/', 1)[-1],
                # GOV.UK publishes the attachment's byte size. It is the only exact
                # completeness check available, so it is preserved verbatim rather
                # than left to a row-count heuristic.
                'file_size': _as_int(block.get('file_size')),
                'updated_at': str(block.get('updated_at') or ''),
            })

    if not candidates:
        raise register_error(
            'No CSV attachment was found on the GOV.UK register publication.',
            f'Publication: {PUBLICATION_URL}',
            'Download the current CSV manually and install it offline with: '
            'python tools/sponsor_register.py refresh --from-file <path>',
        )

    # Prefer an attachment whose title names the worker register, so a supplementary
    # CSV published alongside it is not installed as the register itself.
    def rank(entry):
        title = entry['title'].lower()
        return (0 if 'worker' in title else 1, 0 if 'register' in title else 1, entry['url'])

    chosen = sorted(candidates, key=rank)[0]
    return {
        'source_page': PUBLICATION_URL,
        'source_csv': chosen['url'],
        'attachment_title': chosen['title'],
        'attachment_filename': chosen['filename'],
        'attachment_content_type': chosen['content_type'],
        'expected_bytes': chosen['file_size'],
        'official_updated_at': (chosen['updated_at']
                                or str(document.get('public_updated_at') or '')),
        'attachments_seen': len(candidates),
    }


def download_snapshot(fetch=None):
    """Discover and download the current official CSV. Returns `(raw_bytes, meta)`."""
    fetch = fetch or default_fetch
    discovered = discover_official_csv(fetch=fetch)
    raw = fetch(assert_official(discovered['source_csv']))
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    if len(raw) > MAX_DOWNLOAD_BYTES:
        raise register_error('The downloaded register exceeded the size ceiling.',
                             f'Ceiling: {MAX_DOWNLOAD_BYTES} bytes.')
    discovered = dict(discovered)
    discovered['downloaded_bytes'] = len(raw)
    return raw, discovered


# --------------------------------------------------------------------------
# Parse, validate and install (no network)
# --------------------------------------------------------------------------

def _column(fieldnames, wanted):
    """Find a column by tolerant name match, or '' when the file does not have it."""
    lookup = {re.sub(r'[^a-z0-9 &/]+', '', (name or '').strip().lower()): name
              for name in (fieldnames or [])}
    for candidate in wanted:
        key = re.sub(r'[^a-z0-9 &/]+', '', candidate)
        if key in lookup:
            return lookup[key]
    return ''


def looks_like_html(raw):
    """Whether a payload is a web page rather than a CSV.

    GOV.UK outages, redirects and rate limits all return HTML with a 200, so a
    downloader that trusts the extension installs an error page as the register.
    """
    head = (raw[:2048].decode('utf-8', 'ignore') if isinstance(raw, bytes) else str(raw)[:2048])
    lowered = head.lstrip().lower()
    return (lowered.startswith('<!doctype html') or lowered.startswith('<html')
            or '<head>' in lowered or '<body' in lowered or '<title>' in lowered)


def parse_register(raw):
    """Parse register CSV bytes into `(rows, columns, organisation_column)`."""
    text = raw.decode('utf-8-sig', 'replace') if isinstance(raw, bytes) else str(raw)
    reader = csv.DictReader(io.StringIO(text))
    try:
        rows = [row for row in reader]
    except csv.Error as exc:
        raise register_error('The register CSV could not be parsed.',
                             f'csv error: {exc}') from None
    return rows, list(reader.fieldnames or []), _column(reader.fieldnames, ORGANISATION_COLUMNS)


def validation_problems(raw, min_rows=MIN_ROWS):
    """Every reason a downloaded payload must not replace the current snapshot."""
    problems = []
    # Decode before testing for emptiness: `str(b'  ')` is the repr `"b'  '"`, which
    # is not blank, so a whitespace-only download would otherwise slip past this and
    # be reported as a stranger problem further down.
    body = (raw.decode('utf-8-sig', 'replace') if isinstance(raw, (bytes, bytearray))
            else str(raw or ''))
    if not body.strip():
        return [{'problem': 'empty_download'}]
    if looks_like_html(raw):
        return [{'problem': 'html_not_csv',
                 'detail': 'The payload is a web page, not a CSV. An error or '
                           'interstitial page must never be installed as the register.'}]
    try:
        rows, columns, organisation = parse_register(raw)
    except SystemExit as exc:
        return [{'problem': 'unparseable_csv', 'detail': str(exc)}]
    if not columns:
        return [{'problem': 'no_csv_header'}]
    if not organisation:
        problems.append({'problem': 'organisation_column_missing', 'columns': columns,
                         'detail': 'A register without an organisation-name column '
                                   'cannot answer the only question it exists for.'})
    if len(rows) < min_rows:
        problems.append({'problem': 'implausible_row_count', 'rows': len(rows),
                         'minimum': min_rows,
                         'detail': 'Far too few rows to be the official register. A '
                                   'truncated download or an error payload looks like '
                                   'this. No exact count is asserted, because the '
                                   'register legitimately changes every week.'})
    if organisation:
        named = sum(1 for row in rows if str(row.get(organisation) or '').strip())
        if named < max(1, int(len(rows) * 0.5)):
            problems.append({'problem': 'organisation_column_mostly_empty',
                             'named_rows': named, 'rows': len(rows)})
    return problems


def sha256_of(raw):
    return hashlib.sha256(raw if isinstance(raw, (bytes, bytearray))
                          else str(raw).encode('utf-8')).hexdigest()


def atomic_write_bytes(path, data):
    """Write bytes verbatim via a same-directory temp file, fsync, then replace.

    The snapshot is stored EXACTLY as GOV.UK published it, rather than through the
    workspace's text writer. Text mode applies platform newline translation, and the
    real register carries carriage returns inside quoted fields: those survive a
    translated write but are normalised on read, so a digest taken over decoded text
    would never match the file again. Storing the published bytes keeps the digest an
    attestation of what was actually downloaded, and keeps `expected_bytes` equal to
    the size on disk.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f'.{path.name}.',
                                           suffix='.tmp')
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def install_snapshot(raw, source_page=PUBLICATION_URL, source_csv='',
                     official_updated_at='', min_rows=MIN_ROWS, snapshot_path=None,
                     meta_path=None, downloaded_at='', expected_bytes=None,
                     attachment_filename='', attachment_content_type='',
                     min_retained_fraction=MIN_RETAINED_FRACTION):
    """Validate a payload and atomically install it as the current snapshot.

    Nothing touches the existing snapshot until BOTH validations pass, so a bad
    download can never destroy a good last-known-good copy:

      validation_problems     is it a well-formed register at all?
      completeness_problems   is it the WHOLE file that was published?

    Those are different questions. A download truncated to the first 40,000 rows
    passes every schema check and looks entirely healthy, so only the published
    byte size, or a comparison against the snapshot it would replace, can see it.

    This function performs no network access at all, which is what makes every rule
    here testable offline.
    """
    snapshot_path = Path(snapshot_path) if snapshot_path else SNAPSHOT
    meta_path = Path(meta_path) if meta_path else META
    problems = validation_problems(raw, min_rows=min_rows)
    if problems:
        raise register_error(
            'Refusing to install an invalid sponsor-register snapshot.',
            f'Problems: {json.dumps(problems, ensure_ascii=False)}',
            'The previous validated snapshot, if any, is untouched.',
        )
    incomplete = completeness_problems(
        raw, expected_bytes=expected_bytes, previous_meta=load_meta(meta_path),
        min_retained_fraction=min_retained_fraction)
    if incomplete:
        raise register_error(
            'Refusing to install an incomplete sponsor-register snapshot.',
            f'Problems: {json.dumps(incomplete, ensure_ascii=False)}',
            'A truncated register parses cleanly and looks healthy, so an incomplete '
            'download must be refused on size rather than on structure.',
            'The previous validated snapshot, if any, is untouched.',
        )

    payload = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode('utf-8')
    rows, columns, organisation = parse_register(payload)
    digest = sha256_of(payload)
    meta = {
        'schema_version': SCHEMA_VERSION,
        'source_page': source_page,
        'source_csv': source_csv,
        'attachment_filename': attachment_filename,
        'attachment_content_type': attachment_content_type,
        'downloaded_at': downloaded_at or now_iso(),
        'official_updated_at': official_updated_at,
        'sha256': digest,
        # Both sizes are recorded: the bytes as published (which the size check
        # compares against) and the bytes on disk after decoding. They differ when
        # the file carries a BOM or non-UTF-8 bytes.
        'expected_bytes': _as_int(expected_bytes),
        'downloaded_bytes': len(payload),
        'file_bytes': len(payload),
        'size_verified': bool(_as_int(expected_bytes)),
        'row_count': len(rows),
        'columns': columns,
        'organisation_column': organisation,
        'town_column': _column(columns, TOWN_COLUMNS),
        'county_column': _column(columns, COUNTY_COLUMNS),
        'rating_column': _column(columns, RATING_COLUMNS),
        'route_column': _column(columns, ROUTE_COLUMNS),
        'organisation_count': len({norm_name(r.get(organisation, '')) for r in rows
                                   if str(r.get(organisation) or '').strip()}),
        'fresh_hours': FRESH_HOURS,
        'note': LICENCE_CAVEAT,
    }
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(snapshot_path, payload)
    atomic_write_text(meta_path, json.dumps(meta, indent=2, ensure_ascii=False) + '\n')
    return meta


def load_meta(meta_path=None):
    meta_path = Path(meta_path) if meta_path else META
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def status(snapshot_path=None, meta_path=None, fresh_hours=FRESH_HOURS, on=''):
    """Local-only snapshot health. Never touches the network.

    `refresh_needed` is what a discovery run reads at startup: it is true when the
    snapshot is missing or older than the freshness target, and a run acts on it at
    most once.
    """
    snapshot_path = Path(snapshot_path) if snapshot_path else SNAPSHOT
    meta_path = Path(meta_path) if meta_path else META
    meta = load_meta(meta_path)
    if not snapshot_path.exists() or meta is None:
        return {
            'available': False, 'fresh': False, 'stale': False, 'refresh_needed': True,
            'snapshot': snapshot_path.relative_to(ROOT).as_posix()
            if snapshot_path.is_relative_to(ROOT) else str(snapshot_path),
            'stale_reason': 'missing_snapshot' if not snapshot_path.exists() else 'missing_metadata',
            'note': 'No validated official register snapshot. Employer licence lookups '
                    'are UNAVAILABLE locally, which is not the same as an employer '
                    'being absent from the register.',
        }

    reference = parse_iso(on) if on else None
    hours = age_hours(meta.get('downloaded_at'), reference)
    integrity_ok = True
    try:
        integrity_ok = sha256_of(snapshot_path.read_bytes()) == meta.get('sha256')
    except OSError:
        integrity_ok = False
    fresh = bool(integrity_ok and hours is not None and hours <= fresh_hours)
    stale_reason = ''
    if not integrity_ok:
        stale_reason = 'sha256_mismatch'
    elif hours is None:
        stale_reason = 'unreadable_download_time'
    elif hours > fresh_hours:
        stale_reason = 'older_than_freshness_target'
    return {
        'available': True,
        'fresh': fresh,
        'stale': not fresh,
        'refresh_needed': not fresh,
        'integrity_ok': integrity_ok,
        'age_hours': hours,
        'fresh_hours': fresh_hours,
        'downloaded_at': meta.get('downloaded_at', ''),
        'official_updated_at': meta.get('official_updated_at', ''),
        'source_page': meta.get('source_page', ''),
        'source_csv': meta.get('source_csv', ''),
        'sha256': meta.get('sha256', ''),
        'row_count': meta.get('row_count', 0),
        'organisation_count': meta.get('organisation_count', 0),
        'columns': meta.get('columns', []),
        'stale_reason': stale_reason,
        'note': LICENCE_CAVEAT,
    }


def refresh(fetch=None, from_file='', min_rows=MIN_ROWS, snapshot_path=None, meta_path=None,
            min_retained_fraction=MIN_RETAINED_FRACTION):
    """Attempt one refresh, never destroying a good snapshot on failure.

    A GOV.UK outage must not break a discovery run. When the refresh fails and a
    validated snapshot already exists, that snapshot is retained and marked stale so
    the run continues with an honest warning. When it fails and no snapshot exists,
    local lookups are UNAVAILABLE, which is emphatically not the same answer as an
    employer being absent from the register.
    """
    snapshot_path = Path(snapshot_path) if snapshot_path else SNAPSHOT
    meta_path = Path(meta_path) if meta_path else META
    previous = status(snapshot_path, meta_path)
    try:
        if from_file:
            path = Path(from_file)
            if not path.exists():
                raise register_error(f'Register file not found: {path}')
            raw = path.read_bytes()
            # A manually downloaded file carries no published size, so completeness
            # falls back to comparison against the snapshot it would replace.
            discovered = {'source_page': PUBLICATION_URL,
                          'source_csv': f'file://{path.as_posix()}',
                          'attachment_filename': path.name,
                          'official_updated_at': '', 'expected_bytes': None}
        else:
            raw, discovered = download_snapshot(fetch=fetch)
        meta = install_snapshot(
            raw, source_page=discovered.get('source_page', PUBLICATION_URL),
            source_csv=discovered.get('source_csv', ''),
            official_updated_at=discovered.get('official_updated_at', ''),
            expected_bytes=discovered.get('expected_bytes'),
            attachment_filename=discovered.get('attachment_filename', ''),
            attachment_content_type=discovered.get('attachment_content_type', ''),
            min_rows=min_rows, snapshot_path=snapshot_path, meta_path=meta_path,
            min_retained_fraction=min_retained_fraction)
        return {'refreshed': True, 'installed': True, 'meta': meta,
                'retained_previous': False,
                'discovered': discovered,
                'status': status(snapshot_path, meta_path)}
    except (SystemExit, urllib.error.URLError, urllib.error.HTTPError, OSError,
            TimeoutError, ValueError) as exc:
        detail = str(exc)
        if previous.get('available'):
            return {
                'refreshed': False, 'installed': False, 'retained_previous': True,
                'error': detail,
                'status': status(snapshot_path, meta_path),
                'note': 'The refresh failed, so the previous validated snapshot was '
                        'retained and is treated as stale. Discovery continues with a '
                        'warning rather than failing.',
            }
        return {
            'refreshed': False, 'installed': False, 'retained_previous': False,
            'error': detail,
            'status': status(snapshot_path, meta_path),
            'note': 'The refresh failed and there is no validated snapshot, so official '
                    'local licence lookups are UNAVAILABLE. That is not evidence that '
                    'any employer is absent from the register: verify live instead.',
        }


# --------------------------------------------------------------------------
# Local lookup
# --------------------------------------------------------------------------

def _rows(snapshot_path=None, meta_path=None):
    snapshot_path = Path(snapshot_path) if snapshot_path else SNAPSHOT
    meta = load_meta(meta_path)
    if not snapshot_path.exists() or meta is None:
        return [], meta
    try:
        rows, _, _ = parse_register(snapshot_path.read_bytes())
    except (OSError, SystemExit):
        return [], meta
    return rows, meta


def _row_view(row, meta):
    """One register row projected onto the columns the official file actually has.

    Nothing here invents a field. A column the download did not contain simply does
    not appear, rather than being filled with an empty promise.
    """
    view = {'organisation': str(row.get(meta.get('organisation_column', ''), '') or '').strip()}
    for key, column in (('town', 'town_column'), ('county', 'county_column'),
                        ('rating', 'rating_column'), ('route', 'route_column')):
        name = meta.get(column, '')
        value = str(row.get(name, '') or '').strip() if name else ''
        if value:
            view[key] = value
    return view


def _identity_candidates(name, employer_store=None):
    """The legal-entity names worth looking up, strongest evidence first.

    A recorded `sponsor_register_name` is the strongest: somebody previously
    confirmed the exact organisation the register lists this employer under, which
    is frequently not its trading name.
    """
    tried = []
    seen = set()

    def add(value, quality):
        token = norm_name(value)
        if value and token and token not in seen:
            seen.add(token)
            tried.append({'name': str(value).strip(), 'quality': quality})

    resolved = None
    try:
        store = employer_store if employer_store is not None else load_employer_store()
        resolved = resolve_employer(name, store=store)
    except SystemExit:
        resolved = None
    if resolved and resolved.get('resolved'):
        entity = resolved.get('entity') or {}
        add(entity.get('sponsor_register_name'), 'sponsor_register_name')
        add(entity.get('canonical_name'), 'exact')
        for alias in entity.get('aliases', []) or []:
            add(alias, 'alias')
    add(name, 'exact')
    return tried, resolved


def search(name, snapshot_path=None, meta_path=None, employer_store=None, on=''):
    """Look one employer up in the local official snapshot.

    The ladder is exact normalised legal name, then legal-suffix normalisation, then
    an explicitly recorded employer alias or registered name. Short-name substring
    matching is absent by construction: `Sky` must never match `Kaspersky` and `One`
    must never match `AXONE`, and the only way to guarantee that is to never compare
    substrings at all.
    """
    snap = status(snapshot_path, meta_path, on=on)
    if not snap.get('available'):
        return {'status': 'UNAVAILABLE', 'query': name, 'snapshot': snap,
                'requires_live_check': True,
                'reason': 'No validated official register snapshot is installed.',
                'note': snap.get('note', '')}

    rows, meta = _rows(snapshot_path, meta_path)
    organisation_column = (meta or {}).get('organisation_column', '')
    if not rows or not organisation_column:
        return {'status': 'UNAVAILABLE', 'query': name, 'snapshot': snap,
                'requires_live_check': True,
                'reason': 'The installed snapshot could not be read.',
                'note': snap.get('note', '')}

    identities, resolved = _identity_candidates(name, employer_store)
    exact_index, bare_index = {}, {}
    for row in rows:
        organisation = str(row.get(organisation_column, '') or '').strip()
        if not organisation:
            continue
        exact_index.setdefault(norm_name(organisation), []).append(row)
        bare_index.setdefault(without_legal_suffix(norm_name(organisation)), []).append(row)

    for identity in identities:
        wanted, declared = identity['name'], identity['quality']
        for quality, index, key in (
                (declared if declared != 'exact' else 'exact', exact_index, norm_name(wanted)),
                (declared if declared != 'exact' else 'legal_suffix', bare_index,
                 without_legal_suffix(norm_name(wanted)))):
            if not key:
                continue
            matched = index.get(key) or []
            if not matched:
                continue
            organisations = sorted({str(r.get(organisation_column, '')).strip() for r in matched})
            distinct = {norm_name(o) for o in organisations}
            if len(distinct) > 1:
                # Two genuinely different registered organisations collapse to the
                # same normalised name. Guessing between them would attach one
                # employer's licence to another.
                return {
                    'status': 'AMBIGUOUS', 'query': name, 'matched_via': wanted,
                    'match_quality': quality, 'organisations': organisations,
                    'snapshot': snap, 'requires_live_check': True,
                    'reason': f'{len(distinct)} distinct registered organisations match '
                              f'{wanted!r} at {quality} quality. Record the exact '
                              'registered legal name on the employer entity, or verify '
                              'live, rather than guessing between them.',
                    'note': LICENCE_CAVEAT,
                }
            views = [_row_view(r, meta) for r in matched]
            routes = sorted({v['route'] for v in views if v.get('route')})
            ratings = sorted({v['rating'] for v in views if v.get('rating')})
            skilled = [r for r in routes if 'skilled worker' in r.lower()]
            return {
                'status': 'FOUND',
                'query': name,
                'matched_via': wanted,
                'organisation': organisations[0],
                'match_quality': quality,
                'rows': views,
                'routes': routes,
                'ratings': ratings,
                'rating': ratings[0] if len(ratings) == 1 else '',
                'has_skilled_worker_route': bool(skilled),
                'skilled_worker_routes': skilled,
                'route_note': (
                    'Routes are reported exactly as the official file states them. A '
                    'licence for an unrelated route is NOT the sponsorship evidence a '
                    'Skilled Worker vacancy needs.'
                    if routes else
                    'This snapshot did not carry a route column, so no route-level '
                    'claim can be made from it.'),
                'employer_resolved': bool(resolved and resolved.get('resolved')),
                'snapshot': snap,
                'snapshot_fresh': snap.get('fresh', False),
                # A licence is an organisation fact, so a decision-critical use always
                # needs live confirmation, and a stale snapshot needs it doubly.
                'requires_live_check': True,
                'note': LICENCE_CAVEAT,
            }

    return {
        'status': 'NOT_FOUND',
        'query': name,
        'identities_tried': [i['name'] for i in identities],
        'employer_resolved': bool(resolved and resolved.get('resolved')),
        'snapshot': snap,
        'snapshot_fresh': snap.get('fresh', False),
        'requires_live_check': True,
        'meaning': NOT_FOUND_MEANING,
        'note': NOT_FOUND_MEANING,
    }


def evidence_payload(result):
    """Turn a FOUND lookup into the fields the sponsorship evidence cache stores.

    Deliberately returns licence-level facts only. There is no `sponsors_vacancy`
    field to set, here or anywhere, because a register entry cannot support one.
    """
    if not isinstance(result, dict) or result.get('status') != 'FOUND':
        return None
    snapshot = result.get('snapshot', {}) or {}
    downloaded = str(snapshot.get('downloaded_at') or '')
    return {
        'kind': 'sponsor_register',
        'source': 'gov.uk',
        'url': snapshot.get('source_page', PUBLICATION_URL),
        'organisation': result.get('organisation', ''),
        'observed_at': downloaded,
        'register_extract_date': (snapshot.get('official_updated_at') or downloaded)[:10],
        'snapshot_sha256': snapshot.get('sha256', ''),
        'rating': result.get('rating', ''),
        'routes': ', '.join(result.get('routes', [])),
        'match_quality': result.get('match_quality', ''),
        'detail': (f"Official GOV.UK register snapshot lists {result.get('organisation','')}"
                   f"{' (' + result.get('rating','') + ')' if result.get('rating') else ''}."
                   f" {LICENCE_CAVEAT}"),
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_status(args):
    report = status(on=args.on)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(0 if report.get('available') else 1)


def cmd_refresh(args):
    if not args.from_file and not args.allow_network:
        raise register_error(
            'A network refresh must be requested explicitly.',
            'Use --allow-network to download from GOV.UK, or --from-file <path> to '
            'install a CSV you already downloaded.',
            'This keeps an accidental network call out of routine tooling.',
        )
    result = refresh(from_file=args.from_file, min_rows=args.min_rows)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result.get('installed') else 1)


def cmd_search(args):
    result = search(args.name, on=args.on)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result['status'] == 'FOUND' else 1)


def cmd_check(args):
    result = search(args.name, on=args.on)
    compact = {
        'query': args.name,
        'status': result['status'],
        'organisation': result.get('organisation', ''),
        'match_quality': result.get('match_quality', ''),
        'rating': result.get('rating', ''),
        'routes': result.get('routes', []),
        'has_skilled_worker_route': result.get('has_skilled_worker_route', False),
        'snapshot_fresh': result.get('snapshot_fresh', False),
        'snapshot_downloaded_at': (result.get('snapshot', {}) or {}).get('downloaded_at', ''),
        'requires_live_check': result.get('requires_live_check', True),
        'meaning': result.get('meaning', result.get('reason', '')),
        'note': result.get('note', ''),
    }
    if result['status'] == 'FOUND':
        compact['evidence_payload'] = evidence_payload(result)
    print(json.dumps(compact, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result['status'] == 'FOUND' else 1)


def cmd_validate(args):
    raw = Path(args.file).read_bytes()
    problems = validation_problems(raw, min_rows=args.min_rows)
    incomplete = completeness_problems(raw, expected_bytes=args.expected_bytes,
                                       previous_meta=load_meta())
    print(json.dumps({'file': args.file, 'bytes': len(raw),
                      'valid': not problems and not incomplete,
                      'structure_problems': problems,
                      'completeness_problems': incomplete}, indent=2, ensure_ascii=False))
    raise SystemExit(0 if not problems and not incomplete else 1)


def main():
    p = argparse.ArgumentParser(description='Official GOV.UK sponsor register snapshot')
    sub = p.add_subparsers(dest='cmd', required=True)

    st = sub.add_parser('status', help='Local snapshot health. No network.')
    st.add_argument('--on', default='', help='Evaluate freshness as of this timestamp.')
    st.set_defaults(func=cmd_status)

    rf = sub.add_parser('refresh', help='Install a new snapshot from GOV.UK or a local file.')
    rf.add_argument('--from-file', dest='from_file', default='',
                    help='Install an already-downloaded official CSV, without network.')
    rf.add_argument('--allow-network', dest='allow_network', action='store_true',
                    help='Permit the GOV.UK download. Required for a network refresh.')
    rf.add_argument('--min-rows', dest='min_rows', type=int, default=MIN_ROWS)
    rf.set_defaults(func=cmd_refresh)

    se = sub.add_parser('search', help='Full lookup result for one employer.')
    se.add_argument('name')
    se.add_argument('--on', default='')
    se.set_defaults(func=cmd_search)

    ck = sub.add_parser('check', help='Compact lookup plus the evidence payload.')
    ck.add_argument('name')
    ck.add_argument('--on', default='')
    ck.set_defaults(func=cmd_check)

    va = sub.add_parser('validate', help='Validate a CSV without installing it.')
    va.add_argument('--file', required=True)
    va.add_argument('--min-rows', dest='min_rows', type=int, default=MIN_ROWS)
    va.add_argument('--expected-bytes', dest='expected_bytes', type=int,
                    help='The byte size GOV.UK published for this attachment.')
    va.set_defaults(func=cmd_validate)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

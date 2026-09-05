#!/usr/bin/env python3
"""Immutable shortlist snapshots for ranked job-discovery runs."""
import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import atomic_write_text, parse_state  # noqa: E402


def config_fingerprints():
    """sha256 of every configuration in force, or {} when the policy is unavailable.

    Imported lazily so a workspace without a matching policy can still write and
    read shortlists: reproducibility metadata is additive, and its absence must
    never stop a snapshot being saved.
    """
    try:
        from match_evaluation import config_fingerprints as fingerprints
        return fingerprints()
    except (ImportError, SystemExit, OSError):
        return {}


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'job_scraper' / 'seen_jobs.json'
SNAPSHOT_DIR = ROOT / 'job_scraper' / 'shortlists'
SCHEMA_VERSION = 1

# Score bands, matching .claude/skills/job-matcher/job-screening.md exactly.
# Exceptional is represented separately from Strong so a 90+ role is never
# reported under an 80-89 heading.
# `borderline` is the 65 to 69 pilot review band. It sits between `viable` and
# `below` so a role in the range this workspace is least sure about stays in
# front of the human instead of disappearing under a single hard line at 70.
BANDS = ('exceptional', 'strong', 'viable', 'borderline', 'verification', 'agency',
         'below', 'other')


def now_local():
    return datetime.now().astimezone()


def load_state():
    if not STATE.exists():
        raise SystemExit(
            f'Missing discovery state: {STATE}\n'
            '  Run: python tools/job_state.py doctor')
    try:
        raw = STATE.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(
            f'Discovery state could not be read: {STATE}\n'
            f'  {type(exc).__name__}: {exc}\n'
            '  Run: python tools/job_state.py doctor') from None
    return parse_state(raw, STATE)


def safe_run_id(value):
    value = (value or '').strip()
    if not value:
        raise SystemExit('run_id must not be empty')
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '-', value).strip('-')
    if not cleaned:
        raise SystemExit('run_id contains no usable characters')
    return cleaned[:120]


def make_run_id(prefix='rank'):
    stamp = now_local().strftime('%Y%m%dT%H%M%S%f')
    return f'{prefix}-{stamp}'


def snapshot_files():
    if not SNAPSHOT_DIR.exists():
        return []
    return sorted(SNAPSHOT_DIR.glob('*.json'))


def load_snapshot(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise ValueError(f'Invalid shortlist snapshot: {path}')
    data['_path'] = path
    return data


def readable_snapshots():
    """Every snapshot that parses, plus a report of the ones that do not.

    One damaged file must never take the history down with it. A truncated or
    half-written snapshot used to raise JSONDecodeError straight out of every
    retrieval mode, so a single bad byte made `/shortlist`, `/shortlist all` and
    `/shortlist <date>` all fail with a raw traceback and the healthy snapshots
    became unreachable.

    A corrupt file is ISOLATED, never repaired, deleted, or quietly treated as
    valid: it is reported so the damage stays visible, and the readable history
    is returned around it.
    """
    good, damaged = [], []
    for path in snapshot_files():
        try:
            good.append(load_snapshot(path))
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            damaged.append({'path': str(path.relative_to(ROOT)).replace('\\', '/'),
                            'error': f'{type(exc).__name__}: {exc}'})
    return good, damaged


def damage_notice(damaged):
    """A visible, actionable warning block for unreadable snapshots."""
    if not damaged:
        return []
    lines = ['', f'> WARNING: {len(damaged)} shortlist snapshot(s) could not be read and '
                 'were skipped.']
    for row in damaged:
        lines.append(f">   {row['path']}: {row['error']}")
    lines.append('> The readable history above is complete apart from those files. They '
                 'have been left exactly as they are; nothing was repaired or deleted.')
    return lines


def snapshot_sort_key(data):
    raw = data.get('created_at', '')
    try:
        stamp = datetime.fromisoformat(raw).timestamp()
    except (TypeError, ValueError):
        stamp = 0.0
    return (stamp, data.get('run_id', ''))


def sanitised_item(key, item):
    fields = (
        'title', 'company', 'url', 'location', 'posted', 'last_verified', 'quick_fit',
        'fit_band', 'lead_type', 'sponsorship', 'sponsorship_label', 'source',
        'source_type', 'source_confidence', 'source_host', 'job_id', 'requisition_id',
        'filter_reason', 'first_seen', 'last_seen', 'status', 'rank_score',
        'rank_verdict', 'rank_date', 'ranked_at', 'rank_run_id',
        # The machine record of the ranking, when one was stored. A snapshot that
        # carried only the integer and the prose could not be re-audited: nothing in
        # it said whether a role was blocked, or that its total was the sum of its
        # components. Absent on rankings recorded before the field existed, and never
        # backfilled, because an absent evaluation is a knowable unknown.
        'evaluation',
        # Consolidation deliberately never MERGES two records on a resemblance:
        # one employer runs several different vacancies under one title in one
        # city, so merging needs published identifier evidence. What it does
        # instead is HINT, and a hint that stops at the state file helps nobody.
        # Twenty-two of the two hundred stored records carry this, including one
        # Client Server vacancy sighted on CWJobs, LinkedIn and Indeed; without
        # it a shortlist can show the same role three times with no signal that
        # they are linked. Carrying the hint keeps the flag-do-not-merge rule
        # useful to the human it was written for.
        'possible_duplicate_keys',
    )
    out = {'state_key': key}
    for field in fields:
        if field in item:
            out[field] = item.get(field)
    return out


def category(item):
    """Score band for one ranked record.

    lead_type decides the category first. A Direct Match keeps its score band even
    when the human verdict says "Verify first"; that is a recommended action, not a
    Verification Lead. Verification Lead is reserved for a decision-critical
    external gate recorded as lead_type=verification.
    """
    lead = (item.get('lead_type') or '').strip().lower()
    if lead == 'verification':
        return 'verification'
    if lead == 'agency':
        return 'agency'
    if lead != 'direct':
        return 'other'
    try:
        score = int(item.get('rank_score'))
    except (TypeError, ValueError):
        return 'other'
    for band_id, floor in _policy_band_floors():
        if score >= floor:
            return band_id
    return 'below'


# The band thresholds have ONE home: config/matching_policy.json. They were
# hardcoded here as 90/80/70/65 and were silently left behind when the policy was
# recalibrated to 75/66/58/54 on 2026-09-03. The evaluations then used the new
# bands while the shortlist the human actually reads used the old ones, so every
# role was shown roughly a band and a half worse than it scored: a 74 that policy
# calls a Strong Match was rendered 'viable', and 53 roles inside the review range
# were rendered 'below'. Read the policy instead of restating it.
_BAND_ID_ALIASES = {'borderline_review': 'borderline', 'below_threshold': 'below'}


def _band_heading(band_id, label):
    """Heading text with the band's CURRENT range, read from policy.

    These read "(90+)" and "(80-89)" long after the policy moved to 75/66/58/54,
    so the shortlist told the reader a score range that no longer existed.
    """
    for row in _policy_band_rows():
        if _BAND_ID_ALIASES.get(row.get('id'), row.get('id')) != band_id:
            continue
        lo, hi = row.get('min_score'), row.get('max_score')
        if lo is None:
            break
        return f'{label} ({lo}+)' if hi is None or hi >= 100 else f'{label} ({lo}-{hi})'
    return label


def _policy_band_rows():
    path = ROOT / 'config/matching_policy.json'
    try:
        rows = json.loads(path.read_text(encoding='utf-8'))['direct_model']['bands']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(
            f'Band thresholds could not be read: {path}\n'
            f'  {type(exc).__name__}: {exc}\n'
            '  The shortlist reads its bands from the matching policy and will not\n'
            '  guess them: banding every role from a default would silently mis-rank\n'
            '  the whole shortlist. Run: python tools/match_evaluation.py validate-policy'
        ) from None
    if not rows:
        raise SystemExit(f'Band thresholds are empty in {path}')
    return rows


def _policy_band_floors():
    """(band_id, min_score) from the live policy, strongest first."""
    rows = _policy_band_rows()
    out = []
    for row in rows:
        bid = _BAND_ID_ALIASES.get(row.get('id'), row.get('id'))
        if bid in BANDS and isinstance(row.get('min_score'), int):
            out.append((bid, row['min_score']))
    out.sort(key=lambda r: -r[1])
    return tuple(out)


def counts_for(items):
    counts = {k: 0 for k in BANDS}
    for item in items:
        counts[category(item)] += 1
    counts['total'] = len(items)
    return counts


def counts_from_snapshot(snapshot):
    """Counts for one snapshot, recomputed when the stored counts predate a band.

    Historical snapshot JSON is immutable, so an old file that has no exceptional
    count is recounted from its own stored items at read time instead of rewritten.
    """
    stored = snapshot.get('counts')
    if isinstance(stored, dict) and all(band in stored for band in BANDS):
        return stored
    return counts_for(snapshot.get('items', []))


def run_scope_for(ranked, total_matching=0, limit=0):
    total = total_matching or ranked
    return {
        'total_matching': total,
        'ranked': ranked,
        'deferred': max(0, total - ranked),
        'limit': limit or 0,
        'partial': total > ranked,
    }


def write_snapshot(run_id, items, snapshot_date=None, created_at=None, legacy_import=False,
                   run_scope=None):
    run_id = safe_run_id(run_id)
    created = created_at or now_local().isoformat(timespec='seconds')
    snap_date = snapshot_date or created[:10]
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f'{snap_date}_{run_id}.json'
    if path.exists():
        existing = load_snapshot(path)
        return path, existing, False

    payload = {
        'schema_version': SCHEMA_VERSION,
        'run_id': run_id,
        'date': snap_date,
        'created_at': created,
        'legacy_import': bool(legacy_import),
        'source': 'job_scraper/seen_jobs.json',
        'run_scope': run_scope or run_scope_for(len(items)),
        'counts': counts_for(items),
        'items': items,
    }
    # Which calibration produced this shortlist. Without it, a historical ranking is
    # uninterpretable the moment a weight, a threshold or the register snapshot
    # changes: the scores stay on the page while the model behind them moves.
    # Additive and forward-only, so existing snapshots are never rewritten.
    fingerprints = config_fingerprints()
    if fingerprints:
        payload['config_fingerprints'] = fingerprints
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + '\n')
    return path, payload, True


def cmd_begin(args):
    now = now_local()
    print(json.dumps({
        'run_id': make_run_id('rank'),
        'date': now.date().isoformat(),
        'started_at': now.isoformat(timespec='seconds'),
    }, ensure_ascii=False))


def cmd_snapshot(args):
    data = load_state()
    raw_run_id = (args.run_id or '').strip()
    run_id = safe_run_id(raw_run_id)
    rows = []
    for key, item in data['seen'].items():
        if (item.get('rank_run_id') or '') != raw_run_id:
            continue
        if item.get('status') not in {'ranked', 'updated'}:
            continue
        rows.append(sanitised_item(key, item))
    if not rows:
        raise SystemExit(f'No ranked/updated jobs found for rank_run_id={run_id}')
    rows.sort(key=lambda x: (
        0 if x.get('lead_type') == 'direct' else 1,
        -(int(x.get('rank_score')) if isinstance(x.get('rank_score'), int) else -1),
        x.get('company', ''),
        x.get('title', ''),
    ))
    scope = run_scope_for(len(rows), args.total_matching, args.limit)
    path, payload, created = write_snapshot(run_id, rows, run_scope=scope)
    print(json.dumps({
        'created': created,
        'run_id': run_id,
        'snapshot': str(path.relative_to(ROOT)).replace('\\', '/'),
        'run_scope': payload.get('run_scope', scope),
        'counts': payload['counts'],
    }, ensure_ascii=False))


def cmd_bootstrap(args):
    existing = snapshot_files()
    if args.if_empty and existing:
        print(json.dumps({
            'created': 0,
            'skipped': True,
            'reason': 'shortlist snapshots already exist',
            'snapshot_count': len(existing),
        }, ensure_ascii=False))
        return

    data = load_state()['seen']
    groups = {}
    for key, item in data.items():
        if item.get('status') != 'ranked':
            continue
        snap_date = (item.get('rank_date') or item.get('last_verified') or item.get('last_seen') or date.today().isoformat())[:10]
        groups.setdefault(snap_date, []).append(sanitised_item(key, item))

    created_paths = []
    for snap_date, items in sorted(groups.items()):
        run_id = f'legacy-{snap_date}'
        created_at = f'{snap_date}T00:00:00+00:00'
        path, _, made = write_snapshot(run_id, items, snapshot_date=snap_date, created_at=created_at, legacy_import=True)
        if made:
            created_paths.append(str(path.relative_to(ROOT)).replace('\\', '/'))

    print(json.dumps({
        'created': len(created_paths),
        'snapshots': created_paths,
        'ranked_records_imported': sum(len(v) for v in groups.values()),
    }, ensure_ascii=False))


def choose_snapshot(mode, value=''):
    snaps, damaged = readable_snapshots()
    if not snaps:
        if damaged:
            raise SystemExit(
                f'No READABLE shortlist snapshot exists. {len(damaged)} file(s) are '
                'present but could not be parsed:\n'
                + '\n'.join(f"  {row['path']}: {row['error']}" for row in damaged)
                + '\n  They have been left untouched. Restore one from backups, or run '
                  '/rank to create a new snapshot.')
        raise SystemExit('No shortlist snapshots exist yet. Run /rank first.')
    snaps.sort(key=snapshot_sort_key)
    if mode == 'latest':
        # The newest READABLE snapshot. A damaged newest file must not hide the
        # history behind it.
        return snaps[-1], snaps, damaged
    if mode == 'date':
        date_value = value
        if date_value == 'today':
            date_value = date.today().isoformat()
        matches = [s for s in snaps if s.get('date') == date_value]
        if not matches:
            hint = ''
            if damaged:
                hint = (f' {len(damaged)} unreadable snapshot(s) were skipped; one of them '
                        'may be the day you asked for.')
            raise SystemExit(f'No shortlist snapshot found for {date_value}.{hint}')
        matches.sort(key=snapshot_sort_key)
        return matches[-1], matches, damaged
    raise SystemExit(f'Unsupported snapshot selector: {mode}')


def fmt_score(item):
    score = item.get('rank_score')
    return str(score) if isinstance(score, int) else '-'


def fmt_item(item, provisional=False):
    company = item.get('company') or 'Unknown company'
    title = item.get('title') or 'Unknown role'
    location = item.get('location') or 'Location not stated'
    score = fmt_score(item)
    if provisional and score != '-':
        score = f'{score}/75'
    sponsor = item.get('sponsorship') or 'Unresolved'
    verdict = item.get('rank_verdict') or item.get('filter_reason') or ''
    url = item.get('url') or ''
    line = f'- {score} | {company} | {title} | {location} | sponsorship: {sponsor}'
    # Consolidation hints rather than merges, because merging needs published
    # identifier evidence. Say so on the line: the same vacancy legitimately
    # appears on three boards, and a reader comparing rows deserves to know two
    # of them may be one job rather than two opportunities.
    _dupes = item.get('possible_duplicate_keys') or []
    if _dupes:
        line += f' | possible duplicate of {len(_dupes)} other sighting(s)'
    if verdict:
        line += f' | {verdict}'
    if url:
        line += f' | {url}'
    return line


def render_snapshot(snapshot, day_run_count=1):
    items = snapshot.get('items', [])
    buckets = {k: [] for k in BANDS}
    for item in items:
        buckets[category(item)].append(item)

    for name in ('exceptional', 'strong', 'viable', 'borderline', 'below', 'agency'):
        buckets[name].sort(key=lambda x: (-(x.get('rank_score') or -1), x.get('company', ''), x.get('title', '')))

    lines = [
        f"# Shortlist - {snapshot.get('date', '')}",
        f"Run: {snapshot.get('run_id', '')}",
        f"Captured: {snapshot.get('created_at', '')}",
    ]
    if snapshot.get('legacy_import'):
        lines.append('Snapshot type: imported baseline from ranked discovery state')
    scope = snapshot.get('run_scope') or {}
    if scope.get('partial'):
        lines.append(
            f"Ranking run coverage: PARTIAL - ranked {scope.get('ranked', 0)} of "
            f"{scope.get('total_matching', 0)}, deferred {scope.get('deferred', 0)}")
    if day_run_count > 1:
        lines.append(f'Runs recorded for this day: {day_run_count} (showing latest)')

    sections = [
        (_band_heading('exceptional', 'Exceptional Matches'), 'exceptional', False),
        (_band_heading('strong', 'Strong Matches'), 'strong', False),
        (_band_heading('viable', 'Viable Matches'), 'viable', False),
        (_band_heading('borderline', 'Borderline Review'), 'borderline', False),
        ('Verification Leads', 'verification', False),
        ('Agency Leads', 'agency', True),
    ]
    for heading, key, provisional in sections:
        lines.extend(['', f'## {heading}'])
        if not buckets[key]:
            lines.append('- None')
        else:
            lines.extend(fmt_item(x, provisional=provisional) for x in buckets[key])

    lines.extend(['', '## Below Threshold / Skip'])
    if buckets['below']:
        lines.extend(fmt_item(x) for x in buckets['below'])
    else:
        lines.append('- None')
    if buckets['other']:
        lines.extend(['', '## Other Ranked Records'])
        lines.extend(fmt_item(x) for x in buckets['other'])

    return '\n'.join(lines)


def cmd_show(args):
    if args.all:
        snaps, damaged = readable_snapshots()
        if not snaps:
            if damaged:
                raise SystemExit(
                    f'No READABLE shortlist snapshot exists. {len(damaged)} file(s) are '
                    'present but could not be parsed:\n'
                    + '\n'.join(f"  {row['path']}: {row['error']}" for row in damaged)
                    + '\n  They have been left untouched.')
            raise SystemExit('No shortlist snapshots exist yet. Run /rank first.')
        snaps.sort(key=snapshot_sort_key)
        by_date = {}
        for snap in snaps:
            by_date.setdefault(snap.get('date', ''), []).append(snap)
        blocks = ['# Shortlist History']
        for snap_date in sorted(by_date, reverse=True):
            runs = sorted(by_date[snap_date], key=snapshot_sort_key)
            latest = runs[-1]
            c = counts_from_snapshot(latest)
            scope = latest.get('run_scope') or {}
            line = (
                f"- {snap_date}: {len(runs)} run(s), latest {latest.get('run_id')} | "
                f"Exceptional {c.get('exceptional', 0)} | Strong {c.get('strong', 0)} | "
                f"Viable {c.get('viable', 0)} | Borderline {c.get('borderline', 0)} | "
                f"Verification {c.get('verification', 0)} | "
                f"Agency {c.get('agency', 0)} | Below {c.get('below', 0)}"
            )
            if scope.get('partial'):
                line += f" | PARTIAL {scope.get('ranked', 0)}/{scope.get('total_matching', 0)}"
            blocks.append(line)
        blocks.extend(damage_notice(damaged))
        print('\n'.join(blocks))
        return

    if args.date:
        snap, matches, damaged = choose_snapshot('date', args.date)
        print('\n'.join([render_snapshot(snap, day_run_count=len(matches))]
                        + damage_notice(damaged)))
        return

    snap, all_snaps, damaged = choose_snapshot('latest')
    same_day = [s for s in all_snaps if s.get('date') == snap.get('date')]
    print('\n'.join([render_snapshot(snap, day_run_count=len(same_day))]
                    + damage_notice(damaged)))


def _force_utf8_stdout():
    """Vacancy text is not cp1252, and a Windows console is.

    A real advert title carrying an en-dash or a pound sign made this tool exit
    with UnicodeEncodeError instead of printing, which took `/rank` down on
    Windows the moment a normal role title contained one. The DATA was fine; only
    the console encoding was wrong, so fix the stream rather than the text.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if (getattr(stream, 'encoding', '') or '').lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def main():
    _force_utf8_stdout()
    p = argparse.ArgumentParser(description='Immutable shortlist snapshots for ranked job-discovery runs')
    sub = p.add_subparsers(dest='cmd', required=True)

    b = sub.add_parser('begin', help='Create a unique rank run ID')
    b.set_defaults(func=cmd_begin)

    s = sub.add_parser('snapshot', help='Freeze all jobs tagged with one rank run ID')
    s.add_argument('--run-id', required=True)
    s.add_argument('--total-matching', type=int, default=0,
                   help='Total records that matched the /rank selection before any --limit')
    s.add_argument('--limit', type=int, default=0,
                   help='The --limit used when listing candidates for this run')
    s.set_defaults(func=cmd_snapshot)

    boot = sub.add_parser('bootstrap', help='Create baseline snapshots from current ranked state without changing it')
    boot.add_argument('--if-empty', action='store_true')
    boot.set_defaults(func=cmd_bootstrap)

    sh = sub.add_parser('show', help='Display a saved shortlist')
    sel = sh.add_mutually_exclusive_group()
    sel.add_argument('--date', default='')
    sel.add_argument('--all', action='store_true')
    sh.set_defaults(func=cmd_show)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

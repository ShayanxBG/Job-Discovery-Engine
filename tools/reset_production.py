#!/usr/bin/env python3
"""Reset the ACTIVE search state for a clean production start.

WHY THIS EXISTS SEPARATELY FROM /reset-discovery.

`job_state.py reset` clears `seen_jobs.json` and nothing else, which is exactly
right for its own job: give discovery a clean seen-list while every other store
keeps working. But it leaves the JD cache, the suppression store, the run logs and
the shortlist history in place, so a workspace "reset" that way still carries the
whole validation era. The certification audit had to assemble the real reset by
hand, and a reset assembled by hand before every production start is a reset that
will eventually be assembled wrongly.

So this is ONE deterministic command for the complete active-state reset, and
`/reset-discovery` keeps its narrower meaning unchanged.

WHAT IS CLEARED is everything that could let a validation-era vacancy still affect
a production run: seen detection, cache reuse, suppression, ranking, shortlist
history, freshness judgements and run counters.

WHAT IS KEPT is everything that is not about a particular vacancy: the candidate
profile and calibration, the master CV, the matching policy, the search strategy,
the source registry, all code and tests, and the official sponsor-register
snapshot. Employer identity intelligence is kept SELECTIVELY, because a verified
legal name, a recorded alias, an official domain and a confirmed ATS tenant are
facts about a company rather than about a vacancy, and re-earning them costs real
research budget. A speculative guess is not such a fact and is dropped.

SAFETY. Nothing is cleared until one timestamped archive of the complete pre-reset
runtime exists and has been verified byte-for-byte. If the archive cannot be
written or does not verify, the reset does not happen. The archive lives under
`backups/`, which no discovery, ranking or shortlist path reads, so an archived
vacancy can never re-enter a run. Older backups are never touched.
"""
import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = ROOT / 'backups' / 'production-reset'

# Active runtime. Every one of these can make a validation-era vacancy affect a
# production run, so every one is archived and then cleared.
ACTIVE_FILES = (
    'job_scraper/seen_jobs.json',
    'job_scraper/suppression.json',
    'job_scraper/watchlist.json',
)
ACTIVE_DIRS = (
    'job_scraper/runs',
    'job_scraper/cache',
    'job_scraper/shortlists',
)
# Archived and then REBUILT rather than emptied: see `prune_employers`.
CURATED_FILES = (
    'job_scraper/employers.json',
    'job_scraper/sponsorship_evidence.json',
)

# Files that must survive untouched. Verified byte-for-byte after the reset.
PRESERVED = (
    'candidate/profile.md',
    'candidate/config.json',
    'candidate/config.example.json',
    'candidate/profile.example.md',
    'candidate/cv-maintenance.md',
    'documents/master/cv.pdf',
    'documents/master/cv.json',
    'config/matching_policy.json',
    'config/search_strategy.json',
    'config/sources.json',
    'job_scraper/reference/sponsor-register.csv',
    'job_scraper/reference/sponsor-register-meta.json',
    '.claude/agents/public-job-researcher.md',
    '.claude/agents/sponsor-verifier.md',
)

# Empty forms written back so the next run starts from a valid store rather than a
# missing file. A missing state file is a LOST-state workspace, which is a
# different and alarming condition; a reset must not manufacture one.
EMPTY_STATE = {'schema_version': 2, 'seen': {}}
EMPTY_SUPPRESSION = {'schema_version': 1, 'suppressed': {}}

# Employer facts worth carrying across a reset. Each is a verified property of a
# COMPANY, not of a vacancy or of a run.
EMPLOYER_KEEP_FIELDS = (
    'employer_key', 'canonical_name', 'aliases', 'website_domain', 'careers_url',
    'ats_platform', 'ats_tenant', 'sponsor_register_name', 'first_seen',
)


def reset_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_digest(path):
    """Path-and-content digest of a directory, so a copy can be verified exactly."""
    root = Path(path)
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): digest(p)
            for p in sorted(root.rglob('*')) if p.is_file()}


def load_json(rel):
    path = ROOT / rel
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None


def prune_employers(data):
    """Keep verified employer IDENTITY, drop everything speculative.

    A confirmed legal name, an explicitly recorded alias, an official domain and a
    known ATS tenant are facts about a company that survive a reset intact.
    A weak or unresolved guess is not, and carrying one forward would let a
    validation-era mistake quietly shape a production run's employer resolution.
    """
    employers = (data or {}).get('employers')
    if not isinstance(employers, dict):
        return {'schema_version': (data or {}).get('schema_version', 1), 'employers': {}}, 0, 0
    kept, dropped = {}, 0
    for key, row in employers.items():
        if not isinstance(row, dict):
            dropped += 1
            continue
        verified = bool(
            str(row.get('canonical_name') or '').strip()
            and (row.get('website_domain') or row.get('careers_url')
                 or row.get('ats_tenant') or row.get('sponsor_register_name')
                 or row.get('aliases'))
        )
        if not verified:
            dropped += 1
            continue
        kept[key] = {f: row[f] for f in EMPLOYER_KEEP_FIELDS if f in row}
    return ({'schema_version': (data or {}).get('schema_version', 1), 'employers': kept},
            len(kept), dropped)


def sponsorship_is_separable(data):
    """Whether employer-level sponsorship evidence is cleanly distinguishable.

    The schema records evidence per employer, but individual items carry vacancy
    and run-specific kinds alongside employer-level ones. Reusable means: an
    unexpired item whose kind is about the ORGANISATION, with no vacancy URL
    attached. If a store cannot be read at that granularity, the honest answer is
    to reset it entirely, because safety matters more than saving a lookup.
    """
    if data is None:
        return False
    employers = data.get('employers')
    return isinstance(employers, dict)


EMPLOYER_LEVEL_KINDS = ('sponsor_register', 'employer_statement')


def prune_sponsorship(data, today=None):
    """Keep only unexpired, employer-level, non-vacancy sponsorship evidence."""
    today = today or datetime.now().date().isoformat()
    employers = (data or {}).get('employers') or {}
    kept, dropped = {}, 0
    for key, row in employers.items():
        if not isinstance(row, dict):
            dropped += 1
            continue
        items = [
            item for item in (row.get('evidence') or [])
            if isinstance(item, dict)
            and item.get('kind') in EMPLOYER_LEVEL_KINDS
            and not str(item.get('vacancy_url') or '').strip()
            and str(item.get('expires_at') or '') >= today
        ]
        dropped += len(row.get('evidence') or []) - len(items)
        if not items:
            continue
        kept[key] = {k: v for k, v in row.items() if k not in ('evidence',)}
        kept[key]['evidence'] = items
    return ({'schema_version': (data or {}).get('schema_version', 1), 'employers': kept},
            len(kept), dropped)


def survey():
    """What the active runtime currently holds."""
    state = load_json('job_scraper/seen_jobs.json') or {}
    suppression = load_json('job_scraper/suppression.json') or {}
    watchlist = load_json('job_scraper/watchlist.json') or {}
    wl = watchlist.get('employers', watchlist.get('watchlist', []))
    return {
        'seen_jobs': len((state.get('seen') or {})),
        'suppression': len((suppression.get('suppressed') or {})),
        'runs': len(list((ROOT / 'job_scraper/runs').glob('*.json'))
                    if (ROOT / 'job_scraper/runs').is_dir() else []),
        'cache': len(list((ROOT / 'job_scraper/cache').glob('*.json'))
                     if (ROOT / 'job_scraper/cache').is_dir() else []),
        'shortlists': len(list((ROOT / 'job_scraper/shortlists').glob('*.json'))
                          if (ROOT / 'job_scraper/shortlists').is_dir() else []),
        'watchlist': len(wl) if isinstance(wl, (list, dict)) else 0,
    }


def build_plan():
    """Everything the reset would archive, clear and preserve. Reads only."""
    employers_data = load_json('job_scraper/employers.json')
    pruned_employers, emp_kept, emp_dropped = prune_employers(employers_data)
    sponsorship_data = load_json('job_scraper/sponsorship_evidence.json')
    separable = sponsorship_is_separable(sponsorship_data)
    if separable:
        pruned_sponsorship, spon_kept, spon_dropped = prune_sponsorship(sponsorship_data)
    else:
        pruned_sponsorship, spon_kept, spon_dropped = (
            {'schema_version': 1, 'employers': {}}, 0,
            len(((sponsorship_data or {}).get('employers') or {})))
    return {
        'before': survey(),
        'archive_root': ARCHIVE_ROOT.relative_to(ROOT).as_posix(),
        'will_archive': [rel for rel in ACTIVE_FILES + CURATED_FILES if (ROOT / rel).is_file()]
                        + [rel for rel in ACTIVE_DIRS if (ROOT / rel).is_dir()],
        'will_clear': list(ACTIVE_FILES) + list(ACTIVE_DIRS),
        'will_preserve': [rel for rel in PRESERVED if (ROOT / rel).exists()],
        'employers': {'kept': emp_kept, 'dropped_speculative': emp_dropped},
        'sponsorship_evidence': {
            'separable': separable, 'kept_employers': spon_kept, 'dropped_items': spon_dropped},
        '_pruned_employers': pruned_employers,
        '_pruned_sponsorship': pruned_sponsorship,
    }


def archive(stamp):
    """Copy the complete pre-reset runtime into one timestamped folder and VERIFY it.

    Fails closed. If any part cannot be copied or does not verify byte-for-byte,
    nothing is cleared: an unverified archive is not a backup, and clearing behind
    one would be the single most destructive thing this workspace can do.
    """
    # The timestamp has one-second resolution, so two resets in the same second
    # would collide. A collision is a NAMING problem, not a safety one: take the
    # next free name rather than refusing, but never write into an existing archive,
    # because overwriting one backup while creating another is how both get lost.
    dest = ARCHIVE_ROOT / stamp
    suffix = 2
    while dest.exists():
        dest = ARCHIVE_ROOT / f'{stamp}-{suffix}'
        suffix += 1
        if suffix > 100:
            raise reset_error(
                f'Could not find a free archive name under {ARCHIVE_ROOT.relative_to(ROOT).as_posix()}.',
                'Nothing has been cleared.')
    dest.mkdir(parents=True, exist_ok=False)
    copied_files, copied_dirs = [], []
    for rel in ACTIVE_FILES + CURATED_FILES:
        src = ROOT / rel
        if not src.is_file():
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        if digest(out) != digest(src):
            raise reset_error(f'Archive verification FAILED for {rel}.',
                              'Nothing has been cleared. Resolve the copy problem and retry.')
        copied_files.append(rel)
    for rel in ACTIVE_DIRS:
        src = ROOT / rel
        if not src.is_dir():
            continue
        out = dest / rel
        shutil.copytree(src, out)
        if tree_digest(out) != tree_digest(src):
            raise reset_error(f'Archive verification FAILED for {rel}/.',
                              'Nothing has been cleared. Resolve the copy problem and retry.')
        copied_dirs.append(rel)
    manifest = {
        'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'reason': 'complete production reset of the active search state',
        'files': {rel: digest(ROOT / rel) for rel in copied_files},
        'directories': {rel: tree_digest(ROOT / rel) for rel in copied_dirs},
        'counts_before': survey(),
    }
    (dest / 'MANIFEST.json').write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return dest, manifest


def clear_active(plan):
    """Clear the active runtime. Only ever called after a verified archive."""
    (ROOT / 'job_scraper').mkdir(parents=True, exist_ok=True)
    (ROOT / 'job_scraper/seen_jobs.json').write_text(
        json.dumps(EMPTY_STATE, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    (ROOT / 'job_scraper/suppression.json').write_text(
        json.dumps(EMPTY_SUPPRESSION, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    watchlist = ROOT / 'job_scraper/watchlist.json'
    if watchlist.is_file():
        watchlist.unlink()
    for rel in ACTIVE_DIRS:
        directory = ROOT / rel
        if not directory.is_dir():
            directory.mkdir(parents=True, exist_ok=True)
            continue
        for path in sorted(directory.glob('*.json')):
            path.unlink()
    (ROOT / 'job_scraper/employers.json').write_text(
        json.dumps(plan['_pruned_employers'], indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8')
    (ROOT / 'job_scraper/sponsorship_evidence.json').write_text(
        json.dumps(plan['_pruned_sponsorship'], indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8')


def cmd_reset(args):
    plan = build_plan()
    preserved_before = {rel: digest(ROOT / rel) for rel in PRESERVED if (ROOT / rel).is_file()}

    if not args.confirm:
        report = {k: v for k, v in plan.items() if not k.startswith('_')}
        report['dry_run'] = True
        report['after_would_be'] = {k: 0 for k in plan['before']}
        report['mutated'] = False
        report['note'] = (
            'DRY RUN. Nothing was archived, cleared or changed. '
            'To perform the reset: python tools/reset_production.py --confirm')
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest, manifest = archive(stamp)
    clear_active(plan)

    after = survey()
    problems = [name for name, count in after.items() if count]
    changed = [rel for rel, before in preserved_before.items()
               if digest(ROOT / rel) != before]
    report = {
        'dry_run': False,
        'mutated': True,
        'archive': dest.relative_to(ROOT).as_posix(),
        'archive_verified': True,
        'before': plan['before'],
        'after': after,
        'employers': plan['employers'],
        'sponsorship_evidence': {k: v for k, v in plan['sponsorship_evidence'].items()},
        'preserved_verified': not changed,
        'preserved_changed': changed,
        'active_state_clean': not problems,
        'note': ('The active search state is clear. Reference data, calibration, the '
                 'master CV, the sponsor register and verified employer identity are '
                 'preserved. The complete pre-reset runtime is archived under '
                 f'{dest.relative_to(ROOT).as_posix()}, which no discovery, ranking or '
                 'shortlist path reads.'),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if problems or changed:
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser(
        prog='reset_production.py',
        description='Archive and clear the ACTIVE search state for a clean production '
                    'start. Preserves candidate calibration, the master CV, all policy '
                    'and reference data, and verified employer identity. Requires '
                    '--confirm; --help and --dry-run never mutate anything.')
    p.add_argument('--dry-run', action='store_true',
                   help='Show exactly what would be archived, cleared and preserved. '
                        'Writes nothing. This is the default when --confirm is absent.')
    p.add_argument('--confirm', action='store_true',
                   help='Actually perform the reset. Without this nothing is mutated.')
    p.set_defaults(func=cmd_reset)
    args = p.parse_args()
    if args.dry_run and args.confirm:
        raise reset_error('--dry-run and --confirm contradict each other.',
                          'Use one or the other.')
    args.func(args)


if __name__ == '__main__':
    main()

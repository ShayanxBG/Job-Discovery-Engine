#!/usr/bin/env python3
"""Archive the protected master CV and candidate evidence before an approved edit.

Called by /update-master, always BEFORE any protected file is modified, so the
previous approved master can be recovered exactly.

WHY THIS HAS AN ARGUMENT PARSER. It did not, and the certification audit tripped
over the consequence: the script ran its copy unconditionally, so merely asking
`python tools/backup_master.py --help` created a real history folder in the live
workspace. A tool that mutates while being asked what it does is a trap, and the
audit's own CLI enumeration fell into it. `--help` and `-h` now print and exit
without touching the filesystem, an unknown argument is a clean argparse error
with no backup, and only a deliberate invocation archives anything.
"""
import argparse
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / 'documents' / 'master' / 'history'

# The protected artefacts /update-master is allowed to change.
PROTECTED = (
    Path('documents/master/cv.pdf'),
    Path('documents/master/cv.json'),
    Path('candidate/profile.md'),
)


def plan():
    """Which protected files exist and would be archived."""
    return [rel for rel in PROTECTED if (ROOT / rel).exists()]


def cmd_backup(args):
    present = plan()
    if args.dry_run:
        print('\n'.join([
            'DRY RUN. Nothing was written.',
            f'Would archive into: documents/master/history/<timestamp>/',
            *(f'  {rel.as_posix()}' for rel in present),
        ] + ([] if present else ['  (no protected file is present to archive)'])))
        return
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dst = HISTORY / stamp
    dst.mkdir(parents=True, exist_ok=False)
    for rel in present:
        shutil.copy2(ROOT / rel, dst / rel.name)
    print(dst)


def main():
    p = argparse.ArgumentParser(
        prog='backup_master.py',
        description='Archive the master CV, its JSON and the candidate profile into a '
                    'timestamped history folder. Run before an approved /update-master '
                    'edit. --help and --dry-run write nothing.')
    p.add_argument('--dry-run', action='store_true',
                   help='Report exactly what would be archived and write nothing.')
    p.set_defaults(func=cmd_backup)
    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

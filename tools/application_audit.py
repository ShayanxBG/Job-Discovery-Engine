#!/usr/bin/env python3
"""Audit that no operational instruction can perform an application action.

THE PRODUCT BOUNDARY. This workspace stops at the shortlist. It never applies,
never contacts a recruiter, never uploads a CV, never saves a job on an external
platform and never changes an account setting. That boundary is only real if
nothing in the operational rules can be read as an instruction to do those things.

THE HARD PART IS THE OPPOSITE OF NAIVE GREPPING. The safest files in this
workspace are the ones that talk about applying MOST, because they are the ones
forbidding it:

    "Never click Apply/Easy Apply."          <- the boundary working
    "Click Apply on the strongest match."    <- the boundary broken

A check that greps for "Apply" flags both, so it gets disabled or ignored, and
then it protects nothing. This module therefore reads each occurrence IN CONTEXT
and classifies it:

    prohibition   the sentence forbids the action. Expected and healthy.
    reference     the sentence names the action while describing something else,
                  such as a UI element the agent must not touch, or a category of
                  thing the product does not do.
    instruction   the sentence tells an agent to take the action. A violation.

Only `instruction` fails. The audit deliberately reports how many prohibitions it
found too: a file that stopped forbidding these actions is itself a regression,
even though it breaks no rule.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Where operational instructions actually live. Rules the agent follows, not
# changelogs or backups describing what was done historically.
OPERATIONAL_GLOBS = (
    'CLAUDE.md',
    'README.md',
    '.claude/commands/*.md',
    '.claude/agents/*.md',
    '.claude/skills/**/*.md',
    'docs/*.md',
    'tools/*.py',
    'config/*.json',
)
EXCLUDED_PARTS = ('backups', 'reports', '__pycache__', 'CHANGELOG.md', 'PACKAGE_MANIFEST.txt')

# The actions that would break the product boundary.
APPLICATION_ACTIONS = (
    r'easy\s*apply',
    r'\bapply\s+(?:to|for|on)\b',
    r'\bclick(?:ing|s)?\s+apply\b',
    r'submit(?:ting)?\s+(?:an?\s+)?(?:application|form|cv|r[eé]sum[eé])',
    r'send(?:ing)?\s+(?:a\s+)?(?:message|email|dm)\s+to\s+(?:the\s+)?(?:recruiter|hiring|employer)',
    r'contact(?:ing)?\s+(?:the\s+)?recruiter',
    r'upload(?:ing)?\s+(?:the\s+|your\s+|a\s+)?(?:cv|r[eé]sum[eé]|cover\s*letter)',
    r'cover\s*letter\s+submission',
    r'autofill\s+(?:the\s+)?application',
    r'save\s+(?:the\s+)?job\s+(?:on|to)\b',
    r'change\s+(?:the\s+)?(?:job[- ]search\s+)?preference',
    r'change\s+(?:the\s+)?account\s+setting',
)
ACTION_PATTERN = re.compile('|'.join(f'(?:{p})' for p in APPLICATION_ACTIONS), re.I)

# Wording that makes an occurrence a PROHIBITION rather than an instruction.
PROHIBITION_MARKERS = (
    r'\bnever\b', r'\bdo not\b', r"\bdon't\b", r'\bnot\b', r'\bno\b', r'\bnor\b',
    r'\bforbid', r'\bprohibit', r'\brefus', r'\bmust not\b', r'\bcannot\b', r"\bcan't\b",
    r'\bwithout\b', r'\bstops? (?:at|short of|before)\b', r'\bexclud', r'\bavoid',
    r'\bblock', r'\brejec', r'\bdisallow', r'\bunauthoris', r'\bunauthoriz',
    r'\bboundary\b', r'\bnot? application\b', r'\bhuman (?:takes over|decides)\b',
    r'\bleaves? (?:it )?to the (?:human|user)\b', r'\bviolation\b', r'\bforbidden\b',
)
PROHIBITION_PATTERN = re.compile('|'.join(PROHIBITION_MARKERS), re.I)

# Wording that makes an occurrence a REFERENCE: it names the action while talking
# about something else, such as a filter or a UI element.
REFERENCE_MARKERS = (
    r'\bas a requirement\b', r'\bfilter\b', r'\brequirement\b', r'\bbutton\b',
    r'\blabel\b', r'\bapplicant count\b', r'\bapply\s*[-_]?\s*(?:url|link)\b',
    r'\bvocabulary\b', r'\bpattern\b', r'\bregex\b', r'\bexample\b', r'\baudit\b',
)
REFERENCE_PATTERN = re.compile('|'.join(REFERENCE_MARKERS), re.I)

# An imperative verb at the start of a sentence or list item, which is what an
# operational instruction actually looks like.
IMPERATIVE_LEAD = re.compile(
    r'^\s*(?:[-*+]\s*|\d+[.)]\s*)?(?:then\s+|next\s+|finally\s+|now\s+)?'
    r'(?:click|press|select|submit|send|upload|apply|save|message|email|contact|fill|'
    r'autofill|change|set|enable|disable)\b', re.I)


def audit_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def operational_files(root=None):
    root = Path(root) if root else ROOT
    seen, out = set(), []
    for pattern in OPERATIONAL_GLOBS:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            if any(part in EXCLUDED_PARTS for part in path.parts):
                continue
            if path.resolve() in seen:
                continue
            seen.add(path.resolve())
            out.append(path)
    return out


def _sentences(line):
    """Split a line into sentence-ish spans, so context stays local to the claim."""
    return [s for s in re.split(r'(?<=[.!?;])\s+', line) if s.strip()]


def classify_occurrence(sentence, lead_in=''):
    """Whether one occurrence forbids, merely mentions, or orders an application action.

    `lead_in` is the sentence that introduces the enclosing list, and it is what
    makes this usable in practice. A prohibition is almost always written as a
    heading plus bullets:

        Never during /scrape:
        - click Apply or Easy Apply
        - upload a CV

    The bullet on its own is a bare imperative and looks exactly like an
    instruction. Only the lead-in says it is forbidden, so the lead-in has to be
    part of the judgement. Without this, every well-written safety list in the
    workspace reads as a violation, the check gets ignored, and it protects nothing.
    """
    if PROHIBITION_PATTERN.search(sentence):
        return 'prohibition'
    if lead_in and PROHIBITION_PATTERN.search(lead_in):
        return 'prohibition'
    if REFERENCE_PATTERN.search(sentence):
        return 'reference'
    if IMPERATIVE_LEAD.match(sentence):
        return 'instruction'
    # A declarative sentence naming an action, with no prohibition and no imperative,
    # describes rather than orders. Reported as a reference so it stays visible.
    return 'reference'


def audit_workspace(root=None):
    """Every occurrence of an application action in operational files, classified."""
    root = Path(root) if root else ROOT
    violations, prohibitions, references = [], [], []
    files = operational_files(root)
    for path in files:
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        lines = text.splitlines()
        lead_in = ''
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            is_item = bool(re.match(r'^\s*(?:[-*+]|\d+[.)])\s+', line))
            if stripped and not is_item:
                # The most recent non-item prose line introduces whatever list
                # follows. A BLANK line must not clear it: markdown normally puts a
                # blank line between a lead-in and its bullets, so clearing there
                # would detach every safety list from the sentence forbidding it.
                lead_in = stripped
            if not ACTION_PATTERN.search(line):
                continue
            for sentence in _sentences(line):
                if not ACTION_PATTERN.search(sentence):
                    continue
                verdict = classify_occurrence(sentence, lead_in if is_item else '')
                row = {'file': rel, 'line': number, 'verdict': verdict,
                       'text': sentence.strip()[:200], 'lead_in': lead_in[:120] if is_item else ''}
                if verdict == 'instruction':
                    violations.append(row)
                elif verdict == 'prohibition':
                    prohibitions.append(row)
                else:
                    references.append(row)
    return {
        'files_scanned': len(files),
        'violations': violations,
        'prohibition_count': len(prohibitions),
        'reference_count': len(references),
        'prohibitions': prohibitions,
        'references': references,
        'clean': not violations,
        'note': 'Only an INSTRUCTION to perform an application action is a violation. '
                'Documentation forbidding those actions is the boundary working, and a '
                'file that stopped forbidding them would itself be a regression.',
    }


def cmd_audit(args):
    report = audit_workspace(args.root or None)
    if not args.verbose:
        report.pop('prohibitions', None)
        report.pop('references', None)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(0 if report['clean'] else 1)


def cmd_classify(args):
    print(json.dumps({'text': args.text, 'names_action': bool(ACTION_PATTERN.search(args.text)),
                      'verdict': classify_occurrence(args.text)}, indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description='Audit for application-automation surfaces')
    sub = p.add_subparsers(dest='cmd', required=True)

    a = sub.add_parser('audit', help='Scan operational files for application instructions.')
    a.add_argument('--root', default='')
    a.add_argument('--verbose', action='store_true',
                   help='Include the prohibitions and references found.')
    a.set_defaults(func=cmd_audit)

    c = sub.add_parser('classify', help='Classify one sentence.')
    c.add_argument('text')
    c.set_defaults(func=cmd_classify)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

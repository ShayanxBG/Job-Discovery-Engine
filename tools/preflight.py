#!/usr/bin/env python3
"""One deterministic readiness check, run before a live discovery or ranking cycle.

WHY A SINGLE GATE. The workspace has many independent health checks: state doctor,
candidate config, matching policy, source registry, search strategy, sponsor
snapshot. Running them one at a time before a live cycle is easy to do partially
and easy to misread, and the interesting question is not whether each passes but
whether the SYSTEM is safe to point at real, untrusted job pages right now.

So this answers one question with one word:

    READY                 every gate passed. A live cycle may proceed.
    READY_WITH_WARNINGS   nothing is broken, but something is degraded in a way the
                          run itself can recover from.
    NOT_READY             a gate that protects real data failed. Do not run.

THE DISTINCTION THAT MATTERS. A stale or missing sponsor snapshot is a WARNING,
because `/scrape` refreshes it at startup and falls back honestly when GOV.UK is
unavailable. A corrupt discovery state or an invalid matching policy is FATAL,
because the first would risk writing into damaged history and the second would
produce scores that look ordinary and mean nothing. Conflating those two would
either block runs needlessly or let a broken one proceed.

THIS SEARCHES NOTHING. No job board is contacted, no browser is opened, no network
call is made, and nothing is written. It reads configuration and state and reports.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools'

STATUSES = ('READY', 'READY_WITH_WARNINGS', 'NOT_READY')
SEVERITIES = ('ok', 'warning', 'fatal')

# Private candidate evidence a live cycle needs. Their ABSENCE is fatal: a run
# without them would either fail late or, worse, proceed on defaults.
REQUIRED_PRIVATE_FILES = (
    'candidate/profile.md',
    'candidate/config.json',
)
REQUIRED_PUBLIC_FILES = (
    'config/sources.json',
    'config/search_strategy.json',
    'config/matching_policy.json',
    '.claude/agents/public-job-researcher.md',
    '.claude/agents/sponsor-verifier.md',
)
# Directories the run needs to be able to create and write inside.
RUNTIME_DIRS = (
    'job_scraper',
    'job_scraper/runs',
    'job_scraper/cache',
    'job_scraper/shortlists',
    'job_scraper/reference',
)


def check(name, severity, ok, detail='', **extra):
    return {'check': name, 'ok': bool(ok),
            'severity': 'ok' if ok else severity, 'detail': detail, **extra}


def _run_tool(script, *args):
    """Run one helper and return (exit_code, parsed_json_or_None, raw_output)."""
    proc = subprocess.run([sys.executable, str(TOOLS / script), *args],
                          cwd=ROOT, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    try:
        return proc.returncode, json.loads(proc.stdout or '{}'), proc.stdout + proc.stderr
    except json.JSONDecodeError:
        return proc.returncode, None, proc.stdout + proc.stderr


def check_state():
    code, data, raw = _run_tool('job_state.py', 'doctor')
    if data is None:
        return check('discovery_state', 'fatal', False,
                     f'job_state.py doctor did not return readable JSON: {raw[:200]}')
    healthy = bool(data.get('healthy'))
    return check('discovery_state', 'fatal', healthy,
                 'Discovery state is healthy.' if healthy else
                 'Discovery state is not healthy. A live cycle must not write into '
                 'damaged history. Run: python tools/job_state.py doctor',
                 record_count=data.get('record_count'),
                 schema_version=data.get('schema_version'),
                 errors=data.get('errors', [])[:3],
                 schema_violations=data.get('schema_violations', [])[:3])


def check_candidate_config():
    path = ROOT / 'candidate' / 'config.json'
    if not path.exists():
        return check('candidate_config', 'fatal', False,
                     'candidate/config.json is missing. Build it with: '
                     'python tools/candidate_config.py build')
    code, data, raw = _run_tool('candidate_config.py', 'validate')
    if data is None:
        return check('candidate_config', 'fatal', False, f'validate did not return JSON: {raw[:200]}')
    valid = bool(data.get('valid'))
    return check('candidate_config', 'fatal', valid,
                 'Candidate calibration is valid.' if valid else
                 'The candidate calibration is invalid. Ranking against it would apply '
                 'constraints nobody configured.',
                 structure_problems=data.get('structure_problems', [])[:3],
                 privacy_problems=data.get('privacy_problems', [])[:3],
                 unknown_fields=data.get('unknown_fields', []))


def check_matching_policy():
    code, data, raw = _run_tool('match_evaluation.py', 'validate-policy')
    if data is None:
        return check('matching_policy', 'fatal', False, f'validate-policy did not return JSON: {raw[:200]}')
    valid = bool(data.get('valid'))
    return check('matching_policy', 'fatal', valid,
                 'Matching policy is valid.' if valid else
                 'The matching policy is invalid. Scores produced against a broken '
                 'weighting look ordinary and mean nothing.',
                 sum_of_component_maxima=data.get('sum_of_component_maxima'),
                 location_score_weight=data.get('location_score_weight'),
                 problems=data.get('problems', [])[:3])


def check_source_registry():
    code, data, raw = _run_tool('sources.py', 'validate')
    if data is None:
        return check('source_registry', 'fatal', False, f'validate did not return JSON: {raw[:200]}')
    valid = bool(data.get('valid'))
    return check('source_registry', 'fatal', valid,
                 'Source registry is valid.' if valid else
                 'The source registry is invalid, so source identity and family '
                 'coverage cannot be judged.',
                 problems=data.get('problems', [])[:3])


def check_search_strategy():
    code, data, raw = _run_tool('search_strategy.py', 'validate')
    if data is None:
        return check('search_strategy', 'fatal', False, f'validate did not return JSON: {raw[:200]}')
    valid = bool(data.get('valid'))
    return check('search_strategy', 'fatal', valid,
                 'Search strategy is valid.' if valid else
                 'The search strategy is invalid, so query planning and stopping rules '
                 'cannot be trusted.',
                 families=data.get('families', []),
                 problems=data.get('problems', [])[:3])


def check_experience_calibration():
    """Commercial-experience freshness. A maintenance WARNING, never a gate.

    The Frontier role is ongoing and the profile states month-granularity dates, so
    the recorded range describes a moment rather than a standing fact. It is never
    extrapolated by guessing elapsed time, because that would invent a day nobody
    stated; it is reported as stale instead. It can reject nothing: the hard
    experience blocker turns on the vacancy's own explicit minimum, never on
    subtracting two approximate figures.
    """
    code, data, raw = _run_tool('candidate_config.py', 'validate')
    if data is None:
        return check('experience_calibration', 'warning', False,
                     f'validate did not return JSON: {raw[:200]}')
    state = (data.get('experience_staleness') or {})
    status = state.get('status')
    if status is None:
        return check('experience_calibration', 'warning', False,
                     'The calibration reports no commercial-experience freshness at all.')
    return check('experience_calibration', 'warning', status == 'fresh',
                 state.get('detail', ''), observed_at=state.get('observed_at'),
                 age_days=state.get('age_days'),
                 review_after_days=state.get('review_after_days'))


def check_immigration_reference():
    """Legal reference health. Stale is a WARNING, never fatal.

    Salary thresholds and going rates change by statement of changes rather than
    by anything this workspace controls, so the only real failure mode is a figure
    that keeps being applied long after it moved. Past its review date the
    reference says so out loud and every threshold judgement it supports has to be
    re-verified before it decides anything. It is deliberately not fatal: a stale
    reference still beats no reference, and stopping a whole run because a review
    date passed would be the harsher error.
    """
    code, data, raw = _run_tool('immigration_rules.py', 'status')
    if data is None:
        return check('immigration_reference', 'warning', False,
                     f'status did not return readable JSON: {raw[:200]}')
    state = data.get('status')
    if state == 'unavailable':
        return check('immigration_reference', 'warning', False,
                     'No usable immigration reference. Salary and sponsorship judgements have '
                     'no dated official basis and must be verified live.',
                     detail=data.get('detail', ''))
    return check('immigration_reference', 'warning', state == 'fresh',
                 'Immigration reference is inside its review window.' if state == 'fresh' else
                 'The immigration reference is past its review date. Re-verify every threshold '
                 'against the official GOV.UK pages before it decides a recommendation.',
                 observed_at=data.get('observed_at'), review_after=data.get('review_after'),
                 days_until_review=data.get('days_until_review'))


def check_sponsor_snapshot():
    """Sponsor snapshot health. Degraded is a WARNING, never fatal.

    `/scrape` refreshes the snapshot once at startup and falls back honestly when
    GOV.UK is unavailable, reporting UNAVAILABLE rather than a false negative. A
    stale or missing snapshot therefore costs local lookups, not correctness.
    """
    code, data, raw = _run_tool('sponsor_register.py', 'status')
    if data is None:
        return check('sponsor_snapshot', 'warning', False,
                     f'status did not return readable JSON: {raw[:200]}')
    if not data.get('available'):
        return check('sponsor_snapshot', 'warning', False,
                     'No official sponsor-register snapshot is installed. Employer '
                     'licence lookups will report UNAVAILABLE, which is not the same as '
                     'an employer being absent. /scrape will attempt one refresh.',
                     stale_reason=data.get('stale_reason', ''))
    if not data.get('integrity_ok', True):
        return check('sponsor_snapshot', 'warning', False,
                     'The installed snapshot no longer matches its recorded digest. It '
                     'will be treated as stale and refreshed.',
                     stale_reason=data.get('stale_reason', ''))
    if not data.get('fresh'):
        return check('sponsor_snapshot', 'warning', False,
                     'The sponsor-register snapshot is stale. /scrape will attempt one '
                     'refresh and continue with a warning if that fails.',
                     age_hours=data.get('age_hours'),
                     stale_reason=data.get('stale_reason', ''))
    return check('sponsor_snapshot', 'warning', True, 'Sponsor snapshot is fresh and intact.',
                 age_hours=data.get('age_hours'), row_count=data.get('row_count'),
                 organisation_count=data.get('organisation_count'))


def check_required_files():
    rows = []
    for rel in REQUIRED_PRIVATE_FILES:
        path = ROOT / rel
        rows.append(check(f'private_file:{rel}', 'fatal', path.is_file(),
                          'Present.' if path.is_file() else
                          f'{rel} is required private candidate evidence and is missing.'))
    for rel in REQUIRED_PUBLIC_FILES:
        path = ROOT / rel
        rows.append(check(f'required_file:{rel}', 'fatal', path.is_file(),
                          'Present.' if path.is_file() else f'{rel} is missing.'))
    return rows


# The complete tool grant every read-only discovery worker is allowed to hold.
#
# An ALLOWLIST, not a blacklist. A blacklist can only refuse the capabilities
# somebody thought to name, and the first version proved it: it rejected Read,
# Write and Bash but silently permitted WebFetch and browser automation, which are
# exactly the two the trust boundary depends on excluding. `url_safety.py` is owned
# by the parent, so a worker holding a fetch tool could be steered by untrusted
# page text into a target that never passed the gate, leaving the gate in place
# while routing around it. A new tool invented next year is refused by default here
# and has to be authorised deliberately.
WORKER_ALLOWED_TOOLS = frozenset({'WebSearch'})
WORKER_CONTRACTS = ('.claude/agents/public-job-researcher.md',
                    '.claude/agents/sponsor-verifier.md')


def parse_agent_tools(text):
    """The tool grant declared in an agent's YAML frontmatter.

    Returns `(tools, found)`. `found` distinguishes "declared nothing" from "no
    `tools:` key at all", because in Claude Code an absent key means the agent
    INHERITS the full tool set, which is the most permissive outcome of the three
    and must never be mistaken for an empty grant.

    Only the frontmatter block is read. Prose below it that happens to mention a
    tool name is documentation, not capability, and this must not confuse the two.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return [], False
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == '---':
            break
        if not line.startswith('tools:'):
            continue
        raw = line.split(':', 1)[1].strip()
        if raw.startswith('[') and raw.endswith(']'):
            raw = raw[1:-1]
        if raw:
            return [t.strip().strip('"\'') for t in raw.split(',') if t.strip()], True
        # A block list: `tools:` followed by `  - WebSearch` rows.
        collected = []
        for follow in lines[index + 1:]:
            if follow.strip() == '---':
                break
            stripped = follow.strip()
            if stripped.startswith('-'):
                collected.append(stripped[1:].strip().strip('"\''))
            elif stripped:
                break
        return collected, True
    return [], False


def check_worker_privileges():
    """Workers must hold EXACTLY the tools their contract allows, and nothing else.

    This is the one privilege boundary this repository can actually enforce: the
    agent `tools:` frontmatter is a real Claude Code mechanism, so a worker granted
    Read could open the candidate profile whatever its prose promised, and one
    granted WebFetch could follow a URL the parent never gated.
    """
    rows = []
    for rel in WORKER_CONTRACTS:
        path = ROOT / rel
        name = Path(rel).stem
        if not path.is_file():
            rows.append(check(f'worker_privileges:{name}', 'fatal', False,
                              f'Worker contract missing: {rel}'))
            continue
        granted, declared = parse_agent_tools(path.read_text(encoding='utf-8'))
        allowed = sorted(WORKER_ALLOWED_TOOLS)
        if not declared:
            rows.append(check(f'worker_privileges:{name}', 'fatal', False,
                              'No `tools:` key in the frontmatter, so this worker INHERITS '
                              'the full tool set, including filesystem, shell and fetch '
                              f'capability. Declare exactly: tools: {", ".join(allowed)}',
                              granted=[], allowed=allowed))
            continue
        extra = sorted(set(granted) - WORKER_ALLOWED_TOOLS)
        missing = sorted(WORKER_ALLOWED_TOOLS - set(granted))
        if extra:
            detail = (f'Worker holds capability outside its contract: {extra}. '
                      'A worker with file or shell access can read the private candidate '
                      'profile, and one with fetch or browser access can follow a target '
                      'that never passed tools/url_safety.py. '
                      f'The allowed grant is exactly: {allowed}')
        elif missing:
            detail = (f'Worker is missing the tools it needs: {missing}. '
                      f'The required grant is exactly: {allowed}')
        else:
            detail = f'Worker holds exactly {allowed} and nothing else.'
        rows.append(check(f'worker_privileges:{name}', 'fatal',
                          not extra and not missing, detail,
                          granted=sorted(granted), allowed=allowed))
    return rows


def check_runtime_dirs():
    """Every runtime directory must be usable, WITHOUT preflight creating anything.

    A cycle that discovers a read-only state directory halfway through has already
    done the expensive work, so this is worth checking early. But preflight is a
    read-only gate: it must not leave `job_scraper/runs/` and `job_scraper/cache/`
    behind as a side effect of asking whether they could be written. A directory
    that does not exist yet is judged by whether its nearest existing ancestor is
    writable, which is the same question without the side effect.
    """
    rows = []
    for rel in RUNTIME_DIRS:
        path = ROOT / rel
        existed = path.is_dir()
        probe_dir = path if existed else next(
            (parent for parent in path.parents if parent.is_dir()), ROOT)
        try:
            probe = probe_dir / '.preflight-write-probe'
            probe.write_text('probe', encoding='utf-8')
            probe.unlink()
            rows.append(check(f'writable:{rel}', 'fatal', True,
                              'Writable.' if existed else
                              f'Not yet created; {probe_dir.name}/ is writable so the run '
                              'can create it.',
                              exists=existed))
        except OSError as exc:
            rows.append(check(f'writable:{rel}', 'fatal', False,
                              f'{rel} is not writable: {type(exc).__name__}: {exc}',
                              exists=existed))
    return rows


def check_application_surface():
    """No operational instruction anywhere may perform an application action.

    The distinction is between documentation FORBIDDING an action and an
    instruction TELLING an agent to take one. The former is exactly what a safety
    boundary looks like and must not trip this check.
    """
    try:
        from application_audit import audit_workspace
    except ImportError:
        return check('application_surface', 'fatal', False,
                     'tools/application_audit.py is missing, so the product boundary '
                     'cannot be verified.')
    report = audit_workspace(ROOT)
    clean = not report['violations']
    return check('application_surface', 'fatal', clean,
                 'No application-automation surface found. The product still stops at '
                 'the shortlist.' if clean else
                 'An operational instruction could perform an application action.',
                 violations=report['violations'][:5],
                 files_scanned=report['files_scanned'])


def run_preflight():
    rows = []
    rows.append(check_state())
    rows.append(check_candidate_config())
    rows.append(check_matching_policy())
    rows.append(check_source_registry())
    rows.append(check_search_strategy())
    rows.append(check_immigration_reference())
    rows.append(check_experience_calibration())
    rows.append(check_sponsor_snapshot())
    rows.extend(check_required_files())
    rows.extend(check_worker_privileges())
    rows.extend(check_runtime_dirs())
    rows.append(check_application_surface())

    fatal = [r for r in rows if not r['ok'] and r['severity'] == 'fatal']
    warnings = [r for r in rows if not r['ok'] and r['severity'] == 'warning']
    status = 'NOT_READY' if fatal else ('READY_WITH_WARNINGS' if warnings else 'READY')
    return {
        'status': status,
        'checks_run': len(rows),
        'passed': sum(1 for r in rows if r['ok']),
        'fatal': [{'check': r['check'], 'detail': r['detail']} for r in fatal],
        'warnings': [{'check': r['check'], 'detail': r['detail']} for r in warnings],
        'checks': rows,
        'searched_anything': False,
        'wrote_anything': False,
        'note': {
            'READY': 'Every gate passed. A controlled live cycle may proceed.',
            'READY_WITH_WARNINGS': 'Nothing is broken. Something is degraded in a way the '
                                   'run itself can recover from, most often a stale or '
                                   'missing sponsor snapshot that /scrape will refresh.',
            'NOT_READY': 'A gate protecting real data failed. Do not run a live cycle '
                         'until it is resolved.',
        }[status],
    }


def cmd_check(args):
    report = run_preflight()
    if not args.verbose:
        report.pop('checks', None)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(0 if report['status'] != 'NOT_READY' else 1)


def _force_utf8_stdout():
    """Vacancy text is not cp1252, and a Windows console is.

    Real adverts carry emoji and arrows. Printing one through a cp1252 console
    raised UnicodeEncodeError and killed the process mid-ranking. The DATA is
    fine; only the stream is wrong, so fix the stream rather than the text.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if (getattr(stream, 'encoding', '') or '').lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass


def main():
    _force_utf8_stdout()
    p = argparse.ArgumentParser(
        description='Deterministic pre-live readiness gate. Searches nothing, writes nothing.')
    p.add_argument('--verbose', action='store_true', help='Include every individual check.')
    p.set_defaults(func=cmd_check)
    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()

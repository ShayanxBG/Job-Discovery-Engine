---
description: Deliberate, narrow reset of seen-job state only
---
# /reset-discovery

Use this only when the user explicitly wants a clean discovery baseline.

This command is deliberately NARROW: it clears the seen-job list and nothing else, so the JD cache, suppression, run logs and shortlist history all keep working. If the user wants the COMPLETE active search state cleared before starting a real search, that is a different command: `python tools/reset_production.py --dry-run` to show the plan, then `--confirm` to archive, verify and clear. Do not widen this one to mean that.

1. Explain that this resets only `job_scraper/seen_jobs.json`.
2. It must NOT alter historical shortlist snapshots, `candidate/profile.md`, or the master CV/PDF/JSON.
3. Run `python tools/job_state.py reset`.
4. Report the number of seen jobs removed and the backup path returned by the tool.
5. Run `python tools/validate_workspace.py --deep` immediately after the reset.
6. If validation passes, confirm that the next `/scrape` will intentionally rediscover previously seen vacancies once.
7. Do not delete the reset backup.

Never run this command automatically or as part of normal `/scrape`.

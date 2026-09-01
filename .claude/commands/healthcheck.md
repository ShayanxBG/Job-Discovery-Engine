---
description: Read-only workspace validation
---
# /healthcheck - Validate the discovery workspace

1. Run `python tools/validate_workspace.py --deep`.
2. Report the exact passed/failed counts.
3. If it passes, confirm the core discovery/matching files, private candidate profile, master CV, sponsor subset, shortlist history logic, deduplication/reset helper, lead categories, read-only subagents, product boundary, and source strategy are healthy.
4. If it fails, report the failing checks and do not claim the workspace is healthy.
5. Remind the user that authenticated browser-source health is tested separately with `/scrape health browser`.
6. Discovery-state health is reported separately by the read-only `python tools/job_state.py doctor`. Run it when the user asks about state integrity, and report what it finds.

Do not edit files during `/healthcheck` unless the user explicitly asks to repair a failure. Never run `job_state.py doctor --repair` as part of `/healthcheck`; repairing discovery state is always an explicit user decision.

---
description: Retrieves immutable ranking snapshots, including historical days
argument-hint: "[latest | today | YYYY-MM-DD | all]"
---
# /shortlist - Read saved ranking history

Input is `$ARGUMENTS`.

This command is read-only. It must never search the web, open the browser, re-rank jobs, change `seen_jobs.json`, or modify a saved shortlist.

Modes:

- no argument or `latest`: run `python tools/shortlist.py show`
- `today`: run `python tools/shortlist.py show --date today`
- `YYYY-MM-DD`: run `python tools/shortlist.py show --date YYYY-MM-DD`
- `all`: run `python tools/shortlist.py show --all`

Present the saved result as historical decision support. Do not silently refresh vacancy facts because that would rewrite the meaning of the saved ranking. If the user wants current market information, tell them to run `/scrape`.

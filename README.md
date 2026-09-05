# Job Discovery Engine

A UK-focused job discovery, verification, matching, ranking, and shortlist workspace. Claude Code is the current execution environment.

The product boundary is intentionally narrow:

```text
discover -> verify -> match -> rank -> shortlist -> human decides
```

It does **not** auto-apply, tailor application packages, contact recruiters, fill forms, or submit applications.

## What it does

- Searches authenticated job platforms through the user's existing browser session when available.
- Searches employer career sites, ATS platforms, public job boards, and sponsorship-focused lead sources.
- Chooses its search window from RUN HISTORY, not from how many roles it found.
- Resolves strong aggregator/board leads to employer-owned sources where possible.
- Deduplicates the same vacancy across multiple sources while preserving separate requisitions.
- Tracks previously seen jobs so daily runs focus on new or materially updated opportunities.
- Filters unrealistic seniority, experience, contract, and wrong-specialism matches early.
- Verifies sponsorship and salary evidence when they affect the decision.
- Scores viable direct-employer roles against the candidate's private profile.
- Keeps Direct, Verification, Agency, and Updated leads separate.
- Saves immutable daily shortlist history that survives terminal or machine restarts.

## Browser model

The project should use the user's **existing authenticated browser session**, not store account passwords.

Browser-assisted discovery is read-only. If a site presents a CAPTCHA, anti-bot verification, or account challenge, the system stops for manual user action or marks the source incomplete. It does not bypass protections.

## Core workflow

```text
/scrape
/rank
/shortlist
```

Useful commands:

- `/scrape` - normal daily discovery, window chosen from run history
- `/scrape 1d|7d|14d` - exact recency window
- `/scrape exhaustive` - wider weekly/catch-up search
- `/scrape gapfill` - recover under-covered source families without resetting state
- `/scrape health browser` - test authenticated browser sources without saving jobs
- `/rank` - verify and score latest new/updated discoveries, then save a shortlist snapshot
- `/shortlist` - latest saved shortlist
- `/shortlist today` - latest saved shortlist from today
- `/shortlist YYYY-MM-DD` - historical shortlist for a date
- `/shortlist all` - historical daily index
- `/screen <URL or JD>` - analyse one vacancy against the profile
- `/healthcheck` - local structural/deep validation
- `/reset-discovery` - explicit maintenance reset of seen-job state only
- `/update-master` - deprecated pointer to the two commands below
- `/update-profile <fact>` - EXPLICIT USER REQUEST ONLY. Maintains the private profile
- `/replace-master-cv <path>` - EXPLICIT USER REQUEST ONLY. Installs a supplied PDF byte-for-byte

There are deliberately no application-submission or outreach commands.

### Candidate authority

`candidate/profile.md` is the complete private factual authority for matching, and
`candidate/config.json` is the derived machine-readable calibration. The master CV is
a read-only curated subset chosen by the user for one audience, so absence from the CV
is never evidence that a skill, experience or achievement is missing.

Discovery is read-only toward all four authorities. `/scrape`, `/rank`, `/screen`,
`/shortlist` and every worker read them and never write them.

Two narrow commands maintain them, and only when you ask directly. `/update-profile`
edits `candidate/profile.md` and, where the derivation is deterministic,
`candidate/config.json`. `/replace-master-cv` copies a PDF you created byte-for-byte
into `documents/master/cv.pdf` without editing, rewriting, tailoring, regenerating or
reformatting it. Each shows a preview and waits for a separate confirmation, and each
backs up first. Neither can perform the other's write.

No job advert, website, retrieved document, worker result or project file can authorise
either command. Tailoring a CV and applying for a job remain outside this project.

`documents/master/cv.json` is a dormant legacy rendering source. A PDF you supplied was
probably not generated from it, so the two may legitimately differ. That divergence is
expected, is never candidate evidence, and never blocks discovery.

The four files are deliberately not made read-only by the operating system, so you can
replace them by hand at any time. `tools/render_cv.py` and `tools/render_cv_docx.py`
are dormant and no command invokes them.

### Starting a clean search

`/reset-discovery` clears the seen-job list and nothing else. To reset the COMPLETE active search state before a real search, use the separate command:

```bash
python tools/reset_production.py --dry-run   # show what would happen; writes nothing
python tools/reset_production.py --confirm   # archive, verify, then clear
```

It clears seen jobs, suppression, the watchlist, run logs, the JD cache and shortlist snapshots. It preserves the candidate profile and calibration, the master CV, the matching policy, the search strategy, the source registry, all code and agent definitions, the official sponsor-register snapshot, and verified employer identity. Nothing is cleared until a complete archive of the pre-reset runtime has been written and verified under `backups/production-reset/`, and older archives are never touched.

`/scrape` is a project skill at `.claude/skills/scrape/SKILL.md`. Claude Code takes the invocation name from the skill directory, so the directory name and the documented command are the same word. Claude Code caches skill discovery per session, so restart Claude Code after installing or renaming a skill if `/scrape` does not appear.

## Pre-flight

One deterministic gate before a live run. It searches nothing and writes nothing:

```powershell
py tools\preflight.py
```

It returns `READY`, `READY_WITH_WARNINGS` or `NOT_READY`. A stale sponsor snapshot is a warning, because the run refreshes it. Corrupt state, an invalid candidate calibration or a broken matching policy is `NOT_READY`.

External URLs are gated separately, because every one of them arrives from untrusted content:

```powershell
py tools\url_safety.py check "https://boards.greenhouse.io/example/jobs/1"
```

Only https (and http for a legitimate source) is fetched. `file:`, `data:`, `javascript:` and similar schemes are refused, as are localhost, private and link-local addresses, and URLs embedding credentials. It is an input gate, not network security.

## State health

Discovery-state writes are atomic and backed by a bounded backup set, so an interrupted write cannot destroy discovery history:

```powershell
py tools\job_state.py doctor
```

`doctor` is read-only. It reports parse health, schema shape, record count, controlled-vocabulary violations, duplicate/origin-key issues and the recovery backups available. Repair is a separate explicit action, `doctor --repair`, which restores only a validated backup and preserves the damaged file first.

## Private candidate data

The current local workspace uses:

- `candidate/profile.md`
- `documents/master/`
- `job_scraper/seen_jobs.json`
- `job_scraper/shortlists/`
- `job_scraper/runs/`
- `job_scraper/cache/`
- `job_scraper/suppression.json`
- `job_scraper/employers.json`
- `job_scraper/sponsorship_evidence.json`
- `job_scraper/watchlist.json`
- `job_scraper/reference/`
- `candidate/config.json`

These paths all hold private data, and they are NOT all gitignored. That is deliberate. `candidate/profile.md`, `candidate/config.json`, `candidate/cv-maintenance.md`, both files under `documents/master/`, and the `job_scraper/` state files (`seen_jobs.json`, `suppression.json`, `employers.json`, `sponsorship_evidence.json`, `watchlist.json`) are TRACKED in this strictly local Git repository, so a bad edit to an authority can be recovered. A checkpoint that excluded the authorities would not protect the work it exists to protect.

The rest are gitignored: `job_scraper/runs/`, `job_scraper/shortlists/`, `job_scraper/cache/`, `job_scraper/reference/`, `reports/`, `backups/`, `documents/master/history/`, `candidate/config.proposed.json` and `.claude/settings.local.json`.

The repository has NO remote and must not gain one casually. Because the authorities are tracked, this history cannot be published as it stands. See **Privacy and GitHub** below before sharing anything.

`config/sources.json`, `config/search_strategy.json` and `config/matching_policy.json` are deliberately NOT private. They describe discovery sources, search methods and evaluation policy only, and hold no credentials, cookies, account names or candidate data.

For a reusable/public setup, start from `candidate/profile.example.md` and create your own local `candidate/profile.md`.

## Discovery sources

A normal deep run can combine:

1. Authenticated LinkedIn Jobs
2. Authenticated Indeed UK
3. CWJobs / Totaljobs through the browser
4. Employer career pages and ATS platforms
5. Public LinkedIn/Indeed fallback
6. Other UK boards such as Reed, DWP/Work Hub, Built In, Welcome to the Jungle, JobServe, and Technojobs
7. Sponsorship-focused sources used as leads, followed by employer/GOV.UK verification

Source identity and inventory family are defined once in `config/sources.json`:

```powershell
py tools\sources.py list
py tools\sources.py families
```

Coverage diversity is counted by inventory family, not by site name. CWJobs and Totaljobs run on one StepStone platform and share a family, so searching both is one family rather than two.

Source coverage is reported explicitly using a controlled outcome vocabulary: `ok`, `empty`, `partial`, `blocked_captcha`, `blocked_permission`, `changed_layout`, `timeout`, `unavailable`, `error`. A blocked or unreadable source must not be counted as successfully searched, and `empty` (the source genuinely held nothing) is never used for a source that broke. Each run keeps a private coverage log:

```powershell
py tools\discovery_run.py show
```

Coverage is judged by inventory family. A run is PARTIAL when an attempted family has no successful source, so a short candidate list is not mistaken for a quiet market. When one site in a family fails but a sibling covered the same inventory (CWJobs succeeding while Totaljobs breaks), the run is COMPLETE_WITH_WARNINGS: the failure stays visible as a source warning, and it creates no gap-fill work, because that inventory was still searched.

A run reports completion as three separate facts, so a fully covered run never looks incomplete because one sibling site broke. `finished` says only whether the cycle closed. `family_coverage_complete` says whether every attempted family was seen. `complete` is aligned with family coverage and means both: COMPLETE and COMPLETE_WITH_WARNINGS are complete, PARTIAL is not, and an unfinished run never is.

## Search strategy and query budget

`config/search_strategy.json` defines HOW the workspace searches: the query families, their budgets, the term slots each fills, and the stopping rules. It is publishable and holds no candidate values.

```powershell
py tools\search_strategy.py list
py tools\search_plan.py plan --mode deep --window 24h
```

A SEARCH family is not a SOURCE family. Ten variations of "Python Developer" across five boards is five source families and one search family: broad source coverage, narrow query coverage. Both are reported separately, because a thin result from narrow queries and a thin result from a collapsed source need different fixes. The families are direct-title, backend-capability, adjacent-software, early-career, employer-ats, sponsorship-oriented and gapfill.

`tools/search_profile.py` derives a compact set of search TERMS from the private candidate profile (titles, languages, frameworks, capabilities, exclusions) and refuses to emit identity, contact, date or right-to-work content. Workers receive those terms; the profile itself never leaves the main agent.

The planner deduplicates semantically equivalent queries, so "Python Backend Engineer" and "Backend Engineer Python" never both consume budget for the same source. Budgets are counted in queries, candidates and new canonical yield, never in wall-clock time. Stopping is deterministic: `CONTINUE`, `SATURATED`, `BUDGET_EXHAUSTED` or `GAP_REMAINS`. One empty query never saturates a family, and a failed source is lost coverage rather than zero yield.

## Employer, sponsorship and watchlist caches

```powershell
py tools\employers.py resolve "Example Ltd"
py tools\sponsorship_evidence.py get --employer "Example Ltd"
py tools\watchlist.py due --limit 10
```

The employer cache records what an employer is: aliases, domain, careers URL, ATS platform and tenant. Resolution is conservative: exact, legal-suffix, explicit alias or domain evidence resolve, and a weak substring never merges two employers automatically.

Sponsorship is stored as dated evidence with provenance, not a boolean. Status is derived from unexpired evidence, so it falls back to unknown when support expires. A sponsor-register hit means the organisation holds a licence; it is never evidence that a particular vacancy will be sponsored, and it always requires a live check before a decision-critical recommendation.

The employer watchlist is bounded at 60 active employers. Enumerating the whole sponsor register against ATS platforms is explicitly not what this does.

## Official sponsor register snapshot

```powershell
py tools\sponsor_register.py status
py tools\sponsor_register.py refresh --allow-network
py tools\sponsor_register.py check "Example Ltd"
```

`tools/sponsor_register.py` keeps a validated local snapshot of the official GOV.UK *Register of licensed sponsors: workers*, so a licence check is a dictionary lookup rather than a web search. The current CSV is discovered from the GOV.UK publication instead of a pinned dated URL, only official GOV.UK hosts are accepted, and the snapshot lives in the private `job_scraper/reference/` directory.

Freshness target is 24 hours, with one refresh attempt per discovery run. A download must pass validation (parseable CSV, not an HTML error page, organisation column present, plausible row count) before it can replace the current snapshot, so a bad download never destroys a good one. If a refresh fails and a snapshot exists it is retained and marked stale; if none exists, lookups return `UNAVAILABLE`. A GOV.UK outage does not break a run.

Lookups return `FOUND`, `NOT_FOUND`, `AMBIGUOUS` or `UNAVAILABLE`, using exact, legal-suffix, alias or recorded registered-name matching. Substring matching is absent by construction. `NOT_FOUND` means only that no credible match exists in that snapshot under the names we know: registered legal names routinely differ from trading names, so it never means the employer cannot sponsor.

### The tech subset is supplementary

`data/uksponsorregistertechsubset20260812.csv` remains, and `tools/check_sponsor.py` still queries it. It is a dated 2026-08-12, filtered tech/consultancy subset with no visa-route column: useful for spotting that a company is plausibly licensed, but not the official register, and absence from it proves nothing.

## Job description cache

`job_scraper/cache/` stops the same posting being fetched and re-interpreted repeatedly inside one discovery/ranking cycle:

```powershell
py tools\job_cache.py stats
```

It keeps four separate clocks, because conflating them lets an unrelated write make stale evidence look fresh. `cached_at` is the file-write time and never decides reuse. `description_fetched_at` moves only when a write actually supplies a description. `facts_fetched_at` moves only when a write actually supplies structured facts. `open_status_checked_at` moves only when a write supplies an open/closed observation.

Description and facts are two evidence classes, not one, so `get` reports `description_fresh`, `facts_fresh`, `reuse_description` and `reuse_facts` independently. Refreshing one class never refreshes the other, and a metadata-only write refreshes neither. Reuse inside a run follows the same rule: description/facts actually fetched by this run are reusable for that evidence class, while a metadata-only or open-status-only write by the current run grants no reuse.

A cached open/closed observation is never a substitute for a live check before presenting a high-priority recommendation.

## Matching model

Two files own the mechanics. `config/matching_policy.json` is publishable and defines HOW evaluation works; `candidate/config.json` is private and defines this candidate's calibration, derived from `candidate/profile.md` by `tools/candidate_config.py`.

```powershell
py tools\candidate_config.py build
py tools\candidate_config.py validate
py tools\match_evaluation.py validate-policy
```

The model proposes a structured evaluation; `tools/match_evaluation.py` validates it against policy and computes the total, the band and eligibility. It rejects an out-of-range component rather than quietly correcting it. A hard blocker sets `eligible: false` without destroying the diagnostic component scores.

Direct Matches use a 100-point model:

- Technical stack/day-to-day work: 40
- Seniority/commercial experience: 15
- Sponsorship viability: 25
- Salary/contract/work pattern: 10
- Company/domain environment: 10

Current interpretation:

- 90-100: Exceptional Match
- 80-89: Strong Match, and the default full-tailoring threshold
- 70-79: Viable Match
- 65-69: Borderline Review, kept visible for human review during the pilot
- below 65: Below Threshold, deprioritised but never deleted

The boundaries are a pilot calibration pending production outcomes and do not predict interviews.
- hard blockers override the score

Location inside the UK is not a score penalty in the current profile.

## Shortlist history

Every successful `/rank` run creates an immutable snapshot under `job_scraper/shortlists/`. Multiple runs on the same day are preserved separately and date lookup selects the latest run for that day.

Shortlists report Exceptional, Strong and Viable as separate sections. A ranking run that scored only part of the matching set records that it was partial, so a truncated run is never presented as complete coverage.

Closing PowerShell or restarting Claude Code does not lose shortlist history.

## Setup

Install dependencies:

```powershell
py -m pip install -r requirements.txt
```

Validate the workspace:

```powershell
py tools\validate_workspace.py --deep
```

Start Claude Code with browser integration:

```powershell
claude --chrome
```

When Chrome integration is unavailable, plain `claude` is the supported alternative. Everything works except authenticated browser discovery, which reports itself as unavailable rather than failing silently:

```powershell
claude
```

Then run:

```text
/healthcheck
/scrape health browser
```

## How a run decides what to search

### The window comes from run history, never from yield

```text
python tools/search_window.py select
```

| Decision | When | Window | Budget mode |
| --- | --- | --- | --- |
| `INITIAL_CATCHUP` | no successful completed production run exists | 14d, in ONE pass | initial_catchup |
| `DAILY` | the gap is 24 hours or less | 24h | daily |
| `RECOVERY` | the gap is more than 24 hours | smallest window that COVERS it | catchup |
| `EXPLICIT` | the user asked for `1d`, `7d` or `14d` | exactly that | unchanged |

The rule this replaced searched 24 hours, counted new Direct matches, and widened to 7 then 14 days when it found fewer than six then fewer than four. That inferred a coverage failure from a quiet market, and it spent three query budgets covering one window three times. Yield is market supply: a day on which UK employers posted three matching vacancies is a quiet day, not a badly searched one.

Exact boundaries, with inclusive upper bounds:

| Gap since the last successful run | Window | Decision |
| --- | --- | --- |
| up to and including 24h | 24h | DAILY |
| 24h and one minute, up to and including 7d | 7d | RECOVERY |
| 7d and one minute, up to and including 14d | 14d | RECOVERY |
| more than 14d | 14d, capped | RECOVERY, uncovered hours reported |

`python tools/search_window.py boundaries` probes every one of them.

There is deliberately no timing grace. An earlier version allowed twelve hours of slack, so a 30-hour gap was searched with a 24-hour window and six hours of inventory went unsearched with nothing reporting it. That is the same invisible coverage failure that yield-based widening was removed for, arriving through the other door. Re-searching overlap costs nothing because deduplication absorbs it; a shortfall cannot be recovered, because nobody knows it happened.

Every decision carries `coverage.covers_gap` and `coverage.uncovered_hours`. When the gap exceeds 14 days the run does NOT achieve full historical coverage: the shortfall is reported in hours and days, and the decision carries recovery advice rather than describing the run as complete.

A run is evidence of coverage only if it FINISHED, was not PARTIAL, and ran in a production mode. A health check searches nothing and a `quick` troubleshooting sample is not a day of coverage, so neither resets the clock.

`config/search_strategy.json` carries `window_policy.yield_may_widen_window: false`, and the validator refuses any other value.

### Budgets by mode

| Mode | Queries | Raw candidates | Deep JD reads | Employer ATS checks |
| --- | --- | --- | --- | --- |
| `quick` | 12 | 120 | 20 | 2 |
| `gapfill` | 18 | 180 | 30 | 6 |
| `daily` | 30 | 260 | 45 | 8 |
| `deep` | 36 | 400 | 70 | 10 |
| `catchup` | 36 | 400 | 70 | 12 |
| `exhaustive` | 60 | 700 | 120 | 20 |

Employer ATS checks are bounded separately from web queries, so a busy watchlist cannot eat board coverage and a quiet one cannot fund extra board queries. A ceiling is a limit, not a quota: stopping early because a family is saturated is a correct outcome.

### The ATS ceiling is enforced, not reported

```text
python tools/ats_budget.py tasks --run-id <run_id>
python tools/ats_budget.py outcome <employer_key> --run-id <run_id> [--failed]
python tools/ats_budget.py status --run-id <run_id>
```

Capacity is RESERVED before each external check, not counted after it. `tasks` writes the increased reservation to the run record and only then returns the employers, so there is no path that performs a check without holding a slot, and `--limit` can only make the list shorter. A caller asking for a hundred tasks in daily mode receives eight.

### One production run at a time, atomically

`discovery_run.py begin` claims `job_scraper/runs/.active-run.json` with a single `os.open(..., O_CREAT | O_EXCL)`, which is one atomic filesystem operation on both Windows and POSIX. An earlier version read the lock and then wrote it; ten simultaneous calls would all have found it absent and all believed themselves the winner. A guard made of a check followed by a write is not a guard.

Measured: ten processes released from a barrier, five times over. Exactly one won each round, nine were refused, and **no losing process wrote a run record or an ATS ledger**, because the lock is taken before anything else.

- Only the **owner** may release during normal completion; a non-owner release is refused by name.
- An **unreadable** lock fails closed and is reported as held-and-stale: corruption must not be a way past the guard.
- A lock pointing at an **already finished** run is stale bookkeeping and self-heals.
- An **abandoned** lock is never discarded automatically. `discovery_run.py release --run-id <id> --reason <why>` is explicit and recorded.
- `health` and every read-only command stay usable while it is held.

- A FAILED check still spends its slot. The ceiling bounds external work, not successes; refunding failures would let one dead careers page consume a whole run one retry at a time.
- Reaching the ceiling is `stop_reason: ceiling_reached`, a normal bounded stop. Deferred employers stay enabled and stay due for the next run, and nothing is recorded as a source failure.
- Closing a run refuses to store a ledger that does not reconcile: `reserved` may not exceed the ceiling, `attempted` may not exceed `reserved`, and `succeeded + failed` must equal `attempted`.
- A run recorded before the ledger existed has none, is not accused of a breach, and still reads correctly.

### Temporal coverage: the unit is a bucket, not a website

Rotation proves a bucket comes back. It says nothing about what interval the returning query covers.

Phase 4C fixed half of this and got the unit wrong. It used the inventory family and argued that a board holds one inventory, so a second title could not cover an interval the first did not. The premise is true; the conclusion is not. A board holds one inventory and FILTERS its results by query text. Searching `Integration Developer` on LinkedIn proves nothing about `Python Django` on LinkedIn, and calling the second one "supplemental" because it shares a website is precisely how the gap stayed hidden.

The unit is a **coverage bucket**:

```text
{inventory_family}::{search_family}::{term_cluster}
```

All three change what comes back. The family decides which inventory is searched; the search family is the intent (title, capability, early career, sponsorship); and within an intent, terms that are not substitutes find different adverts.

```text
python tools/coverage_ledger.py buckets
python tools/coverage_ledger.py checkpoints
python tools/coverage_ledger.py windows --window 24h
```

**Clustering keeps it affordable.** Board search is conjunctive over significant tokens, so the results of `Python Backend Developer` are a subset of `Python Developer`: running the shorter query searches the longer one's interval too. Terms whose token sets nest share a cluster with the shortest as its anchor. The relationship is computed from the token sets, never from a hand-written table, and it never crosses a board or an intent. The assumption is stated rather than buried: a broad query subsumes a narrow one only if its result list was not truncated first.

**Only a completed query advances a bucket.** Not a source outcome, because a board answering one question says nothing about another. Not a plan that named the bucket, because planning is not searching. Not a failed query, even beside a successful sibling on the same board. Not a partial, unfinished, health or deferred run.

### The first run: a derived one-time bootstrap

```text
python tools/coverage_ledger.py bootstrap
python tools/coverage_ledger.py feasibility
```

The first production run uses `initial_catchup`, whose budget is derived rather than picked:

| Component | Queries |
| --- | --- |
| critical obligations | 45 |
| minimum family coverage (one route per family with no critical work) | 7 |
| event-driven startup (employer ATS) | 2 |
| bounded exploratory allowance | 4 |
| **total** | **58** |

Raw candidates 644 and deep reads 113, scaled from the catch-up mode's own ratios. Employer ATS stays at 12. HISTORICAL MEASUREMENT, not current authority: when this was written the critical tier held 45 buckets and the plan funded 45 of 45 with zero deferred, across all four intent classes, 13 of 13 inventory families and a 14-day window. The denominator has since changed twice (source-capability corrections on 2026-08-31), so read the live number from `python tools/coverage_ledger.py denominators` and never from this paragraph. The ordinary daily ceiling of 30 is untouched and 58 is well below exhaustive at 100.

Completion is derived, not asserted: initial catch-up is complete only when every critical bucket carries a successful checkpoint. A partial run, a failed critical query and an unfunded bucket each leave it incomplete, and a bucket first searched days later carries the date it was actually searched, so it cannot retroactively claim the interval the first run was meant to cover.

### Global earliest-deadline-first allocation

Per-family allocation was a defect. Each family planned inside its own budget, so a bucket with an early deadline in one family could not borrow a slot another was not using: at a 30-hour cadence `direct-title` needs eight critical slots per run and has exactly eight in total, leaving nothing for its five rolling buckets. No ordering inside a family fixes a shortage between families.

Every candidate is now ranked globally, in this order:

1. past the 14-day cap
2. breached, most overdue first
3. will breach before the next run, earliest deadline first
4. remaining `critical_fresh`, smallest slack
5. remaining `rolling_recall`, smallest slack
6. a route into an inventory family nothing else has reached
7. event-driven work holding its own reservation
8. exploratory and supplemental
9. deterministic term and source tie-breaks

Term and source diversity are tie-breaks, not barriers. Per-family budgets are soft preferences; the global query budget, the event-driven reservation and the ATS ceiling stay hard. Urgent mandatory work borrows from exploratory allocation, unused family allocation and non-urgent rolling capacity.

### Feasibility is deadline-safe, not average

```text
max_intervals    = floor(target_revisit_hours / cadence_hours)
required_uniques = ceil(bucket_count / max_intervals)
```

Average capacity is necessary and not sufficient with discrete runs: a tier needing 4.0 slots on average still breaches if the schedule hands it 3 twice in a row.

| Cadence | Tier | Intervals | Unique slots needed | Available | Safe |
| --- | --- | --- | --- | --- | --- |
| 24h | critical | 3 | 15 | 18 | yes |
| 24h | rolling | 7 | 4 | 8 | yes |
| 30h | critical | 2 | 23 | 23 | yes |
| 30h | rolling | 5 | 6 | 9 | yes |

Both figures are reported separately, and a policy may claim a hard maximum revisit only when the deadline-safe check passes.

### Deadlines, not just ages

Every mandatory bucket derives `deadline_at`, `current_age_hours`, `slack_hours`, `overdue_hours`, `predicted_age_at_next_normal_run` and an urgency. Critical work is ordered by **slack** before raw debt, and a bucket predicted to breach before the next run outranks the rolling quota.

Capacity feasibility is arithmetic that validation enforces:

```text
critical_buckets * cadence_hours / target_revisit_hours <= critical_slots_per_run
```

At 24 hours: 45 buckets need 15 slots, 18 are available. A policy promising a freshness its budget cannot deliver is refused.

**Two service levels, measured and reported separately.** The strict standard is STANDARD DAILY: a run interval up to and including 24 hours, holding 72 hours critical and 168 hours rolling. An interval above 24 and up to 30 hours is DELAYED DAILY, held to a measured tolerance of 90 hours critical and 180 hours rolling. A target derived from a 24-hour interval is not one a 30-hour schedule was ever going to meet, because only two intervals fit inside 72 hours at that cadence. A tolerance pass is reported as a tolerance pass, never as the strict standard being met, and the shortfall is reported in hours. Gaps above 30 hours stay on the recovery-window logic.

Both cadences are supported and neither is labelled degraded. The earlier degraded label described a slot-distribution defect between families, and global allocation fixed the defect rather than accepting it. The service levels live in `config/search_strategy.json` under `deadlines.service_levels`.

### Service tiers: a bucket existing is not a promise to search it daily

Phase 4D classified all 173 term-by-source combinations as equally required. A daily run funded 24 and deferred 81; a mode named `exhaustive` funded 33 while deferring 140 it called mandatory; and `backend-capability` and `early-career` buckets could wait eleven days. That optimises a Cartesian product rather than timely discovery.

Every bucket now carries exactly one tier and the reason it has it, derived from structure the workspace already declares: whether the source registry says that intent is productive there, which query template the term came from, and whether the inventory family is primary or secondary.

| Tier | Buckets | Owes an interval | Target revisit |
| --- | --- | --- | --- |
| `critical_fresh` | 45 | yes | 72 hours |
| `rolling_recall` | 28 | yes | 7 days |
| `exploratory` | 144 | no | none |
| `watchlist_or_event_driven` | 6 | no | its own ceiling |

`critical_fresh` holds 15 direct-title, 15 core backend-capability, 12 early-career and 3 sponsorship buckets on the primary inventory families. `rolling_recall` reaches every secondary board through one representative route per applicable intent, plus the capability refinements beyond the critical three. Everything else is exploratory: still executed when budget allows, still recorded, and structurally unable to advance a checkpoint it does not own.

**73 buckets owe an interval**, down from 173, over the eleven inventory-owning families. A search engine indexes other people's boards, so it earns no bucket of its own, and employer/ATS sources answer to the watchlist ceiling rather than to a clock; both are still searched.

Measured revisit intervals under consecutive standard daily operation, all four critical intent classes:

| Class | Worst revisit | Strict target |
| --- | --- | --- |
| critical direct-title | 72h | 72h |
| critical backend-capability | 72h | 72h |
| critical early-career | 72h | 72h |
| critical sponsorship-oriented | 72h | 72h |
| rolling (all classes) | 168h | 168h |

Nothing was reduced to make a count look better. Each reclassification is a recorded consequence of a declared fact, and `exploratory` keeps its full identity, checkpoint and window.

**Coverage here is the planned SEARCH INTERVAL that was queried. It is not a guarantee that the external site returned every vacancy it held.**

A rotating family overdue by TIME is pulled forward whatever its cycle position says. The cycle counts runs and the cap counts hours: run weekly, a three-run family would wait twenty-one days against a fourteen-day cap and the cycle would report itself on schedule the whole time. `force_due_after_hours` is 168, well below the 336-hour cap, so the pull-forward happens before anything is lost.

Measured across simulated runs, counting only intervals permanently skipped between two coverages. These are the recorded figures in `config/search_strategy.json` under `deadlines.measured_performance`, which `--deep` re-derives from `tools/search_plan.py` rather than trusting:

| Schedule | Runs | Mandatory covered | Interval lost | Critical worst | Rolling worst | Service level |
| --- | --- | --- | --- | --- | --- | --- |
| every 24h | 30 | 73 / 73 | none | 72h | 168h | standard, meets both strict targets |
| every 30h | 30 | 73 / 73 | none | 90h | 180h | delayed, misses strict by 18h critical and 12h rolling |
| alternating 24h/30h | 30 | 73 / 73 | none | 84h | 162h | delayed, misses strict by 12h critical; rolling meets 168h |
| one run six hours late | 15 | 73 / 73 | none | 78h | 150h | delayed, misses strict by 6h critical; rolling meets 168h |

**One six-hour delay is the case most easily overstated, so it is stated twice.** It measures 78 hours critical: inside the 90-hour delayed tolerance, and six hours OUTSIDE the strict 72-hour standard. Both facts are reported and neither is described as the other. A delayed run is never reported as meeting the 72-hour target.

A missed run, two failed runs and a seven-day absence are simulated as well. What is asserted for them is what the scheduler actually guarantees: every mandatory bucket is still searched, no interval is permanently skipped, and every bucket stays inside the 14-day cap. Those disruptions have no recorded steady-state revisit figure, so none is quoted here.

**No bucket has a permanently skipped interval in any measured schedule.** The strict tier targets are stated for standard daily operation and are met exactly there. A missed or failed run widens the returning bucket's window rather than losing the interval, which is why the covered count stays at 73 while the revisit figures grow. Everything stays inside the 14-day cap, and anything beyond it is reported as a capped bucket with its uncovered hours.

### Planned means funded

A plan may not list a family it does not fund. Where the budget cannot reach one, it is reclassified from planned to **deferred**, carrying its reason, priority, coverage debt in hours, last successful coverage and next opportunity. `families_planned_but_unfunded` remains in the output and is empty by construction, so a reader can check the invariant rather than trust it. A deferred family advances no checkpoint, so the next run that funds it searches back over the whole interval.

### Inventory-family coverage

Each family in `config/sources.json` declares a monitoring class:

| Class | Meaning |
| --- | --- |
| `daily` | searched on every ordinary run |
| `rotating` | searched on a documented rolling cycle |
| `exhaustive` | reached only by an explicitly requested wide sweep |
| `excluded` | deliberately not queried, and removed from every denominator |

Exhaustive mode reaches 13 of 13 enabled queryable families. A daily run reaches the seven daily families plus the rotating families due this cycle, currently nine of thirteen, and its plan lists every omission with a reason and how many runs until that family is due. The three-run rotating cycle reaches all thirteen. A failed or partial run does not advance it.

Coverage is counted by FAMILY. Six sponsor boards inside one registry family are one family covered, never six, because searching two sites that share a label does not search more inventory. Their independent inventories still rotate, so no board stays permanently unreachable.

Adzuna is `rotating` rather than `daily`: it is an aggregator whose inventory is overwhelmingly copies of adverts already carried by Reed, CWJobs, Totaljobs and the employer sites, arriving with weaker identity and an unreliable date filter. It stays enabled and queryable, exhaustive always reaches it, and the decision carries a review date.

### Reserved query allocations

Against the 30-query daily reference budget, before any lower-priority family is funded:

| Class | Families | Reserved |
| --- | --- | --- |
| core | `direct-title`, `backend-capability` | 12 |
| early career | `early-career` | 4 |
| sponsorship | `sponsorship-oriented` | 4 |

Below the reference budget these scale proportionally and are floored at one, so a reduced-budget mode never reduces a required family to zero. Priority spending alone had starved exactly the two families most likely to hold this candidate's vacancy: a 36-query plan reached early-career and sponsorship with two queries each. Sponsorship queries SUPPLEMENT ordinary searches and never replace them, because most viable adverts say nothing about sponsorship at all.

### Deterministic title rotation

The planner used to pair titles with sources on a fixed diagonal, so LinkedIn only ever saw the first title and most title-source combinations were structurally unreachable. Rotation offsets that diagonal by a cycle index derived from run history:

```text
python tools/search_rotation.py index
python tools/search_rotation.py cycle direct-title
```

Over a five-run cycle every rotating title reaches every applicable primary inventory family. The index is the count of SUCCESSFUL COMPLETED production runs, so it is deterministic, re-planning the same state gives the same plan, and a failed or partial run does not advance past combinations it never covered. An explicit focused mode may override rotation and says so in the plan.

### The employer watchlist

Bounded at 60 active employers. An employer earns a place by evidence: a previously strong ranked match, a resolved ATS tenant or careers URL, a manual decision, or a recurring pattern across searches. Sponsor-register membership alone never qualifies, because the register lists thousands of licensed organisations and promoting on it would be an unbounded crawler.

```text
python tools/watchlist.py seed            # dry run, writes nothing
python tools/watchlist.py seed --apply    # backs up first, then writes
python tools/watchlist.py validate
python tools/watchlist.py due
```

Failed checks back off along 1, 3, 7, 14 then 30 days on top of the ordinary interval, and an entry is disabled rather than deleted after five consecutive failures so its evidence survives.

### Productivity metrics

```text
python tools/run_metrics.py run
python tools/run_metrics.py rolling
python tools/run_metrics.py check
```

Per run: window and reason, queries by family and by source, title-source combinations, rotation index, the full funnel, source and inventory-family outcomes, employer ATS checks, sponsorship lookups and duration. Derived: new Direct matches per ten queries, detailed-read conversion, new Direct per JD read, duplicate and hard-filter rates, and query- and source-family contribution.

Every ratio is `null` rather than `0.0` when its denominator is zero, because a run that read nothing has no conversion rate and reporting zero would claim it converted nothing. Rolling summaries cover the last seven SUCCESSFUL runs, name the partial runs they excluded, and flag a sample below three as insufficient. Metrics inform a later human calibration decision and change nothing by themselves.

## Instruction maintenance

Claude reads instructions in three tiers, and the tier decides the cost:

| Tier | Files | Cost |
| --- | --- | --- |
| Always loaded | `CLAUDE.md` | Read into EVERY session, so every character is a permanent tax |
| On invocation | `.claude/commands/*.md`, `.claude/skills/*/SKILL.md`, `.claude/agents/*.md` | Read when that command, skill or agent runs |
| On demand | `.claude/skills/*/references/*.md` and other skill files | Read only when the instruction that owns them says to |

### Where a new rule belongs

Ask what KIND of rule it is:

- A number, threshold, vocabulary or band: configuration (`config/*.json`, `candidate/config.json`). Never prose.
- A rule that must hold whatever a model decides: Python, enforced at a boundary.
- A candidate fact: `candidate/profile.md`, and let `tools/candidate_config.py` derive from it.
- A step in running a command: that command or skill file.
- A procedure needed only on one branch: a skill reference under `references/`.
- A safety invariant that must be true before Claude reads anything else: `CLAUDE.md`, stated once and briefly.
- Anything a human needs but Claude does not: this file.

### Avoiding duplication

Every rule has ONE authority, listed in the table in `CLAUDE.md`. Other files REFERENCE it. If you find yourself writing a threshold, a band table or an immigration figure into prose, it belongs in configuration and the prose should name the tool that prints it:

```text
python tools/match_evaluation.py schema
python tools/immigration_rules.py show
python tools/candidate_config.py show --compact
python tools/sources.py list
```

The deep validator enforces this: it fails when a live immigration salary table, a duplicated band table, or the candidate experience calibration appears outside its authority, and when a superseded rule reappears anywhere.

### Instruction-size budgets

| File | Lines | Characters |
| --- | --- | --- |
| `CLAUDE.md` | 200 | 25,000 |
| `.claude/skills/scrape/SKILL.md` | 400 | 30,000 |
| `.claude/commands/rank.md` | 220 | 30,000 |
| `.claude/skills/job-matcher/job-screening.md` | 220 | 30,000 |

The `CLAUDE.md` budget follows the official Claude Code guidance to target under 200 lines, because longer always-loaded files consume context and reduce adherence. The validator checks each budget and reports the actual figure on failure.

### Adding an optional reference without making it always loaded

1. Put the file in the owning skill's `references/` directory.
2. Start it with a heading and a line saying it is NOT loaded automatically, and when to read it.
3. Name it from the owning `SKILL.md` in the read-when table, as a plain relative path.
4. Do NOT write `@path` outside backticks anywhere in an instruction file. An `@path` import is expanded into context AT LAUNCH, so it would make the reference always loaded and undo the point of moving it. Backticked paths are literal text and are safe.
5. Keep the reference set small and cohesive. Four to six references per skill is the target; dozens of fragments are harder to route than one long file.

The validator checks that every reference is named by its skill, that every name resolves to a file, that each reference declares its on-demand status, and that no instruction file imports anything through `@path`.

### Command and skill frontmatter

Every file in `.claude/commands/` opens with a YAML frontmatter block carrying a `description`, and an `argument-hint` where the command takes arguments. The description is copied VERBATIM from the command table in `CLAUDE.md`, and the deep validator asserts the two agree, so a command cannot end up describing itself differently in two places.

Frontmatter is metadata, never a permission grant. Do not add `allowed-tools` to a command: tool permissions belong in `.claude/settings.json`, and the validator refuses an `allowed-tools` key in a command's frontmatter. The one deliberate exception is `.claude/skills/scrape/SKILL.md`, whose grant is part of the discovery design and is checked for change.

`claude plugin validate .claude` is what reports a missing or unparseable frontmatter block. Run it after adding or renaming a command, a skill or an agent.

### Running the validation

Two layers, and they check different things. Run both after any instruction change.

**1. Official Claude Code validation: frontmatter and component discovery.**

```text
claude plugin validate .claude
```

This is the preferred check for frontmatter. Since Claude Code 2.1.233 it validates a directory of skills, agents and commands WITHOUT a plugin manifest, so a plain project needs no `plugin.json` and none should be added merely to make validation run. It answers one question: can Claude Code discover and parse these components. It reports a missing frontmatter block, an unparseable one, or a field it cannot read. It knows nothing about this project's rules.

**2. Project deep validation: behaviour, authority, size, duplication and deterministic invariants.**

```text
python tools/validate_workspace.py --deep
```

This is the final gate after any instruction change, and it is not replaceable by the official command. It checks the things that are specific to this workspace: that a hard blocker still needs canonical employer evidence, that the calibration still derives the thresholds it is supposed to, that immigration figures live only in their authority, that no superseded rule has returned, that each instruction file is within its size budget, that every skill reference is reachable and declares itself on-demand, and that nothing has become always-loaded through an `@path`.

`python tools/preflight.py` is the lighter gate before a live run.

## Privacy and GitHub

This repository is a strictly LOCAL recovery checkpoint. It has no remote, and it must not gain one without a deliberate decision.

The private candidate authorities are tracked here on purpose: `candidate/profile.md`, `candidate/config.json`, `candidate/cv-maintenance.md`, `documents/master/cv.pdf`, `documents/master/cv.json`, and the `job_scraper/` state files. They are the files a mistake is most expensive in, and a checkpoint that excluded them would not protect the work it exists to protect. Run captures, shortlist snapshots, the job-description cache, the regenerable sponsor snapshot, generated reports, backups and local Claude permission state ARE gitignored.

The consequence is that this history cannot be published as it stands. Before adding any remote, either strip those paths from history or start a fresh repository from a sanitised export. Always inspect `git status` and the repository history before publishing anything.

### A shareable archive

A copy of this project intended for anyone else must exclude at minimum:

- `.git/` (it carries the private authorities in its history)
- `.claude/settings.local.json`
- `backups/`
- `reports/`
- `tools/__pycache__/` and any `*.pyc`
- temporary captures: `job_scraper/runs/`, `job_scraper/cache/`, `job_scraper/shortlists/`
- the regenerable official sponsor snapshot in `job_scraper/reference/`
- the private candidate authorities and everything under `documents/`

`PACKAGE_MANIFEST.txt` is exactly that shareable set, one `SHA-256  ./path` line per file. It is derived rather than hand-listed, so a new tool joins it automatically and a private authority cannot:

```text
python tools/package_manifest.py list      # the derived package
python tools/package_manifest.py generate  # rewrite the manifest
python tools/package_manifest.py verify    # every path, digest and exclusion
```

`verify` fails on a missing path, a changed digest, a duplicated entry, an unlisted package file, a private or ignored path that got in, and on the manifest naming itself. `--deep` runs it against the live workspace and against fixtures, so the exclusions are tested rather than described.

This project is adapted from Mads Lorentzen's open-source `ai-job-search` project. See `UPSTREAM_NOTICE.md` and `UPSTREAM_LICENSE` for attribution and licence information.

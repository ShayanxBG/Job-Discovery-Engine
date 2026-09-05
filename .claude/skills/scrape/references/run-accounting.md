# Run accounting, output and health mode

Reference for `.claude/skills/scrape/SKILL.md`. Read this when opening or closing a run record, deciding a stopping verdict, reconciling counters, rendering the result, or running health mode.

This file is NOT loaded automatically. The main skill says when to read it.

### Step 0B: Open the run record

Every discovery run keeps a private coverage log under `job_scraper/runs/`, so a collapsed source can never look healthy by accident:

```text
python tools/discovery_run.py begin --mode deep --requested-window 24h
```

Retain the returned `run_id` for the whole run. Record each source as soon as it finishes, using the controlled outcome vocabulary:

```text
python tools/discovery_run.py source --run-id <id> --source-id totaljobs --outcome changed_layout --searched 2 --candidates 0 --notes "result list did not render"
```

Outcomes are `ok`, `empty`, `partial`, `blocked_captcha`, `blocked_permission`, `changed_layout`, `timeout`, `unavailable` and `error`.

`empty` and a failure outcome mean completely different things and must never be conflated. `empty` says the source genuinely held nothing, which is market supply. `changed_layout`, `blocked_captcha`, `timeout` and the rest say coverage was lost, which is a source problem. A source that broke must never be recorded as `0 results`.

**How to tell them apart, because both arrive as an HTTP 200 with no results.** A
throttled board does not announce itself: it returns a normal status code, the
usual page furniture, often the query echoed back, and no cards. Believing it is
worse than a visible failure, because `empty` is a COVERING outcome, so it counts
the source as searched end to end AND advances that bucket's checkpoint. The
interval is then recorded as covered and is never searched again.

So a result set with no items is never recorded from a single observation. Retry
it once, and only then decide:

- Empty again, and shallower pages of the same query also returned nothing: the
  source genuinely held nothing. Record `empty`.
- Returns items on retry: the source is throttling. Record `partial`, which is a
  FAILED outcome, so the bucket does not advance and the family keeps
  `GAP_REMAINS`.
- Empty on retry at an offset where SHALLOWER pages returned items: throttling
  again, and the same `partial` - BUT ONLY IF the board's own reported total is
  not already accounted for. **A published total beats this heuristic.** Measured
  2026-09-04: CWJobs `Integration Developer` returned 25 cards on page 1 and
  nothing on page 2, which the heuristic alone calls throttling; the page's own
  heading said `12 Integration Developer jobs`, so page 1 had already exhausted
  the result set and the extra cards were promoted filler. That query is `ok`.
  Recording it `partial` would have manufactured lost coverage out of a source
  that answered completely.

**And the degraded answer is not always empty. It can be plausible.** The rule
above is about a result set with NO items, which at least looks wrong. Reed does
something harder to catch: measured 2026-09-04, a fetch of
`reed.co.uk/jobs/python-developer-jobs` returned a well-formed page whose own
embedded data said `count: 7` and carried exactly 7 jobs. Five immediate repeats
of the SAME url returned `count: 1182` and 25 jobs. Nothing in the degraded
response was malformed, no status code was unusual, and its internal numbers
agreed with each other - it simply described a market that does not exist.

A run believing it would have recorded `ok` with 7 candidates, raised no warning,
and advanced the bucket over 1,175 vacancies it never saw.

So extend the retry rule from EMPTY to IMPLAUSIBLE: before recording the outcome of
a query on a REQUIRED bucket, retry once when the result set is empty OR is far
smaller than that source usually returns for that kind of term. Where the board
publishes its own total, compare the two runs' totals rather than the page you were
given. Agreement across two observations is the evidence; a single self-consistent
page is not, because that is exactly what the degraded response looks like.

This has been measured on more than one board - LinkedIn guest at `start=850` and
`start=900` returning no cards, and Reed returning a plausible wrong total - so
treat it as the default behaviour of a busy board rather than a quirk of one site.

Close the run with its counts:

```text
python tools/discovery_run.py finish --run-id <id> --windows 24h,7d --raw 312 --duplicates 190 --hard-filtered 60 --suppressed 14 --deep-checked 48 --new-direct 7 --updated 2 --agency 5 --verification 1
```

A run is recorded as PARTIAL when an attempted inventory family has no successful source, and as COMPLETE_WITH_WARNINGS when every family was covered but a sibling source inside one failed. Run records hold coverage and counts only. Never write candidate profile text, CV content, credentials, cookies or browser session data into them.

### Stopping rules

Ask the planner, do not guess:

```text
python tools/search_plan.py progress --file query_outcomes.json --mode deep
```

- `CONTINUE`: more useful work available in that family
- `SATURATED`: minimum query coverage met AND two consecutive distinct completed queries produced zero NEW canonical candidates
- `BUDGET_EXHAUSTED`: the family or the run spent its query budget
- `GAP_REMAINS`: a query lost coverage to a failed source, so the family is not finished

One empty query never saturates a family. A FAILED source is not zero yield: it is lost coverage, it is excluded from the zero-yield streak, and it leaves the family `GAP_REMAINS`. Treating a broken source as a saturated family is the same error as reporting it as `0 results`. A productive query resets the streak.

Budgets are counted in queries, candidates and new canonical yield, never in minutes. A prompt cannot reliably stop a worker at a wall-clock time, so no budget here pretends to. The separate operational rule still applies: a stuck or non-essential worker is dropped rather than allowed to block run completion.

## Step 7: Present the useful output

### Sorting

Direct Matches:

1. High before Medium.
2. Verified recency, newest first.
3. Sponsorship evidence strength.
4. Technical/profile closeness.
5. Seniority realism.
6. Source confidence.
7. London, Cambridge, Oxford as tie-breakers only.

Agency Leads:

1. Strong before Stretch.
2. Verified recency, newest first.
3. Technical/profile closeness.
4. Seniority realism.
5. Source confidence.

Updated Leads are supplementary and do not count as new direct matches.

### Coverage output

Always include a compact coverage block before the jobs:

```text
Coverage:
  LinkedIn guest: ok [queries X, cards inspected Y]
  DWP Work Hub: ok [queries X, cards inspected Y]
  Reed: ok [queries X]
  CWJobs browser: ok [queries X]
  Totaljobs browser: changed_layout [queries X] <- lost coverage
  Employer/ATS: ok [searches X, candidate resolutions Y]
  Other UK boards: ok [searches X]
  Sponsor sources: ok [searches X]
Source families attempted: N | covered: C | gaps: G
Run coverage: COMPLETE | COMPLETE_WITH_WARNINGS | PARTIAL
Source warnings: totaljobs changed_layout (stepstone family still covered by cwjobs)
Family gaps: none
Windows searched: 24h -> 7d [-> 14d if used]
Raw discoveries: X | Duplicate/already seen: Y | Hard-filtered: Z | Suppressed: S | Deep-checked: N
New Direct Matches: D | Agency Leads: A | Verification Leads: V | Updated Leads: U
```

Use the controlled outcome vocabulary for each source, not free-form words, and take the family and run-coverage lines from `python tools/discovery_run.py show` rather than estimating them.

### Search-productivity output

Source coverage answers "did we look?". Query coverage answers "did we look for the right things?". Report both, so a thin result is diagnosable rather than merely disappointing. Record each executed query as it completes:

```text
python tools/discovery_run.py query --run-id <run_id> --query-id <id> --search-family direct-title \
  --source-id linkedin --outcome ok --coverage-bucket linkedin::direct-title::python-developer --window 14d \
  --raw-candidates 20 --new-canonical 6 --eligible 9 --deep-checked 4
```

### The coverage-bucket recording contract

`--coverage-bucket` is the whole of a query's coverage evidence. Pass the bucket
the PLAN assigned to that task, verbatim, together with the task's
`effective_window`. `coverage_ledger.checkpoints()` credits nothing else, so a
query recorded without it is a query that reads as searched and advances no
checkpoint.

That is not hypothetical. Production run `scrape-20260831T083115570281` recorded
58 queries, 31 of them `ok`, and credited ZERO buckets, because the recording CLI
had no parameter for the field the ledger reads. Initial catch-up could never
have completed, no matter how much searching happened.

The gate now fails closed:

- A MANDATORY obligation with no `--coverage-bucket` is REFUSED. Nothing is
  written, so the omission cannot be mistaken for a searched interval.
- A bucket must agree with the query it claims to describe: the source's own
  inventory family, the recorded `--search-family`, the planner's `--query-id`
  where that id is planner-shaped, and a term cluster the required universe
  actually declares. Any disagreement is refused.
- `--window` is required whenever a bucket is supplied. A checkpoint records
  WHICH interval was searched, so crediting one without a stated window would
  claim an interval nobody named.
- Record the bucket for EVERY outcome, failures included. `changed_layout`,
  `error` and `partial` rows keep their bucket for audit and receive no credit,
  because only `ok` and `empty` are covering outcomes. Lost coverage must stay
  visible and stay uncredited.

`--subsumes` declares a narrower bucket a completed broad query also searched.
Declare it in the task; it is never re-derived later.

`discovery_run.py show` and the run summary expose
`query_coverage.covering_queries_without_coverage_bucket`. It must be empty. A
name in that list is a query that credited nothing.

### Safe close is not coverage complete

Four different questions, four different fields. Never answer one with another:

| Question | Field |
| --- | --- |
| Did the process close without an abandoned lock? | `finished_at` set, and `discovery_run.py active` reports `held: false` |
| Did the operator declare the run untrustworthy? | `forced_partial` |
| Did the run itself error? | `errors` |
| Was the INVENTORY actually covered? | `summary.coverage_status`, `family_gaps`, `family_coverage_complete` |

A run can close perfectly cleanly, hold no lock, carry `errors: []`, and still be
`coverage_status: PARTIAL` with five family gaps. Report it as partial. `errors:
[]` says nothing about source health: a collapsed source lives in `sources` and
in `source_warnings`, never in `errors`.

Two predicates read these differently, deliberately:

- `search_window.run_is_successful` also refuses a run whose run-level coverage
  is PARTIAL. That is right for "which was the last successful run", because a
  run that missed whole families should not set the next window.
- `coverage_ledger.run_is_creditable` requires finished, production mode and not
  `forced_partial`, and then lets each QUERY's own outcome decide its bucket.
  Coverage is keyed per bucket precisely because a board holds one inventory and
  filters it by query text, so a broken LinkedIn cannot un-search the eleven DWP
  queries that returned `ok` beside it. Gating per-bucket evidence on a whole-run
  verdict is what left 30 genuinely searched buckets uncredited.

An incomplete catch-up still reports `INITIAL_CATCHUP`, and that is correct: the
MODE names the budget, while each bucket's own `effective_window` names the work.
After a partial first run the plan already behaves as gap-fill at bucket level,
covered buckets planning from `bucket_checkpoint` and never-searched ones from
`first_coverage`. Read `python tools/coverage_ledger.py bootstrap` for how much of
catch-up is genuinely outstanding, and `python tools/search_window.py gapfill` for
which families to repair.

### Never leave a run backup inside `job_scraper/runs/`

Anything matching a run file in that directory is read back as another production
run and corrupts run history and counts. Backups belong in
`backups/discovery-runs/`.

Then take the productivity block straight from `discovery_run.py show`:

```text
Queries:
  attempted: X | completed: C | productive: P | failed: F
  new canonical candidates: N | per completed query: R
  stopping state: CONTINUE | SATURATED | BUDGET_EXHAUSTED | GAP_REMAINS
  saturated families: [...]
  families with lost coverage: [...]

Search-family yield:
  direct-title           X new / Y queries
  backend-capability     X new / Y queries
  adjacent-software      X new / Y queries
  early-career           X new / Y queries
  employer-ats           X new / Y queries
  Search families completed: N (broad query coverage needs 3): BROAD | NARROW
```

Report sponsor-register work too, so it is visible whether licence checks are spending web budget they did not need to:

```text
Sponsor register:
  snapshot: fresh | stale | unavailable
  downloaded_at: ...
  official_updated_at: ... (when known)
  local lookups: X | credible matches: Y | ambiguous: A | not found: N
  live verification fallbacks: L
```

Together with source coverage this separates five very different causes of a short result list:

- a genuinely thin market: sources `ok`/`empty`, several search families covered, low yield everywhere
- a narrow query strategy: high yield in one family, `NARROW` family coverage
- a failed source: a family gap or `GAP_REMAINS`, never to be read as a quiet market
- over-filtering: high raw candidates, very low eligible-after-cheap-filters
- genuine saturation: `SATURATED` families after real query coverage

Never print candidate profile text in the run report. Query rows carry counts only.

Do not report a source as `ok` unless it actually returned readable search/result data. A source that broke is `changed_layout`, `blocked_captcha`, `timeout`, `unavailable`, `blocked_permission` or `error`, and is never reported as `empty` or as `0 results`. A run is PARTIAL when an attempted family has no successful source, or when the run was explicitly forced partial; a failed source inside a family another site covered makes the run COMPLETE_WITH_WARNINGS and stays listed as a source warning.

### Result sections

Use these sections in this order:

```text
## Direct Matches

| # | Quick fit | Sponsorship | Role | Company | Location | Posted | Source |

## Agency Leads

| # | Lead strength | Role | Agency | Location | Posted | Main uncertainty | Source |

## Updated Leads

| # | Type | Role | Company/Agency | What changed | Posted | Source |
```

For each High Direct Match, include a compact detail block with why it fits, one main concern if any, salary, sponsorship evidence and source confidence.

For each Agency Lead, include:

- why the technical/job-function match is worth recruiter contact
- what is unknown about the client/sponsorship/seniority
- the exact verification questions to ask, but do not send them

Do not dump weak jobs. A shortlist of 3-12 genuinely useful direct matches plus a separate handful of agency leads is preferable to 20 marginal roles.

If no High roles survive, say so. If there are no good Direct Matches today, say that plainly rather than promoting weaker jobs.

Finish with:

`Run /rank to fully score the new/updated direct matches and provisionally rank agency leads.`

## Health mode

`/scrape health` checks:

1. candidate evidence readable
2. search strategy readable
3. `seen_jobs.json` valid JSON
4. sponsor helper runs
5. WebSearch returns current UK software-job results
6. discovery rules contain run-history window selection, deep-coverage, strict-seniority, agency-category, source-diversity and deduplication logic

Browser availability is informational in ordinary health mode.

`/scrape health browser` additionally requires:

1. Claude in Chrome tools available
2. read-only access to a logged-in LinkedIn Jobs page
3. test CWJobs navigability when permitted
4. test Totaljobs navigability when permitted

Indeed is NOT part of this test and must not be opened by it: the family is not
queryable, and a health check that visits it is still an attempt.

CWJobs/Totaljobs being blocked should be reported explicitly as incomplete source coverage rather than causing the core LinkedIn browser health test to fail. LinkedIn's own coverage path needs no browser at all, so a browser failure never means LinkedIn was unsearchable.

Do not save any jobs in health mode.

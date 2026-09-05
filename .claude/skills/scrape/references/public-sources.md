# Public, employer and ATS discovery

Reference for `.claude/skills/scrape/SKILL.md`. Read this when delegating to public-source workers, running employer/ATS discovery, or judging minimum source coverage for a deep run.

This file is NOT loaded automatically. The main skill says when to read it.

### 1B. Parallel public discovery with bounded subagents

Use the `Agent` tool to delegate public discovery tasks in parallel where useful. Keep authenticated browser control in the main agent so multiple workers do not compete for the same Chrome session.

Do NOT give one public worker the entire UK-board universe. Split broad work into bounded source groups so one slow source cannot consume the whole run. A normal public discovery worker should usually receive no more than 2-3 source domains/platforms, roughly 8-12 focused searches, and roughly 15-20 promising posting fetches. It should return partial useful findings once that budget is reached or marginal yield collapses instead of endlessly expanding.

Preferred parallel tasks:

1. Employer/ATS discovery and exact-title employer resolution.
2. Core UK boards, for example Reed + DWP + JobServe.
3. Alternate UK boards, for example Built In + Welcome to the Jungle + Technojobs/Adzuna.
4. Sponsorship-focused lead discovery.

Use the read-only `public-job-researcher` subagent for these tasks. It must not write state.

Its final return is ONE machine-readable JSON object, not prose:

```json
{
  "source_id": "reed",
  "outcome": "ok",
  "searched": ["site:reed.co.uk/jobs \"Python Developer\""],
  "candidates": [],
  "warnings": []
}
```

`source_id` must exist in `config/sources.json`, `outcome` must be one of the controlled source outcomes, and every candidate must satisfy the discovery-candidate schema. Validate every worker return before using any of it:

```text
python tools/discovery_candidate.py validate-worker --file worker.json
```

Only the `accepted` array may enter the pipeline. Rejected rows are reported in coverage and never written to state. Do not reinterpret a worker's paragraphs to recover a company, title, URL, posted date, salary, experience level or stack; if the structured fields are missing, the row is rejected rather than reconstructed by guesswork.

Do not let a non-essential public worker block the final report indefinitely. If a background worker is still running after roughly 15-20 minutes, continue productive main-agent work and prefer a partial return, narrower retry, or explicit incomplete-coverage note over waiting another hour. Background agents consume model usage; never describe an indefinitely running worker as costing nothing.

For promising employers where sponsorship is uncertain, use the read-only `sponsor-verifier` subagent in parallel for current public evidence. Batch verification into small groups (normally no more than 6-8 employers per worker). Local CSV checks remain a quick hint, not final proof.

### 1C. Employer and ATS discovery, always run

Employer-direct sources have the highest confidence.

Do not rely only on generic `site:greenhouse` queries because they can return platform landing pages. Use two approaches:

1. Independent employer/ATS discovery using the queries in `search-queries.md`.
2. Candidate resolution: whenever LinkedIn, Indeed, a UK board, or a sponsor board surfaces a promising role, search `"<company>" "<exact title>" careers` and resolve it to the employer/ATS posting when possible.

Employer/ATS sources include Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday and direct careers pages.

### Minimum source coverage for a normal deep run

Unless access fails or the user explicitly limits the source, attempt all of these families:

- LinkedIn Jobs: a PRIMARY inventory family holding nine critical buckets, and the
  only one whose coverage path needs no browser. Search it by the paginated guest
  endpoint in `references/browser-sources.md` EVEN WHEN CHROME IS UNAVAILABLE, and
  page each query deep rather than taking one screen. At least 12 title/query
  families in normal deep mode; add the authenticated card list for extra breadth
  when Chrome is connected.
- Indeed UK: **NOT QUERYABLE, and not attempted at all.** It is excluded with a
  review date in `config/sources.json`, the planner funds it zero queries in every
  mode, and `references/browser-sources.md` holds the measured evidence. Do not
  count it toward any floor below and do not record a source outcome for it: a
  family becomes a GAP only once ATTEMPTED, and that gap alone makes a run PARTIAL.
- Employer/ATS direct: at least 8 targeted searches/resolution attempts across employer careers, Greenhouse, Lever, Ashby, Workable, SmartRecruiters and Workday.
- General UK boards: at least 6 focused searches AND at least 4 distinct board/platform families attempted across CWJobs/Totaljobs, Reed, DWP Find a Job, Built In, Welcome to the Jungle, JobServe, Technojobs or Adzuna where useful. CWJobs and Totaljobs share a platform and count as one diversity family even when both are searched. No single board may satisfy the diversity floor by repeating queries. Browser-search CWJobs/Totaljobs when available.
- Sponsorship-focused sources: normally 8-12 focused searches across at least 4 current sources in `search-queries.md`. Once direct-employer/ATS and general-board coverage is incomplete, do not spend dozens of extra searches on sponsor boards. Additional sponsor-board searching beyond roughly 15 queries is justified only when those sources are still producing unique plausible leads and the primary source families are already covered.

### Fetch boards with a complete browser header set

A server-side board fetch must send the headers an ordinary browser sends, not a
bare `User-Agent`. Several UK boards refuse an incomplete header set outright, and
they refuse it with a status that reads like a dead site, so an under-specified
fetch gets recorded as a broken source and its queries are written off.

Measured 2026-09-04, five requests each way:

| Source | Bare `User-Agent` | Complete header set |
| --- | --- | --- |
| DWP Work Hub (`www.jobs.service.gov.uk`) | 403 five times out of five | 200 five times out of five, 2,571 results, dated, keyword-faithful |
| Adzuna | 403 | 200, 4,358 results |

Both were previously recorded as blocked on the strength of a bare-`User-Agent`
probe. That was wrong, and it was expensive: DWP carries eleven of the
fifty-eight bootstrap queries and Adzuna two, so 22% of the run was being written
off by the way it asked.

Send all of: `User-Agent` (a current desktop Chrome string), `Accept`,
`Accept-Language: en-GB,en;q=0.9`, `Accept-Encoding`, `Upgrade-Insecure-Requests`,
and `Sec-Fetch-Dest`/`Sec-Fetch-Mode`/`Sec-Fetch-Site`. Leave-one-out showed
`Accept-Language` and the `Sec-Fetch-*` group are each individually necessary for
DWP, but supplying only those two still returns 403: it is header COMPLETENESS
that is being judged, so send the whole set rather than a minimal pair. The same
set was verified to work, and to break nothing, on LinkedIn guest, Reed,
Totaljobs, Built In and Welcome to the Jungle.

Two corollaries. A 403 or an error page from a board is never evidence that the
site is down until it has been retried with complete headers. And this is
independent of the card-less-200 rule in `references/run-accounting.md`: headers
decide whether you are served at all, that rule decides whether what you were
served can be believed.

### DWP Work Hub: window it with `postingDateRange`, and read `Added on`

Two things about this source, both measured 2026-09-04, and both worth getting
right because it carries eleven of the fifty-eight bootstrap queries.

**Window it with `postingDateRange`, in days**, appended to the registry's
`search_url_template`. It genuinely filters: `Python Developer` returned 558
results unwindowed, 308 at `postingDateRange=14` and 146 at `postingDateRange=7`.
Do NOT reach for `sortOption=DATE` to get freshness - date sorting on this source
OR-matches the query terms and destroys keyword fidelity. Relevance ordering plus
`postingDateRange` keeps both.

**Take the posted date from the `Added on` label, never from a bare date regex.**
Result cards render it as `Added on 30 Aug 2026`. A naive scan for any date in the
page also matches text inside the vacancy body: a card in the 7-day result set
carried `This document was prepared on 30 April 2026. It was last reviewed on
27 May 2026`, which is a privacy statement the employer pasted into the advert. A
regex taking the first date would have aged that vacancy at roughly 100 days and
dropped a fresh role as out of window - and, because an out-of-window judgement is
recorded rather than re-derived, it would have stayed dropped.

Source targets are floors, not ceilings. If a source is still producing unique plausible vacancies, continue. If a source is blocked or unproductive, record that and spend effort elsewhere. Source diversity outranks merely hitting a raw query count.

### `/scrape gapfill` recovery mode

Use this after a completed scrape explicitly reported meaningful source gaps. Preserve `seen_jobs.json`; never reset it. Default to the last 14 days unless the user supplies another window.

Target FAMILY GAPS first, meaning families where no source completed, not every degraded sibling source. A family another site already covered does not need a gap-fill pass, though a persistently broken sibling such as Totaljobs is still worth repairing on its own account. After the gaps, prioritise under-covered families: DWP Find a Job, Built In, Welcome to the Jungle, JobServe, Technojobs and Adzuna, then resolve promising hits to employer/ATS pages. Do not repeat the full LinkedIn/Indeed sweep unless those authenticated sources were themselves incomplete.

A gap-fill run may discover new roles or materially upgrade existing ones. Ordinary duplicates stay hidden. Report exactly which prior gaps were attempted and which remain unresolved.

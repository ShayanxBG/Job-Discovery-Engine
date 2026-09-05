# Browser Discovery Setup

For the strongest job-discovery coverage, launch Claude Code with Chrome connected:

```powershell
cd <path-to-your-job-discovery-workspace>
claude --chrome
```

Inside Claude Code, run:

```text
/chrome
```

The healthy state is:

```text
Status: Enabled
Extension: Installed
```

Use the Chrome profile where the user is already signed into LinkedIn and Indeed UK. CWJobs and Totaljobs can also be used through the browser when their domains are permitted and readable.

## Permissions

Authenticated discovery is read-only. Site permissions may allow Claude to navigate, search, set filters, scroll, open job cards and read postings.

During `/scrape`, Claude must never:

- click Apply or Easy Apply
- submit an application form
- send a recruiter/employer message
- save/follow jobs or companies
- change account/profile settings
- upload a CV

If a CAPTCHA or login challenge appears, the user handles it manually.

## Health test

Run:

```text
/scrape health browser
```

The core browser health test requires authenticated LinkedIn Jobs and Indeed UK. It also tests CWJobs and Totaljobs navigability when permissions allow. If CWJobs/Totaljobs are blocked, the run must report incomplete source coverage rather than pretending they were searched.

No jobs are saved in health mode.

## Daily use

After browser health passes:

```text
/scrape
```

Normal v2.2 discovery searches deeply across authenticated browser sources and parallel public sources. The state file prevents ordinary repeated vacancies from being shown again on later days.


## Runtime failures after a healthy check

A site can pass `/scrape health browser` and still fail later because of transient navigation permissions, anti-bot checks or session state.

- CWJobs/Totaljobs: if one passed health but is denied during `/scrape`, retry once in a fresh tab/session context. If it still fails, use public/shared-platform evidence only as partial coverage and report the gap.
- Indeed: never bypass Cloudflare/CAPTCHA. Stop that pass, do other source work, and optionally retry once later.
- Do not repeatedly hammer a blocked source.

Use `/scrape gapfill` after an otherwise completed run when meaningful source families remained uncovered.

## Source findings from production runs

Recorded from real runs. Each names the run that produced it, so a later reader can tell a measured finding from a guess.

### DWP: sort by relevance, never by date

On DWP Work Hub (`www.jobs.service.gov.uk`), `sortOption=DATE` collapses keyword relevance. Measured in run `scrape-20260901T090954515780`: `Python Developer` sorted by date returned Clinical Practice Development Nurse, Training and Development Chef and Business Development Manager, because the query OR-matches "Developer" and "Development" and drops "Python". The same query under `sortOption=RELEVANCE` returned genuinely Python-specific roles.

This is the same failure LinkedIn shows under `sortBy=DD`, on a different site.

Use relevance sort together with the source-side date filter, which is honoured:

```text
https://www.jobs.service.gov.uk/jobs/search?keywords=<query>&postingDateRange=14&sortOption=RELEVANCE&resultsPerPage=30
```

`postingDateRange` accepts 1, 3, 7 and 14. Relevance still degrades into OR-matching further down a common-word result list, so read each card's own title and stop when the results stop naming the stack.

### LinkedIn: the authenticated list does not date its cards

In the same run the authenticated LinkedIn jobs list rendered 7 of 32 result items, and every one of them was undated with 6 badged `Promoted`. An undated card can never be counted toward a window or credit a bucket, so the authenticated pass alone could not evidence freshness.

The public `jobs-guest` endpoint returned the same searches with clean per-card posted dates, and that is what the run used for LinkedIn coverage.

Record that honestly: results obtained this way are PUBLIC coverage and must never be reported as authenticated inventory. The distinction is not cosmetic, because public and authenticated LinkedIn expose different inventory.

### Reed: capability-term queries were dropped, pending confirmation

In run `scrape-20260901T090954515780` Reed returned no usable results for both capability-term queries, `Python Django REST Framework` and `Python REST API`: the `site:` restriction was not honoured and the results came back from unrelated domains. Both were recorded `partial` and correctly credited no coverage, which is why 31 of 33 critical buckets completed rather than all 33.

No configuration has been changed for Reed. One run is not enough to distinguish a search-engine artefact from a source-capability limit. Confirm on the next run before deciding whether this is a `query_execution` fact about Reed or a transient indexing failure.

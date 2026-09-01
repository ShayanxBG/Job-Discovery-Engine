# Authenticated browser discovery

Reference for `.claude/skills/scrape/SKILL.md`. Read this ONLY when the run will use the signed-in Chrome session: the authenticated LinkedIn, Indeed, CWJobs or Totaljobs passes. A public-only or employer/ATS-only run never needs it.

This file is NOT loaded automatically. The main skill says when to read it.

### 1A. Authenticated browser discovery, preferred when available

Claude Code with Chrome can share the browser's existing login state. If Claude in Chrome is connected, use it for LinkedIn Jobs and Indeed UK because public search engines expose only part of those inventories. Also use browser access for CWJobs and Totaljobs when those sites are permitted and readable.

#### Verify the page you are reading is the page you asked for

A read issued after a navigation can return the PREVIOUS page. Before extracting
anything, and before crediting any coverage, confirm from the echoed URL and the
rendered page that the hostname, the query text and the applied filters are the
ones you requested. Every browser tool result echoes the tab's current URL and
title; that echo comes from the extension rather than from the page, so it is the
evidence to trust.

If a read returns the preceding source, an old URL, stale content or results that
do not match the query: do not extract it, do not save it, do not count it, and do
not advance the bucket. Wait, re-read the intended page, and proceed only once
identity is verified. Fail closed whenever source identity, freshness, filter
application, result-list rendering or query execution is uncertain.

Browser tools are read-only for discovery:

- search
- set filters
- sort
- scroll result lists
- open job cards/postings
- read title/company/location/date/requirements
- copy source URLs

Never during `/scrape`:

- click Apply or Easy Apply
- submit forms
- send messages
- save/follow jobs or companies
- alter account/profile settings
- upload a CV

If Chrome tools are not available in a normal `/scrape`, continue with public discovery but mark authenticated coverage as unavailable in the coverage summary. Do not pretend public LinkedIn/Indeed coverage is equivalent to logged-in coverage.

#### LinkedIn authenticated pass

LinkedIn is a SUPPLEMENTAL source. It was removed from the primary inventory
families on 2026-08-31 and owes no 72-hour critical interval; it keeps
rolling-recall representatives and is still searched, but a LinkedIn result can
never discharge a critical obligation. `config/search_strategy.json` owns that
decision and the reasoning behind it.

**NEVER pass `sortBy=DD`.** It destroys keyword semantics. Verified in production
run `scrape-20260831T083115570281`: with `sortBy=DD`, `Python Developer` and
`Integration Developer` returned 17 of 18 IDENTICAL job ids, `Python Django REST
Framework` returned Splunk Consultant and Document Review Specialist, and `Junior
Backend Developer` returned Water Process Engineer. Reproduced in a fresh tab. The
keyword is effectively ignored and a generic UK recency feed comes back instead.

Without that parameter the search works. Use natural-language phrasing and the
supported Date posted filter, and let relevance order the results:

```text
/jobs/search/?keywords=junior%20Python%20backend%20developer&location=United%20Kingdom&geoId=101165590&f_TPR=r604800
```

Verified 2026-08-31 across three materially different searches: results were
genuinely query-specific, did not collapse into one feed, and kept UK scope apart
from occasional EMEA leakage.

Freshness is the part that does NOT hold. Across those three searches only 3 of 16
cards carried any readable posted age while 12 were badged `Promoted`. The Date
posted chip cannot be verified per card, which is the same promoted-slot pathology
proven on CWJobs. So:

- Read every card's own posted age. Where it is absent, freshness is UNKNOWN.
- Never count an undated card toward a window, and never credit a bucket from one.
- Open the vacancy and take its own posted date before treating it as in-window.

Window filters: `r86400` past 24 hours, `r604800` past week. `r1209600` is an
accepted value that LinkedIn labels "Past month". There is no true 14-day bucket,
so use public/employer/ATS sources for the 8-14 day slice.

Normal deep `/scrape`:

- run at least 12 distinct high-value title/query families from `search-queries.md`
- inspect up to roughly 20-30 potentially relevant cards per productive family before moving on
- keep scrolling while unique plausible roles are still appearing
- stop a family after results become mostly duplicate/off-profile for two consecutive result screens or after enough useful unique cards have been collected

`/scrape exhaustive` may use all 15+ title families and inspect deeper result pages where available.

Do not require Easy Apply. Do not rely on LinkedIn's experience-level classification as a hard filter because employers classify roles inconsistently.

When visible, collect applicant count or `under 10 applicants` as an informational signal only. It never changes fit by itself.

#### Indeed authenticated pass

Use the same window filters with Indeed UK's Date posted filter and newest/date sorting when available.

Normal deep `/scrape`:

- run at least 8 distinct title/query families
- inspect roughly 20-30 current results per productive family where available
- continue while unique relevant results are still appearing

Indeed personalised recommendations may be used as supplemental leads, but every role still goes through the same evidence and fit checks. Personalisation is not proof of fit.

#### Indeed hidden-card rule: a card must OWN its own id

Indeed's result list carries HIDDEN placeholder elements that also have a
`data-jk` attribute. They render at zero height with no visible ancestor, and
they sit between real cards. Verified in production run
`scrape-20260831T102144228455` (2026-08-31): walking up from such a placeholder
with `closest('.job_seen_beacon')` resolved to a NEIGHBOURING card, so the
placeholder inherited that card's title, employer and location. The same id
`f1e2d3c4b5a67890` was consequently attributed to `One Big Circle - Junior
Platform Software Developer` on one query and `Alexander Technologies - Test
Engineer (Software)` on another, and `fedcba9876543210` to a third job. Saving
either would have created a phantom vacancy with a real-looking id.

So, before any Indeed card is extracted:

- Take cards from the title anchor itself (`a.jcs-JobTitle[data-jk]`), never from
  a bare `[data-jk]` scan.
- SKIP any element that is not visible: `offsetParent === null`, or a bounding
  rectangle of zero height or width, or a hidden ancestor.
- The `data-jk` must belong to the SAME self-contained visible card as the title,
  employer, location and URL that will be stored with it. If the id and the
  fields come from different elements, the card is not extracted.
- Never let `closest()` climb past the card boundary. If the nearest card
  container cannot be resolved from the anchor itself, discard the row.
- Reject a placeholder or synthetic-looking id whenever card ownership cannot be
  proven, and never "repair" it by borrowing a neighbour's fields.
- Deduplicate only AFTER ownership is verified, so a hidden duplicate can never
  collapse into, or evict, a real card.
- Report visible and hidden counts separately, so a layout change that starts
  hiding real cards is visible rather than silent.

#### Ignored-query rule: a source that did not run the query did not cover it

Some boards accept a query and return their whole unfiltered inventory anyway.
Verified in the same run: GradSponsor ignored both `?q=` and its own in-page
search box and returned the identical 3,460-job list; FindSponsorJobs did the
same with 11,678 roles; JobServe reported its unfiltered 20,004-job UK total with
top results unrelated to the query. None of those searched what was asked.

Before attributing ANY result to a query:

- Confirm the rendered page reflects the REQUESTED QUERY, not merely the
  requested site: the results heading, the echoed URL, or the filter chips must
  name the query text.
- Confirm result relevance. A result set that has no relationship to the query
  text is evidence the query was dropped.
- If submitting the query leaves the result set, the total count and the top
  results UNCHANGED from the unfiltered listing, the query was ignored.
- Record that as `partial` (or `changed_layout` where the search UI itself broke).
  NEVER `ok`, and never `empty`: `empty` means the source genuinely held nothing
  for a query it actually ran.
- Such a query receives NO coverage credit. Its bucket is still recorded for
  audit and stays uncredited.
- A source repeatedly proven to ignore queries is recorded in
  `config/sources.json` as `query_execution: ignores_query`, which removes its
  mandatory obligations. It may stay enabled as a supplemental lead source.

#### CWJobs and Totaljobs authenticated/browser pass

When Chrome access is permitted:

- run several focused Python/backend searches on each site
- use date/newest filters where available
- inspect current cards rather than relying only on public search-engine indexing
- report each site separately as `used`, `blocked`, `unavailable`, or `no useful results`

A site being blocked must never be silently counted as searched.

Transient runtime recovery:

- If CWJobs or Totaljobs passed `/scrape health browser` but navigation is denied during the live scrape, retry that site once in a fresh tab/session context. If it still fails, use public indexing/shared-platform evidence as a partial fallback and label the site `blocked/partial`; do not hammer it.
- If Indeed presents Cloudflare/CAPTCHA, stop rather than bypass it. After substantial work on other sources, one later retry is allowed. If still blocked, mark Indeed incomplete and continue.
- Health-check success is evidence that a source can work, not a guarantee that every later navigation will succeed.

#### Totaljobs recommendation-panel rule

Totaljobs can serve a page where the filtered result list never renders while the rest of the page paints normally. In that state `get_page_text` returns a personalised recommendation panel instead of the search results. That panel is identified by wording such as:

- `Suggested based on your CV`
- `Strong Fit`
- `Explore jobs that match your experience and skills`
- `Were these jobs of interest?`

That panel is NOT the requested result list. It ignores the selected posted-within filter, carries stale postings, and is largely off-profile. It must never be ingested as discovery inventory.

Check the extracted block mechanically rather than by eye:

```text
python tools/discovery_candidate.py check-panel --source-id totaljobs --file page.txt
```

A `do_not_ingest` verdict means the extraction captured the panel, not the results.

When the requested Totaljobs result list fails to render:

1. Retry once in a fresh tab or session context.
2. If it still fails, classify the source as `changed_layout` (or `partial` when some genuine results were readable).
3. Never ingest the recommendation panel, and never count its rows as candidates.
4. Continue with the remaining sources rather than hammering the site.
5. Report the source problem in coverage, so a lost source is never reported as `0 results`.

Never bypass a CAPTCHA or any other site protection.

#### CWJobs and Totaljobs promoted-card freshness rule

On both StepStone sites the active `Last 24 hours` / posted-within filter does NOT constrain promoted slots. Cards badged `PREMIUM` or `FEATURED` are served regardless of the selected window, and have been observed showing `1 week ago` and `1 month ago` under an active 24-hour filter.

Therefore:

1. A page-level date filter is never evidence that a card is fresh.
2. Read every candidate's own visible posted age or posted date before counting it toward a 24h, 7d or 14d threshold.
3. A promoted card that says `1 week ago` is a 7-day-old vacancy. It must not count as a 24-hour result.
4. When a card's age cannot be read at all, treat its freshness as unknown and do not count it toward a widening threshold.

Decide this deterministically rather than by eye:

```text
python tools/discovery_candidate.py window --posted-raw "1 week ago" --window-days 1
```

It returns `inside`, `outside` or `unknown` from the candidate's own date/age only.

#### Extraction method for StepStone sites

Prefer accessibility-tree extraction (`read_page`) for CWJobs and Totaljobs. Plain page-text extraction targets the largest article-like block, which on these sites can be a recommendation or FAQ panel rather than the result list. Confirm with a screenshot before trusting extracted text when a result list looks unexpectedly short or unexpectedly off-profile.

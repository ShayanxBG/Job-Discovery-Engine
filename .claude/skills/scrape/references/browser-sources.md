# Authenticated browser discovery

Reference for `.claude/skills/scrape/SKILL.md`. Read this ONLY when the run will use the signed-in Chrome session: the authenticated LinkedIn, Indeed, CWJobs or Totaljobs passes. A public-only or employer/ATS-only run never needs it.

This file is NOT loaded automatically. The main skill says when to read it.

### 1A. Authenticated browser discovery, preferred when available

Claude Code with Chrome can share the browser's existing login state. Where it is connected, use it for extra LinkedIn BREADTH on top of the guest endpoint, and for CWJobs and Totaljobs when those sites are permitted and readable. Do NOT use it for Indeed: that family is not queryable and attempting it is what puts a gap back into the run.

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

#### LinkedIn pass: two paths, different capabilities

LinkedIn is a PRIMARY inventory family again as of 2026-09-03 and holds nine
critical buckets. `config/search_strategy.json` owns that decision and
`config/sources.json` owns the measurement behind it. The 2026-08-31 removal was
right about the access path it tested and wrong about the inventory, so the two
paths are now used for different jobs:

- **Guest search endpoint - the COVERAGE path.** Dated, keyword-faithful, deeply
  paginable, and needs no browser. This is what discharges a critical bucket.
- **Authenticated card list - the BREADTH path.** Still unreliable for freshness
  and still virtualised. Use it to see roles the guest path may not surface;
  never use it to prove a window.

**NEVER pass `sortBy=DD` on either path.** It destroys keyword semantics. Verified
in production run `scrape-20260831T083115570281`: `Python Developer` and
`Integration Developer` returned 17 of 18 IDENTICAL job ids, `Python Django REST
Framework` returned Splunk Consultant and Document Review Specialist, and `Junior
Backend Developer` returned Water Process Engineer. Reproduced in a fresh tab.

##### Guest search endpoint (no authentication required)

```text
https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
  ?keywords=<terms>&location=United+Kingdom&geoId=101165590&f_TPR=<window>&start=<n>
```

It returns an HTML fragment of ten cards. Each card carries
`data-entity-urn="urn:li:jobPosting:<id>"` and a `<time datetime="YYYY-MM-DD">`.

Measured 2026-09-03, and these are the facts the promotion rests on:

- **Every card is dated.** 10 of 10 on each of 26 consecutive pages. This is what
  the authenticated list cannot do, and it is why the family may owe a 72-hour
  interval again.
- **Keyword semantics hold.** `Python Developer`, `Integration Developer` and
  `Junior Backend Developer` returned ZERO overlapping job ids, with titles that
  tracked the query.
- **`f_TPR` is honoured per item**, so a window claim is checkable per card
  rather than trusted from a chip.
- **It paginates to exhaustion, and the inventory is large.** `start` advances
  in steps of 10. Paged to exhaustion on 2026-09-03, ONE query, `Python
  Developer`, returned 332 unique dated vacancies on a 24-hour window in 40
  requests, 480 on a 7-day window in 59, and 615 when paged past `start=650`.
  Unique yield is close to linear with depth, about 0.88 new ids per card, so
  within one query there is very little duplication: stopping early simply
  discards inventory. The duplication is BETWEEN queries - the ten planned
  LinkedIn queries measured 1,336 rows for 674 unique, so budget for roughly
  50 percent overlap across the family, not within a query.

Location sharding, which is where most of the missing inventory is:

- The result set is capped PER SEARCH SCOPE, not globally, so the UK-wide query
  alone reaches roughly HALF the inventory. Run every query against the shard
  list in `guest_search_location_shards`, not just `United Kingdom`.
- Measured 2026-09-03 on `Python Developer` over 7 days, each scope paged to
  exhaustion: United Kingdom 493 unique, then London 341, Manchester 260,
  Birmingham 178 and Cambridge 214, of which **124, 207, 158 and 69 were
  vacancies the UK-wide query never returned**. Union 992 against 493.
- **They are RECALL WIDENERS, NOT LOCATION FILTERS.** Measured over 60 cards
  each: only 18% of the `Manchester` shard's results were actually in
  Manchester, 8% for `Birmingham`, and 10% for `Cambridge`, whose results were
  mostly London. Only `London` tracked its own label, at 90%. NEVER infer a
  vacancy's location from the shard that found it - read the card's own
  location field, which is what the state record must store.
- Overlap cannot be tuned away. `distance` is ignored in practice: a London
  search returned Glasgow and Newcastle at every value from 10 to 100. So
  deduplicate by job id across every scope and never add shard totals together.
- A guest card carries title, company, location, an ISO datetime, `Actively
  Hiring` and sometimes an applicant count. It carries NO salary, so
  `employment_conditions` can never be scored from a card. It also carries NO
  `Promoted` badge - zero in the fragment against 12 of 16 on the authenticated
  list - so the promoted-card freshness pathology does not apply to this path.
- Plain city strings resolve with no `geoId`. Prefer the short form: `Edinburgh,
  United Kingdom` works where `Edinburgh, Scotland, United Kingdom` returned
  nothing.

- `f_E` (experience level) is ALSO scope-separate, but it is second-order and is
  not part of the default shard list. Measured the same day against an unfiltered
  336: `f_E=1` internship added 36 new ids, `f_E=2` entry level 45, `f_E=4`
  mid-senior 89, and `f_E=3` associate returned nothing at all. That is roughly
  170 extra for four times the requests, against 499 from location sharding. Use
  it only in `/scrape exhaustive`, and only as a RECALL WIDENER: `f_E=2` returning
  128 where the unfiltered scope returns 336 is more evidence that LinkedIn's
  experience classification is unreliable, so it must never become a filter.

- **Do not try to exclude seniority in the query text. Both operators are traps.**
  Measured 2026-09-03 over three samples each: `Backend Developer` alone returns
  25% senior/lead/principal titles; `Backend Developer -senior` returns **87%**,
  so the `-` operator does not negate and appears to boost the term instead; and
  `Backend Developer NOT senior` paged to exhaustion returns **zero usable job
  ids** against 169 for the plain term, losing all 103 of its non-senior roles.
  A single short sample of the `NOT` form looks like a clean 0% senior, which is
  an empty result set rather than a filtered one - do not be fooled by it.
  Let the deterministic title and seniority gates drop those roles instead. They
  cost only a card fetch, they are counted in `hard_filtered`, and unlike a
  query-side exclusion they are auditable and cannot silently drop a junior role
  whose advert merely mentions a senior colleague.

Paging rules:

- Advance `start` by 10 and DEDUPLICATE BY JOB ID. Deep pages reshuffle, so a
  small number of ids repeat; `start=N` is not a stable slice.
- Keep paging while new unique in-window ids are still appearing. Stop on two
  consecutive pages that add no new in-window id, on a CONFIRMED empty page, or
  at `guest_search_max_start` from the registry. `guest_search_max_start` is 700
  and is deliberately below the measured hard boundary: 2026-09-04, `start=990`
  still returned ten cards and `start=1000` returned HTTP 400. It is not the
  binding limit in practice, because a 400-row query budget spread across five
  scopes stops each scope near `start=80` long before either number.
- **A card-less HTTP 200 is NOT evidence that the results ended.** LinkedIn guest
  throttles by returning an ordinary, well-formed 200 that simply has no cards in
  it. Measured 2026-09-04 on `Python Developer` over `r1209600`: `start=850` and
  `start=900` both came back empty, and both returned ten cards on every one of
  three immediate retries, while `start=950`, `975` and `990` answered normally
  throughout. Believing the first response would have stopped that query 15%
  short of its own budget.
- So RETRY a card-less page once before believing it, and only a page still empty
  on retry stops the loop. This is not a politeness rule, it is a correctness one:
  `empty` is a COVERING outcome, so a throttled page believed as empty both
  truncates the query and ADVANCES the bucket checkpoint, recording an interval
  as searched that was never read. That interval is then never searched again.
- If a page is still empty on retry at an offset where SHALLOWER pages returned
  cards, the source is throttling rather than exhausted: record the query
  `partial`, never `empty` and never `ok`, and leave the bucket unadvanced.
- `inspect_cards_per_query` in `config/sources.json` is the budget of RESULT ROWS
  READ for the WHOLE QUERY, spent ACROSS the shard list rather than per shard.
  It is 400 for LinkedIn. Read per shard it would mean 400 x 5 scopes x 11
  bootstrap queries = 22,000 rows against a run ceiling of 8,056: 2,200 requests
  for a pool the run has already said it will not consider.
- So spend it BREADTH-FIRST: round robin across the scopes, roughly 80 rows each,
  so every scope is sampled before any one is paged deep. That is also the better
  recall trade, because the measured value of sharding is that each scope returns
  vacancies the others never do.
- The row budget is NOT the same unit as `global_raw_candidate_ceiling`, which
  counts canonical unique candidates AFTER consolidation. Do not stop paging
  merely because the raw ceiling looks close; stop when this query's row budget
  is spent.
- Windows: `f_TPR` takes SECONDS, and every value this workspace needs is a true
  per-item filter. Verified 2026-09-03: `r86400` (24h) oldest 1 day, `r604800`
  (7d) oldest 6, `r1209600` (14d) oldest 13 across 120 cards, `r2592000` (30d)
  oldest 30 across 120. Nothing fell outside its window in any of them.
- **`r1209600` IS a true 14-day bucket**, which matters because the bootstrap and
  every recovery run search 14 days. An earlier note here claimed no 14-day
  bucket existed because LinkedIn's own UI labels that chip "Past month"; the
  label is wrong and the filter is not. Do NOT substitute `r2592000` for it: the
  per-query result set is capped, so a wider window spends that cap on older
  inventory instead of adding any - `r2592000` returned 178 unique of which only
  123 were inside 14 days, against `r604800`'s 166 all inside 7.
- So map the run window straight onto the parameter and use the SMALLEST value
  that covers the gap: 24h -> `r86400`, 7d -> `r604800`, 14d -> `r1209600`.

Per-vacancy description and posted date, for the roles worth reading:
`https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<jobId>`.

The DESCRIPTION BODY is isolatable there, which is what the cache rule requires.
It sits in the element carrying `show-more-less-html__markup` (older postings use
`description__text`); verified on three postings 2026-09-03, returning 3147, 659
and 1890 characters of clean advert text with no account chrome. Cache THAT as
`description_text`. Never cache the whole fragment: it carries LinkedIn's own
navigation and sign-in prompts. If the container is absent, cache nothing.

##### Authenticated card list (breadth only)

Use the normal search UI with natural-language phrasing and the supported Date
posted filter, letting relevance order the results:

```text
/jobs/search/?keywords=junior%20Python%20backend%20developer&location=United%20Kingdom&geoId=101165590&f_TPR=r604800
```

The card set is Promoted-dominated and the list virtualises: across three
verification searches only 3 of 16 cards carried any readable posted age while 12
were badged `Promoted`. So:

- Never count an undated card toward a window, and never credit a bucket from one.
- Take the date from the guest endpoint by job id rather than from the card.
- Authenticated inventory is still believed larger than the guest view, and that
  has NOT been measured. Never claim the two are equivalent in either direction.

Do not require Easy Apply. Do not rely on LinkedIn's experience-level
classification as a hard filter because employers classify roles inconsistently.

When visible, collect applicant count or `under 10 applicants` as an
informational signal only. It never changes fit by itself.
#### Indeed: NOT QUERYABLE, do not attempt it

**Indeed is out of scope until its review date. Do not open it, do not plan a
query against it, and do not record a source outcome for it.** The planner
already funds zero Indeed queries in every mode; attempting it anyway is the one
way to put the family back into a run, because a family only becomes a GAP once
it has been ATTEMPTED, and a gap makes the whole run PARTIAL.

That is not hypothetical. Indeed held nine critical buckets and drew ten of the
fifty-eight bootstrap queries for zero candidates, and its gap is why no run had
ever registered as successful, which kept the workspace in permanent catch-up.

**The reason is a rate limit that ends in a CAPTCHA, not a blanket block.** An
earlier note here said every path returned 403. That was wrong, and re-measuring
it on 2026-09-04 matters, because the wrong reason implies the wrong retry: a 403
invites a retry with different headers, and this does not.

What is actually true. The root returns 200 and an ordinary homepage. The classic
search returns **200 with 16 genuine job cards** on a cold request, and 403 or 307
while limited. `/m` returns 307 REDIRECTING to the classic search, not 403. The
legacy RSS path is 404. `/viewjob` is 403. `api.indeed.com` still does not resolve.

The limit is a HANDFUL of searches per extended cooldown, and the header set moves
that number, so it must be measured with the complete header set from
`references/public-sources.md`. Holding a cookie jar on a bare User-Agent, request
1 returned 16 cards and requests 2 to 5 each returned **HTTP 200 carrying a
reCAPTCHA page** with zero cards; paced at 15 seconds over 9 requests, 1 succeeded
and 8 returned 307. With complete headers after a cooldown, requests 1 and 2 each
returned 16 real cards and request 3 returned 403.

Note the shape of that failure: a CAPTCHA arriving under a 200 is the card-less-200
trap from the paging rules above, which is why neither this nor any other source may
be graded from a status code alone. Note also that TWO successes are no more usable
than one: a bootstrap query pages far past two requests, so the family still cannot
fund a bucket.

So the blocker is not reachability, it is that a CAPTCHA is never bypassed and one
query per run cannot fund a coverage bucket. Every run would record `partial`,
leaving the family in `GAP_REMAINS` and rebuilding the permanent-PARTIAL loop that
excluding it removed.

Much of the inventory is not lost: Indeed largely syndicates from employer ATS
feeds and other boards, which this workspace now reaches directly.

The extraction rules below are RETAINED for the review date, unchanged. Reinstate
the family only on evidence that a PACED sample sustains several consecutive
keyword-faithful searches without a reCAPTCHA page. A single successful request is
not that evidence: one has been available throughout.

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
- If ANY source presents Cloudflare or a CAPTCHA, stop rather than bypass it and
  record the outcome as `blocked_captcha`. Indeed is the reason this rule exists
  and is now out of scope entirely, so there is no retry to schedule for it: do
  not open it at all. For any other source, one later retry after substantial
  work elsewhere is allowed before marking it incomplete and continuing.
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

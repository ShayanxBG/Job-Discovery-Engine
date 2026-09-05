# UK Search Query Semantics

This file is NOT loaded automatically. The main skill says when to read it.

It explains how to search a source WELL. It does not define what a search family is, which titles are searched, how much budget anything gets, or which sources exist. Those are machine facts with a machine owner:

| Question | Ask |
| --- | --- |
| Which families exist, their budgets, term slots, stopping rules | `python tools/search_strategy.py list` |
| Which titles and terms this candidate searches under | `python tools/search_profile.py show` |
| The bounded, deduplicated plan for this run | `python tools/search_plan.py plan --mode <mode> --window <window>` |
| Which window this run should search, and why | `python tools/search_window.py select` |
| Which families pay off on a source, and cards worth inspecting | `python tools/sources.py get <source-id>` |

Nothing below repeats a title list, a budget or a source list. When prose and a tool disagree, the tool is right.

## Market and location

- United Kingdom only. Search UK-wide on every normal run.
- London is first preference; Cambridge and Oxford are also preferred.
- UK location NEVER reduces fit. Preferred locations are tie-breakers only, because the candidate will relocate anywhere in the UK for the right role.
- Remote, hybrid and on-site are all acceptable.
- Extra preferred-location passes are allowed but never replace the UK-wide pass.

## Freshness

The window comes from `python tools/search_window.py select` and is decided by run history. A low result count never widens it. Within the same result category and fit band, newer verified postings rank ahead of older ones.

Prefer a source-side date filter wherever `freshness_support` is `reliable-filter`. Where it is `unreliable-filter`, read each card's own posted age instead: a promoted card ignores the page filter.

## What each family is actually asking

The planner builds the text. This is what the family MEANS, which is what tells you whether a result belongs.

- **direct-title** asks for the job by its name. Highest signal, easiest to over-search: ten variations of one title are one family, not breadth.
- **backend-capability** asks for the WORK rather than the name, so a well-fitting vacancy with an unusual title is still found. More noise, same gates.
- **adjacent-software** asks under generic engineering titles whose adverts may never name Python. Its results require the body-signal gate before a deep read, because the title alone carries no backend evidence.
- **early-career** asks under junior, associate, graduate and entry-level constructions. It is reserved budget, not leftovers: it is one of the two families most likely to hold this candidate's vacancy.
- **sponsorship-oriented** asks with Skilled Worker, visa sponsorship and licensed-sponsor wording. It SUPPLEMENTS ordinary searches and never replaces them, because most viable adverts say nothing about sponsorship at all and would be invisible to a sponsorship-only search.
- **employer-ats** asks a known employer's own requisition feed. Driven by the bounded watchlist and resolved employer entities, never by enumerating a sponsor register.

Do not search as if AWS, Azure, Kubernetes, production LLM integration, agent frameworks or MCP are established experience.

## Authenticated browser passes

When Chrome is connected and `/scrape public` was not requested, search LinkedIn and Indeed while signed in, and CWJobs and Totaljobs directly rather than through search-engine indexing. The signed-in inventory is materially larger, and `inspect_cards_per_query` in the source registry says how deep to go on each.

- Sort newest-first where available; apply the posted-window filter where the source supports one.
- Continue scrolling while unique plausible cards keep appearing. Stop a query after two consecutive result screens dominated by duplicates or off-profile roles.
- Never use Easy Apply as a requirement, and never treat a platform experience-level label as a hard filter.
- Applicant counts and `under 10 applicants` are informational context only.
- Profile recommendations are supplemental leads, never automatic evidence of fit.
- Indeed: preserve the `jk`/`vjk` identity rules. Distinct ids stay distinct; tracking parameters never create a new identity.
- Totaljobs: the `Suggested based on your CV` / `Strong Fit` panel is NOT the result list. Never ingest it. Retry once in a fresh tab, then record `changed_layout` or `partial`.

Public search-engine fallback is incomplete by design. Report it as fallback coverage, never as full inventory for that source.

## Site-restricted query syntax

Only the shapes, because the terms come from the planner:

- Employer ATS: `site:job-boards.greenhouse.io`, `site:boards.greenhouse.io`, `site:jobs.lever.co`, `site:jobs.ashbyhq.com`, `site:jobs.smartrecruiters.com`, `site:apply.workable.com`, `site:myworkdayjobs.com`
- Employer careers: `"<title>" careers United Kingdom -recruitment-agency`
- Candidate resolution: `"<company>" "<exact title>" careers`, `site:<known company domain> "<exact title>"`
- Boards: `site:welcometothejungle.com/en/companies`, `site:jobserve.com`,
  `site:technojobs.co.uk`, `site:adzuna.co.uk`. Only these four need a search
  engine. CWJobs, Totaljobs, Reed and DWP are fetched directly, and so is Built In:
  `https://builtin.com/jobs/search?search=<terms>` was verified query-faithful on
  2026-09-03, returning 25 job links for `python developer` and a COMPLETELY
  DISJOINT 25 for `nurse`. Built In was recorded as a family gap on 2026-09-02 only
  because a worker `site:` query found nothing on it.
  Two of the four genuinely cannot be fetched, which is why they keep the worker
  route: `adzuna.co.uk` returns HTTP 403 to a direct fetch, and
  `www.technojobs.co.uk` has no DNS record at all while the apex times out. Record
  those as `blocked` and `unavailable`, never as `empty`.
- Sponsor boards: FETCH THESE DIRECTLY, do not delegate them as `site:` queries.
  Every one returned `unavailable` on 2026-09-01 and 2026-09-02 purely because a
  worker's `site:` restriction found no on-domain pages, and all five were then
  confirmed live and fetchable on 2026-09-03. That is the search-engine failure
  described below, not a board failure, and it wasted sixteen queries.
  Only two of them apply the query at all, measured the same day by fetching one
  page for `python developer` and one for `nurse` and comparing the results:

  | Board | Direct search URL | Applies the query? |
  | --- | --- | --- |
  | Hunt UK Visa Sponsors | `https://huntukvisasponsors.com/jobs?q=<terms>` | YES |
  | SkilledJobs | `https://www.skilledjobs.com/visa-sponsorship-jobs?q=<terms>` | YES |
  | SponsoredJobs | `https://sponsoredjobs.co.uk/jobs?q=<terms>` | NO, 100% identical results |
  | FindSponsorJobs | `https://findsponsorjobs.co.uk/jobs?q=<terms>` | NO |
  | GradSponsor | `https://gradsponsor.co.uk/jobs?q=<terms>` | NO, byte-identical page |
  | JobSponsor | `https://www.jobsponsor.uk/` | NO, credit-gated |

  SkilledJobs returns 41 results for `nurse` and ZERO for `python developer`, so
  it genuinely holds no Python backend inventory. Record that as `empty`, never as
  `unavailable`. Count its results from the page's own `<n> jobs` phrase: the
  search box echoes the query back, so counting the word `python` in the HTML
  measures the echo and not the results. The four that ignore the query stay
  enabled as supplemental lead sources and can never be recorded as query coverage.

DWP Find a Job is deliberately NOT in that list. Fetch `https://www.jobs.service.gov.uk/jobs/search?keywords=<terms>` directly.

It supports exact filters, which is why it is a primary inventory family:
`postingDateRange=1|3|7|14`, `sortOption=RELEVANCE|DATE|SALARY_DESC|SALARY_ASC`
and `resultsPerPage=10|20|30`. Verify the filter took effect from the headline
count: `Python Developer` fell from 577 unfiltered to 342 under
`postingDateRange=14`.

Its keyword matching is token-OR, so **sort by RELEVANCE for broad multi-word
titles**. Under `sortOption=DATE` a broad title returns whatever is newest on ANY
token: `Junior Software Engineer` returned Junior Sous Chef, Junior Poojari and
Junior Fire Alarm Engineer. The same query under `sortOption=RELEVANCE` returned
genuine software roles. Precise capability queries (`Python Django`, `Python
FastAPI`, `Python REST API`) are narrow enough to be correct under either sort.

Note also that this host is not reachable from the general-purpose fetch tool
(five of five timeouts, 2026-08-31). Use the browser session.

### A search engine is not a source health check

Judging whether a source holds inventory by whether a `site:` restriction returns results is unsound, and it produced a false conclusion in the first real run. `site:jobs.service.gov.uk` was silently rewritten by the search backend and came back with `itjobswatch.co.uk` pages, which was read as the DWP service having moved to IT Jobs Watch. It had not moved anywhere: a direct read-only fetch of the service's own search URL returns hundreds of results with per-vacancy listings.

So when a `site:` query returns nothing, or returns another domain entirely, that is evidence about the SEARCH ENGINE and never about the source. Record the source outcome from a direct check of the source's own search URL. IT Jobs Watch is a salary-trend aggregator and is never a substitute for any board's inventory.

Split delegated board work into small source groups rather than one all-boards worker.

## Sponsor-focused boards are leads, never truth

They are useful lead generators because sponsorship is a major constraint, and they are never sources of truth for vacancy-specific sponsorship. For every promising lead, resolve the exact vacancy to an employer career page, ATS page, authenticated posting or major-board copy before presenting it.

## Agency leads

Recruitment agencies are not automatically excluded. Retain a strong agency advert as an Agency Lead when Python, backend or application engineering is a strong match, the role appears permanent or plausibly permanent, there is no explicit no-sponsorship blocker, and the client is hidden or sponsorship cannot yet be tied to the employing entity.

- Strong: strong technical match, realistic level, client or sponsorship unresolved.
- Stretch: strong enough to justify a recruiter question, plus one additional material concern.
- Drop: multiple additional concerns, wrong role type, contract or day rate, or an explicit sponsorship blocker.

Do not convert a weak role into an Agency Lead just because a recruiter posted it, and do not search agencies as a priority source at the expense of direct employers.

## Strong negative filters

Cheap drops, before anything is opened. The experience thresholds are calibrated and live in `candidate/config.json`; `python tools/candidate_config.py show --compact` is authoritative and the wording below never overrides it.

Drop by default: senior, staff, principal, lead, head or architect titles; frontend-only; DevOps or SRE-only; data science or ML research; quant or data engineering where NumPy, Pandas, Airflow, C++ or trading analytics are primary and backend application work is secondary; Java-only, C#/.NET-only or PHP-only with no material Python; support, helpdesk or service desk; apprenticeship; explicit no sponsorship; permanent or unrestricted-right-to-work language that rules out current or future sponsorship; and salary clearly below the live applicable sponsorship floor.

Two wordings that are commonly misread:

- Ordinary wording that a candidate must currently have the right to work in the UK is NOT automatically a blocker. Distinguish it from `no sponsorship`, `must not require sponsorship`, `indefinite leave` or equivalent restrictive wording.
- A bare mention of the word `contract` is not an exclusion. A fixed-term contract of employment is an employment type this candidate accepts; independent contracting, day rate, outside IR35 and umbrella arrangements are not. Where the advert does not distinguish them, it is ambiguous, which is a reason to verify rather than to drop.

## Direct-match discipline

A High Direct Match usually has most of: Python central day-to-day; Django, FastAPI or strongly adjacent Python web and API work; REST, API or integration responsibility; PostgreSQL or SQL relevance; production software development; a realistic junior-to-mid level; a permanent or fixed-term employment relationship; no known sponsorship blocker; and no meaningful unresolved issue beyond minor uncertainty.

A Medium Direct Match has exactly one meaningful unresolved issue. With several, drop it from Direct Matches rather than filling space.

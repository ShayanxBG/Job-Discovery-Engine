# Deep vacancy processing

Reference for `.claude/skills/scrape/SKILL.md`. Read this once a candidate has survived the cheap gates and is worth fetching: the body-signal gate, employer resolution, the watchlist, the classification rules, the fetch and cache contract, freshness, identity and the state write.

This file is NOT loaded automatically. The main skill says when to read it.

### Cheap body-signal gate for broad searches

Adjacent-title families search generic titles, so they return more noise by design. Gate that noise deterministically before any model reads a posting:

```text
python tools/discovery_candidate.py body-signal --title "Software Engineer" --file body.txt
```

- `KEEP_FOR_DEEP_CHECK`: several DISTINCT specific backend signals are present, so the vacancy earned a full read
- `LOW_SIGNAL`: too few signals, only incidental ones, or counter-signals for another specialism outweigh them
- `HARD_REJECT`: only when an existing deterministic blocker already applies. Never inferred from body text alone

One keyword is never enough. A React role that lists Python once under "nice to have" must not be promoted, which is why the gate requires more than one distinct signal, discounts terms as common as `api`/`sql`/`python`, and weighs frontend counter-signals against them. This is a GATE, not a score: `KEEP_FOR_DEEP_CHECK` means "worth reading", never "good match".

### Resolve promising board listings to the employer

For a promising candidate from LinkedIn, Indeed, CWJobs/Totaljobs, Reed or an aggregator, resolve the employer before deep scoring where practical:

```text
python tools/employers.py check-batch --file candidates.json
```

The employer cache stores what an employer IS: canonical name, confirmed aliases, website domain, careers URL, ATS platform and tenant, sponsor-register name. Resolution is deliberately conservative: exact, legal-suffix, explicit alias, or domain evidence. A weak substring match NEVER resolves automatically, because `One` substrings `AXONE` and `Sky` substrings `Kaspersky`, and a wrong merge silently attaches one company's sponsorship evidence to another.

Record the resolution outcome on the candidate: `resolved_official`, `resolved_ats`, `unresolved`, `agency` or `employer_unknown`.

An unresolved employer stays perfectly usable. Never discard a good vacancy because official resolution failed. It simply keeps its board-level source confidence and provenance. When resolution DOES succeed against an employer/ATS page, that is a provenance upgrade: the ATS URL becomes the preferred source and the board copy stays as secondary evidence.

Resolve each employer once per run. The employer cache and the job cache exist so the second and third sighting cost nothing.

### Bounded employer/ATS watchlist

```text
python tools/ats_budget.py tasks --run-id <run_id>
python tools/ats_budget.py outcome "<employer_key>" --run-id <run_id> [--failed]
python tools/watchlist.py mark-checked "<employer>"
```

The watchlist is capped at 60 active employers, and the cap is deliberate. Enumerating the whole sponsor register against ATS platforms would be an unbounded crawler: enormous budget spent overwhelmingly on employers with no relevant vacancy. An employer earns a place by evidence (`strong_match`, `sponsor_evidence`, `manual`, `known_ats`, `recurring`), never by having merely been seen once, and every entry records the evidence in checkable words.

The per-run ceiling is ENFORCED by reservation, not by counting afterwards. `ats_budget.py tasks` takes capacity and then hands back the tasks, so a check that never reserved cannot happen and a caller cannot ask for more than the mode allows. A failed check still spends its slot: the fetch happened, and refunding it would let one dead careers page consume the whole run one retry at a time. Reaching the ceiling is a bounded stop, so deferred employers stay enabled and due rather than being recorded as a failure.

`due` returns the most promising, most stale entries first. Where an entry has a known ATS platform and tenant, that becomes a targeted `employer-ats` search task; where only a careers URL is known, it becomes an `employer-direct` task. Never invent an ATS URL for an employer whose tenant is unknown.

### Seniority rules

- Senior, Staff, Principal, Lead, Head, Architect: drop by default.
- 5+ years hard minimum: always drop.
- 4+ years hard minimum: drop by default. Keep only if the advert explicitly makes the number flexible/non-mandatory and the duties are clearly junior-to-mid despite the wording.
- 3+ years hard minimum: keep only when the technical fit is unusually strong and the role is otherwise realistic; Quick Fit cannot exceed Medium unless the requirement is clearly flexible.
- `2+ years` or equivalent is realistic and stays in scope when the rest fits.

Do not infer years from a title alone. Read the requirements when the title is ambiguous.

### Role-content rules

Python must be material to the day-to-day work and backend/application engineering must be central.

Drop by default:

- frontend-only
- DevOps/SRE-only
- data science/ML research
- quant/data-engineering roles where NumPy/Pandas/Airflow/trading analytics/C++ are primary and web/API/backend application work is secondary
- Java-only, C#/.NET-only, PHP-only roles with no material Python
- support/helpdesk/service desk
- apprenticeship
- contract, day-rate, outside-IR35, temporary-only

Full Stack is allowed when Python backend work is material and frontend requirements are realistically adjacent.

### Direct-employer gap discipline

A normal Direct Match may have at most one meaningful unresolved issue.

Meaningful issues include:

- over-levelled seniority
- wrong/adjacent primary stack
- weak or unresolved sponsorship
- salary viability risk
- unclear permanent employment
- substantial cloud/tool gap that is central to the role

High = strong fit with no material blocker and at most a minor uncertainty.

Medium = exactly one meaningful unresolved issue.

If a direct-employer role has several meaningful issues, drop it from the normal shortlist rather than rescuing it because one part looks attractive.

A `Moderate` sponsorship label is not automatically a meaningful issue when the Skilled Worker licence/entity is verified, salary appears viable, and the vacancy contains no negative sponsorship wording. A genuinely unresolved route/entity conflict, weak evidence, or decision-critical sponsorship ambiguity is a meaningful issue.

### Verification Lead rules

Use a separate `Verification Lead` category very selectively for a direct employer when the technical fit is already unusually strong but the role cannot honestly qualify as Direct High/Medium because of a decision-critical fact that still needs external verification.

A Verification Lead may have:

- one hard gating uncertainty (for example conflicting Skilled Worker route/entity evidence)
- plus at most one additional manageable concern (for example a 3-4 year requirement on an otherwise exact-stack mid-level role)

Do not use Verification Leads to rescue ordinary weak jobs. Python/backend relevance must already be strong and the exact next verification action must be clear. Verification Leads never satisfy widening thresholds and are not presented as recommendations until the gate is resolved.

Store these with `lead_type: verification`. If later evidence resolves the gate and the role becomes a genuine Direct Match, merge the same vacancy and reopen it as an Updated Lead.

### Agency Lead rules

Agency/recruiter adverts are NOT automatically bad and must not be dropped solely because the client is undisclosed.

Create a separate `Agency Lead` category when:

- the technical/job-function fit is genuinely strong or useful
- the role is permanent or plausibly permanent
- there is no explicit no-sponsorship or permanent/unrestricted-right-to-work blocker
- the hidden client or unclear employing entity prevents reliable sponsorship verification

Agency Leads may have the client/sponsorship uncertainty by definition plus at most one other material concern. If the role is clearly over-levelled, wrong-primary-stack, contract/day-rate, or otherwise poor, drop it.

For each Agency Lead, surface the recruiter-verification action:

1. identify the actual employing client if possible
2. confirm whether that client can consider the candidate's current right-to-work position and any future Skilled Worker sponsorship need recorded in `candidate/profile.md`
3. clarify actual seniority/salary expectations where the advert is ambiguous

Do not contact the recruiter during `/scrape`.

Agency Leads and Verification Leads are excluded from the reported NEW-direct count. That count is a market observation only: it does not change the search window, which comes from run history.

Ordinary wording that a candidate must currently have the right to work in the UK is not automatically a blocker because the candidate currently has work permission. Distinguish it from `no sponsorship`, `must not require sponsorship`, `indefinite leave`, permanent/unrestricted rights, or equivalent restrictive wording.

## Step 3: Fetch and verify promising postings

For each promising hit, fetch/read the actual posting. Prefer the employer page.

Extract and normalise:

- title
- company/legal employer if identifiable
- lead type: `direct` or `agency`
- UK location
- remote/hybrid/on-site
- posted date as ISO `YYYY-MM-DD` where possible
- whether it is explicitly a repost
- source-local job ID when visible
- employer/ATS requisition ID when visible
- applicant count if visible
- salary
- employment type
- core stack
- experience/seniority requirement
- sponsorship/right-to-work wording
- source URL
- source type
- source confidence

Source confidence:

- High: employer-direct career page or ATS page.
- High: authenticated LinkedIn/Indeed page clearly tied to the employer and current posting.
- Medium: major UK job board with full posting detail.
- Low/Medium: sponsor-focused or other aggregator until resolved to an employer posting.

### Cache the extraction

Once a posting has been fetched and read, store the description and the structured facts so the same advert is never fetched and re-interpreted twice in one cycle:

```text
python tools/job_cache.py put --url <url> --run-id <run_id> --open-status open --file facts.json
python tools/job_cache.py get --url <url> --run-id <run_id>
```

Cache policy:

- Description and structured facts are two separate evidence classes. Read `reuse_description` and `reuse_facts` independently; never treat "cache fresh" as one undifferentiated fact.
- Within the same run, description/facts actually fetched by this run are reusable for that evidence class, whatever their age. A metadata-only or open-status-only write by the current run grants no reuse.
- Description text stays reusable for 72 hours from `description_fetched_at`, and structured facts for 72 hours from `facts_fetched_at`. Refreshing one class never refreshes the other.
- A cached open/closed observation ages out after 12 hours and is never a substitute for a live check before presenting a high-priority recommendation.
- A changed `description_hash` marks the advert as materially rewritten, so a refresh is required rather than optional.
- Entries older than 30 days are pruned.

The cache schema contains vacancy and source fields only, and a field whitelist is enforced at the write boundary, so a credential, cookie, browser-session or candidate-profile field name is refused outright. That is a schema guarantee, not a content guarantee: an allowed free-text field such as `description_text` cannot be proven to hold only vacancy text. Pass vacancy and source content only. The cache never intentionally receives profile or credential data.

### Never cache a raw search or results page

`description_text` is the SELECTED VACANCY'S OWN JOB-DESCRIPTION BODY. Nothing else goes in it.

An authenticated search or results page is not a vacancy. It is a personalised interface built around your account, and the health check confirmed exactly that on the real sites: the Indeed results page carried a commute estimate computed from the signed-in user's own saved home address, and the Totaljobs results page served a `Suggested based on your CV` panel derived from the CV uploaded there. Caching either verbatim would write the user's own private data into a vacancy cache under a perfectly legitimate field name.

So the order is:

```text
SEARCH / RESULTS PAGE
  -> extract card fields + the vacancy URL ONLY
     (title, company, location, posted age, salary text, promoted markers)
  -> python tools/url_safety.py check <url>
  -> open the SELECTED VACANCY
  -> isolate the job-description body
  -> python tools/job_cache.py put --url <url> --description-file <body>
```

Never pass to the cache: a whole LinkedIn/Indeed/CWJobs/Totaljobs search or results page, a recommendation panel, a sidebar, a commute widget, an authenticated account page, or a search-engine results page.

Card fields extracted from a results page are structured discovery data and are exactly what you should take from it. The page text itself is not.

If the vacancy body cannot be isolated, cache NOTHING and record the description as unavailable. An absent description is a known unknown; a page-level capture is silent contamination that nothing downstream can detect.

#### The vacancy page's own platform furniture

A LinkedIn vacancy page appends LinkedIn's own block after the employer's text:

```text
...end of the employer's text
Show more / Show less
-  Seniority level    Mid-Senior level
-  Employment type    Contract
-  Job function       Information Technology
-  Industries         Legal Services and Law Practice
Referrals increase your chances of interviewing at <Company> by 2x
See who you know
```

All 22 LinkedIn-sourced entries in the first production cache carried it. Cut it before you cache. `Seniority level` and `Employment type` are LINKEDIN'S classification of the role, not the employer's words, and they are exactly the fields that drive the `seniority` and `contract` hard blockers, so treating them as advert text attributes to the employer a statement it never made. The block also enters `description_hash`, so a change in LinkedIn's furniture would read as a changed advert.

`python tools/job_cache.py put` applies the split for you as a last line of defence, keeping the employer's text in `description_text` and LinkedIn's classification under `platform_metadata` with `platform_metadata_source` naming who asserted it. Check what it will do first when useful:

```text
python tools/discovery_candidate.py split-chrome --source-id linkedin --file <body>
```

Never treat `platform_metadata` as employer-stated evidence, and never let it fire a hard blocker on its own. If LinkedIn says `Employment type: Contract`, that is a reason to read the advert for the employer's own words, not a contract blocker.

If what you extracted is ONLY that block, you did not find the advert. `split-chrome` reports `description_unavailable: true`, the cache stores no description, no hash and no description clock, and the correct record is that the description is unavailable. Do not store the block as the vacancy body: an absent description is a known unknown, and `Seniority level / Mid-Senior level` filed as the employer's words is exactly the contamination this boundary exists to prevent. Open the vacancy again, or record it as unavailable and move on.

The cache keeps four separate clocks. `cached_at` is when the file was rewritten and never decides reuse; `description_fetched_at` moves only when a write actually supplies a description; `facts_fetched_at` moves only when a write actually supplies structured facts; `open_status_checked_at` moves only when a write supplies an open/closed observation. A metadata-only `put --company` therefore leaves every evidence class exactly as stale as it was, and a facts-only refresh leaves an eight-day-old description eight days old. `fetched_at` survives only as a derived summary of the newer class clock and is never the authority for either class.

Run provenance is split the same way. `run_id` is metadata naming the run that last touched the entry and never makes evidence reusable; `description_run_id`, `facts_run_id` and `evidence_run_id` name the run that actually fetched each class. Pass `--run-id` on every put so the run that genuinely fetched a class owns it, and remember that passing it on a metadata-only put buys nothing.

### Freshness verification

A job only belongs inside the requested time window when there is credible evidence for its posting/reposting date.

- Prefer the employer/ATS published date when available.
- Authenticated LinkedIn/Indeed posted-age labels are valid discovery evidence.
- Aggregator `updated` dates can be misleading. Do not let an aggregator refresh date override an older employer posting date.
- If the date cannot be verified, label it `Date unverified` and sort it below dated roles in the same category.
- If the posting is outside the requested window, closed, expired, deleted, or clearly stale, do not present it.

When a search result is an aggregator but the role looks strong, search the company + exact title and resolve it to the employer/ATS URL before saving when possible.

## Step 5: Quick profile fit and lead classification

This is not the full `/rank` calculation.

Direct Matches:

- High: strong Python/backend alignment, realistic junior-to-mid level, no hard blocker, no meaningful unresolved issue beyond minor uncertainty.
- Medium: plausible direct-employer role with exactly one meaningful unresolved technical, seniority, salary, or sponsorship issue.
- Low: substantial mismatch or multiple meaningful gaps. Do not present Low jobs.

Verification Leads:

- Keep only exceptionally relevant direct-employer roles that fail the strict Direct one-gap rule because a decision-critical external fact remains unresolved.
- State the gate, the additional concern if any, and the exact verification action.
- Do not assign High/Medium Direct fit until the gate is resolved.

Agency Leads:

- Strong: strong technical/job-function fit and realistic enough seniority, but client/employer sponsorship needs recruiter verification.
- Stretch: strong enough technical relevance to justify a recruiter question, with one additional material concern such as salary/seniority ambiguity.
- Drop: multiple additional problems, wrong role type, explicit no-sponsorship wording, or unsuitable contract.

Specific truth rules:

- AI coding-tool usage is not LLM application integration.
- Azure DevOps is not Azure cloud.
- React project evidence is not commercial React.
- Django/FastAPI adjacency is useful, but do not invent exact framework experience.

## Step 6: Deduplicate safely, recognise upgrades, and save

Use `tools/job_state.py` for all state reads/writes.

The deduplication goal has two equal priorities:

1. Do not show the candidate the same vacancy repeatedly across days or sources.
2. Do not accidentally hide two genuinely different vacancies just because a large employer reused the same title.

### Identity hierarchy

Treat as a definite duplicate when one of these matches:

1. exact normalised source URL/job identity
2. same employer + employer/ATS requisition ID
3. same source host + same source-local job ID

The URL normaliser preserves identity-bearing query parameters such as Indeed `jk`, while removing tracking parameters.

Company + title + location is a probable cross-source duplicate, NOT an automatic merge.

Company + title without matching location is only a possible duplicate.

Workflow for every candidate before `add`:

1. run `python tools/job_state.py check ...`
2. if it reports a definite duplicate, update that entry
3. if it reports possible duplicates, compare source, location, requisition/job IDs, posted date and job details
4. if it is genuinely the same vacancy, call `add --merge-key <key> --reopen-on-upgrade`
5. if uncertain or clearly a separate requisition, save it separately rather than hiding it

A previously seen vacancy may reappear only as an `Updated Lead` when there is a material improvement, for example:

- aggregator copy resolved to employer-direct/authenticated source
- verified newer repost/requisition date
- sponsorship evidence improves materially
- fit improves materially because missing information is resolved
- agency lead becomes a direct-employer lead because the actual client is identified

`--reopen-on-upgrade` sets the state to `updated`, not `new`. A `verification` lead resolving into a `direct` lead is a material lead-type improvement and should reopen as updated.

For an existing record, `add --status ...` does not change lifecycle state. Use `mark` for an intentional status change. An ordinary duplicate must not become `new` merely because discovery supplied `--status new`.

When the preferred source is already stronger than a rediscovered board/aggregator copy, the weaker copy must not replace `quick_fit`, `fit_band`, `sponsorship`, `sponsorship_label`, `lead_type`, `filter_reason`, or source-owned IDs. If authority moves to a stronger host and that host has no source-local `job_id`, the old weaker-host job ID is cleared rather than attached to the new host.

Do not re-present ordinary duplicates already in `seen_jobs.json`.

Store at least:

- title
- company
- url
- location
- posted
- first_seen
- last_seen
- last_verified
- quick_fit
- fit_band
- lead_type
- sponsorship
- sponsorship_label
- source
- source_type
- source_confidence
- source_host
- source-local job ID when available
- employer requisition ID when available
- status
- filter_reason where relevant

### Machine vocabulary versus human evidence

`quick_fit` and `sponsorship` hold readable evidence for the user. They are prose and are never ranked by state logic.

Every save must ALSO set the two machine-readable fields, because upgrade detection compares only these:

- `--fit-band` one of `unknown`, `low`, `medium`, `high`
- `--sponsorship-label` one of `unknown`, `blocked`, `weak`, `moderate`, `strong`

Rules:

1. Set `fit_band` from the Direct Match calibration only. High, Medium and Low map directly. Agency `Strong`/`Stretch` and Verification `Gated` are a different scale, so their `fit_band` is `unknown`.
2. Set `sponsorship_label` from the discovery label you actually assigned in Step 4. If the evidence does not support one of the five values, use `unknown` rather than guessing. Read the vacancy body with `python tools/discovery_candidate.py sponsorship-signal --file <body>` rather than judging the wording yourself; it separates what the ADVERT offers from what the ORGANISATION holds. An explicit offer to sponsor is `strong`; a licence claim such as `We are a licensed sponsor` is only `moderate` and carries `requires_live_check`, because a licence is organisation-level capability and the official register grants that identical fact no more than `moderate`; a refusal is `blocked` and negation always wins; and silence is `unknown`, never a negative.
3. Never derive either field from an employer's reputation or from job-advert wording alone.
4. `job_state.py` rejects a value outside these vocabularies, and also rejects an invalid `lead_type`, `status`, `source_type` or `source_confidence`, so a bad token fails loudly at the write instead of corrupting upgrade detection.

Because `materially_improved` reads `fit_band` and `sponsorship_label`, omitting them means a genuine sponsorship or fit improvement will not reopen the vacancy as an Updated Lead.

### Persist the structured facts you already extracted

Discovery routinely reads a salary, an experience requirement, a work pattern and a stack, then throws them away. Keep them:

```text
python tools/job_state.py add ... --facts '{"salary_min": 55000, "salary_currency": "GBP", "employment_type": "permanent", "work_pattern": "hybrid", "years_required_min": 2, "skills": ["Python", "Django"], "posted_raw": "3 hours ago", "description_hash": "..."}'
```

Rules:

1. `facts` is additive and optional. A record with no facts stays valid, and existing records are never rewritten merely to add an empty facts object.
2. Facts hold what the vacancy actually stated. Leave a fact out rather than filling it with a guess. Unknown stays unknown.
3. Facts are machine fields and are kept strictly separate from the evidence prose in `quick_fit` and `sponsorship`.
4. A value outside the controlled vocabulary is rejected at the write boundary.
5. A weaker rediscovery may fill a fact that is currently absent, but may not overwrite a fact already recorded from a stronger source.

Persisting facts is what allows `/rank` to score without re-reading the same job description.

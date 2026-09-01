# Sponsorship verification

Reference for `.claude/skills/scrape/SKILL.md`. Read this when sponsorship evidence could change whether a role is worth keeping, which is most deep checks and no cheap ones.

This file is NOT loaded automatically. The main skill says when to read it.

### Sponsorship: local first, live second

At run startup, check the official register snapshot ONCE:

```text
python tools/sponsor_register.py status
```

If it reports `refresh_needed` (missing, or older than the 24-hour target), attempt exactly ONE refresh for the whole run:

```text
python tools/sponsor_register.py refresh --allow-network
```

Then continue regardless of the outcome. Never refresh per employer. If the refresh fails and a snapshot exists, it is retained and marked stale and the run carries on with a warning; if it fails and none exists, local lookups are `UNAVAILABLE` and sponsorship falls back to live verification. A GOV.UK outage must not break discovery.

Every employer check after that is local:

```text
python tools/sponsor_register.py check "<employer>"
```

Follow this order, which is designed so straightforward licence questions never cost a web search:

```text
employer discovered
  -> resolve employer entity            (tools/employers.py)
  -> sponsorship evidence cache         (tools/sponsorship_evidence.py get)
  -> local official register snapshot   (tools/sponsor_register.py check)
  -> on a credible match, store dated register evidence
     (tools/sponsorship_evidence.py add-register --employer "<employer>")
  -> otherwise try known legal aliases / recorded registered name
  -> only if still unresolved AND sponsorship matters: live GOV.UK/employer check
```

Read the four lookup results exactly as they are meant:

- `FOUND` is employer LICENCE evidence. It is not evidence that this vacancy will be sponsored, that the role meets the going rate or skill level, or that the licence is valid today. `requires_live_check` stays true.
- `NOT_FOUND` means no credible match in THIS snapshot under the legal-entity names we know. Registered legal names routinely differ from trading names, so never write or imply that the employer cannot sponsor. Record the registered legal name on the employer entity when you learn it.
- `AMBIGUOUS` means two distinct registered organisations matched. Do not guess between them; record the exact registered name or verify live.
- `UNAVAILABLE` means there is no usable snapshot. It is not a negative result and must never be reported as one.

Routes matter. The register lists them verbatim, and a licence for an unrelated route is not the evidence a Skilled Worker vacancy needs, so check `has_skilled_worker_route` rather than treating any licence as sufficient.

`tools/check_sponsor.py` still queries `data/uksponsorregistertechsubset20260812.csv`, but that is a dated 2026-08-12 filtered tech/consultancy subset with no route column. Use it as a cheap supplementary lead signal only. Absence from it proves nothing, and once a snapshot is installed `sponsor_register.py` is the official lookup.

Live verification is the SECOND option, reserved for an ambiguous entity, a stale or unavailable snapshot, a recent employer change, a decision-critical confirmation, or vacancy-specific sponsorship behaviour.

### Reuse sponsorship research

```text
python tools/sponsorship_evidence.py get --employer "<employer>"
```

Sponsorship status is DERIVED from unexpired evidence, never stored as a boolean. Each item carries its kind, source, observation time and expiry, and the evidence ladder is unchanged:

- a `sponsor_register` hit supports at most `moderate` and always sets `requires_live_check`
- `employer_statement` and `vacancy_statement` can support `strong`
- `absence_statement` blocks
- corroboration across kinds raises confidence; repetition of one weak source does not

A licence on the register means the ORGANISATION holds one. It is not evidence that this vacancy will be sponsored, that the role meets the going rate or skill level, or that the licence is still valid today. Evidence expires, and when the last supporting item expires the derived status falls back to `unknown` on its own. A decision-critical sponsorship conclusion still needs live verification.

This store is not ranked/dismissed job state and not preference learning. It is a cheap cache of deterministic rejections; deleting `job_scraper/suppression.json` only costs one extra pass of cheap filtering.

## Step 4: Sponsorship quick check and verification

For each technically plausible candidate, run:

`python tools/check_sponsor.py "<Company Name>"`

Interpret correctly:

- local register hit = Worker licence evidence only, never proof this vacancy sponsors
- local miss = inconclusive, never proof there is no licence
- explicit advert wording outranks the local register
- sponsor-board badges/claims are leads, not final evidence

For direct High/Medium candidates where sponsorship uncertainty changes whether the role is useful, use WebSearch, the employer careers page, and/or the `sponsor-verifier` subagent to look for current public evidence.

Discovery labels:

- Strong: explicit sponsorship for the role or strong recent comparable-level evidence.
- Moderate: licence hit plus credible international hiring, vacancy-specific sponsorship unconfirmed.
- Weak: unresolved or only weak evidence.
- Blocked: explicit no sponsorship, permanent/unrestricted rights requirement that rules the candidate out, or another clear blocker.

Blocked direct jobs are never presented as recommendations. An agency advert with no explicit blocker but an undisclosed client belongs under Agency Leads rather than being labelled Blocked simply because sponsorship is unassessable.

If a salary is near a Skilled Worker/new-entrant threshold and that threshold would decide whether the role survives, verify the current rule on an official GOV.UK source during that run. Cached workspace figures and third-party immigration pages are context only and must not make a borderline accept/drop decision.

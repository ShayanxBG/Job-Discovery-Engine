# Job Matching and Screening

How to gather the evidence a vacancy decision needs, and how to hand it to the deterministic evaluator. It governs screening and ranking; it does NOT restate the scoring model, which is machine-readable.

The project stops at discovery and decision support. It never prepares or submits an application.

## Workflow

1. Retrieve and verify the vacancy.
2. Compare it against `candidate/profile.md`, the complete factual authority. The master CV may corroborate a claim and may never narrow one: absence from the CV is never evidence of absence.
3. Apply hard blockers before scoring.
4. Score viable direct-employer roles by proposing a structured evaluation.
5. Keep agency and verification leads in separate categories where a decision-critical fact is unresolved.
6. Return a compact recommendation and stop.

## The model proposes, Python decides

Do not add the components yourself and do not state a band:

```text
python tools/match_evaluation.py schema
python tools/match_evaluation.py evaluate --file evaluation.json
```

`schema` is the AUTHORITY for component maxima, bands, uncertainty ceilings, full-marks anchors, the blocker vocabulary with each blocker's factual precondition, the never-blockers and the verification reasons. Read it instead of remembering any of those numbers.

Each component carries a `score`, a short `evidence` claim naming what the vacancy actually said, and an `uncertainty` of `known`, `partial` or `unknown`. Pass the vacancy's structured `facts` and the record's state `key` too. The helper validates against policy and computes the total, the band and eligibility. It rejects rather than repairs, because silently correcting an out-of-range score would hide a misunderstanding of the model.

Unknown information lowers confidence and raises a verification need. It never invites a guess in either direction.

## Calibration is machine-readable

Two files own the mechanics, and neither is prose to be re-interpreted per run:

- `config/matching_policy.json` (publishable) owns HOW evaluation works.
- `candidate/config.json` (private) owns the candidate's calibration: acceptable and excluded levels, the review and inclusive hard experience thresholds, excluded specialisms, employment types, whether sponsorship is required, whether a salary floor exists.

`candidate/profile.md` remains the evidence authority. The config is derived from it by `tools/candidate_config.py`, and a value the profile does not establish stays `null`. Null means unknown, not false: a null salary floor means the salary blocker can never fire, and a null clearance constraint means the clearance blocker can never fire.

`config/immigration_rules.json` owns every immigration and salary figure, published rather than calculated, with the official GOV.UK page and the date each was checked. Never quote a threshold from memory or from prose. Read it with `python tools/immigration_rules.py show`, and re-verify against the official pages when `status` reports `stale` or when a figure decides a recommendation.

## A number is not a judgement until something supports it

Validating arithmetic and ranges proves an evaluation is well FORMED, not that it is TRUE. Three deterministic rules keep a score grounded, and `schema` prints their current values:

- **Uncertainty ceilings.** A component can never exceed the ceiling for its declared uncertainty. Evidence that says only that nothing is known is treated as `unknown` however it was labelled, so an evaluation whose every component is unevidenced can never reach a qualifying band. The ceiling CAPS, it never zeroes: unknown is still not a negative fact.
- **Full-marks anchors.** The exact maximum requires the strongest anchor the policy documents for that component, which usually means structured vacancy facts.
- **Unknown stays visible.** A component that establishes nothing must raise one of its listed verification reasons, so the gap stays an action rather than disappearing into a number.

## Core scoring model

Weights and bands live in `config/matching_policy.json`. What each component MEASURES:

- **Technical stack and day-to-day responsibilities.** Weight actual duties above title keywords. Reward Python/backend alignment, Django/FastAPI/REST/PostgreSQL relevance, production engineering, integrations and testing, with close evidence from the profile.
- **Seniority and commercial-experience fit.** Whether the level is realistic: the stated requirement, whether it is a hard minimum or a preference, and whether the responsibilities describe the candidate's level rather than merely the title.
- **Skilled Worker sponsorship viability.** Strength of evidence on the ladder below. Recent sponsorship at a comparable level matters most; register presence alone is partial evidence.
- **Salary, contract and working-pattern feasibility.** Employment type, salary where stated, and working pattern. Location inside the UK never reduces this score.
- **Company environment and domain fit.** Engineering environment and domain relevance. Domain is secondary to technical and seniority fit, and a preferred domain is a bonus rather than a requirement.

Scores are decision-support values, not predictions of interview or offer probability. The bands were RECALIBRATED against 83, the ceiling a good real advert actually reaches, rather than against 100. 100 stays reachable in principle, because a fully `known` component is uncapped, but none of the 988 ranked adverts had all five components known and the best scored 79: every eligible Direct role from 54 to 65 stays visible for human review, a score alone never creates a hard blocker or a suppression record, and a role below 54 is deprioritised rather than deleted.

## Verify First is an action, not a category

`Verify First` never changes the score band and never changes `lead_type`. A Direct Match scoring 78 or 61 whose verdict says `Verify first` is still a Direct Match in its own band; the action is surfaced in the verdict and the saved shortlist line.

`Verification Lead` is a different concept: the `lead_type` for a direct-employer role that cannot honestly be scored yet because a decision-critical external gate is unresolved, such as a conflicting Skilled Worker route or an unconfirmed employing entity. It is not given a final score while the gate stands. Never reclassify a scored Direct Match into one merely because its verdict recommends verifying something.

## Hard blockers

A hard blocker overrides the numeric score and sets `eligible: false`. It does NOT zero the component scores: a role blocked today on an explicit no-sponsorship statement while scoring 86 on everything else is a completely different record from a genuinely poor 41, and the blocker may be gone next month.

`python tools/match_evaluation.py schema` lists every permitted blocker and the factual precondition each must satisfy. Three rules govern all of them:

1. **Every blocker is conditional on the private calibration.** One whose calibration field is null cannot fire, and the helper rejects a proposal that tries to apply it.
2. **Membership of the vocabulary is not permission.** A blocker is a DECIDED factual rejection, so it must quote the vacancy in `evidence.excerpt`, name where that was read in `evidence.source_url`, and say who said it in `evidence.stated_by`. A search platform's own classification is `platform`, not `employer`, and can never decide an employer fact; an inference can never support a blocker at all.
3. **Everything is checked against the CANONICAL vacancy**, not against the proposal. The quotation must appear in the stored employer description, the URL must be one the record names, and every fact a precondition consumes is read from the stored record. A proposal fact contradicting the stored one is refused by name, and where no employer text is cached the blocker fails closed.

If the facts cannot prove it, it is not a blocker. Raise a verification need instead.

Two blockers need more than a quotation. `wrong_specialism` must establish the employer's own ROLE IDENTITY, from the canonical title naming an excluded identity in the controlled alias vocabulary, or from an explicit role-identity statement in the quoted text; repeated terminology proves subject matter, not role identity, so a mention of React, data, mobile, testing or DevOps, a department name, a stakeholder team and a desirable skill are none of them the role, and a mixed or ambiguous title stays reviewable rather than being automatically rejected. `seniority` must be established by the employer's TITLE, not by a level word appearing anywhere in the advert, and a mixed `Mid to Senior` title is reviewable rather than rejected. `outside_market` needs an employer-stated ISO country code, because an unfamiliar city, remote wording, an aggregator location and a missing location can none of them establish that a role sits outside the market.

Do not hide a blocker behind a high technical score, and do not manufacture one from a generic title or missing information.

Deterministic code cannot prove that ordinary qualitative evidence is truthful unless it can compare it with canonical vacancy content. Scores stay decision-support scores rather than factual predictions; hard blockers are held to the stronger canonical standard because a blocker deletes a vacancy rather than ranking it down; and `computed_by` is a label that establishes no provenance at all.

## These are never blockers

Each is a common way to throw away a good vacancy, so each is refused at the boundary rather than left to judgement:

- **Unknown sponsorship.** An employer that has said nothing has not declined. Record the uncertainty and a `sponsorship` verification need.
- **Unstated salary.** Many UK adverts omit salary. Score `employment_conditions` from contract type and working pattern, leave salary unknown, and raise a `salary` verification need.
- **One missing desirable skill.** A small `tech_fit` deduction, never a rejection.
- **A non-preferred location inside the accepted market.** Zero penalty. Preferred cities are tie-breakers only.
- **A generic job title.** "Software Engineer" says nothing on its own. Judge the responsibilities.
- **Absence from a sponsor-register snapshot.** A miss under the names we know is not evidence that an employer cannot sponsor.

## Experience requirements: preferred is not required

A stated preference and a stated minimum are different facts:

- `3 years preferred` / `ideally 3 years` / `approximately 3 years` / `3+ desirable` is a preference. It may cost a little on seniority; it is never a blocker.
- `minimum 3 years` / `3+ years required` / `at least 3 years` is a hard minimum, and the private calibration decides what happens to it.
- A `Senior`, `Staff`, `Principal`, `Lead`, `Head` or `Architect` TITLE is a seniority signal even with no years printed. Read the responsibilities.
- A generic title with realistic junior-to-mid responsibilities is in scope. Do not reject a good role for a boring title.

## Experience-year calibration

The candidate's confirmed commercial-experience total has ONE home, `candidate/profile.md`, and reaches matching through the derived `candidate/config.json`. Read it from there; never restate it here, because a second copy is a copy that will drift. The thresholds below are calibrated against that total and are deliberately generous relative to it, because an advert's stated minimum is a filter the employer wrote, not a measurement of who can do the job.

- 5+ years hard minimum: drop. Clearly out of scope when the requirement is genuine.
- 4+ years hard minimum: drop. This is the INCLUSIVE hard threshold: a stated hard minimum of exactly four blocks, and it fires only on an explicit, mandatory, employer-stated minimum proved against canonical evidence.
- 3+ years hard minimum: retain only for human review. A realistic stretch, never a blocker. Keep it visible when the technical fit is strong and record the seniority concern.
- 0 to 2 years: strongly in scope when the duties and stack fit. Do not downgrade a role merely because its title says junior or graduate.
- no stated minimum: assess from duties and seniority signals rather than title alone.

A PREFERENCE is not a minimum at any of those numbers. `preferred`, `ideally`, `approximately`, `around`, `desirable`, `nice to have` and wishlist wording never fire the experience blocker, so `4 years preferred` is a role to score and `3 years preferred` is not even a stretch. Only an explicit mandatory minimum counts, and `tools/discovery_candidate.py experience_minimum()` is what decides which is which.

## Direct, Verification, and Agency categories

- **Direct Match.** The full 100-point model, when the employer is known and the role can be assessed without a decision-critical unresolved gate. Discovery calibration: High means no meaningful unresolved issue beyond minor uncertainty; Medium means exactly one.
- **Verification Lead.** A direct-employer role whose technical relevance is unusually strong but where a decision-critical external fact prevents an honest recommendation, for example a sponsorship route, programme permanence, employment entity or salary eligibility. Do not force a numeric score to hide the gate. State exactly what must be verified and what outcome would change the classification.
- **Agency Lead.** A recruiter advert whose technical fit is useful but whose employing client or sponsorship route is unknown. The sponsorship component is EXCLUDED rather than scored zero, the total is out of 75, and it borrows no Direct band. State the recruiter-verification questions, but do not contact anyone.

## Sponsorship evidence ladder

Register presence alone is weak evidence. Weight evidence in this order, strongest first:

1. The employer sponsored someone at roughly the candidate's level within the last 12 months.
2. The employer sponsored anyone within the last 12 months.
3. Licensed plus a structured graduate/junior sponsorship scheme or visible international hiring.
4. Licensed, large employer, no recent comparable-level evidence.
5. Licensed, small employer, no recent evidence.
6. Not found on the register.

That is a RANKING of evidence strength, not a filter, and the difference is load bearing. Positions 4, 5 and 6 are weaker evidence, not adverse evidence:

- A junior or early-career vacancy with no discoverable sponsorship history is NOT rejected for that. Most employers publish nothing about who they have sponsored, and a small or young company has no history to find. Absence of discoverable history is unknown, scored conservatively with a visible `sponsorship` verification need.
- Register presence proves the ORGANISATION holds a licence. It does not prove this vacancy will be sponsored, that the role meets the going rate or skill level, or that the licence is valid today.
- Absence from a snapshot is not proof that sponsorship is impossible: registered legal names routinely differ from trading names and the snapshot has a date.
- The current employer's inability to sponsor says nothing about any other employer.
- Licence evidence, vacancy-specific evidence, salary feasibility and new-entrant eligibility are four separate questions. Never let one answer another.

## Sponsor-register usage

Two register sources exist and they are not interchangeable. `tools/sponsor_register.py` is the OFFICIAL lookup: a validated local snapshot of the GOV.UK register of licensed sponsors (workers), with route and rating preserved. Check it first. `data/uksponsorregistertechsubset20260812.csv`, via `tools/check_sponsor.py`, is SUPPLEMENTARY: a dated 2026-08-12 tech/consultancy subset with no route column, a cheap lead signal and never the official register.

Read the official result exactly as stated: `FOUND` is licence evidence in that dated snapshot and proves nothing about this vacancy; `NOT_FOUND` means no credible match under the legal-entity names known, so never write that an employer cannot sponsor; `AMBIGUOUS` means two distinct organisations matched, so do not choose between them; `UNAVAILABLE` is not a negative result. Absence from the tech subset proves nothing at all.

Routes matter. A licence for an unrelated route is not the evidence a Skilled Worker vacancy needs, so read the routes the register actually lists rather than treating any licence as sufficient. When sponsorship affects the decision, check current company evidence and the live register before finalising the verdict.

## Employment type

`permanent`, `fixed-term`, `temporary`, `contract`, `freelance` and `contract-unspecified` are six different facts. Independent contracting is outside the configured target, and only UNAMBIGUOUS wording establishes it: a day or daily rate, an inside or outside IR35 status, `contractor`, a contracting or consultancy engagement, freelance, self-employed, sole trader, umbrella. The bare word `contract` is not that wording, and neither are `interim` and `secondment`: each is compatible with direct employment and raises an `employment_type` verification need. A DIRECTLY EMPLOYED fixed-term role is not prohibited by official sponsor guidance, so score its duration, stability, salary, direct employer identity and sponsorship evidence conservatively and keep it reviewable. Never infer direct employment from an ambiguous use of the word `contract`, and keep a temporary agency placement separate from a direct fixed-term role. Apprenticeships and internships stay out of the normal search unless the candidate asks for them.

## Fast screening output

Use this compact block unless the user explicitly asks for more detail:

```text
Match: X%
Recommendation: Exceptional Match / Strong Match / Viable Match / Borderline Review / Verify First / Skip
Sponsorship: Strong / Moderate / Weak / Blocked - [one short reason]
Main match: [one or two lines]
Main gap: [one or two lines]
Best action: [one or two lines]
```

Map the recommendation from the band the evaluator returned, with `Verify First` where material uncertainty remains and `Skip` for a hard blocker regardless of score. A Borderline Review role is shown, with what would move it.

## Full screening

Same categories and model, but verify more deeply before scoring: employer identity, live vacancy status, sponsor evidence, salary and immigration thresholds where decision-sensitive, actual must-haves versus bonuses, seniority signals, and any right-to-work wording. Keep the result concise and do not expose a point-by-point scoring table unless asked.

## Writing rules

British English, no em dashes, strict evidence-based scoring, no softened verdicts. Keep below-threshold roles short.

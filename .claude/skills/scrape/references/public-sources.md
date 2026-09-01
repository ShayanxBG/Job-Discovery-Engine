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

- LinkedIn Jobs: authenticated if available, at least 12 title/query families in normal deep mode.
- Indeed UK: authenticated if available, at least 8 title/query families in normal deep mode.
- Employer/ATS direct: at least 8 targeted searches/resolution attempts across employer careers, Greenhouse, Lever, Ashby, Workable, SmartRecruiters and Workday.
- General UK boards: at least 6 focused searches AND at least 4 distinct board/platform families attempted across CWJobs/Totaljobs, Reed, DWP Find a Job, Built In, Welcome to the Jungle, JobServe, Technojobs or Adzuna where useful. CWJobs and Totaljobs share a platform and count as one diversity family even when both are searched. No single board may satisfy the diversity floor by repeating queries. Browser-search CWJobs/Totaljobs when available.
- Sponsorship-focused sources: normally 8-12 focused searches across at least 4 current sources in `search-queries.md`. Once direct-employer/ATS and general-board coverage is incomplete, do not spend dozens of extra searches on sponsor boards. Additional sponsor-board searching beyond roughly 15 queries is justified only when those sources are still producing unique plausible leads and the primary source families are already covered.

Source targets are floors, not ceilings. If a source is still producing unique plausible vacancies, continue. If a source is blocked or unproductive, record that and spend effort elsewhere. Source diversity outranks merely hitting a raw query count.

### `/scrape gapfill` recovery mode

Use this after a completed scrape explicitly reported meaningful source gaps. Preserve `seen_jobs.json`; never reset it. Default to the last 14 days unless the user supplies another window.

Target FAMILY GAPS first, meaning families where no source completed, not every degraded sibling source. A family another site already covered does not need a gap-fill pass, though a persistently broken sibling such as Totaljobs is still worth repairing on its own account. After the gaps, prioritise under-covered families: DWP Find a Job, Built In, Welcome to the Jungle, JobServe, Technojobs and Adzuna, then resolve promising hits to employer/ATS pages. Do not repeat the full LinkedIn/Indeed sweep unless those authenticated sources were themselves incomplete.

A gap-fill run may discover new roles or materially upgrade existing ones. Ordinary duplicates stay hidden. Report exactly which prior gaps were attempted and which remain unresolved.

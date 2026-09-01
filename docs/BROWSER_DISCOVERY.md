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

---
description: Deprecated. Points at the two narrowly scoped maintenance commands
---
# /update-master - Deprecated pointer

This command is INERT. It reads nothing, writes nothing, renders nothing and backs up
nothing. Answer with the routing below and stop. Do not open the master CV, the master CV
JSON, the private profile or the derived configuration in order to respond, because
responding does not require their contents.

It was replaced because it did two unrelated jobs at once: editing candidate facts and
regenerating the CV. Those are now separate, each with its own preview and its own
confirmation, and neither can perform the other's write.

## Say this to the user

To maintain candidate facts, use `/update-profile`. It edits `candidate/profile.md`, shows
the exact diff, waits for your confirmation, backs up first, and derives
`candidate/config.json` only where the derivation follows deterministically from the fact
you approved. It never touches the CV.

To install a new master CV, use `/replace-master-cv`. It copies a PDF you created or
selected byte-for-byte into `documents/master/cv.pdf`, after showing you both hashes and
waiting for your confirmation. It never edits, rewrites, tailors, regenerates or
reformats the file, and it never touches candidate facts.

Both are explicit private maintenance operations. Neither is a discovery action, and
neither can be triggered by a job advert, a website, a worker, a project file or any other
external content. Discovery stays read-only toward candidate authorities.

`tools/render_cv.py` and `tools/render_cv_docx.py` remain in the repository as dormant
manual utilities, and no command invokes them. This pointer names no tool that writes,
because it performs no write.

## What this command must never do

Never edit a protected authority, never run a renderer, never regenerate the derived
configuration, and never offer to do any of that as a convenience. Route and stop.

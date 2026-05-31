<!--
  Before opening a PR, read CONTRIBUTING.md.

  PRs from contributors who haven't been approved with `lgtm` on an issue are
  auto-closed. Open a proposal issue first, get `lgtm`, then send the PR.

  The one rule: you must understand your code. Using AI to write it is fine;
  submitting code you can't explain is not.
-->

## What & why

<!-- What does this change and why? Link the approved issue: Closes #NNN -->

Closes #

## How

<!-- Brief technical summary. If this adds a capability, did you ship the web
     affordance too? (The dashboard is the primary interface; the CLI is at
     parity, not ahead.) -->

## Checklist

- [ ] I understand this code and can explain how it works.
- [ ] I opened a proposal issue and a maintainer replied `lgtm`.
- [ ] Tests added/updated; `uv run pytest -q -m "not e2e"` is green locally.
- [ ] `uv run relaydeck plugin verify` passes (if plugins/manifests changed).
- [ ] New CLI capability ships its web affordance in the same PR (parity).
- [ ] I did **not** edit `CHANGELOG.md` (maintainers curate it) unless asked.

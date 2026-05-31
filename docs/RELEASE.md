# Releasing relaydeck

relaydeck publishes to **PyPI** as the single `relaydeck` distribution (engine +
bundled `plugins/`). This doc covers the release flow and the recommended
GitHub repo hardening.

## TL;DR flow

1. **Bump the version.** Run the `version-bump` workflow (Actions → version-bump
   → Run, pick `patch`/`minor`/`major`), or locally:
   ```sh
   uv run python scripts/bump_version.py minor   # edits pyproject + __init__ + CHANGELOG
   ```
   Either way it updates `pyproject.toml`, the `_resolve_version()` fallback in
   `relaydeck/__init__.py`, and rolls `CHANGELOG.md`'s `[Unreleased]` into a
   dated section. The workflow opens a PR; review and **rebase-merge** it.
2. **Cut a GitHub Release.** Create a release with tag `vX.Y.Z` (matching the
   new `pyproject` version) and paste the changelog notes.
3. **PyPI publish is automatic.** `release.yml` triggers on the published
   release: it verifies the tag matches the package version, builds the sdist +
   wheel, `twine check`s them, and publishes to PyPI via **Trusted Publishing**
   (OIDC — no token stored in the repo).

   You can rehearse the build without uploading via Actions → release →
   Run workflow (the `dry_run` dispatch builds + `twine check`s only).

Because the PyPI upload is triggered *by* the GitHub Release, the PyPI version
and the latest GitHub Release tag are always in lockstep — which is what the
in-app update check (`relaydeck/version_check.py`, the dashboard banner, and
`relaydeck update`) relies on to tell users a new version exists.

## Upgrade path (post-PyPI)

- `scripts/install.sh` defaults to installing the bare PyPI name
  (`uv tool install relaydeck`), falling back to the GitHub main branch if PyPI
  isn't reachable yet (e.g. before the first publish). Override with
  `RELAYDECK_SOURCE` for a pinned ref or a local checkout.
- `relaydeck update` and the dashboard's "Update now" banner run
  `uv tool upgrade relaydeck` (override with `RELAYDECK_UPDATE_CMD`), which moves
  a PyPI install to the latest published version.
- Anyone still on an old `git+github` install can migrate with
  `uv tool install --reinstall relaydeck` (re-pins the source to PyPI), or just
  re-run `install.sh`.

## Recommended GitHub repo hardening

These are settings, not code — apply them in the repo's Settings after the repo
is (re)created.

**Branch protection / rulesets for `main`:**
- Require a pull request before merging (≥1 approval); dismiss stale approvals
  on new commits.
- Require status checks to pass: the `ci` jobs (`pytest + plugin verify`,
  `ruff`, `build`). Require branches to be up to date.
- **Require linear history**, and in *General → Pull Requests* allow **only
  "Rebase merging"** (disable merge commits and squash). Rebase-only is a good
  fit here: it keeps history linear, plays well with the version-bump PR flow,
  and is exactly what `version_check` / the changelog assume. 👍
- Block force-pushes and branch deletion. Require conversation resolution.
- Optional: require signed commits.

**Actions:**
- Settings → Actions → General → Workflow permissions: **Read repository
  contents** by default; the workflows that need more request it explicitly
  (`approve-contributor` and `version-bump` ask for `contents: write`;
  `release`'s publish job asks for `id-token: write`).
- Protect the `pypi` environment (optional required reviewer) so a publish can't
  happen unattended.

**Supply chain & secrets:**
- Enable **secret scanning** + **push protection**, **Dependabot alerts**, and
  **Dependabot security updates**. `.github/dependabot.yml` already opens weekly
  PRs for GitHub Actions and Python deps.
- Consider pinning third-party actions to commit SHAs (this repo pins to major
  tags for readability; Dependabot keeps them current).

**Contribution gate:** issues/PRs from non-collaborators are auto-closed until a
maintainer replies `lgtm`/`lgtmi` (see `.github/workflows/*-gate.yml` and
`CONTRIBUTING.md`). Collaborators are always exempt.

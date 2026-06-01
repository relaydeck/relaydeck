# Contributor Gate GitHub App

The **Approve Contributor** workflow (`.github/workflows/approve-contributor.yml`)
pushes to `main` when a maintainer replies `lgtm` / `lgtmi` on an issue. Branch
protection requires a dedicated GitHub App with a ruleset bypass — not
`GITHUB_TOKEN` / `github-actions[bot]`.

## 1. Create the app (org)

Open (pre-filled):

<https://github.com/organizations/relaydeck/settings/apps/new?name=relaydeck+Contributor+Gate&url=https%3A%2F%2Fgithub.com%2Frelaydeck%2Frelaydeck&description=Updates+.github%2FAPPROVED_CONTRIBUTORS+when+maintainers+reply+lgtm%2Flgtmi.&public=0&webhook_active=0&contents=write&issues=write&metadata=read>

Confirm:

| Setting | Value |
|---|---|
| Homepage URL | `https://github.com/relaydeck/relaydeck` |
| Webhook | **Inactive** (workflow triggers on `issue_comment`, not app webhooks) |
| Repository permissions | **Contents** Read and write, **Issues** Read and write, **Metadata** Read |
| Where | **Only on this account** → select **`relaydeck/relaydeck`** only |

Click **Create GitHub App**.

## 2. Generate a private key

On the app settings page: **Private keys** → **Generate a private key**. Save
the downloaded `.pem` file (you cannot download it again).

Note the **Client ID** on the same page (not the App ID).

## 3. Install on `relaydeck/relaydeck`

**Install App** → **Only select repositories** → `relaydeck` → **Install**.

## 4. Repository secrets and variables

From a checkout with `gh` authenticated as an org admin:

```sh
# Client ID from the app settings page
gh variable set CONTRIBUTOR_GATE_APP_CLIENT_ID --repo relaydeck/relaydeck --body "YOUR_CLIENT_ID"

# Paste the full PEM file contents
gh secret set CONTRIBUTOR_GATE_APP_PRIVATE_KEY --repo relaydeck/relaydeck < path/to/private-key.pem
```

## 5. Ruleset bypass on `main`

**Settings → Rules → main-protect → Bypass list → Add bypass → GitHub App** →
select **relaydeck Contributor Gate**.

Without this step the workflow commit succeeds locally but `git push` is
rejected (`Changes must be made through a pull request`).

## 6. Verify

Comment `lgtm` or `lgtmi` on a test issue (as a maintainer). The workflow should:

1. Commit `chore: approve contributor …` as `relaydeck-contributor-gate[bot]`
   (slug may vary slightly)
2. Push to `main`
3. Post the approval confirmation comment on the issue

## Rotating the private key

Generate a new key on the app settings page, update
`CONTRIBUTOR_GATE_APP_PRIVATE_KEY`, then revoke the old key.

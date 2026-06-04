# Orchestration recipes

Composable patterns. All assume you've bootstrapped (`scripts/relaydeck-bootstrap.sh`)
and registered a workspace. Replace ids/types/purposes to taste.

## 1. Parallel fan-out (N specialists, one task)

Many agents, same brief, independent work — then collect.

```sh
relaydeck workspace add . --name proj --plugin messaging --plugin skills
for spec in security:"security audit" perf:"performance audit" \
            a11y:"accessibility audit"; do
  id=${spec%%:*}
  relaydeck agent create "$id" --type claude-code --workspace proj \
      --purpose "${spec#*:}" --autonomy auto
done
relaydeck agent start security perf a11y
relaydeck workspace message 'Audit your specialty. When done, hand back your
findings with: relaydeck agent result put "$RELAYDECK_AGENT_ID" --summary "<one line>" --body @findings.md'
for id in security perf a11y; do
  relaydeck agent wait "$id" --status complete-unread --timeout 900 \
    || relaydeck agent screen "$id"
  # Durable hand-back — survives an agent crash, unlike scrollback.
  relaydeck agent result get "$id"
done
relaydeck agent rm security perf a11y
```

## 2. Pipeline (implementer → reviewer → fixer)

Sequential hand-off; each stage waits for the last.

```sh
relaydeck agent create impl --type codex-cli --workspace proj --purpose "implement"
relaydeck agent create review --type claude-code --workspace proj --purpose "review"
relaydeck agent start impl review

relaydeck agent send impl 'Implement the change in ISSUE.md. Reply DONE when committed.'
relaydeck agent wait impl --status complete-unread --timeout 1200

relaydeck agent send review 'Review impl's commit. Reply APPROVE or list blocking issues.'
relaydeck agent wait review --status complete-unread --timeout 600
relaydeck agent screen review            # read the verdict
# branch: if issues, loop back to impl with the review body, else ship.
```

## 3. Supervisor loop (long-running fleet you babysit)

Keep a fleet alive, clear benign stalls, surface real ones. Pair with
`autopilot` (Layer 2) so most stalls clear themselves.

```sh
relaydeck plugin set autopilot mode benign
while :; do
  relaydeck agent list -A
  # React to held prompts the human needs to see:
  relaydeck events tail --type autopilot.held --agent "" 2>/dev/null || true
  sleep 15
done
```

(Run the loop in your own session; agents keep running in the daemon
regardless. Ctrl-C the loop without killing the fleet.)

## 4. Reviewer-of-reviewers (quorum)

Spawn three reviewers, broadcast the artifact, accept only if ≥2 approve.
Use `relaydeck reply`/`workspace inbox` to collect structured verdicts, then
decide in your own logic. Cap at three — more reviewers rarely change a 2/3
quorum and just burn tokens.

## 5. Cross-workspace orchestration (fan-out over repos)

Register several repos as workspaces and run the same job in each.

```sh
for d in ~/src/api ~/src/web ~/src/worker; do
  name=$(basename "$d")
  relaydeck workspace add "$d" --name "$name" --plugin messaging
  relaydeck agent create "bump-$name" --type codex-cli --workspace "$name" \
      --purpose "bump deps + run tests" --autonomy auto
  relaydeck agent start "bump-$name"
done
relaydeck agent list -A          # watch all of them across workspaces
```

## Patterns to avoid

- **Doing the work yourself "to save a round-trip."** If you're editing
  files, you've stopped orchestrating. Delegate it.
- **Polling an inbox in a tight loop to "wait" for a peer.** Use
  `relaydeck agent wait` (status) or just let the reply arrive — peer
  messages are pushed into sessions, not pulled.
- **Spawning a worker that also runs this bootstrap.** That's a
  fleet-of-fleets. A spawned worker is already inside relaydeck
  (`RELAYDECK_AGENT_ID` is set) and the skill's §0 guard stops it — keep it
  that way; don't pass orchestration intent down to workers unless that's
  literally the task.
- **Forgetting teardown.** `relaydeck agent rm` the one-offs.

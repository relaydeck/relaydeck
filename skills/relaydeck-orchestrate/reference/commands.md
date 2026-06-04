# relaydeck command cheat-sheet (orchestration subset)

`relaydeck --help` / `relaydeck <cmd> --help` are authoritative. This lists
the commands an orchestrator actually reaches for. `rdk` is an alias for
`relaydeck`.

## Bootstrap & health
| Command | What |
| --- | --- |
| `relaydeck --version` | Confirm it's installed |
| `relaydeck daemon start` / `stop` / `restart` / `status` | Daemon lifecycle |
| `relaydeck daemon logs` | Tail the daemon log |
| `relaydeck status` | Who you are, peers, unread inbox, **dashboard URL** |
| `relaydeck doctor [--fix]` | Self-diagnostic (config, auth, DB, plugins) |

## Workspaces
| Command | What |
| --- | --- |
| `relaydeck workspace add <path> --name <n> [--plugin messaging --plugin skills]` | Register a working dir |
| `relaydeck workspace list` | Registered workspaces + health |
| `relaydeck workspace info [name]` | Plugins + agents in a workspace |
| `relaydeck workspace rm <name>` | Unregister (leaves the directory) |

## Agents (workers)
| Command | What |
| --- | --- |
| `relaydeck agent create <id> --type <harness> --workspace <ws> --purpose "<role>" [--autonomy auto\|bypass\|locked] [--tag x]` | Define a worker |
| `relaydeck agent start <id> [<id>…]` | Bring up now |
| `relaydeck agent stop / restart / rm <id>` | Lifecycle |
| `relaydeck agent list [-w <ws>] [-A]` | Workers + live semantic status |
| `relaydeck agent find --purpose <substr>` / `--tag <t>` | Discover peers |
| `relaydeck agent screen <id>` | Render a worker's current screen |
| `relaydeck agent events <id> [-f] [--type <substr>]` | One worker's event log |
| `relaydeck agent wait <id> --status <s> [--timeout N]` | **Block until done** |
| `relaydeck agent send <id> '<body>'` | Push a message into a worker |
| `relaydeck agent unblock <id> [--answer T \| --enter \| --key K]` | Answer a stuck prompt |

Harness `--type`: `claude-code`, `codex-cli`, `cursor-cli`, `opencode-cli`,
`gemini`, `pi`, `relaydeck`.

## Messaging & events
| Command | What |
| --- | --- |
| `relaydeck workspace message '<body>' [--agent <id>]` | Broadcast into inboxes (or one agent) |
| `relaydeck workspace inbox [-f] [--full] [--agent <id>]` | Read messages passing through |
| `relaydeck reply <msg-id> '<body>'` | Threaded reply to a `[relay …]` line |
| `relaydeck broadcast '<msg>' [--data k=v]` | Ambient event on the stream (not inbox) |
| `relaydeck events emit <type> [--data k=v] [-m msg]` | Emit a custom event |
| `relaydeck events tail -f [--type <substr>] [--agent <id>]` | The fleet firehose |

**Inbox vs event:** `workspace message` / `agent send` *deliver text into an
agent's session*. `broadcast` / `events emit` put an *ambient event on the
stream* the dashboard, `view` TUI, and `events tail` watch — nobody's inbox
is touched. Use messages to task agents; use broadcasts to announce
milestones an observer should see.

## Autopilot (auto-unblock policy)
| Command | What |
| --- | --- |
| `relaydeck plugin set autopilot mode <off\|benign\|all-known>` | How aggressive |
| `relaydeck plugin set autopilot auto_accept_terms true` | Opt into terms/license auto-accept |
| `relaydeck autopilot status` | Mode + active episodes |
| `relaydeck autopilot rules` | The prompt-shape allowlist |
| `relaydeck autopilot test "<screen text>" [--mode …] [--terms]` | Dry-run the matcher |

## Observe everything at once
- `relaydeck view` — built-in multi-pane TUI (sidebar of agents + live PTY +
  messages + events).
- The **web dashboard** (URL from `relaydeck status`) — real-time fleet view.

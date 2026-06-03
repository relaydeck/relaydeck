"""Migration upgrade checks — snapshot a real DB/daemon, then assert the
upgrade preserved everything and applied the new schema.

No mocks: `snapshot` reads the live SQLite DB and the running daemon's HTTP
API; `verify` re-reads both after the new version migrated in place and fails
loudly on data loss, a stale schema, or a missing post-upgrade feature.

Usage:
  python _migcheck.py snapshot --db <db> --base-url <url> --out before.json
  python _migcheck.py verify   --db <db> --base-url <url> --before before.json \
                               --expect-schema 18
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request


def _schema_version(db: str) -> int:
    c = sqlite3.connect(db)
    try:
        return int(c.execute("PRAGMA user_version").fetchone()[0])
    finally:
        c.close()


def _tables(db: str) -> list[str]:
    c = sqlite3.connect(db)
    try:
        return sorted(
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )
    finally:
        c.close()


def _counts(db: str) -> dict[str, int]:
    c = sqlite3.connect(db)
    out: dict[str, int] = {}
    try:
        for t in _tables(db):
            out[t] = int(c.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0])
    finally:
        c.close()
    return out


def _agent_ids(db: str) -> list[str]:
    c = sqlite3.connect(db)
    try:
        return sorted(r[0] for r in c.execute("SELECT id FROM agents"))
    finally:
        c.close()


def _token(base_url: str) -> str:
    try:
        with urllib.request.urlopen(f"{base_url}/api/auth/bootstrap", timeout=10) as r:
            return json.loads(r.read()).get("token", "")
    except Exception:
        return ""


def _api(base_url: str, path: str, token: str):
    req = urllib.request.Request(f"{base_url}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read())


def snapshot(db: str, base_url: str) -> dict:
    tok = _token(base_url)
    status, agents = _api(base_url, "/api/agents", tok)
    return {
        "schema": _schema_version(db),
        "counts": _counts(db),
        "agent_ids": _agent_ids(db),
        "api_status": status,
        "api_agents": [a.get("id") for a in agents],
    }


def verify(db: str, base_url: str, before: dict, expect_schema: int) -> int:
    fails: list[str] = []
    after = snapshot(db, base_url)

    # 1. Schema ended at the new version and never went backwards. We do NOT
    #    require a strict increase: when the previous published release is
    #    already at the current schema (a release with no migration), the
    #    schema correctly stays the same — that's a valid upgrade, not a
    #    failure. `!= expect_schema` already catches "migration didn't run."
    if after["schema"] != expect_schema:
        fails.append(f"schema {after['schema']} != expected {expect_schema} "
                     f"(was {before['schema']} before upgrade)")
    if after["schema"] < before["schema"]:
        fails.append(f"schema regressed ({before['schema']} -> {after['schema']})")

    # 2. No row loss in any pre-existing table (migrations are additive).
    for t, n_before in before["counts"].items():
        if t not in after["counts"]:
            fails.append(f"table dropped by migration: {t}")
        elif after["counts"][t] < n_before:
            fails.append(f"row loss in {t}: {n_before} -> {after['counts'][t]}")

    # 3. Every seeded agent survives the upgrade.
    missing = set(before["agent_ids"]) - set(after["agent_ids"])
    if missing:
        fails.append(f"agents lost across upgrade: {sorted(missing)}")

    # 4. The daemon is healthy on the new version and serves the agents.
    if after["api_status"] != 200:
        fails.append(f"daemon API unhealthy after upgrade: HTTP {after['api_status']}")
    missing_api = set(before["agent_ids"]) - set(after["api_agents"])
    if missing_api:
        fails.append(f"agents missing from /api/agents after upgrade: {sorted(missing_api)}")

    # 5. A post-upgrade feature is actually present (proves NEW code is live
    #    and the new column is queryable, not just that the daemon booted).
    tok = _token(base_url)
    _, agents = _api(base_url, "/api/agents", tok)
    if agents and "restart_pending" not in agents[0]:
        fails.append("new field 'restart_pending' absent from /api/agents "
                     "(new version not actually serving, or migration skipped)")

    print(json.dumps({"before": before, "after": after, "failures": fails}, indent=2))
    if fails:
        print("\nMIGRATION CHECK FAILED:", file=sys.stderr)
        for f in fails:
            print("  ✗", f, file=sys.stderr)
        return 1
    print(f"\n✓ migration OK: schema {before['schema']} -> {after['schema']}, "
          f"{len(after['agent_ids'])} agents preserved, "
          f"{len(after['counts'])} tables intact, restart_pending live")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--db", required=True)
    s.add_argument("--base-url", required=True)
    s.add_argument("--out", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--db", required=True)
    v.add_argument("--base-url", required=True)
    v.add_argument("--before", required=True)
    v.add_argument("--expect-schema", type=int, required=True)
    a = ap.parse_args()

    if a.cmd == "snapshot":
        snap = snapshot(a.db, a.base_url)
        with open(a.out, "w") as f:
            json.dump(snap, f, indent=2)
        print(f"snapshot: schema={snap['schema']} agents={snap['agent_ids']} "
              f"tables={len(snap['counts'])}")
        return 0
    if a.cmd == "verify":
        with open(a.before) as f:
            before = json.load(f)
        return verify(a.db, a.base_url, before, a.expect_schema)
    return 2


if __name__ == "__main__":
    sys.exit(main())

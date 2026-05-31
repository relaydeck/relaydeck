"""
atomic_write_text — correctness + the concurrency property that fixes
the `....tmp -> ....json` FileNotFoundError that hit handover.save_state
and github.save_cursor (both used a fixed `.tmp` name).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaydeck.atomicio import atomic_write_text


def test_creates_parent_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "c.json"
    atomic_write_text(p, "hello")
    assert p.read_text() == "hello"


def test_overwrites_in_place(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_text(p, "1")
    atomic_write_text(p, "2")
    assert p.read_text() == "2"


def test_leaves_no_temp_files(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_text(p, "data")
    assert [f.name for f in tmp_path.iterdir()] == ["x.json"]


def test_concurrent_writers_do_not_collide(tmp_path):
    # The original bug: a shared `<name>.tmp` meant two writers raced on
    # replace — one renamed the tmp away, the other got FileNotFoundError.
    # Unique per-call temp names must let many threads pound the same path
    # with zero errors and no leftover temp files.
    p = tmp_path / "shared.json"
    errors: list[Exception] = []

    def writer(i: int) -> None:
        for _ in range(50):
            try:
                atomic_write_text(p, f"writer-{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writes raised: {errors[:3]}"
    assert p.read_text().startswith("writer-")
    assert [f for f in tmp_path.iterdir() if f.suffix == ".tmp"] == []

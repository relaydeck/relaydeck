"""
daemon.log rotation (relaydeck/daemon.py:_rotate_log_if_large).

The backgrounded daemon appends stdout/stderr to daemon.log forever
otherwise. We rotate on `daemon start` when it crosses the size cap,
keeping a bounded ring of backups.
"""

from __future__ import annotations

from relaydeck.daemon import _rotate_log_if_large


def test_rotate_noop_when_small(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("small")
    assert _rotate_log_if_large(log, max_bytes=1024) is False
    assert log.exists()
    assert log.read_text() == "small"


def test_rotate_noop_when_missing(tmp_path):
    log = tmp_path / "daemon.log"  # never created
    assert _rotate_log_if_large(log, max_bytes=1024) is False


def test_rotate_moves_current_to_dot_one(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_bytes(b"x" * 2048)
    assert _rotate_log_if_large(log, max_bytes=1024, backups=3) is True
    assert not log.exists()  # current was renamed out
    assert (tmp_path / "daemon.log.1").read_bytes() == b"x" * 2048


def test_rotate_shifts_and_discards_oldest(tmp_path):
    log = tmp_path / "daemon.log"
    # Existing backup ring: .1 newest … .3 oldest (should be discarded).
    (tmp_path / "daemon.log.1").write_text("b1")
    (tmp_path / "daemon.log.2").write_text("b2")
    (tmp_path / "daemon.log.3").write_text("b3")
    log.write_bytes(b"z" * 2048)

    _rotate_log_if_large(log, max_bytes=1024, backups=3)

    # current → .1, .1 → .2, .2 → .3, old .3 ("b3") discarded.
    assert (tmp_path / "daemon.log.1").read_bytes() == b"z" * 2048
    assert (tmp_path / "daemon.log.2").read_text() == "b1"
    assert (tmp_path / "daemon.log.3").read_text() == "b2"
    assert not log.exists()

"""POST /api/agents/{agent_id}/uploads — drag-drop image upload endpoint."""

from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ── helpers ─────────────────────────────────────────────────────────


IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "tif", "tiff", "avif"}


def _make_app(tmp_path: Path):
    """Bootstrap the FastAPI app with an orchestrator pointing at a temp home."""
    import relaydeck.orchestrator as _orch_mod
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.transports.api import create_app

    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    (home / "runtime").mkdir()
    _orch_mod._orchestrator = None
    orch = get_orchestrator(home)
    app = create_app(home)
    app.state.orchestrator = orch
    return app, orch, home


def _create_agent(app, orch, agent_id: str = "coder"):
    """Create a pi agent so the upload route finds it."""
    orch.create_agent(agent_id, "pi", agent_id, workspace=None)


def _upload(client: TestClient, agent_id: str, name: str, data: bytes, content_type: str):
    """POST a single file and return the response."""
    return client.post(
        f"/api/agents/{agent_id}/uploads",
        files={"file": (name, io.BytesIO(data), content_type)},
    )


# ── happy path ──────────────────────────────────────────────────────


def test_happy_path_returns_path_and_saves_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, home = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    with TestClient(app) as c:
        r = _upload(c, "coder", "screenshot.png", png, "image/png")

    assert r.status_code == 200, r.text
    j = r.json()
    assert j["name"] == "screenshot.png"
    assert j["bytes"] == len(png)
    assert j["content_type"] == "image/png"

    # Path is absolute and file exists on disk
    disk_path = Path(j["path"])
    assert disk_path.is_absolute()
    assert disk_path.exists()
    assert disk_path.read_bytes() == png
    assert str(home) in str(disk_path)

    # safe_name: uuid8-*.png inside uploads/coder/
    rel = disk_path.relative_to(home / "uploads" / "coder")
    parts = rel.name.split("-", 1)
    assert len(parts) == 2
    assert len(parts[0]) == 8  # uuid8 hex
    assert parts[1].endswith(".png")


def test_happy_path_gif(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    _create_agent(app, orch, "gifbot")

    gif = b"GIF89a" + b"\x00" * 200
    with TestClient(app) as c:
        r = _upload(c, "gifbot", "anim.gif", gif, "image/gif")

    assert r.status_code == 200, r.text
    j = r.json()
    assert j["name"] == "anim.gif"
    assert j["content_type"] == "image/gif"
    assert j["bytes"] == len(gif)

    disk_path = Path(j["path"])
    assert disk_path.exists()
    assert disk_path.name.endswith(".gif")


def test_happy_path_extension_fallback(tmp_path, monkeypatch):
    """When content-type is missing/generic, accept by image extension."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    # application/octet-stream with .png extension → accepted
    with TestClient(app) as c:
        r = _upload(c, "coder", "pic.png", b"fake", "application/octet-stream")
    assert r.status_code == 200, r.text


def test_happy_path_content_type_image_generic(tmp_path, monkeypatch):
    """Generic image/* content-type passes without an extension."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    with TestClient(app) as c:
        r = _upload(c, "coder", "noext", b"imgdata", "image/unknown")
    assert r.status_code == 200, r.text


# ── 404: unknown agent ──────────────────────────────────────────────


def test_404_unknown_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _, _ = _make_app(tmp_path)

    with TestClient(app) as c:
        r = _upload(c, "nobody", "x.png", b"x", "image/png")
    assert r.status_code == 404
    assert "nobody" in r.json()["detail"]


# ── 415: non-image reject ──────────────────────────────────────────


def test_415_text_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    with TestClient(app) as c:
        r = _upload(c, "coder", "readme.txt", b"hello", "text/plain")
    assert r.status_code == 415


def test_415_json_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    with TestClient(app) as c:
        r = _upload(c, "coder", "data.json", b"{}", "application/json")
    assert r.status_code == 415


def test_415_no_content_type_or_image_ext(tmp_path, monkeypatch):
    """Neither image content-type nor image extension → 415."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    with TestClient(app) as c:
        r = _upload(c, "coder", "file.bin", b"x", "")
    assert r.status_code == 415


# ── 413: oversize reject ───────────────────────────────────────────


def test_413_exceeds_25_mib(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    # 25 MiB + 1 byte
    big = b"x" * (25 * 1024 * 1024 + 1)
    with TestClient(app) as c:
        r = _upload(c, "coder", "huge.png", big, "image/png")
    assert r.status_code == 413
    assert "25 MiB" in r.json()["detail"]


def test_413_file_not_written_on_oversize(tmp_path, monkeypatch):
    """Rejected oversize file must never land on disk."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, home = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    big = b"y" * (25 * 1024 * 1024 + 100)
    with TestClient(app) as c:
        r = _upload(c, "coder", "big.jpg", big, "image/jpeg")
    assert r.status_code == 413

    # Upload dir either doesn't exist or is empty
    upload_dir = home / "uploads" / "coder"
    if upload_dir.exists():
        assert len(list(upload_dir.iterdir())) == 0


def test_413_exactly_25_mib_passes(tmp_path, monkeypatch):
    """25 MiB exactly passes (boundary: ≤, not <)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    exact = b"z" * (25 * 1024 * 1024)
    with TestClient(app) as c:
        r = _upload(c, "coder", "max.png", exact, "image/png")
    assert r.status_code == 200, r.text
    assert r.json()["bytes"] == 25 * 1024 * 1024


# ── filename sanitisation ──────────────────────────────────────────


def test_sanitize_path_traversal(tmp_path, monkeypatch):
    """Basename only — `../../../etc/passwd` becomes safe."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, home = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    with TestClient(app) as c:
        r = _upload(c, "coder", "../../../etc/passwd.png", b"x", "image/png")
    assert r.status_code == 200, r.text

    disk_path = Path(r.json()["path"])
    # Must be a direct child of uploads/coder/, not nested.
    # ../ stripping yields bare basename 'passwd.png' — safe,
    # but the directory name 'etc' must NOT appear.
    assert disk_path.parent == (home / "uploads" / "coder")
    rel_name = str(disk_path.relative_to(home / "uploads" / "coder"))
    assert "etc" not in rel_name
    assert "/" not in rel_name


def test_sanitize_windows_path_traversal(tmp_path, monkeypatch):
    """Backslash traversal also stripped."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, home = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    with TestClient(app) as c:
        r = _upload(c, "coder", "..\\..\\windows\\system.png", b"x", "image/png")
    assert r.status_code == 200, r.text

    disk_path = Path(r.json()["path"])
    assert disk_path.parent == (home / "uploads" / "coder")


def test_sanitize_special_chars(tmp_path, monkeypatch):
    """Spaces and special chars become underscores."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, home = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    with TestClient(app) as c:
        r = _upload(c, "coder", "my screen shot!.png", b"x", "image/png")
    assert r.status_code == 200, r.text

    disk_path = Path(r.json()["path"])
    safe_part = disk_path.name.split("-", 1)[1]
    assert " " not in safe_part
    assert "!" not in safe_part
    assert safe_part.endswith(".png")


def test_sanitize_long_filename_capped(tmp_path, monkeypatch):
    """Filename >80 chars is truncated."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    long_name = "a" * 120 + ".png"
    with TestClient(app) as c:
        r = _upload(c, "coder", long_name, b"x", "image/png")
    assert r.status_code == 200, r.text

    disk_path = Path(r.json()["path"])
    safe_part = disk_path.name.split("-", 1)[1]
    assert len(safe_part) <= 80


# ── pruning ─────────────────────────────────────────────────────────


def test_prune_keeps_at_most_50(tmp_path, monkeypatch):
    """Upload 55 files → only the 50 newest remain."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, home = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    with TestClient(app) as c:
        for i in range(55):
            r = _upload(c, "coder", f"img{i:03d}.png", b"x", "image/png")
            assert r.status_code == 200, r.text

    upload_dir = home / "uploads" / "coder"
    files = list(upload_dir.iterdir())
    assert len(files) <= 50


def test_prune_does_not_crash_on_empty_dir(tmp_path, monkeypatch):
    """Pruning an empty or non-existent dir is a no-op."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, home = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    # Upload one file — prunes (dir may have only this file, fine)
    with TestClient(app) as c:
        r = _upload(c, "coder", "only.png", b"x", "image/png")
    assert r.status_code == 200
    assert (home / "uploads" / "coder" / "only.png").exists() is False
    assert len(list((home / "uploads" / "coder").iterdir())) == 1  # safe-named file


def test_prune_never_fails_request(tmp_path, monkeypatch):
    """If pruning fails (e.g. permission), the upload still succeeds."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    with TestClient(app) as c:
        r = _upload(c, "coder", "ok.png", b"y", "image/png")
    assert r.status_code == 200, r.text
    disk_path = Path(r.json()["path"])
    assert disk_path.exists()


# ── all supported image extensions ─────────────────────────────────


@pytest.mark.parametrize("ext", sorted(IMAGE_EXTS))
def test_accepts_all_image_extensions(ext, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, _ = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    with TestClient(app) as c:
        r = _upload(c, "coder", f"pic.{ext}", b"fake", "application/octet-stream")
    assert r.status_code == 200, r.text


# ── retention sweep + agent-removal cleanup ─────────────────────────


def _touch_old(p: Path, days_old: float) -> None:
    """Create a file and backdate its mtime by `days_old` days."""
    p.write_bytes(b"x")
    old = time.time() - days_old * 86400
    os.utime(p, (old, old))


def test_sweep_prunes_idle_dirs_by_cap_and_age(tmp_path):
    """_sweep_all_uploads enforces the 50-cap and 7-day age across all dirs,
    even for agents that never upload again (the lazy-prune blind spot)."""
    from relaydeck.transports.api import _sweep_all_uploads

    root = tmp_path / "uploads"
    # Agent over the file cap (60 fresh files → keep 50).
    a1 = root / "a1"
    a1.mkdir(parents=True)
    for i in range(60):
        (a1 / f"f{i:02d}.png").write_bytes(b"x")
    # Agent with only aged files (all > 7 days → all removed → dir reclaimed).
    a2 = root / "a2"
    a2.mkdir(parents=True)
    for i in range(3):
        _touch_old(a2 / f"old{i}.png", days_old=8)

    _sweep_all_uploads(root)

    assert len(list(a1.iterdir())) == 50
    assert not a2.exists(), "emptied dir should be reclaimed"


def test_sweep_reclaims_empty_dirs(tmp_path):
    from relaydeck.transports.api import _sweep_all_uploads

    root = tmp_path / "uploads"
    empty = root / "ghost"
    empty.mkdir(parents=True)
    _sweep_all_uploads(root)
    assert not empty.exists()


def test_sweep_noop_when_root_missing(tmp_path):
    from relaydeck.transports.api import _sweep_all_uploads

    # Must not raise when the uploads tree was never created.
    _sweep_all_uploads(tmp_path / "does-not-exist")


def test_boot_sweep_runs_on_create_app(tmp_path, monkeypatch):
    """Constructing the app sweeps the upload tree once at boot."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".relaydeck"
    home.mkdir(parents=True)
    (home / "runtime").mkdir()
    aged = home / "uploads" / "stale"
    aged.mkdir(parents=True)
    _touch_old(aged / "old.png", days_old=30)

    import relaydeck.orchestrator as _orch_mod
    from relaydeck.orchestrator import get_orchestrator
    from relaydeck.transports.api import create_app

    _orch_mod._orchestrator = None
    get_orchestrator(home)
    create_app(home)

    assert not aged.exists(), "boot sweep should age out + reclaim the dir"


def test_delete_agent_purges_upload_dir(tmp_path, monkeypatch):
    """Removing an agent deletes its upload dir (no orphaned leak)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, orch, home = _make_app(tmp_path)
    _create_agent(app, orch, "coder")

    up = home / "uploads" / "coder"
    up.mkdir(parents=True)
    (up / "shot.png").write_bytes(b"x")
    assert up.exists()

    orch.delete_agent("coder")
    assert not up.exists(), "agent removal must purge its upload dir"

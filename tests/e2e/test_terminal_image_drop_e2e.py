"""
E2E: terminal drag-drop image upload → POST FormData → inject path via PTY stdin.

The upload API is mocked via Playwright route so this test validates the
frontend contract without depending on the backend route landing first.
"""

from __future__ import annotations

import json
import shutil

import pytest

from ._webutil import (
    errors,
    make_ws_dir,
    seed_agent,
    seed_workspace,
    start_agent,
    wait_running,
)

pytestmark = pytest.mark.e2e


def _spawn_terminal_page(browser, base: str, tmp_path, *, agent_id: str = "a-term-drop"):
    if shutil.which("pi") is None:
        pytest.skip("pi not on PATH")

    ws_name = f"e2e-{agent_id}"
    ws = make_ws_dir(tmp_path, ws_name)
    seed_workspace(base, ws_name, ws)
    seed_agent(base, agent_id, "pi", ws_name)
    start_agent(base, agent_id)
    assert wait_running(base, agent_id, timeout=45.0) == "running"

    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    msgs: list = []
    page.on("console", lambda m: msgs.append(m))
    page.goto(f"{base}/?lens=agents&agent={agent_id}")
    page.wait_for_selector(".rail", timeout=15000)
    page.wait_for_selector(".xterm, .term-host", timeout=20000)
    page.wait_for_function(
        "() => window.__relaydeckTerm?.ws?.readyState === 1",
        timeout=15000,
    )
    return ctx, page, agent_id, msgs


def test_terminal_image_drop_overlay_and_upload(live_daemon, browser, tmp_path):
    """Drop simulation shows overlay, POSTs multipart file, injects path (no Enter)."""
    ctx, page, agent_id, msgs = _spawn_terminal_page(browser, live_daemon, tmp_path)
    mock_path = "/tmp/relaydeck-uploads/a-term-drop/deadbeef-test.png"
    upload_meta: list[dict] = []

    def handle_upload(route):
        req = route.request
        if req.method != "POST":
            return route.continue_()
        upload_meta.append({
            "url": req.url,
            "content_type": req.headers.get("content-type", ""),
            "authorization": req.headers.get("authorization", ""),
            "has_multipart": "multipart/form-data" in req.headers.get("content-type", ""),
        })
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "path": mock_path,
                "name": "test.png",
                "bytes": 8,
                "content_type": "image/png",
            }),
        )

    try:
        page.route(f"**/api/agents/{agent_id}/uploads", handle_upload)

        page.evaluate("""() => {
            const t = window.__relaydeckTerm;
            t._sendTextSpy = [];
            const orig = t._sendText.bind(t);
            t._sendText = (text) => { t._sendTextSpy.push(text); return orig(text); };
        }""")

        result = page.evaluate("""async () => {
            const term = window.__relaydeckTerm;
            const host = term.host;
            const dt = new DataTransfer();
            const blob = new Blob(
                [new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])],
                { type: 'image/png' },
            );
            dt.items.add(new File([blob], 'test.png', { type: 'image/png' }));
            host.dispatchEvent(new DragEvent('dragenter', {
                bubbles: true, cancelable: true, dataTransfer: dt,
            }));
            host.dispatchEvent(new DragEvent('dragover', {
                bubbles: true, cancelable: true, dataTransfer: dt,
            }));
            const overlayVisible = term._dropOverlay?.style.display === 'flex';
            host.dispatchEvent(new DragEvent('drop', {
                bubbles: true, cancelable: true, dataTransfer: dt,
            }));
            await new Promise((r) => setTimeout(r, 400));
            return {
                overlayVisible,
                overlayHidden: term._dropOverlay?.style.display === 'none',
                sendTextSpy: term._sendTextSpy || [],
            };
        }""")

        assert result["overlayVisible"], "drag overlay should appear on dragenter"
        assert result["overlayHidden"], "drag overlay should hide after drop"
        assert upload_meta, "expected POST /uploads"
        assert upload_meta[0]["has_multipart"], "fetch should send multipart boundary"
        assert upload_meta[0]["authorization"].lower().startswith("bearer ")

        spy = result["sendTextSpy"]
        assert spy, "expected _sendText after successful upload"
        sent = spy[-1]
        assert mock_path in sent
        assert sent.endswith(" ")
        assert "\r" not in sent and "\n" not in sent

        bad = errors(msgs)
        assert not bad, "console errors during drop upload:\n" + "\n".join(bad)
    finally:
        ctx.close()


def test_terminal_image_drop_quotes_paths_with_spaces(live_daemon, browser, tmp_path):
    """Paths containing whitespace are single-quoted before PTY injection."""
    ctx, page, agent_id, _msgs = _spawn_terminal_page(
        browser, live_daemon, tmp_path, agent_id="a-term-drop-sp",
    )
    mock_path = "/tmp/my upload/spaced name.png"

    def handle_upload(route):
        if route.request.method != "POST":
            return route.continue_()
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "path": mock_path,
                "name": "spaced name.png",
                "bytes": 4,
                "content_type": "image/png",
            }),
        )

    try:
        page.route(f"**/api/agents/{agent_id}/uploads", handle_upload)
        page.evaluate("""() => {
            const t = window.__relaydeckTerm;
            t._sendTextSpy = [];
            const orig = t._sendText.bind(t);
            t._sendText = (text) => { t._sendTextSpy.push(text); return orig(text); };
        }""")

        sent = page.evaluate("""async () => {
            const host = window.__relaydeckTerm.host;
            const dt = new DataTransfer();
            dt.items.add(new File([new Blob(['x'])], 'a.png', { type: 'image/png' }));
            host.dispatchEvent(new DragEvent('drop', {
                bubbles: true, cancelable: true, dataTransfer: dt,
            }));
            await new Promise((r) => setTimeout(r, 400));
            const spy = window.__relaydeckTerm._sendTextSpy || [];
            return spy.length ? spy[spy.length - 1] : '';
        }""")

        assert sent == "'/tmp/my upload/spaced name.png' "
    finally:
        ctx.close()

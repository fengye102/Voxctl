"""Regression tests for audit privacy and retention."""

import json
from types import SimpleNamespace

from core import audit


def test_clipboard_content_is_not_persisted(tmp_path):
    secret = "sk-test-sensitive-clipboard-token"
    result = SimpleNamespace(
        action_type="clipboard_read",
        success=True,
        needs_confirmation=False,
        should_exit=False,
        error=None,
        payload={
            "message": f"剪贴板内容是：{secret}",
            "text": secret,
            "length": len(secret),
            "truncated": False,
        },
    )
    intent = SimpleNamespace(intent="clipboard_read")
    path = tmp_path / "actions.log"

    audit.log_action(path, "读一下剪贴板", intent, result, False)

    written = path.read_text(encoding="utf-8")
    entry = json.loads(written)
    assert secret not in written
    assert entry["action_type"] == "clipboard_read"
    assert entry["payload"] == {"length": len(secret), "truncated": False}
    assert result.payload["text"] == secret  # TTS still receives the original result.


def test_audit_log_rotates_before_unbounded_growth(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_MAX_AUDIT_BYTES", 256)
    path = tmp_path / "actions.log"
    path.write_text("old\n" * 100, encoding="utf-8")

    audit._write_entry(path, {"event": "new"})

    assert (tmp_path / "actions.log.1").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["event"] == "new"


def test_non_sensitive_action_payload_is_preserved(tmp_path):
    result = SimpleNamespace(
        action_type="query_system", success=True, needs_confirmation=False,
        should_exit=False, error=None, payload={"battery_percent": 60},
    )
    path = tmp_path / "actions.log"

    audit.log_action(path, "电量多少", SimpleNamespace(intent="query_system"), result, False)

    assert json.loads(path.read_text(encoding="utf-8"))["payload"] == {"battery_percent": 60}

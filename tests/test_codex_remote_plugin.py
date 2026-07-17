"""Contract and behavior tests for the fork-maintained remote Codex plugin."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "mempalace-remote"
HOOK_SCRIPT = PLUGIN_ROOT / "hooks" / "mempalace_remote.py"
CONFIG_SCRIPT = PLUGIN_ROOT / "scripts" / "configure_static_token.py"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("mempalace_remote_hook", HOOK_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_config_module():
    spec = importlib.util.spec_from_file_location("mempalace_remote_config", CONFIG_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_codex_transcript(path: Path, user_messages: int) -> None:
    records = [
        {
            "timestamp": "2026-07-17T00:00:00Z",
            "type": "session_meta",
            "payload": {"cwd": "/work/project-alpha"},
        }
    ]
    for index in range(user_messages):
        records.extend(
            [
                {
                    "timestamp": f"2026-07-17T00:{index:02d}:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": f"exact user text {index}"},
                },
                {
                    "timestamp": f"2026-07-17T00:{index:02d}:30Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "message": f"exact assistant text {index}",
                    },
                },
                {
                    "timestamp": f"2026-07-17T00:{index:02d}:31Z",
                    "type": "response_item",
                    "payload": {"message": f"duplicate assistant text {index}"},
                },
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


@pytest.fixture
def hook(monkeypatch, tmp_path):
    module = _load_hook_module()
    transcript_root = tmp_path / "sessions"
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("MEMPAL_REMOTE_TRANSCRIPT_ROOTS", str(transcript_root))
    monkeypatch.setenv("MEMPALACE_MCP_TOKEN", "test-token")
    monkeypatch.delenv("MEMPAL_DISABLE_HOOK", raising=False)
    monkeypatch.delenv("MEMPALACE_HOOKS_AUTO_SAVE", raising=False)
    monkeypatch.delenv("MEMPAL_SAVE_INTERVAL", raising=False)
    return module


def test_remote_plugin_manifest_and_marketplace_contract():
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "mempalace-remote"
    assert "mcpServers" not in manifest
    assert "hooks" not in manifest
    assert not (PLUGIN_ROOT / ".mcp.json").exists()

    marketplace = json.loads((REPO_ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
    remote = next(
        plugin for plugin in marketplace["plugins"] if plugin["name"] == "mempalace-remote"
    )
    assert remote["source"]["path"] == "./plugins/mempalace-remote"


def test_hook_manifest_matches_current_codex_lifecycle_contract():
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "Stop", "PreCompact"}
    for groups in hooks.values():
        for group in groups:
            for command_hook in group["hooks"]:
                assert command_hook["type"] == "command"
                assert "${PLUGIN_ROOT}/hooks/mempalace_remote.py" in command_hook["command"]
                assert "mempalace hook run" not in command_hook["command"]


def test_stop_matches_upstream_interval_and_writes_remote_checkpoint(hook, tmp_path):
    transcript = tmp_path / "sessions" / "rollout.jsonl"
    _write_codex_transcript(transcript, user_messages=15)
    calls = []

    def fake_call(tool_name, arguments):
        calls.append((tool_name, arguments))
        return {"content": []}

    result = hook.process_event(
        "stop",
        {
            "session_id": "session-1",
            "transcript_path": str(transcript),
            "cwd": "/work/project-alpha",
        },
        mcp_call=fake_call,
    )

    assert result == {"saved": True, "messages": 30, "chunks": 1, "wing": "wing_project_alpha"}
    assert [name for name, _ in calls] == ["mempalace_add_drawer", "mempalace_diary_write"]
    drawer = calls[0][1]
    assert "exact user text 0" in drawer["content"]
    assert "exact assistant text 14" in drawer["content"]
    assert "duplicate assistant text" not in drawer["content"]
    assert drawer["room"] == "sessions"
    assert drawer["added_by"] == "codex"
    diary = calls[1][1]
    assert diary["agent_name"] == "codex"
    assert diary["topic"] == "checkpoint"

    state = json.loads(hook._state_path("session-1").read_text())
    assert state["message_count"] == 30
    assert state["user_count"] == 15


def test_stop_below_interval_is_a_noop(hook, tmp_path):
    transcript = tmp_path / "sessions" / "rollout.jsonl"
    _write_codex_transcript(transcript, user_messages=14)
    calls = []

    result = hook.process_event(
        "stop",
        {"session_id": "session-2", "transcript_path": str(transcript)},
        mcp_call=lambda *args: calls.append(args),
    )

    assert result == {"saved": False, "reason": "interval not reached"}
    assert calls == []
    assert not hook._state_path("session-2").exists()


def test_precompact_flushes_only_the_unsaved_delta(hook, tmp_path):
    transcript = tmp_path / "sessions" / "rollout.jsonl"
    _write_codex_transcript(transcript, user_messages=15)
    data = {"session_id": "session-3", "transcript_path": str(transcript)}
    first_calls = []
    hook.process_event("stop", data, mcp_call=lambda *args: first_calls.append(args) or {})

    _write_codex_transcript(transcript, user_messages=16)
    second_calls = []
    result = hook.process_event(
        "precompact",
        data,
        mcp_call=lambda *args: second_calls.append(args) or {},
    )

    assert result["saved"] is True
    assert result["messages"] == 2
    assert "exact user text 15" in second_calls[0][1]["content"]
    assert "exact user text 14" not in second_calls[0][1]["content"]


def test_failed_remote_write_does_not_advance_state(hook, tmp_path):
    transcript = tmp_path / "sessions" / "rollout.jsonl"
    _write_codex_transcript(transcript, user_messages=15)

    def fail_call(tool_name, arguments):
        raise hook.RemoteHookError(f"simulated failure for {tool_name}")

    with pytest.raises(hook.RemoteHookError, match="simulated failure"):
        hook.process_event(
            "stop",
            {"session_id": "session-4", "transcript_path": str(transcript)},
            mcp_call=fail_call,
        )
    assert not hook._state_path("session-4").exists()


def test_transcript_path_must_be_inside_codex_roots(hook, tmp_path):
    transcript = tmp_path / "outside.jsonl"
    _write_codex_transcript(transcript, user_messages=15)
    with pytest.raises(hook.RemoteHookError, match="outside the configured Codex roots"):
        hook.process_event(
            "stop",
            {"session_id": "session-5", "transcript_path": str(transcript)},
            mcp_call=lambda *args: {},
        )


def test_static_config_helper_preserves_existing_config_and_replaces_credential(tmp_path):
    config_module = _load_config_module()
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "gpt-5"\n', encoding="utf-8")

    config_module.update_config(config_path, "first-test-token")
    config_module.update_config(config_path, "second-test-token")

    content = config_path.read_text(encoding="utf-8")
    assert 'model = "gpt-5"' in content
    assert content.count(config_module.BEGIN_MARKER) == 1
    assert "first-test-token" not in content
    assert "Bearer second-test-token" in content
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_hook_prefers_static_codex_config_over_environment(hook, monkeypatch, tmp_path):
    config_module = _load_config_module()
    config_path = tmp_path / "codex-home" / "config.toml"
    config_module.update_config(
        config_path,
        "static-test-token",
        "https://memory.example.test/mcp",
    )
    monkeypatch.setenv("MEMPALACE_MCP_TOKEN", "environment-test-token")

    url, token = hook._mcp_connection_settings()

    assert url == "https://memory.example.test/mcp"
    assert token == "static-test-token"


def test_mcp_call_uses_environment_fallback_without_static_config(hook, monkeypatch):
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": '{"success": true}'}]},
                }
            ).encode()

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)
    result = hook._mcp_call("mempalace_status", {})
    assert result["content"]
    assert captured["authorization"] == "Bearer test-token"
    assert captured["timeout"] == hook.DEFAULT_TIMEOUT_SECONDS

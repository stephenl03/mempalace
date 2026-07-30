#!/usr/bin/env python3
"""Claude Code lifecycle hook that checkpoints transcripts to a remote MemPalace.

This is the Claude Code counterpart to ``mempalace_remote.py`` (Codex). It keeps
the same contract — no local MemPalace install, every write goes to the shared
HTTP MCP server — so Codex and Claude Code populate one palace.

What differs from the Codex hook:

* connection settings come from ``~/.claude.json`` (``mcpServers.mempalace``)
  rather than ``~/.codex/config.toml``;
* transcripts are Claude Code JSONL under ``~/.claude/projects``, whose entries
  carry ``message.content`` as a string or a list of typed blocks;
* drawers are attributed to ``claude-code`` and keyed ``claude://…``.

Events: ``session-start``, ``stop``, ``precompact``, ``session-end``.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

DEFAULT_MCP_URL = "https://mempalace.k8s.lazy.sh/mcp"
DEFAULT_MCP_SERVER_NAME = "mempalace"
DEFAULT_SAVE_INTERVAL = 15
DEFAULT_MAX_DRAWER_CHARS = 24_000
DEFAULT_TIMEOUT_SECONDS = 20.0
LOCK_STALE_SECONDS = 120.0
TRUTHY = frozenset({"1", "true", "yes", "on"})
FALSEY = frozenset({"0", "false", "no", "off"})
SUPPORTED_EVENTS = frozenset({"session-start", "stop", "precompact", "session-end"})
AGENT_NAME = "claude-code"


class RemoteHookError(RuntimeError):
    """Raised for any condition that should abort this hook without failing Claude."""


def _safe_slug(value: str, fallback: str = "sessions", limit: int = 120) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in value.strip())
    cleaned = cleaned.strip("_").lower()
    return (cleaned[:limit] or fallback)


def _session_id(data: dict) -> str:
    raw = data.get("session_id") or data.get("sessionId") or "unknown"
    return _safe_slug(str(raw), fallback="unknown", limit=80)


def _data_dir() -> Path:
    configured = os.environ.get("MEMPAL_REMOTE_DATA_DIR", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".mempalace" / "claude-remote"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _log(message: str) -> None:
    """Append a single line to hook.log. Never raises, never logs secrets."""
    try:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log_path = _data_dir() / "hook.log"
        previous = os.umask(0o077)
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {message}\n")
        finally:
            os.umask(previous)
    except OSError:
        pass


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in TRUTHY:
        return True
    if raw in FALSEY:
        return False
    return default


def _hooks_enabled() -> bool:
    if _env_flag("MEMPAL_DISABLE_HOOK", False):
        return False
    return _env_flag("MEMPALACE_HOOKS_AUTO_SAVE", True)


def _positive_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default
    if not minimum <= value <= maximum:
        return default
    return value


def _claude_config_path() -> Path:
    configured = os.environ.get("MEMPAL_REMOTE_CONFIG_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("CLAUDE_CONFIG_PATH", Path.home() / ".claude.json")).expanduser()


def _config_mcp_settings() -> tuple[Optional[str], Optional[str]]:
    """Return (url, bearer token) from Claude Code's static MCP configuration."""
    path = _claude_config_path()
    if not path.is_file():
        return None, None
    try:
        with path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RemoteHookError(f"could not read Claude MCP config: {type(exc).__name__}") from exc
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return None, None
    server = servers.get(os.environ.get("MEMPAL_REMOTE_SERVER_NAME", DEFAULT_MCP_SERVER_NAME))
    if not isinstance(server, dict):
        return None, None
    configured_url = server.get("url")
    url = configured_url.strip() if isinstance(configured_url, str) else None
    headers = server.get("headers", {})
    if not isinstance(headers, dict):
        return url, None
    authorization = next(
        (
            value.strip()
            for name, value in headers.items()
            if isinstance(name, str) and name.lower() == "authorization" and isinstance(value, str)
        ),
        "",
    )
    if not authorization:
        return url, None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise RemoteHookError("Claude MCP Authorization header must contain a Bearer token")
    return url, token.strip()


def _mcp_connection_settings() -> tuple[str, str]:
    config_url, config_token = _config_mcp_settings()
    url = os.environ.get("MEMPALACE_MCP_URL", "").strip() or config_url or DEFAULT_MCP_URL
    token = config_token or os.environ.get("MEMPALACE_MCP_TOKEN", "").strip()
    if not token:
        raise RemoteHookError(
            "MemPalace token is missing from ~/.claude.json and MEMPALACE_MCP_TOKEN is not set"
        )
    return url, token


def _save_interval() -> int:
    return _positive_int("MEMPAL_SAVE_INTERVAL", DEFAULT_SAVE_INTERVAL, 1, 10_000)


def _max_drawer_chars() -> int:
    return _positive_int("MEMPAL_REMOTE_MAX_DRAWER_CHARS", DEFAULT_MAX_DRAWER_CHARS, 2_000, 100_000)


def _timeout_seconds() -> float:
    try:
        value = float(os.environ.get("MEMPAL_REMOTE_TIMEOUT_SECONDS", "").strip())
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return value if 1.0 <= value <= 120.0 else DEFAULT_TIMEOUT_SECONDS


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _allowed_transcript_roots() -> list[Path]:
    configured = os.environ.get("MEMPAL_REMOTE_TRANSCRIPT_ROOTS", "")
    if configured:
        raw_roots = [item for item in configured.split(os.pathsep) if item]
    else:
        claude_home = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude")).expanduser()
        raw_roots = [str(claude_home / "projects")]
    return [Path(item).expanduser().resolve() for item in raw_roots]


def _validate_transcript_path(raw_path: str) -> Path:
    if not raw_path:
        raise RemoteHookError("hook payload did not include transcript_path")
    if ".." in Path(raw_path).parts:
        raise RemoteHookError("transcript_path contains traversal components")
    path = Path(raw_path).expanduser().resolve()
    if path.suffix not in {".json", ".jsonl"}:
        raise RemoteHookError("transcript_path must end in .json or .jsonl")
    if not path.is_file():
        raise RemoteHookError("transcript_path is not a regular file")
    if not any(_is_relative_to(path, root) for root in _allowed_transcript_roots()):
        raise RemoteHookError("transcript_path is outside the configured Claude roots")
    return path


def _strip_system_reminders(text: str) -> str:
    """Drop harness-injected <system-reminder> blocks; they are not user speech."""
    while True:
        start = text.find("<system-reminder>")
        if start == -1:
            break
        end = text.find("</system-reminder>", start)
        if end == -1:
            text = text[:start]
            break
        text = text[:start] + text[end + len("</system-reminder>") :]
    return text


def _block_text(content) -> str:
    """Flatten Claude message content to its human-readable text.

    Only ``text`` blocks are canonical turn content. ``thinking`` is internal,
    and ``tool_use``/``tool_result`` are captured as their own entries upstream;
    including them here would duplicate and bloat every drawer.
    """
    if isinstance(content, str):
        return _strip_system_reminders(content).strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(_strip_system_reminders(text).strip())
    return "\n\n".join(part for part in parts if part).strip()


def _parse_transcript(path: Path) -> tuple[list[dict[str, str]], Optional[str]]:
    """Parse canonical Claude Code user/assistant turns from a JSONL transcript."""
    messages: list[dict[str, str]] = []
    transcript_cwd: Optional[str] = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                if isinstance(entry.get("cwd"), str) and not transcript_cwd:
                    transcript_cwd = entry["cwd"]
                role = entry.get("type")
                if role not in {"user", "assistant"}:
                    continue
                # Sidechains are subagent transcripts; meta entries are harness noise.
                if entry.get("isSidechain") or entry.get("isMeta"):
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                text = _block_text(message.get("content"))
                if not text or "<command-message>" in text:
                    continue
                messages.append(
                    {
                        "role": role,
                        "text": text,
                        "timestamp": str(entry.get("timestamp", "")),
                    }
                )
    except OSError as exc:
        raise RemoteHookError(f"could not read transcript: {exc}") from exc
    return messages, transcript_cwd


def _state_path(session_id: str) -> Path:
    return _data_dir() / "sessions" / f"{session_id}.json"


def _load_state(session_id: str) -> dict:
    try:
        data = json.loads(_state_path(session_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_state(session_id: str, state: dict) -> None:
    path = _state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    previous = os.umask(0o077)
    try:
        temp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        temp.replace(path)
    finally:
        os.umask(previous)


@contextmanager
def _session_lock(session_id: str, wait_seconds: float) -> Iterator[bool]:
    """Best-effort per-session lock so concurrent checkpoints don't double-write."""
    lock_path = _data_dir() / "sessions" / f"{session_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    handle = None
    while True:
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > LOCK_STALE_SECONDS:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(0.25)
    try:
        yield True
    finally:
        if handle is not None:
            os.close(handle)
        lock_path.unlink(missing_ok=True)


def _decode_mcp_response(raw: bytes, content_type: str) -> dict:
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in (content_type or "").lower():
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RemoteHookError("MemPalace returned a non-JSON response") from exc
    if not isinstance(payload, dict):
        raise RemoteHookError("MemPalace returned an unexpected response shape")
    return payload


def _mcp_call(tool_name: str, arguments: dict) -> dict:
    """Call one MCP tool over streamable HTTP. Never logs the token or payload."""
    url, token = _mcp_connection_settings()
    body = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) % 1_000_000,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_timeout_seconds()) as response:
            payload = _decode_mcp_response(response.read(), response.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as exc:
        raise RemoteHookError(f"MemPalace HTTP {exc.code} for {tool_name}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RemoteHookError(f"MemPalace unreachable: {type(exc).__name__}") from None
    if "error" in payload:
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else "unknown"
        raise RemoteHookError(f"MemPalace rejected {tool_name} (code {code})")
    return payload


def _message_blocks(messages: list[dict[str, str]]) -> list[str]:
    blocks = []
    for message in messages:
        role = message["role"].upper()
        timestamp = message.get("timestamp", "")
        heading = f"{role} [{timestamp}]" if timestamp else role
        blocks.append(f"{heading}:\n{message['text']}")
    return blocks


def _chunk_messages(messages: list[dict[str, str]], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for block in _message_blocks(messages):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(block) > max_chars:
            chunks.append(block[:max_chars])
            block = block[max_chars:]
        current = block
    if current:
        chunks.append(current)
    return chunks


def _project_wing(data: dict, transcript_cwd: Optional[str]) -> str:
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else transcript_cwd
    if not cwd:
        return "wing_sessions"
    return f"wing_{_safe_slug(Path(cwd).expanduser().name)}"


def _diary_entry(messages: list[dict[str, str]], session_id: str) -> str:
    recent = [item["text"][:200] for item in messages if item["role"] == "user"][-30:]
    topics = "|".join(message[:80] for message in recent[-10:])
    today = datetime.now().strftime("%Y-%m-%d")
    return f"CHECKPOINT:{today}|session:{session_id}|msgs:{len(recent)}|recent:{topics}"


MCPCall = Callable[[str, dict], dict]


def process_event(event: str, data: dict, mcp_call: MCPCall = _mcp_call) -> dict:
    """Process one hook event. Synchronous so it is deterministic under test."""
    if event not in SUPPORTED_EVENTS:
        raise RemoteHookError(f"unsupported hook event: {event}")
    if not _hooks_enabled():
        return {"saved": False, "reason": "disabled"}
    if event == "session-start":
        _data_dir().mkdir(parents=True, exist_ok=True)
        return {"saved": False, "reason": "initialized"}

    session_id = _session_id(data)
    path = _validate_transcript_path(str(data.get("transcript_path", "")))
    messages, transcript_cwd = _parse_transcript(path)
    if not messages:
        return {"saved": False, "reason": "empty transcript"}

    wait_seconds = 20.0 if event in {"precompact", "session-end"} else 0.0
    with _session_lock(session_id, wait_seconds) as acquired:
        if not acquired:
            return {"saved": False, "reason": "checkpoint already running"}
        state = _load_state(session_id)
        saved_message_count = int(state.get("message_count", 0) or 0)
        saved_user_count = int(state.get("user_count", 0) or 0)
        total_user_count = sum(item["role"] == "user" for item in messages)
        # Transcript shrank (rewind/rollback): fall back to a full re-save.
        if saved_message_count > len(messages) or saved_user_count > total_user_count:
            saved_message_count = 0
            saved_user_count = 0
        if event == "stop" and total_user_count - saved_user_count < _save_interval():
            return {"saved": False, "reason": "interval not reached"}
        delta = messages[saved_message_count:]
        if not delta:
            return {"saved": False, "reason": "no new messages"}

        wing = _project_wing(data, transcript_cwd)
        hostname = _safe_slug(socket.gethostname(), fallback="host", limit=63)
        source_base = f"claude://{hostname}/{session_id}/{saved_message_count + 1}-{len(messages)}"
        chunks = _chunk_messages(delta, _max_drawer_chars())
        for index, content in enumerate(chunks, start=1):
            mcp_call(
                "mempalace_add_drawer",
                {
                    "wing": wing,
                    "room": "sessions",
                    "content": content,
                    "source_file": f"{source_base}#chunk-{index}-of-{len(chunks)}",
                    "added_by": AGENT_NAME,
                },
            )
        mcp_call(
            "mempalace_diary_write",
            {
                "agent_name": AGENT_NAME,
                "entry": _diary_entry(messages, session_id),
                "topic": "checkpoint",
                "wing": wing,
            },
        )
        # State advances only after every remote write succeeded, so a failure
        # is retried on the next lifecycle event rather than silently dropped.
        _write_state(
            session_id,
            {
                "message_count": len(messages),
                "user_count": total_user_count,
                "transcript_path": str(path),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return {"saved": True, "messages": len(delta), "chunks": len(chunks), "wing": wing}


def _spawn_worker(event: str, data: dict) -> None:
    command = [sys.executable, str(Path(__file__).resolve()), event, "--worker"]
    kwargs: dict = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = 0
        for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
            flags |= getattr(subprocess, name, 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    if process.stdin is None:
        raise RemoteHookError("could not open worker stdin")
    process.stdin.write(json.dumps(data))
    process.stdin.close()


def _emit(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":")))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=sorted(SUPPORTED_EVENTS))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise RemoteHookError("hook input must be a JSON object")
    except (json.JSONDecodeError, EOFError, RemoteHookError) as exc:
        _log(f"{args.event} invalid input: {exc}")
        if not args.worker:
            _emit({})
        return 0

    try:
        foreground = _env_flag("MEMPAL_REMOTE_FOREGROUND", False)
        # Stop and SessionEnd must return immediately; do the work detached.
        if args.event in {"stop", "session-end"} and not args.worker and not foreground and _hooks_enabled():
            _spawn_worker(args.event, data)
            _emit({})
            return 0
        result = process_event(args.event, data)
        if result.get("saved"):
            _log(
                f"{args.event} saved session={_session_id(data)} "
                f"messages={result['messages']} chunks={result['chunks']} wing={result['wing']}"
            )
        if not args.worker:
            if result.get("saved") and _env_flag("MEMPAL_VERBOSE", False):
                _emit({"systemMessage": "MemPalace remote checkpoint saved"})
            else:
                _emit({})
    except Exception as exc:
        _log(f"{args.event} failed session={_session_id(data)} error={type(exc).__name__}: {exc}")
        if not args.worker:
            _emit({"systemMessage": "MemPalace remote checkpoint failed; see hook.log"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

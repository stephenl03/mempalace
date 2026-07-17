#!/usr/bin/env python3
"""Codex lifecycle hooks that checkpoint transcripts to a remote MemPalace MCP server."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10 compatibility
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # pragma: no cover - diagnosed when config auth is used
        tomllib = None


DEFAULT_MCP_URL = "https://mempalace.k8s.lazy.sh/mcp"
DEFAULT_MCP_SERVER_NAME = "mempalace"
DEFAULT_SAVE_INTERVAL = 15
DEFAULT_MAX_DRAWER_CHARS = 24_000
DEFAULT_TIMEOUT_SECONDS = 20.0
LOCK_STALE_SECONDS = 120.0
TRUTHY = frozenset({"1", "true", "yes", "on"})
FALSEY = frozenset({"0", "false", "no", "off"})
SUPPORTED_EVENTS = frozenset({"session-start", "stop", "precompact"})


class RemoteHookError(RuntimeError):
    """Raised when a remote checkpoint cannot be completed."""


def _safe_slug(value: str, fallback: str = "sessions", limit: int = 120) -> str:
    slug = value.lower().replace(" ", "_").replace("-", "_")
    slug = re.sub(r"[^\w.']+", "_", slug)
    slug = re.sub(r"\.{2,}", ".", slug)
    slug = slug[:limit].strip("_.'")
    return slug or fallback


def _session_id(data: dict) -> str:
    raw = str(data.get("session_id", "unknown"))
    return re.sub(r"[^a-zA-Z0-9_-]", "", raw) or "unknown"


def _plugin_data_dir() -> Path:
    configured = os.environ.get("PLUGIN_DATA") or os.environ.get("MEMPAL_REMOTE_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return codex_home / "mempalace-remote"


def _log(message: str) -> None:
    """Write metadata-only diagnostics. Callers must never pass transcript content."""
    try:
        data_dir = _plugin_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = data_dir / "hook.log"
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
        try:
            log_path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        pass


def _hooks_enabled() -> bool:
    if os.environ.get("MEMPAL_DISABLE_HOOK", "").strip().lower() in TRUTHY:
        return False
    setting = os.environ.get("MEMPALACE_HOOKS_AUTO_SAVE", "").strip().lower()
    return setting not in FALSEY


def _positive_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    if not minimum <= value <= maximum:
        return default
    return value


def _codex_config_path() -> Path:
    configured = os.environ.get("MEMPAL_REMOTE_CONFIG_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return codex_home / "config.toml"


def _config_mcp_settings() -> tuple[Optional[str], Optional[str]]:
    """Return the URL and bearer token from Codex's static MCP configuration."""
    path = _codex_config_path()
    if not path.is_file():
        return None, None
    if tomllib is None:
        raise RemoteHookError("reading static MCP auth requires Python 3.11+ or the tomli package")
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, ValueError) as exc:
        raise RemoteHookError(f"could not read Codex MCP config: {type(exc).__name__}") from exc
    server = config.get("mcp_servers", {}).get(DEFAULT_MCP_SERVER_NAME, {})
    if not isinstance(server, dict):
        return None, None
    configured_url = server.get("url")
    url = configured_url.strip() if isinstance(configured_url, str) else None
    headers = server.get("http_headers", {})
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
        raise RemoteHookError("Codex MCP Authorization header must contain a Bearer token")
    return url, token.strip()


def _mcp_connection_settings() -> tuple[str, str]:
    config_url, config_token = _config_mcp_settings()
    url = os.environ.get("MEMPALACE_MCP_URL", "").strip() or config_url or DEFAULT_MCP_URL
    token = config_token or os.environ.get("MEMPALACE_MCP_TOKEN", "").strip()
    if not token:
        raise RemoteHookError(
            "MemPalace token is missing from Codex config and MEMPALACE_MCP_TOKEN is not set"
        )
    return url, token


def _save_interval() -> int:
    return _positive_int("MEMPAL_SAVE_INTERVAL", DEFAULT_SAVE_INTERVAL, 1, 10_000)


def _max_drawer_chars() -> int:
    return _positive_int(
        "MEMPAL_REMOTE_MAX_DRAWER_CHARS",
        DEFAULT_MAX_DRAWER_CHARS,
        2_000,
        100_000,
    )


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
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        raw_roots = [str(codex_home / "sessions"), str(codex_home / "archived_sessions")]
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
        raise RemoteHookError("transcript_path is outside the configured Codex roots")
    return path


def _parse_transcript(path: Path) -> tuple[list[dict[str, str]], Optional[str]]:
    """Parse canonical Codex event messages, avoiding duplicate response_item records."""
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
                if entry.get("type") == "session_meta":
                    payload = entry.get("payload", {})
                    if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                        transcript_cwd = payload["cwd"]
                    continue
                if entry.get("type") != "event_msg":
                    continue
                payload = entry.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                payload_type = payload.get("type")
                text = payload.get("message")
                if not isinstance(text, str) or not text.strip():
                    continue
                if "<command-message>" in text:
                    continue
                role = {"user_message": "user", "agent_message": "assistant"}.get(payload_type)
                if role is None:
                    continue
                messages.append(
                    {
                        "role": role,
                        "text": text.strip(),
                        "timestamp": str(entry.get("timestamp", "")),
                    }
                )
    except OSError as exc:
        raise RemoteHookError(f"could not read transcript: {exc}") from exc
    return messages, transcript_cwd


def _state_path(session_id: str) -> Path:
    return _plugin_data_dir() / "sessions" / f"{session_id}.json"


def _load_state(session_id: str) -> dict:
    try:
        data = json.loads(_state_path(session_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _write_state(session_id: str, state: dict) -> None:
    path = _state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


@contextmanager
def _session_lock(session_id: str, wait_seconds: float) -> Iterator[bool]:
    lock_path = _plugin_data_dir() / "locks" / f"{session_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    acquired = False
    while True:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(f"{os.getpid()} {time.time()}")
            acquired = True
            break
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                lock_path.unlink()
            except OSError:
                pass


def _decode_mcp_response(raw: bytes, content_type: str) -> dict:
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in content_type:
        candidates = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if not candidates:
            raise RemoteHookError("remote MCP returned an empty event stream")
        text = candidates[-1]
    try:
        response = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RemoteHookError("remote MCP returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise RemoteHookError("remote MCP returned a non-object response")
    return response


def _mcp_call(tool_name: str, arguments: dict) -> dict:
    url, token = _mcp_connection_settings()
    parsed_url = urllib.parse.urlparse(url)
    allow_insecure = os.environ.get("MEMPAL_REMOTE_ALLOW_INSECURE", "").lower() in TRUTHY
    if parsed_url.scheme != "https" and not allow_insecure:
        raise RemoteHookError("remote MCP URL must use HTTPS")
    try:
        timeout = float(os.environ.get("MEMPAL_REMOTE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "mempalace-remote-codex-hook/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, timeout)) as remote_response:
            response = _decode_mcp_response(
                remote_response.read(),
                remote_response.headers.get("Content-Type", "application/json"),
            )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RemoteHookError(f"remote MCP request failed: {type(exc).__name__}") from exc
    if response.get("error"):
        error = response["error"]
        message = (
            error.get("message", "unknown MCP error") if isinstance(error, dict) else str(error)
        )
        raise RemoteHookError(f"remote MCP error: {message}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RemoteHookError("remote MCP response is missing result")
    if result.get("isError"):
        raise RemoteHookError("remote MCP tool reported an error")
    for item in result.get("content", []):
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            tool_result = json.loads(item.get("text", ""))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(tool_result, dict) and tool_result.get("success") is False:
            raise RemoteHookError(str(tool_result.get("error", "remote write failed")))
    return result


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
    project_name = Path(cwd).expanduser().name
    return f"wing_{_safe_slug(project_name)}"


def _diary_entry(messages: list[dict[str, str]], session_id: str) -> str:
    recent = [item["text"][:200] for item in messages if item["role"] == "user"][-30:]
    topics = "|".join(message[:80] for message in recent[-10:])
    today = datetime.now().strftime("%Y-%m-%d")
    return f"CHECKPOINT:{today}|session:{session_id}|msgs:{len(recent)}|recent:{topics}"


MCPCall = Callable[[str, dict], dict]


def process_event(event: str, data: dict, mcp_call: MCPCall = _mcp_call) -> dict:
    """Process one hook event. This function is synchronous for deterministic testing."""
    if event not in SUPPORTED_EVENTS:
        raise RemoteHookError(f"unsupported hook event: {event}")
    if not _hooks_enabled():
        return {"saved": False, "reason": "disabled"}
    if event == "session-start":
        _plugin_data_dir().mkdir(parents=True, exist_ok=True)
        return {"saved": False, "reason": "initialized"}

    session_id = _session_id(data)
    path = _validate_transcript_path(str(data.get("transcript_path", "")))
    messages, transcript_cwd = _parse_transcript(path)
    if not messages:
        return {"saved": False, "reason": "empty transcript"}

    wait_seconds = 20.0 if event == "precompact" else 0.0
    with _session_lock(session_id, wait_seconds) as acquired:
        if not acquired:
            return {"saved": False, "reason": "checkpoint already running"}
        state = _load_state(session_id)
        saved_message_count = int(state.get("message_count", 0) or 0)
        saved_user_count = int(state.get("user_count", 0) or 0)
        total_user_count = sum(item["role"] == "user" for item in messages)
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
        source_base = f"codex://{hostname}/{session_id}/{saved_message_count + 1}-{len(messages)}"
        chunks = _chunk_messages(delta, _max_drawer_chars())
        for index, content in enumerate(chunks, start=1):
            mcp_call(
                "mempalace_add_drawer",
                {
                    "wing": wing,
                    "room": "sessions",
                    "content": content,
                    "source_file": f"{source_base}#chunk-{index}-of-{len(chunks)}",
                    "added_by": "codex",
                },
            )
        mcp_call(
            "mempalace_diary_write",
            {
                "agent_name": "codex",
                "entry": _diary_entry(messages, session_id),
                "topic": "checkpoint",
                "wing": wing,
            },
        )
        _write_state(
            session_id,
            {
                "message_count": len(messages),
                "user_count": total_user_count,
                "transcript_path": str(path),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return {
            "saved": True,
            "messages": len(delta),
            "chunks": len(chunks),
            "wing": wing,
        }


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
        foreground = os.environ.get("MEMPAL_REMOTE_FOREGROUND", "").lower() in TRUTHY
        if args.event == "stop" and not args.worker and not foreground and _hooks_enabled():
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
            if result.get("saved") and os.environ.get("MEMPAL_VERBOSE", "").lower() in TRUTHY:
                _emit({"systemMessage": "MemPalace remote checkpoint saved"})
            else:
                _emit({})
    except Exception as exc:
        _log(f"{args.event} failed session={_session_id(data)} error={type(exc).__name__}: {exc}")
        if not args.worker:
            _emit({"systemMessage": "MemPalace remote checkpoint failed; see plugin hook.log"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

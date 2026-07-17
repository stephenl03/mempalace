#!/usr/bin/env python3
"""Store remote MemPalace MCP authentication in Codex's user config."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional


DEFAULT_URL = "https://mempalace.k8s.lazy.sh/mcp"
BEGIN_MARKER = "# BEGIN mempalace-remote managed MCP"
END_MARKER = "# END mempalace-remote managed MCP"
MANAGED_BLOCK = re.compile(rf"(?ms)^{re.escape(BEGIN_MARKER)}\n.*?^{re.escape(END_MARKER)}\n?")
MEMPALACE_TABLE = re.compile(
    r"(?m)^\s*\[\s*mcp_servers\.(?:mempalace|\"mempalace\")\s*\]\s*(?:#.*)?$"
)


class ConfigError(RuntimeError):
    """Raised when the static credential config cannot be updated safely."""


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_block(token: str, url: str) -> str:
    authorization = _toml_string(f"Bearer {token}")
    return (
        f"{BEGIN_MARKER}\n"
        "[mcp_servers.mempalace]\n"
        f"url = {_toml_string(url)}\n"
        f"http_headers = {{ Authorization = {authorization} }}\n"
        "startup_timeout_sec = 20\n"
        "tool_timeout_sec = 120\n"
        'default_tools_approval_mode = "writes"\n'
        f"{END_MARKER}\n"
    )


def update_config(config_path: Path, token: str, url: str = DEFAULT_URL) -> None:
    token = token.strip()
    url = url.strip()
    if not token:
        raise ConfigError("token input was empty")
    if "\r" in token or "\n" in token:
        raise ConfigError("token input contains an embedded newline")
    if not url.startswith("https://"):
        raise ConfigError("MCP URL must use HTTPS")

    try:
        existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except OSError as exc:
        raise ConfigError(f"could not read {config_path}: {exc}") from exc
    managed = MANAGED_BLOCK.search(existing)
    if not managed and MEMPALACE_TABLE.search(existing):
        raise ConfigError(
            "an unmanaged [mcp_servers.mempalace] table already exists; remove or merge it first"
        )

    block = _render_block(token, url)
    if managed:
        updated = MANAGED_BLOCK.sub(block, existing, count=1)
    else:
        separator = "" if not existing or existing.endswith("\n\n") else "\n"
        updated = f"{existing}{separator}{block}"

    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(updated)
        temporary_path.chmod(0o600)
        os.replace(temporary_path, config_path)
        config_path.chmod(0o600)
    except OSError as exc:
        raise ConfigError(f"could not update {config_path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml",
        help="Codex user config path (default: $CODEX_HOME/config.toml)",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="HTTPS MemPalace MCP endpoint")
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        required=True,
        help="read the bearer token from standard input without echoing it",
    )
    args = parser.parse_args(argv)
    try:
        update_config(args.config.expanduser(), sys.stdin.read(), args.url)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Configured static MemPalace MCP authentication in {args.config.expanduser()}")
    print("The token was not displayed; protect this plaintext config with mode 0600.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

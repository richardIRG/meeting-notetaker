"""Small shared helpers: logging, subprocess calls, tool lookup, and naming."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import TextIO

# launchd starts jobs with a minimal PATH, so look in the usual Homebrew
# locations too.
EXTRA_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


class Log:
    """Timestamped lines to stdout, and to a per-session file once attached."""

    def __init__(self) -> None:
        self._handle: TextIO | None = None

    def attach(self, path: Path) -> None:
        self.detach()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a", encoding="utf-8")
        except OSError:
            self._handle = None

    def detach(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
        self._handle = None

    def __call__(self, message: str) -> None:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        line = f"{stamp} {message}"
        print(line, flush=True)
        if self._handle is not None:
            try:
                self._handle.write(line + "\n")
                self._handle.flush()
            except OSError:
                pass


log = Log()


def run_safe(args: list[str], timeout: int = 30, label: str = "") -> tuple[int, str, str]:
    """Run a command and never raise. Returns (returncode, stdout, stderr)."""
    tag = label or (args[0] if args else "cmd")
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT {tag} after {timeout}s")
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "not found"
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR {tag}: {exc!r}")
        return 1, "", str(exc)


def resolve_tool(name_or_path: str) -> str:
    """Absolute path to a tool, or "" when it cannot be found."""
    if not name_or_path:
        return ""
    candidate = Path(name_or_path).expanduser()
    if candidate.is_absolute():
        return str(candidate) if os.access(candidate, os.X_OK) else ""
    found = shutil.which(name_or_path)
    if found:
        return found
    for directory in EXTRA_TOOL_DIRS:
        path = Path(directory) / name_or_path
        if os.access(path, os.X_OK):
            return str(path)
    return ""


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:limit].strip("-") or "meeting"


def redact_url(url: str) -> str:
    """Drop the query string and fragment, which can carry passcodes and tokens."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    hidden = parsed.query or parsed.fragment
    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return f"{base}?<redacted>" if hidden else base


def detect_platform(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host == "zoom.us" or host.endswith(".zoom.us") or host.endswith("zoomgov.com"):
        return "zoom"
    if host == "teams.microsoft.com" or host.endswith(".teams.microsoft.com"):
        return "teams"
    return ""


def friendly_time(value: str) -> str:
    """'2026-01-15T09:00:00-08:00' -> '2026-01-15 09:00'; anything else unchanged."""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return value

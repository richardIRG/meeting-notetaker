"""Optional one-shot scheduling with launchd (macOS LaunchAgents).

Nothing here runs unless you call `notetaker schedule`. Each scheduled
meeting becomes one LaunchAgent that fires a few minutes before the start.

Two details that matter:

- launchd calendar triggers have no year, so a job would fire again on the
  same date next year. Each job passes `--only-on <date>` to `record`, which
  exits without recording on any other date. Clean up spent jobs with
  `notetaker unschedule --expired`.
- /bin/bash stays the job's root process on purpose. macOS attributes the
  Microphone permission (needed by ffmpeg to read BlackHole) to the job's
  root process, and /bin/bash is Apple-signed at a stable path, so the grant
  survives Python and Homebrew upgrades. The command runs as `bash -c
  '"$0" "$@"; exit $?' ...`, which keeps bash as the parent instead of
  letting it exec into Python.
"""

from __future__ import annotations

import os
import plistlib
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from notetaker.config import Config
from notetaker.util import run_safe, slugify

LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()
JOB_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LABEL_TIME_RE = re.compile(r"\.(\d{8}-\d{4})-")
PASS_THROUGH_ENV = ("NOTETAKER_ENV_FILE", "NOTETAKER_OUTPUT_DIR")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def build_job(
    config: Config, url: str, title: str, at: datetime, record_args: list[str]
) -> tuple[str, dict[str, Any]]:
    fire = at - timedelta(minutes=config.schedule_lead_minutes)
    label = f"{config.schedule_label_prefix}.{at:%Y%m%d-%H%M}-{slugify(title, 32)}"
    command = [sys.executable, "-m", "notetaker"]
    if config.config_path:
        command += ["--config", str(config.config_path)]
    command += ["record", url, "--title", title, "--only-on", fire.date().isoformat(), *record_args]
    env = {"PATH": JOB_PATH, "PYTHONUNBUFFERED": "1"}
    for key in PASS_THROUGH_ENV:
        if os.environ.get(key):
            env[key] = os.environ[key]
    log_dir = config.schedule_log_dir
    job = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", "-c", '"$0" "$@"; exit $?', *command],
        "WorkingDirectory": str(Path(__file__).resolve().parent.parent),
        "StartCalendarInterval": {
            "Month": fire.month, "Day": fire.day, "Hour": fire.hour, "Minute": fire.minute,
        },
        "ProcessType": "Interactive",
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / f"{label}.out.log"),
        "StandardErrorPath": str(log_dir / f"{label}.err.log"),
        "EnvironmentVariables": env,
    }
    return label, job


def render(job: dict[str, Any]) -> str:
    return plistlib.dumps(job).decode()


def install(config: Config, label: str, job: dict[str, Any], load: bool = True) -> Path:
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    config.schedule_log_dir.mkdir(parents=True, exist_ok=True)
    path = LAUNCH_AGENTS / f"{label}.plist"
    path.write_bytes(plistlib.dumps(job))
    os.chmod(path, 0o600)  # the plist holds the meeting link
    if load:
        rc, _, err = run_safe(["/bin/launchctl", "bootstrap", _domain(), str(path)], 20, "launchctl")
        if rc != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {err.strip()}")
    return path


def _meeting_time(label: str) -> datetime | None:
    match = LABEL_TIME_RE.search(label)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d-%H%M")
    except ValueError:
        return None


def jobs(config: Config) -> list[dict[str, Any]]:
    found = []
    for path in sorted(LAUNCH_AGENTS.glob(f"{config.schedule_label_prefix}.*.plist")):
        label = path.stem
        rc, _, _ = run_safe(["/bin/launchctl", "print", f"{_domain()}/{label}"], 10, "launchctl")
        found.append({
            "label": label,
            "path": path,
            "meeting_time": _meeting_time(label),
            "loaded": rc == 0,
        })
    return found


def remove(config: Config, label: str) -> bool:
    path = LAUNCH_AGENTS / f"{label}.plist"
    run_safe(["/bin/launchctl", "bootout", f"{_domain()}/{label}"], 20, "launchctl")
    if path.is_file():
        path.unlink()
        return True
    return False


def expired(config: Config, older_than_hours: int = 12) -> list[str]:
    cutoff = datetime.now() - timedelta(hours=older_than_hours)
    return [
        job["label"] for job in jobs(config)
        if job["meeting_time"] is not None and job["meeting_time"] < cutoff
    ]

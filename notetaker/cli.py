"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import platform as platform_mod
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

from notetaker import __version__, audio, schedule, session, teams, zoom
from notetaker.browser import Chrome
from notetaker.config import Config, ConfigError
from notetaker.util import detect_platform, log, redact_url, resolve_tool, run_safe

DESCRIPTION = "Joins Zoom and Teams calls on its own, records them, and writes the notes."

EPILOG = """examples:
  notetaker doctor
  notetaker record "https://zoom.us/j/12345678901?pwd=..." --title "Weekly sync" --dry-run
  notetaker record "https://teams.microsoft.com/l/meetup-join/..." --title "Planning" --max-minutes 60
  notetaker transcribe ~/Meetings/2026-01-15-weekly-sync
  notetaker schedule "https://zoom.us/j/12345678901" --at "2026-01-15 09:00" --title "Weekly sync"
"""


# --- doctor -------------------------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def line(self, status: str, name: str, detail: str = "") -> None:
        if status == "FAIL":
            self.failures += 1
        elif status == "WARN":
            self.warnings += 1
        print(f"  [{status:<4}] {name}" + (f": {detail}" if detail else ""))


def _http_status(url: str, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, ""
    except urllib.error.HTTPError as exc:
        body = exc.read(400).decode(errors="replace")
        exc.close()
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return 0, type(exc).__name__


def run_checks(config: Config, report: Report, online: bool = False, capture_test: bool = False) -> None:
    print("System")
    if sys.platform == "darwin":
        report.line("OK", "macOS", platform_mod.mac_ver()[0])
    else:
        report.line("FAIL", "macOS", f"this tool needs macOS (found {sys.platform})")
    report.line("OK", "Python", platform_mod.python_version())
    try:
        from importlib.metadata import version

        report.line("OK", "playwright", version("playwright"))
    except Exception:  # noqa: BLE001
        report.line("FAIL", "playwright", "not installed (pip install -r requirements.txt)")
    for tool in ("/usr/bin/curl", "/usr/bin/caffeinate", "/usr/bin/osascript"):
        report.line("OK" if os.access(tool, os.X_OK) else "FAIL", Path(tool).name)

    print("Configuration")
    if config.config_path:
        report.line("OK", "config file", str(config.config_path))
    else:
        report.line("INFO", "config file", "none found; using built-in defaults")
    for warning in config.warnings:
        report.line("WARN", "config", warning)
    if config.env_path.is_file():
        mode = config.env_path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            report.line("WARN", "env file", f"{config.env_path} is readable by other users (chmod 600 it)")
        else:
            report.line("OK", "env file", str(config.env_path))
    else:
        report.line("INFO", "env file", f"{config.env_path} not found; using the process environment only")

    print("Audio")
    ffmpeg = resolve_tool(config.ffmpeg)
    report.line("OK" if ffmpeg else "FAIL", "ffmpeg", ffmpeg or "not found (brew install ffmpeg)")
    sas = resolve_tool(config.switch_audio_source)
    report.line("OK" if sas else "FAIL", "SwitchAudioSource", sas or "not found (brew install switchaudio-osx)")
    if sas:
        router = audio.AudioRouter(sas)
        outputs = router.devices("output")
        if config.audio_device in outputs:
            report.line("OK", "loopback output device", config.audio_device)
        else:
            report.line("FAIL", "loopback output device",
                        f"{config.audio_device!r} not found (brew install blackhole-2ch, then reboot)")
        current = router.current("output")
        if current == config.audio_device and not config.fallback_output_device:
            report.line("WARN", "current output",
                        f"already {current!r}; after a recording the output stays there. "
                        "Set audio.fallback_output_device if you want your speakers back")
        else:
            report.line("INFO", "current output", f"{current!r} (restored after each recording)")
    if ffmpeg:
        inputs = audio.capture_devices(ffmpeg)
        if config.audio_device in inputs:
            report.line("OK", "capture device", f"ffmpeg sees {config.audio_device!r}")
        else:
            report.line("FAIL", "capture device", f"ffmpeg does not list {config.audio_device!r} (found: {inputs})")
    if capture_test and ffmpeg:
        _capture_test(config, ffmpeg, report)

    print("Chrome")
    chrome_ok = os.access(config.chrome_path, os.X_OK)
    report.line("OK" if chrome_ok else "FAIL", "Chrome", config.chrome_path if chrome_ok else f"not found at {config.chrome_path}")
    if config.chrome_profile_dir.is_dir():
        report.line("OK", "recorder profile", str(config.chrome_profile_dir))
    else:
        report.line("INFO", "recorder profile", f"{config.chrome_profile_dir} (created on first run)")
    chrome = Chrome(config.chrome_path, config.chrome_profile_dir, config.cdp_port)
    if chrome.alive():
        ok, detail = chrome.owns_port()
        report.line("OK" if ok else "FAIL", f"debugging port {config.cdp_port}", detail)
    else:
        report.line("OK", f"debugging port {config.cdp_port}", "free")

    print("Services")
    if config.elevenlabs_api_key:
        report.line("OK", "ELEVENLABS_API_KEY", "set")
    else:
        report.line("FAIL", "ELEVENLABS_API_KEY", "not set")
    if config.notes_enabled:
        report.line("OK" if config.openai_api_key else "FAIL", "OPENAI_API_KEY",
                    "set" if config.openai_api_key else "not set (or set notes.enabled = false)")
        report.line("INFO", "notes model", config.notes_model)
    else:
        report.line("INFO", "notes", "disabled")
    if config.telegram_enabled:
        missing = [k for k, v in (("TELEGRAM_BOT_TOKEN", config.telegram_bot_token),
                                  ("TELEGRAM_CHAT_ID", config.telegram_chat_id)) if not v]
        report.line("WARN" if missing else "OK", "Telegram", f"missing {', '.join(missing)}" if missing else "enabled")
    else:
        report.line("INFO", "Telegram", "disabled")
    if online:
        _online_checks(config, report)

    print("Output")
    for label, directory in (("output folder", config.output_dir), ("notes copy folder", config.notes_copy_dir)):
        if directory is None:
            continue
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory):
                pass
            report.line("OK", label, str(directory))
        except OSError as exc:
            report.line("FAIL", label, f"{directory} is not writable ({exc.strerror})")


def _online_checks(config: Config, report: Report) -> None:
    if config.elevenlabs_api_key:
        code, body = _http_status("https://api.elevenlabs.io/v1/user", {"xi-api-key": config.elevenlabs_api_key})
        if code == 200 or (code == 401 and "missing_permissions" in body):
            report.line("OK", "ElevenLabs key", "accepted")
        else:
            report.line("FAIL", "ElevenLabs key", f"rejected (HTTP {code})")
    if config.notes_enabled and config.openai_api_key:
        code, _ = _http_status(
            f"{config.openai_base_url}/models/{config.notes_model}",
            {"Authorization": f"Bearer {config.openai_api_key}"},
        )
        if code == 200:
            report.line("OK", "OpenAI key and model", f"{config.notes_model} available")
        elif code == 404:
            report.line("FAIL", "OpenAI key and model", f"{config.notes_model} not available to this key")
        else:
            report.line("FAIL", "OpenAI key and model", f"HTTP {code}")
    if config.telegram_enabled and config.telegram_bot_token:
        code, _ = _http_status(f"https://api.telegram.org/bot{config.telegram_bot_token}/getMe", {})
        report.line("OK" if code == 200 else "FAIL", "Telegram bot token", f"HTTP {code}")


def _capture_test(config: Config, ffmpeg: str, report: Report) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "test.mp3"
        rc, _, err = run_safe(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
             "-i", f":{config.audio_device}", "-t", "3", "-y", str(out)],
            30, "capture-test",
        )
        if rc == 0 and out.is_file() and out.stat().st_size > 0:
            peak = audio.max_volume_db(ffmpeg, out)
            report.line("OK", "3-second capture", f"{out.stat().st_size} bytes, peak {peak} dB "
                        "(silence is expected unless something is playing)")
        else:
            report.line("FAIL", "3-second capture",
                        f"ffmpeg could not record ({err.strip()[:160] or rc}); check Microphone permission "
                        "for your terminal in System Settings > Privacy & Security")


def cmd_doctor(config: Config, args: argparse.Namespace) -> int:
    print(f"meeting-notetaker {__version__} doctor")
    report = Report()
    run_checks(config, report, online=args.online, capture_test=args.capture_test)
    print(f"\n{report.failures} problem(s), {report.warnings} warning(s)")
    return 1 if report.failures else 0


# --- record ---------------------------------------------------------------------------
def _resolve_platform(url: str, forced: str | None) -> str:
    found = forced or detect_platform(url)
    if found not in session.PLATFORMS:
        raise SystemExit(
            "error: could not tell the platform from the link; "
            "use a zoom.us or teams.microsoft.com link, or pass --platform"
        )
    return found


def cmd_record(config: Config, args: argparse.Namespace) -> int:
    if args.output_dir:
        config.output_dir = Path(args.output_dir).expanduser()
    platform = _resolve_platform(args.url, args.platform)
    title = args.title or f"{platform} meeting"
    name = args.name or config.display_name
    max_minutes = args.max_minutes or config.max_minutes
    join_window = args.join_window_minutes or config.join_window_minutes
    with_notes = config.notes_enabled and not args.no_notes

    if args.only_on and args.only_on != date.today().isoformat():
        print(f"scheduled for {args.only_on}, today is {date.today().isoformat()}; not recording")
        return 0
    if platform == "teams" and any(ch in name for ch in "()"):
        print("warning: Teams rejects parentheses in guest names", file=sys.stderr)

    flow = zoom if platform == "zoom" else teams
    if args.dry_run:
        print("Dry run: nothing will be joined, recorded, or switched.\n")
        print(f"  platform          {platform}")
        print(f"  link              {redact_url(args.url)}")
        print(f"  browser opens     {redact_url(flow.join_url(args.url))}")
        print(f"  title             {title}")
        print(f"  display name      {name}")
        print(f"  join window       {join_window} min")
        print(f"  meeting cap       {max_minutes} min (capture hard cap {join_window + max_minutes + 2} min)")
        print(f"  output folder     {config.output_dir}")
        print(f"  audio device      {config.audio_device}")
        print(f"  notes             {'on (' + config.notes_model + ')' if with_notes else 'off'}")
        print()
        report = Report()
        run_checks(config, report)
        print(f"\n{report.failures} problem(s), {report.warnings} warning(s)")
        return 1 if report.failures else 0

    blockers = _preflight(config, with_notes)
    if blockers:
        for item in blockers:
            print(f"error: {item}", file=sys.stderr)
        print("Run `python -m notetaker doctor` for details.", file=sys.stderr)
        return 2
    return session.record(config, args.url, title, platform, name, max_minutes, join_window, with_notes)


def _preflight(config: Config, with_notes: bool) -> list[str]:
    problems = []
    if sys.platform != "darwin":
        problems.append("macOS is required")
    if not resolve_tool(config.ffmpeg):
        problems.append("ffmpeg not found")
    sas = resolve_tool(config.switch_audio_source)
    if not sas:
        problems.append("SwitchAudioSource not found")
    elif config.audio_device not in audio.AudioRouter(sas).devices("output"):
        problems.append(f"audio device {config.audio_device!r} not found")
    if not os.access(config.chrome_path, os.X_OK):
        problems.append(f"Chrome not found at {config.chrome_path}")
    try:
        import playwright  # noqa: F401
    except ImportError:
        problems.append("playwright is not installed")
    if not config.elevenlabs_api_key:
        problems.append("ELEVENLABS_API_KEY is not set")
    if with_notes and not config.openai_api_key:
        problems.append("OPENAI_API_KEY is not set (or pass --no-notes)")
    return problems


# --- transcribe / notes ------------------------------------------------------------------
def _session_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_dir():
        raise SystemExit(f"error: {path} is not a folder")
    return path


def cmd_transcribe(config: Config, args: argparse.Namespace) -> int:
    directory = _session_dir(args.session_dir)
    log.attach(directory / "run.log")
    outcome = session.process(config, directory, with_notes=config.notes_enabled and not args.no_notes)
    log.detach()
    print(json.dumps(outcome))
    return 0 if outcome.get("transcript") == "ok" else 1


def cmd_notes(config: Config, args: argparse.Namespace) -> int:
    directory = _session_dir(args.session_dir)
    log.attach(directory / "run.log")
    outcome = session.make_notes(config, directory)
    log.detach()
    print(json.dumps(outcome))
    return 0 if outcome.get("notes") == "ok" else 1


# --- schedule ------------------------------------------------------------------------------
def cmd_schedule(config: Config, args: argparse.Namespace) -> int:
    if args.list:
        found = schedule.jobs(config)
        if not found:
            print("no scheduled recordings")
        for job in found:
            when = job["meeting_time"].strftime("%Y-%m-%d %H:%M") if job["meeting_time"] else "?"
            print(f"{when}  {'loaded ' if job['loaded'] else 'unloaded'}  {job['label']}")
        return 0
    if not args.url or not args.at:
        raise SystemExit("error: schedule needs a meeting link and --at, or --list")
    try:
        at = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
    except ValueError:
        raise SystemExit('error: --at must look like "2026-01-15 09:00" (local time)') from None
    if at <= datetime.now():
        raise SystemExit("error: --at is in the past")
    platform = _resolve_platform(args.url, args.platform)
    title = args.title or f"{platform} meeting"
    record_args = ["--platform", platform]
    for flag, value in (("--name", args.name), ("--max-minutes", args.max_minutes),
                        ("--join-window-minutes", args.join_window_minutes)):
        if value:
            record_args += [flag, str(value)]
    if args.no_notes:
        record_args.append("--no-notes")
    label, job = schedule.build_job(config, args.url, title, at, record_args)
    if args.dry_run:
        print(schedule.render(job), end="")
        return 0
    path = schedule.install(config, label, job, load=not args.no_load)
    fire = at.replace(second=0)
    print(f"scheduled {label}")
    print(f"  plist  {path}")
    print(f"  fires  {config.schedule_lead_minutes} min before {fire:%Y-%m-%d %H:%M}")
    print(f"  logs   {config.schedule_log_dir}")
    if args.no_load:
        print(f"  load it with: launchctl bootstrap gui/{os.getuid()} {path}")
    return 0


def cmd_unschedule(config: Config, args: argparse.Namespace) -> int:
    labels = schedule.expired(config) if args.expired else list(args.labels)
    if not labels:
        print("nothing to remove")
        return 0
    for label in labels:
        removed = schedule.remove(config, label)
        print(f"{'removed' if removed else 'not found'}: {label}")
    return 0


# --- parser --------------------------------------------------------------------------------
def _record_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title", help="meeting title, used for the folder name and notes")
    parser.add_argument("--name", help="display name shown in the meeting (default from config)")
    parser.add_argument("--max-minutes", type=int, help="leave after this many minutes in the call")
    parser.add_argument("--join-window-minutes", type=int,
                        help="how long to keep trying to get in (raise it when starting early)")
    parser.add_argument("--platform", choices=sorted(session.PLATFORMS), help="override link detection")
    parser.add_argument("--no-notes", action="store_true", help="transcribe only; skip the notes step")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notetaker", description=DESCRIPTION, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="path to config.toml (default ~/.config/meeting-notetaker/config.toml)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("record", help="join a meeting now, record it, and write notes")
    p.add_argument("url", help="Zoom or Teams join link")
    _record_options(p)
    p.add_argument("--output-dir", help="folder for this recording (default from config)")
    p.add_argument("--only-on", metavar="YYYY-MM-DD", help="exit without recording unless today is this date")
    p.add_argument("--dry-run", action="store_true", help="show the plan and check prerequisites; join nothing")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("transcribe", help="transcribe (or re-transcribe) a recording folder, then write notes")
    p.add_argument("session_dir")
    p.add_argument("--no-notes", action="store_true")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("notes", help="write (or rewrite) notes from a folder's existing transcript")
    p.add_argument("session_dir")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("doctor", help="check prerequisites without joining anything")
    p.add_argument("--online", action="store_true", help="also verify the API keys with each service")
    p.add_argument("--capture-test", action="store_true",
                   help="record 3 seconds from the loopback device to confirm Microphone permission")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("schedule", help="schedule a future recording with launchd")
    p.add_argument("url", nargs="?", help="Zoom or Teams join link")
    p.add_argument("--at", metavar='"YYYY-MM-DD HH:MM"', help="meeting start, local time")
    _record_options(p)
    p.add_argument("--list", action="store_true", help="list scheduled recordings")
    p.add_argument("--no-load", action="store_true", help="write the LaunchAgent but do not load it")
    p.add_argument("--dry-run", action="store_true", help="print the LaunchAgent plist and stop")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("unschedule", help="remove scheduled recordings")
    p.add_argument("labels", nargs="*", help="labels shown by `schedule --list`")
    p.add_argument("--expired", action="store_true", help="remove every job whose meeting is over")
    p.set_defaults(func=cmd_unschedule)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return args.func(config, args)

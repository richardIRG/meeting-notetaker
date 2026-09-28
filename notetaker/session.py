"""One recording from start to finish, and the post-processing that follows.

Order of operations for `record`:
  1. Take a lock so a duplicate launch of the same meeting exits quietly
  2. Remember the current audio devices, switch output and input to
     BlackHole, raise the BlackHole output volume
  3. Start ffmpeg with a hard duration cap (join window + meeting cap)
  4. Start or reuse the dedicated Chrome, join the meeting, join computer
     audio, mute, and watch for the end
  5. Always: stop ffmpeg, restore the audio devices, close the tab
  6. Transcribe and write notes (skipped when the recorder never got in)
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any

from notetaker import __version__, audio, notes, teams, transcribe, zoom
from notetaker.browser import Chrome
from notetaker.config import Config
from notetaker.notify import Notifier
from notetaker.util import log, redact_url, resolve_tool, slugify

PLATFORMS = {"zoom": zoom, "teams": teams}
MIN_AUDIO_BYTES = 50_000
CAFFEINATE = "/usr/bin/caffeinate"


class SessionError(RuntimeError):
    pass


# --- Locking --------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(lock_path: Path) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                pid = int(lock_path.read_text().strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                return False
            lock_path.unlink(missing_ok=True)  # stale lock from a crashed run
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        return True
    return False


# --- Paths ----------------------------------------------------------------------
def new_session_dir(output_dir: Path, day: str, slug: str) -> Path:
    base = output_dir / f"{day}-{slug}"
    candidate, n = base, 2
    while candidate.exists() and any(candidate.iterdir()):
        candidate = output_dir / f"{day}-{slug}-{n}"
        n += 1
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _duration(meta: dict[str, Any]) -> str:
    try:
        start = datetime.fromisoformat(meta.get("joined_at") or meta["started_at"])
        end = datetime.fromisoformat(meta["ended_at"])
    except (KeyError, TypeError, ValueError):
        return ""
    minutes = max(0, round((end - start).total_seconds() / 60))
    return f"{minutes} min"


# --- Post-processing --------------------------------------------------------------
def make_notes(config: Config, session_dir: Path) -> dict[str, Any]:
    """Write notes.json and notes.md from an existing transcript.json."""
    meta = _read_json(session_dir / "meta.json")
    result = _read_json(session_dir / "transcript.json")
    if not result:
        return {"notes": "skipped", "reason": "no transcript.json"}
    turns = transcribe.speaker_turns(result)
    text = transcribe.plain_transcript(turns)
    if len(text.split()) < 12:
        return {"notes": "skipped", "reason": "transcript too short"}
    started = str(meta.get("started_at") or "")
    context = {
        "title": meta.get("title") or session_dir.name,
        "platform": meta.get("platform") or "",
        "meeting_date": started[:10] or date.today().isoformat(),
        "started_at": started,
        "length": _duration(meta),
    }
    try:
        raw, provenance = notes.request_notes(
            text, context,
            api_key=config.openai_api_key,
            model=config.notes_model,
            base_url=config.openai_base_url,
            reasoning_effort=config.reasoning_effort,
            max_completion_tokens=config.max_completion_tokens,
        )
        # Verify evidence against the words alone, so a quote that crosses a
        # turn boundary is not failed by the inserted timestamp and label.
        note = notes.validate_note(raw, " ".join(turn["text"] for turn in turns))
    except notes.NotesError as exc:
        log(f"ERROR writing notes: {exc}")
        return {"notes": "failed", "reason": str(exc)}
    _write_json(session_dir / "notes.json", {
        "schema_version": notes.NOTE_SCHEMA_VERSION,
        "prompt_version": notes.PROMPT_VERSION,
        "model": config.notes_model,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "usage": provenance.get("usage", {}),
        "note": note,
    })
    render_meta = dict(meta, duration=_duration(meta))
    markdown = notes.render_markdown(note, render_meta)
    (session_dir / "notes.md").write_text(markdown, encoding="utf-8")
    log(f"notes written: {session_dir / 'notes.md'}")
    if config.notes_copy_dir:
        try:
            config.notes_copy_dir.mkdir(parents=True, exist_ok=True)
            copy_path = config.notes_copy_dir / f"{session_dir.name}.md"
            shutil.copyfile(session_dir / "notes.md", copy_path)
            log(f"notes copied to {copy_path}")
        except OSError as exc:
            log(f"WARN copying notes: {exc!r}")
    return {"notes": "ok"}


def process(config: Config, session_dir: Path, with_notes: bool = True) -> dict[str, Any]:
    """Transcribe capture.mp3, then write notes. Safe to re-run."""
    outcome: dict[str, Any] = {"transcript": "skipped", "notes": "skipped"}
    capture = session_dir / "capture.mp3"
    size = capture.stat().st_size if capture.is_file() else 0
    if size < MIN_AUDIO_BYTES:
        log(f"no usable audio in {capture} ({size} bytes)")
        outcome["reason"] = "no usable audio"
        return outcome
    ffmpeg = resolve_tool(config.ffmpeg)
    peak = audio.max_volume_db(ffmpeg, capture) if ffmpeg else None
    if peak is not None:
        log(f"capture peak level: {peak:.1f} dB")
        if peak <= config.silence_threshold_db:
            log("capture is digital silence; skipping transcription. Check that the BlackHole "
                "output volume is not zero and that the meeting audio was joined.")
            outcome["reason"] = "silent capture"
            return outcome
    meta = _read_json(session_dir / "meta.json")
    try:
        result = transcribe.transcribe(
            capture, config.elevenlabs_api_key, config.transcription_model,
            config.keyterms, config.transcription_timeout,
        )
    except transcribe.EmptyTranscript:
        log("the transcript came back empty (a muted room with nobody talking does this)")
        outcome.update(transcript="empty")
        return outcome
    except transcribe.TranscriptionError as exc:
        log(f"ERROR transcribing: {exc}")
        outcome.update(transcript="failed", reason=str(exc))
        return outcome
    _write_json(session_dir / "transcript.json", result)
    turns = transcribe.speaker_turns(result)
    (session_dir / "transcript.md").write_text(transcribe.render_markdown(turns, meta), encoding="utf-8")
    log(f"transcript written: {session_dir / 'transcript.md'} ({len(str(result.get('text', '')))} chars)")
    outcome["transcript"] = "ok"
    if not config.keep_audio:
        capture.unlink(missing_ok=True)
        log("audio deleted (keep_audio = false)")
    if with_notes and config.notes_enabled:
        outcome.update(make_notes(config, session_dir))
    return outcome


# --- Recording ----------------------------------------------------------------------
def _raise_exit(signum, _frame):  # noqa: ANN001
    raise SystemExit(128 + signum)


class _Resources:
    capture = None
    playwright = None
    page = None


def record(
    config: Config,
    url: str,
    title: str,
    platform: str,
    display_name: str,
    max_minutes: int,
    join_window_minutes: int,
    with_notes: bool,
) -> int:
    notifier = Notifier(config)
    started = datetime.now().astimezone()
    day = started.strftime("%Y-%m-%d")
    slug = slugify(title)

    lock_path = config.state_dir / "locks" / f"{day}-{slug}.lock"
    if not acquire_lock(lock_path):
        log(f"another run for '{title}' today is still active; exiting")
        return 0

    session_dir = new_session_dir(config.output_dir, day, slug)
    log.attach(session_dir / "run.log")
    log(f"meeting-notetaker {__version__}: recording '{title}' ({platform}) into {session_dir}")

    router = audio.AudioRouter(resolve_tool(config.switch_audio_source))
    prior_output = router.current("output")
    if config.fallback_output_device and prior_output in ("", config.audio_device):
        prior_output = config.fallback_output_device
    prior_input = router.current("input")
    log(f"current audio: output={prior_output!r} input={prior_input!r}")

    meta: dict[str, Any] = {
        "title": title,
        "platform": platform,
        "url": redact_url(url),
        "display_name": display_name,
        "started_at": started.isoformat(timespec="seconds"),
        "join_state": "not_started",
        "end_reason": "",
        "max_minutes": max_minutes,
        "tool_version": __version__,
    }
    res = _Resources()
    chrome = Chrome(config.chrome_path, config.chrome_profile_dir, config.cdp_port)
    previous_handlers = {
        sig: signal.signal(sig, _raise_exit) for sig in (signal.SIGTERM, signal.SIGHUP)
    }
    rc = 1
    try:
        rc = _join_and_watch(
            config, url, title, platform, display_name, max_minutes, join_window_minutes,
            session_dir, router, chrome, notifier, meta, res,
        )
    except KeyboardInterrupt:
        meta["end_reason"] = "interrupted"
        log("interrupted; stopping the recording")
        rc = 0
    except Exception as exc:  # noqa: BLE001
        meta["end_reason"] = f"error: {type(exc).__name__}"
        log(f"ERROR during the meeting: {exc!r}")
        log(traceback.format_exc().rstrip())
    finally:
        # Runs on every exit path, including SIGTERM from launchd.
        log("cleanup: stopping capture, restoring audio, closing the tab")
        audio.stop_capture(res.capture)
        if prior_output:
            router.switch("output", prior_output)
        if prior_input:
            router.switch("input", prior_input)
        if res.page is not None:
            try:
                res.page.close()
            except Exception:  # noqa: BLE001
                pass
        if res.playwright is not None:
            try:
                res.playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        if config.close_chrome:
            chrome.close_if_launched()
        meta["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            _write_json(session_dir / "meta.json", meta)
        except OSError as exc:
            log(f"WARN writing meta.json: {exc!r}")
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        lock_path.unlink(missing_ok=True)
    # Not reached when a SIGTERM is propagating: the audio is kept and can be
    # processed later with `transcribe`.
    _finish(config, session_dir, meta, notifier, with_notes)
    return rc


def _join_and_watch(
    config: Config, url: str, title: str, platform: str, display_name: str,
    max_minutes: int, join_window_minutes: int, session_dir: Path,
    router: audio.AudioRouter, chrome: Chrome, notifier: Notifier,
    meta: dict[str, Any], res: _Resources,
) -> int:
    flow = PLATFORMS[platform]
    join_window_s = join_window_minutes * 60
    try:
        subprocess.Popen(
            [CAFFEINATE, "-dimsu", "-t", str(join_window_s + max_minutes * 60 + 600)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as exc:
        log(f"WARN: caffeinate failed: {exc!r}")

    router.switch("output", config.audio_device)
    router.switch("input", config.audio_device)
    audio.set_output_volume(config.capture_volume)

    # Capture starts before the join so the opening minutes are never lost.
    # The hard cap covers the join window, the meeting cap, and a margin.
    cap_seconds = join_window_s + max_minutes * 60 + 120
    res.capture = audio.start_capture(
        resolve_tool(config.ffmpeg), config.audio_device, session_dir / "capture.mp3",
        cap_seconds, session_dir / "ffmpeg.err.log",
    )
    if res.capture is None:
        meta["join_state"] = "capture_failed"
        notifier.send(f"Notetaker could not start the audio capture for '{title}'. See {session_dir / 'run.log'}")
        return 1

    if not chrome.ensure():
        meta["join_state"] = "failed"
        notifier.send(f"Notetaker could not start Chrome for '{title}'.")
        return 1

    from playwright.sync_api import sync_playwright

    res.playwright = sync_playwright().start()
    browser = res.playwright.chromium.connect_over_cdp(chrome.cdp_url)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    try:
        # Pre-grant so Chrome's permission dialog never blocks the Join button.
        context.grant_permissions(["microphone", "camera"])
    except Exception as exc:  # noqa: BLE001
        log(f"WARN: grant_permissions failed: {exc!r}")
    res.page = context.new_page()
    flow.prepare(res.page)

    def admission_alert(where: str) -> None:
        notifier.send(
            f"Notetaker is still waiting in {where} for '{title}'. "
            f"If you are in the meeting, admit \"{display_name}\"."
        )

    state = flow.join(
        res.page, url, display_name, join_window_s,
        on_admission_wait=admission_alert,
        admission_alert_s=config.lobby_alert_minutes * 60,
    )
    meta["join_state"] = state
    if state == "failed":
        notifier.send(f"Notetaker could not get into '{title}' within the join window.")
        return 1
    meta["joined_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    flow.join_audio(res.page)
    flow.ensure_muted(res.page)
    meta["end_reason"] = flow.watch(res.page, max_minutes * 60)
    return 0


def _finish(
    config: Config, session_dir: Path, meta: dict[str, Any], notifier: Notifier, with_notes: bool,
) -> None:
    state = meta.get("join_state")
    if state in ("failed", "capture_failed", "not_started"):
        log("the recorder never got into the meeting; skipping transcription. "
            f"To transcribe anyway: python -m notetaker transcribe {session_dir}")
        log.detach()
        return
    try:
        outcome = process(config, session_dir, with_notes=with_notes)
    except KeyboardInterrupt:
        log(f"post-processing interrupted; resume with: python -m notetaker transcribe {session_dir}")
        log.detach()
        return
    summary = (
        f"Notetaker finished '{meta.get('title')}': joined={state}, "
        f"transcript={outcome.get('transcript')}, notes={outcome.get('notes')}"
        + (" (stopped early)" if meta.get("end_reason") == "interrupted" else "")
        + f". Files: {session_dir}"
    )
    log(summary)
    notifier.send(summary)
    log.detach()

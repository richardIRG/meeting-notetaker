"""Speech to text with ElevenLabs Scribe (speaker diarization, word timestamps)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from notetaker import __version__
from notetaker.util import friendly_time

API_URL = "https://api.elevenlabs.io/v1/speech-to-text"
CURL = "/usr/bin/curl"
MAX_TURN_SECONDS = 60


class TranscriptionError(RuntimeError):
    pass


class EmptyTranscript(TranscriptionError):
    """The service returned no speech. A muted room with only the recorder in it does this."""


def transcribe(
    audio_path: Path, api_key: str, model: str, keyterms: list[str], timeout: int
) -> dict[str, Any]:
    if not api_key:
        raise TranscriptionError("ELEVENLABS_API_KEY is not set")
    # The key goes to curl on stdin (--header @-), so it never appears in the
    # process list.
    cmd = [
        CURL, "--silent", "--show-error", "--fail-with-body",
        "--connect-timeout", "30", "--max-time", str(timeout),
        "--request", "POST",
        "--url", API_URL,
        "--header", "@-",
        "--header", "Accept: application/json",
        "--header", f"User-Agent: meeting-notetaker/{__version__}",
        "--form", f"file=@{audio_path};type=audio/mpeg",
        "--form", f"model_id={model}",
        "--form", "diarize=true",
        "--form", "timestamps_granularity=word",
    ]
    for term in keyterms:
        cmd.extend(("--form-string", f"keyterms={term}"))
    try:
        proc = subprocess.run(
            cmd,
            input=f"xi-api-key: {api_key}\n".encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TranscriptionError(f"transcription timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = (proc.stdout or proc.stderr).decode(errors="replace")[:300]
        raise TranscriptionError(f"ElevenLabs request failed (curl exit {proc.returncode}): {detail}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TranscriptionError("ElevenLabs returned something other than JSON") from exc
    if not str(result.get("text") or "").strip():
        raise EmptyTranscript("ElevenLabs returned an empty transcript")
    return result


def _speaker_label(raw: Any) -> str:
    match = re.search(r"(\d+)$", str(raw or ""))
    return f"Speaker {int(match.group(1)) + 1}" if match else "Speaker ?"


def speaker_turns(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Group word-level output into turns: a new turn on each speaker change,
    and every MAX_TURN_SECONDS within a long monologue so timestamps stay useful."""
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for word in result.get("words") or []:
        if not isinstance(word, dict) or word.get("type") == "spacing":
            continue
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        speaker = _speaker_label(word.get("speaker_id"))
        start = float(word.get("start") or 0.0)
        if (current is None or speaker != current["speaker"]
                or start - current["start"] > MAX_TURN_SECONDS):
            current = {"start": start, "speaker": speaker, "words": []}
            turns.append(current)
        current["words"].append(text)
    for turn in turns:
        turn["text"] = " ".join(turn.pop("words"))
    if not turns and str(result.get("text") or "").strip():
        turns.append({"start": 0.0, "speaker": "Speaker ?", "text": str(result["text"]).strip()})
    return turns


def clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def plain_transcript(turns: list[dict[str, Any]]) -> str:
    """One line per turn, the form sent to the notes model."""
    return "\n".join(f"[{clock(t['start'])}] {t['speaker']}: {t['text']}" for t in turns)


def render_markdown(turns: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = [
        f"# {meta.get('title') or 'Meeting'}: transcript",
        "",
        f"- Recorded: {friendly_time(str(meta.get('started_at') or '?'))}",
        f"- Platform: {str(meta.get('platform') or '?').title()}",
        "- Speakers are numbered by automatic diarization, not identified by name",
        "",
    ]
    for turn in turns:
        lines.append(f"**[{clock(turn['start'])}] {turn['speaker']}:** {turn['text']}")
        lines.append("")
    return "\n".join(lines)

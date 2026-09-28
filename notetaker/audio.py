"""System audio routing (SwitchAudioSource) and capture (ffmpeg on BlackHole).

How the capture works: the Mac's default output is switched to the BlackHole
loopback device, so everything Chrome plays (the meeting) lands on
BlackHole's input side, and ffmpeg records that input to an mp3. The default
input is also switched to BlackHole so the browser never opens a real
microphone. The previous devices are restored when the recording ends.
"""

from __future__ import annotations

import re
import signal
import subprocess
import time
from pathlib import Path

from notetaker.util import log, run_safe


class AudioRouter:
    def __init__(self, switch_audio_source: str) -> None:
        self.tool = switch_audio_source

    def current(self, kind: str) -> str:
        rc, out, _ = run_safe([self.tool, "-c", "-t", kind], 10, f"get-{kind}")
        return out.strip() if rc == 0 else ""

    def devices(self, kind: str) -> list[str]:
        rc, out, _ = run_safe([self.tool, "-a", "-t", kind], 10, f"list-{kind}")
        if rc != 0:
            return []
        names = []
        for line in out.splitlines():
            # Older SwitchAudioSource builds append " (output)" / " (input)".
            name = re.sub(r"\s+\((input|output)\)$", "", line.strip())
            if name:
                names.append(name)
        return names

    def switch(self, kind: str, name: str) -> bool:
        if not name:
            return False
        rc, _, _ = run_safe([self.tool, "-t", kind, "-s", name], 10, f"set-{kind}")
        ok = rc == 0
        log(f"set {kind} -> {name}: {'ok' if ok else 'FAILED'}")
        return ok


def set_output_volume(level: int) -> None:
    """Raise the volume of the CURRENT default output device.

    BlackHole is a loopback: audio appears on its input at the level it was
    played to its output. If the BlackHole output volume is 0, the capture is
    digital silence (around -91 dB). Call this after switching the output to
    BlackHole so it targets the right device. Restoring the previous output
    device brings back that device's own volume.
    """
    rc, _, _ = run_safe(
        ["/usr/bin/osascript", "-e", f"set volume output volume {int(level)}"],
        10,
        "set-output-volume",
    )
    log(f"set output volume -> {level}: {'ok' if rc == 0 else 'FAILED'}")


def capture_devices(ffmpeg: str) -> list[str]:
    """Audio input devices as ffmpeg's avfoundation layer sees them."""
    _, _, err = run_safe(
        [ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        20,
        "ffmpeg-list-devices",
    )
    names: list[str] = []
    in_audio = False
    for line in err.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        match = re.search(r"\]\s*\[\d+\]\s*(.+)$", line)
        if in_audio and match:
            names.append(match.group(1).strip())
    return names


def start_capture(
    ffmpeg: str, device: str, out_path: Path, max_seconds: int, err_log: Path
) -> subprocess.Popen | None:
    """Start recording `device` to an mp3. ffmpeg's -t is the hard duration cap."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation", "-i", f":{device}",
        "-ac", "2", "-codec:a", "libmp3lame", "-q:a", "2",
        "-t", str(int(max_seconds)),
        "-y", str(out_path),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err_log.open("ab"),
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR starting ffmpeg: {exc!r}")
        return None
    time.sleep(3)
    if proc.poll() is not None:
        log(
            f"ERROR: ffmpeg exited immediately (rc={proc.returncode}). The usual cause is "
            "missing Microphone permission for the process that launched it. "
            f"See {err_log}"
        )
        return None
    log(f"capture started (ffmpeg pid {proc.pid}, cap {max_seconds // 60} min) -> {out_path.name}")
    return proc


def stop_capture(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)  # lets ffmpeg finalize the mp3
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log("capture stopped")
    except Exception as exc:  # noqa: BLE001
        log(f"WARN stopping ffmpeg: {exc!r}")


def max_volume_db(ffmpeg: str, path: Path) -> float | None:
    """Peak level of an audio file in dB, or None when it cannot be measured."""
    _, _, err = run_safe(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        600,
        "ffmpeg-volumedetect",
    )
    match = re.search(r"max_volume:\s*(-?[\d.]+|-inf)\s*dB", err)
    if not match:
        return None
    value = match.group(1)
    return float("-inf") if value == "-inf" else float(value)

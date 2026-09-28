"""Settings come from a TOML file; secrets come from environment variables.

Lookup order for the config file: --config, then $NOTETAKER_CONFIG, then
~/.config/meeting-notetaker/config.toml. A missing default file is fine (the
built-in defaults apply).

Secrets (API keys, Telegram token) are read from the environment. For
unattended runs, put them in an env file (default
~/.config/meeting-notetaker/.env, or $NOTETAKER_ENV_FILE). Variables already
set in the environment win over the file.
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("~/.config/meeting-notetaker/config.toml")

DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {
        "output_dir": "~/Meetings",
        "display_name": "Notetaker",
        "max_minutes": 90,
        "join_window_minutes": 7,
        "lobby_alert_minutes": 5,
        "keep_audio": True,
        "notes_copy_dir": "",
        "state_dir": "~/.local/state/meeting-notetaker",
        "env_file": "~/.config/meeting-notetaker/.env",
    },
    "audio": {
        "device": "BlackHole 2ch",
        "fallback_output_device": "",
        "capture_volume": 90,
        "silence_threshold_db": -80.0,
    },
    "chrome": {
        "path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "profile_dir": "~/.local/share/meeting-notetaker/chrome-profile",
        "cdp_port": 9250,
        "close_after": True,
    },
    "tools": {
        "ffmpeg": "ffmpeg",
        "switch_audio_source": "SwitchAudioSource",
    },
    "transcription": {
        "model": "scribe_v2",
        "keyterms": [],
        "timeout_seconds": 1800,
    },
    "notes": {
        "enabled": True,
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "max_completion_tokens": 16384,
        "base_url": "https://api.openai.com/v1",
    },
    "telegram": {
        "enabled": False,
    },
    "schedule": {
        "label_prefix": "local.meeting-notetaker",
        "lead_minutes": 3,
        "log_dir": "~/Library/Logs/meeting-notetaker",
    },
}


class ConfigError(RuntimeError):
    pass


def load_env_file(path: Path) -> list[str]:
    """Load KEY=VALUE lines into os.environ without overriding existing values."""
    loaded: list[str] = []
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _merge(base: dict[str, Any], override: dict[str, Any], warnings: list[str], prefix: str = "") -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        name = f"{prefix}{key}"
        if key not in base:
            warnings.append(f"unknown config key: {name}")
            continue
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                warnings.append(f"config key {name} should be a table")
                continue
            merged[key] = _merge(base[key], value, warnings, prefix=f"{name}.")
        else:
            merged[key] = value
    return merged


def _path(value: Any) -> Path:
    return Path(str(value)).expanduser()


@dataclass
class Config:
    config_path: Path | None
    env_path: Path
    output_dir: Path
    display_name: str
    max_minutes: int
    join_window_minutes: int
    lobby_alert_minutes: int
    keep_audio: bool
    notes_copy_dir: Path | None
    state_dir: Path
    audio_device: str
    fallback_output_device: str
    capture_volume: int
    silence_threshold_db: float
    chrome_path: str
    chrome_profile_dir: Path
    cdp_port: int
    close_chrome: bool
    ffmpeg: str
    switch_audio_source: str
    transcription_model: str
    keyterms: list[str]
    transcription_timeout: int
    notes_enabled: bool
    notes_model: str
    reasoning_effort: str
    max_completion_tokens: int
    openai_base_url: str
    telegram_enabled: bool
    schedule_label_prefix: str
    schedule_lead_minutes: int
    schedule_log_dir: Path
    warnings: list[str] = field(default_factory=list)

    @property
    def elevenlabs_api_key(self) -> str:
        return os.environ.get("ELEVENLABS_API_KEY", "").strip()

    @property
    def openai_api_key(self) -> str:
        return os.environ.get("OPENAI_API_KEY", "").strip()

    @property
    def telegram_bot_token(self) -> str:
        return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    @property
    def telegram_chat_id(self) -> str:
        return os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    @classmethod
    def load(cls, config_path: str | None = None) -> "Config":
        explicit = config_path or os.environ.get("NOTETAKER_CONFIG")
        path = _path(explicit) if explicit else _path(DEFAULT_CONFIG_PATH)
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                with path.open("rb") as handle:
                    data = tomllib.load(handle)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"could not parse {path}: {exc}") from exc
        elif explicit:
            raise ConfigError(f"config file not found: {path}")
        warnings: list[str] = []
        merged = _merge(DEFAULTS, data, warnings)
        g, a, c, t = merged["general"], merged["audio"], merged["chrome"], merged["tools"]
        tr, n, s = merged["transcription"], merged["notes"], merged["schedule"]

        env_path = _path(os.environ.get("NOTETAKER_ENV_FILE") or g["env_file"])
        load_env_file(env_path)

        keyterms = tr["keyterms"]
        if not isinstance(keyterms, list):
            warnings.append("transcription.keyterms should be a list of strings")
            keyterms = []
        copy_dir = str(g["notes_copy_dir"] or "").strip()
        try:
            return cls(
                config_path=path if path.is_file() else None,
                env_path=env_path,
                output_dir=_path(os.environ.get("NOTETAKER_OUTPUT_DIR") or g["output_dir"]),
                display_name=str(g["display_name"]),
                max_minutes=int(g["max_minutes"]),
                join_window_minutes=int(g["join_window_minutes"]),
                lobby_alert_minutes=int(g["lobby_alert_minutes"]),
                keep_audio=bool(g["keep_audio"]),
                notes_copy_dir=_path(copy_dir) if copy_dir else None,
                state_dir=_path(g["state_dir"]),
                audio_device=str(a["device"]),
                fallback_output_device=str(a["fallback_output_device"]),
                capture_volume=int(a["capture_volume"]),
                silence_threshold_db=float(a["silence_threshold_db"]),
                chrome_path=str(_path(c["path"])),
                chrome_profile_dir=_path(c["profile_dir"]),
                cdp_port=int(c["cdp_port"]),
                close_chrome=bool(c["close_after"]),
                ffmpeg=str(t["ffmpeg"]),
                switch_audio_source=str(t["switch_audio_source"]),
                transcription_model=str(tr["model"]),
                keyterms=[str(item) for item in keyterms if str(item).strip()],
                transcription_timeout=int(tr["timeout_seconds"]),
                notes_enabled=bool(n["enabled"]),
                notes_model=str(n["model"]),
                reasoning_effort=str(n["reasoning_effort"] or ""),
                max_completion_tokens=int(n["max_completion_tokens"]),
                openai_base_url=str(n["base_url"]).rstrip("/"),
                telegram_enabled=bool(merged["telegram"]["enabled"]),
                schedule_label_prefix=str(s["label_prefix"]),
                schedule_lead_minutes=int(s["lead_minutes"]),
                schedule_log_dir=_path(s["log_dir"]),
                warnings=warnings,
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid value in {path}: {exc}") from exc

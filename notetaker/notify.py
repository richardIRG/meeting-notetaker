"""Optional Telegram notifications. Off unless [telegram] enabled = true and
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from notetaker.config import Config
from notetaker.util import log


class Notifier:
    def __init__(self, config: Config) -> None:
        self.token = config.telegram_bot_token
        self.chat_id = config.telegram_chat_id
        self.enabled = bool(config.telegram_enabled and self.token and self.chat_id)
        if config.telegram_enabled and not self.enabled:
            log("WARN: Telegram is enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")

    def send(self, text: str) -> bool:
        """Best effort; never raises."""
        if not self.enabled:
            return False
        try:
            data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/sendMessage", data=data
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                ok = bool(json.loads(response.read()).get("ok"))
            log(f"telegram notification {'sent' if ok else 'rejected'}")
            return ok
        except Exception as exc:  # noqa: BLE001
            log(f"telegram notification failed: {type(exc).__name__}")
            return False

"""A dedicated Chrome instance driven over the DevTools protocol (CDP).

The recorder runs its own Chrome with its own profile directory and a remote
debugging port, and Playwright connects to it. Recent Chrome versions refuse
remote debugging on the default profile, so a separate profile is required.
It also keeps cookies (consent banners, Zoom and Teams preferences) between
runs without touching your everyday browser.
"""

from __future__ import annotations

import subprocess
import time
import urllib.request
from pathlib import Path

from notetaker.util import log, run_safe


class Chrome:
    def __init__(self, path: str, profile_dir: Path, port: int) -> None:
        self.path = path
        self.profile_dir = profile_dir
        self.port = port
        self.launched: subprocess.Popen | None = None

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.cdp_url}/json/version", timeout=3) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    def owns_port(self) -> tuple[bool, str]:
        """Check that whatever answers on the port is Chrome with OUR profile.

        Without this, a different Chrome (for example another automation on
        the same Mac) that happens to use the port would be driven instead.
        """
        rc, out, _ = run_safe(["/bin/ps", "-axww", "-o", "command="], 10, "ps")
        if rc != 0:
            return False, "could not list processes"
        port_flag = f"--remote-debugging-port={self.port}"
        owners = [line for line in out.splitlines() if port_flag in line]
        if not owners:
            return False, f"port {self.port} is answering but not from a Chrome started with {port_flag}"
        profile_flag = f"--user-data-dir={self.profile_dir}"
        if any(profile_flag in line for line in owners):
            return True, "dedicated Chrome is running"
        return False, f"port {self.port} belongs to a Chrome with a different profile"

    def ensure(self) -> bool:
        if self.alive():
            ok, detail = self.owns_port()
            log(detail)
            return ok
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.launched = subprocess.Popen(
                [
                    self.path,
                    f"--user-data-dir={self.profile_dir}",
                    f"--remote-debugging-port={self.port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--hide-crash-restore-bubble",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR launching Chrome: {exc!r}")
            return False
        for _ in range(30):
            if self.alive():
                log("dedicated Chrome launched")
                return True
            time.sleep(1)
        log("ERROR: Chrome's debugging endpoint never came up")
        return False

    def close_if_launched(self) -> None:
        if self.launched is None or self.launched.poll() is not None:
            return
        try:
            self.launched.terminate()
            self.launched.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.launched.kill()
        except Exception as exc:  # noqa: BLE001
            log(f"WARN closing Chrome: {exc!r}")
            return
        log("closed the dedicated Chrome")


def click_if_visible(page, locator, label: str, timeout: int = 2000) -> bool:
    try:
        loc = locator.first
        if loc.is_visible(timeout=timeout):
            loc.click(timeout=3000)
            log(f"clicked: {label}")
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def body_text(page) -> str:
    try:
        return page.evaluate("document.body ? document.body.innerText : ''") or ""
    except Exception:  # noqa: BLE001
        return ""

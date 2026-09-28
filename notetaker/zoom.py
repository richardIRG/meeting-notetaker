"""Zoom web-client flow.

Lessons from real runs that this code depends on:

- Go straight to https://<host>.zoom.us/wc/join/<id>?... The /j/ landing
  page nags you to open the desktop app (zoommtg:// prompts), and after that
  every Join click bounces back to the preview. Registration tokens (tk=) and
  passcodes (pwd=) ride along in the query string.
- For registration-gated meetings and webinars, use the personal join link
  from the registration confirmation email (it has tk=). The bare /j/ link
  lands on the registration form and the recorder would sit there recording
  silence.
- Chrome's page-embedded permission dialog eats synthetic clicks and blocks
  Join, so microphone and camera are pre-granted over CDP before joining.
- Never interact with the page after clicking Join. The handshake takes
  several seconds and any click (including the preview Mute toggle) silently
  cancels it. Click Join, then only poll; re-engage only if the preview form
  provably comes back.
- Mute in the call, not in the preview. The recorder's microphone is
  BlackHole, which carries the meeting's own audio, so an unmuted bot would
  echo the meeting back into itself.
- In-call detection: the Leave button exists in the DOM. The footer
  auto-hides, so visibility checks are unreliable.
- Registered and tokened flows can skip the name field entirely, so the Join
  click is never gated on having filled a name.
- Webinar attendees have no Mute control; that warning is expected.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Callable

from notetaker.browser import body_text, click_if_visible
from notetaker.util import log

POLL = 15

WAITING_MARKERS = (
    "waiting for the host to start",
    "waiting for host to start the webinar",
    "wait for the host to start this webinar",
    "wait for the host to start this meeting",
    "host will let you in soon",
    "meeting has not started",
)
# Waiting states that need a person to admit the recorder (a waiting room),
# as opposed to waiting for the meeting to begin.
ADMISSION_MARKERS = ("host will let you in soon",)
END_RE = re.compile(
    r"(meeting|webinar) (has been|is|was) ended|webinar has ended|meeting has ended", re.I
)
LEAVE_SELECTOR = 'button[aria-label*="Leave" i], button:has-text("Leave")'


def join_url(url: str) -> str:
    """Turn a /j/<id> or /w/<id> link into the browser web-client URL."""
    parsed = urllib.parse.urlparse(url)
    match = re.search(r"/[jw]/(\d+)", parsed.path)
    if not match:
        return url
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{parsed.netloc}/wc/join/{match.group(1)}{query}"


def prepare(page) -> None:
    """Nothing beyond the context-wide mic/camera grant done by the session."""


def in_call(page) -> bool:
    try:
        return page.locator(LEAVE_SELECTOR).count() > 0
    except Exception:  # noqa: BLE001
        return False


def join(
    page,
    url: str,
    display_name: str,
    join_window_s: int,
    on_admission_wait: Callable[[str], None] | None = None,
    admission_alert_s: int = 300,
) -> str:
    """Returns "in_call" or "failed"."""
    target = join_url(url)
    page.goto(target, wait_until="domcontentloaded", timeout=60000)
    log("opened the Zoom web client")
    try:
        page.wait_for_timeout(5000)  # let the client's scripts initialize
    except Exception:  # noqa: BLE001
        pass
    deadline = time.monotonic() + join_window_s
    name_filled = False
    join_clicked = -1e9  # monotonic time of the last Join click
    waiting_logged = ""
    admission_since: float | None = None
    alerted = False
    while time.monotonic() < deadline:
        if in_call(page):
            log("in call (Leave control present)")
            return "in_call"
        body = body_text(page).lower()
        for marker in WAITING_MARKERS:
            if marker in body and waiting_logged != marker:
                log(f"waiting state: {marker}")
                waiting_logged = marker
        if any(marker in body for marker in ADMISSION_MARKERS):
            admission_since = admission_since or time.monotonic()
            if (on_admission_wait and not alerted
                    and time.monotonic() - admission_since >= admission_alert_s):
                on_admission_wait("the Zoom waiting room")
                alerted = True
        if join_clicked > 0:
            # Quiet phase: only poll. Any click here cancels the handshake.
            if waiting_logged:
                time.sleep(3)
                continue
            bounced = False
            if time.monotonic() - join_clicked > 25:
                try:
                    bounced = page.get_by_role(
                        "button", name=re.compile("^Join$", re.I)
                    ).first.is_visible(timeout=1500)
                except Exception:  # noqa: BLE001
                    bounced = False
            if bounced:
                log("bounced back to the preview; trying again")
                join_clicked = -1e9
                name_filled = False
            time.sleep(3)
            continue
        click_if_visible(page, page.locator("#onetrust-accept-btn-handler"), "cookie accept")
        click_if_visible(
            page, page.get_by_role("button", name=re.compile("^I Agree$", re.I)), "terms I Agree"
        )
        # Fallback only: permissions are pre-granted, so this dialog should not
        # appear. Its footer control dismisses it.
        click_if_visible(
            page,
            page.locator(".pepc-permission-dialog__footer-button"),
            "permission dialog: continue without mic/cam",
        )
        if not name_filled:
            try:
                box = page.locator('#input-for-name, input[placeholder*="Name" i]').first
                if box.is_visible(timeout=2000):
                    box.fill(display_name)
                    name_filled = True
                    log(f"name filled: {display_name}")
            except Exception:  # noqa: BLE001
                pass
        try:
            if page.locator("#input-for-pwd").first.is_visible(timeout=1000):
                log("WARN: Zoom is asking for a passcode; use a join link that includes pwd=")
        except Exception:  # noqa: BLE001
            pass
        # Do not click the preview Mute toggle: it breaks the join handshake.
        if click_if_visible(
            page, page.get_by_role("button", name=re.compile("^Join$", re.I)), "Join", timeout=3000
        ):
            join_clicked = time.monotonic()
        time.sleep(3)
    log("WARN: never reached the meeting within the join window")
    return "failed"


def join_audio(page, window_s: int = 120) -> bool:
    """Without computer audio the web client receives no meeting sound."""
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        if click_if_visible(
            page,
            page.get_by_role(
                "button",
                name=re.compile("join audio by computer|join with computer audio|^join audio$", re.I),
            ),
            "Join Audio (computer)",
            timeout=3000,
        ):
            return True
        try:
            if page.locator('button[aria-label*="mute" i]').count() > 0:
                log("audio already joined (mute control present)")
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    log("WARN: the audio join dialog never appeared; the capture may be silent")
    return False


def ensure_muted(page) -> None:
    """A toolbar button named 'Mute' means the mic is live; 'Unmute' means muted."""
    try:
        if page.get_by_role("button", name=re.compile("^Unmute", re.I)).count() > 0:
            log("already muted in call")
            return
        if click_if_visible(
            page, page.get_by_role("button", name=re.compile("^Mute", re.I)), "in-call Mute", timeout=3000
        ):
            return
        log("WARN: could not confirm mute state (normal for webinar attendees)")
    except Exception as exc:  # noqa: BLE001
        log(f"WARN ensure_muted: {exc!r}")


def watch(page, max_seconds: int) -> str:
    """Block until the meeting ends or the cap is reached. Returns the reason."""
    deadline = time.monotonic() + max_seconds
    misses = 0
    while time.monotonic() < deadline:
        try:
            body = page.evaluate("document.body.innerText") or ""
            if END_RE.search(body):
                log("meeting ended by host")
                return "ended_by_host"
            present = in_call(page)
        except Exception:  # noqa: BLE001
            log("page gone; treating the meeting as ended")
            return "page_closed"
        misses = 0 if present else misses + 1
        if misses >= 2:
            log("Leave control gone twice; meeting ended")
            return "left_meeting"
        time.sleep(POLL)
    log("duration cap reached; leaving")
    try:
        page.locator(LEAVE_SELECTOR).first.click(timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    return "cap_reached"

"""Microsoft Teams web-client flow (joins as an anonymous guest).

The path through Teams in the browser:

  meetup-join link -> launcher page ("Continue on this browser") ->
  teams.microsoft.com/v2/?meetingjoin=true#/l/meetup-join/... pre-join
  screen (name box, mic and camera toggles, "Join now") -> lobby for guests
  ("Someone will let you in shortly") -> in call (#hangup-button with
  #roster-button or #chat-button).

Lessons from real runs that this code depends on:

- Pick a neutral guest name. Teams flags an unverified guest whose name
  contains a signed-in participant's name as a suspected threat, and it
  rejects parentheses in guest names.
- Turning the mic and camera off pre-join raises an "Are you sure you don't
  want audio or video?" overlay. Ignore it and click Join with a JavaScript
  click; that joins with the (BlackHole) mic attached and muted.
- Never click Teams' "Continue without audio or video" or Chrome's "continue
  without" permission control. Both persist a microphone BLOCK for the
  origin. Permissions are granted over CDP instead, which overrides a
  persisted block for the session.
- The direct v2 URL sometimes stalls on the Teams splash logo. After 45
  seconds without a pre-join form, fall back to the original launcher link.
- The organizer leaving does not end the meeting for a guest; the screen
  says "Waiting for others to join". Four polls of that text ends the run.
- Do not drive the same tab from two scripts at once.
- If Teams never shows the in-call toolbar but the pre-join form and lobby
  text are both gone for 60 seconds, the join is treated as "assumed". An
  organizer who never admits the guest (common in externally hosted
  webinars) brings the pre-join form back, which ends the run.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Callable

from notetaker.browser import body_text, click_if_visible
from notetaker.util import log

POLL = 15
TEAMS_ORIGIN = "https://teams.microsoft.com"

LOBBY_MARKERS = (
    "let you in shortly",
    "should let you in soon",
    "waiting for the meeting to start",
    "we'll let people know you're waiting",
    "you're in the lobby",
    "when the meeting starts",
)
# Lobby states that need a person to admit the recorder.
ADMISSION_MARKERS = (
    "let you in shortly",
    "should let you in soon",
    "we'll let people know you're waiting",
    "you're in the lobby",
)
END_RE = re.compile(
    r"you('ve| have)? left the meeting|the meeting has ended|meeting ended|"
    r"call ended|you were removed|rejoin",
    re.I,
)
ALONE_MARKER = "waiting for others to join"
JOIN_BUTTON = '#prejoin-join-button, button[data-tid="prejoin-join-button"]'


def join_url(url: str) -> str:
    """Skip the launcher page by building the v2 pre-join URL directly."""
    parsed = urllib.parse.urlparse(url)
    match = re.search(r"/l/meetup-join/(.*)$", parsed.path)
    if not match:
        return url
    rest = urllib.parse.unquote(match.group(1))
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{TEAMS_ORIGIN}/v2/?meetingjoin=true#/l/meetup-join/{rest}{query}"


def prepare(page) -> None:
    """Grant capture permissions to the Teams origin over CDP.

    This overrides any persisted BLOCK content setting for the session. The
    grant resets when Playwright disconnects, so it must happen in the same
    session as the join.
    """
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Browser.grantPermissions", {
            "origin": TEAMS_ORIGIN,
            "permissions": ["audioCapture", "videoCapture"],
        })
        log("granted audio and video capture for the Teams origin")
    except Exception as exc:  # noqa: BLE001
        log(f"WARN CDP permission grant: {exc!r}")


def in_call(page) -> bool:
    body = body_text(page).lower()
    if any(marker in body for marker in LOBBY_MARKERS):
        return False
    try:
        if page.locator("#prejoin-join-button").count() > 0:
            return False
        toolbar = page.locator("#roster-button, #chat-button").count()
        leave = page.locator("#hangup-button").count()
        return toolbar > 0 and leave > 0
    except Exception:  # noqa: BLE001
        return False


def _prejoin_devices_off(page) -> None:
    """Turn the pre-join mic and camera switches off.

    The switches are input[type=checkbox] role=switch elements without an
    aria-label, identified by data-tid ("toggle-mute", checked = mic ON).
    Teams tolerates toggling these pre-join (unlike Zoom's preview Mute).
    """
    for tid, label in (("toggle-mute", "mic"), ("toggle-video", "camera")):
        try:
            switch = page.locator(f'[data-tid="{tid}"]').first
            if switch.count() == 0:
                log(f"pre-join: {label} switch not found")
                continue
            if not switch.evaluate("e => e.checked"):
                log(f"pre-join: {label} already off")
                continue
            switch_id = switch.get_attribute("id") or ""
            try:
                page.locator(f'label[for="{switch_id}"]').first.click(timeout=3000, force=True)
            except Exception:  # noqa: BLE001
                switch.evaluate("e => e.click()")
            time.sleep(1)
            if switch.evaluate("e => e.checked"):
                switch.evaluate("e => e.click()")
                time.sleep(1)
            log(f"pre-join: {label} off (checked now {switch.evaluate('e => e.checked')})")
        except Exception as exc:  # noqa: BLE001
            log(f"WARN pre-join {label} switch: {exc!r}")


def join(
    page,
    url: str,
    display_name: str,
    join_window_s: int,
    on_admission_wait: Callable[[str], None] | None = None,
    admission_alert_s: int = 300,
) -> str:
    """Returns "in_call", "assumed", or "failed"."""
    target = join_url(url)
    page.goto(target, wait_until="domcontentloaded", timeout=60000)
    log("opened the Teams web client")
    deadline = time.monotonic() + join_window_s
    name_filled = False
    join_clicked = -1e9
    waiting_logged = ""
    opened_at = time.monotonic()
    fell_back = False
    admission_since: float | None = None
    alerted = False
    while time.monotonic() < deadline:
        if (not fell_back and join_clicked < 0
                and time.monotonic() - opened_at > 45
                and page.locator("#prejoin-join-button").count() == 0
                and page.locator('input[data-tid="prejoin-display-name-input"]').count() == 0):
            fell_back = True
            log("no pre-join form after 45s; falling back to the launcher link")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(4)
        if in_call(page):
            log("in call (toolbar and Leave present, no lobby text)")
            return "in_call"
        low = body_text(page).lower()
        for marker in LOBBY_MARKERS:
            if marker in low and waiting_logged != marker:
                log(f"lobby state: {marker}")
                waiting_logged = marker
        if any(marker in low for marker in ADMISSION_MARKERS):
            admission_since = admission_since or time.monotonic()
            if (on_admission_wait and not alerted
                    and time.monotonic() - admission_since >= admission_alert_s):
                on_admission_wait("the Teams lobby")
                alerted = True
        if join_clicked > 0:
            # After Join: only poll. A lobby can last many minutes.
            elapsed = time.monotonic() - join_clicked
            try:
                form_back = page.locator("#prejoin-join-button").count() > 0
            except Exception:  # noqa: BLE001
                form_back = False
            lobby_now = any(marker in low for marker in LOBBY_MARKERS)
            if form_back and not lobby_now and elapsed > 30:
                log("pre-join form returned; trying again")
                join_clicked = -1e9
            elif not form_back and not lobby_now and elapsed > 60:
                log("assumed in call (no form and no lobby text for 60s; toolbar not detected)")
                return "assumed"
            time.sleep(3)
            continue
        try:
            link = page.locator("text=Continue on this browser").first
            if link.is_visible(timeout=1000):
                link.click(timeout=5000)
                log("clicked: Continue on this browser")
                time.sleep(4)
                continue
        except Exception:  # noqa: BLE001
            pass
        click_if_visible(page, page.locator("#onetrust-accept-btn-handler"), "cookie accept")
        if not name_filled:
            try:
                box = page.locator(
                    'input[placeholder*="name" i], input[data-tid="prejoin-display-name-input"]'
                ).first
                if box.is_visible(timeout=2000):
                    box.fill(display_name)
                    name_filled = True
                    log(f"name filled: {display_name}")
                elif box.count() == 0 and page.locator("#prejoin-join-button").count() > 0:
                    name_filled = True  # signed-in profile: no name box
                    log("no name box (signed-in profile)")
            except Exception:  # noqa: BLE001
                pass
        try:
            join_button = page.locator(JOIN_BUTTON).first
            if join_button.is_visible(timeout=2000) and name_filled:
                _prejoin_devices_off(page)
                if join_button.is_enabled():
                    join_button.evaluate("e => e.click()")
                    join_clicked = time.monotonic()
                    log("clicked: Join now")
                else:
                    log("WARN: Join now is disabled (was the name rejected?)")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    log("WARN: never reached the meeting within the join window")
    return "failed"


def join_audio(page, window_s: int = 0) -> bool:
    """Computer audio is chosen on the pre-join screen; just confirm the mic control."""
    try:
        if page.locator('button[aria-label*="mic" i], #microphone-button').count() > 0:
            log("audio: mic control present in call")
            return True
    except Exception:  # noqa: BLE001
        pass
    log("WARN: no mic control found in call")
    return False


def ensure_muted(page) -> None:
    """The mic button reads 'Mute mic' when live and 'Unmute mic' when muted."""
    try:
        mic = page.locator("#mic-button").first
        label = (mic.get_attribute("aria-label") or "") if mic.count() else ""
        if label.lower().startswith("unmute") or "no audio devices" in label.lower():
            log(f"already muted in call ({label})")
            return
        button = page.locator('button[aria-label^="Mute" i], #mic-button').first
        if button.is_visible(timeout=3000):
            button.click(timeout=3000)
            log("clicked: in-call Mute")
            return
        log("WARN: could not confirm mute state in call")
    except Exception as exc:  # noqa: BLE001
        log(f"WARN ensure_muted: {exc!r}")


def watch(page, max_seconds: int) -> str:
    """Block until the meeting ends or the cap is reached. Returns the reason.

    Ends on end-screen text, on the pre-join form coming back (dropped or never
    admitted), on "waiting for others to join" for 4 polls, or, once the
    toolbar has been seen, on the toolbar disappearing for 4 polls. If the
    toolbar was never detected, the run continues to the cap.
    """
    deadline = time.monotonic() + max_seconds
    misses = 0
    alone = 0
    seen_toolbar = False
    while time.monotonic() < deadline:
        try:
            body = page.evaluate("document.body.innerText") or ""
            if END_RE.search(body):
                log("meeting ended (end screen text)")
                return "ended_by_host"
            if page.locator("#prejoin-join-button").count() > 0:
                log("meeting ended (pre-join form is back)")
                return "dropped_to_prejoin"
            if ALONE_MARKER in body.lower():
                alone += 1
                if alone >= 4:
                    log("meeting ended (alone: 'waiting for others to join' for 4 polls)")
                    return "alone"
            else:
                alone = 0
            if in_call(page):
                seen_toolbar = True
                misses = 0
            elif seen_toolbar:
                misses += 1
                if misses >= 4:
                    log("meeting ended (toolbar gone for 4 polls)")
                    return "left_meeting"
        except Exception as exc:  # noqa: BLE001
            log(f"WARN watch: {exc!r}")
            misses += 1
            if misses >= 8:
                return "page_closed"
        time.sleep(POLL)
    log("duration cap reached; leaving")
    try:
        page.locator('button[aria-label*="Leave" i], #hangup-button').first.click(timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    return "cap_reached"

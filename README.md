# Meeting notetaker

Joins Zoom and Teams calls on its own, records them, and writes the notes.

Give it a meeting link. It opens the meeting in its own Chrome window, joins as a muted guest, records what the meeting sounds like, and leaves when the meeting ends or a time cap is reached. Then it sends the audio to ElevenLabs for a transcript with speaker labels and asks an OpenAI model for structured notes: a summary, discussion topics, decisions, action items with owners and dates, risks, and open questions. Everything lands as Markdown in a folder you choose.

It runs on a Mac. A spare Mac mini that sits on the network works well, since the recorder takes over the Mac's audio output while it runs.

## How it works

1. The Mac's audio output is switched to [BlackHole](https://github.com/ExistentialAudio/BlackHole), a virtual loopback device, so the meeting audio Chrome plays can be recorded
2. ffmpeg starts recording BlackHole to an mp3 with a hard duration cap
3. A dedicated Chrome instance, driven with Playwright, opens the Zoom or Teams web client, enters a display name, joins, turns on computer audio, and mutes itself
4. The recorder watches the page for the end of the meeting: an end screen, the Leave button disappearing, being left alone in a Teams call, or the time cap
5. It stops recording and puts your audio devices back the way they were
6. ElevenLabs Scribe transcribes the audio with speaker diarization
7. An OpenAI model writes the notes against a strict JSON schema. Every action item has to quote the transcript word for word; items whose quote cannot be found are marked low confidence.

Nothing is installed in Zoom or Teams, and no meeting bot service is involved. The recorder is a guest in a browser tab.

## Requirements

- macOS (tested on Apple silicon)
- Google Chrome
- Python 3.11 or newer
- [BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole): `brew install blackhole-2ch`, then restart the Mac
- ffmpeg: `brew install ffmpeg`
- SwitchAudioSource: `brew install switchaudio-osx`
- An [ElevenLabs](https://elevenlabs.io) API key with speech-to-text access
- An [OpenAI](https://platform.openai.com) API key, if you want notes

Both services charge per use. A one-hour meeting costs a transcription hour and one notes request.

## Setup

Clone this repository, then from its folder:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

Playwright's own browser download is not needed; the recorder drives your installed Chrome.

Add your keys:

```sh
mkdir -p ~/.config/meeting-notetaker
cp .env.example ~/.config/meeting-notetaker/.env
chmod 600 ~/.config/meeting-notetaker/.env
# edit the file and fill in ELEVENLABS_API_KEY and OPENAI_API_KEY
```

Optionally copy `config.example.toml` to `~/.config/meeting-notetaker/config.toml` and change what you need. Every setting has a default, and the example file documents each one.

Check the setup:

```sh
.venv/bin/notetaker doctor
.venv/bin/notetaker doctor --online --capture-test
```

`doctor` checks the tools, the BlackHole device, Chrome, the debugging port, the keys, and the output folder. It joins nothing and changes no settings. `--online` confirms the API keys with each service. `--capture-test` records three seconds from BlackHole, which also triggers the macOS Microphone permission prompt for your terminal if it has not been granted yet. Approve it.

## Record a meeting

```sh
.venv/bin/notetaker record "https://zoom.us/j/12345678901?pwd=..." --title "Weekly sync"
```

The platform is detected from the link. Useful options:

- `--max-minutes 60`: leave after an hour in the call (default 90)
- `--join-window-minutes 20`: keep trying to get in for 20 minutes, for when you start early and the recorder waits for the host
- `--name "Notetaker"`: the name other participants see
- `--no-notes`: transcript only
- `--dry-run`: print what would happen and run the prerequisite checks, without joining or switching audio

Stop a recording early with Ctrl-C. The audio captured so far is still transcribed.

While a recording runs, the Mac's sound goes to BlackHole, so you will not hear the meeting on that Mac's speakers.

### What you get

Each recording gets a folder, for example `~/Meetings/2026-01-15-weekly-sync/`:

| File | Contents |
|---|---|
| `notes.md` | Summary, discussion, decisions, action items, risks, and open questions |
| `transcript.md` | Timestamped transcript with numbered speakers |
| `capture.mp3` | The recording (delete it automatically with `keep_audio = false`) |
| `notes.json`, `transcript.json` | Structured versions of both |
| `meta.json` | Title, platform, join result, start and end times, and why the recording ended |
| `run.log` | Everything the recorder did, for troubleshooting |

Speakers are labeled Speaker 1, Speaker 2, and so on. The notes use a person's name only when the conversation makes clear who is talking.

To also drop each `notes.md` into another folder (a notes app, a synced drive), set `notes_copy_dir` in the config.

### Redo a step

```sh
.venv/bin/notetaker transcribe ~/Meetings/2026-01-15-weekly-sync   # transcript, then notes
.venv/bin/notetaker notes ~/Meetings/2026-01-15-weekly-sync        # notes only
```

Use these after a network failure, or after changing the notes model.

## Scheduling

To record a meeting you will not be at, schedule it:

```sh
.venv/bin/notetaker schedule "https://zoom.us/j/12345678901?pwd=..." \
  --at "2026-01-15 09:00" --title "Weekly sync" --max-minutes 60
```

This writes a one-shot macOS LaunchAgent to `~/Library/LaunchAgents/` and loads it. It fires three minutes before the meeting (`lead_minutes` in the config) and runs `record` with the options you gave. Logs go to `~/Library/Logs/meeting-notetaker/`.

```sh
.venv/bin/notetaker schedule --list            # what is scheduled
.venv/bin/notetaker unschedule <label>         # remove one
.venv/bin/notetaker unschedule --expired       # remove every job whose meeting is over
```

Things to know:

- launchd date triggers have no year. Each job only records on its scheduled date and exits immediately on the same date next year, but clean up spent jobs with `unschedule --expired` anyway.
- Scheduled jobs need Microphone permission for ffmpeg. The job runs under `/bin/bash` so the permission attaches to a stable, Apple-signed program. Schedule a test a few minutes out while you are at the Mac and approve the prompt when it appears.
- The Mac has to be awake and logged in at the scheduled time. The recorder keeps it awake once it starts.
- Scheduling is off until you use it. Nothing runs in the background otherwise.

## Notifications

Telegram notifications are optional and off by default. Set `enabled = true` under `[telegram]` and put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the env file. You get a message when the recorder cannot get in, when it has waited in a lobby or waiting room longer than `lobby_alert_minutes` (so someone in the meeting can admit it), and when a recording is finished.

## Known limits

- **Lobbies:** Teams puts guests in a lobby and Zoom may use a waiting room. Someone in the meeting has to admit the recorder. If nobody does, it gives up at the end of the join window and skips transcription. Externally hosted Teams webinars often never admit guests; ask the host for the recording instead.
- **Registration links:** for Zoom meetings and webinars that require registration, use your personal join link from the confirmation email (it contains `tk=`). The plain `/j/` link lands on the registration form, and the recorder would wait there until the join window runs out.
- **Passcodes:** use a Zoom link that includes `pwd=`. The recorder does not type passcodes.
- **Sign-in required:** meetings restricted to signed-in or same-organization users will not admit an anonymous guest. You can sign in once inside the recorder's own Chrome profile (quit it, then open it with `open -na "Google Chrome" --args --user-data-dir=$HOME/.local/share/meeting-notetaker/chrome-profile`), but test it before relying on it.
- **Webinars:** Zoom webinar attendees have no mute button, so the mute warning in the log is expected. The recorder waits on the "waiting for the host" page until the webinar starts, so raise `--join-window-minutes` if you start it early.
- **Silent rooms:** if nobody speaks, for example when you test it alone in a muted room, ElevenLabs returns an empty transcript and no notes are written. That is expected. A capture that is pure digital silence is not sent for transcription at all.
- **Teams join detection:** if Teams never shows its in-call toolbar, the recorder treats the join as "assumed" after a minute without a lobby or pre-join screen and records until the meeting ends or the cap is reached. `meta.json` records which case happened.
- **Web client changes:** Zoom and Teams change their web clients without notice. The selectors and page text this tool depends on are in `notetaker/zoom.py` and `notetaker/teams.py`, with comments explaining each one.
- **Speaker names:** diarization numbers voices; it does not identify people. Two similar voices can merge, and one voice can split.
- **Other platforms:** Google Meet, Webex, and consumer Teams links (`teams.live.com`) are not supported.
- **One meeting at a time:** the recorder takes over the Mac's audio, so it cannot record two meetings at once.

## Consent

You are responsible for telling people they are being recorded and for getting any consent your local laws require. Recording laws differ by country and state, and some require every participant's consent. The recorder joins under the display name you choose; pick one that makes clear it is a notetaker.

Meeting audio goes to ElevenLabs and the transcript goes to OpenAI (or the endpoint you configure). Check that this is acceptable for the meetings you record, including any confidentiality obligations. Recordings, transcripts, and notes stay on your Mac otherwise.

## License

MIT. See [LICENSE](LICENSE).

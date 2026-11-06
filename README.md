# Kaira

Free, open-source voice dictation for Windows. Press a hotkey, speak, and your words get pasted into any app.

Download the release zip, extract, run `Kaira.exe`, paste in your OpenAI API key when prompted. That's setup.

Built with Python and OpenAI Whisper. Costs roughly **$0.10–$5/month** in API fees depending on how much you dictate — paid directly to OpenAI, no middleman. Compare to Wispr Flow at $15/month or Dragon at $15/month.

## How It Works

1. Press **Ctrl+Win** and hold (or tap to toggle)
2. Speak
3. Release (or tap again) to stop
4. Your words are transcribed and pasted into whichever app has focus

The floating pill widget sits above your taskbar and shows the current state:

<p align="center">
  <img src="screenshots/idle.png" height="100" alt="Idle">&nbsp;&nbsp;
  <img src="screenshots/hover.png" height="100" alt="Hover">&nbsp;&nbsp;
  <img src="screenshots/recording.png" height="100" alt="Recording">&nbsp;&nbsp;
  <img src="screenshots/transcribing.png" height="100" alt="Transcribing">
</p>

## Why Kaira Exists

I was using Wispr Flow for voice dictation but hit the weekly limit on the free tier in two weeks. The paid plan is $15/month. I realised I could build the same thing using OpenAI's Whisper API for a fraction of the cost and keep the code open so anyone could run it.

A typical month of casual dictation costs about $0.10 in API fees. Heavy daily use (30 minutes a day) is around $5. You pay OpenAI directly — I never see your API key or your audio.

## Install

### Option 1 — Pre-built Release (recommended)

1. Download the latest `Kaira-v1.2.0-win64.zip` from the [Releases page](https://github.com/billywellington/kaira/releases)
2. Extract it anywhere (your user folder, `Program Files`, a USB stick — it's portable)
3. Double-click `Kaira.exe`
4. On first launch, Kaira will prompt you for an OpenAI API key. Paste it in and hit Save — it's stored securely in Windows Credential Manager, never written to disk.

Don't have an OpenAI key? Get one at [platform.openai.com/api-keys](https://platform.openai.com/api-keys). You'll need to add a few dollars of credit.

### Option 2 — Run from Source

If you'd rather run the Python script directly:

```bash
git clone https://github.com/billywellington/kaira.git
cd kaira
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python kaira.py
```

## Features

- **Floating pill overlay** — transparent, always-on-top, shows recording state. Click to start/stop dictation.
- **Two recording modes** — Hold-to-Talk (press and hold like a walkie-talkie) or Tap-to-Toggle. Configurable per hotkey.
- **Command Mode** — highlight text, press Ctrl+Shift+Alt, speak a command ("translate to Afrikaans", "rewrite as an email", "summarise in three bullets"). Kaira applies the command to the selected text and pastes the result.
- **AI cleanup** — raw Whisper output is passed through GPT-4o-mini to strip filler words, fix backtracking, add punctuation, and format lists naturally. Optional.
- **Configurable hotkeys** — change defaults in Tray > Preferences. Includes Sticky Keys advisory and conflict detection.
- **System tray icon** — Pause/Resume, Preferences, Start-on-login toggle, Open log folder, Quit. Left-click opens the menu.
- **Startup on Windows sign-in** — opt-in toggle in the tray menu. Never self-enables.
- **Pause/Resume** — fully releases all hotkeys, the mic stream, and overlay when paused. Survives reboots.
- **Multi-monitor support** — the overlay follows your cursor's screen on every hotkey press.
- **Crash-recovery journal** — every recording is preserved until transcription succeeds. If OpenAI goes down or your network drops, Kaira offers to retry on next launch.
- **Failed recordings queue** — after three failed attempts, a recording moves to a "Failed recordings" list in the tray menu where you can retry, reveal the audio file, or delete it. No infinite retry loops.
- **Hallucination guard** — Silero VAD + RMS pre-gate + phrase blocklist stops Whisper from transcribing silent audio as "Thank you for watching" or "Subscribe to my channel."
- **First-run API key dialog** — no `.env` editing needed. Prompts once, saves to Windows Credential Manager.

## Controls (Defaults)

| Action | Keys |
|--------|------|
| Start / stop dictation | **Ctrl+Win** (hold or tap) |
| Command mode on selected text | **Ctrl+Shift+Alt** (hold or tap) |
| Click the widget | Start / stop dictation |
| Quit Kaira | **Ctrl+Shift+Q** |

All hotkeys configurable via the tray menu > Preferences.

## Widget States

- **Idle** — small circle with a dim dot
- **Hover** — expands to show the hotkey hint
- **Recording** — pulsing red dot + live waveform bars driven by your mic level
- **Transcribing** — spinner + "Transcribing..."
- **Done** — brief flash, returns to idle

## How Much Does It Cost?

Whisper API pricing: **$0.006 per minute** of audio. GPT-4o-mini cleanup is roughly 50x cheaper than that.

| Your usage | Monthly API cost |
|-----------|------------------|
| Casual (a few minutes/day) | ~$0.10 |
| Regular (10 min/day) | ~$1.80 |
| Heavy (30 min/day) | ~$5.40 |

| Tool | Monthly cost |
|------|--------------|
| **Kaira (BYOK)** | **$0.10 – $5** |
| Wispr Flow | $15/mo |
| Aqua Voice | $10/mo |
| Dragon | $15/mo |

Even at heavy daily use, Kaira costs less than half of the cheapest subscription alternative. You're paying OpenAI directly at cost.

## Privacy

- Your OpenAI API key is stored in **Windows Credential Manager** — never written to disk in plain text.
- Your audio goes from your machine → OpenAI Whisper → back. Kaira has no servers, no telemetry, no analytics, no phone-home.
- Recordings are kept in `%APPDATA%\Kaira\logs\audio\` for 7 days by default so failed transcriptions can be retried. You can delete them any time.
- Logs are local and contain no audio — just timestamps, state transitions, and errors for debugging.

## Tech Stack

- **Python 3.12** — core runtime
- **PyQt6 + QPainter** — transparent floating overlay via DWM per-pixel alpha (works reliably on Intel UHD)
- **Raw Input API** — global hotkeys without the low-level hook disconnect bug that plagues pynput
- **OpenAI Whisper API** — speech-to-text
- **OpenAI GPT-4o-mini** — optional AI cleanup
- **Silero VAD (via silero-vad-lite)** — ONNX-based voice activity detection, ~2MB, no PyTorch needed
- **Tenacity** — exponential-backoff retries on transient API failures
- **sounddevice** — 16kHz mono microphone capture
- **keyring** — Windows Credential Manager integration for API key storage

## System Requirements

- Windows 10 or Windows 11 (64-bit)
- A working microphone
- Internet connection (for Whisper API calls)
- 200 MB free disk space for the extracted app
- No admin rights required — Kaira runs as the current user only

Mac and Linux support: the backend (recording, transcription, paste) is cross-platform, but the overlay uses Windows-specific DWM APIs. PRs welcome.

## Troubleshooting

**"Another Kaira instance is already running"**
Open Task Manager and end any `Kaira.exe` or `pythonw.exe` processes. Delete `%APPDATA%\Kaira\kaira.lock` if it still exists. Relaunch.

**Widget doesn't appear**
Check the latest log at `%APPDATA%\Kaira\logs\` (accessible via the tray menu > "Open log folder"). Most common causes: microphone permissions off in Windows Privacy settings, or a Raw Input registration failure (relaunch usually fixes it).

**Hotkey does nothing**
If Sticky Keys is enabled, Windows may intercept the combo. Kaira logs a warning on boot when it detects this. Disable Sticky Keys in Windows Ease-of-Access settings, or change Kaira's hotkey in Preferences.

**"OpenAI is unavailable" toast**
Your network couldn't reach the Whisper API, or OpenAI is genuinely down. The recording is preserved — click the toast to retry, or Kaira will offer again on next launch. After three failed attempts a recording moves to the "Failed recordings" list in the tray menu.

**High latency on short dictation**
Whisper typically returns in 2–4 seconds. If you're seeing >10 seconds, your upload is the bottleneck. Mobile LTE and congested WiFi are the usual culprits.

## Project Structure

```
kaira/
  Kaira.exe              # Pre-built executable (from Releases)
  _internal/             # PyInstaller bundled dependencies
  logs/                  # Runtime logs + audio cache (in %APPDATA%\Kaira\)
```

If running from source:

```
kaira/
  kaira.py               # Main app — overlay + backend
  requirements.txt       # Python dependencies
  kaira.spec             # PyInstaller build config
  start-silent.vbs       # Windows auto-start launcher
  how-it-works.txt       # Technical deep-dive
```

## Contributing

Issues, PRs, and feedback welcome.

- Found a bug? Open an issue with the relevant chunk of your log from `%APPDATA%\Kaira\logs\`.
- Want a feature? Open an issue — I read them all.
- Got it working on Mac or Linux? PR please.

## License

MIT. Do whatever you want — commercial use, redistribution, modification, all fine. Attribution appreciated but not required.

## Author

Built by [Billy Wellington](https://github.com/billywellington) in Cape Town, South Africa.

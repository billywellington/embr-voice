# Your first dictation

## The one-line version

Click into any text field in any Windows app. Tap `Ctrl` + `Win`. Speak. Tap `Ctrl` + `Win` again to stop. Your words appear typed into the field.

## Step-by-step

1. **Make sure Embr is running.** Look for the orange waveform mark in your system tray (bottom-right of the taskbar, possibly hidden behind the `⌄` chevron). If you don't see it, double-click `Embr.exe` again.
2. **Click where you want to dictate.** A browser search box. A Slack message. A Notepad window. Anywhere that accepts typed text.
3. **Tap `Ctrl` + `Win` once.** The tray icon pulses to tell you Embr is listening. A small subtle overlay may appear to indicate recording.
4. **Speak naturally.** Don't over-enunciate. Don't pause awkwardly. Punctuate if you want ("…new paragraph, then period").
5. **Tap `Ctrl` + `Win` again to stop.** Embr sends your audio to OpenAI, waits a second or two, then types the transcription into wherever your cursor was.

## What you'll notice the first time

- **Latency:** The first dictation is usually the slowest because OpenAI's API is "cold". Expect 1–3 seconds. Subsequent dictations are faster (often under 1 second for short utterances).
- **Punctuation:** Whisper handles ". ? !" automatically from intonation. If it's missing, say it out loud — "new line", "new paragraph", "comma", "period".
- **Corrections:** If it mis-hears you, just dictate the correction or type it. Embr doesn't rewrite; it inserts where the cursor is.

## Tap vs. hold

`Ctrl` + `Win` defaults to **TAP-to-toggle**:

- **Tap once** → start listening
- **Tap again** → stop and transcribe

If you prefer **HOLD-to-talk** (like walkie-talkie):

- Open Settings → Dictation → set Hotkey mode to **Hold**
- Now: press-and-hold `Ctrl` + `Win` while you speak, release to transcribe

Tap is the default because most users prefer not to hold a key for a long dictation. Hold is better for short bursts.

## Where it doesn't work

- **Elevated / admin windows** — Windows security forbids a non-elevated app from sending keystrokes to admin windows. If you need Embr to type into an elevated window (Task Manager, Registry Editor), right-click `Embr.exe` → Run as administrator.
- **Password fields** — by design. Embr detects password fields and skips them.
- **Some games / DirectInput apps** — Embr uses `SendInput` which bypasses most games but some anti-cheat-protected games block injected keystrokes.

## What if nothing happens when I tap Ctrl + Win?

See **[Troubleshooting → Hotkey not responding](troubleshooting.md#hotkey-not-responding)**.

## What if it hears me wrong?

Usually a microphone issue, not a transcription issue. See **[Troubleshooting → Mic problems](troubleshooting.md#microphone)**.


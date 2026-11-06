# Changelog

Human-readable history of what shipped in each Embr Voice release. Full commit log on [GitHub](https://github.com/billywellington/embr-voice/commits/main).

## v1.2.0 — 2026-04 (in progress)

Reliability sprint. No new features, focused on making the first-run experience and core dictation loop rock-solid.

**Added**

- 6-screen first-run onboarding wizard (Welcome / API key / Hotkey walkthrough / Try a dictation / Tray tour / Ready)
- Live microphone level meter in onboarding and Settings
- Microphone source selection (dropdown of all input devices)
- Hotkey conflict warnings (detects Sticky Keys, Windows virtual-desktop shortcuts)
- MkDocs Material handbook at billywellington.github.io/embr-voice/
- In-app "Ask Embr" chatbot link (via tray menu → Help)

**Fixed**

- Windows 11 taskbar icon drift across rebuilds (AppUserModelID + HKCU registration)
- Onboarding dialog logo clipping at HiDPI (QPainter coord-space fix)
- Tray icon missing orange accent on some Windows themes
- Hotkey release detected before key-up on modifier combos
- Silent dictation failure when OpenAI returns HTTP 5xx (now surfaces clear error toast)

**Changed**

- Default hotkey mode is now **tap-to-toggle** (was hold-to-talk). Hold is opt-in via Settings.
- Transcription model defaults to `whisper-1` (was auto-select); users can switch to `gpt-4o-transcribe` in Settings.
- Settings split into Dictation / Microphone / Appearance / Startup / Privacy / Advanced tabs.

**Known issues**

- Windows 10 onboarding tray-tour illustration looks slightly off (Win 11 Mica/acrylic chrome mocked).
- GPT-4o-transcribe can occasionally return punctuation-heavy transcriptions — workaround: switch to `whisper-1` via Settings.

## v1.1.0 — 2026-03

Reliability sprint.

**Added**

- Raw Input hook (replaces pynput low-level hook for lower CPU + no elevation needed)
- Multi-monitor overlay positioning
- Tray icon hide-fix (SWP_SHOWWINDOW flag diagnosis)

**Fixed**

- Overlay didn't hide on quit in some cases
- Mic stuck "holding" after hotkey release on fast taps

## v1.0.0 — 2026-02

First public release under the Embr Voice name (previously Kaira, previously Voxly).

**Added**

- Windows tray app with hotkey-triggered dictation
- OpenAI Whisper + GPT-4o-transcribe support
- Translucent on-screen overlay while recording
- Keyring storage for API key
- Single-instance lock
- Command Mode (press-and-hold for single-shot commands)

---

For commit-level detail, see the [CHANGELOG.txt](https://github.com/billywellington/embr-voice/blob/main/CHANGELOG.txt) in the repo root.


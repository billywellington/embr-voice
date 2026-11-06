# Settings

Open via: tray icon → **Settings**.

All settings persist to `%APPDATA%\Embr\settings.json`.

## Dictation

| Setting | Default | Notes |
|---|---|---|
| Hotkey | `Ctrl` + `Win` | [Change via Settings → Dictation → Change hotkey](hotkeys.md#changing-the-hotkey) |
| Hotkey mode | Tap | Tap toggles, Hold is press-and-hold-while-speaking |
| Language | Auto-detect | Explicit language (English, Afrikaans, French, etc.) is more accurate |
| Transcription model | `whisper-1` | Alternative: `gpt-4o-transcribe` (slightly better on accents, same price) |
| Auto-pause in apps | empty | Comma-separated `.exe` names where Embr won't respond |

## Microphone

| Setting | Default | Notes |
|---|---|---|
| Input device | System default | Drop-down of all input devices detected on your system |
| Input gain | 1.0× | Bump to 1.5× or 2× if your mic is quiet |
| Noise suppression | On | Uses Silero VAD to trim silence and background noise before sending to OpenAI |
| Mic test | Button | Opens a live level meter — speak and watch the bars move |

If the bars don't move when you speak, you have the wrong input device selected.

## Appearance

| Setting | Default | Notes |
|---|---|---|
| Overlay visibility | Subtle | Tiny orange waveform pill while recording. Options: off / subtle / prominent |
| Overlay position | Bottom-right | Corner of the screen where the recording indicator appears |
| Tray icon | Colour | Alternative: Monochrome for minimal Windows themes |

## Startup

| Setting | Default | Notes |
|---|---|---|
| Start with Windows | Off | Writes a shortcut to `shell:startup`; no scheduled task, no service |
| Start minimised to tray | On | No window flashes on startup; Embr goes straight to tray |
| Check for updates | On | Once per day, checks GitHub Releases — does not auto-download |

## Privacy

| Setting | Default | Notes |
|---|---|---|
| Pause when screen-sharing detected | Off | Detects Teams / Zoom / Meet / Discord active share |
| Save transcription history | Off | If on, the last 100 dictations are saved to `%APPDATA%\Embr\history.json` (never uploaded) |
| Telemetry | Off | Embr ships with zero telemetry. Ever. This toggle is reserved for v1.3+ opt-in anonymised usage stats; v1.2 does nothing |

## Advanced

| Setting | Default | Notes |
|---|---|---|
| API endpoint | `https://api.openai.com/v1` | Change to a self-hosted OpenAI-compatible endpoint (e.g. Azure OpenAI, Ollama with whisper-server) |
| HTTP timeout | 30s | Raise if you're on a slow connection |
| Log level | Info | Debug is verbose; use when filing a bug |
| Open logs folder | Button | Opens `%APPDATA%\Embr\logs\` in Explorer |
| Reset all settings | Button | Clears `settings.json` and runs the first-run wizard again (keeps your API key) |


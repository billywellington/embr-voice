# FAQ

Short answers to the things people ask before they ask.

## Privacy

### Does my audio leave my computer?

Yes, but only to OpenAI's API, and only while you're actively dictating. Your audio is sent to `api.openai.com`, transcribed, and the response comes back. Nothing is stored on an Embr-owned server (we don't have one).

### Does my audio stay with OpenAI?

Per OpenAI's API data policy (as of 2026), audio sent to their API is **not** used to train their models and is retained for up to 30 days for abuse detection, then deleted. Read the latest at [platform.openai.com/docs/models#data-usage](https://platform.openai.com/docs/models#data-usage).

### Can I use Embr fully offline?

Not in v1.2 — v1.2 is cloud-only. **v1.3** ships a bundled `faster-whisper` local model as a fallback. Lower accuracy, slower, but fully on-device and free.

### Does Embr have telemetry?

No. v1.2 ships with zero telemetry. There is a disabled "Telemetry" toggle in Advanced settings reserved for v1.3+ opt-in anonymised stats (sessions, errors, model choice). It does nothing in v1.2.

### Where is my API key stored?

Windows Credential Manager, AES-encrypted per user. Never in a plain text file. You can inspect it yourself via **Control Panel → Credential Manager → Windows Credentials → Embr-OpenAI-Key**.

## Cost

### How much will the API cost me?

OpenAI charges $0.006 per minute of audio. A heavy user at 30 min/day = about **$5.40/month**. Most users land at **$1–3/month**. Set a spending cap in your OpenAI dashboard for peace of mind.

### Does Embr charge a monthly fee?

No. Embr is free and open source. We don't have a subscription, a Pro tier, or a paywall. v1.3 may introduce optional paid conveniences (managed API billing, cloud sync) — the free tier stays free forever.

### Can I use a free OpenAI trial credit?

Yes. If your OpenAI account has trial credit, Embr uses it transparently. When the credit runs out you'll get an HTTP 429 / 402 and Embr will tell you to top up.

## Compatibility

### Does Embr work on Windows 10?

Partial support. Tested on Windows 10 22H2 — the tray icon, hotkey, dictation all work. The onboarding's Win 11–style tray tour looks slightly off. If something specific breaks on Win 10, [file an issue](https://github.com/billywellington/embr-voice/issues).

### Does Embr work on Mac or Linux?

Not yet. v1.2 is Windows-only. Mac support is planned for v1.5 (Q3 2026 estimate). Linux is community-maintained if someone steps up.

### Does Embr work on ARM / Copilot+ PCs?

Yes. PyQt6 runs on ARM64 Windows. The bundled .exe is x64; an ARM64 native build ships with v1.3.

### Does Embr work inside Microsoft Remote Desktop / Citrix / VMware VM?

Mixed. Audio capture inside remote sessions is often blocked by the host. Embr will start but won't get mic data. This is a platform limitation, not an Embr bug.

## Features

### Can I dictate in languages other than English?

Yes. Whisper supports 99 languages. Set the language explicitly in **Settings → Dictation → Language** for better accuracy than auto-detect.

### Can I dictate code?

It works but unevenly. Whisper transcribes "open paren" as `(` some of the time but not reliably. Use a code-aware model (GPT-4o-transcribe) and speak punctuation explicitly if you do this often.

### Can I customise the hotkey?

Yes. **Settings → Dictation → Change hotkey**. Must include at least one modifier (`Ctrl`, `Alt`, `Win`, `Shift`). See [Hotkeys](hotkeys.md#changing-the-hotkey).

### Can I pause Embr temporarily?

Yes. Right-click the tray icon → **Pause**. See [Pause and resume](pause-resume.md).

### Can Embr auto-pause during screen-shares?

Yes. **Settings → Privacy → Pause when screen-sharing detected**. Off by default.

## Taskbar icon

### Why is the taskbar icon showing a generic icon instead of the orange waveform?

Windows 11 resolves taskbar icons via AppUserModelID. If the icon is wrong:

1. Close all Embr windows (quit from tray).
2. Run `ie4uinit.exe -show` from a regular PowerShell.
3. Restart explorer.exe (Task Manager → Details → `explorer.exe` → End task → File → Run new task → `explorer.exe`).
4. Relaunch Embr.

Still wrong? [File an issue](https://github.com/billywellington/embr-voice/issues) with your Windows version (winver) and a screenshot.

## Contributing

### Can I contribute?

Yes. Embr is open source. See [CONTRIBUTING.md](https://github.com/billywellington/embr-voice/blob/main/CONTRIBUTING.md) in the repo. Bugs, docs improvements, translations, and small features are all welcome.

### Can I fork Embr?

Yes. MIT license. Fork freely. If you ship a commercial derivative, keep the MIT notice.

### How do I request a feature?

[GitHub Discussions → Ideas](https://github.com/billywellington/embr-voice/discussions/categories/ideas). Search first — someone may already have.

## Project

### Who builds Embr?

Embr Voice is maintained by [Billy Wellington](https://github.com/billywellington) at [Intellium Inc.](https://intelliuminc.co.za), based in Cape Town, South Africa. Currently a solo project with community contributors.

### Is Embr a company or a product?

A free open-source product, built alongside Intellium's paid services (websites, automation, consulting). There is no "Embr Inc." — the brand is a marker for the tool itself.

### What happens if the maintainer disappears?

MIT-licensed source is in the GitHub repo. The community can fork, or a new maintainer can take over. Your locally-installed copy keeps working forever; it doesn't phone home, doesn't license-check, doesn't expire.


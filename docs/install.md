# Install

Embr Voice is a single `.exe` for Windows 11. No installer, no admin rights, no DLLs to unpack.

## Requirements

- Windows 11 (Windows 10 22H2 works but is not primary-tested)
- A working microphone (built-in, USB, or Bluetooth)
- An OpenAI API key ([get one](api-key.md))
- About 400 MB of disk space (mostly the bundled transcription model)

## Download

Go to the **[latest release](https://github.com/billywellington/embr-voice/releases/latest)** and grab `Embr-v1.2.0.zip`.

Unzip anywhere. Double-click `Embr.exe` inside the extracted `Embr/` folder.

!!! tip "Put it somewhere permanent"
    Don't unzip into `Downloads/` and leave it there — when you empty Downloads, Embr goes with it. A common spot is `C:\Users\<you>\Tools\Embr\`.

## First-run wizard

On first launch Embr opens a 6-step wizard:

1. **Welcome** — tells you what Embr does
2. **Paste your OpenAI API key** — saved to Windows Credential Manager (not a plain file)
3. **Hotkey walkthrough** — shows you `Ctrl` + `Win` as the dictation trigger
4. **Try a dictation** — speaks into a real textarea with your real mic
5. **Tray tour** — shows where Embr lives in your Windows taskbar
6. **You're ready** — tap `Ctrl` + `Win` anywhere in Windows

You can skip any screen. The wizard only runs once — after that Embr launches silent-to-tray.

## Start with Windows

Open the tray icon → Settings → **Start with Windows** toggle. This adds a lightweight launcher to your startup folder (no scheduled task, no service). Uncheck at any time.

## Uninstall

Embr doesn't write to the registry, `ProgramFiles`, or anywhere else outside its own folder (with two exceptions, see below). To uninstall:

1. Right-click the tray icon → Quit
2. Delete the `Embr/` folder
3. Optional cleanup:
    - `%APPDATA%\Embr\` — settings.json and logs
    - Windows Credential Manager → `generic credential` named `Embr-OpenAI-Key`
    - HKCU\Software\Classes\AppUserModelId\Intellium.EmbrVoice.Dictation.3 (taskbar icon association)

## Smartscreen or Defender warning

Because Embr is new and unsigned, Windows SmartScreen may show "Windows protected your PC" the first time you run it. Click **More info** → **Run anyway**. This will go away once Embr accumulates enough reputation (or once we sign the build, which is planned for v1.3).

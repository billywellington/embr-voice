# Pause and resume

Sometimes you want Embr to stop listening for the hotkey temporarily — during a meeting, while playing a game, during a screen-share, when a colleague is borrowing your machine.

## Pause (temporarily stop responding to the hotkey)

Right-click the Embr tray icon → **Pause**.

While paused:

- Tapping `Ctrl` + `Win` does nothing (Embr ignores it)
- The tray icon turns grey so you know at a glance
- Embr stays running, keeps your API key loaded, keeps your settings in memory — it just doesn't listen

To resume: right-click tray icon → **Resume** (same menu item toggles).

## Pause automatically in specific apps

Settings → Dictation → **Auto-pause in apps** lets you list app executables where Embr should auto-pause:

- `obs.exe` (streaming / recording)
- `notion.exe` (already has voice)
- `gameapp.exe` (to prevent accidental dictation)

Type the `.exe` name. Embr watches the foreground window and pauses/resumes automatically.

## Pause during screen-share (auto-detect)

Settings → Privacy → **Pause when screen-sharing detected**. Embr detects when Teams, Zoom, Meet, or Discord is actively sharing your screen and auto-pauses. Unchecked by default.

## Quit Embr completely

Right-click tray icon → **Quit**. Removes the tray icon and terminates the process. Your API key stays in Credential Manager; relaunching `Embr.exe` picks up right where you left off.

## Why pause instead of just quit?

Starting Embr fresh reloads the transcription model (about 1–2 seconds). Pause keeps everything warm — resume is instant. For a 10-minute break, pause. For overnight, quit.


# Hotkeys

## Default

| Action | Keys |
|---|---|
| Start / stop dictation (tap mode) | `Ctrl` + `Win` |
| Start / stop dictation (hold mode, opt-in) | Hold `Ctrl` + `Win` while speaking |
| Show Embr overlay | `Ctrl` + `Shift` + `Win` |
| Cancel current recording | `Esc` while recording |

## Tap vs Hold — which is right for you?

- **Tap (default):** best for multi-sentence dictations, emails, long thoughts. One tap to start, another to stop. Your hands are free while speaking.
- **Hold:** best for short bursts, quick replies, Slack messages. Press while speaking, release when done.

Change in **Settings → Dictation → Hotkey mode**.

## Why `Ctrl` + `Win`?

- **Globally unused** by Windows defaults (no conflict with `Win` + letter shortcuts, no conflict with `Ctrl` + letter shortcuts in most apps).
- **Two hands** — hard to trigger accidentally.
- **Mid-row** — comfortable to reach without looking.

## Known conflicts

Windows reserves a few `Ctrl` + `Win` combinations for its own use:

| Conflict | What it does | Embr behaviour |
|---|---|---|
| `Ctrl` + `Win` + `D` | Create virtual desktop | Ignored by Embr (won't trigger dictation) |
| `Ctrl` + `Win` + `←/→` | Switch virtual desktops | Ignored |
| `Ctrl` + `Win` + `F4` | Close virtual desktop | Ignored |

So: `Ctrl` + `Win` alone (no extra key) is yours. Combos that add a third key go to Windows.

## Changing the hotkey

Settings → Dictation → **Change hotkey**. You'll see a "Press the new combo…" prompt.

Rules:

- Must include `Ctrl`, `Alt`, `Win`, or `Shift` as at least one modifier
- Cannot be a single key
- Cannot be `F1` (Windows Help) or `PrtScn` (Screenshot)
- Embr will warn if your chosen combo conflicts with a known Windows shortcut

Common alternatives users pick:

- `Alt` + `Space` — very fast, but conflicts with window menu in some apps
- `Ctrl` + `` ` `` — under the tilde, easy reach
- `Win` + `/` — clean and uncontested

## Sticky keys interference

If you have **Sticky Keys** enabled in Windows Ease of Access, pressing `Ctrl` twice or holding `Ctrl` + `Win` may trigger the Sticky Keys prompt ("Do you want to turn on Sticky Keys?"). Embr shows a one-time advisory on boot if Sticky Keys is detected.

To disable Sticky Keys: **Settings → Accessibility → Keyboard → Sticky keys** → off.

## Reset to default

Settings → Dictation → **Reset hotkey** restores `Ctrl` + `Win`.


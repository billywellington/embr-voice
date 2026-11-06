# Troubleshooting

Most issues fall into one of six buckets. Find the symptom, try the fix, escalate only if it's still broken.

## Hotkey not responding

**Symptom:** You tap `Ctrl` + `Win`, nothing happens. No tray pulse, no overlay.

- **Check Embr is actually running:** look for the orange waveform in the tray. If you don't see it (even after expanding the `⌄` chevron), Embr isn't running. Double-click `Embr.exe` again.
- **Check Embr isn't paused:** tray icon is grey when paused. Right-click → Resume.
- **Check the hotkey wasn't changed:** Settings → Dictation → Hotkey. Default is `Ctrl` + `Win`.
- **Admin windows refuse keystrokes from non-admin apps.** If you're trying to dictate into Task Manager / Registry Editor / an elevated PowerShell, right-click `Embr.exe` → Run as administrator.
- **Gaming mode may be intercepting:** Xbox Game Bar, Razer Cortex, NVIDIA GeForce Overlay all hook `Win` combos aggressively. Disable them for a quick test.
- **Sticky Keys prompt:** if you see a "Do you want to turn on Sticky Keys?" dialog when pressing `Ctrl` + `Win`, disable Sticky Keys. **Settings → Accessibility → Keyboard → Sticky keys → off.**

## Microphone

**Symptom:** Embr records but the transcription comes back empty, or always wrong.

- **Open Settings → Microphone → Mic test.** Speak and watch the bars. If they don't move, the wrong input device is selected — change the dropdown.
- **Check Windows itself hears you:** Settings → System → Sound → Input. Speak. Windows should show the input level moving. If it doesn't, it's a Windows-level issue, not Embr.
- **USB / Bluetooth mic just plugged in:** Windows sometimes silently falls back to the laptop mic. Reselect your USB/Bluetooth device in Embr's Settings.
- **Bluetooth headset in HSP mode:** HSP (low-quality call mode) has bad audio. Pair in A2DP for music / Handsfree profile for dictation — consult your headset's docs.
- **Input gain too low:** Settings → Microphone → Input gain → bump to 1.5× or 2×.
- **Background noise:** if Embr transcribes the TV/radio/aircon, enable Noise suppression (Settings → Microphone → Noise suppression).

## API errors

**Symptom:** Embr listens but shows an error toast when it tries to transcribe.

### "API key rejected" / HTTP 401
- Your OpenAI key is wrong, revoked, or hit its spending cap.
- Go to platform.openai.com/api-keys → verify it still exists.
- Check billing at platform.openai.com/settings/organization/billing.
- Regenerate a new key and paste into Embr: Settings → OpenAI key → Change.

### "Rate limited" / HTTP 429
- You've hit OpenAI's rate limit (unusual for personal use).
- Wait 60 seconds.
- If it keeps happening, check platform.openai.com/settings/organization/limits.

### "Network error" / HTTP 0 / timeout
- Check you're online.
- Check a VPN isn't intercepting api.openai.com.
- Check Windows Firewall hasn't blocked Embr.exe.
- Settings → Advanced → HTTP timeout → bump to 60s if you're on slow internet.

### "Service unavailable" / HTTP 5xx
- OpenAI is having an outage. Check status.openai.com.
- v1.3 adds a local faster-whisper fallback that transcribes on your machine when OpenAI is down.

## Transcription quality

**Symptom:** It hears you but the words are wrong.

- **Set an explicit language** (Settings → Dictation → Language). "Auto-detect" guesses poorly for accented English and bilingual speakers.
- **Try `gpt-4o-transcribe`** (Settings → Dictation → Model). Same price as `whisper-1`, often better on accents and fast speech.
- **Speak normally.** Don't shout, don't over-enunciate, don't pause between words. Whisper is trained on natural speech.
- **Short noise-filled recordings** are hardest. Keep dictations over 2 seconds; under 1 second often fails.

## Typed text goes to the wrong place

**Symptom:** Embr transcribes correctly but types into the wrong app / wrong field.

- **Your cursor was somewhere else when you stopped recording.** Embr types into whatever has focus at the moment transcription returns, not the moment you started.
- **Don't click elsewhere during dictation.** Start the dictation, keep your cursor in the target field, stop the dictation.
- **Password fields are skipped by design.** If you're trying to dictate into a password field (not recommended), Embr refuses.

## Tray icon missing / wrong

**Symptom:** Embr is running (you can see it in Task Manager) but no tray icon, or the tray shows the generic Python icon instead of the orange waveform.

- **Icon is hidden in the chevron flyout.** Click the `⌄` at the right end of the taskbar — Embr is often tucked inside. Drag it back to the visible area if you use it often.
- **Old Windows icon cache:** restart explorer.exe. Open Task Manager → Details → `explorer.exe` → End task → File → Run new task → `explorer.exe`.
- **Wrong icon specifically:** see [FAQ → Why is the taskbar icon showing a generic icon](faq.md#taskbar-icon).

## Still stuck?

1. **Reproduce the issue once with Debug logging on.** Settings → Advanced → Log level → **Debug**. Try again. Log level → **Info**.
2. **Open Settings → Advanced → Open logs folder.** Grab the most recent `.log` file.
3. **File an issue** at [github.com/billywellington/embr-voice/issues/new](https://github.com/billywellington/embr-voice/issues/new) and drop the log file in.

Or ask the in-app chatbot: tray icon → Help → Ask Embr. It's trained on this handbook and can answer the common stuff without you leaving the app.


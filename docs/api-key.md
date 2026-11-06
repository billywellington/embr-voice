# Your OpenAI API key

Embr uses your own OpenAI key so audio and transcription never touch a third-party server. No accounts with Embr. No hidden middleman. Your key, your bill, your data.

## Get a key (60 seconds)

1. Go to **[platform.openai.com/api-keys](https://platform.openai.com/api-keys)**
2. Sign in (or create an OpenAI account — this is a separate account from ChatGPT.com but uses the same login)
3. Click **Create new secret key** → give it a name like `Embr Voice` → copy the key (starts with `sk-proj-`)
4. Back in Embr's onboarding, paste it into the input and hit **Next**

You only see the key once in the OpenAI dashboard, so paste it into Embr immediately. Embr stores it in Windows Credential Manager (encrypted per-user), not in any plain file.

## What will it cost me?

Embr uses OpenAI's audio transcription endpoint. At current pricing:

- **Whisper-1**: $0.006 per minute of audio
- **GPT-4o-transcribe**: $0.006 per minute of audio

A heavy user dictating 30 minutes per day = about **$5.40/month**. Most users land around **$1–3/month**.

You set your own spending limit in the OpenAI dashboard. Embr will show an error if the key hits its cap; it does not silently burn credit.

## Where is the key stored?

- **Live key**: Windows Credential Manager → generic credential `Embr-OpenAI-Key` (AES-encrypted with your Windows login)
- **Never written to**: `settings.json`, any log file, or any network destination that isn't OpenAI's API

You can inspect this yourself. Open **Control Panel → Credential Manager → Windows Credentials** and find the Embr entry.

## Rotating or changing the key

- **Change the key**: tray icon → Settings → OpenAI key → **Change**
- **Revoke from OpenAI**: delete the key in the OpenAI dashboard; Embr will show a clear "key rejected" error on next dictation
- **Clear from Windows**: Credential Manager → delete the `Embr-OpenAI-Key` entry

## Using a company or team key

Safe. Embr doesn't phone home or register the key anywhere. If you use a team key, only your own Windows profile has the credential; other profiles on the same machine can't read it.

## Offline / local transcription (coming in v1.3)

v1.2 is cloud-only (OpenAI). v1.3 ships a bundled faster-whisper model as a local fallback — if your internet drops, Embr transcribes on-device. Slightly lower accuracy, zero cost, offline-friendly.


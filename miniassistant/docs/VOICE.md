# Voice (STT + TTS)

Voice input and output on all platforms (Matrix, Discord, Web UI, OpenAI-compatible API).
STT transcribes incoming audio. TTS generates spoken replies via `send_audio`. Both are optional and independent.
**`send_audio` works on every platform** — including Web and API clients. Always use the tool, never output `<audio>` HTML tags directly.

## Rules for voice replies

Read this section before using `send_audio` or replying to `[Voice]` messages.

**send_audio(text="...")** sends a voice reply. The text MUST be plain spoken language:
- No emojis. No markdown. No symbols. No URLs. No coordinates. No technical IDs.
- Short: 1-3 sentences. Only say what a human would say out loud.
- Dense data (coordinates, specs, addresses) goes in a follow-up text message, not in audio.
- After successful send_audio: **no text reply.** No confirmation. No "Technischer Hinweis".

**Incoming voice** (message starts with `[Voice]`): same rules. Plain, short, no formatting.

### Text rewrite rules — apply BEFORE calling send_audio

| Written form | Spoken form |
|---|---|
| Number ranges: `10-20` | `10 bis 20` (compounds like `E-Mail` stay) |
| Times: `14:30` | `14 Uhr 30` |
| Slashes: `km/h` | `Kilometer pro Stunde` |
| `und/oder` | `und oder` |
| `z.B.` | `zum Beispiel` |
| `d.h.` | `das heisst` |
| `ca.` | `circa` |
| `usw.` | `und so weiter` |
| Numbered lists: `1. ... 2. ...` | `Erstens ... Zweitens ...` |

Tables and code blocks are automatically extracted and sent as separate text message.

## Setup

**Requirements:** `ffmpeg` (added by `install.sh`). A running STT and/or TTS server.

### STT — faster-whisper (recommended)
```bash
# Docker
docker run -it -p 10300:10300 rhasspy/wyoming-faster-whisper --model tiny-int8 --language de
# Or pip
pip install wyoming-faster-whisper
wyoming-faster-whisper --uri tcp://0.0.0.0:10300 --model tiny-int8 --language de
```

### TTS option A — Piper (lightweight, many voices)
```bash
docker run -it -p 10200:10200 rhasspy/wyoming-piper --voice de_DE-thorsten-medium
```
Voices: https://github.com/rhasspy/piper/blob/master/VOICES.md

### TTS option B — Kokoro (high quality neural)
```bash
docker run -it -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:v0.2.1
```
Voices: `af_bella`, `af_sarah`, `af_sky`, `af_nicole`, `am_adam`, `am_michael`, `bf_emma`, `bm_george`, `bm_lewis`

## Config

```yaml
# Piper (Wyoming protocol):
voice:
  stt:
    url: tcp://localhost:10300
  tts:
    url: tcp://localhost:10200
  language: de
  tts_voice: de_DE-thorsten-medium

# Kokoro (HTTP API):
voice:
  stt:
    url: tcp://localhost:10300
  tts:
    url: http://localhost:8880
  tts_voice: af_bella

# LocalAI / VibeVoice:
voice:
  tts:
    url: http://localhost:8080/tts
    model: vibevoice
    voice: Emma
```

Backend auto-detected from URL scheme: `tcp://` = Wyoming/Piper, `http://` = HTTP API.
HTTP without path appends `/v1/audio/speech`. HTTP with path is used as-is.
Restart after config changes.

## How it works

1. Incoming audio → ffmpeg → 16kHz mono PCM → STT → transcript
2. Transcript sent to agent with `[Voice]` prefix
3. Agent responds → TTS → WAV → audio message back
4. Tables and code blocks extracted and sent as separate text

Supported input: anything ffmpeg decodes (ogg, mp3, wav, m4a, webm, flac, aac, ...).

## Troubleshooting

| Problem | Fix |
|---|---|
| "Sprachfunktion nicht konfiguriert" | `voice.stt.url` missing in config |
| "Spracherkennung fehlgeschlagen" | STT server unreachable or ffmpeg missing |
| "Konnte Sprachnachricht nicht erkennen" | Empty transcript — try different model or check language |
| TTS fails, text sent instead | TTS server unreachable — check `voice.tts.url` |

# Local startup validation — Windows

Source base: `fd797f6c08262be191d8dc59d135083da2aa2423`, branch
`codex/progress-2026-10-09`, plus uncommitted startup fixes. No branches merged.
Remote main and both reviewed development heads matched the handoff on 2026-10-09.
The assigned checkout's `CLAUDE.md` identifies Foundation v1.1 as its baseline;
no architecture decision, contract replacement, or ADR approval is made here.

## Prerequisites

Use the existing Python 3.12 editable environment. No installs are part of this
change. Normal chat needs loopback Ollama at `127.0.0.1:11434`, with
`gpt-oss:20b` and `nomic-embed-text`. Kokoro and its language assets are optional
for text chat; Whisper and an audio input/output device are needed for real voice.
ComfyUI, large speech helpers and Unreal are outside this startup milestone.

## Scratch validation

From the repository root, using the existing environment:

```powershell
python scripts/local_smoke.py
```

This creates temporary synthetic memory, control and settings stores, serves the
real chat page on a free loopback port, and exercises the real WebSocket and broker
model path. It verifies embeddings, a completed chat turn, reopening persisted
conversation/settings, and shutdown. The microphone and voice generation are off.
Hardware/image model tools are not offered. It does not call the default production
launcher, load owner databases, alter owner policy, or install models.

To inspect the page before its automated exchange:

```powershell
python scripts/local_smoke.py --serve-seconds 180 --url-file "$env:TEMP\freya-smoke-url.txt"
```

Choose a new URL filename: the script refuses to overwrite one. Open that file's
address locally. It contains a single-use scratch session key; do not share it.
The gateway stops after the bounded check and the temporary storage is removed.
The URL no longer works after shutdown. Never point this script at owner storage.
The synthetic stores cannot be used to evaluate authenticated multi-user isolation.

## Normal owner launch

`python -m freya` starts console plus window; `freya-window` starts without a
console. These normal launchers use owner storage and were not executed during
development validation. They are for the owner to run, not synthetic tests.

If Kokoro model construction fails, Freya keeps text chat available, starts no voice
worker, and shows `Voice unavailable`. Saved voice selection is preserved; it is
not reapplied while live voice is disabled. After the owner fixes optional voice
assets, restart to retry. Audio playback exceptions still report their error and
release the speaking state; synthetic tests verify the worker can stop afterward.
This change does not bound a model loader that hangs or fix absent Python packages.

If chat times out, check the loopback runtime and required model availability.
Do not automatically unload other workloads, change model defaults, or install
anything. Preserve failed evidence. A later successful request does not establish
steady reliability under shared GPU load.

## Evidence and remaining gates

- Unchanged baseline: 1,035 passed, one skipped.
- Missing-model regression reproduced the original constructor exception.
- Final changed suite: 1,041 passed, one skipped in 57.55 seconds; saved-blend
  guard's speech/startup suites: 117 passed. `git diff --check` passed. Edited
  files were written with LF; Git warned its existing settings may later convert
  them to CRLF. No Git configuration was changed.
- Real scratch embeddings, completed gateway chat, restart persistence and shutdown
  passed on two bounded retries. The initial live exchange produced an answer but failed
  its 90-second completion deadline.
- A real browser loaded the Chat page and showed `Voice unavailable`; both
  submitted live chat attempts timed out using the smoke client's 20-second
  request timeout. Browser turn reliability is not accepted. The normal launcher's
  client configuration was not changed. Ollama reported another loaded model
  during inspection; this does not establish the cause of the timeouts.
- Actual microphone recognition, audible TTS, production launch, owner-storage
  restart, real audio cancellation latency and repeatability under competing GPU
  workloads remain unverified. Synthetic playback/shutdown tests are not hardware
  acceptance. Unreal synchronization/packaging and coding execution remain separate.

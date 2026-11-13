# Freya live integration validation — 2026-10-09

Tested source head: `598823215de7b4e64f4217b5c0d524c8181f84a7`.

## Confirmed checks

- Existing Unreal Editor target build succeeded; UnrealBuildTool reported the
  target up to date. No C++ compile actions were required. The installed compiler
  is newer than Unreal's preferred version; no build failure resulted.
- The media UI interaction harness passed. This is a JavaScript harness result,
  not a real browser playback or visual-layout check.
- Scratch media catalog import passed actual Windows opened-handle validation.
  Range serving returned the requested WAV bytes with HTTP-style 206 status.
- A generated two-second WAV passed through the real decoder subprocess/PyAV.
  Its transcript then went through the real local broker-backed model summary
  pipeline; the analysis job completed and stored summaries. Recognition used
  a synthetic stub, so actual Whisper recognition/quality was not tested.
- Playback rejected the synthetic file after its contents were replaced.
- The rebuilt Unreal Editor game client paired with an isolated scratch gateway
  and obtained speech playback. Muted synthetic tone utterances received native
  completion acknowledgements, including after a forced WebSocket reconnect using
  the native session. The cancellation request path returned interrupted; audible
  silence and cancellation timing were not independently measured.
- The documented generated speech fixture ran through the real Unreal solver and
  exited successfully: `SOLVER PRODUCED ANIMATION`, 28 solver frames, peak curve
  1.000 over 260 curves, fixture length 10.50 seconds. This proves nonzero curve
  output in this run, not visible articulation or alignment quality.

## Timing result and incomplete gates

The fixture's heuristic timing diagnostic reported lips behind voice by 990 ms,
with match score 0.37. The end diagnostic reported an 830 ms difference. Low match
confidence and a single offscreen run prevent treating those as calibrated timing
measurements. Visual/audible synchronization still needs inspection and tuning;
the avatar's speech synchronization is not accepted by these checks.

The initial Unreal scratch startup did not reach playback within 90 seconds; its
log stopped in engine/shader initialization. A separate bounded retry connected
and passed the protocol checks. The original timeout remains part of the evidence.

Unreal was rendered offscreen. The native protocol test used muted synthetic tones;
the fixture test used the documented generated speech. No visual or acoustic quality
judgment was made. A newly packaged build, real browser codec playback, live Jellyfin,
YouTube rendering, actual Whisper recognition, offline operation, performance beside
all Freya workloads, and authenticated multi-user isolation remain unverified.

## Boundaries and evidence

Owner databases, media, credentials, recordings, and model files were not read by the
validation scripts. Scratch databases and policy rules were created under this chat's
work directory; real owner policy was not changed. Existing Unreal asset/solver
loading occurred through Unreal's normal runtime. No system security setting or
software installation was changed. Production Freya was not restarted.

The scratch gateway and test Unreal process were stopped. No tracked source/config
file changed during the checks. Prior Python evidence remains 1,031 passed, one
skipped; source behavior did not change, so it was not rerun for this documentation.

Machine-readable reports are saved with this chat's outputs:
`live-media-smoke.json`, `live-unreal-smoke.json` (initial timeout),
`live-unreal-smoke-retry.json`, and `live-unreal-fixture.json`.

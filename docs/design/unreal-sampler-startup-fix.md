# Unreal sampler startup fix — 2026-10-09

The owner observed no mouth movement in a visible generated-speech test. That
run confirmed zero solver frames and zero curves, despite successful playback.
This is a failed live validation, not a lip-sync acceptance result.

## Cause and change

Unreal's installed MetaHuman subject initially reports the pipeline completed.
Its Start method starts processing asynchronously on a lower-priority thread;
that thread later changes the state to running. The media sampler starts on a
separate normal-priority thread. Freya's sampler previously entered a
`while (IsRunning())` loop immediately, so it could exit before processing began.
Once gone, it never fed audio to the otherwise running solver.

Freya's custom probe subject now waits up to five seconds for pipeline startup.
An atomic stop flag ends both that wait and normal sampling during shutdown.
Its destructor joins shutdown before its sampler-owned fields are destroyed.
Timeouts produce an explicit probe error. Engine source, settings, and model
files were not modified. This does not add retries or enable deferred features.

## Verification

- Editor target rebuilt successfully: the changed C++ source compiled and linked.
  Existing engine deprecation/toolchain warnings remain.
- Fixed visible generated-speech test: successful exit, 81 solver frames, peak
  curve 1.000 over 260 curves. Owner visual/timing assessment is pending.
- Two further offscreen generated-speech trials: both successful exits, each
  525 solver frames and peak curve 1.000 over 260 curves.
- All three runs logged that the pipeline was running before sampling.
- The visible timing estimate was 0 ms lag, match 0.38. Offscreen estimates were
  70 ms ahead, match 0.09. These low-match diagnostics do not establish calibrated
  timing; no playback delay was changed.

Normal test shutdown completed. Forced startup timeout and shutdown specifically
during the wait were not independently reproduced. Repeated successes do not prove
rare-event reliability. A new packaged build, visual alignment, actual Whisper,
and real browser playback remain unverified. The unchanged Python suite evidence
remains 1,031 passed and one skipped; it was not rerun for this C++-only fix.

Production Freya was left running. The tests used the documented generated speech
fixture in separate Unreal processes; owner recordings/databases/logs were not read.
There was no merge, policy change, system install, or production restart.

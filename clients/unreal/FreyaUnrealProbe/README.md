# Freya Unreal feasibility probe

This is an isolated experimental C++ project, not Freya's GUI. It has no backend
connection, microphone input, chat screen, room, MetaHuman character or automatic
audio playback. It uses Epic's installed runtime APIs directly; the Fab LLM/TTS
plugin is not needed. No Epic source or third-party character assets are bundled.

## Verified installation and build status

- Unreal 5.8.3: `D:\UE_5.8`.
- MSVC 14.51.36231 and SDK 10.0.26100.0 detected. Unreal warns that this compiler
  is newer than its preferred 14.50 version; it is not yet a confirmed problem.
- MetaHuman Optional texture-synthesis `.ar` and body texture `.uasset` files are
  present, matching the installed Creator module's content-detection checks.
- UHT generation succeeded on the first attempted Game build. C++ compilation
  and linking have **not** succeeded. The local UBA executor aborts on sandbox
  disk-volume enumeration. Thus neither runtime nor packaged solver is verified.

## Next step: compile outside the Codex sandbox

In a normal PowerShell terminal, from this folder:

```powershell
.\Build-Probe.ps1 -Target Editor
```

The script uses the installed engine, writes project build output and a project
local accelerator cache, limits compilation concurrency to four actions, and
does not install software or modify engine source. Keep any compiler errors.
After it succeeds, open `FreyaUnrealProbe.uproject` in Unreal.

## Create the first character and test level

Enable **MetaHuman Creator** in Edit > Plugins (the installed plugin descriptor
is named MetaHumanCharacter); restart. Create a MetaHuman Character asset using
the Content Browser. Begin with an included preset, one outfit, and an optimized
assembly. Review actual asset terms for commercial redistribution. Save a small
test level containing that assembled actor, a camera and lighting. Add **Freya
Audio Probe** to the actor and set its Face animation Live Link subject to
`FreyaAudioProbe` according to that character's animation setup.

Prepare a short synthetic Kokoro speech WAV: 24 kHz mono, signed 16-bit PCM, at
most 30 seconds. Use an owner-directed generated clip, not a personal recording.
Convert it with Python:

```powershell
python .\prepare_fixture.py C:\path\speech.wav C:\path\speech.f32
```

Call `Initialize Probe`, then `Start Fixture` with the absolute `.f32` path from
the level Blueprint/component. Registration does **not** prove solver readiness;
if the first call fails while the subject is being created, retry manually after
initialization. Do not automate unbounded retries. Feed one fixture at a time.

The sampler supplies up to 20 ms of full-level audio per packet at real-time
cadence. Inspect Live Link output and the visible face. Input draining is only a
sampler observation, not audible completion or proof of nonzero animation.
`Stop Probe` removes the source; normal EndPlay also removes it.

## What remains before acceptance

1. Resolve C++ compilation errors against installed 5.8.3, if any.
2. Establish solver model loading, curve production and visible articulation.
3. Add calibrated audible playback, cancellation and completion measurements.
4. Cook/package this saved level with its character and required solver content.
5. Repeat the speech test with the Editor closed, including offline operation.
6. Record frame time, incremental VRAM/RAM and concurrent Freya workload impact.

Only after that gate should we add real backend chat and the approved room with
an image display. Chat and essential controls remain readable screen overlays.
Closing the eventual GUI must leave the backend running with the mic muted;
reconnecting must not silently unmute. Avatar-off must keep the GUI usable.

The known VRM identity/extraction issues in the Freya repository remain separate
open fixes. This probe does not accept imported VRM/archive assets or fix them.

## Update, 2026-10-08 (Claude)

Verified today, with how:

- **The Game target builds and links** (`Build-Probe.ps1 -Target Game`, result
  "Succeeded", `Binaries/Win64/FreyaUnrealProbe.exe`). `MetaHumanPipelineCore` is
  typed `Editor` in `MetaHumanCoreTech.uplugin`, but Epic ships precompiled
  `UnrealGame` objects for it and the Runtime module
  `MetaHumanLocalLiveLinkSource` depends on it. Linking is therefore not the
  obstacle. **Whether the solver runs in a cooked build is still untested.**
- The Editor target also rebuilt after the changes below.
- The real-time solver's model is an asset in Epic's **StreamingADA** plugin
  (`Engine/Plugins/Animation/AudioDrivenAnimation/StreamingADA/Content`, 51 MB,
  beta), loaded by path at run time. Assets loaded by path are not cooked unless
  told to be: add `/StreamingADA` to "Additional Asset Directories to Cook"
  before packaging, or the packaged solver will have no model.
- The character asset `Content/Freya.uasset` exists and loads. **It is not
  rigged and not assembled.** `can_build_meta_human` reports "Character is not
  rigged". There is no `Content/MetaHumans` folder.

Changed in `FreyaAudioProbeComponent` (originals kept by Claude; ask if wanted):

- Auto start: with `bAutoStart` (default on) the component initializes at
  BeginPlay and starts `FixturePath` once after `AutoStartDelaySeconds`. No
  retries. A relative path is taken from the project folder;
  `-FreyaFixture=<path>` on the command line replaces it.
- Audible playback through a procedural sound wave, started
  `AudibleDelayMilliseconds` after the solver is fed. `Volume` applies to the
  sound only; the solver always gets full-level audio.
- `CancelSpeech`: stops feeding the solver and silences playback.
- Evidence in the log, the same in the Editor and in a packaged run: the
  component counts results the solver hands back and reads the subject's curve
  values, and ends each fixture with one line beginning `RESULT:`
  (`SOLVER PRODUCED ANIMATION`, `SOLVER RAN BUT CURVES STAYED NEAR ZERO`, or
  `NO SOLVER OUTPUT`). This shows curves were produced; it does not show the
  face moved or that lips match the voice. Those need eyes.

Fixture: `Fixtures/freya_test.wav` and `.f32`, 10.5 s of Kokoro `af_heart`
speech generated for this test (not a recording of a person).

Next, in order:

1. Owner, in MetaHuman Creator: rig the character (the Creator's rigging step;
   it needs the owner's Epic sign-in), then Assembly > UE Optimized > Assemble,
   then Save All.
2. Test level with the assembled actor, camera, light; add Freya Audio Probe;
   set the face's Live Link subject to `FreyaAudioProbe`. Press Play: the
   fixture starts by itself after three seconds.
3. Read the `RESULT:` line and watch the face.
4. Add `/StreamingADA` to the cook, package, and repeat with the Editor closed.

## Result, 2026-10-08 (Claude): the solver works in a packaged build

Done by script, with the owner's go-ahead (`ue_assemble.py`, `ue_level.py` in
Claude's scratch folder; ask for copies):

- Rigged (joints only), downloaded 2k texture sources, and assembled with the UE
  Optimized pipeline through `MetaHumanCharacterEditorSubsystem`. Rigging and
  textures used Epic's service under the owner's sign-in, completed in the
  browser. Output: `Content/Unpacked/Freya/BP_Freya` (257 MB).
- `Content/Maps/ProbeLevel`: the character, a camera, two lights, fixed
  exposure, and the probe component. It is the project's default map.

Measured:

| Run | Result line |
|---|---|
| Editor binaries, `-game` | `SOLVER PRODUCED ANIMATION. solver frames 525, peak curve 1.000 over 260 curves` |
| **Packaged Win64 build, Editor not running** | the same: 525 frames, 260 curves |

Screenshots taken during both runs (`Saved/Screenshots/probe_sheet.png`, and the
same under `Packaged/Windows/FreyaUnrealProbe/Saved`) show her mouth open in
different shapes at four moments, and a blink.

What made it work, beyond the first version of the probe:

- **The face has to be put on Epic's `ABP_MH_LiveLink` animation blueprint and
  given the subject name.** Setting `UseLiveLink` and `LiveLinkSubject` on the
  placed actor was not enough: at run time the face was still on `ABP_Face` and
  the blueprint's subject variable held a leftover value. The component now does
  this itself (`bDriveFaceDirectly`) and logs what it found.
- `/StreamingADA` is in the cook list (`Config/DefaultGame.ini`), so the solver's
  model is in the package.
- Packaging: `RunUAT BuildCookRun ... -cook -stage -pak -archive`, 5 min 54 s,
  2.3 GB.

Flags for unattended runs: `-FreyaShots` (four screenshots), `-FreyaQuit` (exit
after the result), `-FreyaFixture=<path>`.

Still **not** verified:

- Lip-to-voice timing. Audio played, but nobody has judged the alignment or tuned
  `AudibleDelayMilliseconds`.
- `CancelSpeech` in a real run.
- Performance beside Ollama, Whisper, and ComfyUI; VRAM use; frame caps.
- Offline operation (network blocked).
- Whether the leftover subject value and the unused blueprint path matter for
  the eventual design, or the direct approach is simply the right one.

## Live connection to Freya, 2026-10-08 (Claude)

The project now lives in the Freya repository at `clients/unreal/FreyaUnrealProbe`
(source, config, and scripts only; `Content`, builds, and fixtures are ignored).

When Freya starts this window (`FREYA_UNREAL_CLIENT` names the program to run),
she passes her gateway's port and a single-use pairing key in the environment.
The probe component then connects instead of playing a fixture:

- `FreyaLink` opens a WebSocket to `ws://127.0.0.1:<port>/ws` with the key in an
  `Authorization` header, says which protocol version it speaks, and asks to play
  her speech.
- Freya's speech arrives a sentence at a time (24 kHz, 16-bit, in frames of at
  most a tenth of a second) and goes to the solver and the speakers together.
  The window reports back when each sentence has finished sounding; Freya keeps
  her microphone ignoring it until then.
- A chat overlay at the bottom of the window shows the conversation (read-only,
  selectable) and has a box to type in. A line starting with `/` is sent as one
  of Freya's commands.
- The window opens at about three fifths of the display's height, resizable, and
  never fullscreen unless `-fullscreen` is given.

Verified by running real Freya (scratch data) with this window started from the
Editor's program in game mode: paired, playback granted, four sentences spoken
through the window and each reported finished.

Not verified: how the chat overlay looks (nobody has looked at it yet), typing in
it, reconnection, and cancelling mid-sentence.

**Packaging is currently blocked on this PC.** Windows Smart App Control (on)
refuses to load the small rules file Unreal's packaging tool compiles each run
(`UATRules-*.dll`, "An Application Control policy has blocked this file"). It
packaged three times earlier the same day. This is a Windows security setting
and the owner's to decide; until then use `Run-FromEditor.cmd`.

To try it:

    $env:FREYA_UNREAL_CLIENT = "C:\Dev\Freya\clients\unreal\FreyaUnrealProbe\Run-FromEditor.cmd"
    freya

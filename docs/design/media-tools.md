# Media tools: first implementation

Implemented on `feat/media-tools`, based on `feat/unreal-client` at `4f6e6f5`. Claude implemented most of the media backend; Codex researched the integration, finished the client and application wiring after Claude reached its session limit, and reviewed resource limits, cancellation, provenance, and authorization. This is an alpha implementation, not an assertion that the Foundation specification or any ADR has been approved.

## Delivered behavior

- A Media tab with configured-folder scans and imports, local audio/video playback, tags, timestamped notes, owner-confirmed skip intervals, transcript import, searchable media knowledge, JSON/Markdown memory exports, and background analysis job status/cancellation.
- Jellyfin library browsing, paginated search, catalog additions, bounded authenticated direct playback for suitable containers, and an explicit handoff to Jellyfin's own player for unsupported formats/codecs. Tokens stay server-side in headers; redirects are refused.
- Strict YouTube video-link bookmarks and visible privacy-enhanced embeds. Search uses the official Data API when configured; otherwise an external YouTube search link is labeled as external. The embed is removed when changing tabs or hiding the page. YouTube timestamps for notes are entered manually; seek actions reload its player at the selected time.
- Downr compatibility through completed files saved to a designated downloads folder. Freya scans and imports those files; this release does not control Downr, scrape its site, or call an undocumented API. Partial downloads and files modified within fifteen seconds are skipped. Importing does not copy or alter the original file; keeping the configured folder and file is required for later playback.
- Opt-in automatic speech analysis when imported local media is prepared for playback. Native audio is decoded in a trusted helper process in bounded thirty-second chunks, then transcribed with the already-loaded local speech recognizer. Recognition is serialized with microphone transcription and admitted through the turn gate. Summaries use the existing broker-backed model runtime. The helper uses PyAV already present in the lockfile; its direct dependency is declared explicitly.
- A separate media database and source-labeled recall capability. Transcripts, model inference, and owner notes retain distinct evidence/trust labels. Exports preserve original source references and timestamps. Recall is available to the model only after the owner grants it. Questions recognized as asking about the past also use an injected, broker-authorized media lookup; Core does not import the media implementation.
- Logical deletion immediately removes serving/search records, cancels active jobs, invalidates playback tickets, and deletes controlled exports. It retains a minimal tombstone and the owner's external original. SQLite secure deletion is enabled; physical erasure from storage/OS caches/backups is not claimed.

## Owner setup

Do these steps after reviewing and bringing the feature branch into the app checkout. The coding session has not changed the live owner configuration or databases.

1. Set startup environment variables for the folders and services to use:

   - `FREYA_MEDIA_ROOTS`: media directories separated by the OS path separator (`;` on Windows).
   - `FREYA_MEDIA_DOWNLOADS`: a dedicated folder where Downr or another downloader saves completed files. Freya does not assume an existing browser's download location or infer original video URLs from filenames.
   - `FREYA_JELLYFIN_URL`: the configured server address, optionally including a reverse-proxy base path.
   - `FREYA_JELLYFIN_TOKEN`: a server access token, supplied privately through the runtime environment. Never commit or paste it into chat or source. A user-scoped token may require `FREYA_JELLYFIN_USER_ID`.
   - `FREYA_JELLYFIN_USER_ID`: optional user ID for that user's views and items.
   - `FREYA_YOUTUBE_API_KEY`: optional official search key; external search works without it.

2. Inspect and explicitly grant the new media permissions from the reviewed app checkout using its existing Python environment:

   ```powershell
   .\.venv\Scripts\python.exe -m freya.media.activate
   .\.venv\Scripts\python.exe -m freya.media.activate --grant all
   ```

   The first command displays permission state. The second is an owner action that adds media allow rules. Existing deny rules continue to win. Select individual capability names instead of `all` for narrower access. The implementation never grants itself these permissions at startup. Restart Freya after granting recall so the model's tool list includes it.

3. Start Freya, open Media, choose **Find downloaded media**, and import completed files. To analyze local audio/video, start with the existing speech-recognition model loaded; otherwise supply `.srt`, `.vtt`, or text captions. Imported captions can be analyzed without loading speech recognition.

4. Use **Analyze speech** or enable **Analyze imported media when I play it**. Read the coverage label. An item already analyzed is not automatically reanalyzed on every playback; explicitly analyze again when desired. Replacing a transcript invalidates earlier summaries and cancels prior work. A file replaced on disk must have its old catalog record removed before importing again.

Media files/knowledge are stored under the app's existing private data directory at runtime. This work has not opened or inspected that directory. No media is sent to a cloud model by this implementation; model requests follow the configured broker-backed runtime.

## Limits and next implementation stages

- Visual/action recognition is not implemented. Coverage explicitly says speech-only; a vision-capable provider with bounded frame sampling and GPU admission remains the next stage. Summaries cannot truthfully describe unwitnessed actions.
- There is no direct downloader, browser recorder, screen watcher, or automatic ad detector. Downr/manual downloads are imported separately. Skip intervals apply to local/Jellyfin playback and leave the original file unchanged; exporting an edited video is a later stage.
- Official YouTube playback retains ads and controls. It cannot provide a hidden analysis stream or in-app recording. Supplied captions or separately imported permitted files are the analysis inputs. Embedding may be disabled by the uploader or rejected by YouTube's client/referrer requirements; there is no bypass.
- This release searches YouTube or Jellyfin, not arbitrary web providers. Additional discovery providers can use the same service boundary later.
- Native transcription is capped at four hours and summaries at forty-eight transcript chunks. Coverage reports truncation. The decoder refuses discontinuous audio timelines to avoid misleading timestamps. Whisper chunk boundaries may lose word continuity; exact word-level alignment/diarization is not implemented.
- Local byte serving accepts one range at a time and returns at most 4 MiB per response. Large non-range requests receive 416 rather than a truncated 200. Codecs and browser range behavior need live playback smoke checks. Jellyfin transcoding and resumable playback history are not implemented.
- A separate decoder process contains native decoder crashes; it is not an OS security-isolation boundary. It decodes a checked local file with a restricted protocol list. Do not claim Foundation-compliant isolation of arbitrary hostile media.
- Current ownership remains Freya's existing single `local-owner` alpha. Full authenticated multi-user sessions/domain isolation are not implemented by this change.
- Production file access fails closed unless the actual opened file handle can be verified. Windows sandbox restrictions prevent exercising that OS call in this coding session; synthetic-file tests inject it. Verify it on the normal Windows app host before enabling native imports/analysis.

## Validation

Final validation: **888 passed, 1 skipped, 4 deselected** in the broader Python suite. Node's interaction harness passed and exercises the actual Media script for import, Jellyfin paging, discovery results, safe source text, playback construction, and clearing hidden players. `uv lock --check --offline` passed; JavaScript syntax checks passed. New tests use temporary stores, synthetic media, fake model responses, and fake network transports. They check timestamped recall/export, permission revocation, requester-only replies, malformed URLs, atomic transcript replacement, cancelled/deleted-job publication, restart authorization, range errors, and bounded audio chunks.

The new direct PyAV requirement uses the already pinned artifact/version. Full `uv lock` regeneration encountered the sandbox's path-canonicalization restriction while reading an existing dependency's metadata; the root dependency/requirements entries were updated without changing any pinned package, then the lockfile consistency check succeeded.

Run from the reviewed checkout:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m pytest -q
node tests/media_ui_harness.js
uv lock --check --offline
```

The final coding-session report records actual test counts and exclusions. Four existing headless-Edge fixture cases did not execute successfully in this sandbox even before media changes. Test temporary files must be placed in a writable workspace directory here. Live Jellyfin credentials/playback, YouTube rendering, loaded-Whisper/model quality, actual Windows handle validation, and visual layout have not been verified. The native decoder algorithm was tested on a synthetic WAV, with the sandbox-blocked handle boundary injected and the subprocess transport faked; this is not a live media/model smoke test.

## Research

- [Jellyfin SDK](https://typescript-sdk.jellyfin.org/) and [current views routes](https://github.com/jellyfin/jellyfin/blob/master/Jellyfin.Api/Controllers/UserViewsController.cs).
- [YouTube embedded-player requirements](https://developers.google.com/youtube/terms/required-minimum-functionality) and [API policies](https://developers.google.com/youtube/terms/developer-policies).
- [Downr's documentation-only repository](https://github.com/downr/downr). No supported callable Downr API was identified.
- [Whisper timestamp support](https://github.com/SYSTRAN/faster-whisper), [PyAV audio](https://pyav.basswood.io/docs/stable/api/audio.html), and [Ollama vision inputs](https://docs.ollama.com/capabilities/vision).

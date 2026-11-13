# Freya media implementation plan

Status: implementation brief, 2026-10-08. Claude leads coding; Codex researches and reviews. No live personal media, credentials, memory databases, or recordings are included in this work.

## Intended experience

A Media tab combines an imported library, Jellyfin browsing, discovery, playback, an analysis queue, and timestamped notes. Adding a link and acquiring a file are separate operations. The owner can opt into analyzing imported videos as they are played. A media memory is a distinct source, never a conversation or a statement made by the owner.

## Architecture and implementation sequence

1. Build the catalog and Media tab, timestamped notes, collections/tags, source identity, local playback, and a separate media-memory store. Keep new persistence in a separate SQLite database; do not alter the existing personal-memory schema. Use canonical IDs, transactions, schema versions, indexes, and atomic file exports.
2. Add Jellyfin as a replaceable adapter: connect/test, paginated libraries/items/search, select an item, and obtain playback information. Keep credentials server-side and out of URLs, events, logs, and exported memories. Use authenticated same-origin playback routes or an explicitly documented Jellyfin-client handoff where the current gateway cannot stream efficiently. Do not buffer an entire movie in a gateway HTTP response.
3. Add discovery and YouTube link playback. Use official YouTube search when configured, with a clearly labeled external-search fallback rather than scraping search pages. Bookmark a result before downloading. Use narrow iframe CSP permissions and a player-specific referrer policy. Pause/remove the embed when its tab is hidden. Handle disabled embedding, browser codec support, and source errors honestly.
4. Add explicit acquisition of permitted media through a replaceable downloader. Detect an owner-installed downloader and FFmpeg; do not install tools or models automatically. Treat direct-file sources and platform links separately. A YouTube acquisition adapter must be separate from official embedded-player/API behavior, disclose source restrictions, and never use owner browser cookies, evade DRM, or work around restricted access. No assertion that public visibility is permission to download.
5. Implement a bounded local analysis queue. Preserve Whisper segment start/end times; create summaries and timestamped claims through the existing authorized ModelRuntime. Analyze in chunks with bounded inputs. Save partial work and expose queued/running/completed/failed/cancelled states. Cancel cooperatively, limit concurrency, and reconcile interrupted jobs on restart.
6. Add visual analysis through an explicit vision-capable provider. Use bounded, downscaled timestamped frame samples and report sampling coverage. A transcript supports claims about speech; sampled frames support observations at those moments. Neither supports an assertion that Freya continuously observed every action. Missing vision capability yields transcript-only analysis with a visible coverage label. Reuse resource admission and serialize large GPU work; never silently download a model.
7. Add local segment skipping and, later, optional export of an edited derivative. Keep original files unchanged. Preserve a mapping from derivative time to original time so memory citations remain valid. User-marked advertisement/sponsor intervals are distinct from uncertain automated suggestions. Official YouTube embeds retain their advertisements and controls.

## Contracts and practices

- Use the existing provider registry and capability broker for new protected operations. Define narrow catalog, discovery, Jellyfin, playback, acquisition, analysis, recall, and deletion capabilities; register integrations explicitly. Never modify existing policy rows or migrations. If new capability grants are needed, document owner activation instead of silently granting them.
- Offer read-only media recall to the model. Acquiring, analyzing, and deleting media stay tied to explicit owner requests or an opt-in auto-analysis setting, rather than model-invented effects.
- Inject provider interfaces, paths, clocks, download transports, subprocess runners, and model calls so tests use fakes and temporary storage. Reuse the project's standard library and existing dependencies before adding infrastructure.
- Use argument arrays for subprocesses, never shell-built commands. Bound time, bytes, duration, file count, response size, frame count, and model context. Restrict FFmpeg network protocols for local analysis and reject playlists and external-reference formats as local imports.
- Resolve local files only within configured media roots, reject traversal and unsafe reparse/symlink targets, and revalidate the actual file at use time. Serve opaque IDs rather than arbitrary paths. Treat all source text as untrusted data and display it with textContent.
- A download transport must validate destinations and every redirect, prevent arbitrary access to private/local services, and avoid credential forwarding. Jellyfin is a separately configured trusted server destination, not a generic URL proxy.
- Persist imports only when complete: use a staging file, verify limits and expected media, then atomically publish. Preserve source URL, acquisition method, content hash, import time, and file identity. Deduplicate repeated acquisition requests and clean up partial files.
- Never send media or derived text to cloud services without an explicit configured provider choice. Describe the current single-owner alpha honestly; do not claim multi-user isolation or spec compliance that the current runtime does not implement.

## Media memory record

Use a versioned canonical record with media_id, source_type=media, original source URL/provider/item ID, content hash where available, title, duration, owner/domain, creator, trust=untrusted-source, permitted uses, created/analyzed times, analysis model/version, coverage, and derivation lineage. Store transcript segments, visual observations, summaries, and owner notes separately. Every observation/claim should carry source timestamps and its evidence type: speech, visual sample, owner note, or inference. Inference must not overwrite an explicit owner fact.

SQLite FTS5 initially indexes titles, transcript segments, notes, and summaries. Results include the original source and seek time. Markdown/JSON exports are canonical portable memory files with stable IDs, not flattened owner facts. Embeddings can be added as a replaceable index after lexical search works.

Deleting a media record immediately removes it and its derivatives from recall and export serving, cancels queued analysis, and schedules or performs purge of controlled files/indexes/exports. Keep a minimal tombstone; do not delete the owner's original external file. Check deletion state again before a running worker publishes results.

## Acceptance tests

- Fake Jellyfin transport: authentication redaction, pagination, search, server errors, and allowed destination handling.
- Catalog: duplicate imports, interrupted import, finite timestamps, malformed metadata, bounded search, persistence/reopen, and source labels.
- Gateway: session/Host/Origin enforcement on media requests and byte routes; private answers only to the requesting window; range handling if streaming is implemented; no arbitrary path or URL proxy.
- Jobs: bounded queue, cancellation, restart reconciliation, model failures, timestamp preservation, chunk coverage, and deletion during analysis.
- Recall/export: exact provenance and seek times, source trust preserved, prompt-injection text stays data, and deleted sources never return through derived indexes.
- UI: safe text rendering, accessible controls, visible job/errors/coverage, reconnect behavior, and honest unavailable states.
- Run focused tests and the existing suite once changes settle. Live Jellyfin, downloads, browser playback, and model analysis require separately reported smoke checks; fake tests do not establish these.

## Research references

- Jellyfin official SDK and stream request contract: https://typescript-sdk.jellyfin.org/ and https://typescript-sdk.jellyfin.org/interfaces/generated-client.VideoApiGetVideoStreamRequest.html
- Jellyfin OpenAPI source: https://github.com/jellyfin/jellyfin-sdk-kotlin/blob/master/openapi.json
- YouTube embed integration and referrer/visibility requirements: https://developers.google.com/youtube/iframe_api_reference and https://developers.google.com/youtube/terms/required-minimum-functionality
- YouTube API constraints on downloading, ads, and background playback: https://developers.google.com/youtube/terms/developer-policies
- YouTube search API: https://developers.google.com/youtube/v3/docs/search/list
- Whisper segment/word timestamps: https://github.com/SYSTRAN/faster-whisper
- Vision inputs require a vision model: https://docs.ollama.com/capabilities/vision
- FFmpeg subprocess/media/protocol controls: https://ffmpeg.org/ffmpeg.html and https://ffmpeg.org/ffmpeg-protocols.html
- Optional acquisition adapter behavior and executable usage: https://github.com/yt-dlp/yt-dlp

Source facts guide the design; the sequence, interfaces, persistence choices, and limits above are engineering recommendations for Freya, not requirements quoted from those external sources.

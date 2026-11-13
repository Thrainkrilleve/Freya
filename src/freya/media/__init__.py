"""
Freya's media library: local files, Jellyfin, YouTube bookmarks,
timestamped notes, and analysis of what was said in a video.

This is a client-side feature, not part of Core. Core must not import
it; Freya reaches it through the capabilities it registers, and the
model reaches it only through the read-only `media.recall` tool.
"""

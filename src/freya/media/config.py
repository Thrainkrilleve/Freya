import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple


DATA_DIR = Path(__file__).resolve().parents[3] / "data"
MEDIA_DIR = DATA_DIR / "media"
MEDIA_DATABASE_PATH = MEDIA_DIR / "freya_media.db"
EXPORT_DIR = MEDIA_DIR / "exports"

# At most this many folders may be configured as media roots.
MAX_ROOTS = 16


@dataclass(frozen=True)
class MediaConfig:
    """
    Where media may come from. Everything is read from environment
    variables when Freya starts; nothing here is written back.

        FREYA_MEDIA_ROOTS       folders Freya may scan and play from,
                                separated by ';' on Windows
        FREYA_MEDIA_DOWNLOADS   the folder a downloader (such as the
                                Downr browser extension) saves to; it
                                is scanned like a root
        FREYA_JELLYFIN_URL      e.g. http://192.168.1.20:8096
        FREYA_JELLYFIN_TOKEN    a Jellyfin API key; kept server-side
        FREYA_JELLYFIN_USER_ID  optional, the user whose libraries to show
        FREYA_YOUTUBE_API_KEY   optional, for official YouTube search
    """

    roots: Tuple[Path, ...] = ()
    downloads: Optional[Path] = None
    jellyfin_url: Optional[str] = None
    jellyfin_token: Optional[str] = field(default=None, repr=False)
    jellyfin_user_id: Optional[str] = None
    youtube_api_key: Optional[str] = field(default=None, repr=False)

    @property
    def scan_roots(self) -> Tuple[Path, ...]:
        """
        Every folder that may be scanned, the downloads folder last.
        """
        roots = list(self.roots)

        if self.downloads is not None and self.downloads not in roots:
            roots.append(self.downloads)

        return tuple(roots[:MAX_ROOTS])


def _folders(value) -> Tuple[Path, ...]:
    if not value:
        return ()

    folders = []

    for part in str(value).split(os.pathsep):
        part = part.strip().strip('"')

        if part:
            folders.append(Path(part))

    return tuple(folders[:MAX_ROOTS])


def load_media_config(environ=None) -> MediaConfig:
    environ = os.environ if environ is None else environ
    downloads = _folders(environ.get("FREYA_MEDIA_DOWNLOADS"))

    return MediaConfig(
        roots=_folders(environ.get("FREYA_MEDIA_ROOTS")),
        downloads=downloads[0] if downloads else None,
        jellyfin_url=(environ.get("FREYA_JELLYFIN_URL") or "").strip() or None,
        jellyfin_token=(environ.get("FREYA_JELLYFIN_TOKEN") or "").strip() or None,
        jellyfin_user_id=(environ.get("FREYA_JELLYFIN_USER_ID") or "").strip() or None,
        youtube_api_key=(environ.get("FREYA_YOUTUBE_API_KEY") or "").strip() or None,
    )

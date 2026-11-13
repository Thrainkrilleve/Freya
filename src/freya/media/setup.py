from freya.media.analysis import Analyzer
from freya.media.config import EXPORT_DIR, MEDIA_DATABASE_PATH, load_media_config
from freya.media.discovery import YouTubeDiscovery
from freya.media.export import MediaExporter
from freya.media.jobs import AnalysisQueue
from freya.media.service import MediaService, jellyfin_from_config
from freya.media.store import MediaStore
from freya.media.whisper import WhisperTranscriber


def build_media_service(
    broker,
    chat,
    model_name,
    speech_model=None,
    config=None,
    database_path=MEDIA_DATABASE_PATH,
    export_dir=EXPORT_DIR,
    permissions=None,
    transport=None,
    admission=None,
) -> MediaService:
    """
    Assemble the media library and start its analysis worker.

    `chat` is the broker-backed model runtime's chat; `speech_model`
    returns the loaded faster-whisper model, or None when there is none.
    """
    config = config or load_media_config()
    store = MediaStore(database_path)
    service = None

    analyzer = Analyzer(
        store,
        chat=chat,
        model_name=model_name,
        transcriber=WhisperTranscriber(speech_model, admission=admission) if speech_model else None,
        resolve_file=lambda record: service.resolve_file(record),
    )
    queue = AnalysisQueue(store, analyzer, authorize=lambda: bool(permissions and permissions().get("media.analyze")))
    jellyfin, jellyfin_error = jellyfin_from_config(config, transport)
    discovery = (
        YouTubeDiscovery(config.youtube_api_key, transport)
        if transport
        else YouTubeDiscovery(config.youtube_api_key)
    )

    service = MediaService(
        broker,
        store,
        config,
        queue,
        MediaExporter(export_dir),
        analyzer,
        jellyfin=jellyfin,
        jellyfin_error=jellyfin_error,
        discovery=discovery,
        permissions=permissions,
    )
    queue.start()

    return service

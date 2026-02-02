class MediaService:
    """
    Service for handling media extraction and processing.
    
    This service will coordinate between the API/CLI, the Source Resolver,
    and the Extractor Registry to identify media and prepare it for download.

    RESPONSIBILITIES:
    - Orchestrate the "Detection -> Extraction" pipeline.
    - Return actionable metadata (ExtractResult) to the caller.
    - It does NOT perform file downloads.
    - It does NOT manage file system I/O.
    """
    
    def __init__(self, config_repo=None):
        # Placeholder for dependencies (e.g. registry)
        from dlm.extractors.youtube.extractor import YouTubeExtractor
        from dlm.extractors.tiktok.extractor import TikTokExtractor
        from dlm.extractors.facebook.extractor import FacebookExtractor
        from dlm.extractors.spotify.extractor import SpotifyExtractor
        from dlm.extractors.torrent.extractor import TorrentExtractor

        # Simple list for Phase 1. Ideally injected or via registry.
        self.extractors = [
            YouTubeExtractor(),
            TikTokExtractor(),
            SpotifyExtractor(config_repo=config_repo),
            TorrentExtractor(),
            FacebookExtractor(),
        ]

    def extract_info(self, url: str, limit: int = None):
        """
        Main entry point for extracting media info.
        """
        for extractor in self.extractors:
            if extractor.supports(url):
                # Check if extractor supports limit
                import inspect
                sig = inspect.signature(extractor.extract)
                if 'limit' in sig.parameters:
                    return extractor.extract(url, limit=limit)
                return extractor.extract(url)
        return None

    def resolve_stream_url(self, url: str, media_type: str = 'video') -> str:
        """Resolve direct stream URL."""
        for extractor in self.extractors:
            if extractor.supports(url):
                if hasattr(extractor, 'resolve_stream_url'):
                    return extractor.resolve_stream_url(url, media_type)
        return url # Return original if no resolution support (fallback)

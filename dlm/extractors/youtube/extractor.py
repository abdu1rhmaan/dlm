import re
from typing import List, Dict, Any
from ..base import BaseExtractor
from ..result import ExtractResult
from .models import YouTubeMetadata

class YouTubeExtractor(BaseExtractor):
    """YouTube media extractor."""
    
    def supports(self, url: str) -> bool:
        return "youtube.com" in url or "youtu.be" in url

    def extract(self, url: str) -> ExtractResult:
        try:
            import yt_dlp
        except ImportError:
            raise NotImplementedError("yt-dlp is not installed.")

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'best',
            'skip_download': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Resolve metadata ONLY
            # extract_flat=True allows fast playlist resolution without expanding formats for every item
            info = ydl.extract_info(url, download=False, process=False) 
            
            # If process=False, we get raw info.
            # If it's a playlist, we might need more handling.
            # Actually, standard is extract_flat='in_playlist' or True.
            
        # Re-run with flat extraction if we didn't get what we wanted or to be safe
        ydl_opts['extract_flat'] = True
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # 1. Detect Playlist
        if info.get('_type') == 'playlist':
            return ExtractResult(
                platform="youtube",
                source_url=url,
                metadata=YouTubeMetadata(
                    video_id=info.get('id'),
                    title=info.get('title'),
                    duration=0
                ),
                is_collection=True,
                entries=info.get('entries', [])
            )

        # 2. Single Video
        # Metadata extraction
        metadata = YouTubeMetadata(
            video_id=info.get('id'),
            title=info.get('title'),
            duration=float(info.get('duration') or 0)
        )
        
        # Analyze available formats for quality filtering
        formats = info.get('formats', [])
        
        # Detect ORIGINAL maximum resolution
        max_height = 0
        for f in formats:
            h = f.get('height')
            if h and h > max_height:
                max_height = h
        
        # Standard qualities
        standard_qualities = [2160, 1440, 1080, 720, 480, 360, 240, 144]
        available_streams = []
        
        # Filter: resolution <= original max, NO container restriction
        for q in standard_qualities:
             if q <= max_height:
                 available_streams.append({
                     'type': 'video', 
                     'quality': f"{q}p",
                     'height': q,
                     # 'ext': 'mp4'  <-- REMOVED: Allow any container (webm, mkv, mp4)
                 })

        # Audio stream (mp3 target) - always available
        available_streams.append({
            'type': 'audio',
            'quality': 'best',
            'ext': 'mp3'
        })

        return ExtractResult(
            platform="youtube",
            source_url=url,
            metadata=metadata,
            streams=available_streams, 
            is_collection=False
        )

    def resolve_stream_url(self, url: str, media_type: str = 'video') -> str:
        """Resolve direct stream URL for yt-dlp consumption."""
        import yt_dlp
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': 'bestaudio/best' if media_type == 'audio' else 'bestvideo+bestaudio/best'
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # 1. Direct URL (non-adaptive)
            if info.get('url'):
                return info['url']
            
            # 2. Adaptive Formats (requested_formats)
            # When we request bestvideo+bestaudio, we get two streams.
            # For FFmpeg input(-i), we typically want the video stream or the one that contains the main content.
            # If we just return the video URL, FFmpeg might miss audio if it's separate.
            # However, for cut operations, we usually want the video stream if available.
            # Ideally, we should check what we need. 
            # If media_type is video, we prefer video stream.
            
            if info.get('requested_formats'):
                req = info['requested_formats']
                # Search for video stream first if media_type is video
                if media_type == 'video':
                     for f in req:
                         if f.get('vcodec') != 'none' and f.get('url'):
                             return f['url']
                
                # Fallback to audio or first available
                for f in req:
                    if f.get('url'):
                        return f['url']
            
            return None

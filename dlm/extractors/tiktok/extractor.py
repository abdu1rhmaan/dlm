import re
from ..base import BaseExtractor
from ..result import ExtractResult
from .models import TikTokMetadata

class TikTokExtractor(BaseExtractor):
    """TikTok media extractor."""
    
    def supports(self, url: str) -> bool:
        """Check if URL is a TikTok video."""
        return "tiktok.com" in url.lower() or "vm.tiktok.com" in url.lower()

    def extract(self, url: str, limit: int = None) -> ExtractResult:
        """
        Extract TikTok media metadata (Video or Profile).
        """
        try:
            import yt_dlp
        except ImportError:
            raise NotImplementedError("yt-dlp is not installed.")

        # Check if it's a profile URL (e.g. @username)
        is_profile = "/@" in url.lower() and "/video/" not in url.lower()
        
        # Simple extraction for profiles to get total count
        if is_profile:
            # We want to be fast and metadata-only
            # If limit is provided, ydl should respect it to be fast.
            # If no limit provided, the requirement says "Internally work with a small recent subset (default 20)"
            
            effective_limit = limit or 20
            
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'playlist_items': f'1-{effective_limit}'
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # We need to get the playlist count.
                # Process=False might not give playlist_count for all extractors, but it's faster.
                info = ydl.extract_info(url, download=False, process=True) 
            
            profile_name = info.get('title') or info.get('uploader') or info.get('id') or "Profile"
            actual_total = info.get('playlist_count') or len(info.get('entries', []))
            
            # If user provided a limit, the effective total IS the limit (clamped by actual)
            if limit:
                display_total = min(limit, actual_total)
            else:
                display_total = actual_total # Original total
            
            return ExtractResult(
                platform="tiktok",
                source_url=url,
                metadata=TikTokMetadata(
                    video_id=info.get('id', 'profile'),
                    title=f"@{profile_name}" if not profile_name.startswith('@') else profile_name,
                    author=profile_name,
                    total_count=display_total
                ),
                is_collection=True,
                entries=info.get('entries', [])[:effective_limit]
            )

        # Single Video Resolution (Same as before)
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            
            # Extract metadata
            video_id = info.get('id', 'unknown')
            title = info.get('title', '')
            author = info.get('uploader', '') or info.get('creator', '')
            duration = float(info.get('duration') or 0)
            
            metadata = TikTokMetadata(
                video_id=video_id,
                title=title,
                author=author,
                duration=duration
            )
            
            # TikTok has single stream (no quality selection)
            available_streams = [
                {
                    'type': 'video',
                    'quality': 'best',
                    'height': info.get('height', 0)
                },
                {
                    'type': 'audio',
                    'quality': 'best',
                    'ext': 'mp3'
                }
            ]
            
            return ExtractResult(
                platform="tiktok",
                source_url=url,
                metadata=metadata,
                streams=available_streams,
                is_collection=False
            )
            
        except Exception as e:
            # Handle common TikTok errors
            error_msg = str(e).lower()
            if 'private' in error_msg or 'unavailable' in error_msg:
                raise Exception("Video unavailable or private")
            elif 'geo' in error_msg or 'region' in error_msg:
                raise Exception("Video blocked in your region")
            elif '403' in error_msg or 'forbidden' in error_msg:
                raise Exception("Access denied (may be rate limited)")
            else:
                raise Exception(f"TikTok extraction failed: {str(e)}")

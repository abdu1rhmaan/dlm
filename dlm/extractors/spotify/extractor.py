
import re
import time
import base64
try:
    from curl_cffi import requests
    HAVE_CURL_CFFI = True
except (ImportError, Exception):
    import requests
    HAVE_CURL_CFFI = False
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Any, List

from dlm.extractors.base import BaseExtractor, ExtractResult
from dlm.core.config import SecureConfigRepository

@dataclass
class SpotifyMetadata:
    id: str
    title: str
    artist: str
    album: str
    duration: int  # ms (from API)
    variant: str = "ORIGINAL"
    total_count: int = 1

class SpotifyExtractor(BaseExtractor):
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    API_BASE = "https://api.spotify.com/v1"

    def __init__(self, config_repo=None):
        # Lazy config init if not injected
        self.config = config_repo or SecureConfigRepository(Path.cwd())

    def supports(self, url: str) -> bool:
        return "open.spotify.com" in url

    def _ensure_token(self) -> str:
        """Get or refresh Spotify access token."""
        # 1. Check existing valid token
        token = self.config.get("spotify_access_token")
        expires_at = self.config.get("spotify_token_expires_at")
        
        if token and expires_at and time.time() < (expires_at - 60):
            return token

        # 2. Need refresh. Check creds.
        client_id = self.config.get("spotify_client_id")
        client_secret = self.config.get("spotify_client_secret")

        if not client_id or not client_secret:
            print("\n[Spotify] Credentials missing!")
            print("Run 'config spotify' to set them up.")
            # Simple interactive fallback if in CLI mode
            if sys.stdin.isatty():
                print("Or enter them now:")
                try:
                    client_id = input("Client ID: ").strip()
                    client_secret = input("Client Secret: ").strip()
                    if client_id and client_secret:
                        self.config.set("spotify_client_id", client_id)
                        self.config.set("spotify_client_secret", client_secret)
                except KeyboardInterrupt:
                    raise Exception("Spotify setup cancelled")
            
            if not client_id or not client_secret:
                raise Exception("Spotify credentials required. Run 'config spotify'.")

        # 3. Request Token
        auth_str = f"{client_id}:{client_secret}"
        auth_b64 = base64.b64encode(auth_str.encode()).decode()
        
        try:
            resp = requests.post(
                self.TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {auth_b64}"},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            
            token = data["access_token"]
            expires_in = data["expires_in"]
            
            self.config.set("spotify_access_token", token)
            self.config.set("spotify_token_expires_at", time.time() + expires_in)
            
            return token
        except Exception as e:
            raise Exception(f"Failed to authenticate with Spotify: {e}")

    def _detect_variant(self, title: str, album: str, duration_ms: int) -> str:
        """Detect track variant (Original, Remix, Live, etc.)."""
        lower_title = title.lower()
        lower_album = album.lower()
        
        # Explicit markers take precedence
        if "remix" in lower_title or "remix" in lower_album:
            return "REMIX"
        if "live" in lower_title or "live" in lower_album:
            return "LIVE"
        if "acoustic" in lower_title or "acoustic" in lower_album:
            return "ACOUSTIC"
        if "slowed" in lower_title:
            return "SLOWED"
        if "edit" in lower_title:
             return "EDIT"
             
        # Duration signal? (Hard to know without reference baseline, 
        # so for now rely on markers)
        return "ORIGINAL"

    def extract(self, url: str, limit: int = None) -> ExtractResult:
        token = self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        
        # Regex to parse ID
        # https://open.spotify.com/track/123...
        match = re.search(r"/(track|playlist)/([a-zA-Z0-9]+)", url)
        if not match:
             raise ValueError("Invalid Spotify URL")
             
        type_, id_ = match.groups()
        
        if type_ == 'track':
            resp = requests.get(f"{self.API_BASE}/tracks/{id_}", headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            artist = ", ".join(a['name'] for a in data.get('artists', []))
            title = data.get('name')
            album = data.get('album', {}).get('name')
            duration_ms = data.get('duration_ms', 0)
            
            # Simplified: No variant detection
            variant = "ORIGINAL"
            
            meta = SpotifyMetadata(
                id=id_,
                title=title,
                artist=artist,
                album=album,
                duration=duration_ms,
                variant=variant
            )
            
            return ExtractResult(
                platform="spotify",
                source_url=url,
                metadata=meta,
                is_collection=False
            )
            
        elif type_ == 'playlist':
            # Get Playlist Info
            resp = requests.get(f"{self.API_BASE}/playlists/{id_}", headers=headers, timeout=10)
            resp.raise_for_status()
            pl_data = resp.json()
            
            pl_title = pl_data.get('name')
            total = pl_data.get('tracks', {}).get('total', 0)
            
            entries = []
            
            # Pagination
            next_url = f"{self.API_BASE}/playlists/{id_}/tracks?limit=50"
            fetched = 0
            
            print(f"Fetching playlist tracks ({total} items)...")
            
            while next_url:
                if limit and fetched >= limit:
                    break
                    
                resp = requests.get(next_url, headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                
                for item in data.get('items', []):
                    track = item.get('track')
                    if not track: continue
                    
                    # We pass the track URL as "url", but we embed simplified metadata
                    # so the worker doesn't need to re-fetch individual tracks if possible.
                    # Ideally, returning the Spotify URL makes the system re-process via 'extract' for each.
                    # This is cleaner but slower (N API calls). 
                    # Optimization: Return dictionary with pre-filled metadata?
                    # DLM loop usually calls 'add_download' with url.
                    # So we return URLs.
                    
                    # IMPORTANT: For Phase 1, we just return the track URL.
                    # The system will call extract() again for each.
                    # This is acceptable API rate-wise for single users.
                    
                    tid = track.get('id')
                    if tid:
                        t_url = f"https://open.spotify.com/track/{tid}"
                        entries.append({
                            'url': t_url,
                            'title': track.get('name')
                        })
                        fetched += 1
                        if limit and fetched >= limit: break
                
                next_url = data.get('next')
            
            meta = SpotifyMetadata(
                id=id_,
                title=pl_title,
                artist="Various",
                album=pl_title,
                duration=0,
                total_count=total
            )
            
            return ExtractResult(
                platform="spotify",
                source_url=url,
                metadata=meta,
                is_collection=True,
                entries=entries
            )
            
        return None

from typing import List, Optional, Any
from dataclasses import dataclass, field
from dlm.extractors.base import BaseExtractor
from dlm.extractors.result import ExtractResult
import os
import time
from urllib.parse import urlparse, parse_qs

try:
    import libtorrent as lt
except ImportError:
    lt = None

@dataclass
class TorrentFile:
    index: int
    name: str
    size: int
    offset: int = 0

@dataclass
class TorrentMetadata:
    title: str
    total_size: int
    files: List[TorrentFile] = field(default_factory=list)
    info_hash: str = ""
    piece_length: int = 0
    pieces: bytes = b''
    is_magnet: bool = False

class BencodeDecoder:
    @staticmethod
    def decode(data: bytes) -> Any:
        def decode_next(index):
            if index >= len(data):
                raise ValueError("Unexpected end of bencode data")
            char = data[index:index+1]
            if char == b'i':
                end = data.find(b'e', index)
                if end == -1: raise ValueError("Unterminated integer")
                return int(data[index+1:end]), end + 1
            elif char == b'l':
                res = []
                index += 1
                while index < len(data) and data[index:index+1] != b'e':
                    val, index = decode_next(index)
                    res.append(val)
                return res, index + 1
            elif char == b'd':
                res = {}
                index += 1
                while index < len(data) and data[index:index+1] != b'e':
                    key, index = decode_next(index)
                    if not isinstance(key, bytes):
                        raise ValueError("Dictionary key must be bytes")
                    val, index = decode_next(index)
                    res.update({key.decode('utf-8', errors='ignore'): val})
                return res, index + 1
            elif b'0' <= char <= b'9':
                colon = data.find(b':', index)
                if colon == -1: raise ValueError("Malformed string length")
                length = int(data[index:colon])
                start = colon + 1
                end = start + length
                if end > len(data):
                    raise ValueError(f"String length {length} exceeds remaining data")
                return data[start:end], end
            else:
                raise ValueError(f"Invalid bencode character at {index}: {char}")
        
        try:
            val, _ = decode_next(0)
            return val
        except Exception as e:
            print(f"[Torrent] Bencode decode error: {e}")
            return None

class TorrentExtractor(BaseExtractor):
    """
    Extractor for Torrent sources (Magnets and .torrent files).
    """

    def supports(self, url: str) -> bool:
        if url.startswith('magnet:'):
            return True
        if url.lower().endswith('.torrent'):
            return True
        if os.path.isfile(url) and url.lower().endswith('.torrent'):
            return True
        return False

    def extract(self, url: str) -> ExtractResult:
        if url.startswith('magnet:'):
            return self._extract_magnet(url)
        else:
            return self._extract_torrent_file(url)

    def _extract_magnet(self, url: str) -> ExtractResult:
        # 1. Parse basic magnet info
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        
        xt = params.get('xt', [''])[0]
        info_hash = xt.split(':')[-1] if 'urn:btih:' in xt else ''
        dn = params.get('dn', ['Unknown Torrent'])[0]

        metadata = TorrentMetadata(
            title=dn,
            total_size=0,
            files=[],
            info_hash=info_hash,
            is_magnet=True
        )

        if lt:
             # Try to resolve metadata via DHT if libtorrent is available
             # This is a synchronous resolve for the extractor phase
             try:
                 ses = lt.session()
                 ses.listen_on(6881, 6891)
                 params = {
                     'save_path': '.',
                     'storage_mode': lt.storage_mode_t.storage_mode_sparse
                 }
                 handle = lt.add_magnet_uri(ses, url, params)
                 
                 print(f"[Torrent] Resolving magnet metadata ({info_hash})...")
                 timeout = 30 # seconds
                 start = time.time()
                 while not handle.has_metadata():
                     if time.time() - start > timeout:
                         break
                     time.sleep(1)
                 
                 if handle.has_metadata():
                     info = handle.get_torrent_info()
                     metadata.title = info.name()
                     metadata.total_size = info.total_size()
                     metadata.piece_length = info.piece_length()
                     
                     for i in range(info.num_files()):
                         fe = info.file_at(i)
                         metadata.files.append(TorrentFile(
                             index=i,
                             name=fe.path,
                             size=fe.size,
                             offset=fe.offset
                         ))
                                         
             except Exception as e:
                 print(f"[Torrent] Magnet resolve failed: {e}")

        return ExtractResult(
            platform='torrent',
            source_url=url,
            metadata=metadata,
            is_collection=True,
            entries=[{'index': f.index, 'title': f.name, 'size': f.size} for f in metadata.files]
        )

    def _extract_torrent_file(self, url: str) -> ExtractResult:
        metadata = TorrentMetadata(title="Unknown", total_size=0)
        
        path = url
        if not os.path.isfile(path):
            # Might be a URL to a .torrent file
            # Ideally we'd download it here to a temp file
            pass
            
        # 1. Fallback / Immediate Resolve via Bencode (Always try if file exists)
        if os.path.isfile(path):
            try:
                with open(path, 'rb') as f:
                    data = f.read()
                
                decoded = BencodeDecoder.decode(data)
                if decoded and 'info' in decoded:
                    info_dict = decoded['info']
                    metadata.title = info_dict.get('name', b'Unknown').decode('utf-8', errors='ignore')
                    
                    if 'files' in info_dict:
                        # Multi-file torrent
                        current_offset = 0
                        for i, f_info in enumerate(info_dict['files']):
                            f_size = f_info.get('length', 0)
                            f_path_parts = f_info.get('path', [])
                            f_path = "/".join([p.decode('utf-8', errors='ignore') for p in f_path_parts])
                            
                            metadata.files.append(TorrentFile(
                                index=i,
                                name=f_path,
                                size=f_size,
                                offset=current_offset
                            ))
                            current_offset += f_size
                        metadata.total_size = current_offset
                    else:
                        # Single-file torrent
                        f_size = info_dict.get('length', 0)
                        metadata.total_size = f_size
                        metadata.files.append(TorrentFile(
                            index=0,
                            name=metadata.title,
                            size=f_size,
                            offset=0
                        ))
            except Exception as e:
                print(f"[Torrent] Fallback bencode parse failed: {e}")

        # 2. Enhanced libtorrent parsing (if available)
        if not lt:
            print("\n[WARNING] libtorrent dependency is missing or failed to load (DLL error).")
            print("          Ensure you have 'Microsoft Visual C++ Redistributable 2019/2022' installed.")
            
        if lt and os.path.isfile(path):
            try:
                # Use libtorrent for more accurate info if possible
                info = lt.torrent_info(path)
                metadata.title = info.name()
                metadata.total_size = info.total_size()
                metadata.info_hash = str(info.info_hash())
                
                # Clear and re-populate with libtorrent info for consistency if successful
                metadata.files = []
                for i in range(info.num_files()):
                    fe = info.file_at(i)
                    metadata.files.append(TorrentFile(
                        index=i,
                        name=fe.path,
                        size=fe.size,
                        offset=fe.offset
                    ))
            except Exception as e:
                print(f"[Torrent] Libtorrent parse failed: {e}")
        elif not os.path.isfile(path) and not path.startswith('http'):
             print(f"[Torrent] File not found: {path}")
        
        return ExtractResult(
            platform='torrent',
            source_url=url,
            metadata=metadata,
            is_collection=True,
            entries=[{'index': f.index, 'title': f.name, 'size': f.size} for f in metadata.files]
        )

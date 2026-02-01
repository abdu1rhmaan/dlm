import ssl
from typing import Iterator, Optional, Dict
from dlm.core.interfaces import NetworkAdapter
try:
    from curl_cffi import requests
    HAVE_CURL_CFFI = True
except (ImportError, Exception):
    # Fallback for environments like Termux where curl_cffi might fail due to .so issues
    import requests
    HAVE_CURL_CFFI = False

class NetworkError(Exception):
    pass

class ServerError(Exception):
    pass

class HttpNetworkAdapter(NetworkAdapter):
    def __init__(self):
        # Create SSL context that doesn't verify certificates
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE
    
    def _add_browser_headers(self, url: str, referer: Optional[str] = None, headers: Optional[list] = None, cookies: Optional[dict] = None, user_agent: Optional[str] = None) -> tuple:
        final_headers = []
        
        # 1. Process Captured Headers (Exact Order)
        if headers:
            # If headers is list of {'name': ..., 'value': ...} (from Playwright)
            if isinstance(headers, list):
                for h in headers:
                    name = h.get("name", "")
                    value = h.get("value", "")
                    # Remove ONLY Host and Content-Length as they are handled by the library/server
                    if name.lower() in ["host", "content-length"]:
                        continue
                    final_headers.append((name, value))
            # Fallback for dict
            elif isinstance(headers, dict):
                for k, v in headers.items():
                    if k.lower() in ["host", "content-length"]:
                        continue
                    final_headers.append((k, v))
        
        # 2. Add Referer if missing but provided
        if referer and not any(h[0].lower() == "referer" for h in final_headers):
            final_headers.append(("Referer", referer))
        
        # 3. Cookies - Pass as dict for curl_cffi
        final_cookies = {}
        if cookies:
            if isinstance(cookies, list):
                for c in cookies:
                    final_cookies[c["name"]] = c["value"]
            elif isinstance(cookies, dict):
                final_cookies = cookies

        return final_headers, final_cookies

    def get_content_length(self, url: str, referer: Optional[str] = None, headers: Optional[dict] = None, cookies: Optional[dict] = None, user_agent: Optional[str] = None, timeout: int = 15) -> Optional[int]:
        try:
            h, c = self._add_browser_headers(url, referer, headers, cookies, user_agent)
            
            # Use Session with impersonation if available
            session_args = {"impersonate": "chrome120"} if HAVE_CURL_CFFI else {}
            with requests.Session(**session_args) as s:
                # Attempt 1: HEAD
                resp = s.head(url, headers=h, cookies=c, timeout=(10, 30), verify=False)
                
                # Attempt 2: Stream-Based Probe if HEAD fails or doesn't have length
                if resp.status_code != 200 or not resp.headers.get("Content-Length"):
                    print(f"[DISCOVERY] HEAD failed or no length → probing size via stream (bytes=0-0)")
                    h_range = h.copy() if isinstance(h, dict) else list(h)
                    # Add Range header for probe
                    if isinstance(h_range, list):
                        h_range.append(("Range", "bytes=0-0"))
                    else:
                        h_range["Range"] = "bytes=0-0"
                    
                    # Perform stream probe GET request
                    with s.get(url, headers=h_range, cookies=c, stream=True, timeout=(10, 30), verify=False) as r_stream:
                        resp = r_stream # Use the stream response for header extraction
                        
                        # Extract size from Content-Range or Content-Length
                        length = resp.headers.get("Content-Length")
                        if "Content-Range" in resp.headers:
                            cr = resp.headers["Content-Range"]
                            if '/' in cr:
                                total = cr.split('/')[-1]
                                if total.isdigit():
                                    return int(total)
                        
                        if length and str(length).isdigit():
                            return int(length)
                        
                        # If still unknown or failed status code
                        if resp.status_code not in [200, 206]:
                            if resp.status_code in [401, 403, 410]:
                                raise ServerError(f"HTTP {resp.status_code}")
                            return None

                # CRITICAL: Validate Content-Type for the successful HEAD request if it reached here
                content_type = resp.headers.get("Content-Type", "").lower()
                if "text/html" in content_type:
                    raise NetworkError("Server returned HTML (likely session expired)")

                length = resp.headers.get("Content-Length")
                
                # Handle Content-Range for HEAD (some servers might return it)
                if not length and "Content-Range" in resp.headers:
                    cr = resp.headers["Content-Range"]
                    if '/' in cr:
                        total = cr.split('/')[-1]
                        if total.isdigit():
                            return int(total)
                
                return int(length) if length and str(length).isdigit() else None
        except Exception as e:
            if isinstance(e, (NetworkError, ServerError)): raise
            return None

    def supports_ranges(self, url: str, referer: Optional[str] = None, headers: Optional[list] = None, cookies: Optional[dict] = None, user_agent: Optional[str] = None) -> bool:
        try:
            h, c = self._add_browser_headers(url, referer, headers, cookies, user_agent)
            if isinstance(h, list):
                h.append(("Range", "bytes=0-0"))
            else:
                h["Range"] = "bytes=0-0"
            session_args = {"impersonate": "chrome120"} if HAVE_CURL_CFFI else {}
            with requests.Session(**session_args) as s:
                resp = s.get(url, headers=h, cookies=c, verify=False, timeout=(10, 30))
                return resp.status_code == 206
        except:
            return False

    def download_range(self, url: str, start: int, end: int, referer: Optional[str] = None, headers: Optional[list] = None, cookies: Optional[dict] = None, user_agent: Optional[str] = None) -> Iterator[bytes]:
        h, c = self._add_browser_headers(url, referer, headers, cookies, user_agent)
        if isinstance(h, list):
            h.append(("Range", f"bytes={start}-{end}"))
        else:
            h["Range"] = f"bytes={start}-{end}"
        
        try:
            session_args = {"impersonate": "chrome120"} if HAVE_CURL_CFFI else {}
            s = requests.Session(**session_args)
            resp = s.get(url, headers=h, cookies=c, stream=True, verify=False, timeout=(10, 30))
            
            if resp.status_code not in [200, 206]:
                if resp.status_code in [401, 403, 410]:
                    raise ServerError(f"HTTP {resp.status_code}")
                raise NetworkError(f"HTTP {resp.status_code}")

            content_type = resp.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise NetworkError("Server returned HTML instead of binary")

            for chunk in resp.iter_content(chunk_size=64 * 1024):
                yield chunk
            s.close()
        except requests.exceptions.RequestException as e:
            raise NetworkError(f"Connection failed: {e}")

    def download_stream(self, url: str, referer: Optional[str] = None, headers: Optional[list] = None, cookies: Optional[dict] = None, user_agent: Optional[str] = None) -> Iterator[bytes]:
        h, c = self._add_browser_headers(url, referer, headers, cookies, user_agent)
        
        try:
            session_args = {"impersonate": "chrome120"} if HAVE_CURL_CFFI else {}
            s = requests.Session(**session_args)
            resp = s.get(url, headers=h, cookies=c, stream=True, verify=False, timeout=(10, 30))
            
            if resp.status_code != 200:
                if resp.status_code in [401, 403, 410]:
                    raise ServerError(f"HTTP {resp.status_code}")
                raise NetworkError(f"HTTP {resp.status_code}")

            content_type = resp.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise NetworkError("Server returned HTML instead of binary")

            for chunk in resp.iter_content(chunk_size=64 * 1024):
                yield chunk
            s.close()
        except requests.exceptions.RequestException as e:
            raise NetworkError(f"Connection failed: {e}")

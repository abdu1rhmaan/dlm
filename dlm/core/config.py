import os
import json
import base64
from pathlib import Path
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

class SecureConfigRepository:
    """
    Manages encrypted configuration settings.
    Saves to 'config.enc' in the project root.
    """
    def __init__(self, root_path: Path):
        # Store config inside dlm folder, not project root
        config_dir = root_path / "dlm"
        config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = config_dir / "config.enc"
        
        if HAVE_CRYPTO:
            self._key = self._derive_key()
            self._fernet = Fernet(self._key)
        else:
            self._key = None
            self._fernet = None
            
        self._cache = {}
        self._load()

    def _derive_key(self) -> bytes:
        """
        Derive a consistent key. 
        """
        if not HAVE_CRYPTO:
            return b""
            
        # Obfuscated machine-specific seed (simple mitigation)
        import uuid
        try:
            # Stable node ID (MAC address)
            machine_id = str(uuid.getnode())
        except:
            machine_id = "default_fallback_id"
            
        # Salt must be consistent for the file to be readable across restarts
        salt = b'dlm_secure_salt_v1' 
        
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        return base64.urlsafe_b64encode(kdf.derive(machine_id.encode()))

    def _load(self):
        if not self.config_path.exists():
            self._cache = {}
            return

        try:
            with open(self.config_path, 'rb') as f:
                data = f.read()
            
            if HAVE_CRYPTO and self._fernet:
                try:
                    decrypted_data = self._fernet.decrypt(data)
                    self._cache = json.loads(decrypted_data.decode())
                except:
                    # Fallback if decryption fails (might be plain text)
                    self._cache = json.loads(data.decode())
            else:
                self._cache = json.loads(data.decode())
        except Exception:
            # If load fails (tampering/corruption), allow reset silently
            self._cache = {}

    def save(self):
        try:
            data_str = json.dumps(self._cache)
            if HAVE_CRYPTO and self._fernet:
                final_data = self._fernet.encrypt(data_str.encode())
            else:
                final_data = data_str.encode()
            
            with open(self.config_path, 'wb') as f:
                f.write(final_data)
        except Exception as e:
            print(f"[Config] Failed to save config: {e}")

    def get(self, key: str, default=None):
        return self._cache.get(key, default)

    def set(self, key: str, value):
        self._cache[key] = value
        self.save()

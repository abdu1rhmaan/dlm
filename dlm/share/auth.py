import uuid
import secrets
from typing import Dict, Optional
from datetime import datetime
from .models import Room, Session

class AuthManager:
    def __init__(self):
        self._sessions: Dict[str, Session] = {}
        
    def generate_token(self) -> str:
        """Generate a short human-readable token (XXX-XXX)."""
        # Using digits for simplicity on numpads, or hex?
        # User example: "123-456" or "XXX-XXX".
        # Let's use 3 digits - 3 digits for easy typing on mobile.
        part1 = secrets.randbelow(1000)
        part2 = secrets.randbelow(1000)
        return f"{part1:03d}-{part2:03d}"

    def create_session(self, token: str, room: Room) -> Optional[Session]:
        """Validate token and create session."""
        if room.is_expired:
            return None
            
        # Constant time comparison to prevent timing attacks
        if not secrets.compare_digest(token, room.token):
            return None
            
        session_id = str(uuid.uuid4())
        session = Session(
            session_id=session_id,
            token=token,
            expires_at=room.expires_at
        )
        self._sessions[session_id] = session
        return session

    def validate_session(self, session_id: str) -> bool:
        """Check if session exists and is valid."""
        if session_id not in self._sessions:
            return False
            
        session = self._sessions[session_id]
        if not session.is_valid:
            del self._sessions[session_id]
            return False
            
        return True

    def get_session(self, session_id: str) -> Optional[Session]:
        if self.validate_session(session_id):
            return self._sessions[session_id]
        return None

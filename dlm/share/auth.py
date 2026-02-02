import uuid
import secrets
from typing import Dict, Optional
from datetime import datetime
from .room import Room

from dataclasses import dataclass

@dataclass
class Session:
    session_id: str
    token: str
    expires_at: datetime
    
    @property
    def is_valid(self) -> bool:
        return datetime.now() <= self.expires_at

class AuthManager:
    def __init__(self):
        self._sessions: Dict[str, Session] = {}
        
    def generate_token(self) -> str:
        """Generate a short human-readable token (XXX-XXX)."""
        part1 = secrets.randbelow(1000)
        part2 = secrets.randbelow(1000)
        return f"{part1:03d}-{part2:03d}"
    
    def _generate_room_id(self) -> str:
        """Helper to generate room ID."""
        import string
        chars = string.ascii_uppercase + string.digits
        return ''.join(secrets.choice(chars) for _ in range(4))

    def create_session(self, token: str, room: Room) -> Optional[Session]:
        """Validate token and create session."""
        # Simple validation: room must exist and token must match
        if not secrets.compare_digest(token, room.token):
            return None
            
        session_id = str(uuid.uuid4())
        # Session expires in 1 hour or when room ends
        from datetime import timedelta
        expires_at = datetime.now() + timedelta(minutes=15)
        
        session = Session(
            session_id=session_id,
            token=token,
            expires_at=expires_at
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

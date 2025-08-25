import uuid
import time
from typing import Dict, List, Optional, Any, Tuple

from utils.base_logger import get_logger

# Configure logging
logger = get_logger(__name__)


class SessionManager:
    def __init__(self, session_timeout: int = 900):
        """
        Initialize the session manager

        Args:
            session_timeout: Session timeout in seconds (default: 24 hours)
        """
        self.session_timeout = session_timeout
        self.thread_store: Dict[str, Dict[str, Any]] = {}
        self.history_store: Dict[str, List[str]] = {}
        self.session_timestamps: Dict[str, float] = {}

    def create_session(self) -> str:
        """Create a new session and return its ID"""
        session_id = str(uuid.uuid4())
        self.history_store[session_id] = []
        self.session_timestamps[session_id] = time.time()
        return session_id

    def add_to_history(self, session_id: str, message: str) -> None:
        """Add message to session history"""
        if session_id not in self.history_store:
            self.history_store[session_id] = []

        self.history_store[session_id].append(message)
        self.update_session_timestamp(session_id)

    def get_history(self, session_id: str) -> List[str]:
        """Get history for a session"""
        self.cleanup_expired_sessions()

        if session_id in self.history_store:
            self.update_session_timestamp(session_id)
            return self.history_store[session_id]
        return []

    def update_session_timestamp(self, session_id: str) -> None:
        """Update last access timestamp for a session"""
        self.session_timestamps[session_id] = time.time()

    def is_session_valid(self, session_id: str) -> bool:
        """Check if a session is valid and not expired"""
        if session_id not in self.session_timestamps:
            return False

        current_time = time.time()
        session_time = self.session_timestamps[session_id]

        return (current_time - session_time) < self.session_timeout

    def cleanup_expired_sessions(self) -> None:
        """Clean up expired sessions"""
        current_time = time.time()
        expired_sessions = []

        for session_id, timestamp in self.session_timestamps.items():
            if (current_time - timestamp) >= self.session_timeout:
                expired_sessions.append(session_id)

        for session_id in expired_sessions:
            if session_id in self.thread_store:
                del self.thread_store[session_id]

            if session_id in self.history_store:
                del self.history_store[session_id]

            del self.session_timestamps[session_id]

        if expired_sessions:
            logger.info(f"Cleaned up {len(expired_sessions)} expired sessions")

    def delete_session(self, session_id: str) -> bool:
        """Delete a session"""
        if session_id in self.thread_store:
            del self.thread_store[session_id]

        if session_id in self.history_store:
            del self.history_store[session_id]

        if session_id in self.session_timestamps:
            del self.session_timestamps[session_id]

        return True

    def get_last_activity(self, session_id: str) -> Optional[float]:
        # Return the last activity timestamp for a session
        return self.session_timestamps.get(session_id)

from contextvars import ContextVar
from typing import Optional


current_user_id: ContextVar[Optional[str]] = ContextVar(
    "current_user_id",
    default=None
)
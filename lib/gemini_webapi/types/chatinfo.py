from datetime import datetime

from pydantic import BaseModel


class ChatInfo(BaseModel):
    """Metadata for a chat entry visible in the Gemini account."""

    cid: str
    title: str
    is_pinned: bool = False
    timestamp: float

    def __str__(self) -> str:
        pin = "[Pinned] " if self.is_pinned else ""
        title = self.title or f"Chat({self.cid})"
        dt = datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        return f"{pin}{title} ({dt})"

    def __repr__(self) -> str:
        return (
            f"ChatInfo(cid={self.cid!r}, title={self.title!r}, "
            f"is_pinned={self.is_pinned!r}, timestamp={self.timestamp!r})"
        )

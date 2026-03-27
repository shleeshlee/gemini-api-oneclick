from textwrap import shorten
from typing import List, Optional

from pydantic import BaseModel

from .modeloutput import ModelOutput


class ChatTurn(BaseModel):
    """A single turn in a Gemini chat history."""

    role: str
    text: str
    model_output: Optional[ModelOutput] = None

    def __str__(self):
        return f"{self.role.upper()}: {shorten(self.text, width=100)}"

    def __repr__(self):
        return f"ChatTurn(role={self.role!r}, text={shorten(self.text, width=100)!r})"


class ChatHistory(BaseModel):
    """The ordered conversation history for one chat id."""

    cid: str
    turns: List[ChatTurn]

    def __str__(self) -> str:
        return f"ChatHistory(cid={self.cid!r})"

    def __repr__(self) -> str:
        return f"ChatHistory(cid={self.cid!r}, turns={self.turns!r})"

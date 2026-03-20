import html

from pydantic import BaseModel, Field, field_validator

from .image import GeneratedImage, Image, WebImage
from .video import GeneratedVideo


class Candidate(BaseModel):
    """
    A single reply candidate object in the model output. A full response from Gemini usually contains multiple reply candidates.

    Parameters
    ----------
    rcid: `str`
        Reply candidate ID to build the metadata
    text: `str`
        Text output
    thoughts: `str`, optional
        Model's thought process, can be empty. Only populated with `-thinking` models
    web_images: `list[WebImage]`, optional
        List of web images in reply, can be empty.
    generated_images: `list[GeneratedImage]`, optional
        List of generated images in reply, can be empty
    generated_videos: `list[GeneratedVideo]`, optional
        List of generated videos in reply, can be empty
    """

    rcid: str
    text: str
    text_delta: str | None = None
    thoughts: str | None = None
    thoughts_delta: str | None = None
    web_images: list[WebImage] = Field(default_factory=list)
    generated_images: list[GeneratedImage] = Field(default_factory=list)
    generated_videos: list[GeneratedVideo] = Field(default_factory=list)

    def __str__(self):
        return self.text

    def __repr__(self):
        return f"Candidate(rcid='{self.rcid}', text='{(len(self.text) <= 20 and self.text) or self.text[:20] + '...'}', images={self.images})"

    @field_validator("text", "thoughts", mode="after")
    @classmethod
    def decode_html(cls, value: str | None) -> str | None:
        """
        Auto unescape HTML entities in text/thoughts if any.
        """

        if value:
            value = html.unescape(value)
        return value

    @property
    def images(self) -> list[Image]:
        return self.web_images + self.generated_images

    @property
    def videos(self) -> list[GeneratedVideo]:
        return self.generated_videos

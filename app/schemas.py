"""Pydantic models for API requests and structured Gemini output."""

from pydantic import BaseModel

# --- Gemini output schemas ---


class VideoSummary(BaseModel):
    """Structured summary of a YouTube video (Mode 1: no question provided)."""

    title: str
    main_topics: list[str]
    key_points: list[str]
    sentiment: str
    tone: str


class ThemeAnalysis(BaseModel):
    """Per-video analysis scoped to a user-provided theme (Mode 2)."""

    relevance_to_theme: str
    key_points: list[str]
    sentiment: str
    creator_stance: str


# --- API request models ---


class VideoAnalysisRequest(BaseModel):
    """Request body for single video analysis."""

    url: str
    question: str = ""


class ThemeAnalysisRequest(BaseModel):
    """Request body for theme exploration."""

    theme: str
    date_start: str = ""
    date_end: str = ""
    max_duration_min: int = 30
    blacklist: str = ""

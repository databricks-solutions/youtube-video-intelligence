"""Tests for core analysis logic extracted from app.py.

Uses `patch.object` on the imported module to avoid cross-test module
identity issues when running alongside test_api.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

# Pre-register mock lakebase before importing app
sys.modules["lakebase"] = MagicMock()

from schemas import ThemeAnalysis, VideoSummary
from youtube import VideoSearchResult

import app as app_mod

# --- _analyze_single_video ---


def test_analyze_single_video_freeform() -> None:
    """Freeform mode returns raw text and correct type."""
    with patch.object(app_mod, "analyze_video", return_value="The answer is 42"):
        result_type, result_json, md = app_mod._analyze_single_video(
            MagicMock(), "https://youtube.com/watch?v=test", "What is it?", None
        )
    assert result_type == "freeform"
    assert result_json == {"text": "The answer is 42"}
    assert md == "The answer is 42"


def test_analyze_single_video_freeform_failure() -> None:
    """Freeform mode handles None result gracefully."""
    with patch.object(app_mod, "analyze_video", return_value=None):
        result_type, result_json, md = app_mod._analyze_single_video(
            MagicMock(), "https://youtube.com/watch?v=test", "question?", None
        )
    assert result_type == "freeform"
    assert result_json == {}
    assert "failed" in md.lower()


def test_analyze_single_video_summary() -> None:
    """Summary mode returns structured data and formatted markdown."""
    summary = VideoSummary(
        title="Test",
        main_topics=["AI", "ML"],
        key_points=["Point A"],
        sentiment="positive",
        tone="excited",
    )
    with patch.object(app_mod, "analyze_video", return_value=summary):
        result_type, result_json, md = app_mod._analyze_single_video(
            MagicMock(), "https://youtube.com/watch?v=test", "", None
        )
    assert result_type == "summary"
    assert result_json["sentiment"] == "positive"
    assert "AI, ML" in md
    assert "- Point A" in md


def test_analyze_single_video_summary_failure() -> None:
    """Summary mode handles None result gracefully."""
    with patch.object(app_mod, "analyze_video", return_value=None):
        result_type, result_json, md = app_mod._analyze_single_video(
            MagicMock(), "https://youtube.com/watch?v=test", "", "120s"
        )
    assert result_type == "summary"
    assert result_json == {}
    assert "failed" in md.lower() or "unavailable" in md.lower()


# --- _videos_to_dicts ---


def test_videos_to_dicts() -> None:
    """Converts VideoSearchResult list to serializable dicts."""
    videos = [
        VideoSearchResult(
            url="https://www.youtube.com/watch?v=abc123def45",
            title="Video 1",
            channel="Ch1",
            upload_date="20240101",
            duration_seconds=300,
            view_count=1000,
            search_rank=0,
        ),
        VideoSearchResult(
            url="https://www.youtube.com/watch?v=xyz789ghi01",
            title="Video 2",
            channel="Ch2",
            upload_date="20240201",
            duration_seconds=600,
            view_count=5000,
            search_rank=1,
        ),
    ]
    result = app_mod._videos_to_dicts(videos)
    assert len(result) == 2
    assert result[0]["title"] == "Video 1"
    assert result[0]["views"] == 1000
    assert result[0]["duration"] == 300
    assert result[0]["thumbnail"].startswith("https://img.youtube.com/vi/")
    assert result[1]["channel"] == "Ch2"


def test_videos_to_dicts_empty() -> None:
    """Returns empty list for empty input."""
    assert app_mod._videos_to_dicts([]) == []


# --- _synthesize_analyses ---


def test_synthesize_no_analyses() -> None:
    """Returns failure message when no analyses available."""
    result = app_mod._synthesize_analyses([], "test theme", lambda e: None)
    assert "no videos" in result.lower()


def test_synthesize_single_analysis() -> None:
    """Returns insufficient-data message for single analysis."""
    analyses = [
        {
            "title": "V1",
            "channel": "C1",
            "url": "u1",
            "analysis": ThemeAnalysis(
                relevance_to_theme="high",
                key_points=["kp"],
                sentiment="pos",
                creator_stance="pro",
            ),
        }
    ]
    result = app_mod._synthesize_analyses(analyses, "test", lambda e: None)
    assert "only one" in result.lower()

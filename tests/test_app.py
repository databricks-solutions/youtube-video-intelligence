"""Tests for the app modules: youtube.py, gemini.py, schemas.py."""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from pydantic import BaseModel

# Add app/ to import path so we can import modules directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from schemas import ThemeAnalysis, VideoSummary
from youtube import (
    VideoSearchResult,
    compute_end_offset,
    filter_videos,
    rank_videos,
    thumbnail_url,
)

# ---------- schemas ----------


def test_video_summary_fields() -> None:
    """VideoSummary accepts expected fields."""
    s = VideoSummary(
        title="Test",
        main_topics=["a", "b"],
        key_points=["x"],
        sentiment="positive",
        tone="casual",
    )
    assert s.title == "Test"
    assert len(s.main_topics) == 2


def test_theme_analysis_fields() -> None:
    """ThemeAnalysis accepts expected fields."""
    t = ThemeAnalysis(
        relevance_to_theme="high",
        key_points=["point"],
        sentiment="mixed",
        creator_stance="neutral",
    )
    assert t.relevance_to_theme == "high"


# ---------- youtube.py ----------


def test_thumbnail_url_standard() -> None:
    """Extracts video ID from standard watch URL."""
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert thumbnail_url(url) == (
        "https://img.youtube.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
    )


def test_thumbnail_url_short() -> None:
    """Extracts video ID from youtu.be short URL."""
    url = "https://youtu.be/dQw4w9WgXcQ"
    assert thumbnail_url(url) == (
        "https://img.youtube.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
    )


def test_thumbnail_url_embed() -> None:
    """Extracts video ID from embed URL."""
    url = "https://youtube.com/embed/dQw4w9WgXcQ"
    assert thumbnail_url(url) == (
        "https://img.youtube.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
    )


def test_thumbnail_url_with_params() -> None:
    """Extracts video ID from URL with extra query parameters."""
    url = "https://www.youtube.com/watch?v=1gusR1iGar8&t=1s"
    assert thumbnail_url(url) == (
        "https://img.youtube.com/vi/1gusR1iGar8/mqdefault.jpg"
    )


def test_thumbnail_url_invalid() -> None:
    """Returns empty string for non-YouTube URLs."""
    assert thumbnail_url("https://example.com") == ""
    assert thumbnail_url("") == ""


def _video(
    url: str = "u",
    title: str = "T",
    channel: str = "C",
    upload_date: str = "20250101",
    duration: int = 300,
    views: int = 1000,
    rank: int = 0,
) -> VideoSearchResult:
    """Helper to construct VideoSearchResult with defaults."""
    return VideoSearchResult(url, title, channel, upload_date, duration, views, rank)


def test_filter_videos_blacklist() -> None:
    """Channel blacklist excludes matching channels (case-insensitive)."""
    videos = [
        _video(url="u1", title="V1", channel="BadChannel"),
        _video(url="u2", title="V2", channel="GoodChannel"),
    ]
    result = filter_videos(videos, blacklist={"badchannel"})
    assert len(result) == 1
    assert result[0].channel == "GoodChannel"


def test_filter_videos_allowlist() -> None:
    """Channel allowlist keeps only matching channels (case-insensitive)."""
    videos = [
        _video(url="u1", title="V1", channel="WantedChannel"),
        _video(url="u2", title="V2", channel="OtherChannel"),
    ]
    result = filter_videos(videos, allowlist={"wantedchannel"})
    assert len(result) == 1
    assert result[0].channel == "WantedChannel"


def test_theme_search_queries() -> None:
    """Allowlist scopes the search to one query per channel; else one broad query."""
    from app import _theme_search_queries

    assert _theme_search_queries("crypto", None) == ["crypto"]
    assert _theme_search_queries("crypto", {"elxokas"}) == ["crypto elxokas"]
    assert _theme_search_queries("ai", {"b", "a"}) == ["ai a", "ai b"]


def test_filter_videos_date_range() -> None:
    """Date range filter includes only videos within bounds."""
    videos = [
        _video(url="u1", title="Old", upload_date="20230601", duration=100),
        _video(url="u2", title="Mid", upload_date="20240601", duration=100),
        _video(url="u3", title="New", upload_date="20250601", duration=100),
    ]
    result = filter_videos(
        videos, date_start=date(2024, 1, 1), date_end=date(2024, 12, 31)
    )
    assert len(result) == 1
    assert result[0].title == "Mid"


def test_filter_videos_max_duration() -> None:
    """Max duration filter excludes videos longer than the limit."""
    videos = [
        _video(url="u1", title="Short", duration=120),
        _video(url="u2", title="Long", duration=1800),
        _video(url="u3", title="Exact", duration=600),
    ]
    result = filter_videos(videos, max_duration_seconds=600)
    assert len(result) == 2
    assert {v.title for v in result} == {"Short", "Exact"}


def test_filter_videos_missing_date_included() -> None:
    """Videos with empty upload_date are included (not filtered out)."""
    videos = [_video(url="u1", title="NoDate", upload_date="", duration=100)]
    result = filter_videos(videos, date_start=date(2024, 1, 1))
    assert len(result) == 1


def test_filter_videos_no_filters() -> None:
    """All videos pass when no filters are applied."""
    videos = [
        _video(url="u1", title="A", channel="C1", upload_date="20240101"),
        _video(url="u2", title="B", channel="C2", upload_date="20250101"),
    ]
    result = filter_videos(videos)
    assert len(result) == 2


def test_rank_videos_prefers_views_relevancy_recency() -> None:
    """rank_videos returns top N sorted by composite score."""
    videos = [
        _video(title="HighViews", views=5_000_000, rank=5, upload_date="20230101"),
        _video(title="TopRank", views=100, rank=0, upload_date="20240601"),
        _video(title="Recent", views=50_000, rank=3, upload_date="20260301"),
        _video(title="Mediocre", views=500, rank=8, upload_date="20220101"),
    ]
    ranked = rank_videos(videos, top_n=2)
    assert len(ranked) == 2
    # Top 2 should not include the weakest video
    titles = {v.title for v in ranked}
    assert "Mediocre" not in titles


def test_rank_videos_respects_top_n() -> None:
    """rank_videos caps output at top_n."""
    videos = [_video(title=f"V{i}", rank=i) for i in range(20)]
    ranked = rank_videos(videos, top_n=5)
    assert len(ranked) == 5


def test_compute_end_offset_normal() -> None:
    """End offset trims last 60 seconds from a 10-minute video."""
    assert compute_end_offset(600, trim_seconds=60) == "540s"


def test_compute_end_offset_short_video() -> None:
    """End offset floors at 60 seconds for very short videos."""
    assert compute_end_offset(90, trim_seconds=60) == "60s"


def test_compute_end_offset_very_short() -> None:
    """End offset floors at 60 seconds even if video is shorter."""
    assert compute_end_offset(30, trim_seconds=60) == "60s"


def test_search_videos_mock() -> None:
    """search_videos parses yt-dlp output correctly."""
    fake_info = {
        "entries": [
            {
                "id": "abc123def45",
                "title": "Test Video",
                "channel": "TestChannel",
                "upload_date": "20250315",
                "duration": 600,
                "view_count": 150000,
            },
            None,  # yt-dlp can return None entries
            {
                "id": "xyz789ghi01",
                "title": "Another",
                "uploader": "FallbackChannel",
                "upload_date": "20250310",
                "duration": 300,
                "view_count": 5000,
            },
        ]
    }

    with patch("youtube.yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = fake_info
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl_cls.return_value = mock_ydl

        from youtube import search_videos

        results = search_videos("test query", max_results=3)

    assert len(results) == 2
    assert results[0].url == "https://www.youtube.com/watch?v=abc123def45"
    assert results[0].channel == "TestChannel"
    assert results[0].duration_seconds == 600
    assert results[0].view_count == 150000
    assert results[0].search_rank == 0
    # Fallback to uploader when channel is missing
    assert results[1].channel == "FallbackChannel"
    assert results[1].search_rank == 2  # None entry at index 1 is skipped


# ---------- gemini.py ----------


class _FakeResponse:
    """Minimal mock of google.genai generate_content response."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModels:
    def __init__(self, response_text: str) -> None:
        self._text = response_text

    def generate_content(self, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(self._text)


class _FakeClient:
    def __init__(self, response_text: str) -> None:
        self.models = _FakeModels(response_text)


class SimpleSchema(BaseModel):
    title: str
    score: float


def test_analyze_video_structured() -> None:
    """analyze_video returns parsed Pydantic model when schema is provided."""
    from gemini import analyze_video

    expected = SimpleSchema(title="Test", score=0.9)
    client = _FakeClient(expected.model_dump_json())

    result = analyze_video(
        client,
        url="https://www.youtube.com/watch?v=test123",
        prompt="Analyze this",
        schema=SimpleSchema,
        model="test-gemini",
    )

    assert isinstance(result, SimpleSchema)
    assert result.title == "Test"
    assert result.score == 0.9


def test_analyze_video_freeform() -> None:
    """analyze_video returns raw text when schema is None."""
    from gemini import analyze_video

    client = _FakeClient("This video discusses game balance changes.")

    result = analyze_video(
        client,
        url="https://www.youtube.com/watch?v=test123",
        prompt="What is this about?",
        schema=None,
        model="test-gemini",
    )

    assert isinstance(result, str)
    assert "balance changes" in result


def test_analyze_video_returns_none_on_502() -> None:
    """analyze_video returns None immediately on 502."""
    from gemini import analyze_video

    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("502 Bad Gateway")

    result = analyze_video(
        client,
        url="https://www.youtube.com/watch?v=fail",
        prompt="Analyze",
        schema=SimpleSchema,
        model="test-gemini",
    )
    assert result is None
    # Should not retry on 502
    assert client.models.generate_content.call_count == 1


def test_analyze_video_retries_on_429() -> None:
    """analyze_video retries on 429 rate limit then succeeds."""
    from gemini import analyze_video

    expected = SimpleSchema(title="Recovered", score=0.5)
    call_count = 0

    def flaky_generate(**kwargs: object) -> _FakeResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return _FakeResponse(expected.model_dump_json())

    client = MagicMock()
    client.models.generate_content.side_effect = flaky_generate

    with patch("gemini.time.sleep"):
        result = analyze_video(
            client,
            url="https://www.youtube.com/watch?v=flaky",
            prompt="Analyze",
            schema=SimpleSchema,
            model="test-gemini",
        )

    assert result is not None
    assert result.title == "Recovered"
    assert call_count == 2


def test_synthesize_text() -> None:
    """synthesize_text returns model response text."""
    from gemini import synthesize_text

    client = _FakeClient("The main themes are balance and strategy.")

    result = synthesize_text(client, prompt="Summarize themes", model="test-gemini")
    assert "balance" in result
    assert "strategy" in result


def test_synthesize_text_returns_empty_on_failure() -> None:
    """synthesize_text returns empty string on exception."""
    from gemini import synthesize_text

    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("API down")

    result = synthesize_text(client, prompt="Summarize", model="test-gemini")
    assert result == ""


def test_model_version_parsing() -> None:
    """_model_version parses (major, minor) from endpoint names."""
    from gemini import _model_version

    assert _model_version("databricks-gemini-3-5-flash") == (3, 5)
    assert _model_version("databricks-gemini-3-1-flash-lite") == (3, 1)
    assert _model_version("databricks-gemini-3-flash") == (3, 0)
    assert _model_version("databricks-gemini-2-5-pro") == (2, 5)
    assert _model_version("databricks-claude-sonnet") == (0, 0)


def test_select_model_prefers_flash_v3() -> None:
    """_select_model picks the highest-version flash/lite model that is v3+."""
    from gemini import _select_model

    names = [
        "databricks-gemini-3-5-flash",
        "databricks-gemini-3-1-flash-lite",
        "databricks-gemini-2-5-flash",
        "databricks-gemini-2-5-pro",
    ]
    assert _select_model(names) == "databricks-gemini-3-5-flash"


def test_select_model_prefers_flash_over_pro() -> None:
    """Flash/lite tier wins over pro even when pro has a higher version."""
    from gemini import _select_model

    names = ["databricks-gemini-3-1-pro", "databricks-gemini-3-flash"]
    assert _select_model(names) == "databricks-gemini-3-flash"


def test_select_model_falls_back_to_pro_when_no_flash() -> None:
    """When no flash/lite v3+ exists, the highest v3+ model is used."""
    from gemini import _select_model

    names = ["databricks-gemini-3-5-pro", "databricks-gemini-2-5-flash"]
    assert _select_model(names) == "databricks-gemini-3-5-pro"


def test_select_model_falls_back_when_no_v3() -> None:
    """With no v3+ model, the newest available Gemini is used."""
    from gemini import _select_model

    names = ["databricks-gemini-2-5-flash", "databricks-gemini-2-0-flash"]
    assert _select_model(names) == "databricks-gemini-2-5-flash"


def test_select_model_empty_returns_none() -> None:
    """_select_model returns None when no Gemini endpoints are available."""
    from gemini import _select_model

    assert _select_model([]) is None


def test_resolve_gemini_model_env_override() -> None:
    """resolve_gemini_model honors the GEMINI_MODEL env override."""
    from gemini import GEMINI_MODEL_ENV, resolve_gemini_model

    with patch.dict("os.environ", {GEMINI_MODEL_ENV: "my-custom-gemini"}):
        assert resolve_gemini_model() == "my-custom-gemini"

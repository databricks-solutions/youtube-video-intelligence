"""Tests for the FastAPI backend endpoints.

Uses deps.override_* for clean dependency injection instead of
sys.modules patching.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

# Pre-register mock lakebase so real import never runs
sys.modules.setdefault("lakebase", MagicMock())

from deps import override_gemini_factory, override_lakebase, reset_gemini_client
from schemas import VideoSummary

import app as app_mod


def _make_mock_lakebase() -> MagicMock:
    """Create a mock lakebase module with all CRUD methods."""
    mock = MagicMock()
    mock.is_available.return_value = True
    mock.ensure_schema.return_value = None
    mock.list_single_analyses.return_value = []
    mock.list_theme_explorations.return_value = []
    mock.get_single_analysis.return_value = None
    mock.get_theme_exploration.return_value = None
    mock.save_single_analysis.return_value = "test-uuid"
    mock.save_theme_exploration.return_value = "test-uuid"
    return mock


@pytest.fixture
def mock_lakebase() -> MagicMock:
    """Provide a fresh mock lakebase for each test."""
    return _make_mock_lakebase()


@pytest.fixture
def client(mock_lakebase: MagicMock) -> TestClient:
    """Create a FastAPI test client with injected dependencies."""
    reset_gemini_client()
    override_lakebase(mock_lakebase)
    override_gemini_factory(lambda: MagicMock())
    return TestClient(app_mod.app)


# --- Static file serving ---


def test_index_html_served(client: TestClient) -> None:
    """The root URL serves the React SPA."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Video Intelligence" in response.text
    assert "react" in response.text.lower()


# --- History endpoints (empty state) ---


def test_video_history_empty(client: TestClient) -> None:
    """Video history returns empty list when no analyses exist."""
    response = client.get("/api/history/videos")
    assert response.status_code == 200
    assert response.json() == []


def test_theme_history_empty(client: TestClient) -> None:
    """Theme history returns empty list when no analyses exist."""
    response = client.get("/api/history/themes")
    assert response.status_code == 200
    assert response.json() == []


# --- History endpoints (with data) ---


def test_video_history_with_data(client: TestClient, mock_lakebase: MagicMock) -> None:
    """Video history returns formatted entries."""
    mock_lakebase.list_single_analyses.return_value = [
        {
            "id": "uuid-1",
            "video_title": "Test Video",
            "channel": "TestChannel",
            "question": "What happens?",
            "result_type": "freeform",
            "created_at": "2026-03-23 12:00:00",
        }
    ]
    response = client.get("/api/history/videos")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["video_title"] == "Test Video"
    assert data[0]["question"] == "What happens?"


def test_theme_history_with_data(client: TestClient, mock_lakebase: MagicMock) -> None:
    """Theme history returns formatted entries."""
    mock_lakebase.list_theme_explorations.return_value = [
        {
            "id": "uuid-2",
            "theme": "battle of hastings",
            "video_count": 10,
            "created_at": "2026-03-23 13:00:00",
        }
    ]
    response = client.get("/api/history/themes")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["theme"] == "battle of hastings"
    assert data[0]["video_count"] == 10


# --- History detail endpoints ---


def test_video_detail_not_found(client: TestClient) -> None:
    """Video detail returns error for non-existent ID."""
    response = client.get("/api/history/videos/nonexistent")
    assert response.status_code == 200
    assert response.json()["error"] == "Not found"


def test_theme_detail_not_found(client: TestClient) -> None:
    """Theme detail returns error for non-existent ID."""
    response = client.get("/api/history/themes/nonexistent")
    assert response.status_code == 200
    assert response.json()["error"] == "Not found"


def test_video_detail_with_summary(
    client: TestClient, mock_lakebase: MagicMock
) -> None:
    """Video detail returns formatted markdown for summary type."""
    mock_lakebase.get_single_analysis.return_value = {
        "id": "uuid-1",
        "video_url": "https://www.youtube.com/watch?v=abc123def45",
        "video_title": "Test Video",
        "channel": "TestChannel",
        "result_type": "summary",
        "result_json": {
            "sentiment": "positive",
            "tone": "casual",
            "main_topics": ["topic1", "topic2"],
            "key_points": ["point1", "point2"],
        },
    }
    response = client.get("/api/history/videos/uuid-1")
    assert response.status_code == 200
    data = response.json()
    assert "positive" in data["markdown"]
    assert "topic1" in data["markdown"]
    assert data["thumbnail"].startswith("https://img.youtube.com/vi/")


def test_video_detail_with_freeform(
    client: TestClient, mock_lakebase: MagicMock
) -> None:
    """Video detail returns raw text for freeform type."""
    mock_lakebase.get_single_analysis.return_value = {
        "id": "uuid-2",
        "video_url": "https://www.youtube.com/watch?v=xyz789ghi01",
        "video_title": "Another Video",
        "channel": "Ch",
        "result_type": "freeform",
        "result_json": {"text": "The answer is 42."},
    }
    response = client.get("/api/history/videos/uuid-2")
    data = response.json()
    assert data["markdown"] == "The answer is 42."


def test_video_detail_with_json_string_result(
    client: TestClient, mock_lakebase: MagicMock
) -> None:
    """Video detail handles result_json stored as a JSON string."""
    mock_lakebase.get_single_analysis.return_value = {
        "id": "uuid-3",
        "video_url": "https://www.youtube.com/watch?v=test123test1",
        "video_title": "Stringified",
        "channel": "Ch",
        "result_type": "freeform",
        "result_json": json.dumps({"text": "Stringified result"}),
    }
    response = client.get("/api/history/videos/uuid-3")
    data = response.json()
    assert data["markdown"] == "Stringified result"


def test_theme_detail_with_data(client: TestClient, mock_lakebase: MagicMock) -> None:
    """Theme detail returns synthesis and mosaic."""
    mock_lakebase.get_theme_exploration.return_value = {
        "id": "uuid-3",
        "theme": "dogs",
        "synthesis": "Dogs are popular.",
        "video_count": 2,
        "videos": [
            {
                "video_url": "https://www.youtube.com/watch?v=dog1xxxxxxx",
                "video_title": "Dog Video 1",
                "channel": "DogCh",
            },
            {
                "video_url": "https://www.youtube.com/watch?v=dog2xxxxxxx",
                "video_title": "Dog Video 2",
                "channel": "DogCh",
            },
        ],
    }
    response = client.get("/api/history/themes/uuid-3")
    data = response.json()
    assert data["synthesis"] == "Dogs are popular."
    assert len(data["mosaic"]) == 2
    assert data["mosaic"][0]["title"] == "Dog Video 1"


# --- SSE endpoints ---


def test_analyze_video_sse_returns_stream(client: TestClient) -> None:
    """The analyze-video endpoint returns an SSE stream."""
    with (
        patch.object(app_mod, "get_video_metadata") as mock_meta,
        patch.object(app_mod, "analyze_video") as mock_analyze,
        patch.object(app_mod, "resolve_gemini_model", return_value="test-gemini"),
    ):
        mock_meta.return_value = MagicMock(
            title="Test",
            channel="Ch",
            view_count=0,
            duration_seconds=60,
            description="",
        )
        mock_analyze.return_value = "Test answer"

        response = client.post(
            "/api/analyze-video",
            json={
                "url": "https://www.youtube.com/watch?v=test1234567",
                "question": "test?",
            },
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert "data:" in response.text


def test_analyze_video_sse_summary_mode(client: TestClient) -> None:
    """Summary mode returns structured result with markdown."""
    summary = VideoSummary(
        title="T",
        main_topics=["AI"],
        key_points=["KP"],
        sentiment="positive",
        tone="excited",
    )
    with (
        patch.object(app_mod, "get_video_metadata") as mock_meta,
        patch.object(app_mod, "analyze_video") as mock_analyze,
        patch.object(app_mod, "resolve_gemini_model", return_value="test-gemini"),
    ):
        mock_meta.return_value = MagicMock(
            title="Test", channel="Ch", duration_seconds=120
        )
        mock_analyze.return_value = summary

        response = client.post(
            "/api/analyze-video",
            json={"url": "https://www.youtube.com/watch?v=test1234567"},
        )
        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.strip().split("\n")
            if line.startswith("data: ")
        ]
        result_events = [e for e in events if e.get("type") == "result"]
        assert len(result_events) == 1
        assert "positive" in result_events[0]["markdown"]


def test_analyze_video_sse_metadata_degrades(client: TestClient) -> None:
    """Metadata fetch failure is non-fatal: analysis proceeds without it.

    Metadata (title/channel) is cosmetic; the Gemini FMAPI fetches the video
    server-side, so a container DNS failure on the oEmbed call must not abort
    the request.
    """
    with (
        patch.object(app_mod, "get_video_metadata") as mock_meta,
        patch.object(app_mod, "analyze_video") as mock_analyze,
        patch.object(app_mod, "resolve_gemini_model", return_value="test-gemini"),
    ):
        mock_meta.side_effect = RuntimeError(
            "<urlopen error [Errno -3] Temporary failure in name resolution>"
        )
        mock_analyze.return_value = "Test answer"

        response = client.post(
            "/api/analyze-video",
            json={
                "url": "https://www.youtube.com/watch?v=bad_url_test",
                "question": "test?",
            },
        )
        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.strip().split("\n")
            if line.startswith("data: ")
        ]
        assert not [e for e in events if e.get("type") == "error"]
        assert len([e for e in events if e.get("type") == "result"]) == 1
        metadata_events = [e for e in events if e.get("type") == "metadata"]
        assert len(metadata_events) == 1
        assert metadata_events[0]["title"] == "YouTube video"


# --- get_user_email ---


def test_get_user_email_from_header() -> None:
    """Extracts email from x-forwarded-email header."""
    mock_request = MagicMock()
    mock_request.headers = {"x-forwarded-email": "user@example.com"}
    assert app_mod.get_user_email(mock_request) == "user@example.com"


def test_get_user_email_fallback() -> None:
    """Falls back to x-forwarded-preferred-username."""
    mock_request = MagicMock()
    mock_request.headers = {
        "x-forwarded-email": "",
        "x-forwarded-preferred-username": "user2@example.com",
    }
    assert app_mod.get_user_email(mock_request) == "user2@example.com"


def test_get_user_email_missing() -> None:
    """Returns empty string when no identity headers present."""
    mock_request = MagicMock()
    mock_request.headers = {}
    assert app_mod.get_user_email(mock_request) == ""

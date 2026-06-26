"""Tests for the formatting module: pure functions, no mocking needed."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from formatting import (
    format_summary_markdown,
    format_summary_markdown_from_dict,
    format_video_detail_markdown,
    parse_blacklist,
    parse_date,
)
from schemas import VideoSummary

# --- parse_date ---


def test_parse_date_valid() -> None:
    """Parses ISO date string."""
    assert parse_date("2024-03-15") == date(2024, 3, 15)


def test_parse_date_with_whitespace() -> None:
    """Strips whitespace before parsing."""
    assert parse_date("  2024-03-15  ") == date(2024, 3, 15)


def test_parse_date_empty() -> None:
    """Returns None for empty input."""
    assert parse_date("") is None


def test_parse_date_none_like() -> None:
    """Returns None for whitespace-only input."""
    assert parse_date("   ") is None


def test_parse_date_invalid() -> None:
    """Returns None for malformed dates."""
    assert parse_date("not-a-date") is None


# --- parse_blacklist ---


def test_parse_blacklist_valid() -> None:
    """Parses newline-separated channels to lowercase set."""
    result = parse_blacklist("Channel One\nCHANNEL TWO\n")
    assert result == {"channel one", "channel two"}


def test_parse_blacklist_empty_lines() -> None:
    """Ignores empty lines."""
    result = parse_blacklist("foo\n\n\nbar\n")
    assert result == {"foo", "bar"}


def test_parse_blacklist_empty_string() -> None:
    """Returns None for empty string."""
    assert parse_blacklist("") is None


def test_parse_blacklist_only_whitespace() -> None:
    """Returns None when all lines are whitespace."""
    assert parse_blacklist("  \n  \n") is None


# --- format_summary_markdown ---


def test_format_summary_markdown() -> None:
    """Formats a VideoSummary into readable markdown."""
    summary = VideoSummary(
        title="Test",
        main_topics=["AI", "ML"],
        key_points=["Point A", "Point B"],
        sentiment="positive",
        tone="enthusiastic",
    )
    md = format_summary_markdown(summary)
    assert "**Sentiment:** positive" in md
    assert "**Tone:** enthusiastic" in md
    assert "AI, ML" in md
    assert "- Point A" in md
    assert "- Point B" in md


# --- format_summary_markdown_from_dict ---


def test_format_summary_markdown_from_dict() -> None:
    """Formats a dict (from Lakebase) into markdown."""
    data = {
        "sentiment": "negative",
        "tone": "critical",
        "main_topics": ["topic1"],
        "key_points": ["kp1", "kp2"],
    }
    md = format_summary_markdown_from_dict(data)
    assert "negative" in md
    assert "critical" in md
    assert "topic1" in md
    assert "- kp1" in md


def test_format_summary_markdown_from_dict_missing_keys() -> None:
    """Handles missing keys gracefully."""
    md = format_summary_markdown_from_dict({})
    assert "**Sentiment:**" in md
    assert "**Key Points:**" in md


# --- format_video_detail_markdown ---


def test_format_video_detail_freeform() -> None:
    """Freeform type returns the raw text."""
    md = format_video_detail_markdown("freeform", {"text": "The answer is 42."})
    assert md == "The answer is 42."


def test_format_video_detail_summary() -> None:
    """Summary type returns formatted markdown."""
    data = {
        "sentiment": "neutral",
        "tone": "calm",
        "main_topics": ["A"],
        "key_points": ["B"],
    }
    md = format_video_detail_markdown("summary", data)
    assert "neutral" in md
    assert "- B" in md

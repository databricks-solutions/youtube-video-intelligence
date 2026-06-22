"""Pure formatting functions for analysis results.

All functions are side-effect-free and operate on plain data, making them
trivially testable without mocking.
"""

from datetime import date, datetime

from schemas import VideoSummary


def format_yt_date(yt_date: str) -> str:
    """Convert yt-dlp YYYYMMDD string to ISO YYYY-MM-DD.

    Args:
        yt_date: Date string in YYYYMMDD format from yt-dlp.

    Returns:
        ISO date string, or empty string if input is invalid.
    """
    if not yt_date or len(yt_date) != 8:
        return ""
    try:
        return datetime.strptime(yt_date, "%Y%m%d").date().isoformat()
    except ValueError:
        return ""


def parse_date(text: str) -> date | None:
    """Parse a YYYY-MM-DD string to a date object.

    Args:
        text: ISO date string.

    Returns:
        Parsed date, or None if input is empty or invalid.
    """
    if not text or not text.strip():
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def parse_blacklist(text: str) -> set[str] | None:
    """Parse newline-separated blacklist text into a lowercase set.

    Args:
        text: Newline-separated channel names.

    Returns:
        Set of lowercased channel names, or None if empty.
    """
    if not text:
        return None
    channels = {c.strip().lower() for c in text.splitlines() if c.strip()}
    return channels or None


def format_summary_markdown(summary: VideoSummary) -> str:
    """Format a VideoSummary into display markdown.

    Args:
        summary: Parsed video summary from Gemini.

    Returns:
        Markdown string with sentiment, topics, and key points.
    """
    lines = [
        f"**Sentiment:** {summary.sentiment} | **Tone:** {summary.tone}",
        "",
        f"**Main Topics:** {', '.join(summary.main_topics)}",
        "",
        "**Key Points:**",
    ]
    for point in summary.key_points:
        lines.append(f"- {point}")
    return "\n".join(lines)


def format_summary_markdown_from_dict(data: dict) -> str:
    """Format a summary result dict (from Lakebase JSON) into markdown.

    Args:
        data: Dictionary with sentiment, tone, main_topics, key_points keys.

    Returns:
        Markdown string.
    """
    lines = [
        f"**Sentiment:** {data.get('sentiment', '')} | "
        f"**Tone:** {data.get('tone', '')}",
        "",
        f"**Main Topics:** {', '.join(data.get('main_topics', []))}",
        "",
        "**Key Points:**",
    ]
    for point in data.get("key_points", []):
        lines.append(f"- {point}")
    return "\n".join(lines)


def format_video_detail_markdown(result_type: str, result_data: dict) -> str:
    """Format a stored video analysis result into display markdown.

    Args:
        result_type: Either "freeform" or "summary".
        result_data: The stored result JSON dict.

    Returns:
        Formatted markdown string.
    """
    if result_type == "freeform":
        return result_data.get("text", "")
    return format_summary_markdown_from_dict(result_data)

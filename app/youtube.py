"""YouTube search and metadata via yt-dlp."""

import json
import logging
import math
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime

import yt_dlp

logger = logging.getLogger(__name__)


def _parse_yt_date(yt_date: str) -> date | None:
    """Parse yt-dlp YYYYMMDD string to a date object."""
    if not yt_date or len(yt_date) != 8:
        return None
    try:
        return datetime.strptime(yt_date, "%Y%m%d").date()
    except ValueError:
        return None


VIDEO_ID_PATTERN = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)"
    r"([a-zA-Z0-9_-]{11})"
)


@dataclass
class VideoSearchResult:
    """Metadata for a YouTube video returned by search."""

    url: str
    title: str
    channel: str
    upload_date: str  # YYYYMMDD from yt-dlp
    duration_seconds: int
    view_count: int
    search_rank: int  # 0-based position in YouTube search results


def search_videos(query: str, max_results: int = 10) -> list[VideoSearchResult]:
    """Search YouTube for videos matching a query.

    Args:
        query: Free-text search query.
        max_results: Maximum number of results to return.

    Returns:
        List of VideoSearchResult with metadata (no video download).
    """
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "default_search": "ytsearch",
        # Own instance + cache off per call, so searches can run concurrently.
        "cachedir": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
        entries = (info or {}).get("entries") or []
        results: list[VideoSearchResult] = []
        for rank, entry in enumerate(entries):
            if not entry:
                continue
            video_id = entry.get("id", "")
            results.append(
                VideoSearchResult(
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    title=entry.get("title", ""),
                    channel=entry.get("channel", entry.get("uploader", "")),
                    upload_date=entry.get("upload_date", ""),
                    duration_seconds=int(entry.get("duration") or 0),
                    view_count=int(entry.get("view_count") or 0),
                    search_rank=rank,
                )
            )
        return results
    except Exception as e:
        logger.error("YouTube search failed: %s", e)
        return []


@dataclass
class VideoMetadata:
    """Rich metadata for a single YouTube video."""

    title: str
    channel: str
    description: str
    duration_seconds: int
    view_count: int
    upload_date: str


def get_video_metadata(url: str) -> VideoMetadata:
    """Fetch metadata for a single YouTube video via oEmbed API.

    Uses YouTube's oEmbed endpoint which is reliable and never triggers
    bot detection, unlike full yt-dlp extraction.

    Args:
        url: YouTube video URL.

    Returns:
        VideoMetadata with title and channel. Views, duration, and
        description are not available via oEmbed.

    Raises:
        RuntimeError: If metadata cannot be fetched.
    """
    oembed_api = (
        f"https://www.youtube.com/oembed?url={urllib.parse.quote(url, safe='')}"
        "&format=json"
    )
    try:
        req = urllib.request.Request(oembed_api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return VideoMetadata(
            title=data.get("title", ""),
            channel=data.get("author_name", ""),
            description="",
            duration_seconds=0,
            view_count=0,
            upload_date="",
        )
    except Exception as e:
        logger.error("Failed to get video metadata for %s: %s", url, e)
        raise RuntimeError(str(e)) from e


def get_video_duration(url: str) -> int | None:
    """Fetch duration in seconds for a single YouTube video.

    Args:
        url: YouTube video URL.

    Returns:
        Duration in seconds, or None if unavailable.
    """
    try:
        meta = get_video_metadata(url)
    except RuntimeError:
        return None
    return meta.duration_seconds if meta.duration_seconds else None


def filter_videos(
    videos: list[VideoSearchResult],
    date_start: date | None = None,
    date_end: date | None = None,
    blacklist: set[str] | None = None,
    allowlist: set[str] | None = None,
    max_duration_seconds: int | None = None,
) -> list[VideoSearchResult]:
    """Filter search results by date range, channel blacklist, and duration.

    Args:
        videos: List of search results to filter.
        date_start: Earliest upload date (inclusive).
        date_end: Latest upload date (inclusive).
        blacklist: Set of lowercased channel names to exclude.
        allowlist: If set, only videos from these lowercased channel names are
            kept; everything else is excluded.
        max_duration_seconds: Maximum video length in seconds. Videos longer
            than this are excluded.

    Returns:
        Filtered list of VideoSearchResult.
    """
    filtered: list[VideoSearchResult] = []
    for video in videos:
        if blacklist and video.channel.lower() in blacklist:
            continue
        if allowlist and video.channel.lower() not in allowlist:
            continue
        if max_duration_seconds and video.duration_seconds > max_duration_seconds:
            continue
        upload = _parse_yt_date(video.upload_date)
        if upload:
            if date_start and upload < date_start:
                continue
            if date_end and upload > date_end:
                continue
        filtered.append(video)
    return filtered


def rank_videos(
    videos: list[VideoSearchResult],
    top_n: int = 10,
) -> list[VideoSearchResult]:
    """Rank videos by a weighted score of views, relevancy, and recency.

    Scoring (each component normalized to 0-1):
    - Relevancy (40%): inverse of YouTube search rank position.
    - Views (35%): log-scaled view count.
    - Recency (25%): days since upload, decayed over 2 years.

    Args:
        videos: Filtered search results to rank.
        top_n: Number of top videos to return.

    Returns:
        Top N videos sorted by descending composite score.
    """
    if not videos:
        return []

    today = date.today()
    max_rank = max(v.search_rank for v in videos)
    max_log_views = max(math.log1p(v.view_count) for v in videos) or 1.0
    decay_days = 730.0  # 2-year half-life

    scored: list[tuple[float, VideoSearchResult]] = []
    for video in videos:
        # Relevancy: top search result = 1.0, last = 0.0
        relevancy = 1.0 - (video.search_rank / max(max_rank, 1))

        # Views: log-scaled, normalized against the max in this batch
        views_score = math.log1p(video.view_count) / max_log_views

        # Recency: exponential decay from today
        recency = 0.5
        upload = _parse_yt_date(video.upload_date)
        if upload:
            days_old = max((today - upload).days, 0)
            recency = math.exp(-days_old / decay_days)

        score = 0.40 * relevancy + 0.35 * views_score + 0.25 * recency
        scored.append((score, video))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [video for _, video in scored[:top_n]]


def compute_end_offset(duration_seconds: int, trim_seconds: int = 60) -> str:
    """Compute Gemini end_offset to trim the last N seconds of a video.

    Args:
        duration_seconds: Total video duration in seconds.
        trim_seconds: Seconds to trim from the end.

    Returns:
        End offset string for Gemini VideoMetadata (e.g. "540s").
    """
    clipped = max(duration_seconds - trim_seconds, 60)
    return f"{clipped}s"


def thumbnail_url(video_url: str) -> str:
    """Extract YouTube video ID and return a thumbnail URL.

    Args:
        video_url: YouTube video URL.

    Returns:
        Medium-quality thumbnail URL, or empty string if ID not found.
    """
    match = VIDEO_ID_PATTERN.search(video_url)
    if match:
        return f"https://img.youtube.com/vi/{match.group(1)}/mqdefault.jpg"
    return ""

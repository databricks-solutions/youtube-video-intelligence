"""YouTube Video Intelligence: FastAPI backend.

Thin routing layer. Business logic lives in deps, formatting, gemini,
youtube, and lakebase modules.
"""

import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from deps import gemini_semaphore, get_gemini_client, get_lakebase
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from formatting import (
    format_summary_markdown,
    format_video_detail_markdown,
    parse_blacklist,
    parse_date,
)
from gemini import analyze_video, resolve_gemini_model, synthesize_text
from schemas import (
    ThemeAnalysis,
    ThemeAnalysisRequest,
    VideoAnalysisRequest,
    VideoSummary,
)
from youtube import (
    VideoMetadata,
    VideoSearchResult,
    compute_end_offset,
    filter_videos,
    get_video_metadata,
    rank_videos,
    search_videos,
    thumbnail_url,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="YouTube Video Intelligence")

# --- Prompts ---

SUMMARY_PROMPT = (
    "Watch this YouTube video carefully. Provide a structured summary "
    "including the title, main topics discussed, key points made by the "
    "creator, the overall sentiment (positive/negative/mixed/neutral), "
    "and the tone of the content."
)

THEME_PROMPT_TEMPLATE = (
    "Watch this YouTube video. Analyze it specifically in the context of "
    'this theme: "{theme}". Explain how relevant the video is to this '
    "theme, list the key points the creator makes related to it, describe "
    "the overall sentiment, and summarize the creator's stance."
)


# --- Shared helpers ---


def get_user_email(request: Request) -> str:
    """Extract user email from Databricks App proxy headers.

    Args:
        request: FastAPI request with forwarded headers.

    Returns:
        User email string, or empty string if not available.
    """
    return request.headers.get("x-forwarded-email", "") or request.headers.get(
        "x-forwarded-preferred-username", ""
    )


def _json_default(obj: object) -> dict:
    """JSON serializer for Pydantic models in SSE events.

    Args:
        obj: Object to serialize.

    Returns:
        Dictionary representation via model_dump().

    Raises:
        TypeError: If object is not a Pydantic model.
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


async def sse_stream(q: queue.Queue) -> AsyncGenerator[str, None]:
    """Yield SSE events from a thread-safe queue until done or error.

    Args:
        q: Queue producing dicts with a "type" key.

    Yields:
        SSE-formatted data lines.
    """
    keepalive_payload = json.dumps({"type": "keepalive"})
    while True:
        try:
            event = await asyncio.to_thread(q.get, timeout=2)
            yield f"data: {json.dumps(event, default=_json_default)}\n\n"
            if event.get("type") in ("done", "error"):
                break
        except Exception:
            yield f"data: {keepalive_payload}\n\n"


# --- Theme task storage ---
# Decouples long-running theme analysis from the SSE connection so the
# background thread keeps running even if the proxy drops the stream.
# The client auto-reconnects and replays missed events.

_theme_tasks: dict[str, dict] = {}
_theme_tasks_lock = threading.Lock()
_TASK_MAX_AGE_S = 600  # Clean up tasks older than 10 minutes


def _task_push(task_id: str, event: dict) -> None:
    """Append an event to a task's event list (thread-safe).

    Args:
        task_id: Task identifier.
        event: SSE event dict.
    """
    with _theme_tasks_lock:
        task = _theme_tasks.get(task_id)
        if task is not None:
            task["events"].append(event)
            if event.get("type") in ("done", "error"):
                task["done"] = True


def _cleanup_stale_tasks() -> None:
    """Remove tasks older than _TASK_MAX_AGE_S."""
    cutoff = time.time() - _TASK_MAX_AGE_S
    with _theme_tasks_lock:
        stale = [tid for tid, t in _theme_tasks.items() if t["created"] < cutoff]
        for tid in stale:
            del _theme_tasks[tid]


async def _task_sse_stream(task: dict, after: int) -> AsyncGenerator[str, None]:
    """Stream events from a task, starting after the given index.

    Replays past events immediately, then polls for new ones. Sends
    keepalive data events every 2 seconds to survive proxy timeouts.

    Args:
        task: Task dict with "events" list and "done" flag.
        after: Event index to start from (0 = replay all).

    Yields:
        SSE-formatted data lines.
    """
    cursor = after
    keepalive_payload = json.dumps({"type": "keepalive"})
    while True:
        events = task["events"]
        if cursor < len(events):
            while cursor < len(events):
                event = events[cursor]
                cursor += 1
                yield f"data: {json.dumps(event, default=_json_default)}\n\n"
                if event.get("type") in ("done", "error"):
                    return
        elif task["done"]:
            return
        else:
            await asyncio.sleep(1)
            yield f"data: {keepalive_payload}\n\n"


# --- API: Single Video Analysis (SSE) ---


def _run_video_analysis(
    q: queue.Queue, url: str, question: str, user_email: str
) -> None:
    """Background thread: fetch metadata, analyze with Gemini, save to Lakebase.

    Args:
        q: Queue for SSE events.
        url: YouTube video URL.
        question: Optional user question (empty string for summary mode).
        user_email: Current user's email for history.
    """
    try:
        q.put({"type": "status", "message": "Fetching video info..."})
        # Metadata (title/channel) is cosmetic: the Gemini FMAPI fetches the
        # video server-side, so analysis proceeds even when the app container
        # cannot reach YouTube (e.g. DNS resolution fails on Databricks Apps).
        try:
            meta = get_video_metadata(url)
        except RuntimeError as exc:
            logger.warning("Proceeding without video metadata for %s: %s", url, exc)
            meta = VideoMetadata(
                title="",
                channel="",
                description="",
                duration_seconds=0,
                view_count=0,
                upload_date="",
            )

        display_title = meta.title or "YouTube video"
        q.put(
            {
                "type": "metadata",
                "title": display_title,
                "channel": meta.channel,
                "thumbnail": thumbnail_url(url),
                "url": url,
            }
        )

        q.put({"type": "status", "message": "Analyzing video with Gemini..."})
        client = get_gemini_client()
        end_offset = (
            compute_end_offset(meta.duration_seconds) if meta.duration_seconds else None
        )

        result_type, result_json, md = _analyze_single_video(
            client, url, question, end_offset
        )

        lb = get_lakebase()
        if lb:
            lb.save_single_analysis(
                video_url=url,
                video_title=display_title,
                channel=meta.channel,
                question=question.strip() if question and question.strip() else None,
                result_type=result_type,
                result_json=result_json,
                user_email=user_email,
            )

        q.put({"type": "result", "markdown": md, "model": resolve_gemini_model()})
        q.put({"type": "done"})
    except Exception as exc:
        logger.exception("Video analysis error")
        q.put({"type": "error", "message": str(exc)})


def _analyze_single_video(
    client: object,
    url: str,
    question: str,
    end_offset: str | None,
) -> tuple[str, dict, str]:
    """Run Gemini analysis and format the result.

    Args:
        client: Gemini client.
        url: YouTube video URL.
        question: User question (empty for summary mode).
        end_offset: Optional video end offset.

    Returns:
        Tuple of (result_type, result_json, markdown).
    """
    if question and question.strip():
        prompt = (
            "Watch this YouTube video and answer the following question "
            f"based on its content:\n\n{question.strip()}"
        )
        result = analyze_video(
            client, url=url, prompt=prompt, schema=None, fps=0.1, end_offset=end_offset
        )
        result_json = {"text": result} if result else {}
        md = result or "Analysis failed."
        return "freeform", result_json, md

    result = analyze_video(
        client,
        url=url,
        prompt=SUMMARY_PROMPT,
        schema=VideoSummary,
        fps=0.1,
        end_offset=end_offset,
    )
    if result:
        return "summary", result.model_dump(), format_summary_markdown(result)
    return "summary", {}, "Analysis failed. The video may be unavailable or too short."


@app.post("/api/analyze-video")
async def analyze_video_endpoint(
    data: VideoAnalysisRequest, request: Request
) -> StreamingResponse:
    """SSE endpoint for single video analysis."""
    user = get_user_email(request)
    q: queue.Queue = queue.Queue()
    threading.Thread(
        target=_run_video_analysis,
        args=(q, data.url.strip(), data.question, user),
        daemon=True,
    ).start()
    return StreamingResponse(sse_stream(q), media_type="text/event-stream")


# --- API: Theme Analysis (task-based) ---


def _theme_search_queries(theme: str, allow: set[str] | None) -> list[str]:
    """Build the YouTube search queries for a theme.

    With a channel allowlist, scope the search to those channels. A broad theme
    search (e.g. "crypto") returns YouTube's top global results, dominated by
    channels specialised in that term; an allowed channel that is not a
    topic-specialist will not appear there (regardless of its size), so a
    post-filter alone would drop everything. Without an allowlist, one query.

    Args:
        theme: The (stripped) search theme.
        allow: Lowercased allowed channel names, or None.

    Returns:
        One query per allowed channel, or a single theme query.
    """
    if allow:
        return [f"{theme} {channel}" for channel in sorted(allow)]
    return [theme]


def _run_theme_analysis(
    task_id: str,
    req: ThemeAnalysisRequest,
    user_email: str,
) -> None:
    """Background thread: search, filter, analyze videos, synthesize.

    Pushes events to the task store so the SSE stream can be
    reconnected independently of this thread's lifetime.

    Args:
        task_id: Task identifier for event storage.
        req: Validated theme request (theme, date/duration filters, channel
            allow/blocklists).
        user_email: Current user's email for history.
    """

    def emit(event: dict) -> None:
        _task_push(task_id, event)

    theme = req.theme.strip()

    try:
        emit({"type": "progress", "pct": 2, "message": "Searching YouTube..."})
        allow = parse_blacklist(req.allowlist)
        # Allowlist: scope to fewer results per channel (the channel's on-theme
        # videos rank high in a scoped query) and run the channel searches in
        # parallel. ponytail: bounded pool; fine for a handful of channels.
        per_query = 20 if allow else 100
        queries = _theme_search_queries(theme, allow)
        results: list[VideoSearchResult] = []
        seen: set[str] = set()
        with ThreadPoolExecutor(max_workers=min(len(queries), 8)) as pool:
            for batch in pool.map(
                lambda q: search_videos(q, max_results=per_query), queries
            ):
                for video in batch:
                    if video.url not in seen:
                        seen.add(video.url)
                        results.append(video)
        if not results:
            emit(
                {"type": "error", "message": "No videos found. Try a different query."}
            )
            return

        emit({"type": "progress", "pct": 5, "message": "Filtering and ranking..."})
        filtered = filter_videos(
            results,
            date_start=parse_date(req.date_start),
            date_end=parse_date(req.date_end),
            blacklist=parse_blacklist(req.blacklist),
            allowlist=allow,
            max_duration_seconds=req.max_duration_min * 60
            if req.max_duration_min
            else None,
        )
        if not filtered:
            emit(
                {
                    "type": "error",
                    "message": "All results filtered out. Adjust your filters.",
                }
            )
            return

        to_analyze = rank_videos(filtered, top_n=3)
        n = len(to_analyze)

        emit({"type": "videos_found", "videos": _videos_to_dicts(to_analyze)})
        emit({"type": "progress", "pct": 10, "message": f"Analyzing 0/{n} videos..."})

        analyses, failed = _analyze_videos_parallel(emit, to_analyze, theme, n)

        synthesis = _synthesize_analyses(
            analyses, theme, emit, question=req.question.strip()
        )
        mosaic = [
            {
                "url": a["url"],
                "title": a["title"],
                "thumbnail": thumbnail_url(a["url"]),
            }
            for a in analyses
        ]
        progress_text = f"**{len(analyses)}** of {n} videos analyzed."
        if failed:
            progress_text += f" {len(failed)} failed."

        lb = get_lakebase()
        if lb:
            lb.save_theme_exploration(
                theme=theme,
                video_count=len(analyses),
                synthesis=synthesis,
                video_analyses=analyses,
                user_email=user_email,
            )

        emit(
            {
                "type": "result",
                "mosaic": mosaic,
                "synthesis": synthesis,
                "progress_text": progress_text,
                "failed": failed,
                "model": resolve_gemini_model(),
            }
        )
        emit({"type": "done"})
    except Exception as exc:
        logger.exception("Theme analysis error")
        emit({"type": "error", "message": str(exc)})


def _videos_to_dicts(videos: list[VideoSearchResult]) -> list[dict]:
    """Convert search results to serializable dicts for the SSE event.

    Args:
        videos: List of video search results.

    Returns:
        List of dicts with url, title, channel, thumbnail, duration, views.
    """
    return [
        {
            "url": v.url,
            "title": v.title,
            "channel": v.channel,
            "thumbnail": thumbnail_url(v.url),
            "duration": v.duration_seconds,
            "views": v.view_count,
        }
        for v in videos
    ]


def _analyze_videos_parallel(
    emit: Callable[[dict], None],
    videos: list[VideoSearchResult],
    theme: str,
    total: int,
) -> tuple[list[dict], list[str]]:
    """Analyze videos in parallel with Gemini, reporting progress.

    Args:
        emit: Callback to push SSE events.
        videos: Videos to analyze.
        theme: Theme context for the analysis prompt.
        total: Total video count (for progress reporting).

    Returns:
        Tuple of (successful analyses, failed video titles).
    """
    client = get_gemini_client()
    prompt = THEME_PROMPT_TEMPLATE.format(theme=theme)
    analyses: list[dict] = []
    failed: list[str] = []

    def _analyze_one(
        video: VideoSearchResult,
    ) -> tuple[VideoSearchResult, ThemeAnalysis | None, str | None]:
        end_offset = (
            compute_end_offset(video.duration_seconds)
            if video.duration_seconds
            else None
        )
        with gemini_semaphore:
            try:
                result = analyze_video(
                    client,
                    url=video.url,
                    prompt=prompt,
                    schema=ThemeAnalysis,
                    fps=0.1,
                    end_offset=end_offset,
                )
                return video, result, None
            except Exception as exc:
                return video, None, str(exc)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_analyze_one, v): v for v in videos}
        for i, future in enumerate(as_completed(futures), start=1):
            video, result, error = future.result()
            if result and isinstance(result, ThemeAnalysis):
                analyses.append(
                    {
                        "title": video.title,
                        "channel": video.channel,
                        "url": video.url,
                        "analysis": result,
                    }
                )
            else:
                err_msg = f" ({error})" if error else ""
                failed.append(f"{video.title}{err_msg}")
                if error and "REQUEST_LIMIT_EXCEEDED" in error:
                    emit(
                        {
                            "type": "progress",
                            "pct": 10 + int(80 * i / total),
                            "message": f"Analyzed {i}/{total} videos "
                            "(hitting workspace rate limits)...",
                        }
                    )
                    continue
            pct = 10 + int(80 * i / total)
            emit(
                {
                    "type": "progress",
                    "pct": pct,
                    "message": f"Analyzed {i}/{total} videos...",
                }
            )

    return analyses, failed


def _synthesize_analyses(
    analyses: list[dict],
    theme: str,
    emit: Callable[[dict], None],
    question: str = "",
) -> str:
    """Synthesize cross-video themes, or answer a question, from the analyses.

    Args:
        analyses: List of dicts with "analysis" ThemeAnalysis objects.
        theme: The search theme for context.
        emit: Callback to push SSE progress events.
        question: If set, answer it across the videos instead of producing a
            generic cross-video synthesis.

    Returns:
        Synthesis (or answer) markdown text.
    """
    if not analyses:
        return "No videos were successfully analyzed."
    if not question and len(analyses) < 2:
        return "Only one video analyzed. Need at least two for cross-video synthesis."

    emit({"type": "progress", "pct": 91, "message": "Rate limit cooldown..."})
    _sleep_with_progress(emit, seconds=10, pct_start=91, pct_end=94)

    client = get_gemini_client()
    summaries = "\n\n".join(
        f"**{a['title']}** ({a['channel']}): "
        f"{a['analysis'].relevance_to_theme}. "
        f"Key points: {'; '.join(a['analysis'].key_points)}"
        for a in analyses
    )
    intro = f'The following are analyses of YouTube videos about "{theme}":\n\n'
    if question:
        synthesis_prompt = (
            f"{intro}{summaries}\n\n"
            f"Answer this question using only these videos: {question}\n"
            "If the videos do not address it, say so. Be concise."
        )
    else:
        synthesis_prompt = (
            f"{intro}{summaries}\n\n"
            "Synthesize the major themes, points of agreement and "
            "disagreement, and overall sentiment across these videos. "
            "Be concise."
        )
    for attempt in range(3):
        pct = 94 + attempt * 2
        emit(
            {
                "type": "progress",
                "pct": pct,
                "message": f"Synthesizing cross-video themes (attempt {attempt + 1})...",
            }
        )
        synthesis = synthesize_text(client, synthesis_prompt)
        if synthesis:
            return synthesis
        emit(
            {
                "type": "progress",
                "pct": pct + 1,
                "message": "Synthesis rate-limited, retrying...",
            }
        )
        _sleep_with_progress(emit, seconds=15, pct_start=pct + 1, pct_end=pct + 2)
    return (
        "Synthesis unavailable (workspace token rate limit exceeded). "
        "The individual video analyses were completed."
    )


def _sleep_with_progress(
    emit: Callable[[dict], None],
    seconds: int,
    pct_start: int,
    pct_end: int,
) -> None:
    """Sleep while sending progress events to keep the SSE connection alive.

    Args:
        emit: Callback to push SSE progress events.
        seconds: Total seconds to sleep.
        pct_start: Progress percentage at start of sleep.
        pct_end: Progress percentage at end of sleep.
    """
    interval = 2
    steps = max(seconds // interval, 1)
    for i in range(steps):
        time.sleep(interval)
        pct = pct_start + int((pct_end - pct_start) * (i + 1) / steps)
        emit({"type": "progress", "pct": pct, "message": "Preparing synthesis..."})


@app.post("/api/analyze-theme")
async def analyze_theme_endpoint(
    data: ThemeAnalysisRequest, request: Request
) -> JSONResponse:
    """Start theme analysis and return a task ID.

    The analysis runs in a background thread. Events are stored
    server-side and streamed via GET /api/analyze-theme/{task_id}/events.
    """
    _cleanup_stale_tasks()
    user = get_user_email(request)
    task_id = uuid.uuid4().hex[:12]
    with _theme_tasks_lock:
        _theme_tasks[task_id] = {"events": [], "done": False, "created": time.time()}
    threading.Thread(
        target=_run_theme_analysis,
        args=(task_id, data, user),
        daemon=True,
    ).start()
    return JSONResponse({"task_id": task_id})


@app.get("/api/analyze-theme/{task_id}/events")
async def theme_events(task_id: str, after: int = 0) -> StreamingResponse:
    """SSE stream of theme analysis events.

    Supports reconnection: pass ?after=N to skip the first N events.
    The background analysis thread runs independently of this stream.
    """
    task = _theme_tasks.get(task_id)
    if not task:
        error_payload = json.dumps(
            {"type": "error", "message": "Task not found or expired."}
        )
        return StreamingResponse(
            iter([f"data: {error_payload}\n\n"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return StreamingResponse(
        _task_sse_stream(task, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- API: History ---


@app.get("/api/history/videos")
async def get_video_history(request: Request) -> list[dict]:
    """Get single video analysis history for the current user."""
    lb = get_lakebase()
    if not lb:
        return []
    user = get_user_email(request)
    items = lb.list_single_analyses(limit=15, user_email=user)
    return [
        {
            "id": str(item["id"]),
            "video_title": item.get("video_title", ""),
            "channel": item.get("channel", ""),
            "question": item.get("question"),
            "result_type": item.get("result_type", ""),
            "created_at": str(item["created_at"])[:16],
        }
        for item in items
    ]


@app.get("/api/history/videos/{analysis_id}")
async def get_video_analysis_detail(analysis_id: str, request: Request) -> dict:
    """Get full single video analysis result, scoped to the current user."""
    lb = get_lakebase()
    if not lb:
        return {"error": "Not found"}
    user = get_user_email(request)
    record = lb.get_single_analysis(analysis_id, user_email=user)
    if not record:
        return {"error": "Not found"}
    result_data = record.get("result_json", {})
    if isinstance(result_data, str):
        result_data = json.loads(result_data)

    return {
        "id": str(record["id"]),
        "video_url": record["video_url"],
        "video_title": record.get("video_title", ""),
        "channel": record.get("channel", ""),
        "thumbnail": thumbnail_url(record["video_url"]),
        "markdown": format_video_detail_markdown(record["result_type"], result_data),
    }


@app.get("/api/history/themes")
async def get_theme_history(request: Request) -> list[dict]:
    """Get theme exploration history for the current user."""
    lb = get_lakebase()
    if not lb:
        return []
    user = get_user_email(request)
    items = lb.list_theme_explorations(limit=15, user_email=user)
    return [
        {
            "id": str(item["id"]),
            "theme": item.get("theme", ""),
            "video_count": item.get("video_count", 0),
            "created_at": str(item["created_at"])[:16],
        }
        for item in items
    ]


@app.get("/api/history/themes/{theme_id}")
async def get_theme_detail(theme_id: str, request: Request) -> dict:
    """Get full theme exploration result, scoped to the current user."""
    lb = get_lakebase()
    if not lb:
        return {"error": "Not found"}
    user = get_user_email(request)
    record = lb.get_theme_exploration(theme_id, user_email=user)
    if not record:
        return {"error": "Not found"}
    videos = record.get("videos", [])
    mosaic = [
        {
            "url": v.get("video_url", ""),
            "title": v.get("video_title", ""),
            "thumbnail": thumbnail_url(v.get("video_url", "")),
        }
        for v in videos
    ]
    return {
        "id": str(record["id"]),
        "theme": record.get("theme", ""),
        "synthesis": record.get("synthesis", ""),
        "video_count": record.get("video_count", 0),
        "mosaic": mosaic,
    }


# --- Static files (React SPA) ---
# Must be last so it doesn't override API routes
_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

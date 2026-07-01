"""Gemini client initialization and video analysis via Foundation Model API.

Adapted from notebooks/_resources/setup.py for use in the Gradio app context.
"""

import logging
import os
import re
import threading
import time

from databricks.sdk import WorkspaceClient
from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Foundation Model API provisions different Gemini models per workspace and
# region, so the model is resolved at runtime instead of hardcoded. Selection
# rule: prefer the Gemini 2.5 tier (it reliably serves video on FMAPI, whereas
# the newer 3.x endpoints currently 502 on video input), then the flash/lite
# tier, then the newest version. Everything else stays as a fallback. Set
# GEMINI_MODEL to pin an explicit endpoint.
GEMINI_MODEL_ENV = "GEMINI_MODEL"
# Video-capable tier on FMAPI today; revisit when 3.x serves video.
_PREFERRED_VERSION = (2, 5)

_endpoint_names: list[str] | None = None
_model_lock = threading.Lock()


def _model_version(name: str) -> tuple[int, int]:
    """Parse the (major, minor) Gemini version from an endpoint name.

    Args:
        name: Serving endpoint name, e.g. "databricks-gemini-3-5-flash".

    Returns:
        (major, minor) version tuple; (0, 0) if no version is present.
    """
    match = re.search(r"gemini-(\d+)(?:-(\d+))?", name)
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2) or 0))


def _rank_models(names: list[str]) -> list[str]:
    """Rank Gemini endpoints best-first: the 2.5 tier first (it serves video on
    FMAPI; 3.x endpoints currently 502 on video), then the flash/lite tier, then
    newest version. Image-generation endpoints are dropped (they cannot analyze
    video or text). Everything else stays in the list as a fallback."""
    return sorted(
        [n for n in names if "image" not in n],
        key=lambda n: (
            _model_version(n) == _PREFERRED_VERSION,
            "flash" in n or "lite" in n,
            _model_version(n),
        ),
        reverse=True,
    )


def _list_gemini_endpoints() -> list[str]:
    """List the workspace's Gemini serving-endpoint names, cached per process."""
    global _endpoint_names
    with _model_lock:
        if _endpoint_names is None:
            try:
                w = WorkspaceClient()
                _endpoint_names = [
                    e.name
                    for e in w.serving_endpoints.list()
                    if e.name and "gemini" in e.name.lower()
                ]
            except Exception as exc:
                logger.error("Failed to list serving endpoints: %s", exc)
                _endpoint_names = []
        return _endpoint_names


def gemini_candidates() -> list[str]:
    """Ordered Gemini models to try, best-first (see _rank_models). Honors the
    GEMINI_MODEL override. analyze_video walks the list, falling back when a
    preferred model cannot serve a request."""
    override = os.environ.get(GEMINI_MODEL_ENV)
    if override:
        return [override]
    return _rank_models(_list_gemini_endpoints())


def resolve_gemini_model() -> str:
    """Resolve a single Gemini model (the top candidate). Honors GEMINI_MODEL.

    Raises:
        RuntimeError: If no Gemini endpoint is available.
    """
    candidates = gemini_candidates()
    if not candidates:
        raise RuntimeError(
            "No Gemini serving endpoint available. Set the "
            f"{GEMINI_MODEL_ENV} env var to specify one."
        )
    return candidates[0]


def _is_retryable(err: str) -> bool:
    """Rate-limit style errors worth retrying on the same model."""
    return any(s in err for s in ("429", "RESOURCE_EXHAUSTED"))


def create_gemini_client() -> genai.Client:
    """Create a Gemini client authenticated via Databricks workspace OAuth.

    Returns:
        Authenticated genai.Client routed through Foundation Model API.
    """
    w = WorkspaceClient()
    return genai.Client(
        api_key="databricks",
        http_options=types.HttpOptions(
            base_url=f"{w.config.host}/serving-endpoints/gemini",
            headers=w.config.authenticate(),
        ),
    )


def analyze_video(
    client: genai.Client,
    url: str,
    prompt: str,
    schema: type[BaseModel] | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
    fps: float = 0.1,
    end_offset: str | None = None,
    max_retries: int = 2,
) -> BaseModel | str | None:
    """Send a YouTube video URL to Gemini and return analysis.

    Args:
        client: Authenticated Gemini client.
        url: YouTube video URL.
        prompt: Analysis instructions for the model.
        schema: Pydantic model for structured JSON output. If None, returns
            raw text (freeform mode for Q&A).
        model: Gemini model name. If None, resolved at runtime via
            resolve_gemini_model().
        max_tokens: Maximum output tokens.
        fps: Frame rate for video sampling (lower = fewer tokens).
        end_offset: Optional end offset (e.g. "540s") to clip video.
        max_retries: Number of retry attempts on transient errors.

    Note:
        Uses media_resolution=LOW (66 tokens/frame vs 258 default) and
        video/* MIME type for format auto-detection.

    Returns:
        Parsed Pydantic model (if schema provided), raw text string (if
        schema is None), or None on failure.
    """
    models = [model] if model else gemini_candidates()
    if not models:
        raise RuntimeError(
            f"No Gemini serving endpoint available. Set the {GEMINI_MODEL_ENV} env var."
        )
    video_meta = types.VideoMetadata(fps=fps, end_offset=end_offset)
    config = types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        response_mime_type="application/json" if schema else None,
        response_schema=schema,
    )
    contents = types.Content(
        role="user",
        parts=[
            types.Part(
                file_data=types.FileData(file_uri=url, mime_type="video/*"),
                video_metadata=video_meta,
            ),
            types.Part(text=prompt),
        ],
    )

    # Try each candidate model; a 502/INTERNAL_ERROR means that model cannot
    # serve the request (e.g. v3 flash endpoints 502 on video), so fall back to
    # the next one rather than failing the whole analysis.
    for candidate in models:
        for attempt in range(max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=candidate, contents=contents, config=config
                )
                text = response.text or ""
                if not text.strip():
                    finish = _extract_finish_reason(response)
                    if attempt < max_retries:
                        logger.warning(
                            "Empty response (finish=%s) from %s for %s, retrying",
                            finish,
                            candidate,
                            url,
                        )
                        time.sleep(3 * (attempt + 1))
                        continue
                    logger.warning(
                        "Empty response (finish=%s) from %s for %s; trying next model",
                        finish,
                        candidate,
                        url,
                    )
                    break
                if schema is not None:
                    return schema.model_validate_json(text)
                return text
            except Exception as e:
                err = str(e)
                if _is_retryable(err) and attempt < max_retries:
                    logger.warning(
                        "Retry %d/%d on %s for %s: %s",
                        attempt + 1,
                        max_retries,
                        candidate,
                        url,
                        err[:120],
                    )
                    time.sleep(3 * (attempt + 1))
                    continue
                # This model failed (e.g. 502 on video, or a 400 from a model
                # that cannot take video); fall back to the next candidate.
                logger.warning(
                    "%s failed for %s (%s); trying next model",
                    candidate,
                    url,
                    err[:120],
                )
                break
    logger.error("All candidate models failed for %s", url)
    return None


def _extract_finish_reason(response: object) -> str:
    """Extract the finish reason from a Gemini response for diagnostics.

    Args:
        response: Gemini GenerateContentResponse.

    Returns:
        Finish reason string, or "unknown" if not available.
    """
    try:
        candidates = getattr(response, "candidates", None)
        if candidates and len(candidates) > 0:
            return str(getattr(candidates[0], "finish_reason", "unknown"))
    except Exception:
        pass
    return "unknown"


def synthesize_text(
    client: genai.Client,
    prompt: str,
    model: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """Text-only Gemini call (no video). Used for cross-video synthesis and Q&A.

    Args:
        client: Authenticated Gemini client.
        prompt: The text prompt.
        model: Gemini model name. If None, resolved via resolve_gemini_model().
        max_tokens: Maximum output tokens.

    Returns:
        Model response text, or empty string on failure.
    """
    if model is None:
        model = resolve_gemini_model()
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(max_output_tokens=max_tokens),
        )
        return response.text or ""
    except Exception as e:
        logger.error("Text synthesis failed: %s", e)
        return ""

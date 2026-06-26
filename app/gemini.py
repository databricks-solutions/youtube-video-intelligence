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
# rule: among Gemini endpoints of version 3 or newer, prefer the cheaper
# flash/lite tier, then the highest version; fall back to the newest Gemini of
# any version if none are v3+. Set GEMINI_MODEL to pin an explicit endpoint.
GEMINI_MODEL_ENV = "GEMINI_MODEL"
_MIN_MAJOR_VERSION = 3

_resolved_model: str | None = None
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


def _select_model(names: list[str]) -> str | None:
    """Choose the best Gemini model from available endpoint names.

    Prefers v3+ models, the flash/lite tier (cheaper), then highest version.
    Falls back to the newest available Gemini of any version if none are v3+.

    Args:
        names: Available Gemini serving-endpoint names.

    Returns:
        The chosen endpoint name, or None if no Gemini endpoint is available.
    """
    if not names:
        return None
    modern = [n for n in names if _model_version(n)[0] >= _MIN_MAJOR_VERSION]
    if modern:
        return max(
            modern,
            key=lambda n: (("flash" in n or "lite" in n), _model_version(n)),
        )
    newest = max(names, key=_model_version)
    logger.warning(
        "No Gemini v%d+ endpoint found; falling back to %s",
        _MIN_MAJOR_VERSION,
        newest,
    )
    return newest


def resolve_gemini_model() -> str:
    """Resolve the Gemini endpoint to use, cached for the process.

    Honors the GEMINI_MODEL env override; otherwise lists the workspace's
    Gemini serving endpoints and applies the selection rule.

    Returns:
        A Gemini serving-endpoint name.

    Raises:
        RuntimeError: If no model can be resolved (no override, and no Gemini
            endpoints could be listed).
    """
    global _resolved_model
    override = os.environ.get(GEMINI_MODEL_ENV)
    if override:
        return override
    with _model_lock:
        if _resolved_model is None:
            try:
                w = WorkspaceClient()
                names = [
                    e.name
                    for e in w.serving_endpoints.list()
                    if e.name and "gemini" in e.name.lower()
                ]
            except Exception as exc:
                logger.error("Failed to list serving endpoints: %s", exc)
                names = []
            chosen = _select_model(names)
            if not chosen:
                raise RuntimeError(
                    "No Gemini serving endpoint available. Set the "
                    f"{GEMINI_MODEL_ENV} env var to specify one."
                )
            logger.info("Resolved Gemini model: %s", chosen)
            _resolved_model = chosen
        return _resolved_model


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


_MAX_OUTPUT_TOKENS = 4096
_VIDEO_FPS = 0.1
_MAX_RETRIES = 2


def analyze_video(
    client: genai.Client,
    url: str,
    prompt: str,
    schema: type[BaseModel] | None = None,
    model: str | None = None,
    end_offset: str | None = None,
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
        end_offset: Optional end offset (e.g. "540s") to clip video.

    Note:
        Uses media_resolution=LOW (66 tokens/frame vs 258 default) and
        video/* MIME type for format auto-detection.

    Returns:
        Parsed Pydantic model (if schema provided), raw text string (if
        schema is None), or None on failure.
    """
    if model is None:
        model = resolve_gemini_model()
    config = types.GenerateContentConfig(
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        response_mime_type="application/json" if schema else None,
        response_schema=schema,
    )
    contents = types.Content(
        role="user",
        parts=[
            types.Part(
                file_data=types.FileData(file_uri=url, mime_type="video/*"),
                video_metadata=types.VideoMetadata(
                    fps=_VIDEO_FPS, end_offset=end_offset
                ),
            ),
            types.Part(text=prompt),
        ],
    )

    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model, contents=contents, config=config
            )
            text = response.text or ""
            if not text.strip():
                finish = _extract_finish_reason(response)
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "Empty response (attempt %d/%d, finish=%s) for %s, retrying",
                        attempt + 1,
                        _MAX_RETRIES + 1,
                        finish,
                        url,
                    )
                    time.sleep(3 * (attempt + 1))
                    continue
                logger.warning(
                    "Empty response from Gemini (finish=%s) for %s after %d attempts",
                    finish,
                    url,
                    attempt + 1,
                )
                return None
            if schema is not None:
                return schema.model_validate_json(text)
            return text
        except Exception as e:
            err_str = str(e)
            if "502" in err_str:
                logger.warning("Skipping (likely unavailable video): %s", url)
                return None
            is_retryable = any(
                s in err_str
                for s in (
                    "429",
                    "RESOURCE_EXHAUSTED",
                    "UNAVAILABLE",
                    "INTERNAL_ERROR",
                )
            )
            if is_retryable and attempt < _MAX_RETRIES:
                wait = 3 * (attempt + 1)
                logger.warning(
                    "Retry %d/%d for %s: %s", attempt + 1, _MAX_RETRIES, url, e
                )
                time.sleep(wait)
                continue
            logger.error("Failed after %d attempts: %s: %s", attempt + 1, url, e)
            return None
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
) -> str:
    """Text-only Gemini call (no video). Used for cross-video synthesis and Q&A.

    Args:
        client: Authenticated Gemini client.
        prompt: The text prompt.
        model: Gemini model name. If None, resolved via resolve_gemini_model().

    Returns:
        Model response text, or empty string on failure.
    """
    if model is None:
        model = resolve_gemini_model()
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(max_output_tokens=_MAX_OUTPUT_TOKENS),
        )
        return response.text or ""
    except Exception as e:
        logger.error("Text synthesis failed: %s", e)
        return ""

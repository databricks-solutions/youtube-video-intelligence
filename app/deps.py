"""Dependency management for FastAPI app.

Provides injectable access to the Gemini client and Lakebase module,
replacing module-level globals. Test code can override these via
`override_gemini_factory` and `override_lakebase`.
"""

import logging
import threading
import time
from collections.abc import Callable
from types import ModuleType

from gemini import create_gemini_client
from google import genai

logger = logging.getLogger(__name__)

# --- Gemini client with TTL-based refresh ---

_client: genai.Client | None = None
_client_ts: float = 0.0
_client_lock = threading.Lock()
CLIENT_TTL = 1800  # 30 minutes

_gemini_factory: Callable[[], genai.Client] = create_gemini_client
gemini_semaphore = threading.Semaphore(10)


def override_gemini_factory(factory: Callable[[], genai.Client]) -> None:
    """Replace the Gemini client factory (for testing).

    Args:
        factory: Callable that returns a genai.Client instance.
    """
    global _gemini_factory, _client
    _gemini_factory = factory
    _client = None


def get_gemini_client() -> genai.Client:
    """Get a Gemini client, creating or refreshing as needed.

    Returns:
        Authenticated genai.Client with automatic TTL-based refresh.
    """
    global _client, _client_ts
    with _client_lock:
        if _client is None or (time.time() - _client_ts) > CLIENT_TTL:
            _client = _gemini_factory()
            _client_ts = time.time()
        return _client


def reset_gemini_client() -> None:
    """Force client refresh on next call. Useful for testing."""
    global _client, _client_ts
    with _client_lock:
        _client = None
        _client_ts = 0.0


# --- Lakebase access ---

_lakebase: ModuleType | None = None


def _load_lakebase() -> ModuleType | None:
    """Attempt to import and initialize the lakebase module."""
    try:
        import lakebase

        if lakebase.ensure_schema():
            return lakebase
        return None
    except Exception as e:
        logger.warning("Lakebase not available: %s", e)
        return None


def override_lakebase(module: ModuleType | None) -> None:
    """Replace the lakebase module (for testing).

    Args:
        module: A mock or real lakebase module, or None to disable.
    """
    global _lakebase
    _lakebase = module


def get_lakebase() -> ModuleType | None:
    """Get the lakebase module, initializing on first call.

    Returns:
        The lakebase module, or None if unavailable.
    """
    global _lakebase
    if _lakebase is None:
        _lakebase = _load_lakebase()
    return _lakebase

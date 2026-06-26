"""Tests for the Lakebase module.

Ensures the real lakebase module is imported even when other test files
have registered a MagicMock in sys.modules.
"""

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


@pytest.fixture(autouse=True)
def _real_lakebase():
    """Ensure the real lakebase module is loaded, not a mock."""
    # Remove mock if present, force-import the real module
    if "lakebase" in sys.modules and not hasattr(sys.modules["lakebase"], "__file__"):
        del sys.modules["lakebase"]
    import lakebase

    if not hasattr(lakebase, "__file__") or lakebase.__file__ is None:
        del sys.modules["lakebase"]
        importlib.invalidate_caches()
        import lakebase
    lakebase._endpoint_cache = None
    yield lakebase
    # Reset engine state after each test
    lakebase._engine = None
    lakebase._engine_ts = 0.0
    lakebase._endpoint_cache = None


# --- Availability tests ---


def test_is_available_without_pghost(_real_lakebase) -> None:
    """is_available returns False when PGHOST is not set."""
    with patch.dict(os.environ, {}, clear=True):
        assert not _real_lakebase.is_available()


def test_is_available_with_pghost(_real_lakebase) -> None:
    """is_available returns True when PGHOST is set."""
    with patch.dict(os.environ, {"PGHOST": "localhost"}):
        assert _real_lakebase.is_available()


def test_graceful_degradation_no_pghost(_real_lakebase) -> None:
    """All CRUD functions return empty/None when PGHOST is unset."""
    lb = _real_lakebase
    with patch.dict(os.environ, {}, clear=True):
        assert lb._get_engine() is None
        assert lb.save_single_analysis("u", "t", "c", None, "summary", {}) is None
        assert lb.list_single_analyses() == []
        assert lb.get_single_analysis("abc") is None
        assert lb.save_theme_exploration("t", 0, "", []) is None
        assert lb.list_theme_explorations() == []
        assert lb.get_theme_exploration("abc") is None


# --- Schema definition tests ---


def test_tables_have_expected_columns(_real_lakebase) -> None:
    """Verify the SQLAlchemy table definitions have the right columns."""
    lb = _real_lakebase

    sv_cols = {c.name for c in lb.single_video_analyses.columns}
    assert "id" in sv_cols
    assert "video_url" in sv_cols
    assert "result_json" in sv_cols
    assert "created_at" in sv_cols

    te_cols = {c.name for c in lb.theme_explorations.columns}
    assert "theme" in te_cols
    assert "synthesis" in te_cols
    assert "video_count" in te_cols

    tva_cols = {c.name for c in lb.theme_video_analyses.columns}
    assert "theme_id" in tva_cols
    assert "analysis_json" in tva_cols


# --- Endpoint resolution / credential tests ---


def _fake_endpoint(host: str, name: str) -> SimpleNamespace:
    """Build a fake Endpoint exposing status.hosts.host."""
    return SimpleNamespace(
        name=name, status=SimpleNamespace(hosts=SimpleNamespace(host=host))
    )


def _fake_postgres(host: str, endpoint_name: str) -> MagicMock:
    """Build a fake postgres API with one project/branch/endpoint."""
    pg = MagicMock()
    pg.list_projects.return_value = [SimpleNamespace(name="projects/p")]
    pg.list_branches.return_value = [
        SimpleNamespace(name="projects/p/branches/production")
    ]
    pg.list_endpoints.return_value = [_fake_endpoint(host, endpoint_name)]
    return pg


def test_resolve_endpoint_matches_host(_real_lakebase) -> None:
    """_resolve_endpoint returns the endpoint whose host matches PGHOST."""
    lb = _real_lakebase
    w = SimpleNamespace(postgres=_fake_postgres("h1.example.com", "ep/primary"))
    assert lb._resolve_endpoint(w, "h1.example.com") == "ep/primary"


def test_resolve_endpoint_no_match_returns_none(_real_lakebase) -> None:
    """_resolve_endpoint returns None when no host matches."""
    lb = _real_lakebase
    w = SimpleNamespace(postgres=_fake_postgres("h1.example.com", "ep/primary"))
    assert lb._resolve_endpoint(w, "other.example.com") is None


def test_resolve_endpoint_swallows_list_errors(_real_lakebase) -> None:
    """_resolve_endpoint returns None (not raise) when listing is denied."""
    lb = _real_lakebase
    pg = MagicMock()
    pg.list_projects.side_effect = PermissionError("denied")
    w = SimpleNamespace(postgres=pg)
    assert lb._resolve_endpoint(w, "h1") is None


def test_generate_credentials_prefers_override_endpoint(_real_lakebase) -> None:
    """LAKEBASE_ENDPOINT override is used directly, skipping resolution."""
    lb = _real_lakebase
    fake_w = MagicMock()
    fake_w.postgres.generate_database_credential.return_value = SimpleNamespace(
        token="tok"
    )
    env = {"PGUSER": "sp", "PGHOST": "h", "LAKEBASE_ENDPOINT": "ep-x"}
    with (
        patch("databricks.sdk.WorkspaceClient", return_value=fake_w),
        patch.dict(os.environ, env, clear=True),
    ):
        user, pwd = lb._generate_credentials()
    assert (user, pwd) == ("sp", "tok")
    fake_w.postgres.generate_database_credential.assert_called_once_with(
        endpoint="ep-x"
    )
    fake_w.postgres.list_projects.assert_not_called()


def test_generate_credentials_resolves_from_pghost(_real_lakebase) -> None:
    """Without an override, the endpoint is resolved from PGHOST."""
    lb = _real_lakebase
    fake_w = MagicMock()
    fake_w.postgres = _fake_postgres("h1", "ep-resolved")
    fake_w.postgres.generate_database_credential.return_value = SimpleNamespace(
        token="tok2"
    )
    with (
        patch("databricks.sdk.WorkspaceClient", return_value=fake_w),
        patch.dict(os.environ, {"PGUSER": "sp", "PGHOST": "h1"}, clear=True),
    ):
        user, pwd = lb._generate_credentials()
    assert (user, pwd) == ("sp", "tok2")
    fake_w.postgres.generate_database_credential.assert_called_once_with(
        endpoint="ep-resolved"
    )


def test_generate_credentials_falls_back_to_pgpassword(_real_lakebase) -> None:
    """When no endpoint resolves, the injected PGPASSWORD is used."""
    lb = _real_lakebase
    fake_w = MagicMock()
    fake_w.postgres.list_projects.return_value = []
    env = {"PGUSER": "sp", "PGHOST": "h1", "PGPASSWORD": "pw"}
    with (
        patch("databricks.sdk.WorkspaceClient", return_value=fake_w),
        patch.dict(os.environ, env, clear=True),
    ):
        user, pwd = lb._generate_credentials()
    assert (user, pwd) == ("sp", "pw")
    fake_w.postgres.generate_database_credential.assert_not_called()


def test_generate_credentials_token_failure_falls_back(_real_lakebase) -> None:
    """A token-generation error falls back to PGPASSWORD."""
    lb = _real_lakebase
    fake_w = MagicMock()
    fake_w.postgres.generate_database_credential.side_effect = RuntimeError("no perms")
    env = {
        "PGUSER": "sp",
        "PGHOST": "h",
        "LAKEBASE_ENDPOINT": "ep-x",
        "PGPASSWORD": "pw",
    }
    with (
        patch("databricks.sdk.WorkspaceClient", return_value=fake_w),
        patch.dict(os.environ, env, clear=True),
    ):
        user, pwd = lb._generate_credentials()
    assert (user, pwd) == ("sp", "pw")


def test_generate_credentials_raises_when_nothing_available(_real_lakebase) -> None:
    """RuntimeError is raised when no endpoint resolves and no PGPASSWORD set."""
    lb = _real_lakebase
    fake_w = MagicMock()
    fake_w.postgres.list_projects.return_value = []
    with (
        patch("databricks.sdk.WorkspaceClient", return_value=fake_w),
        patch.dict(os.environ, {"PGUSER": "sp", "PGHOST": "h1"}, clear=True),
        pytest.raises(RuntimeError),
    ):
        lb._generate_credentials()

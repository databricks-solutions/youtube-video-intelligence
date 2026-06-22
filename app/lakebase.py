"""Lakebase (PostgreSQL) persistence for analysis history.

Uses SQLAlchemy Core for clean SQL generation. Connection is portable across
workspaces with no hardcoded resource IDs: the app resource auto-injects the
PG* env vars (PGHOST/PGPORT/PGDATABASE/PGUSER and, when available, PGPASSWORD).
Credentials are obtained by generating a short-lived OAuth token, resolving the
Lakebase endpoint dynamically from PGHOST (see `_resolve_endpoint`); the token
is refreshed before it expires. `LAKEBASE_ENDPOINT` may be set to skip
resolution, and the injected `PGPASSWORD` is used as a last-resort fallback.
If PGHOST is unset (local dev), all functions gracefully return empty results.

Schema is set via LAKEBASE_SCHEMA (default "public", overridden to "app" in
app.yaml). A dedicated schema is required because PostgreSQL 15+ revokes CREATE
on `public` from non-owners; the app's service principal creates and owns the
configured schema on first run via `ensure_schema()`.
"""

import json
import logging
import os
import threading
import time

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

logger = logging.getLogger(__name__)

_engine = None
_engine_lock = threading.Lock()
_engine_ts = 0.0
_ENGINE_TTL = 2700  # 45 minutes (tokens expire at 60 min)
_endpoint_cache: str | None = None

SCHEMA = os.environ.get("LAKEBASE_SCHEMA", "public")
metadata = MetaData(schema=SCHEMA)

single_video_analyses = Table(
    "single_video_analyses",
    metadata,
    Column("id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_email", Text, nullable=False, server_default=""),
    Column("video_url", Text, nullable=False),
    Column("video_title", Text, nullable=False, server_default=""),
    Column("channel", Text, nullable=False, server_default=""),
    Column("question", Text),
    Column("result_type", String(20), nullable=False),
    Column("result_json", JSONB, nullable=False),
    Column("is_bookmarked", Boolean, nullable=False, server_default="false"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)

theme_explorations = Table(
    "theme_explorations",
    metadata,
    Column("id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_email", Text, nullable=False, server_default=""),
    Column("theme", Text, nullable=False),
    Column("video_count", Integer, nullable=False, server_default="0"),
    Column("synthesis", Text),
    Column("is_bookmarked", Boolean, nullable=False, server_default="false"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)

theme_video_analyses = Table(
    "theme_video_analyses",
    metadata,
    Column("id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column(
        "theme_id",
        UUID,
        ForeignKey(f"{SCHEMA}.theme_explorations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("video_url", Text, nullable=False),
    Column("video_title", Text, nullable=False, server_default=""),
    Column("channel", Text, nullable=False, server_default=""),
    Column("analysis_json", JSONB, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)


def is_available() -> bool:
    """Check if Lakebase env vars are configured."""
    return bool(os.environ.get("PGHOST"))


def _resolve_endpoint(w: "object", pghost: str) -> str | None:
    """Resolve the Lakebase endpoint resource name from the injected PGHOST.

    Makes the app portable: rather than hardcoding the project/branch/endpoint
    (which embeds the deploy prefix and target), it finds the endpoint whose
    host matches PGHOST. Cached after the first success since endpoints are
    stable. Returns None if no match is found or the lookup is not permitted.

    Args:
        w: An authenticated `databricks.sdk.WorkspaceClient`.
        pghost: The injected PGHOST value to match against endpoint hosts.

    Returns:
        The endpoint resource name, or None if it cannot be resolved.
    """
    global _endpoint_cache
    if _endpoint_cache:
        return _endpoint_cache
    if not pghost:
        return None
    try:
        for project in w.postgres.list_projects():
            for branch in w.postgres.list_branches(parent=project.name):
                for endpoint in w.postgres.list_endpoints(parent=branch.name):
                    hosts = getattr(endpoint.status, "hosts", None)
                    if hosts is not None and hosts.host == pghost:
                        _endpoint_cache = endpoint.name
                        return endpoint.name
    except Exception as e:
        logger.warning("Could not resolve Lakebase endpoint from PGHOST: %s", e)
    return None


def _generate_credentials() -> tuple[str, str]:
    """Return (username, password) for connecting to Lakebase.

    Portable across workspaces, prefixes, and deploy targets: the endpoint is
    resolved from `LAKEBASE_ENDPOINT` if explicitly set, otherwise from the
    platform-injected `PGHOST` (see `_resolve_endpoint`). A freshly generated
    OAuth token is preferred so it can be refreshed for a long-running app; if
    token generation is unavailable (e.g. the service principal cannot list
    Lakebase projects), the injected `PGPASSWORD` is used as a fallback.

    Raises:
        RuntimeError: If no credential can be obtained by any method.
    """
    from databricks.sdk import WorkspaceClient

    username = os.environ.get("PGUSER", "")
    w = WorkspaceClient()
    endpoint = os.environ.get("LAKEBASE_ENDPOINT") or _resolve_endpoint(
        w, os.environ.get("PGHOST", "")
    )
    if endpoint:
        try:
            cred = w.postgres.generate_database_credential(endpoint=endpoint)
            logger.info("Lakebase credential: OAuth token (endpoint=%s)", endpoint)
            return (username or w.current_user.me().user_name), cred.token
        except Exception as e:
            logger.warning(
                "Lakebase token generation failed (endpoint=%s): %s; "
                "falling back to injected PGPASSWORD.",
                endpoint,
                e,
            )
    pgpassword = os.environ.get("PGPASSWORD")
    if username and pgpassword:
        logger.info("Lakebase credential: injected PGPASSWORD")
        return username, pgpassword
    raise RuntimeError(
        "Cannot obtain Lakebase credentials: no endpoint resolved (set "
        "LAKEBASE_ENDPOINT, or grant the app's service principal access to "
        "list Lakebase projects) and no PGPASSWORD was injected."
    )


def _get_engine() -> sa.engine.Engine | None:
    """Get or create a SQLAlchemy engine with OAuth token refresh."""
    if not is_available():
        return None

    global _engine, _engine_ts
    with _engine_lock:
        now = time.time()
        if _engine is not None and (now - _engine_ts) > _ENGINE_TTL:
            _engine.dispose()
            _engine = None

        if _engine is None:
            try:
                username, token = _generate_credentials()
                host = os.environ["PGHOST"]
                database = os.environ.get("PGDATABASE", "databricks_postgres")
                port = os.environ.get("PGPORT", "5432")
                url = sa.URL.create(
                    "postgresql+psycopg2",
                    username=username,
                    password=token,
                    host=host,
                    port=int(port),
                    database=database,
                    query={"sslmode": "require"},
                )
                _engine = sa.create_engine(url, pool_size=3, pool_recycle=1800)
                _engine_ts = now
            except Exception as e:
                logger.error("Failed to create Lakebase engine: %s", e)
                return None
        return _engine


def ensure_schema() -> bool:
    """Create the schema (if non-public) and all tables. Idempotent.

    Returns:
        True if Lakebase is ready, False otherwise.
    """
    engine = _get_engine()
    if engine is None:
        logger.warning("Lakebase not available (PGHOST unset), history disabled")
        return False
    try:
        if SCHEMA != "public":
            with engine.connect() as conn:
                conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
                conn.commit()
        metadata.create_all(engine)
        # Verify: run a simple query to confirm read access
        with engine.connect() as conn:
            conn.execute(text(f"SELECT 1 FROM {SCHEMA}.single_video_analyses LIMIT 0"))
        logger.info(
            "Lakebase ready (schema=%s, host=%s)",
            SCHEMA,
            os.environ.get("PGHOST", "?"),
        )
        return True
    except Exception as e:
        logger.error(
            "Lakebase setup failed (schema=%s): %s. "
            "History will be disabled. If using a custom schema, ensure the "
            "app's service principal has USAGE and CREATE privileges on it. "
            "Set LAKEBASE_SCHEMA=public to use the default schema instead.",
            SCHEMA,
            e,
        )
        return False


# --- Single Video CRUD ---


def save_single_analysis(
    video_url: str,
    video_title: str,
    channel: str,
    question: str | None,
    result_type: str,
    result_json: dict,
    user_email: str = "",
) -> str | None:
    """Save a single video analysis. Returns UUID string or None."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            result = conn.execute(
                single_video_analyses.insert()
                .values(
                    user_email=user_email,
                    video_url=video_url,
                    video_title=video_title,
                    channel=channel,
                    question=question,
                    result_type=result_type,
                    result_json=json.dumps(result_json),
                )
                .returning(single_video_analyses.c.id)
            )
            row_id = str(result.scalar())
            conn.commit()
            return row_id
    except Exception as e:
        logger.error("Failed to save single analysis: %s", e)
        return None


def list_single_analyses(limit: int = 20, user_email: str = "") -> list[dict]:
    """List recent single video analyses for a user, newest first."""
    engine = _get_engine()
    if engine is None:
        return []
    try:
        with engine.connect() as conn:
            stmt = (
                sa.select(
                    single_video_analyses.c.id,
                    single_video_analyses.c.video_url,
                    single_video_analyses.c.video_title,
                    single_video_analyses.c.channel,
                    single_video_analyses.c.question,
                    single_video_analyses.c.result_type,
                    single_video_analyses.c.is_bookmarked,
                    single_video_analyses.c.created_at,
                )
                .where(single_video_analyses.c.user_email == user_email)
                .order_by(single_video_analyses.c.created_at.desc())
                .limit(limit)
            )
            rows = conn.execute(stmt).mappings().all()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error("Failed to list single analyses: %s", e)
        return []


def get_single_analysis(analysis_id: str, user_email: str = "") -> dict | None:
    """Get a single video analysis by ID, scoped to the given user."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            stmt = single_video_analyses.select().where(
                single_video_analyses.c.id == analysis_id,
                single_video_analyses.c.user_email == user_email,
            )
            row = conn.execute(stmt).mappings().first()
            return dict(row) if row else None
    except Exception as e:
        logger.error("Failed to get single analysis %s: %s", analysis_id, e)
        return None


# --- Theme CRUD ---


def save_theme_exploration(
    theme: str,
    video_count: int,
    synthesis: str,
    video_analyses: list[dict],
    user_email: str = "",
) -> str | None:
    """Save a theme exploration with child video analyses. Returns UUID or None."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.begin() as conn:
            result = conn.execute(
                theme_explorations.insert()
                .values(
                    user_email=user_email,
                    theme=theme,
                    video_count=video_count,
                    synthesis=synthesis,
                )
                .returning(theme_explorations.c.id)
            )
            theme_id = str(result.scalar())

            for va in video_analyses:
                analysis = va.get("analysis")
                analysis_dict = (
                    analysis.model_dump()
                    if hasattr(analysis, "model_dump")
                    else analysis
                )
                conn.execute(
                    theme_video_analyses.insert().values(
                        theme_id=theme_id,
                        video_url=va.get("url", ""),
                        video_title=va.get("title", ""),
                        channel=va.get("channel", ""),
                        analysis_json=json.dumps(analysis_dict),
                    )
                )
            return theme_id
    except Exception as e:
        logger.error("Failed to save theme exploration: %s", e)
        return None


def list_theme_explorations(limit: int = 20, user_email: str = "") -> list[dict]:
    """List recent theme explorations for a user, newest first."""
    engine = _get_engine()
    if engine is None:
        return []
    try:
        with engine.connect() as conn:
            stmt = (
                sa.select(
                    theme_explorations.c.id,
                    theme_explorations.c.theme,
                    theme_explorations.c.video_count,
                    theme_explorations.c.is_bookmarked,
                    theme_explorations.c.created_at,
                )
                .where(theme_explorations.c.user_email == user_email)
                .order_by(theme_explorations.c.created_at.desc())
                .limit(limit)
            )
            rows = conn.execute(stmt).mappings().all()
            return [dict(row) for row in rows]
    except Exception as e:
        logger.error("Failed to list theme explorations: %s", e)
        return []


def get_theme_exploration(theme_id: str, user_email: str = "") -> dict | None:
    """Get a theme exploration with child video analyses, scoped to the given user."""
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.connect() as conn:
            stmt = theme_explorations.select().where(
                theme_explorations.c.id == theme_id,
                theme_explorations.c.user_email == user_email,
            )
            row = conn.execute(stmt).mappings().first()
            if not row:
                return None
            result = dict(row)

            child_stmt = (
                sa.select(
                    theme_video_analyses.c.video_url,
                    theme_video_analyses.c.video_title,
                    theme_video_analyses.c.channel,
                    theme_video_analyses.c.analysis_json,
                )
                .where(theme_video_analyses.c.theme_id == theme_id)
                .order_by(theme_video_analyses.c.created_at)
            )
            children = conn.execute(child_stmt).mappings().all()
            result["videos"] = [dict(c) for c in children]
            return result
    except Exception as e:
        logger.error("Failed to get theme exploration %s: %s", theme_id, e)
        return None


# --- Shared ---

_VALID_TABLES = {
    "single_video_analyses": single_video_analyses,
    "theme_explorations": theme_explorations,
}


def toggle_bookmark(table_name: str, record_id: str) -> bool | None:
    """Toggle is_bookmarked on a record. Returns new state or None."""
    table = _VALID_TABLES.get(table_name)
    if table is None:
        return None
    engine = _get_engine()
    if engine is None:
        return None
    try:
        with engine.begin() as conn:
            stmt = (
                table.update()
                .where(table.c.id == record_id)
                .values(is_bookmarked=~table.c.is_bookmarked)
                .returning(table.c.is_bookmarked)
            )
            row = conn.execute(stmt).scalar()
            return row
    except Exception as e:
        logger.error("Failed to toggle bookmark: %s", e)
        return None


def search_analyses(table_name: str, query: str, limit: int = 10) -> list[dict]:
    """Full-text search across analyses using Postgres tsvector."""
    table = _VALID_TABLES.get(table_name)
    if table is None:
        return []
    engine = _get_engine()
    if engine is None:
        return []

    if table_name == "single_video_analyses":
        ts_col = sa.func.to_tsvector(
            "english",
            sa.func.coalesce(table.c.video_title, "")
            + " "
            + sa.cast(sa.func.coalesce(table.c.result_json, "{}"), Text),
        )
        columns = [
            table.c.id,
            table.c.video_url,
            table.c.video_title,
            table.c.channel,
            table.c.question,
            table.c.result_type,
            table.c.is_bookmarked,
            table.c.created_at,
        ]
    else:
        ts_col = sa.func.to_tsvector(
            "english",
            sa.func.coalesce(table.c.theme, "")
            + " "
            + sa.func.coalesce(table.c.synthesis, ""),
        )
        columns = [
            table.c.id,
            table.c.theme,
            table.c.video_count,
            table.c.is_bookmarked,
            table.c.created_at,
        ]

    try:
        with engine.connect() as conn:
            ts_query = sa.func.plainto_tsquery("english", query)
            stmt = (
                sa.select(*columns)
                .where(ts_col.op("@@")(ts_query))
                .order_by(table.c.created_at.desc())
                .limit(limit)
            )
            rows = conn.execute(stmt).mappings().all()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error("Failed to search %s: %s", table_name, e)
        return []

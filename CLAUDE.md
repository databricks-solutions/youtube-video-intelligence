# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

YouTube Video Intelligence with Gemini on Databricks: real-time analysis of YouTube videos via Gemini Foundation Model API. FastAPI backend + React SPA frontend. Lakebase-backed per-user history. Deployed as a Databricks App via DAB.

## Architecture

FastAPI serves API endpoints (SSE for real-time progress) and static files (React SPA). Both the app and Lakebase project are managed by DAB.

```
app/
  app.py              # FastAPI backend: API endpoints + static file serving
  static/index.html   # React 18 SPA via CDN (YouTube dark theme, Babel standalone for JSX)
  gemini.py           # Gemini client init + analyze_video() + synthesize_text()
  youtube.py          # yt-dlp YouTube search + filter + rank + metadata
  schemas.py          # Pydantic models: VideoSummary, ThemeAnalysis
  lakebase.py         # SQLAlchemy Core persistence for analysis history
  app.yaml            # Databricks App config (python app.py + Lakebase resource)
  requirements.txt    # GENERATED from pyproject.toml; do not hand-edit (see Conventions)
```

Notebooks in `notebooks/` are retained for reference (batch pipeline) but decoupled from the app.

## App Modes

**Single Video Analyzer**: user pastes a YouTube URL + optional question. SSE stream sends: metadata card (via oEmbed), loading status, then result (structured summary or freeform answer).

**Theme Explorer**: user types a theme. SSE stream sends: search progress, found videos (with views + duration), per-video progress (10-90%), synthesis (90-100%). Thumbnail mosaic of analyzed videos. Top 3 ranked videos analyzed in parallel, with a global semaphore for rate limit protection.

**History**: per-user (via `x-forwarded-email` header), stored in Lakebase. Loaded on page load, collapsible panel on right side, clickable entries load past results. Database is the source of truth (no in-page updates).

## API Endpoints

- `POST /api/analyze-video` - SSE stream for single video analysis
- `POST /api/analyze-theme` - SSE stream for theme exploration
- `GET /api/history/videos` - user's single video history
- `GET /api/history/videos/{id}` - full single video result
- `GET /api/history/themes` - user's theme history
- `GET /api/history/themes/{id}` - full theme result with mosaic

## Lakebase Setup

Lakebase Autoscale project managed via DAB (`postgres_projects` resource). App resource links to it via `postgres` block in `databricks.yml`. PG* env vars (PGHOST, PGDATABASE, PGUSER, PGPORT, and when available PGPASSWORD) are auto-injected by the app resource.

Auth is **portable, with no hardcoded resource IDs** (`app/lakebase.py`):
1. Resolve the endpoint name: use `LAKEBASE_ENDPOINT` if set (optional override), else resolve at runtime by matching the injected `PGHOST` against the workspace's Lakebase endpoints (`_resolve_endpoint`, cached).
2. Generate a short-lived OAuth token via `w.postgres.generate_database_credential(endpoint=...)`, refreshed on a 45-min TTL.
3. If token generation is unavailable (e.g. the SP cannot list projects), fall back to the injected `PGPASSWORD`.

Tables (in `app` schema): `single_video_analyses`, `theme_explorations`, `theme_video_analyses`. The schema is created automatically by `lakebase.ensure_schema()` at startup and is **owned by the app's service principal**. A non-public schema is required because PostgreSQL 15+ revokes CREATE on `public` from non-owners. SQLAlchemy Core for all queries.

## Resource Naming

All resource names use `${var.prefix}` from `databricks.yml`:
- App: `{prefix}-video-intel-{target}`
- Lakebase project: `{prefix}-video-intel-{target}`
- Default prefix: `sc`. Override with `--var="prefix=xyz"` at deploy time.
- The Lakebase database name is the fixed default `databricks-postgres` (not an auto-generated ID), so the templated `database:` value in `databricks.yml` is deterministic and needs no manual edit after deploy.

## Commands

```bash
# Python
uv venv --python 3.11 && uv sync --all-extras
.venv/bin/ruff check app/ tests/
.venv/bin/ruff format app/ tests/
.venv/bin/pytest tests/ -v

# Deploy
databricks bundle validate -t dev
databricks bundle deploy -t dev
databricks bundle run video_intelligence_app -t dev
```

## Deploying to a New Workspace (Portability)

The aim is turnkey: clone, set your workspace/prefix, `bundle deploy` + `run`, working app. What makes that work, and the two prerequisites that bite:

- **No hardcoded resource IDs.** The Lakebase endpoint is resolved at runtime from `PGHOST`; the DB name is the fixed `databricks-postgres`; resource names derive from `${var.prefix}`/`${bundle.target}`. A fresh deploy in any workspace creates its own SP and Lakebase project, and `ensure_schema()` creates+owns the `app` schema on first start. No post-deploy manual edits.
- **Serverless egress (network policy) is the #1 prerequisite.** Apps run on serverless compute, which is deny-by-default for outbound internet. The build needs `pypi.org` and `files.pythonhosted.org`; runtime needs `www.youtube.com` (oEmbed metadata + yt-dlp Theme Explorer search). If the workspace's serverless network policy is "restricted", allowlist those FQDNs, or use "allow all destinations". Symptom when blocked: `[Errno -3] Temporary failure in name resolution` (build fails on pypi; single-video runtime degrades gracefully but Theme Explorer fails).
  - Gotcha: a network-policy change does NOT apply to already-running compute. **Stop and start the app** (not just redeploy) so compute re-provisions under the new policy.
  - Diagnose denials via `system.access.outbound_network` (filter by `access_type IN ('DROP','DRY_RUN_DENIAL')`).
- **App-recreate footgun.** Deleting and recreating the app mints a NEW service principal (a new Postgres role) that cannot use the `app` schema owned by the old SP, so history breaks with `permission denied for schema app`. Fix: connect to Lakebase as an identity with rights on the database and `DROP SCHEMA app CASCADE` (discards old history), then restart the app so the new SP recreates and owns it. Avoid recreating the app when history matters.

## Conventions

- Python deps: `pyproject.toml` is canonical. `app/requirements.txt` is **generated** via `uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o app/requirements.txt` because the Databricks Apps build container only reads `requirements.txt`. Regenerate after any dep change. Don't hand-edit `app/requirements.txt`.
- FastAPI backend, React SPA frontend (not Gradio)
- React loaded via CDN (unpkg), JSX via Babel standalone. No build step needed for production.
- Both tabs always rendered (hidden with `display:none`), so state persists across tab switches
- SSE (Server-Sent Events) via fetch + ReadableStream for real-time progress
- Background threads for sync Gemini/yt-dlp calls in async FastAPI context
- Queue-based communication between background threads and SSE generators
- Gemini client cached with TTL-based refresh (threading.Lock + 30-min expiry)
- Global semaphore (10 concurrent) limits Gemini calls across all tabs/users
- `analyze_video()` in `app/gemini.py` takes client as parameter (no module-level global)
- `schema=None` on `analyze_video()` returns raw text (freeform Q&A mode)
- Video token control: FPS=0.1, end_offset computed from yt-dlp duration minus 60s
- yt-dlp `extract_flat=True` for search (metadata only, no download). Does NOT return upload_date.
- `rank_videos()` scores by: relevancy 40%, views 35% (log-scaled), recency 25%
- Date parsing: use `datetime.strptime(yt_date, "%Y%m%d")` for yt-dlp dates, `date.fromisoformat()` for ISO dates. No manual string slicing.
- Lakebase connection via SQLAlchemy + psycopg2 with OAuth token refresh (45-min TTL)
- JSONB columns for analysis results (no schema migrations needed)
- User identity from `x-forwarded-email` header (Databricks App proxy)
- Synthesis has retry logic (3 attempts, 15s gaps) due to FMAPI rate limits after parallel analysis
- Notebooks still use `%run ./_resources/setup` and `%run ./_resources/youtube_urls` patterns

## Known Issues

- yt-dlp `extract_flat=True` does NOT return `upload_date`. Only views and duration are available from search results.
- oEmbed for single video metadata only returns title/channel (no views, duration, description) because full yt-dlp extraction gets blocked by YouTube bot detection on DBX container. Metadata fetch is non-fatal: if it fails (e.g. egress blocked), single-video analysis still proceeds with a fallback title because the Gemini FMAPI fetches the video server-side.
- FMAPI rate limits: parallel Gemini calls (top 3 per theme) can pressure the workspace token budget. Global semaphore + synthesis retry mitigates but doesn't eliminate.
- Serverless egress: a restricted network policy blocks pypi.org (build) and youtube.com (runtime). See "Deploying to a New Workspace". Network-policy changes require an app stop/start to take effect.

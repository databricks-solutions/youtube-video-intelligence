# YouTube Video Intelligence with Gemini on Databricks

Real-time YouTube video analysis app powered by Google Gemini on Databricks Foundation Model API, with Lakebase-backed history.

## What It Does

**Single Video Analyzer**: paste any YouTube URL, optionally ask a question. Gemini watches the video and returns a structured summary or a direct answer.

**Theme Explorer**: type any theme (e.g., "audience reaction to our latest show launch"). The app searches YouTube for videos, filters/ranks by views + relevancy + recency, analyzes the top 3 in parallel with Gemini, and produces a cross-video theme synthesis.

**History**: every analysis is persisted to Lakebase (managed PostgreSQL), per-user, clickable to reload past results.

## Architecture

```
React SPA (static/index.html)
    |
FastAPI (app.py) -- SSE for real-time progress
    |
    +-- Gemini FMAPI (video analysis)
    +-- yt-dlp (YouTube search)
    +-- Lakebase PostgreSQL (history)
```

Deployed as a Databricks App via DAB. Lakebase project also managed by DAB.

## Prerequisites

**Model access**
- Access to the Gemini pay-per-token Foundation Model API endpoints: `databricks-gemini-3-1-pro` (analysis) and `databricks-gemini-3-flash` (synthesis).
- **Cross-geo processing enabled for Foundation Model APIs** (sometimes shown as "cross-geo routing"). Gemini is a partner-hosted model, and its video understanding fetches the YouTube URL server-side, so requests must be allowed to process across geos. This is an account/workspace AI setting and may require a support ticket to enable.

**Workspace capabilities**
- **Lakebase Autoscale** enabled (for the history database).
- **Serverless compute** and **Databricks Apps** enabled (Apps run on serverless).

**Networking**
- **Serverless network egress** must let the app reach the public internet. Apps run on serverless compute, which is deny-by-default for egress when a network policy is set to "restricted". The build needs `pypi.org` + `files.pythonhosted.org`; runtime needs `www.youtube.com` + `youtube.com`. See [Network egress](#3-network-egress-if-restricted). If your workspace has no serverless network policy (or it allows all destinations), you are already set.

**Permissions (deploying identity)**
- Rights to create a Databricks App and a Lakebase project (typically workspace admin or equivalent).
- At least **connect/read on the Lakebase database**. Automatic on a fresh deploy (the DAB creates the project and you own it); needs an explicit grant when deploying against a project someone else created. Required to inspect or recover history directly.

**Tooling**
- Databricks CLI authenticated to the target workspace (`databricks auth login`).

## Deployment

### 1. Choose your prefix

All resource names use a configurable prefix to avoid collisions. Edit `databricks.yml` or pass it at deploy time:

```bash
# Option A: edit the default in databricks.yml
variables:
  prefix:
    default: "your-initials"  # e.g., "cm", "jd", "team-alpha"

# Option B: pass at deploy time
databricks bundle deploy -t dev --var="prefix=your-initials"
```

This creates:
- App: `{prefix}-video-intel-dev`
- Lakebase project: `{prefix}-video-intel-dev`

### 2. Deploy

Set your workspace host first: either edit `host` under the `dev` target in `databricks.yml`, or remove that line and let the active CLI profile / `DATABRICKS_HOST` env var provide it.

```bash
# Validate the bundle
databricks bundle validate -t dev

# Deploy (creates the app + Lakebase project)
databricks bundle deploy -t dev

# Start the app
databricks bundle run video_intelligence_app -t dev
```

The first deploy creates the Lakebase project automatically. The app's Lakebase resource links to it, and the app creates its history schema on first start. No manual database configuration is needed: the database name is the fixed default `databricks-postgres`, and the connection endpoint is resolved at runtime, so nothing is hardcoded to a prefix or workspace.

### 3. Network egress (if restricted)

Databricks Apps run on serverless compute, which is **deny-by-default for outbound internet**. If your workspace has a serverless network policy set to "restricted", the app cannot reach the internet and you will see `[Errno -3] Temporary failure in name resolution` (the build fails installing packages; at runtime, video search and metadata fail).

The app needs these destinations:

| Destination | Needed for |
|-------------|-----------|
| `pypi.org`, `files.pythonhosted.org` | Build (pip install) |
| `www.youtube.com`, `youtube.com` | Runtime (oEmbed metadata + Theme Explorer search) |

Fix (workspace/account admin): in **Settings > Network > Network policies**, either set egress to **Allow access to all destinations**, or add the FQDNs above under **Restricted access to specific destinations**.

Important: a network-policy change does **not** apply to already-running app compute. After changing it, **stop and start the app** so its compute re-provisions:

```bash
databricks apps stop {prefix}-video-intel-dev
databricks apps start {prefix}-video-intel-dev
```

(Gemini analysis itself does not need egress: Google fetches the video server-side. Single-video analysis degrades gracefully if metadata is blocked, but Theme Explorer's YouTube search and the build genuinely require egress.)

### 4. Access the app

The deploy output shows the app URL:
```
https://{prefix}-video-intel-dev-{workspace-id}.aws.databricksapps.com
```

## Local Development

```bash
# Python backend
uv venv --python 3.11 && uv sync --all-extras
.venv/bin/ruff check app/ tests/
.venv/bin/ruff format app/ tests/
.venv/bin/pytest tests/ -v

# Frontend utility tests (Node.js built-in test runner, no install)
node --test tests/test_frontend_utils.mjs
```

## Project Structure

```
app/
  app.py              # FastAPI backend (API endpoints + static file serving)
  static/index.html   # React SPA via CDN, JSX transpiled in-browser by Babel
  static/utils.js     # Pure helper functions, unit-tested with node --test
  gemini.py           # Gemini client + analyze_video() + synthesize_text()
  youtube.py          # yt-dlp YouTube search + filter + rank
  schemas.py          # Pydantic models: VideoSummary, ThemeAnalysis
  lakebase.py         # SQLAlchemy-based Lakebase persistence
  app.yaml            # Databricks App config
  requirements.txt    # Generated from pyproject.toml — see Dependency management
databricks.yml        # DAB config (app + Lakebase project)
tests/                # Python tests (backend + Lakebase) + Node tests for utils.js
```

## Dependency Management

`pyproject.toml` is the **single source of truth** for Python dependencies. `app/requirements.txt` is **generated** from it because the Databricks Apps build container does not read `pyproject.toml` natively — only `requirements.txt` is supported.

To regenerate after changing dependencies in `pyproject.toml`:

```bash
uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o app/requirements.txt
```

Notes:
- `--no-dev` excludes development dependencies (ruff, pytest, mypy)
- `--no-emit-project` excludes the project itself
- `--no-hashes` keeps the file readable; the platform doesn't require pinned hashes
- The output is fully version-pinned (transitive deps included) for reproducible builds. The first lines are an autogenerated comment that records the exact command used.
- Commit both files together. CI / a pre-commit hook can verify they're in sync by running the command and diffing.

Background: the Databricks Apps platform documents that "only `requirements.txt` is natively supported" for Python apps. There is also a `uv`-runner workaround (one-line `requirements.txt` containing just `uv` plus `command: [uv, run, app.py]` in `app.yaml`), but it shifts dependency installation to app startup and slows cold starts. We chose generation at build time instead.

## Token Budget

3 videos analyzed at 0.1 FPS with last 60s trimmed. Estimated: ~150K-300K input tokens per theme analysis. The workspace FMAPI rate limit may throttle parallel calls; the app handles 429s with retries.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Build fails: `Temporary failure in name resolution` on pypi.org | Serverless egress blocks PyPI. Allowlist `pypi.org` + `files.pythonhosted.org`, then stop/start the app. See [Network egress](#3-network-egress-if-restricted). |
| Videos fail to analyze | Gemini rate limit (429). Wait 1 min and retry. |
| "Could not fetch video metadata" | Egress to `youtube.com` is blocked, or the URL is not a valid YouTube video. Single-video analysis still runs with a fallback title; Theme Explorer needs egress. See [Network egress](#3-network-egress-if-restricted). |
| History not showing | Check `databricks apps logs {app-name}`. If you see `permission denied for schema app`, the app was deleted/recreated and got a new service principal. Connect to Lakebase and `DROP SCHEMA app CASCADE` (discards old history), then stop/start the app so it recreates the schema. |
| App crashes on start | Check `databricks apps logs {app-name}`. Common: missing deps in requirements.txt, or egress blocking the build. |
| Network policy change had no effect | Egress config binds at compute provision time. Stop and start the app, do not just redeploy. |

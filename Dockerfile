# Container image for the Garmin MCP server (Streamable HTTP + bearer auth).

FROM ghcr.io/astral-sh/uv:0.4.20-python3.12-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy project metadata and sources early so the build backend sees README and package
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install dependencies and the project (no dev deps). The build resolves the
# current GarminConnect native-auth dependency when the checked-in lock changes.
RUN uv sync --no-dev

# Garmin OAuth tokens live at /root/.garminconnect. On Railway, attach a
# persistent volume mounted at that path (railway volume add --mount-path
# /root/.garminconnect) so tokens survive redeploys. NOTE: do NOT add a Docker
# VOLUME instruction for that path — Railway's builder rejects it as a conflict
# with the mounted volume.

# Default port for local runs. Railway injects $PORT at runtime, which the
# server prefers over this. EXPOSE is documentation only.
EXPOSE 8000

# Runtime environment variables (set on Railway, never baked into the image):
#   MCP_ACCESS_TOKEN   (required) bearer token guarding /mcp
#   GARMIN_TOKEN_JSON (one-time secure bootstrap for the native token store)
#   GARMIN_EMAIL / GARMIN_PASSWORD  (fallback) full login; may trigger MFA
#   GARMIN_MFA_CODE / GARMIN_MFA_WAIT_SECONDS  (optional) non-interactive MFA
#   GARMIN_MCP_TRANSPORT=streamable-http, GARMIN_MCP_HOST=0.0.0.0
#   PORT (injected by Railway) / GARMIN_MCP_PORT (fallback)
ENV GARMIN_MCP_TRANSPORT=streamable-http \
    GARMIN_MCP_HOST=0.0.0.0

# Container-level health probe (Railway also probes /healthz via railway.json).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
port=os.environ.get('PORT') or os.environ.get('GARMIN_MCP_PORT') or '8000'; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4).status==200 else 1)" \
    || exit 1

# Run the bearer-authenticated Streamable HTTP server (respects $PORT).
ENTRYPOINT ["uv", "run", "garmin-mcp-http"]

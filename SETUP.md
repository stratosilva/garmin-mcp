# Garmin MCP on Railway — Setup & Operations

A self-hosted [Garmin Connect MCP server](https://github.com/Taxuspt/garmin_mcp)
(110+ tools) running on Railway over **Streamable HTTP**, protected by a
**bearer token**, and connectable to Claude (web / desktop / mobile) as a remote
custom connector.

---

## 1. Architecture

```
Claude (web / desktop / mobile)
        │   HTTPS + Authorization: Bearer <MCP_ACCESS_TOKEN>
        ▼
https://<your-app>.up.railway.app/mcp
        │
        ▼
┌─────────────────────────────────────────────┐
│ Railway service  "garmin-mcp"               │
│                                             │
│  BearerAuthMiddleware (ASGI)                │
│    • /healthz  → open (Railway health probe)│
│    • everything else → 401 without token    │
│        │                                    │
│        ▼                                    │
│  FastMCP Streamable HTTP app  (/mcp)        │
│    • 110+ Garmin tools                      │
│        │                                    │
│        ▼                                    │
│  garminconnect + garth                      │
│    • OAuth tokens on volume /root/.garminconnect
└─────────────────────────────────────────────┘
        │  authenticated API calls (bearer OAuth token)
        ▼
   Garmin Connect API
```

Key design points:

- **Auth is fail-closed.** The server *refuses to start* unless
  `MCP_ACCESS_TOKEN` (≥16 chars) is set, so the `/mcp` endpoint is never exposed
  unprotected — not even for one deploy. Token comparison is constant-time.
- **`$PORT` is respected.** uvicorn binds `0.0.0.0:$PORT` (Railway injects
  `$PORT`; falls back to `GARMIN_MCP_PORT`, then `8000`). Binding is controlled
  by uvicorn, not FastMCP settings.
- **Token-first Garmin login.** To avoid Garmin's Cloudflare TLS-fingerprint
  blocking of cloud IPs, the server prefers a pre-minted **base64 OAuth blob**
  (`GARMIN_TOKEN_BASE64`) and cached tokens on the persistent volume over a full
  email/password login. See §3.

---

## 2. Prerequisites

- Railway CLI installed and logged in (`railway login`).
- This repository pushed to your own GitHub account (e.g. `sumal009/garmin_mcp`).
- Python + [uv](https://docs.astral.sh/uv/) locally (only to mint tokens, §3).

---

## 3. One-time: mint Garmin OAuth tokens locally (recommended)

Garmin tightened login fingerprinting; headless logins from Railway IPs can be
blocked (403 / Cloudflare). Do the **one** real login from your own machine,
then hand Railway only the resulting token blob.

```bash
uv sync
uv run garmin-mcp-mint            # prompts for email, password, and MFA code
```

It prints a single base64 line to stdout (and caches tokens to
`~/.garminconnect`). Copy that line — it is your `GARMIN_TOKEN_BASE64`.

> If you'd rather let Railway do the login (email/password + MFA), skip this and
> see §5 "Alternative: email/password login". The base64 path is more reliable.

---

## 4. Deploy to Railway

From the repository root:

```bash
# 1. Create the project
railway init                       # name it: garmin-mcp

# 2. First deploy — creates the service. It will stay DOWN until the token is
#    set (intentional & safe: the endpoint is never exposed unprotected).
railway up

# 3. Persistent volume for Garmin tokens (survives redeploys)
railway volume add --mount-path /root/.garminconnect

# 4. Environment variables
railway variables \
  --set "MCP_ACCESS_TOKEN=<YOUR_MCP_ACCESS_TOKEN>" \
  --set "GARMIN_TOKEN_BASE64=<paste the base64 line from step 3 in §3>" \
  --set "GARMIN_MCP_TRANSPORT=streamable-http" \
  --set "GARMIN_MCP_HOST=0.0.0.0"

# 5. Redeploy so the volume + variables take effect
railway up

# 6. Generate a public domain (prints https://<something>.up.railway.app)
railway domain

# 7. Follow logs — look for:
#    "Garmin Connect client initialized successfully."
#    "Uvicorn running on http://0.0.0.0:<port>"
railway logs
```

> Volume mount path and variables can also be set in the Railway dashboard
> (Service → Settings → Volumes; Service → Variables) if you prefer.

---

## 5. Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `MCP_ACCESS_TOKEN` | **Yes** | Bearer token guarding `/mcp`. ≥16 chars or the server won't start. |
| `GARMIN_TOKEN_BASE64` | Recommended | Pre-minted OAuth blob (§3). Avoids logging in from Railway's IP. |
| `GARMIN_MCP_TRANSPORT` | Yes | `streamable-http`. |
| `GARMIN_MCP_HOST` | Yes | `0.0.0.0`. |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Fallback | Full login if no token blob/cache. May trigger MFA. |
| `GARMIN_MFA_CODE` | Situational | MFA code for non-interactive login. Remove after tokens are saved. |
| `GARMIN_MFA_WAIT_SECONDS` | Optional | Poll this long (e.g. `180`) for `GARMIN_MFA_CODE`/file to appear. |
| `PORT` | Auto | Injected by Railway; the server binds it. |
| `GARMINTOKENS` | Optional | Token dir; defaults to `~/.garminconnect` (the volume). |
| `STRENGTH_LOG_PATH` | Optional | Dashboard strength corrections/manual sets; defaults to `~/.garminconnect/strength_training.json` on the same volume. |
| `DATABASE_URL` | Recommended | PostgreSQL connection URL used for all dashboard-entered measurements and strength overlays. Reference the Railway Postgres service's `DATABASE_URL`. |

### PostgreSQL for dashboard-entered data

Add a PostgreSQL service to the Railway project and expose its `DATABASE_URL`
to the `garmin-mcp` service. On the next request, the dashboard creates its
tables and performs an idempotent one-time import from the legacy CSV/JSON files
on `/root/.garminconnect`. The original files are retained as a backup.
| `GARMIN_MCP_ENABLE_HOST_CHECK` | Optional | Re-enable the MCP DNS-rebinding Host check (off by default; the bearer token already gates access). |
| `GARMIN_MCP_ALLOWED_HOSTS` | Optional | Extra comma-separated Host values to allow when the host check is on. `RAILWAY_PUBLIC_DOMAIN` is added automatically. |

**Alternative: email/password login (no pre-minted blob).**
Set `GARMIN_EMAIL`, `GARMIN_PASSWORD`, `GARMIN_MCP_TRANSPORT`, `GARMIN_MCP_HOST`,
`MCP_ACCESS_TOKEN`, and `GARMIN_MFA_WAIT_SECONDS=180`. On first boot the server
triggers Garmin's MFA (code sent to your email/SMS). Because changing a Railway
variable redeploys the container (restarting the login), the reliable way to
feed the code without a restart is a file on the volume:

```bash
# while the container is waiting (see logs), from another terminal:
railway ssh                                   # into the running container
echo -n "123456" > /root/.garminconnect/mfa   # the code you received
# and set once, up front:
railway variables --set "GARMIN_MFA_CODE_FILE=/root/.garminconnect/mfa"
```

Once tokens are saved to the volume, **remove** `GARMIN_MFA_CODE` /
`GARMIN_MFA_CODE_FILE`. If you hit repeated 403/Cloudflare errors, stop retrying
and use the base64 path (§3) or the local stdio fallback (§8).

---

## 6. Verify end to end

```bash
DOMAIN="https://<your-app>.up.railway.app"

# Health (open) → 200
curl -s -o /dev/null -w "%{http_code}\n" "$DOMAIN/healthz"          # 200

# /mcp WITHOUT token → 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$DOMAIN/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'   # 401

# /mcp WITH token → 200 + an MCP initialize result (SSE)
curl -s -X POST "$DOMAIN/mcp" \
  -H "Authorization: Bearer <YOUR_MCP_ACCESS_TOKEN>" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

---

## 7. Connect to Claude (remote connector)

Claude → **Settings → Connectors → Add custom connector**.

Claude's connector dialog only offers OAuth (Client ID/Secret) — there is **no
field for a custom `Authorization` header**. So supply the token in the URL as a
query parameter and leave the OAuth fields blank:

- **Name:** `Garmin` (anything)
- **URL:** `https://<your-app>.up.railway.app/mcp?token=<YOUR_MCP_ACCESS_TOKEN>`
- **OAuth Client ID / Secret:** leave blank

The server accepts the token from either the URL (`?token=` / `?access_token=`)
or an `Authorization: Bearer <token>` header — use the header form for any client
that supports custom headers (e.g. curl, scripts), since a token in a URL can
appear in server logs. Rotate the token (§9) if you're concerned.

If you'd rather not put the token in a URL at all, use the local stdio fallback
(§8), which needs no bearer token (desktop only).

---

## 8. Local stdio fallback (Claude Desktop, no Railway)

If Garmin blocks cloud logins entirely, run the same server locally over stdio.
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/garmin_mcp", "garmin-mcp"],
      "env": {
        "GARMIN_EMAIL": "you@example.com",
        "GARMIN_PASSWORD": "your-password",
        "GARMIN_MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

Tokens cache to `~/.garminconnect`; MFA is prompted interactively on first run.
No bearer token is needed for stdio (it isn't network-exposed).

---

## 9. Operations

**Rotate the bearer token**
```bash
NEW=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
railway variables --set "MCP_ACCESS_TOKEN=$NEW"   # redeploys automatically
```
Then update the token in Claude's connector settings.

**Re-auth when Garmin tokens expire** (garth OAuth tokens last ~1 year;
symptoms: 401s from Garmin in logs)
```bash
uv run garmin-mcp-mint                              # mint a fresh blob locally
railway variables --set "GARMIN_TOKEN_BASE64=<new blob>"
```

**Redeploy**
```bash
railway up            # rebuilds from the Dockerfile and redeploys
```
Or connect the GitHub repo in the Railway dashboard for auto-deploy on push.

---

## 10. Cost estimate (Railway)

This is a lightweight, mostly-idle Python service (~150–250 MB RAM, negligible
CPU when idle) plus a tiny persistent volume (a few KB of tokens).

| Item | Approx. monthly |
|---|---|
| Compute (always-on, ~256 MB, low CPU) | ~$3–5 |
| Persistent volume (~1 GB min) | ~$0.15–0.25 |
| **Total** | **~$5 / month** |

On Railway's **Hobby** plan ($5/mo, includes $5 of usage), this workload
typically fits within the included usage — so **~$5/month all-in**. Enabling
*App Sleeping* (serverless) reduces compute cost further if the connector is used
only intermittently. The **Pro** plan ($20/mo) is only needed for higher limits
or team features. Pricing is usage-based; check the Railway dashboard for actuals.

---

## 11. Security notes

- The bearer token is the only thing between the public internet and your Garmin
  data. Keep it secret; rotate if leaked (§9).
- `MCP_ACCESS_TOKEN`, `GARMIN_TOKEN_BASE64`, and credentials live only in Railway
  variables — never in git. `.gitignore` covers `.env`, tokens, and token caches.
- `/healthz` is the only unauthenticated route and returns no sensitive data.
- The server fails to start without a valid token, so a misconfigured deploy is
  down, never open.

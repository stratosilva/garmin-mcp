# Garmin → Claude · Self-Hosted MCP Server + Live Dashboard

Put your **Garmin Connect** data inside **Claude** — and run your own private
**health & triathlon dashboard** — on a small server that's entirely yours.

- 🗣️ **Ask your body data in plain English** in Claude (web, desktop, mobile):
  *"How did I sleep?"*, *"Show my last 5 runs"*, *"Am I recovered for a hard session?"*
- 📊 **A live dashboard** at `/dashboard` — body battery, sleep, heart rate,
  stress, VO₂ max, training load, HR zones, weight/hydration, and a
  **Swim / Bike / Run** view that fills in as you train.
- 🔒 **Private by design** — runs on *your* Railway account, locked with *your*
  bearer token. Your Garmin password never leaves your own computer.

Built for the community by [Sudhakar Reddy Gade](https://nirvedha.com/about/) ·
📖 **Full step-by-step guide (no coding needed): <https://www.nirvedha.com/garmin-dashboard/>**

---

## Quick start

### The easy way — let Claude Code do it
Install [Claude Code](https://claude.ai/code), open it in an empty folder, and
paste the ready-made prompt from the
[guide](https://www.nirvedha.com/garmin-dashboard/). It clones this repo, mints
your Garmin token, deploys to Railway, and hands you your connector + dashboard
URLs. You only approve the Railway login and type your Garmin password into your
own terminal.

### The hands-on way
Fork this repo and follow the **[guide](https://www.nirvedha.com/garmin-dashboard/)**
(8 clicks-and-copy steps, ~20 minutes) or the concise **[SETUP.md](SETUP.md)**.
In short: fork → deploy on Railway → add a volume at `/root/.garminconnect` →
set four variables → generate a domain → add to Claude.

---

## What you get

| | |
|---|---|
| **MCP tools** | 90+ Garmin tools (activities, sleep, HRV, body battery, training, gear, …) usable from Claude |
| **Transport** | Streamable HTTP at `/mcp`, health check at `/healthz` |
| **Auth** | Bearer token on every request (header **or** `?token=` in the URL, for clients like Claude that can't set headers) |
| **Dashboard** | Server-rendered, live-on-refresh single page at `/dashboard` |
| **Persistence** | Garmin OAuth tokens on a mounted volume, auto-refreshing |
| **Cost** | ~$5/month on Railway's Hobby plan |

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `MCP_ACCESS_TOKEN` | **Yes** | Bearer token guarding `/mcp` and `/dashboard` (≥16 chars, or the server won't start) |
| `GARMIN_TOKEN_BASE64` | Recommended | Pre-minted Garmin OAuth blob — avoids logging in from a cloud IP (Garmin blocks those) |
| `GARMIN_MCP_TRANSPORT` | Yes | `streamable-http` |
| `GARMIN_MCP_HOST` | Yes | `0.0.0.0` |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Fallback | Full login if no token blob; may trigger MFA |
| `PORT` | Auto | Injected by Railway; the server binds it |
| `DASHBOARD_TZ_OFFSET_HOURS` | Optional | Local-day offset for the dashboard (default `5.5` = IST) |

See **[SETUP.md](SETUP.md)** for the full list, token rotation, and re-auth.

## Security model

- **Fail-closed:** the server refuses to start without a strong `MCP_ACCESS_TOKEN`,
  so the endpoint is never exposed unprotected — not even briefly.
- The bearer check runs **before** the MCP app, so unauthorized callers get `401`
  and never reach your data.
- Your Garmin **password** is only ever used locally to mint a token blob; the
  deployed server uses the blob (read-scoped OAuth tokens), never the password.
- `.gitignore` keeps `.env`, tokens, and caches out of git.

## Architecture

```
Claude (web / desktop / mobile)  ──HTTPS + bearer token──►  Railway service
                                                                 │
                    BearerAuthMiddleware ──► /healthz (open) · /mcp · /dashboard
                                                                 │
                                             FastMCP (90+ tools) + dashboard API
                                                                 │
                                             garminconnect + garth ──► Garmin API
                                             (OAuth tokens on a persistent volume)
```

## Local development

```bash
uv sync
# stdio (for Claude Desktop config):
GARMIN_MCP_TRANSPORT=stdio uv run garmin-mcp
# streamable HTTP + dashboard:
MCP_ACCESS_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))") \
GARMIN_MCP_TRANSPORT=streamable-http uv run garmin-mcp-http
# mint a Garmin token blob (interactive):
uv run garmin-mcp-mint
```

## Credits

Stands on the shoulders of the open-source
[`garmin_mcp`](https://github.com/Taxuspt/garmin_mcp) by **Taxuspt** and the
Docker + streamable-HTTP fork by **k8sautom8**. This repo adds a bearer-token
security layer, `$PORT`/proxy fixes, and the live triathlon dashboard.

## License

MIT — free to use, change, and share. See [LICENSE](LICENSE).

---

<sub>Shared as a community resource by **Nirvedha Executive Coaching Solutions**,
Hyderabad · <https://nirvedha.com></sub>

# Garmin → ChatGPT / Codex · Self-Hosted MCP Server + Live Dashboard

Use your **Garmin Connect** data with **ChatGPT, Codex, or another
MCP-compatible client** — and run your own private **health & triathlon
dashboard** on a small server that's entirely yours.

- 🗣️ **Ask about your body data in plain English** from a connected AI client:
  *"How did I sleep?"*, *"Show my last 5 runs"*, *"Am I recovered for a hard session?"*
- 📊 **A live dashboard** at `/dashboard` — body battery, sleep, heart rate,
  stress, VO₂ max, training load, HR zones, weight/hydration, and a
  **Swim / Bike / Run** view that fills in as you train.
- 🔒 **Private by design** — runs on *your* Railway account, locked with *your*
  bearer token. Your Garmin password never leaves your own computer.

This fork extends the original community project by
[Sudhakar Reddy Gade](https://nirvedha.com/about/) with a PostgreSQL-backed
dashboard and OpenAI-powered recommendations.

---

## Quick start

Follow **[SETUP.md](SETUP.md)** to deploy on Railway, attach the persistent
Garmin volume, configure the required variables, and add PostgreSQL for
dashboard-entered data. Then connect the `/mcp` endpoint from a compatible AI
client, or use the live `/dashboard` directly.

---

## What you get

| | |
|---|---|
| **MCP tools** | 90+ Garmin tools (activities, sleep, HRV, body battery, training, gear, …) usable from an MCP-compatible client |
| **Transport** | Streamable HTTP at `/mcp`, health check at `/healthz` |
| **Auth** | Bearer token on every request (normally the `Authorization` header; the dashboard can also use `?token=`) |
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
| `OPENAI_API_KEY` | Optional | Server-side OpenAI API key for the personalised 24–48h dashboard recommendation |
| `OPENAI_RECOMMENDATION_MODEL` | Optional | Model used for that recommendation (default `gpt-5-mini`) |
| `STRENGTH_LOG_PATH` | Optional | File for dashboard strength corrections and manual sets (default `~/.garminconnect/strength_training.json`) |
| `DATABASE_URL` | Recommended | PostgreSQL connection URL for body measurements, injury scores, and strength-workout overlays. Railway Postgres supplies this automatically when referenced by the service. |

### Dashboard database

When `DATABASE_URL` is configured, dashboard-entered data is stored in PostgreSQL:

- `dashboard_body_measurements`
- `dashboard_injury_measurements`
- `dashboard_strength_overlays`

The application creates these tables at first use. It then imports the existing
body, injury, and strength files once and leaves those files untouched as a
backup. Without `DATABASE_URL`, the legacy file storage continues to work.

### Personalised dashboard recommendation

When `OPENAI_API_KEY` is configured, the dashboard's summary line is replaced
with a structured next-24–48-hour cardio, strength, and recovery/mobility recommendation. The
strength section supplies six movement slots with two alternatives per slot,
three sets, reps and a history-grounded load prescription. One recommendation is generated
and saved per dashboard day, so ordinary page refreshes reuse it without making
another model request. Use **Refresh advice** after logging meaningful new data
(for example, a workout, sleep, or pain score) to explicitly request a new one.
The browser sends the current dashboard snapshot to the protected server; the
server reduces it to recovery, fitness, body-composition, strength-training and
latest pain signals before making the OpenAI API request. The API key is never
sent to or stored in the browser.

Strength activities can be completed from the dashboard after syncing from
Garmin. Open a recent strength activity, assign an exercise to each set, correct
reps or weight, mark unilateral reps as **per side**, or add sets that the watch
did not record. Manual sets accept repetitions or a duration in seconds, so
carries and isometric holds can be recorded with time and optional load. For a
carry, use total carried load consistently (for example, two 24 kg dumbbells as
48 kg). For unilateral holds, **Reps/time are per side** records one duration
for each side. **Copy previous** repeats reps, seconds and weight in one tap, while
**Apply to this + next 2** fills the common three-set exercise pattern.
These edits are a non-destructive local overlay: Garmin's original values stay
visible and can be restored, while the corrected values and manual sets are used
in personalised recommendations. The overlay file should live on the same
persistent volume as the Garmin tokens in production.

The dashboard also rolls those saved working sets into a 12-week muscle-stimulus
view. Primary muscles receive one direct-set credit and assisting muscles receive
half a credit; cardio, steps and recorded floors add conservative, capped
supporting exposure. The selected week's exercise table makes every allocation
visible and flags unclassified exercises instead of silently guessing. These are
planning units, not measured muscle activation or literal hypertrophy-set counts.

Create an API key in the OpenAI Platform and set it as a Railway environment
variable. ChatGPT subscriptions and API billing are managed separately. If the
key is absent or the service is unavailable, the existing rule-based coaching
cue remains in place.

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
ChatGPT / Codex / MCP client  ──HTTPS + bearer token──►  Railway service
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
# stdio (for a local MCP client):
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

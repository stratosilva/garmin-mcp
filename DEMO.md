# Public portfolio demo

The public `/demo` page reuses the real dashboard UI with synthetic data for
**Alan Turing**. Credit: **Manuel Silva Gallego · Built with ChatGPT**.

## What it demonstrates

The tour opens only when a visitor clicks Start tour; Continue tour returns to
the current step without restarting. Nine short tour stops cover data integration, actionable AI advice, sleep and
recovery, muscle allocation, Strava-inspired analytics, Basic-Fit measurements,
injury context, corrected workout inputs, and product strategy. Next, Enter,
Space or Right Arrow advances; Left Arrow goes back; Escape or Skip exits.
Replay and free exploration are always available. Keyboard shortcuts do not
intercept typing in forms or dialogs.

## Data boundary

`demo_data.py` generates fictional values without reading files, credentials,
or Garmin APIs. The page embeds these values and handles its demo requests in
memory. The response's Content Security Policy blocks all network connections
and form submissions. The real `/dashboard`, `/mcp`, and `/api/*` endpoints
remain bearer-protected; only the exact `/demo` path is added to public routes.
No live AI calls are made. Advice is labelled as a prepared example and does
not claim to recalculate after edits. Workout and injury edits reset on reload.
Analytics charts are illustrative snapshots, not recalculated from demo edits.

## Hosting and verification

The existing Railway service serves `/demo` after deploying this commit.
No new service, database, volume, token, or environment variable is required.
Share the base Railway URL plus `/demo`, with **no query string or token**.

Check `/demo` returns 200 without authorization. Check `/dashboard`,
`/api/dashboard`, `/api/injury-settings` and `/mcp` still return 401 without it.
The public page should identify Alan Turing, Manuel Silva Gallego and the
fictional-data boundary. Exercise the tour, settings, notes, and Reset demo.

## Positioning

Lead with the daily decision: an appropriate effort given training load,
recovery and injury context. Describe integration methods accurately: Garmin
connected data, entered body composition measurements, and manual injury reporting. Smart-scale
sync is described as in development.
Attribute Strava inspiration. Avoid unverified market-uniqueness claims or
invented business impact. A useful next evaluation is recommendation relevance,
clarity and uptake; the demo does not claim these outcomes are measured.

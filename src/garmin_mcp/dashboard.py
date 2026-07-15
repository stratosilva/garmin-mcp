"""Live, server-rendered health/triathlon dashboard for the Garmin MCP server.

Adds two bearer-protected routes to the Streamable HTTP app:

- ``GET /api/dashboard`` -> JSON snapshot gathered live from Garmin on each call.
- ``GET /dashboard``     -> a single-page app that fetches the JSON and renders.

Because the server holds the authenticated Garmin session, opening/refreshing
``/dashboard?token=<MCP_ACCESS_TOKEN>`` always shows current data. The page is
triathlon-oriented (Swim / Bike / Run) with graceful empty states for sports
not started yet.
"""

import datetime
import os

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route


# --------------------------------------------------------------------------- #
# Data gathering
# --------------------------------------------------------------------------- #

def _call(fn, *args):
    """Call a Garmin client method, returning None on any error/empty result."""
    try:
        r = fn(*args)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(r, str):  # some methods return "No ... data found." strings
        return None
    return r


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _sport_of(type_key):
    k = (type_key or "").lower()
    if "swim" in k:
        return "swim"
    if "cycl" in k or "bik" in k or "ride" in k:
        return "bike"
    if "run" in k:
        return "run"
    if "walk" in k or "hik" in k:
        return "walk"
    return "other"


def _map_activity(a):
    at = (a.get("activityType") or {}).get("typeKey")
    dur = a.get("duration") or 0
    dist = a.get("distance") or 0
    km = round(dist / 1000.0, 2)
    mins = round(dur / 60.0)
    pace = None
    if km > 0 and dur > 0:
        pace = round((dur / 60.0) / km, 2)
    zones = [round((a.get("hrTimeInZone_" + str(i)) or 0) / 60.0, 1) for i in range(1, 6)]
    return {
        "sport": _sport_of(at),
        "typeKey": at,
        "name": a.get("activityName") or "Activity",
        "date": (a.get("startTimeLocal") or "")[:10],
        "start": a.get("startTimeLocal"),
        "km": km,
        "min": mins,
        "hr": _num(a.get("averageHR")),
        "maxHr": _num(a.get("maxHR")),
        "cal": _num(a.get("calories")),
        "pace": pace,
        "load": round(a.get("activityTrainingLoad") or 0, 1),
        "zones": zones,
        "location": a.get("locationName"),
    }


def _agg(items):
    return {
        "sessions": len(items),
        "km": round(sum(x["km"] for x in items), 1),
        "min": round(sum(x["min"] for x in items)),
        "cal": round(sum((x["cal"] or 0) for x in items)),
    }


def gather(client):
    """Build the full dashboard payload from live Garmin calls."""
    offset = float(os.environ.get("DASHBOARD_TZ_OFFSET_HOURS", "5.5"))
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=offset)
    today = now.date()
    ds = today.isoformat()

    try:
        name = client.get_full_name()
        name = name if isinstance(name, str) and name.strip() else "Athlete"
    except Exception:  # noqa: BLE001
        name = "Athlete"

    out = {
        "generatedAt": now.strftime("%b %d, %Y · %H:%M"),
        "date": ds,
        "name": name,
        "wellness": {},
        "bodyBatterySeries": [],
        "sports": {},
        "recent": [],
        "notes": [],
    }

    # ---- daily stats (fall back to yesterday if today is still empty) ----
    stats = _call(client.get_stats, ds)
    stats_date = ds
    if not stats or stats.get("totalSteps") in (None, 0):
        y = (today - datetime.timedelta(days=1)).isoformat()
        s2 = _call(client.get_stats, y)
        if s2 and s2.get("totalSteps") is not None:
            stats, stats_date = s2, y
    stats = stats or {}
    out["statsDate"] = stats_date

    w = out["wellness"]
    w["steps"] = {"value": _num(stats.get("totalSteps")), "goal": _num(stats.get("dailyStepGoal")) or 10000}
    w["distanceKm"] = round((stats.get("totalDistanceMeters") or 0) / 1000.0, 1)
    w["restingHr"] = {"value": _num(stats.get("restingHeartRate")),
                      "avg7": _num(stats.get("lastSevenDaysAvgRestingHeartRate")),
                      "min": _num(stats.get("minHeartRate")), "max": _num(stats.get("maxHeartRate"))}
    w["stress"] = {"avg": _num(stats.get("averageStressLevel")), "max": _num(stats.get("maxStressLevel"))}
    w["bodyBattery"] = {"current": _num(stats.get("bodyBatteryMostRecentValue")),
                        "high": _num(stats.get("bodyBatteryHighestValue")),
                        "low": _num(stats.get("bodyBatteryLowestValue"))}
    w["spo2"] = {"avg": _num(stats.get("averageSpo2")), "low": _num(stats.get("lowestSpo2"))}
    w["respiration"] = {"avg": _num(stats.get("avgWakingRespirationValue")),
                        "low": _num(stats.get("lowestRespirationValue")),
                        "high": _num(stats.get("highestRespirationValue"))}
    w["intensity"] = {"moderate": _num(stats.get("moderateIntensityMinutes")) or 0,
                      "vigorous": _num(stats.get("vigorousIntensityMinutes")) or 0,
                      "goalWeek": _num(stats.get("intensityMinutesGoal")) or 150}
    w["floors"] = {"value": _num(stats.get("floorsAscended")),
                   "goal": _num(stats.get("userFloorsAscendedGoal")) or 10}
    w["calories"] = {"total": _num(stats.get("totalKilocalories")),
                     "active": _num(stats.get("activeKilocalories")),
                     "bmr": _num(stats.get("bmrKilocalories"))}

    # ---- body battery series (last 7 days) ----
    start = (today - datetime.timedelta(days=6)).isoformat()
    bb = _call(client.get_body_battery, start, ds) or []
    series = []
    for day in bb:
        for pt in (day.get("bodyBatteryValuesArray") or []):
            if isinstance(pt, list) and len(pt) >= 2 and isinstance(pt[1], (int, float)):
                series.append([pt[0], pt[1]])
    series.sort(key=lambda p: p[0])
    out["bodyBatterySeries"] = series
    if series and w["bodyBattery"]["current"] is None:
        w["bodyBattery"]["current"] = series[-1][1]

    # ---- training status: VO2max (run/bike), heat, load ----
    ts = _call(client.get_training_status, ds) or {}
    vo2 = ts.get("mostRecentVO2Max") or {}
    gen = vo2.get("generic") or {}
    cyc = vo2.get("cycling") or {}
    w["vo2maxRun"] = _num(gen.get("vo2MaxValue"))
    w["vo2maxRunDate"] = gen.get("calendarDate")
    w["vo2maxBike"] = _num(cyc.get("vo2MaxValue"))
    heat = vo2.get("heatAltitudeAcclimation") or {}
    w["heatAcclimation"] = _num(heat.get("heatAcclimationPercentage"))
    w["heatTrend"] = heat.get("heatTrend")
    load = (((ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}))
    acute = {}
    for _dev, dev in load.items():
        acute = (dev or {}).get("acuteTrainingLoadDTO") or {}
        break
    w["trainingLoad"] = {"acwr": _num(acute.get("dailyAcuteChronicWorkloadRatio")),
                         "acute": _num(acute.get("dailyTrainingLoadAcute")),
                         "chronic": _num(acute.get("dailyTrainingLoadChronic")),
                         "status": acute.get("acwrStatus")}

    # ---- sleep / hrv / readiness (often empty on wrist-off nights) ----
    sleep = _call(client.get_sleep_data, ds)
    dsd = ((sleep or {}).get("dailySleepDTO") or {}) if isinstance(sleep, dict) else {}
    secs = _num(dsd.get("sleepTimeSeconds"))
    w["sleep"] = {"hours": round(secs / 3600.0, 1) if secs else None,
                  "score": _num(((dsd.get("sleepScores") or {}).get("overall") or {}).get("value"))} if secs else None

    hrv = _call(client.get_hrv_data, ds)
    hsum = ((hrv or {}).get("hrvSummary") or {}) if isinstance(hrv, dict) else {}
    w["hrv"] = {"value": _num(hsum.get("lastNightAvg")), "status": hsum.get("status")} if hsum.get("lastNightAvg") else None

    rd = _call(client.get_training_readiness, ds)
    rd0 = rd[0] if isinstance(rd, list) and rd else (rd if isinstance(rd, dict) else None)
    w["readiness"] = {"score": _num((rd0 or {}).get("score")), "level": (rd0 or {}).get("level")} if rd0 and (rd0 or {}).get("score") is not None else None

    # ---- weight (30d) ----
    w["weight"] = None
    bc = _call(client.get_body_composition, (today - datetime.timedelta(days=30)).isoformat(), ds)
    try:
        dwl = (bc or {}).get("dateWeightList") or []
        grams = None
        if dwl:
            grams = dwl[-1].get("weight")
        if grams is None:
            grams = ((bc or {}).get("totalAverage") or {}).get("weight")
        if grams:
            first = dwl[0].get("weight") if dwl else None
            trend = round((grams - first) / 1000.0, 1) if first else None
            w["weight"] = {"kg": round(grams / 1000.0, 1), "samples": len(dwl), "trend30": trend}
    except Exception:  # noqa: BLE001
        pass

    # ---- hydration (today) ----
    w["hydration"] = None
    hyd = _call(client.get_hydration_data, ds)
    try:
        if isinstance(hyd, dict) and hyd.get("valueInML") is not None:
            w["hydration"] = {"ml": round(hyd.get("valueInML") or 0),
                              "goal": round(hyd.get("goalInML") or hyd.get("baseGoalInML") or 0)}
    except Exception:  # noqa: BLE001
        pass

    # ---- activities -> triathlon sports ----
    raw = _call(client.get_activities, 0, 40) or []
    acts = [_map_activity(a) for a in raw if isinstance(a, dict)]
    out["recent"] = acts[:12]

    def within(dstr, days):
        try:
            d = datetime.datetime.strptime(dstr[:10], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            return False
        delta = (today - d).days
        return 0 <= delta < days

    # ---- training-load trend (last 7 days) ----
    load_by_day = {}
    for a in acts:
        if within(a["date"], 7):
            load_by_day[a["date"]] = load_by_day.get(a["date"], 0) + (a.get("load") or 0)
    trend = []
    for i in range(6, -1, -1):
        dd = today - datetime.timedelta(days=i)
        trend.append({"label": dd.strftime("%a %d"), "load": round(load_by_day.get(dd.isoformat(), 0), 1)})
    out["trainingLoadTrend"] = trend

    # ---- HR zones this week (minutes per zone) ----
    zsum = [0.0, 0.0, 0.0, 0.0, 0.0]
    for a in acts:
        if within(a["date"], 7):
            for i, zv in enumerate(a.get("zones") or []):
                if i < 5:
                    zsum[i] += zv or 0
    out["hrZonesWeek"] = [round(x, 1) for x in zsum]

    for sp in ("swim", "bike", "run", "walk"):
        sp_acts = [a for a in acts if a["sport"] == sp]
        out["sports"][sp] = {
            "hasData": bool(sp_acts),
            "week": _agg([a for a in sp_acts if within(a["date"], 7)]),
            "month": _agg([a for a in sp_acts if within(a["date"], 30)]),
            "last": sp_acts[0] if sp_acts else None,
            "recent": sp_acts[:6],
        }

    return out


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

def add_dashboard_routes(asgi_app, client):
    """Append /dashboard and /api/dashboard routes to the Starlette app."""

    async def api(_request):
        if client is None:
            return JSONResponse({"error": "garmin client not ready"}, status_code=503)
        return JSONResponse(gather(client))

    async def page(_request):
        return HTMLResponse(PAGE_HTML)

    async def favicon(_request):
        from starlette.responses import Response
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
               "<text y='.9em' font-size='90'>⌚</text></svg>")
        return Response(svg, media_type="image/svg+xml",
                        headers={"cache-control": "public, max-age=86400"})

    asgi_app.router.routes.append(Route("/api/dashboard", api, methods=["GET"]))
    asgi_app.router.routes.append(Route("/dashboard", page, methods=["GET"]))
    asgi_app.router.routes.append(Route("/favicon.ico", favicon, methods=["GET"]))


# --------------------------------------------------------------------------- #
# The single-page app (fetches /api/dashboard and renders)
# --------------------------------------------------------------------------- #

PAGE_HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%E2%8C%9A%3C/text%3E%3C/svg%3E">
<title>Triathlon Dashboard</title>
<style>
:root{
  --bg:#eaeef3;--surface:#fff;--surface-2:#f6f8fb;--border:#e3e8ef;
  --text:#0e1a2b;--muted:#5a6b80;--faint:#8a99ad;
  --accent:#1683ff;--accent-2:#12b5a5;
  --swim:#22a7f0;--bike:#f2a20c;--run:#e5484d;
  --good:#12b886;--warn:#f08c00;--low:#fa5252;--track:#e6ebf2;
  --shadow:0 1px 2px rgba(16,27,45,.05),0 10px 26px rgba(16,27,45,.06);--radius:16px;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#080d17;--surface:#111b2c;--surface-2:#0e1725;--border:#223147;
  --text:#e9eff8;--muted:#8ea3be;--faint:#63748d;--accent:#3aa0ff;--accent-2:#25cbba;
  --swim:#38bdf8;--bike:#f5b53c;--run:#ff6b6b;--good:#2fd39a;--warn:#f9b23c;--low:#ff6b6b;
  --track:#1b2840;--shadow:0 1px 2px rgba(0,0,0,.35),0 12px 32px rgba(0,0,0,.4);
}}
:root[data-theme="dark"]{
  --bg:#080d17;--surface:#111b2c;--surface-2:#0e1725;--border:#223147;
  --text:#e9eff8;--muted:#8ea3be;--faint:#63748d;--accent:#3aa0ff;--accent-2:#25cbba;
  --swim:#38bdf8;--bike:#f5b53c;--run:#ff6b6b;--good:#2fd39a;--warn:#f9b23c;--low:#ff6b6b;
  --track:#1b2840;--shadow:0 1px 2px rgba(0,0,0,.35),0 12px 32px rgba(0,0,0,.4);
}
:root[data-theme="light"]{
  --bg:#eaeef3;--surface:#fff;--surface-2:#f6f8fb;--border:#e3e8ef;--text:#0e1a2b;
  --muted:#5a6b80;--faint:#8a99ad;--accent:#1683ff;--accent-2:#12b5a5;
  --swim:#22a7f0;--bike:#f2a20c;--run:#e5484d;--good:#12b886;--warn:#f08c00;--low:#fa5252;
  --track:#e6ebf2;--shadow:0 1px 2px rgba(16,27,45,.05),0 10px 26px rgba(16,27,45,.06);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-variant-numeric:tabular-nums;
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:clamp(18px,4vw,40px)}
.eyebrow{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin:0}
h1{margin:0;font-size:clamp(22px,3.4vw,30px);font-weight:750;letter-spacing:-.02em}
header.top{display:flex;flex-wrap:wrap;align-items:flex-end;gap:14px 22px;margin-bottom:20px}
.sub{color:var(--muted);font-size:14px}
.tools{margin-left:auto;display:flex;gap:8px;align-items:center}
button.rf{display:flex;gap:8px;align-items:center;padding:9px 14px;border-radius:999px;cursor:pointer;
  background:var(--surface);border:1px solid var(--border);box-shadow:var(--shadow);color:var(--text);font-size:13px;font-weight:600}
button.rf:hover{border-color:var(--accent)}
button.rf svg{width:15px;height:15px}
.read{background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 12%,var(--surface)),var(--surface));
  border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:15px 18px;margin-bottom:18px;display:flex;gap:13px}
.read .em{font-size:22px}.read p{margin:0;font-size:14.5px}.read b{color:var(--text)}
.grid{display:grid;gap:14px}
.hero{grid-template-columns:1.5fr 1fr 1fr}
.tri{grid-template-columns:repeat(3,1fr);margin-top:14px}
.stats{grid-template-columns:repeat(4,1fr);margin-top:14px}
@media(max-width:820px){.hero,.tri{grid-template-columns:1fr 1fr}.stats{grid-template-columns:repeat(2,1fr)}}
@media(max-width:520px){.hero,.tri{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:16px;min-width:0}
.card .label{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}
.big{font-size:clamp(24px,4vw,32px);font-weight:750;line-height:1.05}
.unit{font-size:14px;font-weight:600;color:var(--muted);margin-left:3px}
.meta{font-size:12.5px;color:var(--muted);margin-top:4px}
.pill{font-size:10.5px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:3px 8px;border-radius:999px;white-space:nowrap}
.pill.good{color:var(--good);background:color-mix(in srgb,var(--good) 15%,transparent)}
.pill.warn{color:var(--warn);background:color-mix(in srgb,var(--warn) 16%,transparent)}
.pill.low{color:var(--low);background:color-mix(in srgb,var(--low) 15%,transparent)}
.pill.mute{color:var(--faint);background:color-mix(in srgb,var(--faint) 15%,transparent)}
.bb .val{font-size:38px;font-weight:770;line-height:1}
.bbchart{width:100%;height:110px;margin-top:6px;display:block}
.bbaxis{display:flex;justify-content:space-between;font-size:10.5px;color:var(--faint);margin-top:2px}
.ringwrap{display:flex;align-items:center;gap:14px}.ring{width:100px;height:100px;flex:none}
.ringlabel .n{font-size:23px;font-weight:750}.ringlabel .d{font-size:12.5px;color:var(--muted)}
.row{display:flex;align-items:center;gap:10px;margin-top:9px;font-size:13px}
.row .k{width:74px;color:var(--muted);flex:none}
.bar{flex:1;height:8px;background:var(--track);border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--accent),var(--accent-2))}
.row .v{width:60px;text-align:right;font-weight:650;flex:none}
.sec{margin:26px 0 12px;display:flex;align-items:center;gap:10px}
.sec h2{margin:0;font-size:15px;font-weight:700}.sec .line{flex:1;height:1px;background:var(--border)}
/* sport cards */
.sport{position:relative;overflow:hidden}
.sport .top{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.sport .ico{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;font-size:20px;flex:none}
.sport.swim .ico{background:color-mix(in srgb,var(--swim) 18%,transparent)}
.sport.bike .ico{background:color-mix(in srgb,var(--bike) 18%,transparent)}
.sport.run .ico{background:color-mix(in srgb,var(--run) 18%,transparent)}
.sport h3{margin:0;font-size:15px;font-weight:700}.sport .top .d{font-size:11.5px;color:var(--faint)}
.tstat{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:6px}
.tstat .t{background:var(--surface-2);border-radius:10px;padding:9px 10px}
.tstat .t .n{font-size:19px;font-weight:750;line-height:1}.tstat .t .l{font-size:10.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
.sport .last{margin-top:11px;font-size:12.5px;color:var(--muted)}.sport .last b{color:var(--text)}
.empty{display:flex;flex-direction:column;gap:6px;align-items:flex-start;padding:6px 0 2px}
.empty .msg{font-size:13px;color:var(--muted)}.empty .tag{font-size:10.5px}
/* activities */
.acts{display:flex;flex-direction:column;gap:1px;background:var(--border);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
.act{display:grid;grid-template-columns:1.5fr repeat(4,.75fr);gap:10px;align-items:center;background:var(--surface);padding:11px 14px;font-size:13.5px}
.act.head{background:var(--surface-2);color:var(--faint);font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.act .nm{display:flex;align-items:center;gap:9px;font-weight:600;min-width:0}
.act .nm .ic{width:26px;height:26px;border-radius:8px;display:grid;place-items:center;flex:none;font-size:13px}
.act .nm .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.act .nm .t small{display:block;color:var(--muted);font-weight:500;font-size:11.5px}
.act .c{color:var(--muted)}.act .c b{color:var(--text);font-weight:650}
@media(max-width:640px){.act{grid-template-columns:1fr auto;row-gap:5px}.act.head,.act .hidesm{display:none}
  .act .c::before{content:attr(data-k) " ";color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.04em;margin-right:5px}}
.absent{display:flex;flex-wrap:wrap;gap:10px}
.absent .chip{flex:1;min-width:150px;background:var(--surface);border:1px dashed var(--border);border-radius:12px;padding:11px 14px;color:var(--muted)}
.absent .chip b{color:var(--text);display:block;font-size:13.5px}.absent .chip span{font-size:12px}
.tlchart{width:100%;height:120px;display:block}
.zbar{display:flex;height:18px;border-radius:99px;overflow:hidden;background:var(--track);margin:10px 0 12px}
.zbar>span{display:block;height:100%}
.zleg{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--muted)}
.zleg .z{display:flex;align-items:center;gap:6px}.zleg .sw{width:10px;height:10px;border-radius:3px;flex:none}
.zleg b{color:var(--text);font-weight:650}
.hydro .bar{margin-top:10px}
.twogrid{grid-template-columns:1.4fr 1fr}
@media(max-width:820px){.twogrid{grid-template-columns:1fr}}
footer{margin-top:26px;padding-top:15px;border-top:1px solid var(--border);color:var(--muted);font-size:12.5px;display:flex;flex-wrap:wrap;gap:6px 16px}
.state{min-height:40vh;display:grid;place-items:center;color:var(--muted);font-size:14px;text-align:center;padding:40px}
.spin{width:26px;height:26px;border-radius:50%;border:3px solid var(--track);border-top-color:var(--accent);animation:sp .8s linear infinite;margin:0 auto 12px}
@keyframes sp{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.spin{animation:none}}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style></head>
<body>
<div class="wrap" id="app">
  <div class="state"><div><div class="spin"></div>Loading your Garmin data…</div></div>
</div>
<script>
var TOKEN = new URLSearchParams(location.search).get("token") || "";
var ns="http://www.w3.org/2000/svg";
function css(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim();}
function el(t,c,h){var e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e;}
function n(x,f){return (x==null)?"—":(f?f(x):x);}
function comma(x){return x==null?"—":Math.round(x).toLocaleString();}

function load(){
  var app=document.getElementById("app");
  app.innerHTML='<div class="state"><div><div class="spin"></div>Refreshing…</div></div>';
  fetch("/api/dashboard",{headers:{Authorization:"Bearer "+TOKEN}})
   .then(function(r){if(!r.ok)throw new Error("HTTP "+r.status);return r.json();})
   .then(render)
   .catch(function(e){
     app.innerHTML='<div class="state"><div><b>Couldn\'t load data.</b><br>'+e.message+
     '<br><br>Make sure you opened this page with <code>?token=…</code> in the URL.</div></div>';
   });
}

function SPORT(meta,d){
  var s=d.sports[meta.key]||{};
  var c=el("div","card sport "+meta.key);
  var head='<div class="top"><span class="ico">'+meta.emoji+'</span><div><h3>'+meta.name+
    '</h3><div class="d">'+meta.tag+'</div></div></div>';
  if(!s.hasData){
    c.innerHTML=head+'<div class="empty"><span class="pill mute">Ready</span>'+
      '<span class="msg">No '+meta.name.toLowerCase()+' sessions yet — they\'ll show here automatically once you log one on your Garmin.</span></div>';
    return c;
  }
  var wk=s.week||{},mo=s.month||{},last=s.last||{};
  c.innerHTML=head+
    '<div class="tstat">'+
      '<div class="t"><div class="n">'+n(wk.km)+'</div><div class="l">km · 7d</div></div>'+
      '<div class="t"><div class="n">'+n(wk.sessions)+'</div><div class="l">sessions</div></div>'+
      '<div class="t"><div class="n">'+n(mo.km)+'</div><div class="l">km · 30d</div></div>'+
    '</div>'+
    '<div class="last">Last: <b>'+n(last.km)+' km</b> in '+n(last.min)+' min'+
      (last.hr?' · '+last.hr+' bpm':'')+' <span style="color:var(--faint)">('+(last.date||"")+')</span></div>';
  return c;
}

function statCard(label,pill,pcls,big,unit,meta){
  return '<div class="card"><div class="label"><p class="eyebrow">'+label+'</p>'+
    (pill?'<span class="pill '+pcls+'">'+pill+'</span>':'')+'</div>'+
    '<div class="big">'+big+(unit?'<span class="unit">'+unit+'</span>':'')+'</div>'+
    '<div class="meta">'+meta+'</div></div>';
}

function render(d){
  var app=document.getElementById("app");
  app.innerHTML="";
  var w=d.wellness||{};
  var bb=w.bodyBattery||{},steps=w.steps||{},rhr=w.restingHr||{},stress=w.stress||{};

  // header
  var head=el("header","top");
  head.innerHTML='<div><p class="eyebrow">Triathlon Dashboard</p><h1>'+n(d.name)+'</h1>'+
    '<div class="sub">'+n(d.generatedAt)+' · live from Garmin</div></div>'+
    '<div class="tools"><button class="rf" id="rf"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>Refresh</button></div>';
  app.appendChild(head);

  // read line
  var bbv=bb.current, bbcls=bbv==null?"mute":(bbv<25?"low":bbv<50?"warn":"good");
  var msg="Here's your day at a glance.";
  var bits=[];
  if(bbv!=null) bits.push("Body Battery is at <b>"+bbv+"%</b>"+(bbv<25?" — a recovery day":""));
  if(rhr.value!=null&&rhr.avg7!=null) bits.push("resting HR <b>"+rhr.value+"</b> vs "+rhr.avg7+" avg");
  if(steps.value!=null) bits.push("<b>"+comma(steps.value)+"</b> steps");
  if(bits.length) msg=bits.join(" · ")+".";
  var read=el("div","read");
  read.innerHTML='<span class="em">'+(bbv!=null&&bbv<25?"🔋":"🏅")+'</span><p>'+msg+'</p>';
  app.appendChild(read);

  // hero
  var hero=el("div","grid hero");
  var bbCard=el("div","card bb");
  bbCard.innerHTML='<div class="label"><p class="eyebrow">Body Battery · 7 days</p>'+
    '<span class="pill '+bbcls+'">'+(bbv==null?"—":bbv<25?"Low":bbv<50?"Fair":"Good")+'</span></div>'+
    '<div><span class="val" style="color:var(--accent)">'+n(bbv)+'</span> '+
    '<span style="color:var(--muted);font-size:13px">/ 100 now'+(bb.high!=null?" · high "+bb.high:"")+'</span></div>'+
    '<svg class="bbchart" id="bbc" viewBox="0 0 1000 240" preserveAspectRatio="none"></svg><div class="bbaxis" id="bba"></div>';
  hero.appendChild(bbCard);

  var stepPct=(steps.value&&steps.goal)?Math.min(1,steps.value/steps.goal):0;
  var stepCard=el("div","card");
  stepCard.innerHTML='<div class="label"><p class="eyebrow">Steps</p><span class="pill '+(stepPct>=1?"good":"warn")+'">'+Math.round(stepPct*100)+'%</span></div>'+
    '<div class="ringwrap"><svg class="ring" id="ring" viewBox="0 0 120 120"></svg>'+
    '<div class="ringlabel"><div class="n">'+comma(steps.value)+'</div><div class="d">of '+comma(steps.goal)+' goal</div>'+
    '<div class="d" style="margin-top:6px">'+n(w.distanceKm)+' km · '+comma((w.calories||{}).active)+' kcal</div></div></div>';
  hero.appendChild(stepCard);

  // recovery / sleep card
  var rec=el("div","card");
  if(w.sleep||w.hrv||w.readiness){
    var rows="";
    if(w.readiness) rows+='<div class="row"><span class="k">Readiness</span><span class="bar"><i style="width:'+(w.readiness.score||0)+'%"></i></span><span class="v">'+n(w.readiness.score)+'</span></div>';
    if(w.sleep) rows+='<div class="row"><span class="k">Sleep</span><span class="bar"><i style="width:'+Math.min(100,(w.sleep.hours||0)/8*100)+'%"></i></span><span class="v">'+n(w.sleep.hours)+' h</span></div>';
    if(w.hrv) rows+='<div class="row"><span class="k">HRV</span><span class="bar"><i style="width:60%"></i></span><span class="v">'+n(w.hrv.value)+'</span></div>';
    rec.innerHTML='<div class="label"><p class="eyebrow">Recovery</p></div>'+rows;
  } else {
    rec.innerHTML='<div class="label"><p class="eyebrow">Recovery</p><span class="pill mute">No wear</span></div>'+
      '<div class="meta" style="margin-top:8px">Sleep, HRV & readiness need overnight wrist wear — none recorded for this night.</div>';
  }
  hero.appendChild(rec);
  app.appendChild(hero);

  // triathlon
  app.appendChild(sec("Triathlon"));
  var tri=el("div","grid tri");
  tri.appendChild(SPORT({key:"swim",name:"Swim",emoji:"🏊",tag:"pool + open water"},d));
  tri.appendChild(SPORT({key:"bike",name:"Bike",emoji:"🚴",tag:"road + indoor"},d));
  tri.appendChild(SPORT({key:"run",name:"Run",emoji:"🏃",tag:"road + track"},d));
  app.appendChild(tri);

  // wellness stats
  app.appendChild(sec("Today"));
  var tl=w.trainingLoad||{};
  var stats=el("div","grid stats");
  stats.innerHTML=
    statCard("Resting HR", rhr.value!=null&&rhr.avg7!=null&&rhr.value<=rhr.avg7?"Good":"", "good", n(rhr.value),"bpm","7-day avg "+n(rhr.avg7)+" · range "+n(rhr.min)+"–"+n(rhr.max))+
    statCard("Stress", stress.avg!=null?(stress.avg<26?"Low":stress.avg<51?"Medium":"High"):"", stress.avg!=null&&stress.avg>=51?"warn":"mute", n(stress.avg),"avg","Peak "+n(stress.max))+
    statCard("VO₂ Max", "Run", "mute", n(w.vo2maxRun), "", w.vo2maxBike!=null?("Bike "+w.vo2maxBike):("as of "+n(w.vo2maxRunDate)))+
    statCard("Training Load", tl.status?cap(tl.status):"", tl.status=="OPTIMAL"?"good":"mute", n(tl.acwr),"ACWR","Acute "+n(tl.acute)+" · chronic "+n(tl.chronic))+
    statCard("SpO₂", "", "good", n((w.spo2||{}).avg),"%","Lowest "+n((w.spo2||{}).low)+"%")+
    statCard("Respiration", "", "mute", n((w.respiration||{}).avg),"brpm","Range "+n((w.respiration||{}).low)+"–"+n((w.respiration||{}).high))+
    statCard("Intensity", "", "mute", n((w.intensity||{}).moderate)+" / "+n((w.intensity||{}).vigorous),"", "mod / vig min · goal "+n((w.intensity||{}).goalWeek)+"/wk")+
    statCard("Heat Acclim.", w.heatTrend?cap(w.heatTrend):"", "warn", n(w.heatAcclimation),"%", "Calories "+comma((w.calories||{}).total));
  app.appendChild(stats);

  // training load + HR zones
  app.appendChild(sec("Training load & zones"));
  var tz=el("div","grid twogrid");
  var loadTot=(d.trainingLoadTrend||[]).reduce(function(s,x){return s+(x.load||0);},0);
  var tlCard=el("div","card");
  tlCard.innerHTML='<div class="label"><p class="eyebrow">Training load · 7 days</p><p class="eyebrow">'+Math.round(loadTot)+' total</p></div>'+
    '<svg class="tlchart" id="tlc" viewBox="0 0 1000 260" preserveAspectRatio="none"></svg>';
  tz.appendChild(tlCard);

  var zones=d.hrZonesWeek||[0,0,0,0,0];var ztot=zones.reduce(function(s,x){return s+x;},0);
  var zcols=["#9aa7b5","#4dabf7","#51cf66","#ffa94d","#ff6b6b"];var znames=["Z1 Warm-up","Z2 Easy","Z3 Aerobic","Z4 Threshold","Z5 Max"];
  var zCard=el("div","card");
  if(ztot>0){
    var seg=zones.map(function(v,i){return '<span style="width:'+(v/ztot*100)+'%;background:'+zcols[i]+'"></span>';}).join("");
    var leg=zones.map(function(v,i){return '<div class="z"><span class="sw" style="background:'+zcols[i]+'"></span>'+znames[i]+' <b>'+v+'m</b></div>';}).join("");
    zCard.innerHTML='<div class="label"><p class="eyebrow">Heart-rate zones · 7 days</p><p class="eyebrow">'+Math.round(ztot)+' min</p></div>'+
      '<div class="zbar">'+seg+'</div><div class="zleg">'+leg+'</div>';
  } else {
    zCard.innerHTML='<div class="label"><p class="eyebrow">Heart-rate zones · 7 days</p></div><div class="meta" style="margin-top:8px">No zone data recorded this week.</div>';
  }
  tz.appendChild(zCard);
  app.appendChild(tz);

  // body: weight + hydration
  app.appendChild(sec("Body & hydration"));
  var body=el("div","grid stats");
  var wt=w.weight, hy=w.hydration;
  var wtHtml = wt
    ? statCard("Weight", wt.trend30!=null?((wt.trend30>0?"+":"")+wt.trend30+" kg"):"", wt.trend30!=null&&wt.trend30<=0?"good":"mute", n(wt.kg),"kg", wt.samples+" weigh-ins · 30d")
    : statCard("Weight","Not logged","mute","—","","Log on your scale or in Garmin Connect");
  var hyHtml = hy
    ? statCard("Hydration", hy.goal&&hy.ml>=hy.goal?"Goal":"", hy.goal&&hy.ml>=hy.goal?"good":"mute", comma(hy.ml),"ml","of "+comma(hy.goal)+" ml goal")
    : statCard("Hydration","Not logged","mute","—","","Track water intake in Garmin Connect");
  body.innerHTML = wtHtml + hyHtml +
    statCard("Floors","", "mute", n((w.floors||{}).value), "", "of "+n((w.floors||{}).goal)+" goal") +
    statCard("Calories","Total","mute", comma((w.calories||{}).total),"", comma((w.calories||{}).active)+" active + "+comma((w.calories||{}).bmr)+" rest");
  app.appendChild(body);

  // recent activities
  app.appendChild(sec("Recent activities"));
  var acts=el("div","acts");
  acts.appendChild(el("div","act head",'<span>Activity</span><span>Distance</span><span>Time</span><span class="hidesm">Avg HR</span><span>Calories</span>'));
  (d.recent||[]).forEach(function(a){
    var ic={swim:"🏊",bike:"🚴",run:"🏃",walk:"🚶"}[a.sport]||"⌚";
    var col={swim:"var(--swim)",bike:"var(--bike)",run:"var(--run)",walk:"var(--accent)"}[a.sport]||"var(--accent)";
    var r=el("div","act");
    r.innerHTML='<div class="nm"><span class="ic" style="background:color-mix(in srgb,'+col+' 18%,transparent)">'+ic+'</span>'+
      '<span class="t">'+a.name+'<small>'+(a.start||"").replace("T"," ")+(a.location?" · "+a.location:"")+'</small></span></div>'+
      '<div class="c" data-k="Dist"><b>'+n(a.km)+'</b> km</div>'+
      '<div class="c" data-k="Time"><b>'+n(a.min)+'</b> min</div>'+
      '<div class="c hidesm" data-k="HR"><b>'+n(a.hr)+'</b> bpm</div>'+
      '<div class="c" data-k="Cal"><b>'+n(a.cal)+'</b> kcal</div>';
    acts.appendChild(r);
  });
  if(!(d.recent||[]).length) acts.appendChild(el("div","act",'<span style="color:var(--muted)">No recent activities.</span>'));
  app.appendChild(acts);

  // footer
  var f=el("footer",null,'<span>Data pulled live at <b>'+n(d.generatedAt)+'</b>. Refresh anytime for current numbers.</span>');
  app.appendChild(f);

  drawRing(steps);
  drawBB(d.bodyBatterySeries||[]);
  drawTL(d.trainingLoadTrend||[]);
  document.getElementById("rf").addEventListener("click",load);
}

function drawTL(trend){
  var svg=document.getElementById("tlc");if(!svg||!trend.length)return;
  var W=1000,H=260,pB=34,pT=40,n=trend.length,slot=W/n,bw=slot*0.5;
  var max=Math.max.apply(null,trend.map(function(x){return x.load;}).concat([1]));
  var defs=document.createElementNS(ns,"defs");
  defs.innerHTML='<linearGradient id="tg" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="'+css("--accent")+'"/><stop offset="100%" stop-color="'+css("--accent-2")+'"/></linearGradient>';
  svg.appendChild(defs);
  trend.forEach(function(x,i){
    var h=(x.load/max)*(H-pB-pT),bx=i*slot+(slot-bw)/2,by=H-pB-h;
    var r=document.createElementNS(ns,"rect");
    r.setAttribute("x",bx);r.setAttribute("width",bw);r.setAttribute("y",by);r.setAttribute("height",Math.max(h,x.load>0?3:0));
    r.setAttribute("rx",6);r.setAttribute("fill",x.load>0?"url(#tg)":css("--track"));
    if(x.load<=0){r.setAttribute("y",H-pB-3);r.setAttribute("height",3);}
    svg.appendChild(r);
    if(x.load>0){var t=document.createElementNS(ns,"text");t.setAttribute("x",bx+bw/2);t.setAttribute("y",by-7);
      t.setAttribute("text-anchor","middle");t.setAttribute("fill",css("--text"));t.setAttribute("font-size",20);t.setAttribute("font-weight",700);
      t.textContent=Math.round(x.load);svg.appendChild(t);}
    var dl=document.createElementNS(ns,"text");dl.setAttribute("x",bx+bw/2);dl.setAttribute("y",H-9);
    dl.setAttribute("text-anchor","middle");dl.setAttribute("fill",css("--faint"));dl.setAttribute("font-size",18);
    dl.textContent=x.label;svg.appendChild(dl);
  });
}

function sec(t){var s=el("div","sec");s.innerHTML='<h2>'+t+'</h2><span class="line"></span>';return s;}
function cap(s){s=(s||"").toString().toLowerCase().replace(/_/g," ");return s.charAt(0).toUpperCase()+s.slice(1);}

function drawRing(steps){
  var svg=document.getElementById("ring");if(!svg)return;
  var pct=(steps.value&&steps.goal)?Math.min(1,steps.value/steps.goal):0,r=52,c=2*Math.PI*r;
  function circ(st){var e=document.createElementNS(ns,"circle");e.setAttribute("cx",60);e.setAttribute("cy",60);
    e.setAttribute("r",r);e.setAttribute("fill","none");e.setAttribute("stroke",st);e.setAttribute("stroke-width",11);e.setAttribute("stroke-linecap","round");return e;}
  svg.appendChild(circ(css("--track")));
  var fg=circ(css("--accent"));fg.setAttribute("stroke-dasharray",c);fg.setAttribute("stroke-dashoffset",c*(1-pct));
  fg.setAttribute("transform","rotate(-90 60 60)");svg.appendChild(fg);
}

function drawBB(series){
  var svg=document.getElementById("bbc");if(!svg||!series.length)return;
  var W=1000,H=240,pT=14,pB=16,t0=series[0][0],t1=series[series.length-1][0]||t0+1;
  function X(t){return (t1===t0)?0:(t-t0)/(t1-t0)*W;}
  function Y(v){return pT+(1-v/100)*(H-pT-pB);}
  var defs=document.createElementNS(ns,"defs");
  defs.innerHTML='<linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="'+css("--accent")+'" stop-opacity=".38"/><stop offset="100%" stop-color="'+css("--accent")+'" stop-opacity="0"/></linearGradient>';
  svg.appendChild(defs);
  [25,50,75].forEach(function(g){var l=document.createElementNS(ns,"line");l.setAttribute("x1",0);l.setAttribute("x2",W);
    l.setAttribute("y1",Y(g));l.setAttribute("y2",Y(g));l.setAttribute("stroke",css("--border"));l.setAttribute("stroke-width",1.5);svg.appendChild(l);});
  var line=series.map(function(p,i){return (i?"L":"M")+X(p[0]).toFixed(1)+" "+Y(p[1]).toFixed(1);}).join(" ");
  var area="M0 "+H+" L"+X(series[0][0]).toFixed(1)+" "+Y(series[0][1]).toFixed(1)+" "+
    series.slice(1).map(function(p){return "L"+X(p[0]).toFixed(1)+" "+Y(p[1]).toFixed(1);}).join(" ")+" L"+W+" "+H+" Z";
  function path(d,f,s,sw){var p=document.createElementNS(ns,"path");p.setAttribute("d",d);p.setAttribute("fill",f||"none");
    if(s){p.setAttribute("stroke",s);p.setAttribute("stroke-width",sw);p.setAttribute("stroke-linejoin","round");p.setAttribute("vector-effect","non-scaling-stroke");}return p;}
  svg.appendChild(path(area,"url(#g)"));
  svg.appendChild(path(line,null,css("--accent"),3));
  var last=series[series.length-1],dot=document.createElementNS(ns,"circle");
  dot.setAttribute("cx",X(last[0]));dot.setAttribute("cy",Y(last[1]));dot.setAttribute("r",5);
  dot.setAttribute("fill",css("--accent"));dot.setAttribute("stroke",css("--surface"));dot.setAttribute("stroke-width",2.5);svg.appendChild(dot);
  // day ticks
  var days={},order=[];
  series.forEach(function(p){var dd=new Date(p[0]);var key=dd.getMonth()+"-"+dd.getDate();if(!(key in days)){days[key]=1;order.push(dd);}});
  document.getElementById("bba").innerHTML=order.map(function(dd){return "<span>"+dd.toLocaleDateString(undefined,{weekday:"short"})+" "+dd.getDate()+"</span>";}).join("");
}

load();
</script>
</body></html>
"""

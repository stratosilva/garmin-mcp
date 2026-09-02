"""Live, server-rendered health/triathlon dashboard for the Garmin MCP server.

Adds two bearer-protected routes to the Streamable HTTP app:

- ``GET /api/dashboard`` -> JSON snapshot gathered live from Garmin on each call.
- ``GET /dashboard``     -> a single-page app that fetches the JSON and renders.

Because the server holds the authenticated Garmin session, opening/refreshing
``/dashboard?token=<MCP_ACCESS_TOKEN>`` always shows current data. The page is
training-oriented (gym workouts, cycling and running), with seven-day sleep,
HRV, activity and recovery trends.
"""

import csv
import datetime
import json
import os
import shutil
import time
from pathlib import Path

import requests

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route


_HISTORY_CACHE = {"at": 0.0, "activities": []}
_BODY_FIELDS = ("weight_kg", "fat_pct", "muscle_pct", "body_water_pct")
_INJURY_FIELDS = ("left_big_toe_strain", "left_foot_plantar_fasciitis", "right_knee_patellar_tendon")


def _recommendation_snapshot(dashboard):
    """Keep the model input useful, small, and free of account identifiers."""
    wellness = dashboard.get("wellness") or {}
    body = (dashboard.get("body") or {}).get("metrics") or {}
    injuries = (dashboard.get("injuries") or {}).get("records") or []
    latest_pain = next((row for row in reversed(injuries)
                        if any(row.get(field) is not None for field in _INJURY_FIELDS)), None)
    return {
        "date": dashboard.get("date"),
        "recovery": {
            "body_battery": wellness.get("bodyBattery"),
            "training_readiness": wellness.get("readiness"),
            "sleep": wellness.get("sleep"),
            "hrv": wellness.get("hrv"),
            "resting_heart_rate": wellness.get("restingHr"),
            "stress": wellness.get("stress"),
        },
        "fitness": {
            "vo2_max_run": wellness.get("vo2maxRun"),
            "vo2_max_bike": wellness.get("vo2maxBike"),
            "training_load": wellness.get("trainingLoad"),
            "relative_effort": dashboard.get("relativeEffort"),
            "recent_activities": (dashboard.get("recent") or [])[:5],
        },
        "body_composition": body,
        "pain_latest": latest_pain,
    }


def _openai_recommendation(dashboard):
    """Request a concise, non-medical next-48-hour coaching recommendation."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Personal recommendations are not configured yet.")

    model = os.environ.get("OPENAI_RECOMMENDATION_MODEL", "gpt-5-mini")
    instructions = (
        "You are a cautious endurance and general-fitness coach. Use only the supplied "
        "data. Give one concise recommendation for the next 24–48 hours (maximum 70 "
        "words). State the training intensity or recovery action, and name the two most "
        "important signals behind it. Do not diagnose, prescribe, or claim medical certainty. "
        "If pain is 4/10 or higher, worsening, or recovery signals are poor, favour rest or "
        "easy movement and advise professional assessment if pain persists or worsens. "
        "Do not repeat every metric or mention missing data."
    )
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "instructions": instructions,
            "input": json.dumps(_recommendation_snapshot(dashboard), separators=(",", ":")),
            "max_output_tokens": 180,
            "store": False,
        },
        timeout=20,
    )
    if not response.ok:
        raise RuntimeError("The recommendation service is temporarily unavailable.")
    payload = response.json()
    text = (payload.get("output_text") or "").strip()
    if not text:
        raise RuntimeError("The recommendation service returned no advice.")
    return {"text": text, "model": model}


def _body_measurements_path():
    """Persistent data location; Railway's Garmin volume survives deployments."""
    configured = os.environ.get("BODY_MEASUREMENTS_PATH")
    return Path(configured) if configured else Path.home() / ".garminconnect" / "body_measurements.csv"


def _injury_measurements_path():
    """Persistent daily pain log, stored alongside the body measurements."""
    configured = os.environ.get("INJURY_MEASUREMENTS_PATH")
    return Path(configured) if configured else Path.home() / ".garminconnect" / "injury_measurements.csv"


def _recommendation_cache_path():
    """Persist the latest daily recommendation on the Railway volume."""
    configured = os.environ.get("RECOMMENDATION_CACHE_PATH")
    return Path(configured) if configured else Path.home() / ".garminconnect" / "daily_recommendation.json"


def _cached_recommendation(date):
    try:
        payload = json.loads(_recommendation_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if payload.get("date") == date and payload.get("text") else None


def _save_recommendation(date, recommendation):
    target = _recommendation_cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": date, "text": recommendation["text"], "model": recommendation["model"],
               "savedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(target)
    return payload


def _ensure_body_measurements_file():
    target = _body_measurements_path()
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    bundled = Path(__file__).resolve().parent / "data" / "body_measurements.csv"
    if bundled.exists():
        shutil.copyfile(bundled, target)
    else:
        target.write_text("timestamp,weight_kg,fat_pct,muscle_pct,bone_pct,body_water_pct,source\n", encoding="utf-8")
    return target


def _body_measurements():
    """Read Basic-Fit measurements and expose each latest value and change."""
    records = []
    try:
        with _ensure_body_measurements_file().open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                timestamp = row.get("timestamp")
                if not timestamp:
                    continue
                try:
                    parsed = datetime.datetime.fromisoformat(timestamp)
                except ValueError:
                    continue
                record = {"timestamp": parsed.isoformat(), "date": parsed.strftime("%b %d, %Y")}
                for field in _BODY_FIELDS:
                    try:
                        record[field] = float(row[field]) if row.get(field) not in (None, "") else None
                    except ValueError:
                        record[field] = None
                records.append(record)
    except OSError:
        return {"records": [], "metrics": {}}
    records.sort(key=lambda item: item["timestamp"])
    metrics = {}
    for field in _BODY_FIELDS:
        values = [record for record in records if record.get(field) is not None]
        if values:
            latest, previous = values[-1], values[-2] if len(values) > 1 else None
            metrics[field] = {"value": latest[field], "date": latest["date"],
                              "delta": round(latest[field] - previous[field], 1) if previous else None}
    return {"records": records, "metrics": metrics}


def _append_body_measurement(payload):
    """Validate and append one future Basic-Fit measurement to persistent CSV."""
    timestamp = payload.get("timestamp")
    try:
        datetime.datetime.fromisoformat(timestamp)
    except (TypeError, ValueError) as exc:
        raise ValueError("timestamp must be ISO-8601, e.g. 2026-06-22T10:03:00") from exc
    row = {"timestamp": timestamp, "source": payload.get("source") or "Basic-Fit"}
    for field in ("weight_kg", "fat_pct", "muscle_pct", "bone_pct", "body_water_pct"):
        value = payload.get(field)
        if value in (None, ""):
            row[field] = ""
        else:
            try:
                row[field] = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} must be a number") from exc
    if not any(row[field] != "" for field in _BODY_FIELDS):
        raise ValueError("provide at least one body measurement")
    target = _ensure_body_measurements_file()
    with target.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("timestamp", "weight_kg", "fat_pct", "muscle_pct", "bone_pct", "body_water_pct", "source"))
        writer.writerow(row)


def _injury_measurements(days=30):
    """Return a complete recent daily pain series, with missing entries as null."""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days - 1)
    by_date = {}
    target = _injury_measurements_path()
    try:
        if target.exists():
            with target.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    try:
                        date = datetime.date.fromisoformat((row.get("date") or "")[:10])
                    except ValueError:
                        continue
                    if date < start or date > today:
                        continue
                    values = {}
                    for field in _INJURY_FIELDS:
                        try:
                            value = row.get(field)
                            values[field] = float(value) if value not in (None, "") else None
                        except ValueError:
                            values[field] = None
                    by_date[date] = values
    except OSError:
        pass
    return {"records": [
        {"date": (start + datetime.timedelta(days=offset)).isoformat(), **by_date.get(start + datetime.timedelta(days=offset), {})}
        for offset in range(days)
    ]}


def _append_injury_measurement(payload):
    """Validate and upsert today's three Numeric Rating Scale pain values."""
    date = payload.get("date") or datetime.date.today().isoformat()
    try:
        parsed_date = datetime.date.fromisoformat(str(date)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if parsed_date > datetime.date.today():
        raise ValueError("pain measurements cannot be entered for a future day")
    row = {"date": parsed_date.isoformat()}
    for field in _INJURY_FIELDS:
        try:
            value = float(payload[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("enter a whole-number pain score from 0 to 10 for each injury") from exc
        if not value.is_integer() or not 0 <= value <= 10:
            raise ValueError("pain scores must be whole numbers from 0 to 10")
        row[field] = int(value)

    target = _injury_measurements_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    try:
        if target.exists():
            with target.open(newline="", encoding="utf-8") as handle:
                existing = [old for old in csv.DictReader(handle) if old.get("date") != row["date"]]
    except OSError as exc:
        raise ValueError("could not read the injury log") from exc
    existing.append(row)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("date", *_INJURY_FIELDS))
        writer.writeheader()
        writer.writerows(existing)


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


def _find_num(data, keys):
    """Find the first numeric value for one of ``keys`` in a Garmin payload.

    Garmin's wellness endpoints have changed field names between device and
    account generations. A recursive lookup keeps the dashboard useful while
    preferring the documented/current spelling supplied by the caller.
    """
    wanted = {key.lower() for key in keys}
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() in wanted and isinstance(value, (int, float)):
                return value
        for value in data.values():
            found = _find_num(value, keys)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_num(value, keys)
            if found is not None:
                return found
    return None


def _sleep_values(payload):
    daily = ((payload or {}).get("dailySleepDTO") or {}) if isinstance(payload, dict) else {}
    seconds = _num(daily.get("sleepTimeSeconds"))
    score = _num(((daily.get("sleepScores") or {}).get("overall") or {}).get("value"))
    return {
        "hours": round(seconds / 3600.0, 1) if seconds else None,
        "score": score,
    }


def _hrv_values(payload):
    summary = ((payload or {}).get("hrvSummary") or {}) if isinstance(payload, dict) else {}
    return {
        "value": _num(summary.get("lastNightAvg")),
        "status": summary.get("status"),
        "baselineLow": _num(summary.get("baselineLow")),
        "baselineHigh": _num(summary.get("baselineHigh")),
    }


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
    sport = _sport_of(at)
    dur = a.get("duration") or 0
    dist = a.get("distance") or 0
    km = round(dist / 1000.0, 2)
    mins = round(dur / 60.0)
    pace = None
    if km > 0 and dur > 0:
        pace = round((dur / 60.0) / km, 2)
    zones = [round((a.get("hrTimeInZone_" + str(i)) or 0) / 60.0, 1) for i in range(1, 6)]
    # Transparent Suffer-style estimate. Zone coefficients increase
    # non-linearly, then a sport factor calibrates cross-sport differences to
    # the user's reference week (run 8 points, SkiErg/HIIT 2 points).
    effort_base = sum(minutes * weight for minutes, weight in zip(zones, (1, 2, 3, 5, 7))) / 10.0
    sport_multiplier = {"run": 3.5, "bike": 2.0, "swim": 1.8, "other": 1.7, "walk": 1.0}.get(sport, 1.7)
    effort = round(effort_base * sport_multiplier, 1)
    return {
        "sport": sport,
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
        "effort": effort if sum(zones) else None,
        "effortBase": round(effort_base, 1) if sum(zones) else None,
        "effortMultiplier": sport_multiplier,
        "location": a.get("locationName"),
    }


def _agg(items):
    return {
        "sessions": len(items),
        "km": round(sum(x["km"] for x in items), 1),
        "min": round(sum(x["min"] for x in items)),
        "cal": round(sum((x["cal"] or 0) for x in items)),
    }


def _history_activities(client, today):
    """Return two visible years plus a warm-up window for Fitness."""
    # The first Garmin page contains the newest activities. Refresh it on each
    # dashboard request so a second workout today is reflected immediately,
    # while keeping the older 800-day history cached for the Fitness model.
    if _HISTORY_CACHE["activities"] and time.time() - _HISTORY_CACHE["at"] < 1800:
        page = _call(client.get_activities, 0, 100) or []
        newest = [_map_activity(activity) for activity in page if isinstance(activity, dict)]
        if newest:
            fresh_keys = {(item.get("start"), item.get("name")) for item in newest}
            retained = [
                item for item in _HISTORY_CACHE["activities"]
                if (item.get("start"), item.get("name")) not in fresh_keys
            ]
            _HISTORY_CACHE["activities"] = newest + retained
        return _HISTORY_CACHE["activities"]
    cutoff, gathered = today - datetime.timedelta(days=800), []
    for start in range(0, 1000, 100):
        page = _call(client.get_activities, start, 100) or []
        mapped = [_map_activity(activity) for activity in page if isinstance(activity, dict)]
        if not mapped:
            break
        gathered.extend(mapped)
        dates = [datetime.datetime.strptime(x["date"], "%Y-%m-%d").date() for x in mapped if x["date"]]
        if (dates and min(dates) <= cutoff) or len(mapped) < 100:
            break
    _HISTORY_CACHE.update({"at": time.time(), "activities": gathered})
    return gathered


def _training_history(activities, today):
    """Build separate effort and Garmin-load Fitness series (Banister-style)."""
    daily = {}
    for activity in activities:
        try:
            date = datetime.datetime.strptime(activity["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if date > today or (today - date).days > 800:
            continue
        entry = daily.setdefault(date, {"garminLoad": 0.0, "effort": 0.0})
        garmin_load = activity.get("load") or 0
        entry["garminLoad"] += garmin_load
        entry["effort"] += activity.get("effort") or round(garmin_load / 6.3, 1)
    start, fitness, fatigue, series = max(min(daily) if daily else today, today - datetime.timedelta(days=800)), 0.0, 0.0, []
    for offset in range((today - start).days + 1):
        date, entry = start + datetime.timedelta(days=offset), daily.get(start + datetime.timedelta(days=offset), {})
        # Fitness intentionally uses Garmin's EPOC-based Training Load. This was
        # the earlier dashboard model and produces the user's expected ~13.5
        # index. The HR-zone Relative Effort score remains a separate series.
        load = entry.get("garminLoad") or entry.get("effort") or 0.0
        fitness += (load - fitness) / 42.0
        fatigue += (load - fatigue) / 7.0
        series.append({"date": date.isoformat(), "label": date.strftime("%b %-d"), "load": round(load, 1), "effort": round(entry.get("effort") or 0, 1), "garminLoad": round(entry.get("garminLoad") or 0, 1), "fitness": round(fitness, 1), "fatigue": round(fatigue, 1), "form": round(fitness - fatigue, 1)})
    visible_start = (today - datetime.timedelta(days=730)).isoformat()
    return [row for row in series if row["date"] >= visible_start]


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

    # VO₂ Max ratings are age-specific. Prefer the Garmin profile, while
    # allowing a private deployment override if the profile does not expose a
    # birth date.
    profile = _call(client.get_user_profile) or {}
    birth_date = (
        profile.get("birthDate")
        or profile.get("dateOfBirth")
        or profile.get("dob")
        or os.environ.get("DASHBOARD_DATE_OF_BIRTH")
    )
    age = None
    if birth_date:
        try:
            born = datetime.date.fromisoformat(str(birth_date)[:10])
            age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        except (TypeError, ValueError):
            pass
    if age is None:
        age = _num(os.environ.get("DASHBOARD_AGE"))

    out = {
        "generatedAt": now.strftime("%b %d, %Y · %H:%M"),
        "date": ds,
        "name": name,
        "wellness": {},
        "bodyBatterySeries": [],
        "sports": {},
        "workouts": {},
        "body": _body_measurements(),
        "injuries": _injury_measurements(),
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
    w["intensity"] = {"moderate": 0, "vigorous": 0, "total": 0, "goalWeek": 350}
    w["floors"] = {"value": round(stats.get("floorsAscended")) if _num(stats.get("floorsAscended")) is not None else None,
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
    w["vo2RatingAge"] = int(age) if age is not None else None
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

    # ---- seven-day wellness histories ----
    # Daily stats provide reliable steps/calories averages. Garmin's dedicated
    # intensity endpoint provides the *current-week* moderated/vigorous total.
    daily_stats = []
    sleep_series, hrv_series = [], []
    for i in range(6, -1, -1):
        day = today - datetime.timedelta(days=i)
        day_s = day.isoformat()
        day_stats = stats if day_s == stats_date else _call(client.get_stats, day_s)
        if isinstance(day_stats, dict):
            daily_stats.append(day_stats)
        sleep_values = _sleep_values(_call(client.get_sleep_data, day_s))
        if sleep_values["score"] is not None or sleep_values["hours"] is not None:
            sleep_values["label"] = day.strftime("%a")
            sleep_values["date"] = day_s
            sleep_series.append(sleep_values)
        hrv_values = _hrv_values(_call(client.get_hrv_data, day_s))
        if hrv_values["value"] is not None or hrv_values["status"]:
            hrv_values["label"] = day.strftime("%a")
            hrv_values["date"] = day_s
            hrv_series.append(hrv_values)

    def average(field):
        values = [_num(day.get(field)) for day in daily_stats]
        values = [value for value in values if value is not None]
        return round(sum(values) / len(values)) if values else None

    w["steps"]["avg7"] = average("totalSteps")
    w["floors"]["avg7"] = average("floorsAscended")
    w["calories"]["avg7"] = average("totalKilocalories")
    w["calories"]["activeAvg7"] = average("activeKilocalories")
    w["calories"]["restingAvg7"] = average("bmrKilocalories")
    intensity = _call(client.get_intensity_minutes_data, ds) or {}
    moderate = _find_num(intensity, ("weeklyModerate", "moderateIntensityMinutes", "moderateMinutes", "weeklyModerateIntensityMinutes")) or 0
    vigorous = _find_num(intensity, ("weeklyVigorous", "vigorousIntensityMinutes", "vigorousMinutes", "weeklyVigorousIntensityMinutes")) or 0
    total = _find_num(intensity, ("weeklyTotal", "totalIntensityMinutes", "totalMinutes", "weeklyIntensityMinutes"))
    w["intensity"] = {
        "moderate": round(moderate), "vigorous": round(vigorous),
        "total": round(total if total is not None else moderate + vigorous * 2),
        "goalWeek": round(_find_num(intensity, ("weekGoal", "intensityMinutesGoal", "weeklyGoal", "goal")) or 350),
    }


    out["sleepSeries"] = sleep_series
    out["hrvSeries"] = hrv_series
    w["sleep"] = sleep_series[-1] if sleep_series else None
    w["hrv"] = hrv_series[-1] if hrv_series else None

    rd = _call(client.get_training_readiness, ds)
    rd0 = rd[0] if isinstance(rd, list) and rd else (rd if isinstance(rd, dict) else None)
    w["readiness"] = {"score": _num((rd0 or {}).get("score")), "level": (rd0 or {}).get("level")} if rd0 and (rd0 or {}).get("score") is not None else None

    # A conservative coaching cue. It is intentionally not medical advice.
    hrv_status = ((w["hrv"] or {}).get("status") or "").lower()
    sleep_score = ((w["sleep"] or {}).get("score"))
    readiness_score = ((w["readiness"] or {}).get("score"))
    if hrv_status in {"unbalanced", "low", "poor"} or (bb := w["bodyBattery"].get("current")) is not None and bb < 25 or sleep_score is not None and sleep_score < 60:
        out["workoutRecommendation"] = {"level": "recover", "label": "Recovery day", "text": "Keep movement easy; prioritise sleep, hydration and mobility."}
    elif readiness_score is not None and readiness_score < 50 or sleep_score is not None and sleep_score < 70:
        out["workoutRecommendation"] = {"level": "easy", "label": "Easy / technique", "text": "An easy aerobic or skills session is a better fit than high intensity today."}
    else:
        out["workoutRecommendation"] = {"level": "ready", "label": "Ready to train", "text": "Recovery signals look supportive of your planned workout; adjust if you feel unusually fatigued."}

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

    # ---- activities -> training categories ----
    raw = _call(client.get_activities, 0, 40) or []
    acts = [_map_activity(a) for a in raw if isinstance(a, dict)]
    out["recent"] = acts[:12]
    history = _history_activities(client, today)
    out["fitnessSeries"] = _training_history(history, today)

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

    # Weekly Relative Effort with an adaptive capacity band. Capacity changes
    # slowly (roughly a six-week time constant), rises after a within/above
    # week, and uses only prior weeks when setting each week's target.
    week_start = today - datetime.timedelta(days=today.weekday())
    weekly_effort = []
    for week_offset in range(51, -1, -1):
        start_day = week_start - datetime.timedelta(days=week_offset * 7)
        end_day = start_day + datetime.timedelta(days=6)
        rows = [row for row in out["fitnessSeries"] if start_day.isoformat() <= row["date"] <= end_day.isoformat()]
        week_activities = [
            {"date": activity["date"], "name": activity["name"], "min": activity["min"],
             "effort": activity.get("effort") or round((activity.get("load") or 0) / 6.3, 1),
             "garminLoad": activity.get("load"), "hr": activity.get("hr")}
            for activity in history
            if start_day.isoformat() <= (activity.get("date") or "") <= min(end_day, today).isoformat()
        ]
        days = []
        for day_offset in range(7):
            day = start_day + datetime.timedelta(days=day_offset)
            row = next((item for item in rows if item["date"] == day.isoformat()), {})
            days.append({"label": day.strftime("%a"), "date": day.isoformat(),
                         "effort": round(row.get("effort") or 0, 1) if day <= today else None})
        weekly_effort.append({
            "label": "This week" if week_offset == 0 else f"{start_day.strftime('%b')} {start_day.day}",
            "start": start_day.isoformat(), "end": end_day.isoformat(),
            "rangeLabel": f"{start_day.strftime('%b')} {start_day.day} – {end_day.strftime('%b')} {end_day.day}",
            "effort": round(sum(row["effort"] for row in rows), 1),
            "garminLoad": round(sum(row["garminLoad"] for row in rows), 1),
            "days": days, "activities": week_activities,
        })
    capacity, seed = None, []
    for index, week in enumerate(weekly_effort):
        effort = week["effort"]
        if capacity is None:
            if effort > 0:
                seed.append(effort)
            week.update({"rangeLow": None, "rangeHigh": None, "state": "building", "capacity": None})
            if len(seed) >= 4:
                capacity = sum(seed[-4:]) / 4.0
            continue
        low_raw, high_raw = capacity * 0.8, capacity * 1.3
        state = "below" if effort < low_raw else "above" if effort > high_raw else "within"
        week.update({"rangeLow": round(low_raw), "rangeHigh": round(high_raw),
                     "state": state, "capacity": round(capacity, 1)})
        if index < len(weekly_effort) - 1:  # current partial week cannot set its own target
            blended = capacity + (effort - capacity) / 6.0
            if state == "within":
                capacity = max(blended, capacity * 1.03)
            elif state == "above":
                capacity = max(blended, capacity * 1.08)
            else:
                capacity = max(0.0, blended)
    visible_weeks = weekly_effort[-12:]
    current = visible_weeks[-1]
    out["relativeEffort"] = {
        "weeks": visible_weeks, "current": current["effort"],
        "garminLoad": current["garminLoad"],
        "rangeLow": current["rangeLow"], "rangeHigh": current["rangeHigh"], "state": current["state"],
        "baseline": current["capacity"], "days": current["days"],
        "activities": current["activities"],
        "formula": "HR-zone minutes × 1, 2, 3, 5, 7 ÷ 10, then sport-calibrated (run 3.5×; cycling 2×; gym/SkiErg 1.7×). Garmin Load ÷ 6.3 only if zone time is unavailable.",
        "rangeModel": "The weekly band is 80–130% of adaptive capacity. Capacity uses a six-week response and rises after within/above weeks.",
    }

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

    gym_acts = [a for a in acts if a["sport"] == "other"]
    type_counts = {}
    for activity in gym_acts:
        label = activity.get("name") or (activity.get("typeKey") or "Workout").replace("_", " ").title()
        type_counts[label] = type_counts.get(label, 0) + 1
    out["workouts"] = {
        "hasData": bool(gym_acts),
        "week": _agg([a for a in gym_acts if within(a["date"], 7)]),
        "month": _agg([a for a in gym_acts if within(a["date"], 30)]),
        "last": gym_acts[0] if gym_acts else None,
        "types": [{"name": name, "count": count} for name, count in sorted(type_counts.items(), key=lambda item: -item[1])[:4]],
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

    async def body_measurements(request):
        if request.method == "GET":
            return JSONResponse(_body_measurements())
        try:
            _append_body_measurement(await request.json())
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(_body_measurements(), status_code=201)

    async def injury_measurements(request):
        if request.method == "GET":
            return JSONResponse(_injury_measurements())
        try:
            _append_injury_measurement(await request.json())
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(_injury_measurements(), status_code=201)

    async def recommendation(request):
        """Serve today's saved advice, unless the athlete explicitly asks to refresh it."""
        try:
            request_data = await request.json()
            dashboard = request_data.get("dashboard") if isinstance(request_data, dict) and "dashboard" in request_data else request_data
            if not isinstance(dashboard, dict) or not isinstance(dashboard.get("wellness"), dict):
                raise ValueError("a dashboard data snapshot is required")
            date = dashboard.get("date")
            if not isinstance(date, str) or len(date) != 10:
                raise ValueError("a valid dashboard date is required")
            refresh = bool(request_data.get("refresh")) if isinstance(request_data, dict) else False
            if not refresh:
                saved = _cached_recommendation(date)
                if saved:
                    saved["cached"] = True
                    return JSONResponse(saved)
            saved = _save_recommendation(date, _openai_recommendation(dashboard))
            saved["cached"] = False
            return JSONResponse(saved)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        except requests.RequestException:
            return JSONResponse({"error": "The recommendation service is temporarily unavailable."}, status_code=503)

    async def favicon(_request):
        from starlette.responses import Response
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
               "<text y='.9em' font-size='90'>⌚</text></svg>")
        return Response(svg, media_type="image/svg+xml",
                        headers={"cache-control": "public, max-age=86400"})

    asgi_app.router.routes.append(Route("/api/dashboard", api, methods=["GET"]))
    asgi_app.router.routes.append(Route("/api/body-measurements", body_measurements, methods=["GET", "POST"]))
    asgi_app.router.routes.append(Route("/api/injury-measurements", injury_measurements, methods=["GET", "POST"]))
    asgi_app.router.routes.append(Route("/api/recommendation", recommendation, methods=["POST"]))
    asgi_app.router.routes.append(Route("/dashboard", page, methods=["GET"]))
    asgi_app.router.routes.append(Route("/favicon.ico", favicon, methods=["GET"]))


# --------------------------------------------------------------------------- #
# The single-page app (fetches /api/dashboard and renders)
# --------------------------------------------------------------------------- #

PAGE_HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%E2%8C%9A%3C/text%3E%3C/svg%3E">
<title>Training Dashboard</title>
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
.read .em{font-size:22px}.read p{margin:0;font-size:14.5px;flex:1}.read b{color:var(--text)}.read button{align-self:center;white-space:nowrap}.read button:disabled{opacity:.65;cursor:wait}
.grid{display:grid;gap:14px}
.hero{grid-template-columns:1.5fr 1fr 1fr}.topmetrics{grid-template-columns:repeat(3,1fr);margin-top:14px}.bodygrid{grid-template-columns:repeat(4,1fr);margin-top:14px}
.tri{grid-template-columns:repeat(3,1fr);margin-top:14px}
.stats{grid-template-columns:repeat(4,1fr);margin-top:14px}
@media(max-width:820px){.hero,.tri,.topmetrics{grid-template-columns:1fr 1fr}.stats,.bodygrid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:520px){.hero,.tri,.topmetrics,.bodygrid{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:16px;min-width:0}
.card .label{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}
.big{font-size:clamp(24px,4vw,32px);font-weight:750;line-height:1.05}
.unit{font-size:14px;font-weight:600;color:var(--muted);margin-left:3px}
.meta{font-size:12.5px;color:var(--muted);margin-top:4px}
.calbar{display:flex;overflow:hidden;height:10px;border-radius:999px;background:var(--surface-2);margin:13px 0 8px}.calbar i{display:block;height:100%}.calbar .active{background:var(--accent)}.calbar .rest{background:var(--accent-2)}.callegend{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:11.5px}.callegend b{color:var(--text)}
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
.vo2wrap{display:flex;align-items:center;gap:10px}.vo2gauge{width:150px;height:106px;flex:none}.vo2copy .n{font-size:29px;font-weight:780}.vo2copy .d{font-size:12px;color:var(--muted)}.vo2legend{display:flex;gap:4px;flex-wrap:wrap;margin-top:7px;font-size:10px;color:var(--muted)}.vo2legend i{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:3px}.bodydelta{margin-top:7px;font-size:12.5px;font-weight:700}.bodydelta.good{color:var(--good)}.bodydelta.low{color:var(--low)}.bodydate{font-size:11.5px;color:var(--muted);margin-top:4px}
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
.trendchart{width:100%;height:112px;display:block;margin-top:7px}.trendaxis{display:flex;justify-content:space-between;color:var(--faint);font-size:10.5px;margin-top:2px}
.loadchart,.effortdaily{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}.loadchart{width:100%;height:190px;display:block;margin-top:8px}.loadchart text,.effortdaily text{font-family:inherit!important;font-size:19px!important;font-weight:600;letter-spacing:.01em}.primarychart{height:300px;margin-top:12px}.keycharts{display:grid;grid-template-columns:1fr;gap:16px}.rangeband{fill:color-mix(in srgb,var(--accent) 18%,transparent)}.charttabs{display:flex;gap:6px;flex-wrap:wrap}.charttabs button{border:1px solid var(--border);background:var(--surface-2);color:var(--muted);border-radius:999px;padding:5px 9px;font-size:11px;font-weight:700;cursor:pointer}.charttabs button.active{background:var(--accent);border-color:var(--accent);color:#fff}.metricnote{font-size:12px;color:var(--muted);margin-top:8px}
.effortdetail{margin-top:14px;padding-top:13px;border-top:1px solid var(--border)}.effortdaily{width:100%;height:120px;display:block;margin-top:4px;cursor:crosshair}.effortlist{display:grid;gap:7px;margin-top:12px}.effortrow{display:flex;justify-content:space-between;gap:14px;padding:9px 11px;border-radius:10px;background:var(--surface-2);font-size:12.5px;color:var(--muted)}.effortrow b{color:var(--text)}.effortrow .score{white-space:nowrap;color:var(--accent);font-weight:750}.fitsummary{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-top:12px}.fitsummary .change{font-size:28px;font-weight:800}.fitsummary .up{color:var(--good)}.fitsummary .period{width:100%;color:var(--muted);font-size:12.5px}
.entry-actions{display:flex;justify-content:flex-end;margin-top:14px}.entry-form{display:none;margin-top:14px;padding:15px;border:1px solid var(--border);border-radius:12px;background:var(--surface-2)}.entry-form.open{display:block}.entry-fields{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.injury-fields{grid-template-columns:repeat(3,1fr)}.entry-fields label{display:grid;gap:4px;font-size:12px;font-weight:700;color:var(--muted)}.entry-fields input{width:100%;border:1px solid var(--border);border-radius:9px;padding:9px;background:var(--surface);color:var(--text);font:inherit}.entry-submit{margin-top:12px;border:0;border-radius:999px;background:var(--accent);color:white;padding:9px 14px;font-size:13px;font-weight:700;cursor:pointer}.entry-status{margin:9px 0 0;font-size:12px;color:var(--muted)}.injurychart{width:100%;height:300px;display:block;margin-top:10px}.painlegend{display:flex;flex-wrap:wrap;gap:7px 14px;margin-top:12px;font-size:12px;color:var(--muted)}.painlegend span{display:flex;align-items:center;gap:5px}.painlegend i{width:9px;height:9px;border-radius:50%;display:inline-block}.pain-scale{margin-top:12px;padding-top:10px;border-top:1px solid var(--border);font-size:12px;color:var(--muted)}
@media(max-width:640px){.entry-fields,.injury-fields{grid-template-columns:1fr 1fr}.injurychart{height:240px}.primarychart{height:250px}.loadchart text,.effortdaily text{font-size:21px!important}}@media(max-width:420px){.entry-fields,.injury-fields{grid-template-columns:1fr}}
.svg-tip{position:fixed;z-index:20;pointer-events:none;background:var(--text);color:var(--surface);padding:7px 9px;border-radius:8px;font-size:12px;line-height:1.35;box-shadow:var(--shadow);transform:translate(12px,-115%);white-space:nowrap}.svg-tip[hidden]{display:none}.bbchart,.trendchart,.loadchart{cursor:crosshair}
.hrvdots{display:flex;align-items:flex-end;justify-content:space-between;gap:8px;height:96px;padding:8px 3px 0}.hrvday{flex:1;min-width:0;text-align:center;color:var(--faint);font-size:10.5px}.hrvdot{display:block;width:15px;height:15px;border-radius:50%;margin:0 auto 7px;background:var(--faint);box-shadow:0 0 0 4px color-mix(in srgb,var(--faint) 12%,transparent)}.hrvdot.good{background:var(--good);box-shadow:0 0 0 4px color-mix(in srgb,var(--good) 14%,transparent)}.hrvdot.warn{background:var(--warn);box-shadow:0 0 0 4px color-mix(in srgb,var(--warn) 14%,transparent)}.hrvdot.low{background:var(--low);box-shadow:0 0 0 4px color-mix(in srgb,var(--low) 14%,transparent)}
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
function chartTip(svg,points,format){
  if(!svg||!points||!points.length)return;var tip=document.getElementById("svg-tip");if(!tip){tip=el("div","svg-tip");tip.id="svg-tip";tip.hidden=true;document.body.appendChild(tip);}
  svg.onpointermove=function(e){var r=svg.getBoundingClientRect(),i=Math.max(0,Math.min(points.length-1,Math.round((e.clientX-r.left)/r.width*(points.length-1))));tip.innerHTML=format(points[i]);tip.style.left=e.clientX+"px";tip.style.top=e.clientY+"px";tip.hidden=false;};svg.onpointerleave=function(){tip.hidden=true;};
}

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

function WORKOUTS(d){
  var s=d.workouts||{},c=el("div","card sport walk");
  var head='<div class="top"><span class="ico">🏋️</span><div><h3>Workouts</h3><div class="d">HIIT · rowing · SkiErg · elliptical</div></div></div>';
  if(!s.hasData){c.innerHTML=head+'<div class="empty"><span class="pill mute">Ready</span><span class="msg">Log a gym, cardio or indoor-machine activity in Garmin and it will appear here.</span></div>';return c;}
  var wk=s.week||{},last=s.last||{},types=(s.types||[]).map(function(x){return x.name+' · '+x.count;}).join(' · ');
  c.innerHTML=head+'<div class="tstat"><div class="t"><div class="n">'+n(wk.sessions)+'</div><div class="l">sessions · 7d</div></div><div class="t"><div class="n">'+n(wk.min)+'</div><div class="l">minutes · 7d</div></div><div class="t"><div class="n">'+comma(wk.cal)+'</div><div class="l">kcal · 7d</div></div></div><div class="last">Last: <b>'+n(last.name)+'</b> · '+n(last.min)+' min'+(last.hr?' · '+last.hr+' bpm':'')+'<br><span style="color:var(--faint)">'+types+'</span></div>';
  return c;
}

function statCard(label,pill,pcls,big,unit,meta){
  return '<div class="card"><div class="label"><p class="eyebrow">'+label+'</p>'+
    (pill?'<span class="pill '+pcls+'">'+pill+'</span>':'')+'</div>'+
    '<div class="big">'+big+(unit?'<span class="unit">'+unit+'</span>':'')+'</div>'+
    '<div class="meta">'+meta+'</div></div>';
}
function calorieCard(cal){
  cal=cal||{};var total=cal.total||0,active=cal.active||0,rest=cal.bmr||0;
  var activePct=total?active/total*100:0,restPct=total?rest/total*100:0;
  return '<div class="card"><div class="label"><p class="eyebrow">Calories</p><span class="pill mute">Today</span></div>'+
    '<div class="big">'+comma(total)+'<span class="unit">kcal</span></div>'+
    '<div class="calbar" title="Active '+comma(active)+' kcal · Resting '+comma(rest)+' kcal"><i class="active" style="width:'+activePct+'%"></i><i class="rest" style="width:'+restPct+'%"></i></div>'+
    '<div class="callegend"><span><b>'+comma(active)+'</b> active</span><span><b>'+comma(rest)+'</b> resting</span></div>'+
    '<div class="meta">7-day avg '+comma(cal.avg7)+' kcal / day</div></div>';
}
function bodyMetricCard(label,metric,unit,lowerIsBetter){
  metric=metric||{};var delta=metric.delta,cls=delta==null||delta===0?"mute":((lowerIsBetter?delta<0:delta>0)?"good":"low"),change=delta==null?"No previous measurement":(delta===0?"No change":(delta>0?"+":"")+delta.toFixed(1)+unit+" vs previous");
  return '<div class="card"><div class="label"><p class="eyebrow">'+label+'</p><span class="pill mute">Basic-Fit</span></div><div class="big">'+n(metric.value,function(v){return Number(v).toFixed(1);})+'<span class="unit">'+unit+'</span></div><div class="bodydate">'+n(metric.date)+'</div><div class="bodydelta '+cls+'">'+change+'</div></div>';
}
function toggleEntry(id){document.getElementById(id).classList.toggle("open");}
function saveEntry(endpoint,formId,statusId){
  var form=document.getElementById(formId),status=document.getElementById(statusId),data={};
  new FormData(form).forEach(function(value,key){data[key]=value;});
  status.textContent="Saving…";
  fetch(endpoint,{method:"POST",headers:{Authorization:"Bearer "+TOKEN,"Content-Type":"application/json"},body:JSON.stringify(data)})
    .then(function(r){return r.json().then(function(body){if(!r.ok)throw new Error(body.error||"Could not save entry");return body;});})
    .then(function(){status.textContent="Saved. Refreshing dashboard…";load();})
    .catch(function(error){status.textContent=error.message;});
}
function drawInjuries(records){
  var svg=document.getElementById("injuryc");if(!svg)return;svg.innerHTML="";records=records||[];if(!records.length)return;
  var W=1000,H=300,pL=44,pR=18,pT=16,pB=42,plotW=W-pL-pR,plotH=H-pT-pB;
  function X(i){return pL+i*plotW/Math.max(1,records.length-1);}function Y(v){return pT+(10-v)/10*plotH;}
  [0,2,4,6,8,10].forEach(function(v){var y=Y(v),line=document.createElementNS(ns,"line"),text=document.createElementNS(ns,"text");line.setAttribute("x1",pL);line.setAttribute("x2",W-pR);line.setAttribute("y1",y);line.setAttribute("y2",y);line.setAttribute("stroke",css("--border"));svg.appendChild(line);text.setAttribute("x",pL-10);text.setAttribute("y",y+5);text.setAttribute("text-anchor","end");text.setAttribute("font-size",15);text.setAttribute("fill",css("--faint"));text.textContent=v;svg.appendChild(text);});
  [0,7,14,21,29].filter(function(i){return i<records.length;}).forEach(function(i){var date=new Date(records[i].date+"T00:00:00"),text=document.createElementNS(ns,"text");text.setAttribute("x",X(i));text.setAttribute("y",H-12);text.setAttribute("text-anchor","middle");text.setAttribute("font-size",14);text.setAttribute("fill",css("--faint"));text.textContent=(date.getMonth()+1)+"/"+date.getDate();svg.appendChild(text);});
  var series=[{key:"left_big_toe_strain",color:"#e5484d"},{key:"left_foot_plantar_fasciitis",color:"#f08c00"},{key:"right_knee_patellar_tendon",color:"#1683ff"}];
  series.forEach(function(s){var points=records.map(function(row,i){return row[s.key]==null?null:{x:X(i),y:Y(row[s.key])};}).filter(Boolean);if(!points.length)return;var path=document.createElementNS(ns,"path");path.setAttribute("d",points.map(function(point,i){return(i?"L":"M")+point.x.toFixed(1)+" "+point.y.toFixed(1);}).join(" "));path.setAttribute("fill","none");path.setAttribute("stroke",s.color);path.setAttribute("stroke-width",4);path.setAttribute("stroke-linecap","round");path.setAttribute("stroke-linejoin","round");svg.appendChild(path);points.forEach(function(point){var dot=document.createElementNS(ns,"circle");dot.setAttribute("cx",point.x);dot.setAttribute("cy",point.y);dot.setAttribute("r",6);dot.setAttribute("fill",css("--surface"));dot.setAttribute("stroke",s.color);dot.setAttribute("stroke-width",3);svg.appendChild(dot);});});
  chartTip(svg,records,function(row){var fmt=function(v){return v==null?"—":v+" / 10";};return "<b>"+new Date(row.date+"T00:00:00").toLocaleDateString(undefined,{month:"short",day:"numeric"})+"</b><br>Left big toe: "+fmt(row.left_big_toe_strain)+"<br>Left plantar fascia: "+fmt(row.left_foot_plantar_fasciitis)+"<br>Right patellar tendon: "+fmt(row.right_knee_patellar_tendon);});
}

function loadPersonalRecommendation(d,refresh){
  var target=document.getElementById("personal-recommendation");
  var button=document.getElementById("refresh-recommendation");
  if(!target)return;
  if(refresh){target.textContent="Updating your recommendation…";if(button){button.disabled=true;button.textContent="Updating…";}}
  var snapshot={date:d.date,wellness:d.wellness,body:d.body,injuries:d.injuries,recent:d.recent,relativeEffort:d.relativeEffort};
  fetch("/api/recommendation",{method:"POST",headers:{Authorization:"Bearer "+TOKEN,"Content-Type":"application/json"},body:JSON.stringify({dashboard:snapshot,refresh:!!refresh})})
    .then(function(r){return r.json().then(function(body){if(!r.ok)throw new Error(body.error||"Could not get a recommendation");return body;});})
    .then(function(result){target.textContent=result.text;target.parentElement.classList.add("ai-ready");})
    .catch(function(){if(refresh)target.textContent="Could not update the recommendation. Please try again.";})
    .finally(function(){if(button){button.disabled=false;button.textContent="Refresh advice";}});
}

function render(d){
  var app=document.getElementById("app");
  app.innerHTML="";
  var w=d.wellness||{};
  var bb=w.bodyBattery||{},steps=w.steps||{},rhr=w.restingHr||{},stress=w.stress||{};

  // header
  var head=el("header","top");
  head.innerHTML='<div><p class="eyebrow">Training Dashboard</p><h1>'+n(d.name)+'</h1>'+
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
  if(d.workoutRecommendation) bits.push('<b>'+d.workoutRecommendation.label+'</b> — '+d.workoutRecommendation.text);
  if(bits.length) msg=bits.join(" · ")+".";
  var read=el("div","read");
  read.innerHTML='<span class="em">'+(bbv!=null&&bbv<25?"🔋":"🏅")+'</span><p id="personal-recommendation">'+msg+'</p><button class="rf" id="refresh-recommendation" title="Use the latest dashboard data to request fresh advice">Refresh advice</button>';
  app.appendChild(read);
  loadPersonalRecommendation(d);
  document.getElementById("refresh-recommendation").addEventListener("click",function(){loadPersonalRecommendation(d,true);});

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
    '<div class="d" style="margin-top:6px">7-day avg '+comma(steps.avg7)+' · '+n(w.distanceKm)+' km</div></div></div>';
  // recovery / sleep card
  var rec=el("div","card");
  if(w.sleep||w.hrv||w.readiness){
    var rows="";
    if(w.readiness) rows+='<div class="row"><span class="k">Readiness</span><span class="bar"><i style="width:'+(w.readiness.score||0)+'%"></i></span><span class="v">'+n(w.readiness.score)+'</span></div>';
    if(w.sleep) rows+='<div class="row"><span class="k">Sleep</span><span class="bar"><i style="width:'+(w.sleep.score||Math.min(100,(w.sleep.hours||0)/8*100))+'%"></i></span><span class="v">'+n(w.sleep.score)+' / '+n(w.sleep.hours)+'h</span></div>';
    if(w.hrv) rows+='<div class="row"><span class="k">HRV</span><span class="bar"><i style="width:60%"></i></span><span class="v">'+n(w.hrv.value)+' '+cap(w.hrv.status)+'</span></div>';
    rec.innerHTML='<div class="label"><p class="eyebrow">Recovery</p><span class="pill '+(d.workoutRecommendation&&d.workoutRecommendation.level==="recover"?"low":d.workoutRecommendation&&d.workoutRecommendation.level==="easy"?"warn":"good")+'">'+(d.workoutRecommendation?d.workoutRecommendation.label:"")+'</span></div>'+rows;
  } else {
    rec.innerHTML='<div class="label"><p class="eyebrow">Recovery</p><span class="pill mute">No wear</span></div>'+
      '<div class="meta" style="margin-top:8px">Sleep, HRV & readiness need overnight wrist wear — none recorded for this night.</div>';
  }
  hero.appendChild(rec);
  var calTop=el("div",null,calorieCard(w.calories));
  hero.appendChild(calTop.firstChild);
  app.appendChild(hero);

  // daily movement and aerobic capacity
  var topmetrics=el("div","grid topmetrics");
  topmetrics.appendChild(stepCard);
  var floors=w.floors||{},floorPct=(floors.value&&floors.goal)?Math.min(1,floors.value/floors.goal):0;
  var floorCard=el("div","card");
  floorCard.innerHTML='<div class="label"><p class="eyebrow">Floors</p><span class="pill '+(floorPct>=1?"good":"warn")+'">'+Math.round(floorPct*100)+'%</span></div><div class="ringwrap"><svg class="ring" id="floor-ring" viewBox="0 0 120 120"></svg><div class="ringlabel"><div class="n">'+comma(floors.value)+'</div><div class="d">of '+comma(floors.goal)+' goal</div><div class="d" style="margin-top:6px">7-day avg '+comma(floors.avg7)+'</div></div></div>';
  topmetrics.appendChild(floorCard);
  var vo2Card=el("div","card"),vo2=n(w.vo2maxRun),vo2Rating=vo2RatingFor(w.vo2maxRun,w.vo2RatingAge),vo2Age=w.vo2RatingAge?" · male "+w.vo2RatingAge:" · male standards";
  vo2Card.innerHTML='<div class="label"><p class="eyebrow">VO₂ Max · Run</p><span class="pill" id="vo2-label" style="background:'+vo2Rating.color+'20;color:'+vo2Rating.color+'">'+vo2Rating.label+'</span></div><div class="vo2wrap"><svg class="vo2gauge" id="vo2g" viewBox="0 0 240 160"></svg><div class="vo2copy"><div class="n">'+vo2+'</div><div class="d">ml/kg/min</div><div class="d" style="margin-top:7px">as of '+n(w.vo2maxRunDate)+vo2Age+'</div></div></div><div class="vo2legend"><span><i style="background:#ef4444"></i>Poor</span><span><i style="background:#f97316"></i>Fair</span><span><i style="background:#12b886"></i>Good</span><span><i style="background:#1683ff"></i>Excellent</span><span><i style="background:#7c4dff"></i>Superior</span></div>';
  topmetrics.appendChild(vo2Card);
  app.appendChild(topmetrics);

  // wellness stats
  app.appendChild(sec("Today & this week"));
  var tl=w.trainingLoad||{};
  var stats=el("div","grid stats");
  stats.innerHTML=
    statCard("Resting HR", rhr.value!=null&&rhr.avg7!=null&&rhr.value<=rhr.avg7?"Good":"", "good", n(rhr.value),"bpm","7-day avg "+n(rhr.avg7)+" · range "+n(rhr.min)+"–"+n(rhr.max))+
    statCard("Stress", stress.avg!=null?(stress.avg<26?"Low":stress.avg<51?"Medium":"High"):"", stress.avg!=null&&stress.avg>=51?"warn":"mute", n(stress.avg),"avg","Peak "+n(stress.max))+
    statCard("Training Load", tl.status?cap(tl.status):"", tl.status=="OPTIMAL"?"good":"mute", n(tl.acwr),"ACWR","Acute "+n(tl.acute)+" · chronic "+n(tl.chronic))+
    statCard("Intensity · week", "", "mute", comma((w.intensity||{}).total)+" / "+comma((w.intensity||{}).goalWeek),"min", comma((w.intensity||{}).moderate)+" moderate · "+comma((w.intensity||{}).vigorous)+" vigorous");
  app.appendChild(stats);

  // training focus
  app.appendChild(sec("Training & workouts"));
  var tri=el("div","grid tri");
  tri.appendChild(WORKOUTS(d));
  tri.appendChild(SPORT({key:"bike",name:"Bike",emoji:"🚴",tag:"road + indoor"},d));
  tri.appendChild(SPORT({key:"run",name:"Run",emoji:"🏃",tag:"road + track"},d));
  app.appendChild(tri);

  // Strava-inspired effort + fitness views. These deliberately remain separate
  // from Garmin's Training Load because the two products use different models.
  app.appendChild(sec("Training response"));
  var response=el("div","keycharts"),re=d.relativeEffort||{};
  var effortCard=el("div","card");
  effortCard.innerHTML='<div class="label"><p class="eyebrow">Relative effort · weekly</p><span class="pill mute" id="effort-state">Select a week</span></div><div class="big"><span id="effort-value">'+n(re.current)+'</span><span class="unit">points</span></div><div class="meta" id="effort-meta"></div><svg class="loadchart primarychart" id="effortc" viewBox="0 0 1000 360" preserveAspectRatio="none"></svg><div class="effortdetail"><div class="label"><p class="eyebrow" id="effort-week">Daily effort</p><p class="eyebrow">Click another week above</p></div><svg class="effortdaily" id="effortdaily" viewBox="0 0 1000 180" preserveAspectRatio="none"></svg><div class="effortlist" id="effort-list"></div></div><div class="metricnote">'+n(re.rangeModel)+' '+n(re.formula)+'</div>';
  response.appendChild(effortCard);
  var fitnessCard=el("div","card");
  var fs=d.fitnessSeries||[],latest=fs[fs.length-1]||{};
  fitnessCard.innerHTML='<div class="label"><p class="eyebrow">Fitness level · Garmin-load model</p><span class="pill good" id="fit-value">'+n(latest.fitness)+' index</span></div><div class="charttabs" id="fit-tabs"><button data-days="30" class="active">1 month</button><button data-days="90">3 months</button><button data-days="180">6 months</button><button data-days="365">1 year</button><button data-days="731">2 years</button></div><div class="fitsummary" id="fit-summary"></div><svg class="loadchart primarychart" id="fitnessc" viewBox="0 0 1000 360" preserveAspectRatio="none"></svg><div class="metricnote">Click any point to compare it with the first day of the selected period. With no selection, today is used. Uses Garmin’s EPOC-based Training Load.</div>';
  response.appendChild(fitnessCard);app.appendChild(response);

  // sleep + HRV history
  app.appendChild(sec("Sleep & HRV · 7 days"));
  var recoveryTrends=el("div","grid twogrid");
  var sleepCard=el("div","card");
  sleepCard.innerHTML='<div class="label"><p class="eyebrow">Sleep score</p><p class="eyebrow">Latest '+n((w.sleep||{}).score)+' · '+n((w.sleep||{}).hours)+' h</p></div><svg class="trendchart" id="sleepc" viewBox="0 0 1000 220" preserveAspectRatio="none"></svg><div class="trendaxis" id="sleepa"></div>';
  recoveryTrends.appendChild(sleepCard);
  var hrvCard=el("div","card"),hrvs=d.hrvSeries||[];
  var dots=hrvs.map(function(x){var s=(x.status||"").toLowerCase(),cl=s.indexOf("balanc")>=0?"good":s.indexOf("unbalanc")>=0?"warn":s?"low":"";return '<div class="hrvday"><span class="hrvdot '+cl+'" title="'+cap(x.status)+' · '+n(x.value)+' ms"></span>'+x.label+'</div>';}).join("");
  hrvCard.innerHTML='<div class="label"><p class="eyebrow">HRV status</p><span class="pill '+(((w.hrv||{}).status||"").toLowerCase().indexOf("balanc")>=0?"good":"warn")+'">'+cap((w.hrv||{}).status)+'</span></div><div class="hrvdots">'+(dots||'<div class="meta">No HRV data recorded.</div>')+'</div><div class="meta">Green balanced · orange unbalanced · red low</div>';
  recoveryTrends.appendChild(hrvCard);app.appendChild(recoveryTrends);

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

  // body composition from persistent Basic-Fit measurement history
  app.appendChild(sec("Body"));
  var body=el("div","grid bodygrid"),bm=(d.body||{}).metrics||{};
  body.innerHTML=bodyMetricCard("Weight",bm.weight_kg," kg",true)+bodyMetricCard("Fat mass",bm.fat_pct,"%",true)+bodyMetricCard("Muscle mass",bm.muscle_pct,"%",false)+bodyMetricCard("Body water",bm.body_water_pct,"%",false);
  app.appendChild(body);
  var bodyEntry=el("div",null,'<div class="entry-actions"><button class="rf" id="body-entry-toggle">Add body measurement</button></div><form class="entry-form" id="body-entry"><div class="entry-fields"><label>Weight (kg)<input name="weight_kg" type="number" min="1" step="0.1" required></label><label>Fat mass (%)<input name="fat_pct" type="number" min="0" max="100" step="0.1" required></label><label>Muscle mass (%)<input name="muscle_pct" type="number" min="0" max="100" step="0.1" required></label><label>Body water (%)<input name="body_water_pct" type="number" min="0" max="100" step="0.1" required></label></div><button class="entry-submit" type="submit">Save measurement</button><p class="entry-status" id="body-entry-status"></p></form>');
  app.appendChild(bodyEntry);

  // daily injury tracking uses the standard 0–10 Numeric Rating Scale (NRS-11)
  app.appendChild(sec("Injuries"));
  var injury=el("div","card");
  injury.innerHTML='<div class="label"><p class="eyebrow">Pain trend · last 30 days</p><span class="pill mute">0–10 NRS</span></div><svg class="injurychart" id="injuryc" viewBox="0 0 1000 300" preserveAspectRatio="none"></svg><div class="painlegend"><span><i style="background:#e5484d"></i>Left big toe strain</span><span><i style="background:#f08c00"></i>Left foot plantar fasciitis</span><span><i style="background:#1683ff"></i>Right knee patellar tendon</span></div><div class="pain-scale"><b>Numeric Rating Scale (NRS-11):</b> 0 = no pain · 1–3 = mild · 4–6 = moderate · 7–10 = severe / worst pain imaginable.</div><div class="entry-actions"><button class="rf" id="injury-entry-toggle">Add today’s pain scores</button></div><form class="entry-form" id="injury-entry"><div class="entry-fields injury-fields"><label>Left big toe strain (0–10)<input name="left_big_toe_strain" type="number" min="0" max="10" step="1" required></label><label>Left plantar fasciitis (0–10)<input name="left_foot_plantar_fasciitis" type="number" min="0" max="10" step="1" required></label><label>Right patellar tendon (0–10)<input name="right_knee_patellar_tendon" type="number" min="0" max="10" step="1" required></label></div><button class="entry-submit" type="submit">Save today’s scores</button><p class="entry-status" id="injury-entry-status"></p></form>';
  app.appendChild(injury);

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

  drawRing("ring",steps.value,steps.goal,css("--accent"));
  drawRing("floor-ring",floors.value,floors.goal,css("--accent-2"));
  drawVo2(w.vo2maxRun,w.vo2RatingAge);
  drawBB(d.bodyBatterySeries||[]);
  drawTL(d.trainingLoadTrend||[]);
  drawTrend("sleepc","sleepa",d.sleepSeries||[],"score");
  drawEffort(re);
  drawFitness(fs,30);
  drawInjuries((d.injuries||{}).records||[]);
  document.querySelectorAll("#fit-tabs button").forEach(function(button){button.addEventListener("click",function(){document.querySelectorAll("#fit-tabs button").forEach(function(x){x.classList.remove("active")});button.classList.add("active");drawFitness(fs,Number(button.dataset.days),null);});});
  document.getElementById("rf").addEventListener("click",load);
  document.getElementById("body-entry-toggle").addEventListener("click",function(){toggleEntry("body-entry");});
  document.getElementById("injury-entry-toggle").addEventListener("click",function(){toggleEntry("injury-entry");});
  document.getElementById("body-entry").addEventListener("submit",function(event){event.preventDefault();var data={timestamp:new Date().toISOString()};new FormData(event.target).forEach(function(value,key){data[key]=value;});var status=document.getElementById("body-entry-status");status.textContent="Saving…";fetch("/api/body-measurements",{method:"POST",headers:{Authorization:"Bearer "+TOKEN,"Content-Type":"application/json"},body:JSON.stringify(data)}).then(function(r){return r.json().then(function(body){if(!r.ok)throw new Error(body.error||"Could not save measurement");return body;});}).then(function(){status.textContent="Saved. Refreshing dashboard…";load();}).catch(function(error){status.textContent=error.message;});});
  document.getElementById("injury-entry").addEventListener("submit",function(event){event.preventDefault();saveEntry("/api/injury-measurements","injury-entry","injury-entry-status");});
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
    var title=document.createElementNS(ns,"title");title.textContent=x.label+": "+Math.round(x.load)+" Garmin Training Load";r.appendChild(title);
    if(x.load<=0){r.setAttribute("y",H-pB-3);r.setAttribute("height",3);}
    svg.appendChild(r);
    if(x.load>0){var t=document.createElementNS(ns,"text");t.setAttribute("x",bx+bw/2);t.setAttribute("y",by-7);
      t.setAttribute("text-anchor","middle");t.setAttribute("fill",css("--text"));t.setAttribute("font-size",20);t.setAttribute("font-weight",700);
      t.textContent=Math.round(x.load);svg.appendChild(t);}
    var dl=document.createElementNS(ns,"text");dl.setAttribute("x",bx+bw/2);dl.setAttribute("y",H-9);
    dl.setAttribute("text-anchor","middle");dl.setAttribute("fill",css("--faint"));dl.setAttribute("font-size",18);
    dl.textContent=x.label;svg.appendChild(dl);
  });
  chartTip(svg,trend,function(x){return '<b>'+x.label+'</b><br>'+Math.round(x.load)+' Garmin Training Load';});
}

function drawTrend(svgId,axisId,series,key){
  var svg=document.getElementById(svgId);if(!svg||!series.length)return;
  var W=1000,H=220,p=24,values=series.map(function(x){return x[key];}).filter(function(v){return v!=null;});if(!values.length)return;
  var lo=Math.max(0,Math.min.apply(null,values)-10),hi=Math.min(100,Math.max.apply(null,values)+10),range=Math.max(1,hi-lo);
  function X(i){return series.length===1?W/2:i/(series.length-1)*(W-p*2)+p;}function Y(v){return p+(hi-v)/range*(H-p*2);}
  [lo,(lo+hi)/2,hi].forEach(function(v){var l=document.createElementNS(ns,"line");l.setAttribute("x1",p);l.setAttribute("x2",W-p);l.setAttribute("y1",Y(v));l.setAttribute("y2",Y(v));l.setAttribute("stroke",css("--border"));l.setAttribute("stroke-width",1);svg.appendChild(l);var t=document.createElementNS(ns,"text");t.setAttribute("x",1);t.setAttribute("y",Y(v)+4);t.setAttribute("font-size",16);t.setAttribute("fill",css("--faint"));t.textContent=Math.round(v);svg.appendChild(t);});
  var pts=[];series.forEach(function(x,i){if(x[key]!=null)pts.push([X(i),Y(x[key]),x]);});if(!pts.length)return;
  var line=pts.map(function(x,i){return (i?"L":"M")+x[0].toFixed(1)+" "+x[1].toFixed(1);}).join(" ");
  var path=document.createElementNS(ns,"path");path.setAttribute("d",line);path.setAttribute("fill","none");path.setAttribute("stroke",css("--accent-2"));path.setAttribute("stroke-width",3);path.setAttribute("stroke-linecap","round");svg.appendChild(path);
  pts.forEach(function(x){var dot=document.createElementNS(ns,"circle");dot.setAttribute("cx",x[0]);dot.setAttribute("cy",x[1]);dot.setAttribute("r",5);dot.setAttribute("fill",css("--accent-2"));dot.setAttribute("stroke",css("--surface"));dot.setAttribute("stroke-width",3);svg.appendChild(dot);});
  document.getElementById(axisId).innerHTML=series.map(function(x){return '<span>'+x.label+'</span>';}).join("");chartTip(svg,series,function(x){return '<b>'+x.label+'</b><br>Sleep score: '+n(x.score)+'<br>Sleep: '+n(x.hours)+' h';});
}

function drawEffort(data,selectedIndex){
  var svg=document.getElementById("effortc"),weeks=data.weeks||[];if(!svg||!weeks.length)return;svg.innerHTML="";var selected=selectedIndex==null?weeks.length-1:Math.max(0,Math.min(weeks.length-1,selectedIndex));
  var W=1000,H=360,pL=56,pR=22,pT=28,pB=48,max=Math.max.apply(null,weeks.map(function(x){return Math.max(x.effort||0,x.rangeHigh||0);}).concat([1]))*1.14;
  function X(i){return pL+i*(W-pL-pR)/Math.max(1,weeks.length-1)}function Y(v){return pT+(max-v)/max*(H-pT-pB)}
  [0,max/2,max].forEach(function(v){var y=Y(v),l=document.createElementNS(ns,"line"),t=document.createElementNS(ns,"text");l.setAttribute("x1",pL);l.setAttribute("x2",W-pR);l.setAttribute("y1",y);l.setAttribute("y2",y);l.setAttribute("stroke",css("--border"));svg.appendChild(l);t.setAttribute("x",pL-10);t.setAttribute("y",y+5);t.setAttribute("text-anchor","end");t.setAttribute("font-size",15);t.setAttribute("fill",css("--faint"));t.textContent=Math.round(v);svg.appendChild(t);});
  var valid=weeks.map(function(x,i){return{x:x,i:i};}).filter(function(v){return v.x.rangeHigh!=null;});if(valid.length){var upper=valid.map(function(v,i){return(i?"L":"M")+X(v.i)+" "+Y(v.x.rangeHigh);}).join(" "),lower=valid.slice().reverse().map(function(v){return"L"+X(v.i)+" "+Y(v.x.rangeLow);}).join(" "),band=document.createElementNS(ns,"path");band.setAttribute("d",upper+" "+lower+" Z");band.setAttribute("class","rangeband");svg.appendChild(band);}
  var line=weeks.map(function(x,i){return(i?"L":"M")+X(i)+" "+Y(x.effort||0);}).join(" "),p=document.createElementNS(ns,"path");p.setAttribute("d",line);p.setAttribute("fill","none");p.setAttribute("stroke",css("--accent"));p.setAttribute("stroke-width",4);p.setAttribute("stroke-linejoin","round");svg.appendChild(p);
  weeks.forEach(function(x,i){var c=document.createElementNS(ns,"circle");c.setAttribute("cx",X(i));c.setAttribute("cy",Y(x.effort||0));c.setAttribute("r",i===selected?9:6);c.setAttribute("fill",i===selected?css("--accent"):css("--surface"));c.setAttribute("stroke",css("--accent"));c.setAttribute("stroke-width",3);svg.appendChild(c);});
  [0,3,6,9,weeks.length-1].filter(function(v,i,a){return v<weeks.length&&a.indexOf(v)===i;}).forEach(function(i){var t=document.createElementNS(ns,"text");t.setAttribute("x",X(i));t.setAttribute("y",H-13);t.setAttribute("text-anchor","middle");t.setAttribute("font-size",15);t.setAttribute("fill",css("--faint"));t.textContent=weeks[i].label;t.setAttribute("font-weight",i===selected?700:400);svg.appendChild(t);});
  chartTip(svg,weeks,function(x){return '<b>'+x.rangeLabel+'</b><br>'+x.effort+' effort points<br>Suggested: '+n(x.rangeLow)+'–'+n(x.rangeHigh)+'<br>Garmin Load: '+x.garminLoad;});svg.onclick=function(e){var r=svg.getBoundingClientRect(),chartX=(e.clientX-r.left)/r.width*W,i=Math.round((chartX-pL)/(W-pL-pR)*(weeks.length-1));drawEffort(data,Math.max(0,Math.min(weeks.length-1,i)));};drawEffortWeek(weeks[selected]);
}

function drawEffortWeek(week){
  var stateText={within:"Within expected range",below:"Below expected range",above:"Above expected range",building:"Building baseline"}[week.state]||"Building baseline",state=document.getElementById("effort-state"),value=document.getElementById("effort-value"),meta=document.getElementById("effort-meta"),title=document.getElementById("effort-week"),list=document.getElementById("effort-list");if(state){state.textContent=stateText;state.className='pill '+(week.state==='above'?'warn':week.state==='within'?'good':'mute');}if(value)value.textContent=week.effort;if(meta)meta.textContent=week.rangeLabel+' · suggested '+n(week.rangeLow)+'–'+n(week.rangeHigh)+' points · capacity '+n(week.capacity);if(title)title.textContent='Daily effort · '+week.rangeLabel;if(list){list.innerHTML=(week.activities||[]).map(function(a){return '<div class="effortrow"><span><b>'+a.name+'</b> · '+a.min+' min'+(a.hr?' · '+a.hr+' bpm':'')+'<br>Garmin Training Load '+n(a.garminLoad)+'</span><span class="score">'+n(a.effort)+' pts</span></div>';}).join('')||'<div class="meta">No workouts recorded for this week.</div>';}
  var svg=document.getElementById("effortdaily"),days=week.days||[];if(!svg)return;svg.innerHTML="";var W=1000,H=180,pL=34,pR=14,pT=12,pB=38,max=Math.max.apply(null,days.map(function(x){return x.effort||0;}).concat([1]))*1.12,slot=(W-pL-pR)/7,bw=slot*.46;function Y(v){return pT+(max-v)/max*(H-pT-pB)}days.forEach(function(x,i){var bx=pL+i*slot+(slot-bw)/2,t=document.createElementNS(ns,"text");if(x.effort!=null){var bar=document.createElementNS(ns,"rect");bar.setAttribute("x",bx);bar.setAttribute("y",Y(x.effort));bar.setAttribute("width",bw);bar.setAttribute("height",Math.max(3,H-pB-Y(x.effort)));bar.setAttribute("rx",5);bar.setAttribute("fill",x.effort>0?css("--accent"):css("--track"));svg.appendChild(bar);}t.setAttribute("x",bx+bw/2);t.setAttribute("y",H-10);t.setAttribute("text-anchor","middle");t.setAttribute("font-size",17);t.setAttribute("fill",css("--faint"));t.textContent=x.label;svg.appendChild(t);});chartTip(svg,days,function(x){return '<b>'+x.label+'</b><br>'+n(x.effort)+' effort points';});
}

function drawFitness(series,days,selectedIndex){
  var svg=document.getElementById("fitnessc");if(!svg)return;svg.innerHTML="";var rows=series.slice(-days);if(!rows.length)return;
  var selected=selectedIndex==null?rows.length-1:Math.max(0,Math.min(rows.length-1,selectedIndex)),first=rows[0].fitness||0,current=rows[selected].fitness||0,delta=current-first,pct=first>=1?delta/first*100:null,summary=document.getElementById("fit-summary"),value=document.getElementById("fit-value"),period=days===30?'30 days':days===90?'90 days':days===180?'six months':days===365?'year':'two years';
  function fmt(date){return new Date(date+'T00:00:00').toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'});}
  if(value)value.textContent=current+' index';
  if(summary){var pctText=pct==null?'—%':Math.abs(Math.round(pct))+'%',direction=pct==null?'':delta>0?'▲ ':delta<0?'▼ ':'';summary.innerHTML='<span class="change '+(delta>0?'up':'')+'">'+direction+pctText+'</span><span>'+(delta>0?'+':'')+delta.toFixed(1)+' pts</span><span class="period">'+(selected===rows.length-1?'over the past '+period:'from '+fmt(rows[0].date)+' to '+fmt(rows[selected].date))+'</span>';}
  var W=1000,H=360,pL=52,pR=18,pT=26,pB=44,vals=[];rows.forEach(function(x){vals.push(x.fitness||0)});var max=Math.max.apply(null,vals.concat([1]))*1.12;
  function X(i){return pL+i*(W-pL-pR)/Math.max(1,rows.length-1)}function Y(v){return pT+(max-v)/max*(H-pT-pB)}function plot(key,color){var d=rows.map(function(x,i){return(i?"L":"M")+X(i).toFixed(1)+" "+Y(x[key]||0).toFixed(1);}).join(" ");var p=document.createElementNS(ns,"path");p.setAttribute("d",d);p.setAttribute("fill","none");p.setAttribute("stroke",color);p.setAttribute("stroke-width",4);p.setAttribute("stroke-linejoin","round");svg.appendChild(p)}
  [0,max/2,max].forEach(function(v){var l=document.createElementNS(ns,"line"),t=document.createElementNS(ns,"text");l.setAttribute("x1",pL);l.setAttribute("x2",W-pR);l.setAttribute("y1",Y(v));l.setAttribute("y2",Y(v));l.setAttribute("stroke",css("--border"));svg.appendChild(l);t.setAttribute("x",pL-10);t.setAttribute("y",Y(v)+5);t.setAttribute("text-anchor","end");t.setAttribute("font-size",15);t.setAttribute("fill",css("--faint"));t.textContent=Math.round(v);svg.appendChild(t)});
  var area=rows.map(function(x,i){return(i?"L":"M")+X(i).toFixed(1)+" "+Y(x.fitness||0).toFixed(1);}).join(" ")+" L"+X(rows.length-1)+" "+Y(0)+" L"+X(0)+" "+Y(0)+" Z",fill=document.createElementNS(ns,"path");fill.setAttribute("d",area);fill.setAttribute("fill",css("--accent"));fill.setAttribute("fill-opacity",".18");svg.appendChild(fill);plot("fitness",css("--accent"));
  var marker=document.createElementNS(ns,"line"),dot=document.createElementNS(ns,"circle");marker.setAttribute("x1",X(selected));marker.setAttribute("x2",X(selected));marker.setAttribute("y1",pT);marker.setAttribute("y2",H-pB);marker.setAttribute("stroke",css("--accent"));marker.setAttribute("stroke-width",2);svg.appendChild(marker);dot.setAttribute("cx",X(selected));dot.setAttribute("cy",Y(current));dot.setAttribute("r",7);dot.setAttribute("fill",css("--accent"));dot.setAttribute("stroke",css("--surface"));dot.setAttribute("stroke-width",3);svg.appendChild(dot);
  [0,Math.floor(rows.length/2),rows.length-1].forEach(function(i){var t=document.createElementNS(ns,"text");t.setAttribute("x",X(i));t.setAttribute("y",H-10);t.setAttribute("text-anchor","middle");t.setAttribute("font-size",17);t.setAttribute("fill",css("--faint"));t.textContent=rows[i].label;svg.appendChild(t)});chartTip(svg,rows,function(x){return '<b>'+x.label+'</b><br>Fitness: '+x.fitness+'<br>Fatigue: '+x.fatigue+'<br>Form: '+x.form+'<br>Load: '+x.load;});
  svg.onclick=function(e){var r=svg.getBoundingClientRect(),chartX=(e.clientX-r.left)/r.width*W,i=Math.round((chartX-pL)/(W-pL-pR)*(rows.length-1));drawFitness(series,days,Math.max(0,Math.min(rows.length-1,i)));};
}

function sec(t){var s=el("div","sec");s.innerHTML='<h2>'+t+'</h2><span class="line"></span>';return s;}
function cap(s){s=(s||"").toString().toLowerCase().replace(/_/g," ");return s.charAt(0).toUpperCase()+s.slice(1);}

function vo2Thresholds(age){
  var bands=[{max:29,v:[55.4,51.1,45.4,41.7]},{max:39,v:[54,48.3,44,40.5]},{max:49,v:[52.5,46.4,42.4,38.5]},{max:59,v:[48.9,43.4,39.2,35.6]},{max:69,v:[45.7,39.5,35.5,32.3]},{max:79,v:[42.1,36.7,32.3,29.4]}];
  return (bands.find(function(b){return age!=null&&age<=b.max;})||bands[2]).v;
}
function vo2RatingFor(value,age){var t=vo2Thresholds(age);if(value==null)return {label:"No data",color:"#718096"};if(value>=t[0])return {label:"Superior",color:"#7c4dff"};if(value>=t[1])return {label:"Excellent",color:"#1683ff"};if(value>=t[2])return {label:"Good",color:"#12b886"};if(value>=t[3])return {label:"Fair",color:"#f97316"};return {label:"Poor",color:"#ef4444"};}
function drawRing(id,value,goal,color){
  var svg=document.getElementById(id);if(!svg)return;
  var pct=(value&&goal)?Math.min(1,value/goal):0,r=52,c=2*Math.PI*r;
  function circ(st){var e=document.createElementNS(ns,"circle");e.setAttribute("cx",60);e.setAttribute("cy",60);
    e.setAttribute("r",r);e.setAttribute("fill","none");e.setAttribute("stroke",st);e.setAttribute("stroke-width",11);e.setAttribute("stroke-linecap","round");return e;}
  svg.appendChild(circ(css("--track")));
  var fg=circ(color);fg.setAttribute("stroke-dasharray",c);fg.setAttribute("stroke-dashoffset",c*(1-pct));
  fg.setAttribute("transform","rotate(-90 60 60)");svg.appendChild(fg);
}

function drawVo2(value,age){
  var svg=document.getElementById("vo2g");if(!svg)return;svg.innerHTML="";var cx=120,cy=128,r=84,t=vo2Thresholds(age),min=Math.max(15,t[3]-10),max=t[0]+5,start=200,end=340;
  function polar(angle){var a=angle*Math.PI/180;return [cx+r*Math.cos(a),cy+r*Math.sin(a)];}function arc(a,b,color){var p1=polar(a),p2=polar(b),path=document.createElementNS(ns,"path");path.setAttribute("d","M"+p1[0]+" "+p1[1]+" A"+r+" "+r+" 0 0 1 "+p2[0]+" "+p2[1]);path.setAttribute("fill","none");path.setAttribute("stroke",color);path.setAttribute("stroke-width",16);path.setAttribute("stroke-linecap","round");svg.appendChild(path);}
  var bounds=[min,t[3],t[2],t[1],t[0],max],colors=["#ef4444","#f97316","#12b886","#1683ff","#7c4dff"];bounds.slice(0,-1).forEach(function(from,i){var a=start+(end-start)*(from-min)/(max-min),b=start+(end-start)*(bounds[i+1]-min)/(max-min);arc(a+(i?2:0),b-(i<colors.length-1?2:0),colors[i]);});if(value==null)return;var pct=Math.max(0,Math.min(1,(value-min)/(max-min))),angle=start+(end-start)*pct,pos=polar(angle),dot=document.createElementNS(ns,"circle");dot.setAttribute("cx",pos[0]);dot.setAttribute("cy",pos[1]);dot.setAttribute("r",10);dot.setAttribute("fill",css("--surface"));dot.setAttribute("stroke",vo2RatingFor(value,age).color);dot.setAttribute("stroke-width",4);svg.appendChild(dot);
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
  chartTip(svg,series,function(x){return '<b>'+new Date(x[0]).toLocaleString()+'</b><br>Body Battery: '+x[1]+' / 100';});
}

load();
</script>
</body></html>
"""

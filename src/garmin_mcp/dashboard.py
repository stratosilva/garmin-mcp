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
import re
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

import requests

from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route


_HISTORY_CACHE = {"at": 0.0, "activities": []}
_BODY_FIELDS = ("weight_kg", "fat_pct", "muscle_pct", "body_water_pct")
_INJURY_FIELDS = ("left_big_toe_strain", "left_foot_plantar_fasciitis", "right_knee_patellar_tendon")
_STRENGTH_LOG_LOCK = threading.Lock()
_STRENGTH_FALLBACK_EXERCISES = (
    "Barbell Bulgarian Split Squat", "Dumbbell Bulgarian Split Squat",
    "Bulgarian Split Squat", "Barbell Lateral Step-up", "Dumbbell Lateral Step-up",
    "Lateral Step-up", "Leg Curl", "Seated Leg Curl", "Lying Leg Curl",
    "Chest Press", "Machine Chest Press", "Dumbbell Chest Press", "Barbell Bench Press",
    "Calf Raise", "Standing Calf Raise", "Seated Calf Raise", "Leg Press",
    "Leg Extension", "Squat", "Goblet Squat", "Romanian Deadlift", "Hip Thrust",
    "Lat Pull-down", "Seated Row", "Shoulder Press", "Lateral Raise", "Biceps Curl",
    "Triceps Press-down", "Push-up", "Pull-up", "Plank",
    "Farmer's Walk", "Farmers Walk", "Suitcase Carry", "Side Plank", "Lateral Plank",
    "Dead Hang", "Wall Sit",
    "Split Stance Anti-Rotation Cable Lift",
)
_STRENGTH_EXERCISE_CACHE = None
_STRENGTH_LOOKUP_CACHE = None
_STRENGTH_CATEGORY_CACHE = None
_MUSCLE_GROUPS = (
    ("delts", "Delts"), ("triceps", "Triceps"), ("biceps", "Biceps"),
    ("back", "Back / lats"), ("quads", "Quads"), ("glutes", "Glutes"),
    ("hamstrings", "Hamstrings"), ("chest", "Chest"), ("core", "Core"),
    ("calves", "Calves"),
)
_CARDIO_MUSCLE_FACTORS = {
    # One cardio unit is 20 minutes. These deliberately remain conservative:
    # they represent supporting exposure, not literal hypertrophy-set counts.
    "run": {"quads": .65, "glutes": .55, "hamstrings": .45, "calves": .55, "core": .20},
    "bike": {"quads": .70, "glutes": .50, "hamstrings": .35, "calves": .25, "core": .15},
    "walk": {"quads": .20, "glutes": .18, "hamstrings": .12, "calves": .22, "core": .08},
    "swim": {"back": .55, "delts": .50, "triceps": .30, "chest": .25, "core": .30},
    "elliptical": {"quads": .55, "glutes": .45, "hamstrings": .35, "calves": .25, "core": .15},
    "stairs": {"quads": .70, "glutes": .75, "hamstrings": .30, "calves": .40, "core": .20},
    "skierg": {"back": .65, "triceps": .50, "delts": .30, "core": .50, "glutes": .25, "hamstrings": .25},
    "rowing": {"back": .65, "biceps": .30, "quads": .45, "glutes": .40, "hamstrings": .35, "core": .35},
}
_GARMIN_CATEGORY_MUSCLES = {
    "BATTLE_ROPE": (("delts", "core"), ("biceps", "back")),
    "BENCH_PRESS": (("chest",), ("triceps", "delts")),
    "BIKE_OUTDOOR": (("quads", "glutes"), ("hamstrings", "calves", "core")),
    "CALF_RAISE": (("calves",), ()),
    "CARRY": (("core",), ("delts", "back")),
    "CHOP": (("core",), ("delts", "glutes")),
    "CORE": (("core",), ()),
    "CRUNCH": (("core",), ()),
    "CURL": (("biceps",), ()),
    "DEADLIFT": (("hamstrings", "glutes"), ("back", "core")),
    "ELLIPTICAL": (("quads", "glutes"), ("hamstrings", "calves")),
    "FLOOR_CLIMB": (("quads", "glutes"), ("calves", "hamstrings")),
    "FLYE": (("chest",), ("delts",)),
    "HIP_RAISE": (("glutes",), ("hamstrings", "core")),
    "HIP_STABILITY": (("glutes", "core"), ("hamstrings",)),
    "HIP_SWING": (("glutes", "hamstrings"), ("core",)),
    "HYPEREXTENSION": (("back", "glutes"), ("hamstrings", "core")),
    "INDOOR_BIKE": (("quads", "glutes"), ("hamstrings", "calves", "core")),
    "LADDER": (("quads", "calves"), ("glutes", "core")),
    "LATERAL_RAISE": (("delts",), ()),
    "LEG_CURL": (("hamstrings",), ("glutes",)),
    "LEG_RAISE": (("core",), ("quads",)),
    "LUNGE": (("quads", "glutes"), ("hamstrings", "core")),
    "OLYMPIC_LIFT": (("quads", "glutes", "delts"), ("hamstrings", "back", "core")),
    "PLANK": (("core",), ("delts", "glutes")),
    "PLYO": (("quads", "glutes", "calves"), ("hamstrings", "core")),
    "PULL_UP": (("back",), ("biceps", "delts")),
    "PUSH_UP": (("chest",), ("triceps", "delts")),
    "ROW": (("back",), ("biceps", "delts")),
    "RUN": (("quads", "glutes"), ("hamstrings", "calves", "core")),
    "RUN_INDOOR": (("quads", "glutes"), ("hamstrings", "calves", "core")),
    "SANDBAG": (("quads", "glutes", "core"), ("hamstrings", "back", "delts")),
    "SHOULDER_PRESS": (("delts",), ("triceps", "core")),
    "SHOULDER_STABILITY": (("delts", "core"), ("back",)),
    "SHRUG": (("back", "delts"), ()),
    "SIT_UP": (("core",), ()),
    "SLED": (("quads", "glutes"), ("hamstrings", "calves", "core")),
    "SLEDGE_HAMMER": (("core", "delts"), ("back", "triceps")),
    "SQUAT": (("quads", "glutes"), ("hamstrings", "core")),
    "STAIR_STEPPER": (("quads", "glutes"), ("hamstrings", "calves")),
    "SUSPENSION": (("core",), ("delts", "back")),
    "TIRE": (("quads", "glutes", "core"), ("hamstrings", "back", "delts")),
    "TOTAL_BODY": (("quads", "glutes", "core"), ("hamstrings", "back", "delts")),
    "TRICEPS_EXTENSION": (("triceps",), ()),
}
_RECOMMENDATION_PROMPT_VERSION = 5
_RECOMMENDATION_INSTRUCTIONS = (
    "You are a cautious endurance and strength coach. Use only the supplied data. Produce a "
    "practical next-24–48-hour plan in plain text, no more than 550 words, using exactly three "
    "clearly separated sections: CARDIO, STRENGTH — NEXT WORKOUT, and RECOVERY / MOBILITY. Coordinate the two so the "
    "combined leg load is sensible. The goals are to improve VO2, support recovery from the "
    "current leg injuries, return gradually to running, and build balanced strength.\n\n"
    "CARDIO: Recommend one preferred session and one lower-impact alternative chosen from "
    "cycling, walking, running, elliptical, rowing, SkiErg, intervals or sprints as appropriate. "
    "For both, give duration, warm-up/cool-down, heart-rate zone or RPE, and interval/recovery "
    "details where relevant. Interpret pain with the supplied prior-day-load-and-today-pain comparison: "
    "it can indicate tolerance, not prove causation. Low and stable symptoms are not automatically a "
    "reason to prohibit running. When plantar-fascia and patellar-tendon symptoms are 0–2/10 and have "
    "remained at or below their baseline the morning after a comparable run, a modest, conservative "
    "progression can be considered only if readiness and load also support it. At 3/10, prefer a flat, "
    "easy conversational run at maintained or slightly reduced volume; avoid speed work, sprints, "
    "plyometrics, steep hills and especially downhill running. Do not increase running volume while "
    "either symptom is around 3/10. Acknowledge a successful, low-pain next-day response positively "
    "and explain what it supports. If symptoms rise during the session, become sharp, or are worse the "
    "following morning, recommend reducing or substituting the next impact session. At 4/10 or above, "
    "or with a worsening trend, prefer pain-free lower-impact work. Briefly name the most important "
    "data signals behind the choice.\n\n"
    "STRENGTH — NEXT WORKOUT: Give exactly six numbered movement slots. Each slot must contain "
    "exactly two alternative exercises, A and B; the athlete chooses one option per slot, not all "
    "twelve. Every option must prescribe three working sets, repetitions and load. A compact form "
    "such as '3 × 10 @ 20 kg' specifies all three sets; say reps per side for unilateral work. "
    "For carries and isometric holds, prescribe '3 × seconds @ load' instead of repetitions. "
    "Format each numbered slot on three lines: the movement goal, then option A, then option B. "
    "Balance upper and lower body while using the current week's direct and total muscle stimulus "
    "to address meaningful gaps. Direct-set gaps matter more for strength than cardio exposure: "
    "for example, if hamstrings have no direct work, a slot can offer Romanian deadlift versus leg "
    "curl, with exercise-appropriate reps and different loads. Include pain-free calf, foot, knee, "
    "hip or trunk capacity where it supports a gradual return to running, without claiming to treat an injury.\n\n"
    "Use entered strength_training sets—including corrected Garmin data, manual sets, custom names, "
    "reps, timed-set duration, weights and per-side flags—as the athlete's actual history. Prefer familiar exercises. "
    "Base a numeric load on that same exercise's history; never transfer loads between exercises or "
    "machines. Keep progression conservative, normally no more than about 2.5–5% when recent sets "
    "were completed comfortably and recovery and pain are stable. When an option lacks its own load "
    "history, write 'load: choose a pain-free load with 2–3 reps in reserve', 'bodyweight', or an "
    "appropriate band level instead of inventing kilograms. Briefly explain how the six slots balance "
    "the current muscle stimulus.\n\n"
    "RECOVERY / MOBILITY: Give 2–4 concise, optional actions for today or after the proposed session. "
    "Use the recorded activity and strength sets from today and yesterday, plus the current pain trend, "
    "to select the actions rather than giving a generic routine. Each action must state its purpose, "
    "dose (time, repetitions or sets), and a pain-limited instruction. Gentle mobility, calf or foot "
    "capacity work, quadriceps/hip work, and stretching can be suggested when they fit the recorded load. "
    "Foam rolling may target comfortable surrounding muscle (for example calves, quads, glutes or upper back), "
    "but do not instruct direct, aggressive rolling over a painful plantar fascia, patellar tendon, or bony area. "
    "Do not present stretching or foam rolling as a cure. Skip actions that duplicate a demanding exercise already "
    "performed today or would add meaningful load to an irritated area.\n\n"
    "Use 7-day sleep, HRV, readiness, stress, training load, relative effort, fitness, VO2, heart-rate "
    "zones, recent cardio and muscle-stimulus trends together rather than reacting to one metric. Use "
    "calories, weight and body composition only as sustainable guardrails; activity calories are "
    "expenditure, not food intake. Do not diagnose or promise injury recovery. This is load-management "
    "guidance, not a diagnosis. If pain is 4/10 or higher, worsening, sharp, or the recommended movement "
    "provokes escalating pain, substitute pain-free low-impact work and advise professional assessment "
    "if symptoms persist or worsen. Do not repeat every metric or mention missing data."
)


def _recommendation_snapshot(dashboard):
    """Keep the model input useful, small, and free of account identifiers."""
    wellness = dashboard.get("wellness") or {}
    body = (dashboard.get("body") or {}).get("metrics") or {}
    injuries = (dashboard.get("injuries") or {}).get("records") or []
    definitions = (dashboard.get("injuries") or {}).get("definitions")
    injury_fields = tuple(row["id"] for row in definitions if row["enabled"]) if definitions is not None else _INJURY_FIELDS
    injuries = [{"date": row.get("date"), **{key: row.get(key) for key in injury_fields}} for row in injuries]

    recent_activities = dashboard.get("recent") or []
    recent_activities = recent_activities if isinstance(recent_activities, list) else []
    recent_cardio = [row for row in recent_activities
                     if isinstance(row, dict) and not row.get("isStrength") and _cardio_mode(row)]
    muscle_weeks = ((dashboard.get("muscleVolume") or {}).get("weeks") or [])[-2:]
    latest_pain = next((row for row in reversed(injuries)
                        if any(row.get(field) is not None for field in injury_fields)), None)
    previous_day_response = _previous_day_pain_response(
        dashboard.get("date"), injuries, recent_activities, dashboard.get("strength") or {}, injury_fields,
    )
    return {
        "date": dashboard.get("date"),
        "recovery": {
            "body_battery": wellness.get("bodyBattery"),
            "training_readiness": wellness.get("readiness"),
            "sleep": wellness.get("sleep"),
            "sleep_history_7d": (dashboard.get("sleepSeries") or [])[-7:],
            "hrv": wellness.get("hrv"),
            "hrv_history_7d": (dashboard.get("hrvSeries") or [])[-7:],
            "resting_heart_rate": wellness.get("restingHr"),
            "stress": wellness.get("stress"),
            "sleep_debt": ((dashboard.get("recovery") or {}).get("debt") or {}).get("minutes"),
            "manual_sleep_context": ((dashboard.get("recovery") or {}).get("debt") or {}).get("days"),
            "sleep_target_hours": (dashboard.get("recovery") or {}).get("sleepNeedHours"),
            "sleep_days_recorded": ((dashboard.get("recovery") or {}).get("debt") or {}).get("recordedDays"),
            "sleep_debt_level": ((dashboard.get("recovery") or {}).get("debt") or {}).get("level"),
            "sleep_need_hours": (dashboard.get("recovery") or {}).get("sleepNeedHours"),
            "sleep_regularity": (dashboard.get("recovery") or {}).get("regularity"),
            "readiness_contributors": (dashboard.get("recovery") or {}).get("contributors"),
        },
        "energy_and_body": {
            "activity_calories": wellness.get("calories"),
            "garmin_weight": wellness.get("weight"),
            "body_composition": body,
            "body_history": ((dashboard.get("body") or {}).get("records") or [])[-10:],
        },
        "fitness": {
            "vo2_max_run": wellness.get("vo2maxRun"),
            "vo2_max_bike": wellness.get("vo2maxBike"),
            "training_load": wellness.get("trainingLoad"),
            "training_load_trend_7d": dashboard.get("trainingLoadTrend") or [],
            "relative_effort": dashboard.get("relativeEffort"),
            "fitness_level_trend_42d": (dashboard.get("fitnessSeries") or [])[-42:],
            "hr_zones_week": dashboard.get("hrZonesWeek") or [],
            "intensity_minutes": wellness.get("intensity"),
            "cardio_by_sport": dashboard.get("sports") or {},
            "recent_cardio_workouts": recent_cardio[:8],
            "recent_activities": recent_activities[:12],
        },
        "strength_training": dashboard.get("strength") or {"activities": []},
        "muscle_stimulus_recent_weeks": muscle_weeks,
        "body_composition": body,
        "pain_latest": latest_pain,
        "tracked_injuries": [row for row in (definitions or []) if row["enabled"]],
        "pain_trend_14d": injuries[-14:],
        "prior_day_load_and_today_pain": previous_day_response,
    }


def _previous_day_pain_response(today_value, injuries, activities, strength, injury_fields=_INJURY_FIELDS):
    """Summarise yesterday's recorded load beside today's pain, without claiming causation."""
    try:
        today = datetime.date.fromisoformat(str(today_value)[:10])
    except (TypeError, ValueError):
        return None
    yesterday = today - datetime.timedelta(days=1)
    pain_by_date = {
        str(row.get("date"))[:10]: row for row in injuries
        if isinstance(row, dict) and row.get("date")
    }
    today_pain = pain_by_date.get(today.isoformat())
    yesterday_pain = pain_by_date.get(yesterday.isoformat())
    if not today_pain:
        return None

    previous_activities = [
        {key: row.get(key) for key in ("name", "sport", "typeKey", "min", "km", "load", "effort", "zones")}
        for row in activities
        if isinstance(row, dict) and str(row.get("date") or "")[:10] == yesterday.isoformat()
    ]
    previous_strength = []
    for activity in (strength.get("activities") or []):
        if not isinstance(activity, dict) or str(activity.get("start") or "")[:10] != yesterday.isoformat():
            continue
        previous_strength.append({
            "name": activity.get("name"), "workingSets": activity.get("workingSets"),
            "timedSeconds": activity.get("timedSeconds"), "sets": activity.get("sets") or [],
        })
    changes = {}
    for field in injury_fields:
        current, previous = today_pain.get(field), (yesterday_pain or {}).get(field)
        if isinstance(current, (int, float)) and isinstance(previous, (int, float)):
            changes[field] = round(current - previous, 1)
        else:
            changes[field] = None
    return {
        "activity_date": yesterday.isoformat(), "today_date": today.isoformat(),
        "previous_day_activities": previous_activities,
        "previous_day_strength": previous_strength,
        "previous_day_pain": {field: (yesterday_pain or {}).get(field) for field in injury_fields},
        "today_pain": {field: today_pain.get(field) for field in injury_fields},
        "pain_change_next_day": changes,
        "note": "Association only: assess the full pattern and the athlete's in-session symptoms, not one day alone.",
    }


def _openai_recommendation(dashboard):
    """Request a structured, non-medical next-48-hour coaching recommendation."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Personal recommendations are not configured yet.")

    model = os.environ.get("OPENAI_RECOMMENDATION_MODEL", "gpt-5-mini")
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "instructions": _RECOMMENDATION_INSTRUCTIONS,
            "input": json.dumps(_recommendation_snapshot(dashboard), separators=(",", ":")),
            # Spend the response budget on the visible two-part workout plan.
            "reasoning": {"effort": "none"},
            "max_output_tokens": 1200,
            "store": False,
        },
        timeout=45,
    )
    if not response.ok:
        try:
            error = response.json().get("error") or {}
            code = error.get("code") or error.get("type") or "request_failed"
        except ValueError:
            code = "request_failed"
        raise RuntimeError(f"OpenAI request failed (HTTP {response.status_code}: {code}).")
    payload = response.json()
    text = (payload.get("output_text") or "").strip()
    if not text:
        text = "\n".join(
            part.get("text", "")
            for item in payload.get("output", [])
            if isinstance(item, dict)
            for part in item.get("content", [])
            if isinstance(part, dict) and part.get("type") == "output_text"
        ).strip()
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


def _strength_log_path():
    """Persistent local corrections and manual sets for Garmin strength activities."""
    configured = os.environ.get("STRENGTH_LOG_PATH")
    return Path(configured) if configured else Path.home() / ".garminconnect" / "strength_training.json"


def _dashboard_database():
    """Use PostgreSQL in production and import the legacy files on first use."""
    if not os.environ.get("DATABASE_URL"):
        return None
    from garmin_mcp.dashboard_storage import database

    return database(_body_measurements_path(), _injury_measurements_path(), _strength_log_path())


def _strength_exercise_catalog():
    """Return Garmin's exercise names when the installed client exposes its catalog."""
    global _STRENGTH_EXERCISE_CACHE
    if _STRENGTH_EXERCISE_CACHE is not None:
        return _STRENGTH_EXERCISE_CACHE
    names = list(_STRENGTH_FALLBACK_EXERCISES)
    try:
        from garminconnect.exercises import EXERCISES
        names.extend(row.get("name") for row in EXERCISES if isinstance(row, dict))
    except (ImportError, AttributeError):
        pass
    _STRENGTH_EXERCISE_CACHE = sorted({name.strip() for name in names if isinstance(name, str) and name.strip()}, key=str.casefold)
    return _STRENGTH_EXERCISE_CACHE


def _strength_exercise_lookup():
    global _STRENGTH_LOOKUP_CACHE
    if _STRENGTH_LOOKUP_CACHE is not None:
        return _STRENGTH_LOOKUP_CACHE
    lookup = {}
    try:
        from garminconnect.exercises import EXERCISES
        for row in EXERCISES:
            if not isinstance(row, dict):
                continue
            category, exercise, name = row.get("category"), row.get("exercise"), row.get("name")
            if category and exercise and name:
                lookup[(str(category).upper(), str(exercise).upper())] = str(name)
    except (ImportError, AttributeError):
        pass
    _STRENGTH_LOOKUP_CACHE = lookup
    return _STRENGTH_LOOKUP_CACHE


def _strength_category_lookup():
    global _STRENGTH_CATEGORY_CACHE
    if _STRENGTH_CATEGORY_CACHE is not None:
        return _STRENGTH_CATEGORY_CACHE
    lookup = {}
    try:
        from garminconnect.exercises import EXERCISES
        for row in EXERCISES:
            if isinstance(row, dict) and row.get("name") and row.get("category"):
                lookup[str(row["name"]).casefold()] = str(row["category"]).upper()
    except (ImportError, AttributeError):
        pass
    _STRENGTH_CATEGORY_CACHE = lookup
    return _STRENGTH_CATEGORY_CACHE


def _read_strength_log_unlocked():
    db = _dashboard_database()
    if db:
        return db.strength_store()
    try:
        payload = json.loads(_strength_log_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"version": 1, "activities": {}, "manualWorkouts": {}, "manualActivities": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("activities"), dict):
        return {"version": 1, "activities": {}, "manualWorkouts": {}, "manualActivities": {}}
    payload["version"] = 1
    payload.setdefault("activities", {})
    payload.setdefault("manualWorkouts", {})
    payload.setdefault("manualActivities", {})
    return payload


def _write_strength_log_unlocked(payload):
    db = _dashboard_database()
    if db:
        for activity_id, entry in payload.get("activities", {}).items():
            db.save_strength_entry(activity_id, entry)
        return
    target = _strength_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(target)


def _manual_strength_workouts_unlocked():
    db = _dashboard_database()
    if db:
        return db.manual_strength_workouts()
    return _read_strength_log_unlocked().get("manualWorkouts", {})


def _save_manual_strength_workout_unlocked(manual_id, entry):
    db = _dashboard_database()
    if db:
        db.save_manual_strength_workout(manual_id, entry)
        return
    store = _read_strength_log_unlocked()
    store["manualWorkouts"][manual_id] = entry
    _write_strength_log_unlocked(store)


def _manual_endurance_activities_unlocked():
    db = _dashboard_database()
    if db:
        return db.manual_endurance_activities()
    return _read_strength_log_unlocked().get("manualActivities", {})


def _save_manual_endurance_activity_unlocked(manual_id, entry):
    db = _dashboard_database()
    if db:
        db.save_manual_endurance_activity(manual_id, entry)
        return
    store = _read_strength_log_unlocked()
    store["manualActivities"][manual_id] = entry
    _write_strength_log_unlocked(store)


def _delete_manual_activity(manual_id, kind):
    """Delete only the chosen dashboard entry, never a Garmin activity."""
    if kind not in ("strength", "endurance"):
        raise ValueError("invalid manual activity type")
    with _STRENGTH_LOG_LOCK:
        db = _dashboard_database()
        if db:
            return db.delete_manual_activity(manual_id, kind)
        store = _read_strength_log_unlocked()
        entries = store["manualWorkouts" if kind == "strength" else "manualActivities"]
        if manual_id not in entries:
            return False
        entry = entries[manual_id]
        if entry.get("source") == "merged" or entry.get("mergedGarminActivityId"):
            return False
        del entries[manual_id]
        _write_strength_log_unlocked(store)
        return True


def _strength_text(value, field="exercise", maximum=120):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{field} is too long")
    return value or None


def _strength_number(value, field, minimum, maximum, integer=False):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    if integer and not number.is_integer():
        raise ValueError(f"{field} must be a whole number")
    return int(number) if integer else round(number, 3)


def _strength_activity_id(value):
    try:
        activity_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("activity id must be a positive number") from exc
    if activity_id <= 0:
        raise ValueError("activity id must be a positive number")
    return activity_id


def _exercise_label(exercises):
    """Translate Garmin's category/name enums into the catalog's display name."""
    if not isinstance(exercises, list):
        return None
    lookup = _strength_exercise_lookup()
    category_fallback = None
    for exercise in exercises:
        if not isinstance(exercise, dict):
            continue
        category = str(exercise.get("category") or "").upper()
        name = str(exercise.get("name") or "").upper()
        if category and category != "UNKNOWN" and name:
            return lookup.get((category, name)) or name.replace("_", " ").title()
        probability = exercise.get("probability")
        if category and category != "UNKNOWN" and isinstance(probability, (int, float)) and probability > 0:
            category_fallback = category.replace("_", " ").title()
    return category_fallback


def _normalise_garmin_strength_sets(payload):
    """Keep editable non-rest Garmin sets and convert Garmin grams to kilograms."""
    if isinstance(payload, dict):
        rows = payload.get("exerciseSets") or []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    normalised = []
    for source_index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        set_type = str(row.get("setType") or "ACTIVE").upper()
        if set_type == "REST":
            continue
        message_index = row.get("messageIndex")
        stable_index = message_index if isinstance(message_index, int) and message_index >= 0 else source_index
        weight_grams = row.get("weight")
        weight_kg = round(float(weight_grams) / 1000.0, 3) if isinstance(weight_grams, (int, float)) else None
        reps = row.get("repetitionCount")
        reps = int(reps) if isinstance(reps, (int, float)) and float(reps).is_integer() else None
        normalised.append({
            "id": f"garmin:{stable_index}",
            "source": "garmin",
            "garminIndex": stable_index,
            "setType": set_type,
            "startTime": row.get("startTime"),
            "durationSeconds": round(row.get("duration"), 1) if isinstance(row.get("duration"), (int, float)) else None,
            "exercise": _exercise_label(row.get("exercises")),
            "reps": reps,
            "weightKg": weight_kg,
            "perSide": False,
        })
    return normalised


def _apply_strength_entry(base_sets, entry):
    overrides = entry.get("overrides") if isinstance(entry, dict) else {}
    overrides = overrides if isinstance(overrides, dict) else {}
    effective = []
    for raw in base_sets:
        row = dict(raw)
        override = overrides.get(raw["id"])
        if isinstance(override, dict):
            for field in ("exercise", "reps", "durationSeconds", "weightKg", "perSide", "setType"):
                if field in override:
                    row[field] = override[field]
        row.update({
            "rawExercise": raw.get("exercise"),
            "rawReps": raw.get("reps"),
            "rawDurationSeconds": raw.get("durationSeconds"),
            "rawWeightKg": raw.get("weightKg"),
            "edited": any(row.get(field) != raw.get(field) for field in ("exercise", "reps", "durationSeconds", "weightKg", "perSide")),
        })
        effective.append(row)
    manual = (entry.get("manualSets") or []) if isinstance(entry, dict) else []
    effective.extend(dict(row) for row in manual if isinstance(row, dict))
    return effective


def _strength_activity_payload(client, activity_id):
    activity_id = _strength_activity_id(activity_id)
    warning = None
    try:
        raw_sets = _normalise_garmin_strength_sets(client.get_activity_exercise_sets(activity_id))
    except Exception:  # noqa: BLE001 - saved snapshot still allows editing during Garmin outages
        raw_sets = []
        warning = "Garmin could not be reached. Showing the last saved set details."
    with _STRENGTH_LOG_LOCK:
        entry = _read_strength_log_unlocked().get("activities", {}).get(str(activity_id), {})
    if not raw_sets:
        raw_sets = [dict(row) for row in entry.get("lastGarminSets", []) if isinstance(row, dict)]
    effective = _apply_strength_entry(raw_sets, entry)
    recent = []
    with _STRENGTH_LOG_LOCK:
        store = _read_strength_log_unlocked()
    ordered_entries = sorted(store.get("activities", {}).values(), key=lambda row: row.get("updatedAt") or "", reverse=True)
    for saved in ordered_entries:
        for row in _apply_strength_entry(saved.get("lastGarminSets", []), saved):
            name = row.get("exercise")
            if name and name not in recent:
                recent.append(name)
    return {
        "activityId": activity_id,
        "sets": effective,
        "garminSetCount": len(raw_sets),
        "exercises": _strength_exercise_catalog(),
        "recentExercises": recent[:12],
        "savedAt": entry.get("updatedAt"),
        "warning": warning,
    }


def _validated_strength_set(row, source, position):
    if not isinstance(row, dict):
        raise ValueError("each strength set must be an object")
    set_id = _strength_text(row.get("id"), "set id", 80)
    expected_prefix = "garmin:" if source == "garmin" else "manual:"
    if not set_id or not set_id.startswith(expected_prefix):
        raise ValueError(f"invalid {source} set id")
    result = {
        "id": set_id,
        "source": source,
        "setType": (_strength_text(row.get("setType"), "set type", 30) or "ACTIVE").upper(),
        "exercise": _strength_text(row.get("exercise")),
        "reps": _strength_number(row.get("reps"), "repetitions", 0, 999, integer=True),
        "durationSeconds": _strength_number(row.get("durationSeconds"), "duration", 0, 86400),
        "weightKg": _strength_number(row.get("weightKg"), "weight", 0, 1000),
        "perSide": bool(row.get("perSide")),
    }
    if source == "garmin":
        result.update({
            "garminIndex": _strength_number(row.get("garminIndex", position), "Garmin set index", 0, 10000, integer=True),
            "startTime": _strength_text(row.get("startTime"), "start time", 50),
        })
    else:
        if (result["exercise"] is None and result["reps"] is None
                and result["durationSeconds"] is None and result["weightKg"] is None):
            return None
        if result["exercise"] is None:
            raise ValueError("manual sets need an exercise")
        if result["reps"] is None and not result["durationSeconds"]:
            raise ValueError("manual sets need repetitions or a duration")
    return result


def _save_strength_activity(activity_id, payload, current_garmin_sets=None):
    activity_id = _strength_activity_id(activity_id)
    if not isinstance(payload, dict) or not isinstance(payload.get("sets"), list):
        raise ValueError("a list of strength sets is required")
    if len(payload["sets"]) > 200:
        raise ValueError("a workout cannot contain more than 200 sets")
    garmin_sets, manual_sets, overrides = [], [], {}
    current_by_id = {
        row.get("id"): row for row in (current_garmin_sets or [])
        if isinstance(row, dict) and row.get("source") == "garmin" and row.get("id")
    }
    seen = set()
    for position, submitted in enumerate(payload["sets"]):
        source = submitted.get("source") if isinstance(submitted, dict) else None
        if source not in {"garmin", "manual"}:
            raise ValueError("set source must be garmin or manual")
        effective = _validated_strength_set(submitted, source, position)
        if effective is None:
            continue
        if effective["id"] in seen:
            raise ValueError("set ids must be unique")
        seen.add(effective["id"])
        if source == "manual":
            manual_sets.append(effective)
            continue
        current = current_by_id.get(effective["id"])
        raw = _validated_strength_set(current if current is not None else {
            **submitted,
            "exercise": submitted.get("rawExercise"),
            "reps": submitted.get("rawReps"),
            "durationSeconds": submitted.get("rawDurationSeconds"),
            "weightKg": submitted.get("rawWeightKg"),
            "perSide": False,
        }, "garmin", position)
        garmin_sets.append(raw)
        overrides[effective["id"]] = {field: effective.get(field) for field in ("exercise", "reps", "durationSeconds", "weightKg", "perSide", "setType")}
    entry = {
        "activityId": activity_id,
        "activityName": _strength_text(payload.get("activityName"), "activity name"),
        "activityStart": _strength_text(payload.get("activityStart"), "activity start", 50),
        "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "lastGarminSets": garmin_sets,
        "overrides": overrides,
        "manualSets": manual_sets,
    }
    with _STRENGTH_LOG_LOCK:
        store = _read_strength_log_unlocked()
        store["activities"][str(activity_id)] = entry
        _write_strength_log_unlocked(store)
    try:
        _recommendation_cache_path().unlink(missing_ok=True)
    except OSError:
        pass
    return entry


def _strength_summary(limit=8):
    with _STRENGTH_LOG_LOCK:
        store = _read_strength_log_unlocked()
    entries = sorted(
        store.get("activities", {}).values(),
        key=lambda row: row.get("activityStart") or row.get("updatedAt") or "",
        reverse=True,
    )[:limit]
    activities = []
    for entry in entries:
        sets = []
        volume, timed_seconds, timed_sets = 0.0, 0.0, 0
        for row in _apply_strength_entry(entry.get("lastGarminSets", []), entry):
            if not any(row.get(field) is not None for field in ("exercise", "reps", "durationSeconds", "weightKg")):
                continue
            clean = {field: row.get(field) for field in ("exercise", "reps", "durationSeconds", "weightKg", "perSide", "source", "setType")}
            sets.append(clean)
            if isinstance(clean["reps"], int) and isinstance(clean["weightKg"], (int, float)):
                volume += clean["reps"] * clean["weightKg"] * (2 if clean["perSide"] else 1)
            if (clean["reps"] is None and isinstance(clean["durationSeconds"], (int, float))
                    and clean["durationSeconds"] > 0):
                timed_sets += 1
                timed_seconds += clean["durationSeconds"] * (2 if clean["perSide"] else 1)
        activities.append({
            "activityId": entry.get("activityId"),
            "name": entry.get("activityName"),
            "start": entry.get("activityStart"),
            "workingSets": len(sets),
            "externalLoadVolumeKg": round(volume, 1),
            "timedSets": timed_sets,
            "timedSeconds": round(timed_seconds, 1),
            "sets": sets,
        })
    with _STRENGTH_LOG_LOCK:
        manual_entries = list(_manual_strength_workouts_unlocked().values())
    for entry in manual_entries:
        sets = [dict(row) for row in _manual_strength_sets(entry) if isinstance(row, dict)]
        estimate = _manual_strength_estimates(sets, [])
        activities.append({"activityId": "manual:" + str(entry.get("manualId")), "name": entry.get("activityName"),
                           "start": entry.get("activityStart"), "source": entry.get("source") or "manual",
                           "workingSets": estimate["workingSets"], "externalLoadVolumeKg": estimate["externalLoadVolumeKg"],
                           "timedSets": sum(1 for row in sets if row.get("reps") is None and row.get("durationSeconds")),
                           "timedSeconds": estimate["timedSeconds"], "sets": sets})
    activities.sort(key=lambda row: row.get("start") or "", reverse=True)
    activities = activities[:limit]
    return {"activities": activities}


def _manual_strength_sets(entry):
    return entry.get("mergedSets") if entry.get("source") == "merged" else entry.get("sets", [])


def _manual_strength_estimates(sets, garmin_activities):
    """Estimate only dashboard effort/calories; Garmin Training Load stays absent."""
    working = [row for row in sets if isinstance(row, dict) and row.get("exercise")]
    volume = sum((row.get("reps") or 0) * (row.get("weightKg") or 0) * (2 if row.get("perSide") else 1)
                 for row in working)
    timed_minutes = sum((row.get("durationSeconds") or 0) * (2 if row.get("perSide") else 1)
                        for row in working) / 60.0
    historical_rates = []
    for activity in garmin_activities:
        calories, total_sets = activity.get("cal"), activity.get("totalSets")
        if isinstance(calories, (int, float)) and calories > 0 and isinstance(total_sets, (int, float)) and total_sets > 0:
            historical_rates.append(calories / total_sets)
    historical_rates.sort()
    kcal_per_set = historical_rates[len(historical_rates) // 2] if historical_rates else 5.5
    calories = max(15.0, len(working) * kcal_per_set + timed_minutes * 3.0 + volume / 1500.0)
    effort = len(working) * 0.8 + timed_minutes * 0.4 + volume / 10000.0
    return {"workingSets": len(working), "externalLoadVolumeKg": round(volume, 1),
            "timedSeconds": round(timed_minutes * 60, 1), "calories": round(calories),
            "effort": round(max(1.0, effort), 1), "method": "personal strength-history per-set estimate" if historical_rates else "conservative set/volume estimate"}


def _manual_workout_activity(entry, garmin_activities):
    sets = _manual_strength_sets(entry)
    estimate = _manual_strength_estimates(sets, garmin_activities)
    start = entry.get("activityStart") or entry.get("updatedAt") or ""
    activity = {
        "activityId": "manual:" + str(entry.get("manualId")), "manualId": entry.get("manualId"),
        "source": entry.get("source") or "manual", "sport": "other", "typeKey": "strength_training",
        "isStrength": True, "name": entry.get("activityName") or "Manual strength workout",
        "date": start[:10], "start": start, "km": 0, "min": round(estimate["timedSeconds"] / 60),
        "hr": None, "maxHr": None, "cal": estimate["calories"], "pace": None, "load": None,
        "zones": [0, 0, 0, 0, 0], "effort": estimate["effort"], "effortZonePart": None,
        "effortLoadPart": None, "location": None, "totalSets": estimate["workingSets"],
        "totalReps": sum((row.get("reps") or 0) for row in sets if isinstance(row, dict)),
        "totalVolumeGrams": estimate["externalLoadVolumeKg"] * 1000,
        "estimate": estimate,
    }
    if entry.get("source") == "merged":
        garmin = next((row for row in garmin_activities if str(row.get("activityId")) == str(entry.get("mergedGarminActivityId"))), None)
        if garmin:
            for field in ("min", "hr", "maxHr", "cal", "load", "zones", "effort", "effortZonePart", "effortLoadPart", "location"):
                activity[field] = garmin.get(field)
            activity["date"] = garmin.get("date") or activity["date"]
            activity["start"] = garmin.get("start") or activity["start"]
            activity["estimate"] = None
    return activity


def _manual_endurance_calories(sport, minutes, weight_kg=70.0):
    # Standard MET approximation. It deliberately separates walking from running.
    met = {"walk": 3.5, "bike": 6.0, "run": 9.8}[sport]
    return round(met * 3.5 * weight_kg / 200.0 * minutes)


def _save_manual_endurance_activity(manual_id, payload):
    if not isinstance(payload, dict):
        raise ValueError("activity details are required")
    sport = str(payload.get("sport") or "").lower()
    if sport not in {"bike", "run", "walk"}:
        raise ValueError("choose bike, run, or walk")
    minutes = _strength_number(payload.get("minutes"), "minutes", 1, 1440)
    distance_km = _strength_number(payload.get("distanceKm"), "distance", 0, 1000)
    calories = _strength_number(payload.get("calories"), "calories", 0, 20000)
    if minutes is None or distance_km is None:
        raise ValueError("distance and time are required")
    weight_kg = _strength_number(payload.get("weightKg"), "body weight", 30, 300) or 70.0
    estimated = calories is None
    calories = calories if calories is not None else _manual_endurance_calories(sport, minutes, weight_kg)
    start = _strength_text(payload.get("activityStart"), "activity start", 50) or datetime.datetime.now(datetime.timezone.utc).isoformat()
    names = {"bike": "Manual bike", "run": "Manual run", "walk": "Manual walk"}
    entry = {"manualId": manual_id, "sport": sport,
             "activityName": _strength_text(payload.get("activityName"), "activity name") or names[sport],
             "activityStart": start, "minutes": minutes, "distanceKm": distance_km,
             "calories": calories, "caloriesEstimated": estimated, "weightKg": weight_kg,
             "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    with _STRENGTH_LOG_LOCK:
        _save_manual_endurance_activity_unlocked(manual_id, entry)
    return entry


def _manual_endurance_activity(entry):
    minutes, km = entry.get("minutes") or 0, entry.get("distanceKm") or 0
    pace = round(minutes / km, 2) if km else None
    # No heart rate means estimated effort is conservative and clearly separate from Garmin load.
    effort = round(minutes * {"walk": .12, "bike": .2, "run": .35}.get(entry.get("sport"), .1), 1)
    sport = entry.get("sport")
    default_hr = {"walk": 100, "run": 145}.get(sport)
    return {"activityId": "manual-endurance:" + str(entry.get("manualId")), "manualEnduranceId": entry.get("manualId"),
            "source": "manual", "sport": entry.get("sport"), "typeKey": "manual_" + str(entry.get("sport")),
            "isStrength": False, "name": entry.get("activityName"), "date": str(entry.get("activityStart") or "")[:10],
            "start": entry.get("activityStart"), "km": km, "min": minutes, "hr": default_hr, "maxHr": None,
            "cal": entry.get("calories"), "caloriesEstimated": bool(entry.get("caloriesEstimated")), "pace": pace,
            "load": None, "zones": [0, 0, 0, 0, 0], "effort": effort, "effortZonePart": None,
            "effortLoadPart": None, "location": None, "totalSets": None, "totalReps": None, "totalVolumeGrams": None}


def _manual_endurance_steps(entry, height_cm=185.0):
    """Distance-based estimate; walk/run stride lengths scale with recorded height."""
    if entry.get("sport") not in {"walk", "run"}:
        return 0
    stride_m = height_cm * (0.415 if entry.get("sport") == "walk" else 0.65) / 100.0
    return round((float(entry.get("distanceKm") or 0) * 1000) / stride_m) if stride_m else 0


def _manual_strength_payload(manual_id):
    with _STRENGTH_LOG_LOCK:
        entry = _manual_strength_workouts_unlocked().get(manual_id)
    if not isinstance(entry, dict):
        raise ValueError("manual workout was not found")
    payload = dict(entry)
    payload["manualId"] = manual_id
    payload["exercises"] = _strength_exercise_catalog()
    payload["recentExercises"] = []
    return payload


def _save_manual_strength_workout(manual_id, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("sets"), list):
        raise ValueError("a list of strength sets is required")
    if len(payload["sets"]) > 200:
        raise ValueError("a workout cannot contain more than 200 sets")
    sets, seen = [], set()
    for position, row in enumerate(payload["sets"]):
        clean = _validated_strength_set(row, "manual", position)
        if clean is None:
            continue
        if clean["id"] in seen:
            raise ValueError("set ids must be unique")
        seen.add(clean["id"])
        sets.append(clean)
    if not sets:
        raise ValueError("add at least one completed set")
    name = _strength_text(payload.get("activityName"), "workout name") or "Manual strength workout"
    start = _strength_text(payload.get("activityStart"), "workout start", 50)
    if not start:
        start = datetime.datetime.now(datetime.timezone.utc).isoformat()
    entry = {"manualId": manual_id, "activityName": name, "activityStart": start,
             "sets": sets, "source": "manual", "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    with _STRENGTH_LOG_LOCK:
        _save_manual_strength_workout_unlocked(manual_id, entry)
    return entry


def _manual_merge_candidates(client, manual_id):
    entry = _manual_strength_payload(manual_id)
    try:
        manual_start = datetime.datetime.fromisoformat(str(entry["activityStart"]).replace("Z", "+00:00"))
    except ValueError:
        return []
    candidates = []
    for raw in _call(client.get_activities, 0, 40) or []:
        activity = _map_activity(raw) if isinstance(raw, dict) else None
        if not activity or not activity.get("isStrength") or not activity.get("start"):
            continue
        try:
            started = datetime.datetime.fromisoformat(activity["start"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if abs((started - manual_start).total_seconds()) <= 24 * 3600:
            candidates.append(activity)
    return sorted(candidates, key=lambda row: abs((datetime.datetime.fromisoformat(row["start"].replace("Z", "+00:00")) - manual_start).total_seconds()))[:3]


def _merge_manual_strength_workout(client, manual_id, garmin_activity_id):
    entry = _manual_strength_payload(manual_id)
    garmin_id = _strength_activity_id(garmin_activity_id)
    candidates = {str(row["activityId"]) for row in _manual_merge_candidates(client, manual_id)}
    if str(garmin_id) not in candidates:
        raise ValueError("choose one of the recent Garmin strength workouts")
    garmin_sets = _normalise_garmin_strength_sets(client.get_activity_exercise_sets(garmin_id))
    manual_sets = entry.get("sets") or []
    merged = []
    for index, raw in enumerate(garmin_sets):
        row = dict(raw)
        if index < len(manual_sets):
            row["exercise"] = manual_sets[index].get("exercise")
            row["source"] = "merged"
        else:
            row["exercise"] = None
            row["source"] = "garmin-unlabelled"
        merged.append(row)
    for row in manual_sets[len(garmin_sets):]:
        extra = dict(row)
        extra["source"] = "manual"
        merged.append(extra)
    entry.update({"source": "merged", "mergedGarminActivityId": garmin_id, "mergedSets": merged,
                  "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    with _STRENGTH_LOG_LOCK:
        _save_manual_strength_workout_unlocked(manual_id, entry)
    return entry


def _strength_muscles(exercise):
    """Map an exercise name to primary and assisting muscle groups."""
    name = str(exercise or "").lower().replace("_", " ").replace("-", " ")
    if not name:
        return (), ()
    if any(term in name for term in ("anti rotation", "pallof")):
        return ("core",), ("delts", "glutes")
    if "plank" in name:
        return ("core",), ("delts", "glutes")
    if any(term in name for term in ("crunch", "oblique", "ab ", "core")):
        return ("core",), ()
    if any(term in name for term in ("deadbug", "windshield wiper", "spinal twist", "hip crossover", "slide out", "walkout")):
        return ("core",), ("delts", "glutes")
    if any(term in name for term in ("bulgarian", "split squat", "lunge", "step up", "stepup")):
        return ("quads", "glutes"), ("hamstrings", "core")
    if any(term in name for term in ("leg curl", "hamstring curl", "nordic curl")):
        return ("hamstrings",), ("glutes",)
    if any(term in name for term in ("deadlift", "good morning", "kettlebell swing")):
        return ("hamstrings", "glutes"), ("back", "core")
    if any(term in name for term in ("hip thrust", "glute bridge", "kickback", "hip abduction", "clam shell")):
        return ("glutes",), ("hamstrings", "core")
    if any(term in name for term in ("donkey kick", "fire hydrant", "hip extension", "leg abduction", "leg adduction", "band walk")):
        return ("glutes",), ("hamstrings", "core")
    if "calf" in name:
        return ("calves",), ()
    if "leg extension" in name:
        return ("quads",), ()
    if any(term in name for term in ("squat", "leg press")):
        return ("quads", "glutes"), ("hamstrings", "core")
    if any(term in name for term in ("bench press", "chest press", "chest fly", "push up", "pushup", "dip")):
        return ("chest",), ("triceps", "delts")
    if any(term in name for term in ("shoulder press", "military press", "overhead press", "arnold press")):
        return ("delts",), ("triceps", "core")
    if any(term in name for term in ("lateral raise", "front raise", "rear delt", "reverse fly")):
        return ("delts",), ()
    if any(term in name for term in ("external rotation", "internal rotation", "pull apart", "shoulder abduction", "shoulder extension", "shoulder flexion", "wall crawl")):
        return ("delts",), ("back", "core")
    if any(term in name for term in ("pull up", "pullup", "chin up", "chinup", "lat pull", "pulldown", "pull down", " row")) or name.startswith("row"):
        return ("back",), ("biceps", "delts")
    if "latpull" in name:
        return ("back",), ("biceps", "delts")
    if any(term in name for term in ("biceps", "bicep", " curl")) or name.startswith("curl"):
        return ("biceps",), ()
    if any(term in name for term in ("triceps", "tricep", "pressdown", "pushdown", "skull crusher")):
        return ("triceps",), ()
    if any(term in name for term in ("clean", "snatch", "thruster")):
        return ("quads", "glutes", "delts"), ("hamstrings", "back", "core")
    if "farmer" in name or "suitcase" in name or "carry" in name:
        return ("core",), ("delts", "back")
    if "dead hang" in name:
        return ("back",), ("delts", "biceps")
    if "wall sit" in name:
        return ("quads",), ("glutes", "core")
    if any(term in name for term in ("jump rope", "double under", "triple under", "jumping jack", "split jack", "ski mogul", "bob and weave")):
        return ("calves", "quads"), ("glutes", "core")
    if "back extension" in name:
        return ("back", "glutes"), ("hamstrings", "core")
    if name in {"banded fly", "weighted banded fly"}:
        return ("chest",), ("delts",)
    if "opposite arm and leg balance" in name:
        return ("core", "glutes"), ("delts", "back")
    category = _strength_category_lookup().get(str(exercise or "").casefold())
    return _GARMIN_CATEGORY_MUSCLES.get(category, ((), ()))


def _exercise_muscle_profile(exercise):
    primary, secondary = _strength_muscles(exercise)
    labels = dict(_MUSCLE_GROUPS)
    muscles = [
        {"key": key, "label": labels[key], "role": "primary", "volumeCreditPct": 100}
        for key in primary
    ]
    muscles.extend(
        {"key": key, "label": labels[key], "role": "assisting", "volumeCreditPct": 50}
        for key in secondary if key not in primary
    )
    return {"exercise": str(exercise or ""), "muscles": muscles, "mapped": bool(muscles)}


def _cardio_mode(activity):
    text = f"{activity.get('typeKey') or ''} {activity.get('name') or ''}".lower().replace("_", " ")
    if "ski erg" in text or "skierg" in text:
        return "skierg"
    if "row" in text:
        return "rowing"
    if "ellipt" in text or "cross trainer" in text:
        return "elliptical"
    if any(term in text for term in ("stair", "floor climb", "step machine")):
        return "stairs"
    sport = activity.get("sport")
    return sport if sport in {"run", "bike", "walk", "swim"} else None


def _payload_date(row):
    if not isinstance(row, dict):
        return None
    for key in ("calendarDate", "date", "startDate", "start"):
        value = row.get(key)
        if value:
            try:
                return datetime.date.fromisoformat(str(value)[:10])
            except ValueError:
                continue
    return None


def _week_label(start, end):
    def short(value):
        return value.strftime("%b %d").replace(" 0", " ")
    return f"{short(start)}–{short(end)}"


def _muscle_volume_weeks(strength, activities, daily_steps, daily_stats, today, week_count=12):
    """Estimate weekly direct sets and conservative indirect/cardio exposure."""
    current_start = today - datetime.timedelta(days=today.weekday())
    starts = [current_start - datetime.timedelta(weeks=offset) for offset in range(week_count - 1, -1, -1)]
    buckets = {}
    muscle_order = {key: index for index, (key, _label) in enumerate(_MUSCLE_GROUPS)}
    for start in starts:
        values = {key: {"direct": 0.0, "secondary": 0.0, "cardio": 0.0, "movement": 0.0}
                  for key, _label in _MUSCLE_GROUPS}
        buckets[start] = {
            "weekStart": start.isoformat(),
            "weekEnd": (start + datetime.timedelta(days=6)).isoformat(),
            "label": _week_label(start, start + datetime.timedelta(days=6)),
            "isCurrent": start == current_start,
            "values": values,
            "exerciseMap": {},
            "sources": {"strengthSets": 0, "unclassifiedSets": 0, "cardioSessions": 0,
                        "cardioMinutes": 0, "steps": 0, "floors": 0, "movementDays": 0},
        }

    def bucket_for(value):
        date = _payload_date(value) if isinstance(value, dict) else None
        if date is None and value:
            try:
                date = datetime.date.fromisoformat(str(value)[:10])
            except ValueError:
                return None
        if date is None:
            return None
        start = date - datetime.timedelta(days=date.weekday())
        return buckets.get(start)

    for activity in (strength or {}).get("activities", []):
        bucket = bucket_for(activity.get("start")) if isinstance(activity, dict) else None
        if not bucket:
            continue
        for row in activity.get("sets", []):
            if not isinstance(row, dict) or str(row.get("setType") or "ACTIVE").upper() in {"REST", "WARMUP"}:
                continue
            exercise = row.get("exercise") or "Unspecified exercise"
            profile = _exercise_muscle_profile(exercise)
            primary = tuple(item["key"] for item in profile["muscles"] if item["role"] == "primary")
            secondary = tuple(item["key"] for item in profile["muscles"] if item["role"] == "assisting")
            exercise_row = bucket["exerciseMap"].setdefault(exercise, {**profile, "sets": 0})
            exercise_row["sets"] += 1
            if not primary:
                bucket["sources"]["unclassifiedSets"] += 1
                continue
            bucket["sources"]["strengthSets"] += 1
            for muscle in primary:
                bucket["values"][muscle]["direct"] += 1.0
            for muscle in secondary:
                bucket["values"][muscle]["secondary"] += .5

    for activity in activities or []:
        if not isinstance(activity, dict) or activity.get("isStrength"):
            continue
        bucket, mode = bucket_for(activity.get("date") or activity.get("start")), _cardio_mode(activity)
        minutes = activity.get("min")
        if not bucket or not mode or not isinstance(minutes, (int, float)) or minutes <= 0:
            continue
        zones = activity.get("zones") or []
        zone_total = sum(value for value in zones if isinstance(value, (int, float)))
        hard_minutes = sum(value for value in zones[2:] if isinstance(value, (int, float)))
        intensity = 1.0 + min(.35, hard_minutes / zone_total * .35) if zone_total else 1.0
        units = min(float(minutes) / 20.0, 3.5) * intensity
        bucket["sources"]["cardioSessions"] += 1
        bucket["sources"]["cardioMinutes"] += round(minutes)
        for muscle, factor in _CARDIO_MUSCLE_FACTORS[mode].items():
            bucket["values"][muscle]["cardio"] += units * factor

    steps_by_week = {}
    for row in daily_steps or []:
        date = _payload_date(row)
        if date is None:
            continue
        start = date - datetime.timedelta(days=date.weekday())
        steps = _find_num(row, ("totalSteps", "steps")) or 0
        steps_by_week[start] = steps_by_week.get(start, 0) + max(0, steps)
    floors_by_week, movement_days = {}, {}
    for row in daily_stats or []:
        date = _payload_date(row)
        if date is None:
            continue
        start = date - datetime.timedelta(days=date.weekday())
        floors = _find_num(row, ("floorsAscended", "floors")) or 0
        floors_by_week[start] = floors_by_week.get(start, 0) + max(0, floors)
        movement_days[start] = movement_days.get(start, 0) + 1

    for start, bucket in buckets.items():
        steps, floors = steps_by_week.get(start, 0), floors_by_week.get(start, 0)
        bucket["sources"].update({"steps": round(steps), "floors": round(floors),
                                  "movementDays": movement_days.get(start, 0)})
        step_units, floor_units = min(3.0, steps / 35000.0), min(2.0, floors / 60.0)
        for muscle, factor in {"quads": .30, "glutes": .25, "hamstrings": .15, "calves": .35, "core": .08}.items():
            bucket["values"][muscle]["movement"] += step_units * factor
        for muscle, factor in {"quads": .35, "glutes": .50, "hamstrings": .15, "calves": .20, "core": .08}.items():
            bucket["values"][muscle]["movement"] += floor_units * factor

    weeks = []
    for start in starts:
        bucket = buckets[start]
        muscles = []
        for key, label in _MUSCLE_GROUPS:
            values = bucket["values"][key]
            indirect = values["secondary"] + values["cardio"] + values["movement"]
            muscles.append({
                "key": key, "label": label,
                "direct": round(values["direct"], 1),
                "indirectStrength": round(values["secondary"], 1),
                "cardio": round(values["cardio"], 1),
                "movement": round(values["movement"], 1),
                "indirect": round(indirect, 1),
                "total": round(values["direct"] + indirect, 1),
            })
        muscles.sort(key=lambda row: (-row["total"], muscle_order[row["key"]]))
        bucket.pop("values")
        exercise_rows = list(bucket.pop("exerciseMap").values())
        exercise_rows.sort(key=lambda row: (-row["sets"], row["exercise"].casefold()))
        bucket["exerciseBreakdown"] = exercise_rows
        bucket["muscles"] = muscles
        weeks.append(bucket)
    return {
        "weeks": weeks,
        "currentIndex": len(weeks) - 1,
        "formula": (
            "Direct volume is completed working sets for primary muscles. The lighter total adds 0.5 for "
            "assisting muscles plus conservative, capped exposure from cardio, steps and recorded floors. "
            "It is a planning estimate—not a claim that cardio minutes equal hypertrophy sets."
        ),
    }


def _cached_recommendation(date):
    try:
        payload = json.loads(_recommendation_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if (payload.get("date") == date and payload.get("text")
                       and payload.get("promptVersion") == _RECOMMENDATION_PROMPT_VERSION) else None


def _save_recommendation(date, recommendation):
    target = _recommendation_cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": date, "text": recommendation["text"], "model": recommendation["model"],
               "promptVersion": _RECOMMENDATION_PROMPT_VERSION,
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
        db = _dashboard_database()
        if db:
            rows = ({"timestamp": row[0].isoformat(), "weight_kg": row[1], "fat_pct": row[2],
                     "muscle_pct": row[3], "bone_pct": row[4], "body_water_pct": row[5],
                     "source": row[6]} for row in db.body_measurements())
        else:
            handle = _ensure_body_measurements_file().open(newline="", encoding="utf-8")
            rows = csv.DictReader(handle)
        try:
            for row in rows:
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
        finally:
            if not db:
                handle.close()
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
    db = _dashboard_database()
    if db:
        db.add_body_measurement(row)
    else:
        target = _ensure_body_measurements_file()
        with target.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("timestamp", "weight_kg", "fat_pct", "muscle_pct", "bone_pct", "body_water_pct", "source"))
            writer.writerow(row)


_INJURY_LOCK = threading.RLock()
_INJURY_DEFAULTS = [
    {"id": "left_big_toe_strain", "name": "Left big toe strain", "color": "#e5484d", "enabled": True},
    {"id": "left_foot_plantar_fasciitis", "name": "Left foot plantar fasciitis", "color": "#f08c00", "enabled": True},
    {"id": "right_knee_patellar_tendon", "name": "Right knee patellar tendon", "color": "#1683ff", "enabled": True},
]


def _injury_definitions():
    db = _dashboard_database()
    if db:
        definitions = db.injury_definitions()
    else:
        target = _injury_measurements_path().with_suffix(".settings.json")
        definitions = json.loads(target.read_text(encoding="utf-8")) if target.exists() else None
    return definitions if definitions is not None else [dict(row) for row in _INJURY_DEFAULTS]


def _save_injury_definitions(payload):
    rows = payload.get("definitions") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
        raise ValueError("settings must contain between 1 and 50 injuries")
    with _INJURY_LOCK:
        known = {row["id"] for row in _injury_definitions()}
        result, ids, names = [], set(), set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid injury settings")
            injury_id = row.get("id") or "injury_" + uuid.uuid4().hex
            if row.get("id") and injury_id not in known:
                raise ValueError("unknown injury; reload settings before saving")
            name = _strength_text(row.get("name"), "injury name", 100)
            color = row.get("color")
            if not name or name.casefold() in names or injury_id in ids:
                raise ValueError("give each injury a unique name")
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ValueError("choose a valid injury colour")
            if not isinstance(row.get("enabled"), bool):
                raise ValueError("enabled must be true or false")
            ids.add(injury_id); names.add(name.casefold())
            result.append({"id": injury_id, "name": name, "color": color, "enabled": row["enabled"]})
        if not known.issubset(ids):
            raise ValueError("injuries cannot be removed; disable them to preserve history")
        db = _dashboard_database()
        if db:
            db.save_injury_definitions(result)
        else:
            target = _injury_measurements_path().with_suffix(".settings.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
        return result


def _injury_measurements(days=30):
    """Return definitions and retained history, including disabled injuries."""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days - 1)
    definitions = _injury_definitions()
    fields = [row["id"] for row in definitions]
    by_date = {}
    target = _injury_measurements_path()
    db = _dashboard_database()
    if db:
        for values in db.injury_measurements(start, today):
            by_date[values[0]] = dict(zip(_INJURY_FIELDS, values[1:]))
        for date, scores in db.injury_scores(start, today):
            by_date.setdefault(date, {}).update(scores)
    elif target.exists():
        with target.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    date = datetime.date.fromisoformat((row.get("date") or "")[:10])
                except ValueError:
                    continue
                if start <= date <= today:
                    values = {}
                    for field in fields:
                        try:
                            values[field] = float(row[field]) if row.get(field) not in (None, "") else None
                        except ValueError:
                            values[field] = None
                    values["notes"] = row.get("notes") or ""
                    by_date[date] = values
    return {"definitions": definitions, "records": [
        {"date": (start + datetime.timedelta(days=offset)).isoformat(), **by_date.get(start + datetime.timedelta(days=offset), {})}
        for offset in range(days)
    ]}


def _append_injury_measurement(payload):
    """Update enabled injuries while retaining disabled scores on the same day."""
    if not isinstance(payload, dict):
        raise ValueError("pain scores must be an object")
    try:
        date = datetime.date.fromisoformat(str(payload.get("date") or datetime.date.today().isoformat()))
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if date > datetime.date.today():
        raise ValueError("pain measurements cannot be entered for a future day")
    with _INJURY_LOCK:
        fields = [row["id"] for row in _injury_definitions() if row["enabled"]]
        if not fields:
            raise ValueError("enable an injury in settings before adding scores")
        if set(payload) - {"date", "notes", *fields}:
            raise ValueError("injury settings changed; reload the score form")
        scores = {}
        if "notes" in payload:
            notes = payload["notes"]
            if not isinstance(notes, str) or len(notes) > 2000:
                raise ValueError("notes must be text of up to 2,000 characters")
            scores["notes"] = notes.strip()
        for field in fields:
            try:
                value = float(payload[field])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("enter a whole-number pain score from 0 to 10 for each enabled injury") from exc
            if not value.is_integer() or not 0 <= value <= 10:
                raise ValueError("pain scores must be whole numbers from 0 to 10")
            scores[field] = int(value)
        db = _dashboard_database()
        if db:
            db.upsert_injury_scores(date, scores)
            return
        target = _injury_measurements_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        existing, columns = [], ["date", *_INJURY_FIELDS]
        if target.exists():
            with target.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                columns = list(dict.fromkeys(columns + (reader.fieldnames or [])))
                existing = list(reader)
        row = next((old for old in existing if old.get("date") == date.isoformat()), None)
        if row is None:
            row = {"date": date.isoformat()}; existing.append(row)
        row.update(scores)
        columns = list(dict.fromkeys(columns + list(scores)))
        temporary = target.with_suffix(".tmp")
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader(); writer.writerows(existing)
        temporary.replace(target)


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


# ---- sleep & recovery -------------------------------------------------------
# Garmin supplies the raw nightly material; the baselines, sleep debt,
# regularity and contributor grading below are derived here, in the spirit of
# Oura's readiness/sleep breakdown. Body temperature has no equivalent in the
# Garmin Connect endpoints, so that contributor is deliberately absent.

SLEEP_HISTORY_DAYS = 60          # history for recovery context and sleep trends
SLEEP_CONTEXT_DAYS = 14          # sleep balance and regularity context



def _recommended_sleep_hours(age):
    """Midpoint of the National Sleep Foundation band for the athlete's age."""
    if age is None:
        return 8.0
    if age < 18:
        return 9.0
    if age < 65:  # adults 18-64: 7-9 hours
        return 8.0
    return 7.5    # 65+: 7-8 hours


def _local_clock(millis):
    """Convert a Garmin local-timestamp (epoch ms) to hours past midnight."""
    value = _num(millis)
    if not value:
        return None
    # Garmin's *Local timestamps are already shifted, so reading them as UTC
    # recovers the wall-clock time the athlete actually went to bed.
    stamp = datetime.datetime.fromtimestamp(value / 1000.0, datetime.timezone.utc)
    return stamp.hour + stamp.minute / 60.0


def _sleep_night(row):
    """Normalise one night from either Garmin sleep payload shape.

    ``get_sleep_data`` nests everything under ``dailySleepDTO``; the range
    endpoint ``get_sleep_daily`` returns flatter per-day rows. Field spellings
    differ between device generations, so every lookup goes through the
    tolerant recursive helper rather than a fixed path.
    """
    if not isinstance(row, dict):
        return None
    daily = row.get("dailySleepDTO") if isinstance(row.get("dailySleepDTO"), dict) else row
    date = daily.get("calendarDate") or row.get("calendarDate")
    seconds = _find_num(daily, ("sleepTimeSeconds", "totalSleepSeconds", "sleepSeconds"))
    if not date or not seconds:
        return None
    stages = {
        "deep": _find_num(daily, ("deepSleepSeconds",)) or 0,
        "light": _find_num(daily, ("lightSleepSeconds",)) or 0,
        "rem": _find_num(daily, ("remSleepSeconds", "remSleepInSeconds")) or 0,
        "awake": _find_num(daily, ("awakeSleepSeconds", "awakeSeconds")) or 0,
    }
    start_ms = _find_num(daily, ("sleepStartTimestampLocal", "sleepStartTimestampGMT"))
    end_ms = _find_num(daily, ("sleepEndTimestampLocal", "sleepEndTimestampGMT"))
    in_bed = (end_ms - start_ms) / 1000.0 if start_ms and end_ms and end_ms > start_ms else None
    # Garmin reports awake time inside the window, so efficiency is asleep/in-bed.
    efficiency = round(seconds / in_bed * 100) if in_bed and in_bed > 0 else None
    scores = daily.get("sleepScores") if isinstance(daily.get("sleepScores"), dict) else {}
    return {
        "date": str(date)[:10],
        "hours": round(seconds / 3600.0, 1),
        "seconds": seconds,
        "score": _find_num(scores.get("overall") or {}, ("value",)) or _find_num(daily, ("sleepScoreTotal",)),
        "stages": {name: round(value / 60.0) for name, value in stages.items()},
        "inBedHours": round(in_bed / 3600.0, 1) if in_bed else None,
        "efficiency": efficiency if efficiency is None or efficiency <= 100 else 100,
        "latency": round((_find_num(daily, ("sleepLatencySeconds", "latencySeconds")) or 0) / 60.0) or None,
        "restlessMoments": _find_num(daily, ("restlessMomentsCount",)),
        "awakeCount": _find_num(daily, ("awakeCount",)),
        "avgHr": _find_num(daily, ("restingHeartRate", "averageHeartRate")),
        "avgSpo2": _find_num(daily, ("averageSpO2Value", "averageSpo2Value")),
        "avgRespiration": _find_num(daily, ("averageRespirationValue",)),
        "bedTime": _local_clock(start_ms),
        "wakeTime": _local_clock(end_ms),
    }


def _hrv_values(payload):
    """Last night's HRV plus Garmin's own balanced baseline range.

    Garmin nests the baseline under ``baseline`` with names that have changed
    across firmware (``balancedLow``/``balancedUpper``, older ``lowUpper``), so
    the previous flat ``baselineLow``/``baselineHigh`` lookup always missed.
    """
    if not isinstance(payload, dict):
        return {"value": None, "status": None, "baselineLow": None, "baselineHigh": None}
    summary = payload.get("hrvSummary") if isinstance(payload.get("hrvSummary"), dict) else payload
    baseline = summary.get("baseline") if isinstance(summary.get("baseline"), dict) else summary
    return {
        "value": _find_num(summary, ("lastNightAvg", "lastNightAverage")),
        "status": summary.get("status"),
        "weeklyAvg": _find_num(summary, ("weeklyAvg", "weeklyAverage")),
        "baselineLow": _find_num(baseline, ("balancedLow", "baselineLow", "lowUpper")),
        "baselineHigh": _find_num(baseline, ("balancedUpper", "baselineHigh", "balancedUpperLimit")),
    }


def _percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    position = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[position]


def _sleep_need_hours(nights, age):
    """A chosen target, not a physiological need inferred from restricted sleep."""
    achieved = _percentile([n["hours"] for n in nights if n.get("hours")], 0.8)
    return 7.5, _recommended_sleep_hours(age), achieved


_SLEEP_LOG_LOCK = threading.Lock()


def _sleep_log_path():
    return Path(os.environ.get("SLEEP_LOG_PATH") or (_injury_measurements_path().parent / "sleep_log.json"))


def _sleep_log():
    db = _dashboard_database()
    if db:
        return db.sleep_log()
    path = _sleep_log_path()
    if not path.exists():
        return {"targetHours": 7.5, "entries": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_sleep_log(payload, today=None):
    """Upsert one day's manual inputs; never modify Garmin's sleep record."""
    import math
    today = today or datetime.date.today()
    if not isinstance(payload, dict):
        raise ValueError("Enter sleep settings or a daily sleep entry.")
    def number(value, low, high, label):
        if isinstance(value, bool):
            raise ValueError(label + " must be a number.")
        try:
            value = float(value)
        except (ValueError, TypeError):
            raise ValueError(label + " must be a number.")
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{label} must be between {low} and {high}.")
        return value
    target = number(payload['targetHours'], 4, 12, 'Nightly target') if 'targetHours' in payload else None
    date, entry = None, None
    if 'date' in payload:
        try:
            date = datetime.date.fromisoformat(payload['date']).isoformat()
        except (ValueError, TypeError):
            raise ValueError("Choose a valid date.")
        if date > today.isoformat():
            raise ValueError("Sleep entries cannot be in the future.")
        nap = number(payload.get('napMinutes', 0), 0, 720, 'Nap minutes')
        if not nap.is_integer():
            raise ValueError("Enter whole nap minutes.")
        hours = payload.get('nightHours')
        hours = None if hours in (None, '') else number(hours, 0, 24, 'Night sleep hours')
        if hours is not None and hours * 60 + nap > 1440:
            raise ValueError("Total sleep cannot exceed 24 hours in a day.")
        notes = payload.get('notes', '')
        if not isinstance(notes, str) or len(notes) > 2000:
            raise ValueError("Notes must be text, up to 2,000 characters.")
        entry = {'napMinutes': int(nap), 'nightHours': hours, 'notes': notes.strip()}
    if target is None and date is None:
        raise ValueError("No sleep changes supplied.")
    with _SLEEP_LOG_LOCK:
        db = _dashboard_database()
        if db:
            db.save_sleep_log(target, date, entry)
        else:
            data = _sleep_log()
            if target is not None:
                data['targetHours'] = target
            if date is not None:
                data['entries'][date] = entry
            path = _sleep_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data), encoding='utf-8')
            temporary.replace(path)
    try:
        _recommendation_cache_path().unlink(missing_ok=True)
    except OSError:
        pass
    return _sleep_log()


def _sleep_debt(nights, need_hours, today, entries=None):
    """Seven-day sum of daily shortfalls; no interest or surplus repayment."""
    entries = entries or {}
    by_date = {night['date']: night for night in nights}
    def daily(day):
        date = day.isoformat()
        night, manual = by_date.get(date, {}), entries.get(date, {})
        hours = manual.get('nightHours')
        source = 'manual' if hours is not None else 'Garmin'
        if hours is None:
            seconds = night.get('seconds')
            hours = seconds / 3600 if seconds is not None else night.get('hours')
        nap = manual.get('napMinutes', 0)
        shortfall = max(0, need_hours * 60 - hours * 60 - nap) if hours is not None else None
        return {'date': date, 'label': day.strftime('%b %d'), 'nightHours': hours,
                'napMinutes': nap, 'shortfallMinutes': round(shortfall) if shortfall is not None else None,
                'source': source if hours is not None else 'missing', 'notes': manual.get('notes', '')}
    series = []
    for offset in range(6, -1, -1):
        day = today - datetime.timedelta(days=offset)
        rows = [daily(day - datetime.timedelta(days=i)) for i in range(6, -1, -1)]
        known = [row for row in rows if row['shortfallMinutes'] is not None]
        series.append({'date': day.isoformat(), 'label': day.strftime('%b %d'),
                       'minutes': sum(row['shortfallMinutes'] for row in known) if known else None,
                       'recordedDays': len(known)})
    rows = [daily(today - datetime.timedelta(days=i)) for i in range(6, -1, -1)]
    known = [row for row in rows if row['shortfallMinutes'] is not None]
    total = series[-1]['minutes']
    return {'minutes': total, 'level': 'none' if total == 0 else 'low' if total is not None and total < 240 else 'moderate' if total is not None and total < 600 else 'high' if total is not None else 'unknown',
            'series': series, 'bands': [180, 540, 900], 'windowDays': 7,
            'recordedDays': len(known), 'missingDays': 7 - len(known), 'days': rows,
            'napMinutes': sum(row['napMinutes'] for row in known),
            'averageHours': round(sum(row['nightHours'] + row['napMinutes']/60 for row in known)/len(known), 2) if known else None}


def _sleep_regularity(nights):
    """Consistency of mid-sleep time, the strongest circadian signal we have."""
    points = []
    for night in nights[-SLEEP_CONTEXT_DAYS:]:
        bed, wake = night.get("bedTime"), night.get("wakeTime")
        if bed is None or wake is None:
            continue
        # Shift the evening half of the clock negative so a 23:30 bedtime and a
        # 00:30 one are an hour apart rather than twenty-three.
        bed_adjusted = bed - 24 if bed > 12 else bed
        points.append((bed_adjusted + (bed_adjusted + night["hours"])) / 2.0)
    if len(points) < 3:
        return None
    mean = sum(points) / len(points)
    deviation = (sum((value - mean) ** 2 for value in points) / len(points)) ** 0.5
    # An hour of typical drift costs roughly 25 points.
    return {"score": max(0, min(100, round(100 - deviation * 25))),
            "deviationMinutes": round(deviation * 60)}


def _grade(value, optimal, good):
    """Oura-style three-step qualifier for a contributor bar."""
    if value is None:
        return None
    if value >= optimal:
        return "optimal"
    return "good" if value >= good else "attention"


def _titlecase(value):
    text = str(value or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:].lower() if text else None


def _recovery_metrics(sleep_series, hrv_series, rhr_series, wellness, effort, age, today, sleep_log=None):
    """Assemble the readiness contributors and the derived sleep figures."""
    need_hours, guideline_hours, achieved_hours = _sleep_need_hours(sleep_series, age)
    sleep_log = sleep_log or {"targetHours": 7.5, "entries": {}}
    need_hours = sleep_log["targetHours"]
    debt = _sleep_debt(sleep_series, need_hours, today, sleep_log["entries"])
    regularity = _sleep_regularity(sleep_series)
    last_night = wellness.get("sleep") or {}
    hrv_today = wellness.get("hrv") or {}
    recent_nights = sleep_series[-SLEEP_CONTEXT_DAYS:]
    balance_hours = sum(night["hours"] for night in recent_nights)
    balance_pct = (round(balance_hours / (need_hours * len(recent_nights)) * 100)
                   if recent_nights else None)

    rhr_values = [row["value"] for row in rhr_series]
    rhr_baseline = round(sum(rhr_values) / len(rhr_values)) if rhr_values else None
    rhr_today = rhr_values[-1] if rhr_values else (wellness.get("restingHr") or {}).get("value")
    # Below baseline is the good direction for resting heart rate, so this grade
    # is inverted relative to the others: each beat above baseline costs 8.
    rhr_pct = (max(0, min(100, round(100 - (rhr_today - rhr_baseline) * 8)))
               if rhr_today is not None and rhr_baseline else None)

    # Garmin derives HRV status from the seven-day average against the personal
    # baseline, so the contributor reports that figure rather than a single
    # night, which can sit outside the band without the status changing.
    hrv_week = hrv_today.get("weeklyAvg")
    if hrv_week is None:
        recent = [row["value"] for row in hrv_series[-7:] if row.get("value") is not None]
        hrv_week = round(sum(recent) / len(recent)) if recent else hrv_today.get("value")
    hrv_pct = {"balanced": 92, "unbalanced": 55,
               "low": 35, "poor": 30}.get((hrv_today.get("status") or "").lower())

    yesterday_effort = next((day.get("effort") for day in reversed(effort.get("days") or [])
                             if day.get("effort") is not None), None)
    capacity = effort.get("baseline")
    previous_day_pct = None
    if yesterday_effort is not None and capacity:
        # A day near a seventh of weekly capacity is unremarkable; well above it
        # is the "pay attention" case Oura flags after a hard session.
        typical = capacity / 7.0
        previous_day_pct = round(max(0.0, min(1.0, 1 - (yesterday_effort / typical - 1) / 3.0)) * 100)
    activity_pct = {"within": 92, "below": 65, "above": 55}.get(effort.get("state"))

    contributors = [
        {"key": "restingHr", "label": "Resting heart rate", "percent": rhr_pct,
         "detail": f"{rhr_today} bpm" if rhr_today else None,
         "note": f"baseline {rhr_baseline} bpm" if rhr_baseline else None},
        {"key": "hrvBalance", "label": "HRV balance", "percent": hrv_pct,
         "detail": _titlecase(hrv_today.get("status")),
         "note": f"{hrv_week} ms 7-day average" if hrv_week else None},
        {"key": "sleep", "label": "Sleep", "percent": last_night.get("score"),
         "detail": f"{last_night.get('hours')} h" if last_night.get("hours") else None,
         "note": f"sleep score {last_night.get('score')}" if last_night.get("score") else None},
        {"key": "sleepBalance", "label": "Sleep balance", "percent": balance_pct,
         "detail": f"{round(balance_hours)} h over {len(recent_nights)} nights" if recent_nights else None,
         "note": f"need {need_hours} h a night"},
        {"key": "sleepRegularity", "label": "Sleep regularity",
         "percent": (regularity or {}).get("score"),
         "detail": f"±{regularity['deviationMinutes']} min" if regularity else None,
         "note": "drift in mid-sleep time"},
        {"key": "previousDay", "label": "Previous day activity", "percent": previous_day_pct,
         "detail": f"{yesterday_effort} effort points" if yesterday_effort is not None else None,
         "note": "yesterday against a typical day"},
        {"key": "activityBalance", "label": "Activity balance", "percent": activity_pct,
         "detail": {"within": "In range", "below": "Under range",
                    "above": "Over range"}.get(effort.get("state")),
         "note": "this week against your effort band"},
    ]
    for item in contributors:
        item["grade"] = _grade(item["percent"], 85, 70)

    return {
        "contributors": [item for item in contributors if item["percent"] is not None],
        "sleepNeedHours": need_hours,
        "sleepGuidelineHours": guideline_hours,
        "sleepAchievedHours": achieved_hours,
        "age": age,
        "debt": debt,
        "regularity": regularity,
        "rhrBaseline": rhr_baseline,
        "lastNight": last_night or None,
        "nights": recent_nights,
        "sleepLog": sleep_log,
        "needModel": "",
        "debtModel": "Weekly estimate = sum of max(0, target − night sleep − logged naps) over the last 7 calendar days. Longer nights do not erase another day's shortfall. Missing nights are excluded; coverage is shown. Naps are entered manually, not imported from Garmin. This tracks sleep quantity, not complete physiological recovery.",
        "missing": "Body temperature is absent: Garmin Connect exposes no skin-temperature deviation.",
    }


# Treadmill sessions at or above this average HR count as runs; below it they
# are treated as walks (the user walks around 80–100 bpm and runs 125+).
TREADMILL_RUN_HR = 120

# Aerobic effort points per minute, by average heart rate, for activities that
# carry no time-in-zone breakdown. Fitted to the 379 recorded activities that do
# carry one: on a 30% holdout this reproduces 95% of their aerobic total, with a
# median error of 0.5 points on a 12-point activity.
_AEROBIC_RATE_BY_HR = ((90, 0.125), (100, 0.132), (110, 0.149), (120, 0.186),
                       (130, 0.261), (140, 0.361), (150, 0.489), (165, 0.838))


def _aerobic_rate(avg_hr):
    """Interpolate the per-minute aerobic rate for an average heart rate."""
    if avg_hr is None:
        return 0.0
    if avg_hr <= _AEROBIC_RATE_BY_HR[0][0]:
        return _AEROBIC_RATE_BY_HR[0][1]
    if avg_hr >= _AEROBIC_RATE_BY_HR[-1][0]:
        return _AEROBIC_RATE_BY_HR[-1][1]
    for (low_hr, low_rate), (high_hr, high_rate) in zip(_AEROBIC_RATE_BY_HR,
                                                        _AEROBIC_RATE_BY_HR[1:]):
        if low_hr <= avg_hr <= high_hr:
            return low_rate + (high_rate - low_rate) * (avg_hr - low_hr) / (high_hr - low_hr)
    return _AEROBIC_RATE_BY_HR[-1][1]

# Relative Effort capacity response, in weeks, and the per-week decay applied to
# the trailing deviation that widens the top of the suggested range.
CAPACITY_RISE_WEEKS = 6.0
CAPACITY_FALL_WEEKS = 3.0
SIGMA_DECAY = 0.7


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


def _is_strength_type(type_key):
    key = (type_key or "").lower()
    return key == "strength_training" or "strength" in key


def _map_activity(a):
    at = (a.get("activityType") or {}).get("typeKey")
    sport = _sport_of(at)
    dur = a.get("duration") or 0
    dist = a.get("distance") or 0
    km = round(dist / 1000.0, 2)
    mins = round(dur / 60.0)
    avg_hr = _num(a.get("averageHR"))
    # The watch has no treadmill-walk profile, so treadmill sessions arrive as
    # "running" whether they were walks or runs; average HR separates the two.
    if "treadmill" in (at or "").lower() and avg_hr is not None and avg_hr < TREADMILL_RUN_HR:
        sport = "walk"
    pace = None
    if km > 0 and dur > 0:
        pace = round((dur / 60.0) / km, 2)
    zones = [round((a.get("hrTimeInZone_" + str(i)) or 0) / 60.0, 1) for i in range(1, 6)]
    load = round(a.get("activityTrainingLoad") or 0, 1)
    # Strava-comparable Relative Effort, sport-agnostic. Two additive parts:
    # an aerobic-volume term where every active minute earns by HR zone (time
    # below zone 1 keeps a small floor so long walks and easy spins register),
    # plus Garmin's EPOC-based Training Load ÷ 8, which restores the short
    # hard intervals that zone buckets flatten out. Weights calibrated against
    # Strava Relative Effort over the Aug–Sep 2026 reference weeks.
    if sum(zones) > 0:
        easy_minutes = max(0.0, dur / 60.0 - sum(zones))
        zone_part = (easy_minutes * 1.2 + sum(
            minutes * weight for minutes, weight in zip(zones, (2, 4, 8, 14, 22)))) / 10.0
    else:
        # Devices before roughly 2025 recorded no time-in-zone breakdown. Every
        # minute would otherwise fall to the below-zone-1 floor, scoring a hard
        # session as if it were a stroll and halving the effort for whole years
        # of history. Estimate the aerobic term from average heart rate instead.
        zone_part = dur / 60.0 * _aerobic_rate(avg_hr) if avg_hr else 0.0
    load_part = load / 8.0
    has_hr = avg_hr is not None or sum(zones) > 0
    effort = round(zone_part + load_part, 1) if has_hr else None
    return {
        "activityId": a.get("activityId"),
        "sport": sport,
        "typeKey": at,
        "isStrength": _is_strength_type(at),
        "name": a.get("activityName") or "Activity",
        "date": (a.get("startTimeLocal") or "")[:10],
        "start": a.get("startTimeLocal"),
        "km": km,
        "min": mins,
        "hr": avg_hr,
        "maxHr": _num(a.get("maxHR")),
        "cal": _num(a.get("calories")),
        "pace": pace,
        "load": load,
        "zones": zones,
        "effort": effort,
        "effortZonePart": round(zone_part, 1) if has_hr else None,
        "effortLoadPart": round(load_part, 1) if has_hr else None,
        "location": a.get("locationName"),
        "totalSets": _num(a.get("totalSets")),
        "totalReps": _num(a.get("totalReps")),
        "totalVolumeGrams": _num(a.get("totalVolume")),
    }


def _agg(items):
    return {
        "sessions": len(items),
        "km": round(sum(x["km"] for x in items), 1),
        "min": round(sum(x["min"] for x in items)),
        "cal": round(sum((x["cal"] or 0) for x in items)),
    }


# The chart shows two years, but the Fitness average needs a long run-up
# before that window opens or its oldest points are still climbing out of
# their seed. Measured against this athlete's history, 1100 days settles the
# two-year point (it moved the two-year change from +31% to +16%); going
# further changed nothing.
HISTORY_DAYS = 1100
VISIBLE_DAYS = 731  # enough for two calendar years spanning a leap day

# Enough pages to reach that far even at several activities a day. Only a
# safety stop: the loop normally exits as soon as it passes the cutoff.
HISTORY_ACTIVITY_LIMIT = 4000


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
    # Page back until the cutoff is genuinely reached. The previous 1000-activity
    # ceiling was hit long before the cutoff for anyone training most days, and it
    # truncated silently: Fitness then started part-way through the window, so
    # the oldest points sat near their seed and every long-period percentage was
    # computed against a value that was too low.
    cutoff, gathered = today - datetime.timedelta(days=HISTORY_DAYS), []
    for start in range(0, HISTORY_ACTIVITY_LIMIT, 100):
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


# Fitness-only calibration from paired activity/weekly Strava readings (Sep 2026).
# These are empirical coefficients, not Strava's published formula. Preserve
# the existing Relative Effort scores and capacity band; only Fitness receives
# this intensity adjustment and the agreed 20.3% scale increase.
FITNESS_DAYS = 42.0
FATIGUE_DAYS = 7.0
FITNESS_SEED_DAYS = 42
FITNESS_BASE_WEIGHT = 0.8481948641500818
FITNESS_INTENSITY_WEIGHT = 0.8276011107352337
FITNESS_SCALE = 1.203  # small refinement against the Sep 16, 2026 reference


def _fitness_effort(activity, effort):
    """Calibrate existing zone-based effort for Fitness without mutating it.

    Average HR modulates the existing effort; it does not replace recorded
    zone durations. Missing HR gets the baseline weight. The coefficients
    were fitted to 12 weekly totals and two hard runs, with a recent run held
    out. This remains an approximation, especially for intervals.
    """
    hr = _num(activity.get("hr"))
    intensity = max(0.0, ((hr if hr is not None else 130.0) - 130.0) / 30.0)
    return effort * (FITNESS_BASE_WEIGHT + FITNESS_INTENSITY_WEIGHT * intensity) * FITNESS_SCALE


def _training_history(activities, today):
    """Build calibrated Fitness alongside unchanged daily Relative Effort.

    Fatigue retains its existing seven-day effort average. Form is the
    difference between the displayed Fitness and Fatigue values.
    """
    daily = {}
    for activity in activities:
        try:
            date = datetime.datetime.strptime(activity["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if date > today or (today - date).days > HISTORY_DAYS:
            continue
        entry = daily.setdefault(date, {"garminLoad": 0.0, "effort": 0.0, "fitnessEffort": 0.0})
        garmin_load = activity.get("load") or 0
        entry["garminLoad"] += garmin_load
        effort = activity.get("effort") or round(garmin_load / 3.0, 1)
        entry["effort"] += effort
        entry["fitnessEffort"] += _fitness_effort(activity, effort)
    start = max(min(daily) if daily else today, today - datetime.timedelta(days=HISTORY_DAYS))
    span = (today - start).days + 1

    def effort_on(offset, key="effort"):
        return (daily.get(start + datetime.timedelta(days=offset), {}) or {}).get(key) or 0.0

    # Seed both averages with the opening weeks' mean daily effort. Starting
    # from zero would leave the oldest visible points still climbing out of the
    # warm-up, which understated them and inflated every long-period gain.
    seed_days = min(FITNESS_SEED_DAYS, span)
    seed = sum(effort_on(offset) for offset in range(seed_days)) / seed_days if seed_days else 0.0
    fatigue = seed
    fitness = sum(effort_on(offset, "fitnessEffort") for offset in range(seed_days)) / seed_days if seed_days else 0.0
    series = []
    for offset in range(span):
        date = start + datetime.timedelta(days=offset)
        entry = daily.get(date, {})
        load = entry.get("effort") or 0.0
        fitness += ((entry.get("fitnessEffort") or 0.0) - fitness) / FITNESS_DAYS
        fatigue += (load - fatigue) / FATIGUE_DAYS
        # "%b %-d" is a POSIX-only directive that raises on Windows, so the day
        # number is interpolated directly to keep the module importable there.
        series.append({"date": date.isoformat(), "label": f"{date:%b} {date.day}",
                       "load": round(load, 1), "effort": round(load, 1),
                       "garminLoad": round(entry.get("garminLoad") or 0, 1),
                       "fitness": round(fitness, 6), "fatigue": round(fatigue, 1),
                       "form": round(fitness - fatigue, 1)})
    visible_start = (today - datetime.timedelta(days=VISIBLE_DAYS)).isoformat()
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
    for i in range(6, -1, -1):
        day = today - datetime.timedelta(days=i)
        day_s = day.isoformat()
        day_stats = stats if day_s == stats_date else _call(client.get_stats, day_s)
        if isinstance(day_stats, dict):
            daily_stats.append(day_stats)

    # ---- sleep, HRV and resting-HR history ----
    # Range endpoints replace what used to be two calls per day: sixty nights
    # now cost three requests instead of a hundred and twenty, which is what
    # makes the baseline-relative metrics below affordable.
    history_start = (today - datetime.timedelta(days=SLEEP_HISTORY_DAYS - 1)).isoformat()
    sleep_rows = _call(client.get_sleep_daily, history_start, ds) or []
    sleep_series = [night for night in (_sleep_night(row) for row in sleep_rows) if night]
    if not sleep_series:  # older accounts without the stats endpoint
        for i in range(SLEEP_CONTEXT_DAYS - 1, -1, -1):
            day = today - datetime.timedelta(days=i)
            night = _sleep_night(_call(client.get_sleep_data, day.isoformat()))
            if night:
                sleep_series.append(night)
    sleep_series.sort(key=lambda night: night["date"])
    for night in sleep_series:
        night["label"] = datetime.date.fromisoformat(night["date"]).strftime("%a")

    # Last night's full payload carries detail the daily summaries omit.
    detailed = _sleep_night(_call(client.get_sleep_data, ds))
    if detailed and sleep_series and sleep_series[-1]["date"] == detailed["date"]:
        detailed["label"] = sleep_series[-1]["label"]
        sleep_series[-1] = detailed

    hrv_range = _call(client.get_hrv_data_range, history_start, ds)
    hrv_rows = (hrv_range or {}).get("hrvSummaries") or (hrv_range if isinstance(hrv_range, list) else [])
    hrv_series = []
    for row in hrv_rows:
        values = _hrv_values(row)
        date = (row or {}).get("calendarDate") if isinstance(row, dict) else None
        if date and (values["value"] is not None or values["status"]):
            values["date"] = str(date)[:10]
            values["label"] = datetime.date.fromisoformat(values["date"]).strftime("%a")
            hrv_series.append(values)
    if not hrv_series:  # range endpoint unavailable: fall back to recent days
        for i in range(SLEEP_CONTEXT_DAYS - 1, -1, -1):
            day = today - datetime.timedelta(days=i)
            values = _hrv_values(_call(client.get_hrv_data, day.isoformat()))
            if values["value"] is not None or values["status"]:
                values.update({"date": day.isoformat(), "label": day.strftime("%a")})
                hrv_series.append(values)
    hrv_series.sort(key=lambda row: row["date"])

    rhr_series = []
    for row in _call(client.get_rhr_daily, history_start, ds) or []:
        value = _find_num(row, ("value", "restingHeartRate")) if isinstance(row, dict) else None
        date = row.get("calendarDate") if isinstance(row, dict) else None
        if value and date:
            rhr_series.append({"date": str(date)[:10], "value": round(value)})
    rhr_series.sort(key=lambda row: row["date"])

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
    out["rhrSeries"] = rhr_series
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
    with _STRENGTH_LOG_LOCK:
        manual_entries = list(_manual_strength_workouts_unlocked().values())
        manual_endurance_entries = list(_manual_endurance_activities_unlocked().values())
    manual_activities = [_manual_workout_activity(entry, acts) for entry in manual_entries if isinstance(entry, dict)]
    manual_endurance = [_manual_endurance_activity(entry) for entry in manual_endurance_entries if isinstance(entry, dict)]
    manual_steps_by_date = {}
    for entry in manual_endurance_entries:
        if isinstance(entry, dict):
            date = str(entry.get("activityStart") or "")[:10]
            manual_steps_by_date[date] = manual_steps_by_date.get(date, 0) + _manual_endurance_steps(entry)
    manual_steps_on_stats_date = manual_steps_by_date.get(stats_date, 0)
    if manual_steps_on_stats_date:
        w["steps"]["value"] = (w["steps"].get("value") or 0) + manual_steps_on_stats_date
        w["steps"]["manualEstimated"] = manual_steps_on_stats_date
    merged_garmin_ids = {str(entry.get("mergedGarminActivityId")) for entry in manual_entries if entry.get("source") == "merged"}
    acts = [row for row in acts if str(row.get("activityId")) not in merged_garmin_ids]
    acts = sorted(acts + manual_activities + manual_endurance, key=lambda row: row.get("start") or "", reverse=True)
    out["recent"] = acts[:12]
    strength_history = _strength_summary(limit=200)
    out["strength"] = {"activities": strength_history["activities"][:8]}
    history = [row for row in _history_activities(client, today) if str(row.get("activityId")) not in merged_garmin_ids] + manual_activities + manual_endurance
    out["fitnessSeries"] = _training_history(history, today)
    muscle_start = today - datetime.timedelta(days=today.weekday(), weeks=11)
    daily_steps = list(_call(client.get_daily_steps, muscle_start.isoformat(), ds) or daily_stats)
    daily_steps.extend({"calendarDate": date, "totalSteps": steps} for date, steps in manual_steps_by_date.items())
    out["muscleVolume"] = _muscle_volume_weeks(
        strength_history, history, daily_steps, daily_stats, today,
    )

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
             "effort": activity.get("effort") or round((activity.get("load") or 0) / 3.0, 1),
             "garminLoad": activity.get("load"), "hr": activity.get("hr"),
             "sport": activity.get("sport"),
             "zonePart": activity.get("effortZonePart"), "loadPart": activity.get("effortLoadPart")}
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
    capacity, seed, prior_efforts = None, [], []
    for index, week in enumerate(weekly_effort):
        effort = week["effort"]
        is_current = index == len(weekly_effort) - 1
        if capacity is None:
            if effort > 0:
                seed.append(effort)
            if not is_current:
                prior_efforts.append(effort)
            week.update({"rangeLow": None, "rangeHigh": None, "state": "building", "capacity": None})
            if len(seed) >= 4:
                capacity = sum(seed[-4:]) / 4.0
            continue
        # The band floor tracks capacity, but the ceiling also widens with the
        # volatility of the trailing six completed weeks (Strava-like): erratic
        # recent training stretches the acceptable top end, while consistent
        # weeks keep the band at the plain 80-130% of capacity. The deviation is
        # exponentially weighted so a spike widens the band sharply and then
        # relaxes week by week, instead of holding full width for six weeks and
        # collapsing the moment it leaves the window.
        recent = prior_efforts[-6:]
        sigma = 0.0
        if len(recent) >= 3:
            weights = [SIGMA_DECAY ** (len(recent) - 1 - offset) for offset in range(len(recent))]
            total = sum(weights)
            mean = sum(value * weight for value, weight in zip(recent, weights)) / total
            sigma = (sum(weight * (value - mean) ** 2
                         for value, weight in zip(recent, weights)) / total) ** 0.5
        low_raw = capacity * 0.8
        high_raw = min(capacity * 2.5, max(capacity * 1.3, capacity + 1.3 * sigma))
        state = "below" if effort < low_raw else "above" if effort > high_raw else "within"
        week.update({"rangeLow": round(low_raw), "rangeHigh": round(high_raw),
                     "state": state, "capacity": round(capacity, 1)})
        if is_current:
            # Judge the in-progress week against a day-prorated band so Monday
            # isn't flagged "below range" for lacking a full week of training.
            days_elapsed = min(7, max(1, (today - datetime.date.fromisoformat(week["start"])).days + 1))
            if days_elapsed < 7:
                fraction = days_elapsed / 7.0
                week.update({
                    "partial": True, "daysElapsed": days_elapsed,
                    "projected": round(effort / fraction),
                    "state": ("below" if effort < low_raw * fraction
                              else "above" if effort > high_raw * fraction else "within"),
                })
        if not is_current:  # current partial week cannot set its own target
            prior_efforts.append(effort)
            # Asymmetric response: capacity is slow to claim new fitness but
            # gives it up faster, so a quiet block settles the band onto the
            # training actually being done instead of trailing months behind it.
            response = CAPACITY_FALL_WEEKS if state == "below" else CAPACITY_RISE_WEEKS
            blended = capacity + (effort - capacity) / response
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
        "formula": "Each activity scores aerobic minutes by HR zone (below Z1 0.12 · Z1 0.2 · Z2 0.4 · Z3 0.8 · Z4 1.4 · Z5 2.2 points/min) plus Garmin Training Load ÷ 8 for interval intensity — no sport multipliers, so the points are comparable with Strava Relative Effort. Garmin Load ÷ 3 when no heart rate was recorded.",
        "rangeModel": "The shaded band is the suggested weekly range. Its floor is 80% of adaptive capacity, which climbs over ~6 weeks after weeks inside or above the band but eases back over ~3 when you train under it, so the band follows a quieter block instead of trailing months behind it. The ceiling is 130% of capacity, stretched further when recent weeks were erratic (capacity + 1.3× the recency-weighted deviation of the last six weeks, capped at 2.5× capacity) — a big week widens the top sharply, then relaxes week by week. The current week is judged against the band prorated by days elapsed.",
    }

    # Readiness contributors depend on the effort band above, so they are built
    # once it exists rather than alongside the raw sleep history.
    out["recovery"] = _recovery_metrics(sleep_series, hrv_series, rhr_series, w,
                                        out["relativeEffort"], age, today, _sleep_log())

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
    strength_acts = [a for a in gym_acts if a.get("isStrength")]
    type_counts = {}
    for activity in gym_acts:
        label = activity.get("name") or (activity.get("typeKey") or "Workout").replace("_", " ").title()
        type_counts[label] = type_counts.get(label, 0) + 1
    out["workouts"] = {
        "hasData": bool(gym_acts),
        "week": _agg([a for a in gym_acts if within(a["date"], 7)]),
        "month": _agg([a for a in gym_acts if within(a["date"], 30)]),
        "last": gym_acts[0] if gym_acts else None,
        "lastStrength": strength_acts[0] if strength_acts else None,
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

    async def sleep_log(request):
        try:
            data = _save_sleep_log(await request.json()) if request.method == "POST" else _sleep_log()
            return JSONResponse(data)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def body_measurements(request):
        if request.method == "GET":
            return JSONResponse(_body_measurements())
        try:
            _append_body_measurement(await request.json())
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(_body_measurements(), status_code=201)

    async def injury_settings(request):
        try:
            definitions = _save_injury_definitions(await request.json()) if request.method == "POST" else _injury_definitions()
            if request.method == "POST":
                try:
                    _recommendation_cache_path().unlink(missing_ok=True)
                except OSError:
                    pass
            return JSONResponse({"definitions": definitions})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def injury_measurements(request):
        if request.method == "GET":
            return JSONResponse(_injury_measurements())
        try:
            _append_injury_measurement(await request.json())
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(_injury_measurements(), status_code=201)

    async def strength_activity(request):
        if client is None:
            return JSONResponse({"error": "garmin client not ready"}, status_code=503)
        try:
            activity_id = _strength_activity_id(request.path_params.get("activity_id"))
            if request.method == "POST":
                try:
                    current_sets = _normalise_garmin_strength_sets(client.get_activity_exercise_sets(activity_id))
                except Exception:  # noqa: BLE001 - saved/browser snapshot supports offline correction
                    current_sets = None
                _save_strength_activity(activity_id, await request.json(), current_sets)
            return JSONResponse(_strength_activity_payload(client, activity_id), status_code=201 if request.method == "POST" else 200)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def manual_strength_workout(request):
        try:
            manual_id = request.path_params.get("manual_id")
            if request.method == "DELETE":
                deleted = _delete_manual_activity(manual_id, "strength")
                return JSONResponse({"deleted": deleted}, status_code=200 if deleted else 404)
            if request.method == "GET":
                return JSONResponse(_manual_strength_payload(manual_id))
            return JSONResponse(_save_manual_strength_workout(manual_id, await request.json()), status_code=201)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def create_manual_strength_workout(request):
        try:
            manual_id = "manual-" + uuid.uuid4().hex
            return JSONResponse(_save_manual_strength_workout(manual_id, await request.json()), status_code=201)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def delete_manual_endurance_activity(request):
        deleted = _delete_manual_activity(request.path_params.get("manual_id"), "endurance")
        return JSONResponse({"deleted": deleted}, status_code=200 if deleted else 404)

    async def strength_exercises(_request):
        return JSONResponse({"exercises": _strength_exercise_catalog()})

    async def create_manual_endurance_activity(request):
        try:
            return JSONResponse(_save_manual_endurance_activity("manual-endurance-" + uuid.uuid4().hex, await request.json()), status_code=201)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def manual_strength_candidates(request):
        if client is None:
            return JSONResponse({"error": "garmin client not ready"}, status_code=503)
        try:
            return JSONResponse({"candidates": _manual_merge_candidates(client, request.path_params.get("manual_id"))})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def merge_manual_strength_workout(request):
        if client is None:
            return JSONResponse({"error": "garmin client not ready"}, status_code=503)
        try:
            body = await request.json()
            entry = _merge_manual_strength_workout(client, request.path_params.get("manual_id"), body.get("garminActivityId"))
            return JSONResponse(entry)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

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
            print(f"garmin-mcp: recommendation request failed: {exc}")
            return JSONResponse({"error": str(exc)}, status_code=503)
        except requests.RequestException:
            return JSONResponse({"error": "The recommendation service is temporarily unavailable."}, status_code=503)

    async def favicon(_request):
        from starlette.responses import Response
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
               "<text y='.9em' font-size='90'>⌚</text></svg>")
        return Response(svg, media_type="image/svg+xml",
                        headers={"cache-control": "public, max-age=86400"})

    asgi_app.router.routes.append(Route("/api/sleep-log", sleep_log, methods=["GET", "POST"]))
    asgi_app.router.routes.append(Route("/api/dashboard", api, methods=["GET"]))
    asgi_app.router.routes.append(Route("/api/body-measurements", body_measurements, methods=["GET", "POST"]))
    asgi_app.router.routes.append(Route("/api/injury-settings", injury_settings, methods=["GET", "POST"]))
    asgi_app.router.routes.append(Route("/api/injury-measurements", injury_measurements, methods=["GET", "POST"]))
    asgi_app.router.routes.append(Route("/api/strength-activities/{activity_id:int}", strength_activity, methods=["GET", "POST"]))
    asgi_app.router.routes.append(Route("/api/strength-exercises", strength_exercises, methods=["GET"]))
    asgi_app.router.routes.append(Route("/api/manual-endurance-activities", create_manual_endurance_activity, methods=["POST"]))
    asgi_app.router.routes.append(Route("/api/manual-endurance-activities/{manual_id}", delete_manual_endurance_activity, methods=["DELETE"]))
    asgi_app.router.routes.append(Route("/api/manual-strength-workouts", create_manual_strength_workout, methods=["POST"]))
    asgi_app.router.routes.append(Route("/api/manual-strength-workouts/{manual_id}", manual_strength_workout, methods=["GET", "POST", "DELETE"]))
    asgi_app.router.routes.append(Route("/api/manual-strength-workouts/{manual_id}/candidates", manual_strength_candidates, methods=["GET"]))
    asgi_app.router.routes.append(Route("/api/manual-strength-workouts/{manual_id}/merge", merge_manual_strength_workout, methods=["POST"]))
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
.read .em{font-size:22px}.read p{margin:0;font-size:14.5px;flex:1;white-space:pre-line}.read b{color:var(--text)}.read button{align-self:flex-start;white-space:nowrap}.read button:disabled{opacity:.65;cursor:wait}
.grid{display:grid;gap:14px}
.hero{grid-template-columns:1.5fr 1fr 1fr}.topmetrics{grid-template-columns:repeat(3,1fr);margin-top:14px}.bodygrid{grid-template-columns:repeat(4,1fr);margin-top:14px}
.tri{grid-template-columns:repeat(3,1fr);margin-top:14px}
.stats{grid-template-columns:repeat(4,1fr);margin-top:14px}
@media(max-width:820px){.hero,.tri,.topmetrics{grid-template-columns:1fr 1fr}.stats,.bodygrid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:640px){.read{flex-wrap:wrap}.read .em{display:none}.read p{min-width:100%}.read button{margin-left:0}}
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
.loadchart,.effortdaily{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}.loadchart{width:100%;height:190px;display:block;margin-top:8px}.loadchart text,.effortdaily text{font-family:inherit!important;font-size:19px!important;font-weight:600;letter-spacing:.01em}.primarychart{height:300px;margin-top:12px}.keycharts{display:grid;grid-template-columns:1fr;gap:16px}.rangeband{fill:color-mix(in srgb,var(--accent) 18%,transparent)}.charttabs{display:flex;gap:6px;flex-wrap:wrap}.charttabs button{border:1px solid var(--border);background:var(--surface-2);color:var(--muted);border-radius:999px;padding:5px 9px;font-size:11px;font-weight:700;cursor:pointer}.charttabs button.active{background:var(--accent);border-color:var(--accent);color:#fff}.metricnote{font-size:12px;color:var(--muted);margin-top:8px}
.effortdetail{margin-top:14px;padding-top:13px;border-top:1px solid var(--border)}.effortdaily{width:100%;height:120px;display:block;margin-top:4px;cursor:crosshair}.effortlist{display:grid;gap:7px;margin-top:12px}.effortrow{display:flex;justify-content:space-between;gap:14px;padding:9px 11px;border-radius:10px;background:var(--surface-2);font-size:12.5px;color:var(--muted)}.effortrow b{color:var(--text)}.effortrow small{color:var(--faint);font-size:11.5px}.effortrow .score{white-space:nowrap;color:var(--accent);font-weight:750}.effortrow .score.hi{color:var(--warn)}.effortrow .score.max{color:var(--low)}.fitsummary{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-top:12px}.fitsummary .change{font-size:28px;font-weight:800}.fitsummary .up{color:var(--good)}.fitsummary .period{width:100%;color:var(--muted);font-size:12.5px}
.factor{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-top:9px;padding-top:9px;border-top:1px solid var(--border)}
.factor .fl{font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--faint);white-space:nowrap}
.factor .fv{font-size:12.5px;font-weight:700;text-align:right;color:var(--text)}
.factor .fv.optimal{color:var(--good)}.factor .fv.attention{color:var(--warn)}
.contrib{margin-top:13px}.contrib:first-of-type{margin-top:10px}
.contrib-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:5px}
.contrib-head .k{font-size:13px;font-weight:650}
.contrib-head .v{font-size:12.5px;font-weight:700;color:var(--muted);white-space:nowrap}
.contrib-head .v.optimal{color:var(--good)}.contrib-head .v.good{color:var(--muted)}.contrib-head .v.attention{color:var(--warn)}
.contrib-bar{display:block;height:6px;border-radius:999px;background:var(--track);overflow:hidden}
.contrib-bar i{display:block;height:100%;border-radius:999px;background:var(--muted)}
.contrib-bar i.optimal{background:var(--good)}.contrib-bar i.good{background:var(--accent)}.contrib-bar i.attention{background:var(--warn)}
.contrib-note{display:block;margin-top:4px;font-size:11px;color:var(--faint)}
.entry-actions{display:flex;justify-content:flex-end;margin-top:14px}.entry-form{display:none;margin-top:14px;padding:15px;border:1px solid var(--border);border-radius:12px;background:var(--surface-2)}.entry-form.open{display:block}.entry-fields{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.injury-fields{grid-template-columns:repeat(3,1fr)}.entry-fields label{display:grid;gap:4px;font-size:12px;font-weight:700;color:var(--muted)}.entry-fields input,.entry-fields select{width:100%;border:1px solid var(--border);border-radius:9px;padding:9px;background:var(--surface);color:var(--text);font:inherit}.entry-submit{margin-top:12px;border:0;border-radius:999px;background:var(--accent);color:white;padding:9px 14px;font-size:13px;font-weight:700;cursor:pointer}.entry-status{margin:9px 0 0;font-size:12px;color:var(--muted)}.injurychart{width:100%;height:300px;display:block;margin-top:10px}.painlegend{display:flex;flex-wrap:wrap;gap:7px 14px;margin-top:12px;font-size:12px;color:var(--muted)}.painlegend span{display:flex;align-items:center;gap:5px}.painlegend i{width:9px;height:9px;border-radius:50%;display:inline-block}.pain-scale{margin-top:12px;padding-top:10px;border-top:1px solid var(--border);font-size:12px;color:var(--muted)}
#injury-entry textarea{width:100%;box-sizing:border-box;resize:vertical;padding:12px;border:1px solid var(--border);border-radius:10px;background:var(--surface);color:var(--text);font:inherit}
.injury-settings-dialog{width:min(720px,94vw);max-height:85vh;overflow:auto;border:1px solid var(--border);border-radius:18px;padding:24px;background:var(--surface);color:var(--text)}.injury-settings-dialog::backdrop{background:rgba(0,0,0,.45)}.injury-setting{display:grid;grid-template-columns:minmax(140px,1fr) 72px auto;gap:12px;align-items:center;padding:14px 0;border-bottom:1px solid var(--border);margin-bottom:12px}.injury-setting [data-injury-state]{grid-column:1/-1;justify-self:start}.injury-settings-dialog .entry-actions{gap:10px}.injury-settings-dialog p{color:var(--muted)}
@media(max-width:640px){.entry-fields,.injury-fields{grid-template-columns:1fr 1fr}.injurychart{height:240px}.primarychart{height:250px}.loadchart text,.effortdaily text{font-size:21px!important}}@media(max-width:420px){.entry-fields,.injury-fields{grid-template-columns:1fr}}
.strength-open{display:inline-flex;align-items:center;margin-top:5px;border:0;background:transparent;color:var(--accent);padding:2px 0;font:inherit;font-size:11.5px;font-weight:700;cursor:pointer}.strength-card-action{margin-top:12px;width:100%;justify-content:center!important;box-shadow:none!important;background:var(--surface-2)!important}
.strength-modal[hidden]{display:none}.strength-modal{position:fixed;inset:0;z-index:50;background:rgba(5,12,22,.64);display:grid;place-items:center;padding:18px}.strength-dialog{width:min(720px,100%);max-height:calc(100dvh - 36px);display:flex;flex-direction:column;background:var(--bg);border:1px solid var(--border);border-radius:20px;box-shadow:0 24px 70px rgba(0,0,0,.35);overflow:hidden}.strength-dialog-head,.strength-dialog-foot{background:var(--surface);padding:14px 17px;display:flex;align-items:center;justify-content:space-between;gap:12px}.strength-dialog-head{border-bottom:1px solid var(--border)}.strength-dialog-head h2{font-size:18px;margin:0}.strength-dialog-head p{font-size:12px;color:var(--muted);margin:2px 0 0}.strength-dialog-foot{border-top:1px solid var(--border);justify-content:flex-end}.strength-dialog-body{padding:14px;overflow:auto;overscroll-behavior:contain}.strength-close{width:44px;height:44px;border:1px solid var(--border);border-radius:50%;background:var(--surface-2);color:var(--text);font-size:22px;cursor:pointer}.strength-save{border:0;border-radius:999px;background:var(--accent);color:#fff;padding:10px 16px;min-height:44px;font:inherit;font-weight:700;cursor:pointer}.strength-save:disabled{opacity:.65;cursor:wait}.strength-banner{padding:10px 12px;border-radius:11px;background:color-mix(in srgb,var(--accent) 12%,var(--surface));color:var(--muted);font-size:12.5px;margin-bottom:12px}.strength-banner.warn{background:color-mix(in srgb,var(--warn) 13%,var(--surface));color:var(--warn)}.strength-editor-title{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin:17px 2px 8px}.strength-editor-title:first-child{margin-top:0}.strength-editor-title h3{margin:0;font-size:14px}.strength-editor-title p{margin:2px 0 0;font-size:11.5px;color:var(--muted)}.strength-set-list,.strength-manual-list{display:grid;gap:8px}.strength-set-row,.strength-manual-group{background:var(--surface);border:1px solid var(--border);border-radius:13px;padding:11px}.strength-set-meta,.strength-manual-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px}.strength-set-meta b,.strength-manual-head b{font-size:12.5px}.strength-set-meta span,.strength-manual-head span{font-size:11px;color:var(--muted)}.strength-fields{display:grid;grid-template-columns:minmax(170px,1fr) 78px 82px 92px;gap:8px;align-items:end}.strength-field{display:grid;gap:4px;min-width:0;color:var(--muted);font-size:11px;font-weight:700}.strength-field input{width:100%;min-width:0;border:1px solid var(--border);border-radius:9px;padding:9px;background:var(--surface-2);color:var(--text);font:inherit;font-size:16px}.strength-field input.changed{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 7%,var(--surface))}.strength-row-actions{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-top:8px}.strength-per-side{display:flex;align-items:center;gap:7px;min-height:36px;font-size:12px;color:var(--muted);cursor:pointer}.strength-per-side input{width:18px;height:18px}.strength-apply,.strength-add,.strength-remove,.strength-copy{border:1px solid var(--border);border-radius:999px;background:var(--surface-2);color:var(--text);padding:7px 10px;font:inherit;font-size:11.5px;font-weight:700;cursor:pointer}.strength-add{min-height:40px}.strength-remove{border-color:transparent;background:transparent;color:var(--low)}.strength-original{font-size:11px;color:var(--faint);margin:6px 0 0}.strength-manual-sets{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.strength-manual-pair{min-width:0;padding:8px;border-radius:10px;background:var(--surface-2)}.strength-manual-set-head{display:flex;align-items:center;justify-content:space-between;gap:5px;margin-bottom:6px;font-size:11px}.strength-copy{padding:4px 7px;border-color:transparent;background:var(--surface);font-size:10px}.strength-manual-values{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.strength-manual-values .strength-field input{padding:8px 6px;background:var(--surface)}.strength-status{min-height:18px;margin:9px 2px 0;color:var(--muted);font-size:12px}.strength-empty{padding:18px;text-align:center;color:var(--muted);font-size:13px;background:var(--surface);border:1px dashed var(--border);border-radius:13px}.modal-open{overflow:hidden}
@media(max-width:640px){.strength-modal{padding:0}.strength-dialog{width:100%;max-height:100dvh;height:100dvh;border:0;border-radius:0}.strength-dialog-body{padding:12px}.strength-fields{grid-template-columns:repeat(3,minmax(0,1fr))}.strength-fields .strength-exercise{grid-column:1/-1}.strength-manual-sets{grid-template-columns:1fr}.strength-dialog-foot>*{flex:1}.strength-open{min-height:32px}}
@media(max-width:360px){.strength-fields{grid-template-columns:minmax(0,1fr) 64px 68px;gap:6px}.strength-set-row,.strength-manual-group{padding:9px}.strength-field input{padding:8px 6px}}
.muscle-card{margin-top:14px}.muscle-card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.muscle-card-head h3{font-size:15px;margin:0}.muscle-card-head p{font-size:11.5px;color:var(--muted);margin:2px 0 0}.muscle-week-nav{display:flex;align-items:center;gap:8px}.muscle-week-nav button{width:38px;height:38px;border:1px solid var(--border);border-radius:50%;background:var(--surface-2);color:var(--text);font-size:22px;line-height:1;cursor:pointer}.muscle-week-nav button:disabled{opacity:.35;cursor:default}.muscle-week-label{min-width:128px;text-align:center;font-size:12px;font-weight:750}.muscle-summary{font-size:12px;color:var(--muted);margin:10px 0 2px}.muscle-chart-wrap{width:100%;overflow-x:auto;overscroll-behavior-inline:contain}.musclechart{width:100%;height:300px;display:block}.muscle-scroll-hint{display:none}.muscle-legend{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:12px;color:var(--muted);margin:5px 0 0}.muscle-legend span{display:flex;align-items:center;gap:6px}.muscle-legend i{display:inline-block;width:11px;height:11px;border-radius:3px}.muscle-map{margin-top:13px;border-top:1px solid var(--border);padding-top:11px}.muscle-map summary{cursor:pointer;font-size:12.5px;font-weight:750;color:var(--text)}.muscle-table-wrap{overflow:auto;margin-top:9px}.muscle-table{width:100%;border-collapse:collapse;font-size:12px}.muscle-table th,.muscle-table td{text-align:left;padding:8px;border-bottom:1px solid var(--border);vertical-align:top}.muscle-table th{color:var(--faint);font-size:10.5px;text-transform:uppercase;letter-spacing:.05em}.muscle-table td:nth-child(2){font-weight:750;text-align:center}.muscle-credit{display:inline-block;margin:1px 4px 1px 0;padding:2px 6px;border-radius:999px;background:var(--surface-2);white-space:nowrap}.muscle-unmapped{color:var(--warn)}
@media(max-width:520px){.musclechart{width:720px;height:260px}.muscle-scroll-hint{display:block;margin:1px 0 5px;color:var(--faint);font-size:10.5px;text-align:right}.muscle-week-nav{width:100%;justify-content:space-between}.muscle-week-label{flex:1}.muscle-table{min-width:590px}}
.svg-tip{position:fixed;z-index:20;pointer-events:none;background:var(--text);color:var(--surface);padding:7px 9px;border-radius:8px;font-size:12px;line-height:1.35;box-shadow:var(--shadow);transform:translate(12px,-115%);white-space:nowrap}.svg-tip[hidden]{display:none}.bbchart,.trendchart,.loadchart{cursor:crosshair}
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
var CURRENT_DASHBOARD=null,STRENGTH_STATE=null,STRENGTH_DIRTY=false,MUSCLE_STATE=null,MUSCLE_WEEK_INDEX=0;
function css(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim();}
function el(t,c,h){var e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e;}
function n(x,f){return (x==null)?"—":(f?f(x):x);}
function comma(x){return x==null?"—":Math.round(x).toLocaleString();}
function esc(x){return String(x==null?"":x).replace(/[&<>"']/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function inputNumber(value){return value==null?"":String(value);}
function readStrengthNumber(input,label){var value=input.value.trim();if(value==="")return null;var number=Number(value);if(!Number.isFinite(number)||number<0)throw new Error(label+" must be zero or more");return number;}

function ensureStrengthModal(){
  var modal=document.getElementById("strength-modal");if(modal)return modal;
  modal=el("div","strength-modal");modal.id="strength-modal";modal.hidden=true;
  modal.innerHTML='<section class="strength-dialog" role="dialog" aria-modal="true" aria-labelledby="strength-dialog-title"><header class="strength-dialog-head"><div><h2 id="strength-dialog-title">Strength details</h2><p id="strength-dialog-meta"></p></div><button type="button" class="strength-close" data-strength-close aria-label="Close strength editor">×</button></header><div class="strength-dialog-body" id="strength-editor-content"></div><footer class="strength-dialog-foot"><button type="button" class="rf" data-strength-close>Close</button><button type="button" class="strength-save" id="strength-save">Save details</button></footer></section>';
  document.body.appendChild(modal);
  modal.querySelectorAll("[data-strength-close]").forEach(function(button){button.addEventListener("click",closeStrengthEditor);});
  modal.querySelector("#strength-save").addEventListener("click",saveStrengthDetails);
  modal.addEventListener("input",function(event){if(event.target.matches("input")){event.target.classList.add("changed");STRENGTH_DIRTY=true;setStrengthStatus("Unsaved changes");if(event.target.matches('[data-strength-field="perSide"]')){var label=event.target.closest("[data-strength-row]").querySelector("[data-strength-reps-label]");if(label)label.textContent=event.target.checked?"Reps/side":"Reps";}}});
    modal.addEventListener("click",function(event){
    var apply=event.target.closest("[data-strength-apply]");if(apply){applyExerciseToThree(apply.dataset.strengthApply);return;}
    var copy=event.target.closest("[data-manual-copy]");if(copy){copyPreviousManualSet(copy);return;}
    if(event.target.closest("[data-strength-add]")){addManualStrengthGroup();return;}
    var remove=event.target.closest("[data-strength-remove]");if(remove){remove.closest(".strength-manual-group").remove();STRENGTH_DIRTY=true;setStrengthStatus("Manual exercise removed — save to confirm");return;}
    if(event.target.closest("[data-strength-revert]")){revertGarminStrengthRows();}
    var merge=event.target.closest("[data-manual-merge]");if(merge){mergeManualStrengthWorkout(merge.dataset.manualMerge);}
  });
  return modal;
}

function setStrengthStatus(message,isError){var target=document.getElementById("strength-status");if(target){target.textContent=message||"";target.style.color=isError?"var(--low)":"";}}
function closeStrengthEditor(){
  var modal=document.getElementById("strength-modal");if(!modal)return;
  if(STRENGTH_DIRTY&&!window.confirm("Close without saving these strength changes?"))return;
  STRENGTH_DIRTY=false;modal.hidden=true;document.body.classList.remove("modal-open");
}

function strengthOriginalText(row){
  var parts=[];if(row.rawExercise)parts.push(row.rawExercise);parts.push(row.rawReps==null?"no reps":row.rawReps+" reps");if(row.rawDurationSeconds!=null)parts.push(row.rawDurationSeconds+" sec");parts.push(row.rawWeightKg==null?"no weight":row.rawWeightKg+" kg");return "Garmin captured: "+parts.join(" · ");
}

function strengthGarminRow(row,index){
  return '<div class="strength-set-row" data-strength-row data-set-id="'+esc(row.id)+'"><div class="strength-set-meta"><div><b>Set '+(index+1)+'</b> <span>'+esc(row.setType||"ACTIVE")+'</span></div><button type="button" class="strength-apply" data-strength-apply="'+esc(row.id)+'">Use for this + next 2</button></div><div class="strength-fields"><label class="strength-field strength-exercise">Exercise<input data-strength-field="exercise" list="strength-exercise-options" value="'+esc(row.exercise)+'" placeholder="Choose or type"></label><label class="strength-field"><span data-strength-reps-label>'+(row.perSide?"Reps/side":"Reps")+'</span><input data-strength-field="reps" type="number" inputmode="numeric" min="0" step="1" value="'+esc(inputNumber(row.reps))+'"></label><label class="strength-field">Seconds<input data-strength-field="durationSeconds" type="number" inputmode="decimal" min="0" max="86400" step="1" value="'+esc(inputNumber(row.durationSeconds))+'"></label><label class="strength-field">Weight · kg<input data-strength-field="weightKg" type="number" inputmode="decimal" min="0" max="1000" step="0.5" value="'+esc(inputNumber(row.weightKg))+'"></label></div><div class="strength-row-actions"><label class="strength-per-side"><input data-strength-field="perSide" type="checkbox" '+(row.perSide?"checked":"")+'> Reps/time are per side</label></div><p class="strength-original">'+esc(strengthOriginalText(row))+'</p></div>';
}

function manualGroupKey(row){var bits=String(row.id||"").split(":");return bits.length>2?bits.slice(0,-1).join(":"):String(row.id||"");}
function strengthManualGroup(rows,index){
  var first=rows[0]||{},lines=rows.slice();while(lines.length<3){lines.push({id:manualGroupKey(first)+":"+lines.length,reps:null,durationSeconds:null,weightKg:null});}
  return '<div class="strength-manual-group" data-manual-group><div class="strength-manual-head"><div><b>Manual exercise '+(index+1)+'</b> <span>not linked to a Garmin set</span></div><button type="button" class="strength-remove" data-strength-remove>Remove</button></div><label class="strength-field">Exercise<input data-manual-exercise list="strength-exercise-options" value="'+esc(first.exercise)+'" placeholder="Choose or type exercise"></label><div class="strength-row-actions"><label class="strength-per-side"><input data-manual-per-side type="checkbox" '+(first.perSide?"checked":"")+'> Reps/time are per side</label><span class="strength-original">Enter reps or seconds. For carries, use total load consistently (2 × 24 kg = 48 kg).</span></div><div class="strength-manual-sets">'+lines.map(function(row,setIndex){return '<div class="strength-manual-pair" data-manual-line data-set-id="'+esc(row.id)+'"><div class="strength-manual-set-head"><b>Set '+(setIndex+1)+'</b>'+(setIndex?'<button type="button" class="strength-copy" data-manual-copy aria-label="Copy repetitions, duration and weight from previous set">↳ Copy previous</button>':'')+'</div><div class="strength-manual-values"><label class="strength-field">Reps<input data-manual-reps type="number" inputmode="numeric" min="0" step="1" value="'+esc(inputNumber(row.reps))+'"></label><label class="strength-field">Seconds<input data-manual-duration type="number" inputmode="decimal" min="0" max="86400" step="1" value="'+esc(inputNumber(row.durationSeconds))+'"></label><label class="strength-field">kg<input data-manual-weight type="number" inputmode="decimal" min="0" max="1000" step="0.5" value="'+esc(inputNumber(row.weightKg))+'"></label></div></div>';}).join("")+'</div></div>';
}

function renderStrengthEditor(){
  var payload=STRENGTH_STATE.payload||{},sets=payload.sets||[],garmin=sets.filter(function(row){return row.source==="garmin";}),manual=sets.filter(function(row){return row.source==="manual";});
  var recent=(payload.recentExercises||[]).concat(payload.exercises||[]),seen={},options=recent.filter(function(name){var key=String(name).toLowerCase();if(!name||seen[key])return false;seen[key]=true;return true;}).map(function(name){return '<option value="'+esc(name)+'"></option>';}).join("");
  var grouped={},groupOrder=[];manual.forEach(function(row){var key=manualGroupKey(row);if(!grouped[key]){grouped[key]=[];groupOrder.push(key);}grouped[key].push(row);});
  var content=document.getElementById("strength-editor-content");
  content.innerHTML='<datalist id="strength-exercise-options">'+options+'</datalist>'+(payload.warning?'<div class="strength-banner warn">'+esc(payload.warning)+'</div>':'<div class="strength-banner">Garmin values stay recoverable. Saved corrections are used by this dashboard and its AI advice.</div>')+'<div class="strength-editor-title"><div><h3>Garmin sets</h3><p>Tap any value to correct it. Use reps, seconds, or both; weight is optional.</p></div><button type="button" class="strength-apply" data-strength-revert>Revert Garmin values</button></div><div class="strength-set-list">'+(garmin.length?garmin.map(strengthGarminRow).join(""):'<div class="strength-empty">Garmin did not return any recorded sets. You can still add the exercises manually below.</div>')+'</div><div class="strength-editor-title"><div><h3>Sets Garmin missed</h3><p>Adds three sets by default; copy the previous set when values repeat.</p></div><button type="button" class="strength-add" data-strength-add>+ Add exercise</button></div><div class="strength-manual-list">'+groupOrder.map(function(key,index){return strengthManualGroup(grouped[key],index);}).join("")+'</div><p class="strength-status" id="strength-status" aria-live="polite">'+(payload.savedAt?"Last saved "+new Date(payload.savedAt).toLocaleString():"Not saved yet")+'</p>';
}

function renderManualStrengthEditor(){
  var payload=STRENGTH_STATE.payload||{},sets=payload.sets||[],recent=(payload.recentExercises||[]).concat(payload.exercises||[]),seen={},options=recent.filter(function(name){var key=String(name).toLowerCase();if(!name||seen[key])return false;seen[key]=true;return true;}).map(function(name){return '<option value="'+esc(name)+'"></option>';}).join(""),grouped={},groupOrder=[];
  sets.forEach(function(row){var key=manualGroupKey(row);if(!grouped[key]){grouped[key]=[];groupOrder.push(key);}grouped[key].push(row);});
  var merge=payload.manualId?'<div class="strength-editor-title"><div><h3>Match Garmin workout</h3><p>Numbers come from Garmin; your exercise names stay. Your original manual entry remains saved.</p></div><button type="button" class="strength-apply" data-manual-candidates>Show 3 nearby Garmin workouts</button></div><div id="manual-merge-candidates"></div>':'';
  document.getElementById("strength-editor-content").innerHTML='<datalist id="strength-exercise-options">'+options+'</datalist><div class="strength-banner">No session timer. Enter seconds only for time-based sets such as Farmer’s Walk. Calories and strength effort are clearly estimated; Garmin Training Load is never created.</div><label class="strength-field">Workout name<input id="manual-workout-name" value="'+esc(payload.activityName||"Manual strength workout")+'" placeholder="Gym workout"></label><div class="strength-editor-title"><div><h3>Your sets</h3><p>Add an exercise, then reps or seconds and optional weight.</p></div><button type="button" class="strength-add" data-strength-add>+ Add exercise</button></div><div class="strength-manual-list">'+groupOrder.map(function(key,index){return strengthManualGroup(grouped[key],index);}).join("")+'</div>'+merge+'<p class="strength-status" id="strength-status" aria-live="polite">'+(payload.updatedAt?"Last saved "+new Date(payload.updatedAt).toLocaleString():"Not saved yet")+'</p>';
  var candidateButton=document.querySelector("[data-manual-candidates]");if(candidateButton)candidateButton.addEventListener("click",loadManualMergeCandidates);
}

function openManualStrengthEditor(manualId){
  var modal=ensureStrengthModal(),newWorkout=!manualId;STRENGTH_STATE={mode:"manual",activity:{},payload:{manualId:manualId||null,activityName:"Manual strength workout",activityStart:new Date().toISOString(),sets:[]}};STRENGTH_DIRTY=false;modal.hidden=false;document.body.classList.add("modal-open");
  document.getElementById("strength-dialog-title").textContent="Manual gym workout";document.getElementById("strength-dialog-meta").textContent="Enter completed sets as you train";document.getElementById("strength-editor-content").innerHTML='<div class="state"><div><div class="spin"></div>Loading workout…</div></div>';
  var done=function(payload){STRENGTH_STATE.payload=payload;if(!payload.sets.length){var key="manual:"+Date.now().toString(36);for(var i=0;i<3;i++)payload.sets.push({id:key+":"+i,source:"manual",setType:"ACTIVE",exercise:null,reps:null,durationSeconds:null,weightKg:null,perSide:false});}renderManualStrengthEditor();};
  if(newWorkout){fetch("/api/strength-exercises",{headers:{Authorization:"Bearer "+TOKEN}}).then(function(r){return r.ok?r.json():{};}).then(function(reference){STRENGTH_STATE.payload.exercises=reference.exercises||[];done(STRENGTH_STATE.payload);}).catch(function(){done(STRENGTH_STATE.payload);});}
  else fetch("/api/manual-strength-workouts/"+encodeURIComponent(manualId),{headers:{Authorization:"Bearer "+TOKEN}}).then(function(r){return r.json().then(function(b){if(!r.ok)throw new Error(b.error||"Could not load workout");return b;});}).then(done).catch(function(error){document.getElementById("strength-editor-content").innerHTML='<div class="strength-empty">'+esc(error.message)+'</div>';});
}

function openManualEnduranceEditor(defaultSport){
  var modal=ensureStrengthModal();STRENGTH_STATE={mode:"endurance",payload:{sport:defaultSport||"run"}};STRENGTH_DIRTY=false;modal.hidden=false;document.body.classList.add("modal-open");
  document.getElementById("strength-dialog-title").textContent="Manual activity";document.getElementById("strength-dialog-meta").textContent="Add a bike, run, or walk session";document.getElementById("strength-save").textContent="Save activity";
  document.getElementById("strength-editor-content").innerHTML='<div class="strength-banner">Enter the values you know. “Estimate calories” uses your saved weight when available and a sport-specific walking, cycling, or running estimate.</div><div class="entry-fields"><label>Activity type<select id="manual-endurance-sport"><option value="bike" '+(defaultSport==="bike"?"selected":"")+'>Bike</option><option value="run" '+(defaultSport!=="bike"?"selected":"")+'>Run</option><option value="walk">Walk</option></select></label><label>Distance (km)<input id="manual-endurance-distance" type="number" min="0" step="0.01" required></label><label>Time (minutes)<input id="manual-endurance-minutes" type="number" min="1" step="1" required></label><label>Calories (kcal)<input id="manual-endurance-calories" type="number" min="0" step="1"></label></div><div class="entry-actions"><button type="button" class="rf" id="manual-endurance-estimate">Estimate calories</button></div><p class="strength-status" id="strength-status">Choose Run or Walk explicitly—the dashboard keeps them separate.</p>';
  document.getElementById("manual-endurance-estimate").addEventListener("click",function(){var sport=document.getElementById("manual-endurance-sport").value,minutes=Number(document.getElementById("manual-endurance-minutes").value),weight=(((CURRENT_DASHBOARD||{}).wellness||{}).weight||{}).kg||70;if(!minutes||minutes<1){setStrengthStatus("Enter the time first",true);return;}var met={walk:3.5,bike:6,run:9.8}[sport],calories=Math.round(met*3.5*weight/200*minutes);document.getElementById("manual-endurance-calories").value=calories;setStrengthStatus("Estimated "+calories+" kcal for "+sport+" using "+weight+" kg. You can adjust it.");});
}

function saveManualEnduranceDetails(){var button=document.getElementById("strength-save"),sport=document.getElementById("manual-endurance-sport").value,distance=readStrengthNumber(document.getElementById("manual-endurance-distance"),"Distance"),minutes=readStrengthNumber(document.getElementById("manual-endurance-minutes"),"Time"),calories=readStrengthNumber(document.getElementById("manual-endurance-calories"),"Calories");if(distance==null||minutes==null||minutes<1){setStrengthStatus("Distance and time are required",true);return;}button.disabled=true;fetch("/api/manual-endurance-activities",{method:"POST",headers:{Authorization:"Bearer "+TOKEN,"Content-Type":"application/json"},body:JSON.stringify({sport:sport,distanceKm:distance,minutes:minutes,calories:calories,weightKg:((((CURRENT_DASHBOARD||{}).wellness||{}).weight||{}).kg||70),activityStart:new Date().toISOString()})}).then(function(r){return r.json().then(function(b){if(!r.ok)throw new Error(b.error||"Could not save activity");return b;});}).then(function(){setStrengthStatus("Saved. Refreshing dashboard…");setTimeout(function(){var modal=document.getElementById("strength-modal");modal.hidden=true;document.body.classList.remove("modal-open");load();},350);}).catch(function(error){setStrengthStatus(error.message,true);}).finally(function(){button.disabled=false;button.textContent="Save activity";});}

function loadManualMergeCandidates(){var id=(STRENGTH_STATE.payload||{}).manualId,target=document.getElementById("manual-merge-candidates");if(!id||!target)return;target.innerHTML='<div class="strength-empty">Looking for recent Garmin strength workouts…</div>';fetch("/api/manual-strength-workouts/"+encodeURIComponent(id)+"/candidates",{headers:{Authorization:"Bearer "+TOKEN}}).then(function(r){return r.json().then(function(b){if(!r.ok)throw new Error(b.error||"Could not load candidates");return b;});}).then(function(data){var rows=data.candidates||[];target.innerHTML=rows.length?rows.map(function(row){return '<div class="strength-set-row"><div class="strength-set-meta"><div><b>'+esc(row.name)+'</b><span>'+esc((row.start||"").replace("T"," "))+'</span></div><button type="button" class="strength-apply" data-manual-merge="'+esc(row.activityId)+'">Merge</button></div><p class="strength-original">Garmin: '+n(row.totalSets)+' sets · '+n(row.cal)+' kcal. Garmin numbers will replace matched set values.</p></div>';}).join(""):'<div class="strength-empty">No Garmin strength workout was found within 24 hours.</div>';}).catch(function(error){target.innerHTML='<div class="strength-empty">'+esc(error.message)+'</div>';});}
function mergeManualStrengthWorkout(garminActivityId){var id=(STRENGTH_STATE.payload||{}).manualId;if(!id||!window.confirm("Merge this manual workout with the selected Garmin workout? Your original manual sets will be retained."))return;fetch("/api/manual-strength-workouts/"+encodeURIComponent(id)+"/merge",{method:"POST",headers:{Authorization:"Bearer "+TOKEN,"Content-Type":"application/json"},body:JSON.stringify({garminActivityId:garminActivityId})}).then(function(r){return r.json().then(function(b){if(!r.ok)throw new Error(b.error||"Could not merge");return b;});}).then(function(){setStrengthStatus("Merged. Refreshing dashboard…");setTimeout(function(){closeStrengthEditor();load();},350);}).catch(function(error){setStrengthStatus(error.message,true);});}

function openStrengthEditor(activityId){
  var activity=((CURRENT_DASHBOARD||{}).recent||[]).find(function(row){return String(row.activityId)===String(activityId);})||(((CURRENT_DASHBOARD||{}).workouts||{}).lastStrength)||{};
  var modal=ensureStrengthModal();STRENGTH_STATE={activity:activity,payload:{sets:[]}};STRENGTH_DIRTY=false;modal.hidden=false;document.body.classList.add("modal-open");
  document.getElementById("strength-dialog-title").textContent=activity.name||"Strength details";
  document.getElementById("strength-dialog-meta").textContent=((activity.start||"").replace("T"," "))+(activity.min?" · "+activity.min+" min":"");
  document.getElementById("strength-editor-content").innerHTML='<div class="state"><div><div class="spin"></div>Loading Garmin sets…</div></div>';
  fetch("/api/strength-activities/"+encodeURIComponent(activityId),{headers:{Authorization:"Bearer "+TOKEN}})
    .then(function(response){return response.json().then(function(body){if(!response.ok)throw new Error(body.error||"Could not load strength sets");return body;});})
    .then(function(payload){STRENGTH_STATE.payload=payload;renderStrengthEditor();})
    .catch(function(error){document.getElementById("strength-editor-content").innerHTML='<div class="strength-empty"><b>Could not load this activity.</b><br>'+esc(error.message)+'</div>';});
}

function collectStrengthSets(){
  var originalById={};((STRENGTH_STATE.payload||{}).sets||[]).forEach(function(row){originalById[row.id]=row;});var sets=[];
  document.querySelectorAll("#strength-modal [data-strength-row]").forEach(function(node){var source=originalById[node.dataset.setId]||{};sets.push({id:source.id,source:"garmin",garminIndex:source.garminIndex,setType:source.setType,startTime:source.startTime,durationSeconds:readStrengthNumber(node.querySelector('[data-strength-field="durationSeconds"]'),"Duration"),exercise:node.querySelector('[data-strength-field="exercise"]').value.trim()||null,reps:readStrengthNumber(node.querySelector('[data-strength-field="reps"]'),"Repetitions"),weightKg:readStrengthNumber(node.querySelector('[data-strength-field="weightKg"]'),"Weight"),perSide:node.querySelector('[data-strength-field="perSide"]').checked,rawExercise:source.rawExercise,rawReps:source.rawReps,rawDurationSeconds:source.rawDurationSeconds,rawWeightKg:source.rawWeightKg});});
  document.querySelectorAll("#strength-modal [data-manual-group]").forEach(function(group){var exercise=group.querySelector("[data-manual-exercise]").value.trim(),perSide=group.querySelector("[data-manual-per-side]").checked;group.querySelectorAll("[data-manual-line]").forEach(function(line){var reps=readStrengthNumber(line.querySelector("[data-manual-reps]"),"Repetitions"),duration=readStrengthNumber(line.querySelector("[data-manual-duration]"),"Duration"),weight=readStrengthNumber(line.querySelector("[data-manual-weight]"),"Weight");if(reps==null&&duration==null&&weight==null)return;if(!exercise)throw new Error("Choose an exercise for every manual set");if(reps==null&&!duration)throw new Error("Enter repetitions or seconds for every manual set");sets.push({id:line.dataset.setId,source:"manual",setType:"ACTIVE",exercise:exercise,reps:reps,durationSeconds:duration,weightKg:weight,perSide:perSide});});});
  return sets;
}

function copyPreviousManualSet(button){
  var line=button.closest("[data-manual-line]"),previous=line&&line.previousElementSibling;if(!previous)return;
  [["[data-manual-reps]","[data-manual-reps]"],["[data-manual-duration]","[data-manual-duration]"],["[data-manual-weight]","[data-manual-weight]"]].forEach(function(pair){var from=previous.querySelector(pair[0]),to=line.querySelector(pair[1]);to.value=from.value;to.classList.add("changed");});
  STRENGTH_DIRTY=true;setStrengthStatus("Previous set values copied");
}

function applyExerciseToThree(setId){
  var rows=Array.from(document.querySelectorAll("#strength-modal [data-strength-row]")),index=rows.findIndex(function(row){return row.dataset.setId===setId;});if(index<0)return;var exercise=rows[index].querySelector('[data-strength-field="exercise"]').value.trim(),perSide=rows[index].querySelector('[data-strength-field="perSide"]').checked;if(!exercise){setStrengthStatus("Choose or type an exercise first",true);return;}rows.slice(index,index+3).forEach(function(row){var input=row.querySelector('[data-strength-field="exercise"]'),check=row.querySelector('[data-strength-field="perSide"]');input.value=exercise;input.classList.add("changed");check.checked=perSide;var label=row.querySelector("[data-strength-reps-label]");if(label)label.textContent=perSide?"Reps/side":"Reps";});STRENGTH_DIRTY=true;setStrengthStatus(exercise+" assigned to "+Math.min(3,rows.length-index)+" sets");
}

function revertGarminStrengthRows(){
  var originalById={};((STRENGTH_STATE.payload||{}).sets||[]).forEach(function(row){originalById[row.id]=row;});document.querySelectorAll("#strength-modal [data-strength-row]").forEach(function(node){var row=originalById[node.dataset.setId]||{};node.querySelector('[data-strength-field="exercise"]').value=row.rawExercise||"";node.querySelector('[data-strength-field="reps"]').value=inputNumber(row.rawReps);node.querySelector('[data-strength-field="durationSeconds"]').value=inputNumber(row.rawDurationSeconds);node.querySelector('[data-strength-field="weightKg"]').value=inputNumber(row.rawWeightKg);node.querySelector('[data-strength-field="perSide"]').checked=false;node.querySelectorAll(".changed").forEach(function(input){input.classList.remove("changed");});var label=node.querySelector("[data-strength-reps-label]");if(label)label.textContent="Reps";});STRENGTH_DIRTY=true;setStrengthStatus("Original Garmin values restored — save to confirm");
}

var MANUAL_GROUP_SEQUENCE=0;
function addManualStrengthGroup(){
  var list=document.querySelector("#strength-modal .strength-manual-list");if(!list)return;
  var key="manual:"+Date.now().toString(36)+"-"+(++MANUAL_GROUP_SEQUENCE),rows=[];
  for(var index=0;index<3;index++)rows.push({id:key+":"+index,source:"manual",setType:"ACTIVE",exercise:null,reps:null,durationSeconds:null,weightKg:null,perSide:false});
  list.insertAdjacentHTML("beforeend",strengthManualGroup(rows,list.querySelectorAll("[data-manual-group]").length));
  STRENGTH_DIRTY=true;setStrengthStatus("New three-set exercise added");
  var group=list.lastElementChild;if(group){group.scrollIntoView({behavior:"smooth",block:"nearest"});group.querySelector("[data-manual-exercise]").focus();}
}

async function workoutRequest(endpoint,options){
  var controller=new AbortController(),timer=setTimeout(function(){controller.abort();},30000);
  try{
    var response=await fetch(endpoint,Object.assign({},options,{signal:controller.signal}));
    var body;try{body=await response.json();}catch(error){throw new Error("The server returned an unreadable response. Please try again.");}
    if(!response.ok)throw new Error(body.error||"Could not update the workout. Please try again.");
    return body;
  }catch(error){if(error.name==="AbortError")throw new Error("The request timed out. Check the dashboard before retrying; your changes remain in this form.");throw error;}
  finally{clearTimeout(timer);}
}

async function saveStrengthDetails(){
  if(STRENGTH_STATE&&STRENGTH_STATE.mode==="endurance"){saveManualEnduranceDetails();return;}
  var button=document.getElementById("strength-save");if(button.disabled)return;
  try{
    var sets=collectStrengthSets(),activity=STRENGTH_STATE.activity||{},manual=STRENGTH_STATE.mode==="manual",nameInput=document.getElementById("manual-workout-name");
    if(manual&&!nameInput)throw new Error("The workout form could not be read. Close and reopen it, then try again.");
    var payload={activityName:manual?nameInput.value.trim():activity.name,activityStart:manual?(STRENGTH_STATE.payload.activityStart||new Date().toISOString()):activity.start,sets:sets};
    var endpoint=manual?(STRENGTH_STATE.payload.manualId?"/api/manual-strength-workouts/"+encodeURIComponent(STRENGTH_STATE.payload.manualId):"/api/manual-strength-workouts"):"/api/strength-activities/"+encodeURIComponent(activity.activityId);
    button.disabled=true;button.textContent="Saving…";setStrengthStatus("Saving details…");
    var saved=await workoutRequest(endpoint,{method:"POST",headers:{Authorization:"Bearer "+TOKEN,"Content-Type":"application/json"},body:JSON.stringify(payload)});
    STRENGTH_STATE.payload=saved;STRENGTH_DIRTY=false;
    var modal=document.getElementById("strength-modal");if(modal)modal.hidden=true;
    document.body.classList.remove("modal-open");load();
  }catch(error){setStrengthStatus(error.message,true);}
  finally{button.disabled=false;button.textContent="Save details";}
}

function manualDeleteButton(activity){
  if(activity.source==="merged"||activity.source==="garmin"||activity.mergedGarminActivityId)return "";
  var id=activity.manualId||activity.manualEnduranceId;if(!id)return "";
  var kind=activity.manualId?"strength":"endurance";
  return '<button type="button" class="strength-remove" data-manual-delete="'+esc(id)+'" data-manual-kind="'+kind+'" data-manual-name="'+esc(activity.name)+'" title="Delete manual workout" aria-label="Delete manual workout"><svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></svg></button>';
}

async function deleteManualWorkout(button){
  if(button.disabled||!window.confirm('Delete "'+(button.dataset.manualName||"this manual workout")+'"? This removes the manual entry from the dashboard. Garmin activities will not be deleted.'))return;
  button.disabled=true;
  try{
    var endpoint=button.dataset.manualKind==="strength"?"/api/manual-strength-workouts/":"/api/manual-endurance-activities/";
    await workoutRequest(endpoint+encodeURIComponent(button.dataset.manualDelete),{method:"DELETE",headers:{Authorization:"Bearer "+TOKEN}});
    load();
  }catch(error){window.alert(error.message);}
  finally{button.disabled=false;}
}
function chartTip(svg,points,format){
  if(!svg||!points||!points.length)return;var tip=document.getElementById("svg-tip");if(!tip){tip=el("div","svg-tip");tip.id="svg-tip";tip.hidden=true;document.body.appendChild(tip);}
  svg.onpointermove=function(e){var r=svg.getBoundingClientRect(),i=Math.max(0,Math.min(points.length-1,Math.round((e.clientX-r.left)/r.width*(points.length-1))));tip.innerHTML=format(points[i]);tip.style.left=e.clientX+"px";tip.style.top=e.clientY+"px";tip.hidden=false;};svg.onpointerleave=function(){tip.hidden=true;};
}

function muscleCreditList(items,role){
  var selected=(items||[]).filter(function(item){return item.role===role;});
  if(!selected.length)return '<span class="muscle-unmapped">—</span>';
  return selected.map(function(item){return '<span class="muscle-credit">'+esc(item.label)+' · '+item.volumeCreditPct+'%</span>';}).join("");
}
function renderMuscleExerciseTable(week){
  var target=document.getElementById("muscle-exercise-table");if(!target)return;var rows=week.exerciseBreakdown||[];
  if(!rows.length){target.innerHTML='<div class="meta">No detailed strength sets were saved for this week.</div>';return;}
  target.innerHTML='<div class="muscle-table-wrap"><table class="muscle-table"><thead><tr><th>Exercise</th><th>Sets</th><th>Direct credit</th><th>Assisting credit</th></tr></thead><tbody>'+rows.map(function(row){return '<tr><td>'+esc(row.exercise)+(row.mapped?'':' <span class="muscle-unmapped">· needs mapping</span>')+'</td><td>'+row.sets+'</td><td>'+muscleCreditList(row.muscles,"primary")+'</td><td>'+muscleCreditList(row.muscles,"assisting")+'</td></tr>';}).join("")+'</tbody></table></div><p class="metricnote">Percentages are chart volume credits, not measured muscle activation. Primary muscles receive 100% of a set; assisting muscles receive 50%.</p>';
}
function drawMuscleVolume(index){
  var weeks=(MUSCLE_STATE||{}).weeks||[],svg=document.getElementById("muscle-volume-chart");if(!weeks.length||!svg)return;
  MUSCLE_WEEK_INDEX=Math.max(0,Math.min(weeks.length-1,index));var week=weeks[MUSCLE_WEEK_INDEX],rows=week.muscles||[],source=week.sources||{};
  document.getElementById("muscle-week-label").textContent=week.label+(week.isCurrent?" · current":"");
  document.getElementById("muscle-prev").disabled=MUSCLE_WEEK_INDEX===0;document.getElementById("muscle-next").disabled=MUSCLE_WEEK_INDEX===weeks.length-1;
  var movement=(source.steps?comma(source.steps)+" steps":"no step data")+(source.floors?" · "+comma(source.floors)+" floors":"");
  document.getElementById("muscle-summary").textContent=source.strengthSets+" classified strength sets · "+source.cardioSessions+" cardio sessions / "+source.cardioMinutes+" min · "+movement+(source.unclassifiedSets?" · "+source.unclassifiedSets+" set(s) need mapping":"");
  renderMuscleExerciseTable(week);svg.innerHTML="";var W=1000,H=330,pL=76,pR=12,pT=25,pB=66,plotW=W-pL-pR,plotH=H-pT-pB;
  var top=Math.max.apply(null,rows.map(function(row){return row.total||0;}).concat([4]));top=Math.max(4,Math.ceil(top/2)*2);
  function Y(value){return pT+(top-value)/top*plotH;}var ticks=4;
  for(var tick=0;tick<=ticks;tick++){var value=top*tick/ticks,y=Y(value),line=document.createElementNS(ns,"line"),label=document.createElementNS(ns,"text");line.setAttribute("x1",pL);line.setAttribute("x2",W-pR);line.setAttribute("y1",y);line.setAttribute("y2",y);line.setAttribute("stroke",css("--border"));svg.appendChild(line);label.setAttribute("x",pL-9);label.setAttribute("y",y+5);label.setAttribute("text-anchor","end");label.setAttribute("fill",css("--faint"));label.setAttribute("font-size",15);label.textContent=Number(value.toFixed(1));svg.appendChild(label);}
  var axisTitle=document.createElementNS(ns,"text");axisTitle.setAttribute("x",-(pT+plotH/2));axisTitle.setAttribute("y",17);axisTitle.setAttribute("transform","rotate(-90)");axisTitle.setAttribute("text-anchor","middle");axisTitle.setAttribute("fill",css("--faint"));axisTitle.setAttribute("font-size",14);axisTitle.setAttribute("font-weight",650);axisTitle.textContent="Stimulus units";svg.appendChild(axisTitle);
  var slot=plotW/Math.max(1,rows.length),barWidth=slot*.58,shortNames={back:"Back",hamstrings:"Hams",triceps:"Tri",biceps:"Bi",calves:"Calves",delts:"Delts",quads:"Quads",glutes:"Glutes",chest:"Chest",core:"Core"};
  rows.forEach(function(row,i){var x=pL+i*slot+(slot-barWidth)/2,totalY=Y(row.total||0),directY=Y(row.direct||0),total=document.createElementNS(ns,"rect"),direct=document.createElementNS(ns,"rect"),name=document.createElementNS(ns,"text");total.setAttribute("x",x);total.setAttribute("y",totalY);total.setAttribute("width",barWidth);total.setAttribute("height",Math.max(0,Y(0)-totalY));total.setAttribute("rx",4);total.setAttribute("fill","#22d3ee");svg.appendChild(total);direct.setAttribute("x",x);direct.setAttribute("y",directY);direct.setAttribute("width",barWidth);direct.setAttribute("height",Math.max(0,Y(0)-directY));direct.setAttribute("rx",4);direct.setAttribute("fill","#0f8f8a");svg.appendChild(direct);name.setAttribute("x",x+barWidth/2);name.setAttribute("y",H-35);name.setAttribute("text-anchor","middle");name.setAttribute("fill",css("--text"));name.setAttribute("font-size",15);name.setAttribute("font-weight",650);name.textContent=shortNames[row.key]||row.label;svg.appendChild(name);if(row.total>0){var totalLabel=document.createElementNS(ns,"text");totalLabel.setAttribute("x",x+barWidth/2);totalLabel.setAttribute("y",totalY-7);totalLabel.setAttribute("text-anchor","middle");totalLabel.setAttribute("fill",css("--text"));totalLabel.setAttribute("font-size",14);totalLabel.setAttribute("font-weight",700);totalLabel.textContent=row.total;svg.appendChild(totalLabel);}});
  chartTip(svg,rows,function(row){return '<b>'+esc(row.label)+'</b><br>Direct sets: '+row.direct+'<br>Assisting strength: '+row.indirectStrength+'<br>Cardio: '+row.cardio+'<br>Steps / floors: '+row.movement+'<br><b>Total: '+row.total+'</b>';});
}
function changeMuscleWeek(delta){drawMuscleVolume(MUSCLE_WEEK_INDEX+delta);}

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
  var action=(meta.key==="bike"||meta.key==="run")?'<button type="button" class="rf strength-card-action" data-manual-endurance-new="'+meta.key+'">Log '+(meta.key==="bike"?"bike":"run / walk")+'</button>':'';
  if(!s.hasData){
    c.innerHTML=head+'<div class="empty"><span class="pill mute">Ready</span>'+
      '<span class="msg">No '+meta.name.toLowerCase()+' sessions yet — they\'ll show here automatically once you log one on your Garmin.</span></div>'+action;
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
      (last.hr?' · '+last.hr+' bpm':'')+' <span style="color:var(--faint)">('+(last.date||"")+')</span></div>'+action;
  return c;
}

function WORKOUTS(d){
  var s=d.workouts||{},c=el("div","card sport walk");
  var head='<div class="top"><span class="ico">🏋️</span><div><h3>Workouts</h3><div class="d">HIIT · rowing · SkiErg · elliptical</div></div></div>';
  if(!s.hasData){c.innerHTML=head+'<div class="empty"><span class="pill mute">Ready</span><span class="msg">Log a gym workout here, or sync one from Garmin.</span></div><button type="button" class="rf" data-manual-strength-new>Log gym workout</button>';return c;}
  var wk=s.week||{},last=s.last||{},strength=s.lastStrength||{},types=(s.types||[]).map(function(x){return x.name+' · '+x.count;}).join(' · '),action=(strength.activityId&&String(strength.activityId).indexOf("manual:")!==0?'<button type="button" class="rf strength-card-action" data-strength-id="'+esc(strength.activityId)+'">Edit latest strength details</button>':'')+'<button type="button" class="rf strength-card-action" data-manual-strength-new>Log gym workout</button>';
  c.innerHTML=head+'<div class="tstat"><div class="t"><div class="n">'+n(wk.sessions)+'</div><div class="l">sessions · 7d</div></div><div class="t"><div class="n">'+n(wk.min)+'</div><div class="l">minutes · 7d</div></div><div class="t"><div class="n">'+comma(wk.cal)+'</div><div class="l">kcal · 7d</div></div></div><div class="last">Last: <b>'+n(last.name)+'</b> · '+n(last.min)+' min'+(last.hr?' · '+last.hr+' bpm':'')+'<br><span style="color:var(--faint)">'+types+'</span></div>'+action;
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
function fillInjuryScores(){
  var form=document.getElementById("injury-entry"),date=form.elements.namedItem("date").value;
  var row=(((CURRENT_DASHBOARD||{}).injuries||{}).records||[]).find(function(item){return item.date===date;})||{};
  form.querySelectorAll('input[type="number"]').forEach(function(input){input.value=row[input.name]==null?"":row[input.name];});
  form.elements.namedItem("notes").value=row.notes||"";
}

function injurySettingsRow(item){
  return '<div class="injury-setting" data-injury-id="'+esc(item.id||"")+'"><label class="strength-field">Injury name<input data-injury-name maxlength="100" value="'+esc(item.name||"")+'" required></label><label class="strength-field">Colour<input data-injury-color type="color" value="'+esc(item.color||"#8b5cf6")+'"></label><label><input data-injury-enabled type="checkbox" '+(item.enabled?'checked':'')+'> Enabled</label><span data-injury-state class="pill '+(item.enabled?'good':'mute')+'">'+(item.enabled?'Active':'Disabled · history kept')+'</span></div>';
}

function openInjurySettings(){
  var dialog=document.getElementById("injury-settings");
  if(!dialog){
    dialog=document.createElement("dialog");dialog.id="injury-settings";dialog.className="injury-settings-dialog";
    dialog.innerHTML='<form id="injury-settings-form"><h2>Injury settings</h2><p>Disable an injury to hide it from the graph, daily form and current advice. All past scores stay saved and return when you re-enable it.</p><div id="injury-settings-list"></div><button type="button" class="rf" id="injury-settings-add">+ Add injury</button><p class="entry-status" id="injury-settings-status" aria-live="polite"></p><div class="entry-actions"><button type="button" class="rf" id="injury-settings-cancel">Cancel</button><button type="submit" class="entry-submit" id="injury-settings-save">Save settings</button></div></form>';
    document.body.appendChild(dialog);
    var form=dialog.querySelector("form");
    function cancel(event){if(dialog.dataset.busy==="true"){if(event)event.preventDefault();return;}if(dialog.dataset.dirty==="true"&&!window.confirm("Discard unsaved injury settings?")){if(event)event.preventDefault();return;}dialog.close();}
    dialog.addEventListener("cancel",cancel);document.getElementById("injury-settings-cancel").addEventListener("click",function(){cancel();});
    form.addEventListener("input",function(event){dialog.dataset.dirty="true";if(event.target.matches("[data-injury-enabled]")){var badge=event.target.closest(".injury-setting").querySelector("[data-injury-state]");badge.textContent=event.target.checked?"Active":"Disabled · history kept";badge.className="pill "+(event.target.checked?"good":"mute");}});
    document.getElementById("injury-settings-add").addEventListener("click",function(){var list=document.getElementById("injury-settings-list");list.insertAdjacentHTML("beforeend",injurySettingsRow({enabled:true}));dialog.dataset.dirty="true";list.lastElementChild.querySelector("[data-injury-name]").focus();});
    form.addEventListener("submit",async function(event){
      event.preventDefault();if(dialog.dataset.busy==="true")return;
      var definitions=Array.from(form.querySelectorAll(".injury-setting")).map(function(row){return {id:row.dataset.injuryId||null,name:row.querySelector("[data-injury-name]").value.trim(),color:row.querySelector("[data-injury-color]").value,enabled:row.querySelector("[data-injury-enabled]").checked};});
      var button=document.getElementById("injury-settings-save"),status=document.getElementById("injury-settings-status");dialog.dataset.busy="true";form.querySelectorAll("input,button").forEach(function(control){control.disabled=true;});status.textContent="Saving…";
      try{await workoutRequest("/api/injury-settings",{method:"POST",headers:{Authorization:"Bearer "+TOKEN,"Content-Type":"application/json"},body:JSON.stringify({definitions:definitions})});dialog.dataset.dirty="false";dialog.close();load();}
      catch(error){status.textContent=error.message;}
      finally{dialog.dataset.busy="false";form.querySelectorAll("input,button").forEach(function(control){control.disabled=false;});}
    });
  }
  document.getElementById("injury-settings-list").innerHTML=(((CURRENT_DASHBOARD||{}).injuries||{}).definitions||[]).map(injurySettingsRow).join("");
  document.getElementById("injury-settings-status").textContent="";dialog.dataset.dirty="false";dialog.dataset.busy="false";dialog.showModal();
}

function drawInjuries(records,definitions){
  var svg=document.getElementById("injuryc");if(!svg)return;svg.innerHTML="";records=records||[];if(!records.length)return;
  var W=1000,H=300,pL=44,pR=18,pT=16,pB=42,plotW=W-pL-pR,plotH=H-pT-pB;
  function X(i){return pL+i*plotW/Math.max(1,records.length-1);}function Y(v){return pT+(10-v)/10*plotH;}
  [0,2,4,6,8,10].forEach(function(v){var y=Y(v),line=document.createElementNS(ns,"line"),text=document.createElementNS(ns,"text");line.setAttribute("x1",pL);line.setAttribute("x2",W-pR);line.setAttribute("y1",y);line.setAttribute("y2",y);line.setAttribute("stroke",css("--border"));svg.appendChild(line);text.setAttribute("x",pL-10);text.setAttribute("y",y+5);text.setAttribute("text-anchor","end");text.setAttribute("font-size",15);text.setAttribute("fill",css("--faint"));text.textContent=v;svg.appendChild(text);});
  [0,7,14,21,29].filter(function(i){return i<records.length;}).forEach(function(i){var date=new Date(records[i].date+"T00:00:00"),text=document.createElementNS(ns,"text");text.setAttribute("x",X(i));text.setAttribute("y",H-12);text.setAttribute("text-anchor","middle");text.setAttribute("font-size",14);text.setAttribute("fill",css("--faint"));text.textContent=(date.getMonth()+1)+"/"+date.getDate();svg.appendChild(text);});
  var series=(definitions||[]).filter(function(item){return item.enabled;}).map(function(item){return {key:item.id,color:item.color,name:item.name};});
  series.forEach(function(s){var points=records.map(function(row,i){return row[s.key]==null?null:{x:X(i),y:Y(row[s.key])};}).filter(Boolean);if(!points.length)return;var path=document.createElementNS(ns,"path");path.setAttribute("d",points.map(function(point,i){return(i?"L":"M")+point.x.toFixed(1)+" "+point.y.toFixed(1);}).join(" "));path.setAttribute("fill","none");path.setAttribute("stroke",s.color);path.setAttribute("stroke-width",4);path.setAttribute("stroke-linecap","round");path.setAttribute("stroke-linejoin","round");svg.appendChild(path);points.forEach(function(point){var dot=document.createElementNS(ns,"circle");dot.setAttribute("cx",point.x);dot.setAttribute("cy",point.y);dot.setAttribute("r",6);dot.setAttribute("fill",css("--surface"));dot.setAttribute("stroke",s.color);dot.setAttribute("stroke-width",3);svg.appendChild(dot);});});
  chartTip(svg,records,function(row){return "<b>"+esc(row.date)+"</b>"+series.map(function(item){return "<br>"+esc(item.name)+": "+(row[item.key]==null?"—":row[item.key]+" / 10");}).join("")+(row.notes?"<br><b>Notes:</b> "+esc(row.notes).replace(/\n/g,"<br>"):"");});
}

function loadPersonalRecommendation(d,refresh){
  var target=document.getElementById("personal-recommendation");
  var button=document.getElementById("refresh-recommendation");
  if(!target)return;
  if(refresh){target.textContent="Updating your recommendation…";if(button){button.disabled=true;button.textContent="Updating…";}}
  var snapshot={date:d.date,wellness:d.wellness,body:d.body,injuries:d.injuries,recent:d.recent,sports:d.sports,relativeEffort:d.relativeEffort,recovery:d.recovery,strength:d.strength,muscleVolume:d.muscleVolume,fitnessSeries:d.fitnessSeries,trainingLoadTrend:d.trainingLoadTrend,sleepSeries:d.sleepSeries,hrvSeries:d.hrvSeries,hrZonesWeek:d.hrZonesWeek};
  fetch("/api/recommendation",{method:"POST",headers:{Authorization:"Bearer "+TOKEN,"Content-Type":"application/json"},body:JSON.stringify({dashboard:snapshot,refresh:!!refresh})})
    .then(function(r){return r.json().then(function(body){if(!r.ok)throw new Error(body.error||"Could not get a recommendation");return body;});})
    .then(function(result){target.textContent=result.text;target.parentElement.classList.add("ai-ready");})
    .catch(function(error){if(refresh)target.textContent="Could not update the recommendation: "+error.message;})
    .finally(function(){if(button){button.disabled=false;button.textContent="Refresh advice";}});
}

function render(d){
  var app=document.getElementById("app");
  app.innerHTML="";
  CURRENT_DASHBOARD=d;
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
  // Recovery summary. The contributor list below reports every signal; this
  // card exists to answer the two questions you have before scrolling — what
  // should I do today, and which signal is holding me back.
  var rec=el("div","card"),contribs=((d.recovery||{}).contributors||[]).slice();
  if(w.sleep||w.hrv||w.readiness){
    var wr=d.workoutRecommendation||{};
    var ranked=contribs.filter(function(c){return c.percent!=null;})
                       .sort(function(a,b){return a.percent-b.percent;});
    var weakest=ranked[0],strongest=ranked[ranked.length-1];
    var debtMin=((d.recovery||{}).debt||{}).minutes;
    var body='<div class="label"><p class="eyebrow">Recovery</p><span class="pill '+
      (wr.level==="recover"?"low":wr.level==="easy"?"warn":"good")+'">'+n(wr.label)+'</span></div>';
    if(w.readiness) body+='<div class="big"><span>'+n(w.readiness.score)+'</span><span class="unit">readiness</span></div>';
    if(weakest&&strongest&&weakest!==strongest){
      // Only call something a limiting factor when it actually holds you back;
      // with every signal optimal there is nothing to single out.
      var weakLabel=weakest.grade==="optimal"?"Lowest today":weakest.grade==="good"?"Worth watching":"Limiting factor";
      body+='<div class="factor"><span class="fl">'+weakLabel+'</span>'+
        '<span class="fv '+(weakest.grade||"")+'">'+weakest.label+(weakest.detail?' · '+weakest.detail:'')+'</span></div>'+
        '<div class="factor"><span class="fl">Strongest</span>'+
        '<span class="fv '+(strongest.grade||"")+'">'+strongest.label+(strongest.detail?' · '+strongest.detail:'')+'</span></div>';
    }
    if(debtMin!=null&&debtMin>0) body+='<div class="factor"><span class="fl">Sleep debt</span><span class="fv">'+fmtMinutes(debtMin)+' behind</span></div>';
    if(wr.text) body+='<div class="metricnote">'+wr.text+'</div>';
    rec.innerHTML=body;
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

  var mv=d.muscleVolume||{},muscleCard=el("div","card muscle-card");MUSCLE_STATE=mv;
  muscleCard.innerHTML='<div class="muscle-card-head"><div><h3>Muscle stimulus · weekly</h3><p>Direct lifting sets plus conservative supporting exposure</p></div><div class="muscle-week-nav"><button type="button" id="muscle-prev" aria-label="Previous week">‹</button><span class="muscle-week-label" id="muscle-week-label">Current week</span><button type="button" id="muscle-next" aria-label="Next week">›</button></div></div><p class="muscle-summary" id="muscle-summary"></p><div class="muscle-chart-wrap" role="region" aria-label="Scrollable weekly muscle chart" tabindex="0"><svg class="musclechart" id="muscle-volume-chart" viewBox="0 0 1000 330" preserveAspectRatio="none" aria-label="Weekly direct and indirect muscle volume chart"></svg></div><p class="muscle-scroll-hint">Swipe chart and table to see more →</p><div class="muscle-legend"><span><i style="background:#0f8f8a"></i>Direct strength sets</span><span><i style="background:#22d3ee"></i>Total including assisting, cardio and movement</span></div><p class="metricnote">'+esc(mv.formula||"")+'</p><details class="muscle-map" open><summary>Exercise allocation for this week</summary><div id="muscle-exercise-table"></div></details>';
  app.appendChild(muscleCard);
  if((mv.weeks||[]).length){document.getElementById("muscle-prev").addEventListener("click",function(){changeMuscleWeek(-1);});document.getElementById("muscle-next").addEventListener("click",function(){changeMuscleWeek(1);});drawMuscleVolume(mv.currentIndex==null?mv.weeks.length-1:mv.currentIndex);}else{document.getElementById("muscle-summary").textContent="No muscle-volume data is available yet.";document.getElementById("muscle-prev").disabled=true;document.getElementById("muscle-next").disabled=true;}

  // Strava-inspired effort + fitness views. These deliberately remain separate
  // from Garmin's Training Load because the two products use different models.
  app.appendChild(sec("Training response"));
  var response=el("div","keycharts"),re=d.relativeEffort||{};
  var effortCard=el("div","card");
  effortCard.innerHTML='<div class="label"><p class="eyebrow">Relative effort · weekly</p><span class="pill mute" id="effort-state">Select a week</span></div><div class="big"><span id="effort-value">'+n(re.current)+'</span><span class="unit">points</span></div><div class="meta" id="effort-meta"></div><svg class="loadchart primarychart" id="effortc" viewBox="0 0 1000 360" preserveAspectRatio="none"></svg><div class="effortdetail"><div class="label"><p class="eyebrow" id="effort-week">Daily effort</p><p class="eyebrow">Click another week above</p></div><svg class="effortdaily" id="effortdaily" viewBox="0 0 1000 180" preserveAspectRatio="none"></svg><div class="effortlist" id="effort-list"></div></div><div class="metricnote">'+n(re.rangeModel)+' '+n(re.formula)+'</div>';
  response.appendChild(effortCard);
  var fitnessCard=el("div","card");
  var fs=d.fitnessSeries||[],latest=fs[fs.length-1]||{};
  fitnessCard.innerHTML='<div class="label"><p class="eyebrow">Fitness level · effort model</p><span class="pill good" id="fit-value">'+n(latest.fitness)+' index</span></div><div class="charttabs" id="fit-tabs"><button data-days="30" class="active">1 month</button><button data-days="90">3 months</button><button data-days="180">6 months</button><button data-days="365">1 year</button><button data-days="731">2 years</button></div><div class="fitsummary" id="fit-summary"></div><svg class="loadchart primarychart" id="fitnessc" viewBox="0 0 1000 360" preserveAspectRatio="none"></svg><div class="metricnote">A 42-day average calibrated to your Strava reference readings, with extra weight for harder activity. Fitness builds with training and decays with rest. Comparisons use calendar periods and whole-number scores; the line shows smaller daily changes. Relative Effort scores are unchanged. The badge shows the selected day’s level; click any point to compare it with the first day of the period, or leave it on today.</div>';
  response.appendChild(fitnessCard);app.appendChild(response);

  // readiness contributors + sleep detail
  var rec=d.recovery||{},debt=rec.debt||{},lastNight=rec.lastNight||{};
  app.appendChild(sec("Readiness & sleep"));
  var readinessCard=el("div","card");
  var contribRows=(rec.contributors||[]).map(function(c){
    var pct=Math.max(2,Math.min(100,c.percent));
    return '<div class="contrib"><div class="contrib-head"><span class="k">'+c.label+'</span>'+
      '<span class="v '+(c.grade||"")+'">'+(c.detail||"")+'</span></div>'+
      '<span class="contrib-bar"><i class="'+(c.grade||"")+'" style="width:'+pct+'%"></i></span>'+
      (c.note?'<span class="contrib-note">'+c.note+'</span>':'')+'</div>';
  }).join("");
  readinessCard.innerHTML='<div class="label"><p class="eyebrow">Readiness contributors</p>'+
    (w.readiness?'<span class="pill '+((w.readiness.score||0)>=70?"good":(w.readiness.score||0)>=50?"warn":"low")+'">Garmin '+n(w.readiness.score)+'</span>':'')+'</div>'+
    (contribRows||'<div class="meta" style="margin-top:8px">Contributors need overnight wrist wear — nothing recorded yet.</div>')+
    (rec.missing?'<div class="metricnote">'+rec.missing+'</div>':'');
  app.appendChild(readinessCard);

  var sleepGrid=el("div","keycharts");
  // sleep debt
  var debtCard=el("div","card");
  var debtLabel=debt.windowDays?(debt.missingDays?'Partial data':'7-day estimate'):({none:'None',low:'Low',moderate:'Moderate',high:'High'}[debt.level]||'—');
  debtCard.innerHTML='<div class="label"><p class="eyebrow">Sleep debt · '+(debt.windowDays?'rolling 7 days':n(rec.nights?rec.nights.length:0)+' nights')+'</p>'+
    '<span class="pill '+(debt.windowDays?'':debt.level==="none"?"good":debt.level==="low"?"warn":"low")+'">'+debtLabel+'</span></div>'+
    '<div class="big"><span>'+fmtMinutes(debt.minutes)+'</span><span class="unit">shortfall</span></div>'+
    '<div class="meta">Nightly target '+n(rec.sleepNeedHours)+' h · Garmin night '+n(lastNight.hours)+' h'+
      (lastNight.efficiency?' · '+lastNight.efficiency+'% efficient':'')+'</div>'+
    '<svg class="loadchart" id="debtc" viewBox="0 0 1000 220" preserveAspectRatio="none"></svg>'+
    '<div class="metricnote">'+esc(rec.needModel||'')+' '+esc(rec.debtModel||'')+'</div>';
  if(rec.sleepLog) addSleepControls(debtCard,rec,d.date);
  sleepGrid.appendChild(debtCard);

  // sleep stages
  var stagesCard=el("div","card");
  stagesCard.innerHTML='<div class="label"><p class="eyebrow">Sleep stages · nightly</p>'+
    '<span class="pill mute">'+(lastNight.score?"Score "+lastNight.score:"—")+'</span></div>'+
    '<svg class="loadchart primarychart" id="stagesc" viewBox="0 0 1000 300" preserveAspectRatio="none"></svg>'+
    '<div class="zleg" id="stageleg"></div>'+
    '<div class="metricnote">Awake time inside the sleep window is shown above the bar. Deep and REM are the stages most associated with recovery.</div>';
  sleepGrid.appendChild(stagesCard);
  app.appendChild(sleepGrid);

  var recoveryTrends=el("div","grid twogrid");
  // HRV against Garmin's balanced baseline
  var hrvCard=el("div","card"),hrvs=d.hrvSeries||[];
  var hrvNow=w.hrv||{},hrvWeek=hrvNow.weeklyAvg!=null?hrvNow.weeklyAvg:hrvNow.value;
  hrvCard.innerHTML='<div class="label"><p class="eyebrow">HRV vs baseline · '+hrvs.length+' nights</p><span class="pill '+hrvPillClass(hrvNow.status)+'">'+(cap(hrvNow.status)||"No status")+'</span></div>'+
    '<div class="big"><span>'+n(hrvWeek)+'</span><span class="unit">ms · 7-day average</span></div>'+
    '<div class="meta">Last night '+n(hrvNow.value)+' ms'+(hrvNow.baselineLow?' · your balanced range '+Math.round(hrvNow.baselineLow)+'–'+Math.round(hrvNow.baselineHigh)+' ms':'')+'</div>'+
    '<svg class="loadchart" id="hrvc" viewBox="0 0 1000 220" preserveAspectRatio="none"></svg>'+
    '<div class="metricnote">Dots are nightly averages; the shaded band is Garmin’s balanced range for you. Garmin sets the status from your seven-day average, not a single night, so one low night does not move it.</div>';
  recoveryTrends.appendChild(hrvCard);
  // bed/wake consistency
  var timingCard=el("div","card"),reg=rec.regularity||{};
  timingCard.innerHTML='<div class="label"><p class="eyebrow">Sleep timing</p><span class="pill '+((reg.score||0)>=85?"good":(reg.score||0)>=70?"warn":"low")+'">'+(reg.score!=null?"Regularity "+reg.score:"—")+'</span></div>'+
    '<svg class="loadchart" id="timingc" viewBox="0 0 1000 220" preserveAspectRatio="none"></svg>'+
    '<div class="metricnote">Each bar spans bedtime to wake time.'+(reg.deviationMinutes!=null?' Your mid-sleep point drifts by about '+reg.deviationMinutes+' minutes night to night.':'')+' A steady schedule is the strongest lever on sleep quality.</div>';
  recoveryTrends.appendChild(timingCard);
  app.appendChild(recoveryTrends);

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
  var injuryData=d.injuries||{},activeInjuries=(injuryData.definitions||[]).filter(function(item){return item.enabled;});
  injury.innerHTML='<div class="label"><p class="eyebrow">Pain trend · last 30 days</p><button type="button" class="rf" id="injury-settings-open" aria-label="Injury settings">⚙ Settings</button></div><svg class="injurychart" id="injuryc" viewBox="0 0 1000 300" preserveAspectRatio="none"></svg><div class="painlegend">'+activeInjuries.map(function(item){return '<span><i style="background:'+esc(item.color)+'"></i>'+esc(item.name)+'</span>';}).join("")+'</div>'+(!activeInjuries.length?'<p>No active injuries. Open Settings to add or re-enable one. Your previous scores are retained.</p>':'')+'<div class="pain-scale"><b>Numeric Rating Scale (NRS-11):</b> 0 = no pain · 1–3 = mild · 4–6 = moderate · 7–10 = severe / worst pain imaginable.</div><div class="entry-actions"><button class="rf" id="injury-entry-toggle" '+(!activeInjuries.length?'disabled':'')+'>Add / edit pain scores & notes</button></div><form class="entry-form" id="injury-entry"><label class="strength-field">Date<input type="date" name="date" id="injury-score-date" min="'+esc(((injuryData.records||[])[0]||{}).date||d.date)+'" value="'+esc(d.date)+'" max="'+esc(d.date)+'" required></label><div class="entry-fields injury-fields">'+activeInjuries.map(function(item){return '<label>'+esc(item.name)+' (0–10)<input name="'+esc(item.id)+'" type="number" min="0" max="10" step="1" required></label>';}).join("")+'</div><label class="strength-field">Daily notes (optional)<textarea name="notes" rows="3" maxlength="2000" placeholder="Symptoms, recovery progress, or what helped today…"></textarea><small>Up to 2,000 characters. Saved for the selected date.</small></label><button class="entry-submit" type="submit" '+(!activeInjuries.length?'disabled':'')+'>Save scores & notes</button><p class="entry-status" id="injury-entry-status"></p></form>';
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
      '<span class="t">'+a.name+'<small>'+(a.start||"").replace("T"," ")+(a.location?" · "+a.location:"")+(a.source?" · source: "+a.source:"")+'</small>'+(a.manualId?'<button type="button" class="strength-open" data-manual-strength-id="'+esc(a.manualId)+'">Edit / merge</button>':(a.isStrength&&a.activityId?'<button type="button" class="strength-open" data-strength-id="'+esc(a.activityId)+'">Edit exercise sets</button>':''))+manualDeleteButton(a)+'</span></div>'+
      '<div class="c" data-k="Dist"><b>'+n(a.km)+'</b> km</div>'+
      '<div class="c" data-k="Time"><b>'+n(a.min)+'</b> min</div>'+
      '<div class="c hidesm" data-k="HR"><b>'+n(a.hr)+'</b> bpm</div>'+
      '<div class="c" data-k="Cal"><b>'+n(a.cal)+'</b> kcal'+(a.caloriesEstimated?'<small>estimated</small>':'')+'</div>';
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
  drawDebt(rec.debt||{});
  drawStages((rec.nights||[]).slice(-14));
  drawHrv((d.hrvSeries||[]).slice(-60));
  drawTiming((rec.nights||[]).slice(-14));
  drawEffort(re);
  drawFitness(fs,30);
  drawInjuries((d.injuries||{}).records||[],(d.injuries||{}).definitions||[]);
  fillInjuryScores();
  document.getElementById("injury-score-date").addEventListener("change",fillInjuryScores);
  document.getElementById("injury-settings-open").addEventListener("click",openInjurySettings);
  document.querySelectorAll("#fit-tabs button").forEach(function(button){button.addEventListener("click",function(){document.querySelectorAll("#fit-tabs button").forEach(function(x){x.classList.remove("active")});button.classList.add("active");drawFitness(fs,Number(button.dataset.days),null);});});
  document.getElementById("rf").addEventListener("click",load);
  document.getElementById("body-entry-toggle").addEventListener("click",function(){toggleEntry("body-entry");});
  document.getElementById("injury-entry-toggle").addEventListener("click",function(){toggleEntry("injury-entry");});
  document.querySelectorAll("[data-strength-id]").forEach(function(button){button.addEventListener("click",function(){openStrengthEditor(button.dataset.strengthId);});});
  document.querySelectorAll("[data-manual-delete]").forEach(function(button){button.addEventListener("click",function(){deleteManualWorkout(button);});});
  document.querySelectorAll("[data-manual-strength-new]").forEach(function(button){button.addEventListener("click",function(){openManualStrengthEditor();});});
  document.querySelectorAll("[data-manual-strength-id]").forEach(function(button){button.addEventListener("click",function(){openManualStrengthEditor(button.dataset.manualStrengthId);});});
  document.querySelectorAll("[data-manual-endurance-new]").forEach(function(button){button.addEventListener("click",function(){openManualEnduranceEditor(button.dataset.manualEnduranceNew);});});
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

// Garmin's own wording drives the colour. An unknown or missing status must
// stay neutral rather than defaulting to a warning the data does not support.
function hrvPillClass(status){
  var s=(status||"").toLowerCase();
  if(!s)return "mute";
  if(s.indexOf("unbalanc")>=0)return "warn";
  if(s.indexOf("balanc")>=0)return "good";
  if(s.indexOf("low")>=0||s.indexOf("poor")>=0)return "low";
  return "mute";
}

function fmtMinutes(mins){
  if(mins==null)return "—";
  var m=Math.round(mins);if(m<60)return m+"m";
  var h=Math.floor(m/60);
  return m%60?h+"h "+(m%60)+"m":h+"h";   // a whole hour reads better without "0m"
}
function fmtClock(hours){
  if(hours==null)return "";
  var h=((Math.floor(hours)%24)+24)%24,m=Math.round((hours-Math.floor(hours))*60);
  if(m===60){m=0;h=(h+1)%24;}
  return (h<10?"0":"")+h+":"+(m<10?"0":"")+m;
}

// Sleep debt over the rolling window, with Oura-style severity bands.
function addSleepControls(card,rec,today){
  var debt=rec.debt||{},log=rec.sleepLog,days=debt.days||[];
  card.id='sleep-debt-card';
  var panel=document.createElement('div');
  panel.innerHTML='<p class="metricnote"><b>'+n(debt.recordedDays)+'/7 days recorded</b> · '+fmtMinutes(debt.napMinutes)+' naps included · '+n(debt.averageHours)+' h average total sleep'+(debt.missingDays?' · Missing nights are not treated as zero sleep.':'')+'</p>'+
    '<details><summary style="cursor:pointer;padding:12px 0">Day-by-day sleep &amp; shortfall</summary><div style="overflow-x:auto"><table style="width:100%;text-align:left"><thead><tr><th>Date</th><th>Night</th><th>Naps</th><th>Shortfall</th></tr></thead><tbody>'+days.map(function(x){return '<tr><td>'+esc(x.date)+'</td><td>'+fmtMinutes(x.nightHours==null?null:x.nightHours*60)+' <small>('+esc(x.source)+')</small></td><td>'+n(x.napMinutes)+' min</td><td>'+fmtMinutes(x.shortfallMinutes)+'</td></tr>'+(x.notes?'<tr><td colspan="4" style="padding-bottom:10px;white-space:pre-wrap">'+esc(x.notes)+'</td></tr>':'');}).join('')+'</tbody></table></div></details>'+
    '<details><summary style="cursor:pointer;padding:12px 0">Sleep settings &amp; naps</summary>'+
    '<form data-sleep-target class="entry-form" style="display:block"><label class="strength-field">Nightly target (hours)<input name="targetHours" type="number" min="4" max="12" step="0.1" value="'+esc(log.targetHours)+'" required></label><p class="metricnote">Default 7.5 h. Changing this recalculates the displayed history; it does not measure your biological sleep need.</p><button class="entry-submit">Save target</button><p role="status"></p></form>'+
    '<form data-sleep-day class="entry-form" style="display:block"><label class="strength-field">Date (night ending and naps on this day)<input name="date" type="date" max="'+esc(today)+'" value="'+esc(today)+'" required></label><div class="entry-fields"><label>Total nap minutes<input name="napMinutes" type="number" min="0" max="720" step="1" value="0" required></label><label>Night sleep correction (hours, optional)<input name="nightHours" type="number" min="0" max="24" step="0.01" placeholder="Use Garmin"></label></div><p class="metricnote">Enter actual sleep, not time in bed. Naps are manual totals for the selected date, counted once. A night correction replaces Garmin duration only in this estimate. Leave it blank to use Garmin; missing nights need a duration before naps can count. Today remains provisional until the day ends.</p><label class="strength-field">Daily context (optional)<textarea name="notes" rows="3" maxlength="2000" placeholder="Illness, stress, travel, late caffeine, interruptions, how rested you feel…"></textarea></label><p class="metricnote">Notes inform the context for AI advice; they do not add or subtract sleep minutes. To remove an entry, set naps to 0, clear the correction and notes, then save.</p><button class="entry-submit">Save day</button><p role="status"></p></form></details>';
  var caption=document.createElement('p');caption.className='metricnote';caption.textContent='Last 7 days: each point shows the preceding seven-day shortfall. Green: under 3 h; yellow: 3–9 h; red: over 9 h. These are visual guides, not clinical cutoffs. Dashed sections have fewer than 7 recorded days.';card.appendChild(caption);
  card.appendChild(panel);
  var dayForm=panel.querySelector('[data-sleep-day]');
  function populate(){var entry=(log.entries||{})[dayForm.elements.date.value]||{};dayForm.elements.napMinutes.value=entry.napMinutes||0;dayForm.elements.nightHours.value=entry.nightHours==null?'':entry.nightHours;dayForm.elements.notes.value=entry.notes||'';}
  dayForm.elements.date.addEventListener('change',populate);populate();
  panel.querySelectorAll('form').forEach(function(form){form.addEventListener('submit',async function(event){
    event.preventDefault();var button=form.querySelector('button'),status=form.querySelector('[role=status]');button.disabled=true;status.textContent='Saving…';
    var payload={};new FormData(form).forEach(function(value,key){payload[key]=value;});
    var saved=false;
    try{
      var response=await fetch('/api/sleep-log',{method:'POST',headers:{Authorization:'Bearer '+TOKEN,'Content-Type':'application/json'},body:JSON.stringify(payload),signal:AbortSignal.timeout(25000)});
      var body=await response.json();if(!response.ok)throw new Error(body.error||'Could not save.');saved=true;log=body;status.textContent='Saved. Updating the chart…';
      var refreshed=await fetch('/api/dashboard',{headers:{Authorization:'Bearer '+TOKEN},signal:AbortSignal.timeout(90000)});
      if(!refreshed.ok)throw new Error('Refresh failed');render(await refreshed.json());
      var updated=document.getElementById('sleep-debt-card');if(updated){updated.scrollIntoView({block:'center'});var message=document.createElement('p');message.setAttribute('role','status');message.textContent='Sleep changes saved.';updated.appendChild(message);}
    }catch(error){status.textContent=saved?'Saved successfully. Refresh the dashboard to update the chart.':('Could not confirm save: '+error.message+'. Your input is still here; retrying replaces the same entry.');}
    finally{button.disabled=false;}
  });});
}

function drawDebt(debt){
  var svg=document.getElementById("debtc"),rows=((debt||{}).series||[]).slice(-7);if(!svg||!rows.length)return;svg.innerHTML="";
  // The gutter holds durations such as "5h" or "30m", so it needs more room
  // than the numeric axes elsewhere on the page.
  var W=Math.max(280,svg.clientWidth||1000),H=220,pL=50,pR=16,pT=18,pB=34,bands=(debt.bands||[180,540,900]);
  svg.setAttribute("viewBox","0 0 "+W+" "+H);
  var max=Math.max.apply(null,rows.map(function(x){return x.minutes;}).concat([bands[1]]))*1.2;
  function Y(v){return pT+(max-v)/max*(H-pT-pB)}
  [['good',0,bands[0]],['warn',bands[0],bands[1]],['low',bands[1],max]].forEach(function(band){
    if(band[1]>=max)return;
    var rect=document.createElementNS(ns,'rect');rect.setAttribute('x',pL);rect.setAttribute('width',W-pL-pR);
    rect.setAttribute('y',Y(Math.min(band[2],max)));rect.setAttribute('height',Y(band[1])-Y(Math.min(band[2],max)));
    rect.setAttribute('fill',css('--'+band[0]));rect.setAttribute('opacity','.10');svg.appendChild(rect);
  });
  function X(i){return pL+i*(W-pL-pR)/Math.max(1,rows.length-1)}
  [false,true].forEach(function(partial){
    var line='';
    for(var i=1;i<rows.length;i++){
      if(rows[i-1].minutes==null||rows[i].minutes==null)continue;
      var incomplete=rows[i].recordedDays!=null&&(rows[i].recordedDays<7||rows[i-1].recordedDays<7);
      if(incomplete!==partial)continue;
      line+='M'+X(i-1).toFixed(1)+' '+Y(rows[i-1].minutes).toFixed(1)+'L'+X(i).toFixed(1)+' '+Y(rows[i].minutes).toFixed(1);
    }
    var path=document.createElementNS(ns,'path');path.setAttribute('d',line);path.setAttribute('fill','none');
    path.setAttribute('stroke',css('--accent'));path.setAttribute('stroke-width',4);
    if(partial)path.setAttribute('stroke-dasharray','7 6');svg.appendChild(path);
  });
  rows.forEach(function(x,i){
    var cx=X(i),last=i===rows.length-1;
    if(x.minutes!=null){
      var c=document.createElementNS(ns,"circle");c.setAttribute("cx",cx);c.setAttribute("cy",Y(x.minutes));
      c.setAttribute("r",last?8:4);c.setAttribute("fill",last?css("--accent"):css("--surface"));
      c.setAttribute("stroke",css("--accent"));c.setAttribute("stroke-width",3);svg.appendChild(c);
    }
    var t=document.createElementNS(ns,"text");t.setAttribute("x",cx);t.setAttribute("y",H-9);
    t.setAttribute("text-anchor",last?"end":i===0?"start":"middle");
    t.style.setProperty('font-size',W<500?'11px':'16px','important');
    t.setAttribute("fill",last?css("--accent"):css("--faint"));
    t.textContent=x.date?new Date(x.date+'T12:00:00').toLocaleDateString('en',{weekday:'short'}):x.label;
    svg.appendChild(t);
  });
  var lastLabelY=null;
  [0].concat(bands).forEach(function(v){
    if(v>max)return;
    var y=Y(v);
    if(lastLabelY!==null&&Math.abs(y-lastLabelY)<20)return;  // keep labels legible on a phone
    lastLabelY=y;
    var t=document.createElementNS(ns,"text");t.setAttribute("x",pL-8);t.setAttribute("y",y+5);
    t.setAttribute("text-anchor","end");t.setAttribute("font-size",15);t.setAttribute("fill",css("--faint"));
    t.textContent=v===0?"0":fmtMinutes(v);svg.appendChild(t);
  });
  chartTip(svg,rows,function(x){return '<b>'+esc(x.label)+'</b><br>'+fmtMinutes(x.minutes)+(debt.windowDays?' weekly shortfall':' of sleep debt')+(x.recordedDays!=null?'<br>'+x.recordedDays+'/7 days recorded':'');});
}

// Stacked nightly sleep stages, with awake time floated above the asleep total.
function drawStages(nights){
  var svg=document.getElementById("stagesc");if(!svg||!nights.length)return;svg.innerHTML="";
  var W=1000,H=300,pL=52,pR=16,pT=24,pB=40,slot=(W-pL-pR)/nights.length,bw=Math.min(46,slot*0.6);
  var order=[["deep","--accent-2","Deep"],["rem","--accent","REM"],["light","--bike","Light"]];
  var max=Math.max.apply(null,nights.map(function(x){var s=x.stages||{};return (s.deep||0)+(s.rem||0)+(s.light||0)+(s.awake||0);}).concat([420]))*1.12;
  function Y(v){return pT+(max-v)/max*(H-pT-pB)}
  [0,240,480].forEach(function(v){
    if(v>max)return;var l=document.createElementNS(ns,"line");l.setAttribute("x1",pL);l.setAttribute("x2",W-pR);
    l.setAttribute("y1",Y(v));l.setAttribute("y2",Y(v));l.setAttribute("stroke",css("--border"));svg.appendChild(l);
    var t=document.createElementNS(ns,"text");t.setAttribute("x",pL-8);t.setAttribute("y",Y(v)+5);t.setAttribute("text-anchor","end");
    t.setAttribute("font-size",15);t.setAttribute("fill",css("--faint"));t.textContent=(v/60)+"h";svg.appendChild(t);
  });
  nights.forEach(function(night,i){
    var s=night.stages||{},bx=pL+i*slot+(slot-bw)/2,base=0;
    order.forEach(function(stage){
      var mins=s[stage[0]]||0;if(mins<=0)return;
      var r=document.createElementNS(ns,"rect");r.setAttribute("x",bx);r.setAttribute("width",bw);
      r.setAttribute("y",Y(base+mins));r.setAttribute("height",Math.max(1,Y(base)-Y(base+mins)));
      r.setAttribute("fill",css(stage[1]));svg.appendChild(r);
      var ttl=document.createElementNS(ns,"title");ttl.textContent=night.label+" "+stage[2]+": "+fmtMinutes(mins);r.appendChild(ttl);
      base+=mins;
    });
    if(s.awake>0){
      var a=document.createElementNS(ns,"rect");a.setAttribute("x",bx);a.setAttribute("width",bw);
      a.setAttribute("y",Y(base+s.awake));a.setAttribute("height",Math.max(1,Y(base)-Y(base+s.awake)));
      a.setAttribute("fill",css("--track"));svg.appendChild(a);
    }
    var t=document.createElementNS(ns,"text");t.setAttribute("x",bx+bw/2);t.setAttribute("y",H-12);
    t.setAttribute("text-anchor","middle");t.setAttribute("font-size",16);t.setAttribute("fill",css("--faint"));
    t.textContent=night.label;svg.appendChild(t);
  });
  var leg=document.getElementById("stageleg");
  if(leg)leg.innerHTML=order.concat([["awake","--track","Awake"]]).map(function(s){
    return '<div class="z"><span class="sw" style="background:'+css(s[1])+'"></span>'+s[2]+'</div>';}).join("");
  chartTip(svg,nights,function(x){var s=x.stages||{};
    return '<b>'+x.label+'</b> · '+n(x.hours)+' h<br>Deep '+fmtMinutes(s.deep)+' · REM '+fmtMinutes(s.rem)+
      '<br>Light '+fmtMinutes(s.light)+' · Awake '+fmtMinutes(s.awake)+
      (x.efficiency?'<br>Efficiency '+x.efficiency+'%':'');});
}

// Overnight HRV against Garmin's own balanced range.
function drawHrv(series){
  var svg=document.getElementById("hrvc");if(!svg||!series.length)return;svg.innerHTML="";
  var W=1000,H=220,pL=44,pR=14,pT=16,pB=32;
  var vals=series.map(function(x){return x.value;}).filter(function(v){return v!=null;});if(!vals.length)return;
  var lo=Math.min.apply(null,vals.concat(series.map(function(x){return x.baselineLow||999;})))*0.85;
  var hi=Math.max.apply(null,vals.concat(series.map(function(x){return x.baselineHigh||0;})))*1.1;
  var range=Math.max(1,hi-lo);
  function X(i){return series.length===1?W/2:pL+i*(W-pL-pR)/(series.length-1)}
  function Y(v){return pT+(hi-v)/range*(H-pT-pB)}
  var banded=series.map(function(x,i){return{x:x,i:i};}).filter(function(v){return v.x.baselineLow!=null&&v.x.baselineHigh!=null;});
  if(banded.length>1){
    var up=banded.map(function(v,k){return(k?"L":"M")+X(v.i).toFixed(1)+" "+Y(v.x.baselineHigh).toFixed(1);}).join(" ");
    var dn=banded.slice().reverse().map(function(v){return"L"+X(v.i).toFixed(1)+" "+Y(v.x.baselineLow).toFixed(1);}).join(" ");
    var band=document.createElementNS(ns,"path");band.setAttribute("d",up+" "+dn+" Z");band.setAttribute("class","rangeband");svg.appendChild(band);
  }
  var pts=[];series.forEach(function(x,i){if(x.value!=null)pts.push([X(i),Y(x.value),x]);});
  var path=document.createElementNS(ns,"path");
  path.setAttribute("d",pts.map(function(p,i){return(i?"L":"M")+p[0].toFixed(1)+" "+p[1].toFixed(1);}).join(" "));
  path.setAttribute("fill","none");path.setAttribute("stroke",css("--accent-2"));path.setAttribute("stroke-width",3);
  path.setAttribute("stroke-linejoin","round");svg.appendChild(path);
  var last=pts[pts.length-1];
  if(last){var c=document.createElementNS(ns,"circle");c.setAttribute("cx",last[0]);c.setAttribute("cy",last[1]);
    c.setAttribute("r",7);c.setAttribute("fill",css("--accent-2"));c.setAttribute("stroke",css("--surface"));
    c.setAttribute("stroke-width",3);svg.appendChild(c);}
  [lo+range*0.1,hi-range*0.1].forEach(function(v){
    var t=document.createElementNS(ns,"text");t.setAttribute("x",pL-8);t.setAttribute("y",Y(v)+5);
    t.setAttribute("text-anchor","end");t.setAttribute("font-size",15);t.setAttribute("fill",css("--faint"));
    t.textContent=Math.round(v);svg.appendChild(t);});
  chartTip(svg,series,function(x){return '<b>'+x.label+'</b><br>'+n(x.value)+' ms'+
    (x.baselineLow?'<br>Balanced '+Math.round(x.baselineLow)+'–'+Math.round(x.baselineHigh)+' ms':'')+
    (x.status?'<br>'+cap(x.status):'');});
}

// Bed-to-wake spans, so an irregular schedule is visible at a glance.
function drawTiming(nights){
  var svg=document.getElementById("timingc");if(!svg)return;svg.innerHTML="";
  var rows=nights.filter(function(x){return x.bedTime!=null&&x.wakeTime!=null;});if(!rows.length)return;
  // Wider gutter than the other charts: "21:00" needs more room than a number.
  var W=1000,H=220,pL=66,pR=14,pT=18,pB=32,slot=(W-pL-pR)/rows.length,bw=Math.min(38,slot*0.5);
  // Plot on an 18:00 -> 12:00 axis so a night reads left-to-right without wrapping.
  var START=18,SPAN=18;
  function Y(hour){var h=hour<START?hour+24:hour;return pT+((h-START)/SPAN)*(H-pT-pB);}
  [21,0,3,6,9].forEach(function(h){
    var y=Y(h),l=document.createElementNS(ns,"line");l.setAttribute("x1",pL);l.setAttribute("x2",W-pR);
    l.setAttribute("y1",y);l.setAttribute("y2",y);l.setAttribute("stroke",css("--border"));svg.appendChild(l);
    var t=document.createElementNS(ns,"text");t.setAttribute("x",pL-8);t.setAttribute("y",y+5);
    t.setAttribute("text-anchor","end");t.setAttribute("font-size",14);t.setAttribute("fill",css("--faint"));
    t.textContent=fmtClock(h);svg.appendChild(t);
  });
  rows.forEach(function(night,i){
    var bx=pL+i*slot+(slot-bw)/2,y1=Y(night.bedTime),y2=Y(night.wakeTime);
    if(y2<y1){var swap=y1;y1=y2;y2=swap;}
    var r=document.createElementNS(ns,"rect");r.setAttribute("x",bx);r.setAttribute("width",bw);
    r.setAttribute("y",y1);r.setAttribute("height",Math.max(3,y2-y1));r.setAttribute("rx",5);
    r.setAttribute("fill",css("--accent"));r.setAttribute("opacity",".85");svg.appendChild(r);
    var ttl=document.createElementNS(ns,"title");
    ttl.textContent=night.label+": "+fmtClock(night.bedTime)+" – "+fmtClock(night.wakeTime);r.appendChild(ttl);
    if(i%2===0||rows.length<8){
      var t=document.createElementNS(ns,"text");t.setAttribute("x",bx+bw/2);t.setAttribute("y",H-9);
      t.setAttribute("text-anchor","middle");t.setAttribute("font-size",15);t.setAttribute("fill",css("--faint"));
      t.textContent=night.label;svg.appendChild(t);}
  });
  chartTip(svg,rows,function(x){return '<b>'+x.label+'</b><br>'+fmtClock(x.bedTime)+' – '+fmtClock(x.wakeTime)+
    '<br>'+n(x.hours)+' h asleep';});
}


function effortStateText(week){
  if(week.partial)return {within:"On pace",below:"Behind pace",above:"Ahead of pace"}[week.state]||"Building baseline";
  return {within:"Within expected range",below:"Below expected range",above:"Above expected range",building:"Building baseline"}[week.state]||"Building baseline";
}

function drawEffort(data,selectedIndex){
  var svg=document.getElementById("effortc"),weeks=data.weeks||[];if(!svg||!weeks.length)return;svg.innerHTML="";var selected=selectedIndex==null?weeks.length-1:Math.max(0,Math.min(weeks.length-1,selectedIndex));
  var W=1000,H=360,pL=56,pR=30,pT=46,pB=48,max=Math.max.apply(null,weeks.map(function(x){return Math.max(x.effort||0,x.rangeHigh||0);}).concat([1]))*1.14;
  function X(i){return pL+i*(W-pL-pR)/Math.max(1,weeks.length-1)}function Y(v){return pT+(max-v)/max*(H-pT-pB)}
  [0,max/2,max].forEach(function(v){var y=Y(v),l=document.createElementNS(ns,"line"),t=document.createElementNS(ns,"text");l.setAttribute("x1",pL);l.setAttribute("x2",W-pR);l.setAttribute("y1",y);l.setAttribute("y2",y);l.setAttribute("stroke",css("--border"));svg.appendChild(l);t.setAttribute("x",pL-10);t.setAttribute("y",y+5);t.setAttribute("text-anchor","end");t.setAttribute("font-size",15);t.setAttribute("fill",css("--faint"));t.textContent=Math.round(v);svg.appendChild(t);});
  var valid=weeks.map(function(x,i){return{x:x,i:i};}).filter(function(v){return v.x.rangeHigh!=null;});if(valid.length){var upper=valid.map(function(v,i){return(i?"L":"M")+X(v.i)+" "+Y(v.x.rangeHigh);}).join(" "),lower=valid.slice().reverse().map(function(v){return"L"+X(v.i)+" "+Y(v.x.rangeLow);}).join(" "),band=document.createElementNS(ns,"path");band.setAttribute("d",upper+" "+lower+" Z");band.setAttribute("class","rangeband");svg.appendChild(band);
    var bl=document.createElementNS(ns,"text");bl.setAttribute("x",W-pR);bl.setAttribute("y",pT-24);bl.setAttribute("text-anchor","end");bl.setAttribute("font-size",14);bl.setAttribute("font-weight",500);bl.setAttribute("fill",css("--faint"));bl.textContent="Shaded = suggested weekly range";svg.appendChild(bl);}
  var line=weeks.map(function(x,i){return(i?"L":"M")+X(i)+" "+Y(x.effort||0);}).join(" "),p=document.createElementNS(ns,"path");p.setAttribute("d",line);p.setAttribute("fill","none");p.setAttribute("stroke",css("--accent"));p.setAttribute("stroke-width",4);p.setAttribute("stroke-linejoin","round");svg.appendChild(p);
  weeks.forEach(function(x,i){var c=document.createElementNS(ns,"circle");c.setAttribute("cx",X(i));c.setAttribute("cy",Y(x.effort||0));c.setAttribute("r",i===selected?9:6);c.setAttribute("fill",i===selected?css("--accent"):css("--surface"));c.setAttribute("stroke",css("--accent"));c.setAttribute("stroke-width",3);svg.appendChild(c);});
  var sel=weeks[selected],vx=Math.max(pL+34,Math.min(W-pR-34,X(selected))),vt=document.createElementNS(ns,"text");vt.setAttribute("x",vx);vt.setAttribute("y",Math.max(pT-8,Y(sel.effort||0)-18));vt.setAttribute("text-anchor","middle");vt.setAttribute("font-size",19);vt.setAttribute("font-weight",800);vt.setAttribute("fill",css("--accent"));vt.textContent=n(sel.effort);svg.appendChild(vt);
  [0,3,6,9,weeks.length-1].filter(function(v,i,a){return v<weeks.length&&a.indexOf(v)===i;}).forEach(function(i){var t=document.createElementNS(ns,"text");t.setAttribute("x",X(i));t.setAttribute("y",H-13);t.setAttribute("text-anchor",i===0?"start":i===weeks.length-1?"end":"middle");t.setAttribute("font-size",15);t.setAttribute("fill",css("--faint"));t.textContent=weeks[i].label;t.setAttribute("font-weight",i===selected?700:400);svg.appendChild(t);});
  chartTip(svg,weeks,function(x){var i=weeks.indexOf(x),prev=i>0?(weeks[i-1].effort||0):null,delta=prev?Math.round(((x.effort||0)-prev)/prev*100):null;
    return '<b>'+x.rangeLabel+'</b><br>'+x.effort+' effort points'+(delta!=null?' · '+(delta>=0?'+':'')+delta+'% vs prior week':'')+'<br>'+effortStateText(x)+'<br>Suggested: '+n(x.rangeLow)+'–'+n(x.rangeHigh)+'<br>Garmin Load: '+x.garminLoad;});
  svg.onclick=function(e){var r=svg.getBoundingClientRect(),chartX=(e.clientX-r.left)/r.width*W,i=Math.round((chartX-pL)/(W-pL-pR)*(weeks.length-1));drawEffort(data,Math.max(0,Math.min(weeks.length-1,i)));};drawEffortWeek(weeks[selected]);
}

function drawEffortWeek(week){
  var state=document.getElementById("effort-state"),value=document.getElementById("effort-value"),meta=document.getElementById("effort-meta"),title=document.getElementById("effort-week"),list=document.getElementById("effort-list");
  if(state){state.textContent=effortStateText(week);state.className='pill '+(week.state==='above'?'warn':week.state==='within'?'good':'mute');}
  if(value)value.textContent=week.effort;
  if(meta)meta.textContent=week.rangeLabel+(week.partial?' · day '+week.daysElapsed+' of 7':'')+' · suggested '+n(week.rangeLow)+'–'+n(week.rangeHigh)+' points'+(week.partial&&week.projected!=null?' · on pace for ~'+week.projected:'')+' · capacity '+n(week.capacity);
  if(title)title.textContent='Daily effort · '+week.rangeLabel;
  if(list){list.innerHTML=(week.activities||[]).map(function(a){
    var day=a.date?new Date(a.date+'T00:00:00').toLocaleDateString(undefined,{weekday:'short',day:'numeric'}):'';
    var ic={swim:"🏊",bike:"🚴",run:"🏃",walk:"🚶"}[a.sport]||"💪";
    var v=a.effort||0,cls=v>=25?' max':v>=13?' hi':'';
    var parts=(a.zonePart!=null&&a.loadPart!=null)?' · aerobic '+n(a.zonePart)+' + intensity '+n(a.loadPart):'';
    return '<div class="effortrow"><span>'+ic+' <b>'+a.name+'</b>'+(day?' · '+day:'')+' · '+a.min+' min'+(a.hr?' · '+a.hr+' bpm':'')+'<br><small>Garmin Load '+n(a.garminLoad)+parts+'</small></span><span class="score'+cls+'">'+n(a.effort)+' pts</span></div>';
  }).join('')||'<div class="meta">No workouts recorded for this week.</div>';}
  var svg=document.getElementById("effortdaily"),days=week.days||[];if(!svg)return;svg.innerHTML="";var W=1000,H=180,pL=34,pR=14,pT=30,pB=38,max=Math.max.apply(null,days.map(function(x){return x.effort||0;}).concat([1]))*1.12,slot=(W-pL-pR)/7,bw=slot*.46;function Y(v){return pT+(max-v)/max*(H-pT-pB)}
  var now=new Date(),todayIso=now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0')+'-'+String(now.getDate()).padStart(2,'0');
  days.forEach(function(x,i){
    var bx=pL+i*slot+(slot-bw)/2,isToday=x.date===todayIso;
    var base=document.createElementNS(ns,"rect");base.setAttribute("x",bx);base.setAttribute("y",H-pB-3);base.setAttribute("width",bw);base.setAttribute("height",3);base.setAttribute("rx",1.5);base.setAttribute("fill",css("--track"));svg.appendChild(base);
    if(x.effort>0){
      var bar=document.createElementNS(ns,"rect");bar.setAttribute("x",bx);bar.setAttribute("y",Y(x.effort));bar.setAttribute("width",bw);bar.setAttribute("height",Math.max(3,H-pB-Y(x.effort)));bar.setAttribute("rx",5);bar.setAttribute("fill",css("--accent"));svg.appendChild(bar);
      var vl=document.createElementNS(ns,"text");vl.setAttribute("x",bx+bw/2);vl.setAttribute("y",Y(x.effort)-8);vl.setAttribute("text-anchor","middle");vl.setAttribute("font-size",17);vl.setAttribute("font-weight",700);vl.setAttribute("fill",css("--text"));vl.textContent=Math.round(x.effort);svg.appendChild(vl);
    }
    var t=document.createElementNS(ns,"text");t.setAttribute("x",bx+bw/2);t.setAttribute("y",H-10);t.setAttribute("text-anchor","middle");t.setAttribute("font-size",17);t.setAttribute("fill",isToday?css("--accent"):css("--faint"));if(isToday)t.setAttribute("font-weight",800);t.textContent=x.label;svg.appendChild(t);
  });
  chartTip(svg,days,function(x){return '<b>'+x.label+'</b><br>'+(x.effort==null?'Not yet':n(x.effort)+' effort points');});
}

function fitnessWindow(series,days){
  if(!series.length)return [];
  var months=days===30?1:days===90?3:days===180?6:days===365?12:24;
  var last=new Date(series[series.length-1].date+'T00:00:00Z');
  var first=new Date(Date.UTC(last.getUTCFullYear(),last.getUTCMonth()-months,1));
  var monthEnd=new Date(Date.UTC(first.getUTCFullYear(),first.getUTCMonth()+1,0)).getUTCDate();
  first.setUTCDate(Math.min(last.getUTCDate(),monthEnd));
  var cutoff=first.toISOString().slice(0,10);
  return series.filter(function(row){return row.date>=cutoff;});
}

// The curve retains decimal precision. Summary changes use the same whole
// scores as the badges, so small movements within one score do not change them.
function fitnessSummary(first,current){
  var startScore=Math.round(first),score=Math.round(current),delta=score-startScore;
  return {score:score,delta:delta,pct:startScore>0?Math.round(delta/startScore*100):null};
}

function drawFitness(series,days,selectedIndex){
  var svg=document.getElementById("fitnessc");if(!svg)return;svg.innerHTML="";var rows=fitnessWindow(series,days);if(!rows.length)return;
  var selected=selectedIndex==null?rows.length-1:Math.max(0,Math.min(rows.length-1,selectedIndex)),first=rows[0].fitness||0,current=rows[selected].fitness||0,change=fitnessSummary(first,current),delta=change.delta,pct=change.pct,summary=document.getElementById("fit-summary"),value=document.getElementById("fit-value"),period=days===30?'month':days===90?'three months':days===180?'six months':days===365?'year':'two years';
  function fmt(date){return new Date(date+'T00:00:00').toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'});}
  if(value)value.textContent=change.score+' index';
  if(summary){var pctText=pct==null?'—%':Math.abs(Math.round(pct))+'%',direction=pct==null?'':delta>0?'▲ ':delta<0?'▼ ':'';summary.innerHTML='<span class="change '+(delta>0?'up':'')+'">'+direction+pctText+'</span><span>'+(delta>0?'+':'')+Math.round(delta)+' pts</span><span class="period">'+(selected===rows.length-1?(rows.length<days-3?'over the '+rows.length+' days recorded so far':'over the past '+period):'from '+fmt(rows[0].date)+' to '+fmt(rows[selected].date))+'</span>';}
  var W=1000,H=360,pL=52,pR=18,pT=46,pB=44,vals=[];rows.forEach(function(x){vals.push(x.fitness||0)});var max=Math.max.apply(null,vals.concat([1]))*1.12;
  function X(i){return pL+i*(W-pL-pR)/Math.max(1,rows.length-1)}function Y(v){return pT+(max-v)/max*(H-pT-pB)}function plot(key,color){var d=rows.map(function(x,i){return(i?"L":"M")+X(i).toFixed(1)+" "+Y(x[key]||0).toFixed(1);}).join(" ");var p=document.createElementNS(ns,"path");p.setAttribute("d",d);p.setAttribute("fill","none");p.setAttribute("stroke",color);p.setAttribute("stroke-width",4);p.setAttribute("stroke-linejoin","round");svg.appendChild(p)}
  [0,max/2,max].forEach(function(v){var l=document.createElementNS(ns,"line"),t=document.createElementNS(ns,"text");l.setAttribute("x1",pL);l.setAttribute("x2",W-pR);l.setAttribute("y1",Y(v));l.setAttribute("y2",Y(v));l.setAttribute("stroke",css("--border"));svg.appendChild(l);t.setAttribute("x",pL-10);t.setAttribute("y",Y(v)+5);t.setAttribute("text-anchor","end");t.setAttribute("font-size",15);t.setAttribute("fill",css("--faint"));t.textContent=Math.round(v);svg.appendChild(t)});
  var area=rows.map(function(x,i){return(i?"L":"M")+X(i).toFixed(1)+" "+Y(x.fitness||0).toFixed(1);}).join(" ")+" L"+X(rows.length-1)+" "+Y(0)+" L"+X(0)+" "+Y(0)+" Z",fill=document.createElementNS(ns,"path");fill.setAttribute("d",area);fill.setAttribute("fill",css("--accent"));fill.setAttribute("fill-opacity",".18");svg.appendChild(fill);plot("fitness",css("--accent"));
  var marker=document.createElementNS(ns,"line"),dot=document.createElementNS(ns,"circle");marker.setAttribute("x1",X(selected));marker.setAttribute("x2",X(selected));marker.setAttribute("y1",pT);marker.setAttribute("y2",H-pB);marker.setAttribute("stroke",css("--accent"));marker.setAttribute("stroke-width",2);svg.appendChild(marker);dot.setAttribute("cx",X(selected));dot.setAttribute("cy",Y(current));dot.setAttribute("r",7);dot.setAttribute("fill",css("--accent"));dot.setAttribute("stroke",css("--surface"));dot.setAttribute("stroke-width",3);svg.appendChild(dot);
  // The selected day's value sits in a badge on top of its marker, the way
  // Strava labels the current day, so the number is readable without hovering.
  (function(){
    var label=String(change.score),bw=Math.max(56,label.length*17+26),bh=34;
    var bx=Math.max(pL,Math.min(W-pR-bw,X(selected)-bw/2));
    var box=document.createElementNS(ns,"rect");
    box.setAttribute("x",bx);box.setAttribute("y",2);box.setAttribute("width",bw);box.setAttribute("height",bh);
    box.setAttribute("rx",9);box.setAttribute("fill",css("--accent"));svg.appendChild(box);
    var t=document.createElementNS(ns,"text");
    t.setAttribute("x",bx+bw/2);t.setAttribute("y",25);t.setAttribute("text-anchor","middle");
    t.setAttribute("font-size",20);t.setAttribute("font-weight",800);t.setAttribute("fill",css("--surface"));
    t.textContent=label;svg.appendChild(t);
  })();
  [0,Math.floor(rows.length/2),rows.length-1].forEach(function(i){var t=document.createElementNS(ns,"text");t.setAttribute("x",X(i));t.setAttribute("y",H-10);t.setAttribute("text-anchor","middle");t.setAttribute("font-size",17);t.setAttribute("fill",css("--faint"));t.textContent=rows[i].label;svg.appendChild(t)});chartTip(svg,rows,function(x){return '<b>'+x.label+'</b><br>Fitness: '+Math.round(x.fitness)+'<br>Fatigue: '+Math.round(x.fatigue)+'<br>Form: '+Math.round(x.form)+'<br>Effort that day: '+x.load+' pts';});
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

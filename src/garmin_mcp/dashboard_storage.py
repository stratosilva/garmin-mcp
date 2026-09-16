"""PostgreSQL persistence for data entered in the dashboard.

Garmin remains the source of truth for Garmin-owned records.  These tables hold
only dashboard-entered measurements and the non-destructive strength overlay.
When DATABASE_URL is absent the dashboard keeps using its legacy files.
"""

import csv
import datetime
import json
import os
import threading
from pathlib import Path


_INSTANCE = None
_INSTANCE_LOCK = threading.Lock()


class DashboardDatabase:
    def __init__(self, url):
        self.url = url
        self._ready = False
        self._ready_lock = threading.Lock()

    def _connect(self):
        import psycopg

        return psycopg.connect(self.url, connect_timeout=10)

    def ensure_schema(self):
        if self._ready:
            return
        with self._ready_lock:
            if self._ready:
                return
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS dashboard_body_measurements (
                        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        measured_at TIMESTAMPTZ NOT NULL,
                        weight_kg DOUBLE PRECISION,
                        fat_pct DOUBLE PRECISION,
                        muscle_pct DOUBLE PRECISION,
                        bone_pct DOUBLE PRECISION,
                        body_water_pct DOUBLE PRECISION,
                        source TEXT NOT NULL DEFAULT 'Basic-Fit',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS dashboard_body_measurements_unique
                    ON dashboard_body_measurements (measured_at, source)
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS dashboard_injury_measurements (
                        measured_on DATE PRIMARY KEY,
                        left_big_toe_strain SMALLINT NOT NULL CHECK (left_big_toe_strain BETWEEN 0 AND 10),
                        left_foot_plantar_fasciitis SMALLINT NOT NULL CHECK (left_foot_plantar_fasciitis BETWEEN 0 AND 10),
                        right_knee_patellar_tendon SMALLINT NOT NULL CHECK (right_knee_patellar_tendon BETWEEN 0 AND 10),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS dashboard_strength_overlays (
                        activity_id BIGINT PRIMARY KEY,
                        activity_name TEXT,
                        activity_start TIMESTAMPTZ,
                        payload JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS dashboard_manual_strength_workouts (
                        manual_id TEXT PRIMARY KEY,
                        activity_name TEXT NOT NULL,
                        activity_start TIMESTAMPTZ NOT NULL,
                        payload JSONB NOT NULL,
                        merged_garmin_activity_id BIGINT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS dashboard_manual_endurance_activities (
                        manual_id TEXT PRIMARY KEY,
                        sport TEXT NOT NULL CHECK (sport IN ('bike', 'run', 'walk')),
                        activity_name TEXT NOT NULL,
                        activity_start TIMESTAMPTZ NOT NULL,
                        payload JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS dashboard_data_migrations (
                        migration_key TEXT PRIMARY KEY,
                        completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        details JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS dashboard_injury_settings (
                        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                        definitions JSONB NOT NULL
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS dashboard_injury_scores (
                        measured_on DATE PRIMARY KEY,
                        scores JSONB NOT NULL
                    )
                """)
            self._ready = True

    def _migration_done(self, cursor, key):
        cursor.execute("SELECT 1 FROM dashboard_data_migrations WHERE migration_key = %s", (key,))
        return cursor.fetchone() is not None

    def _finish_migration(self, cursor, key, count):
        cursor.execute(
            "INSERT INTO dashboard_data_migrations (migration_key, details) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (migration_key) DO NOTHING",
            (key, json.dumps({"imported": count})),
        )

    def import_legacy_files(self, body_path, injury_path, strength_path):
        """Import each legacy file once. Originals intentionally remain as backups."""
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            self._import_body(cursor, Path(body_path))
            self._import_injuries(cursor, Path(injury_path))
            self._import_strength(cursor, Path(strength_path))

    def _import_body(self, cursor, path):
        key = "legacy-body-csv-v1"
        if self._migration_done(cursor, key):
            return
        count = 0
        if path.exists():
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if not row.get("timestamp"):
                        continue
                    values = [row.get(field) or None for field in
                              ("weight_kg", "fat_pct", "muscle_pct", "bone_pct", "body_water_pct")]
                    cursor.execute("""
                        INSERT INTO dashboard_body_measurements
                            (measured_at, weight_kg, fat_pct, muscle_pct, bone_pct, body_water_pct, source)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (measured_at, source) DO NOTHING
                    """, (row["timestamp"], *values, row.get("source") or "Basic-Fit"))
                    count += cursor.rowcount
        self._finish_migration(cursor, key, count)

    def _import_injuries(self, cursor, path):
        key = "legacy-injury-csv-v1"
        if self._migration_done(cursor, key):
            return
        count = 0
        if path.exists():
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if not row.get("date"):
                        continue
                    cursor.execute("""
                        INSERT INTO dashboard_injury_measurements
                            (measured_on, left_big_toe_strain, left_foot_plantar_fasciitis, right_knee_patellar_tendon)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (measured_on) DO NOTHING
                    """, (row["date"], row.get("left_big_toe_strain"),
                           row.get("left_foot_plantar_fasciitis"), row.get("right_knee_patellar_tendon")))
                    count += cursor.rowcount
        self._finish_migration(cursor, key, count)

    def _import_strength(self, cursor, path):
        key = "legacy-strength-json-v1"
        if self._migration_done(cursor, key):
            return
        count = 0
        try:
            activities = json.loads(path.read_text(encoding="utf-8")).get("activities", {})
        except (OSError, ValueError, TypeError, AttributeError):
            activities = {}
        for activity_id, entry in activities.items():
            if not isinstance(entry, dict):
                continue
            cursor.execute("""
                INSERT INTO dashboard_strength_overlays
                    (activity_id, activity_name, activity_start, payload, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, COALESCE(%s::timestamptz, NOW()))
                ON CONFLICT (activity_id) DO NOTHING
            """, (activity_id, entry.get("activityName"), entry.get("activityStart"),
                   json.dumps(entry), entry.get("updatedAt")))
            count += cursor.rowcount
        self._finish_migration(cursor, key, count)

    def body_measurements(self):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT measured_at, weight_kg, fat_pct, muscle_pct, bone_pct, body_water_pct, source
                FROM dashboard_body_measurements ORDER BY measured_at
            """)
            return cursor.fetchall()

    def add_body_measurement(self, row):
        self.ensure_schema()
        def value(field):
            return None if row[field] == "" else row[field]
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO dashboard_body_measurements
                    (measured_at, weight_kg, fat_pct, muscle_pct, bone_pct, body_water_pct, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (row["timestamp"], value("weight_kg"), value("fat_pct"),
                   value("muscle_pct"), value("bone_pct"),
                   value("body_water_pct"), row["source"]))

    def injury_definitions(self):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT definitions FROM dashboard_injury_settings WHERE singleton = TRUE")
            row = cursor.fetchone()
            return row[0] if row else None

    def save_injury_definitions(self, definitions):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO dashboard_injury_settings (singleton, definitions) VALUES (TRUE, %s::jsonb) "
                           "ON CONFLICT (singleton) DO UPDATE SET definitions = EXCLUDED.definitions", (json.dumps(definitions),))

    def injury_scores(self, start, end):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT measured_on, scores FROM dashboard_injury_scores WHERE measured_on BETWEEN %s AND %s", (start, end))
            return cursor.fetchall()

    def upsert_injury_scores(self, date, scores):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO dashboard_injury_scores (measured_on, scores) VALUES (%s, %s::jsonb) "
                           "ON CONFLICT (measured_on) DO UPDATE SET scores = dashboard_injury_scores.scores || EXCLUDED.scores",
                           (date, json.dumps(scores)))

    def injury_measurements(self, start, end):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT measured_on, left_big_toe_strain, left_foot_plantar_fasciitis,
                       right_knee_patellar_tendon
                FROM dashboard_injury_measurements
                WHERE measured_on BETWEEN %s AND %s ORDER BY measured_on
            """, (start, end))
            return cursor.fetchall()

    def upsert_injury_measurement(self, row):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO dashboard_injury_measurements
                    (measured_on, left_big_toe_strain, left_foot_plantar_fasciitis, right_knee_patellar_tendon)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (measured_on) DO UPDATE SET
                    left_big_toe_strain = EXCLUDED.left_big_toe_strain,
                    left_foot_plantar_fasciitis = EXCLUDED.left_foot_plantar_fasciitis,
                    right_knee_patellar_tendon = EXCLUDED.right_knee_patellar_tendon,
                    updated_at = NOW()
            """, (row["date"], row["left_big_toe_strain"],
                   row["left_foot_plantar_fasciitis"], row["right_knee_patellar_tendon"]))

    def strength_store(self):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT activity_id, payload FROM dashboard_strength_overlays")
            return {"version": 1, "activities": {str(row[0]): row[1] for row in cursor.fetchall()}}

    def save_strength_entry(self, activity_id, entry):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO dashboard_strength_overlays
                    (activity_id, activity_name, activity_start, payload, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, COALESCE(%s::timestamptz, NOW()))
                ON CONFLICT (activity_id) DO UPDATE SET
                    activity_name = EXCLUDED.activity_name,
                    activity_start = EXCLUDED.activity_start,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at
            """, (activity_id, entry.get("activityName"), entry.get("activityStart"),
                   json.dumps(entry), entry.get("updatedAt")))

    def delete_manual_activity(self, manual_id, kind):
        tables = {"strength": "dashboard_manual_strength_workouts",
                  "endurance": "dashboard_manual_endurance_activities"}
        if kind not in tables:
            raise ValueError("invalid manual activity type")
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            sql = "DELETE FROM " + tables[kind] + " WHERE manual_id = %s"
            if kind == "strength":
                sql += " AND merged_garmin_activity_id IS NULL AND COALESCE(payload->>'source', 'manual') <> 'merged'"
            cursor.execute(sql, (manual_id,))
            return cursor.rowcount > 0

    def manual_strength_workouts(self):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT manual_id, payload, merged_garmin_activity_id
                FROM dashboard_manual_strength_workouts
                ORDER BY activity_start DESC
            """)
            workouts = {}
            for manual_id, payload, merged_id in cursor.fetchall():
                row = dict(payload or {})
                row["manualId"] = manual_id
                row["mergedGarminActivityId"] = merged_id
                workouts[manual_id] = row
            return workouts

    def save_manual_strength_workout(self, manual_id, entry):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO dashboard_manual_strength_workouts
                    (manual_id, activity_name, activity_start, payload, merged_garmin_activity_id, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, NOW())
                ON CONFLICT (manual_id) DO UPDATE SET
                    activity_name = EXCLUDED.activity_name,
                    activity_start = EXCLUDED.activity_start,
                    payload = EXCLUDED.payload,
                    merged_garmin_activity_id = EXCLUDED.merged_garmin_activity_id,
                    updated_at = NOW()
            """, (manual_id, entry.get("activityName"), entry.get("activityStart"),
                   json.dumps(entry), entry.get("mergedGarminActivityId")))

    def manual_endurance_activities(self):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT manual_id, payload FROM dashboard_manual_endurance_activities ORDER BY activity_start DESC")
            return {manual_id: dict(payload or {}, manualId=manual_id) for manual_id, payload in cursor.fetchall()}

    def save_manual_endurance_activity(self, manual_id, entry):
        self.ensure_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO dashboard_manual_endurance_activities
                    (manual_id, sport, activity_name, activity_start, payload, updated_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (manual_id) DO UPDATE SET
                    sport = EXCLUDED.sport, activity_name = EXCLUDED.activity_name,
                    activity_start = EXCLUDED.activity_start, payload = EXCLUDED.payload, updated_at = NOW()
            """, (manual_id, entry.get("sport"), entry.get("activityName"), entry.get("activityStart"), json.dumps(entry)))


def database(body_path=None, injury_path=None, strength_path=None):
    """Return the configured database, lazily initialised and legacy-imported."""
    global _INSTANCE
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    if _INSTANCE is None or _INSTANCE.url != url:
        with _INSTANCE_LOCK:
            if _INSTANCE is None or _INSTANCE.url != url:
                _INSTANCE = DashboardDatabase(url)
                _INSTANCE.ensure_schema()
                if body_path and injury_path and strength_path:
                    _INSTANCE.import_legacy_files(body_path, injury_path, strength_path)
    return _INSTANCE

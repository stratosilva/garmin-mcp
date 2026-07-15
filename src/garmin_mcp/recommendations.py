"""
Training and Diet Recommendations functions for Garmin Connect MCP Server
"""
import datetime
import json
from typing import Any, Dict, List, Optional, Tuple, Union

# The garmin_client will be set by the main file
garmin_client = None


def _to_json_str(data):
    """Convert data to JSON string if it's not already a string"""
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, indent=2, default=str)
    except (TypeError, ValueError):
        return str(data)


def configure(client):
    """Configure the module with the Garmin client instance"""
    global garmin_client
    garmin_client = client


def _today() -> datetime.date:
    return datetime.datetime.now().date()


def _parse_iso_date(value: str) -> Optional[datetime.date]:
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_single_date(value: Optional[str]) -> Optional[datetime.date]:
    if not value:
        return None
    value_clean = value.strip().lower()
    if value_clean in ("today", "current day", "now"):
        return _today()
    if value_clean in ("yesterday", "prev day", "previous day"):
        return _today() - datetime.timedelta(days=1)
    if value_clean in ("tomorrow", "next day"):
        return _today()
    return _parse_iso_date(value_clean)


def _last_day_of_month(date_val: datetime.date) -> datetime.date:
    if date_val.month == 12:
        return date_val.replace(day=31)
    first_next = date_val.replace(day=1, month=date_val.month + 1)
    return first_next - datetime.timedelta(days=1)


def _resolve_relative_range(value: Optional[str]) -> Optional[Tuple[datetime.date, datetime.date]]:
    if not value:
        return None
    today = _today()
    clean = value.strip().lower()

    if clean in ("today", "current day", "now"):
        return today, today
    if clean in ("yesterday", "prev day", "previous day"):
        d = today - datetime.timedelta(days=1)
        return d, d
    if clean in ("this week", "current week"):
        start = today - datetime.timedelta(days=today.weekday())
        end = start + datetime.timedelta(days=6)
        return start, end
    if clean in ("last week", "previous week"):
        current_start = today - datetime.timedelta(days=today.weekday())
        start = current_start - datetime.timedelta(days=7)
        end = current_start - datetime.timedelta(days=1)
        return start, end
    if clean in ("this week to date", "current week to date", "week to date"):
        start = today - datetime.timedelta(days=today.weekday())
        return start, today
    if clean in ("this month", "current month"):
        start = today.replace(day=1)
        end = _last_day_of_month(today)
        return start, end
    if clean in ("this month to date", "current month to date", "month to date"):
        start = today.replace(day=1)
        return start, today
    if clean in ("last month", "previous month"):
        first_this = today.replace(day=1)
        last_prev = first_this - datetime.timedelta(days=1)
        start = last_prev.replace(day=1)
        return start, last_prev
    if clean in ("last 7 days", "past 7 days", "previous 7 days", "last seven days"):
        start = today - datetime.timedelta(days=6)
        return start, today
    if clean in ("last 14 days", "past 14 days", "previous 14 days", "last two weeks"):
        start = today - datetime.timedelta(days=13)
        return start, today
    if clean in (
        "last 28 days",
        "past 28 days",
        "previous 28 days",
        "last four weeks",
        "past four weeks",
        "previous four weeks",
    ):
        start = today - datetime.timedelta(days=27)
        return start, today
    if clean in ("last 90 days", "past 90 days", "previous 90 days", "last three months"):
        start = today - datetime.timedelta(days=89)
        return start, today

    return None


def _clamp_range_to_today(start: datetime.date, end: datetime.date) -> Tuple[datetime.date, datetime.date]:
    today = _today()
    if end > today:
        end = today
    if start > end:
        start = end
    return start, end


def _resolve_date_range(start_str: Optional[str], end_str: Optional[str]) -> Tuple[datetime.date, datetime.date]:
    if start_str:
        rel_range = _resolve_relative_range(start_str)
        if rel_range and (not end_str or end_str.strip().lower() == start_str.strip().lower()):
            start, end = _clamp_range_to_today(*rel_range)
            return start, end

    if end_str:
        rel_range = _resolve_relative_range(end_str)
        if rel_range and (not start_str or start_str.strip().lower() == end_str.strip().lower()):
            start, end = _clamp_range_to_today(*rel_range)
            return start, end

    start_date = _parse_single_date(start_str)
    end_date = _parse_single_date(end_str)

    if start_date is None and end_date is None:
        raise ValueError("Unable to resolve date range from inputs.")
    if start_date is None:
        start_date = end_date
    if end_date is None:
        end_date = start_date

    start_date, end_date = _clamp_range_to_today(start_date, end_date)
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def _resolve_anchor_period(period: str, anchor_value: Optional[str]) -> Tuple[datetime.date, datetime.date, datetime.date]:
    today = _today()
    if anchor_value:
        rel_range = _resolve_relative_range(anchor_value)
        if rel_range:
            start, end = _clamp_range_to_today(*rel_range)
            anchor_used = end
            return start, end, anchor_used
        parsed_anchor = _parse_single_date(anchor_value)
        if parsed_anchor:
            anchor = min(parsed_anchor, today)
        else:
            anchor = today
    else:
        anchor = today

    if period == "daily":
        start = end = anchor
    elif period == "weekly":
        start = anchor - datetime.timedelta(days=anchor.weekday())
        end = start + datetime.timedelta(days=6)
    elif period == "monthly":
        start = anchor.replace(day=1)
        end = _last_day_of_month(anchor)
    else:
        start = end = anchor

    start, end = _clamp_range_to_today(start, end)
    # Anchor should reflect the clamped window end for consistency
    anchor_used = min(anchor, end)
    return start, end, anchor_used


def register_tools(app):
    """Register all recommendation tools with the MCP server app"""
    
    @app.tool()
    async def get_optimized_health_data(
        start_date: str,
        end_date: str,
        include_activities: bool = True,
        include_sleep: bool = True,
        include_stress: bool = True,
        include_body_battery: bool = True,
        include_training_readiness: bool = True,
        include_hrv: bool = False,
        activity_type: str = ""
    ) -> str:
        """Optimized tool to fetch multiple health and training data points in one call.
        
        This tool efficiently retrieves comprehensive health and training data for a date range,
        reducing the need for multiple individual tool calls.
        
        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            include_activities: Whether to include activity data (default: True)
            include_sleep: Whether to include sleep data (default: True)
            include_stress: Whether to include stress data (default: True)
            include_body_battery: Whether to include body battery data (default: True)
            include_training_readiness: Whether to include training readiness data (default: True)
            include_hrv: Whether to include HRV data (default: False, as it's more resource-intensive)
            activity_type: Optional activity type filter (e.g., "running", "cycling")
        """
        try:
            result = {
                "date_range": {"start": start_date, "end": end_date},
                "data": {}
            }
            
            # Get activities if requested
            if include_activities:
                try:
                    activities = garmin_client.get_activities_by_date(
                        start_date, end_date, activity_type
                    )
                    result["data"]["activities"] = activities if activities else []
                except Exception as e:
                    result["data"]["activities"] = f"Error: {str(e)}"
            
            # Get daily data for each date in range
            start = datetime.datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.datetime.strptime(end_date, "%Y-%m-%d")
            current = start
            
            daily_data = []
            while current <= end:
                date_str = current.strftime("%Y-%m-%d")
                day_data = {"date": date_str}
                
                # Get sleep data
                if include_sleep:
                    try:
                        sleep = garmin_client.get_sleep_data(date_str)
                        day_data["sleep"] = sleep if sleep else None
                    except Exception:
                        day_data["sleep"] = None
                
                # Get stress data
                if include_stress:
                    try:
                        stress = garmin_client.get_stress_data(date_str)
                        day_data["stress"] = stress if stress else None
                    except Exception:
                        day_data["stress"] = None
                
                # Get body battery
                if include_body_battery:
                    try:
                        # Body battery requires date range, so get single day
                        battery = garmin_client.get_body_battery(date_str, date_str)
                        day_data["body_battery"] = battery if battery else None
                    except Exception:
                        day_data["body_battery"] = None
                
                # Get training readiness
                if include_training_readiness:
                    try:
                        readiness = garmin_client.get_training_readiness(date_str)
                        day_data["training_readiness"] = readiness if readiness else None
                    except Exception:
                        day_data["training_readiness"] = None
                
                # Get HRV (optional, more resource-intensive)
                if include_hrv:
                    try:
                        hrv = garmin_client.get_hrv_data(date_str)
                        day_data["hrv"] = hrv if hrv else None
                    except Exception:
                        day_data["hrv"] = None
                
                # Get steps and basic stats
                try:
                    stats = garmin_client.get_stats(date_str)
                    day_data["stats"] = stats if stats else None
                except Exception:
                    day_data["stats"] = None
                
                daily_data.append(day_data)
                current += datetime.timedelta(days=1)
            
            result["data"]["daily_summary"] = daily_data
            
            # Get overall training metrics
            try:
                # Get max metrics for the end date (most recent)
                max_metrics = garmin_client.get_max_metrics(end_date)
                result["data"]["max_metrics"] = max_metrics if max_metrics else None
            except Exception:
                result["data"]["max_metrics"] = None
            
            return _to_json_str(result)
        except Exception as e:
            return f"Error retrieving optimized health data: {str(e)}"
    
    @app.tool()
    async def get_training_and_diet_recommendations(
        context: str,
        health_data_json: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        focus_area: Optional[str] = None
    ) -> str:
        """Generate training and diet recommendations based on recent health and training data.
        
        This tool analyzes health data (either provided directly or fetched automatically) and
        provides personalized training and diet recommendations based on the user's context.
        
        Args:
            context: User's request or context (e.g., "I want to improve my running performance",
                    "I'm feeling tired and need recovery advice", "Prepare for a marathon")
            health_data_json: Optional pre-fetched health data JSON (from get_optimized_health_data).
                            If not provided, will fetch data for the last 7 days.
            start_date: Start date for data fetch if health_data_json not provided (default: 7 days ago)
            end_date: End date for data fetch if health_data_json not provided (default: today)
            focus_area: Optional focus area ("performance", "recovery", "weight_loss", "endurance", "strength")
        """
        try:
            # If health data not provided, fetch it using the same logic as get_optimized_health_data
            if not health_data_json:
                if not end_date:
                    end_date = datetime.datetime.now().strftime("%Y-%m-%d")
                if not start_date:
                    start = datetime.datetime.now() - datetime.timedelta(days=7)
                    start_date = start.strftime("%Y-%m-%d")
                
                # Fetch data directly using garmin_client (same logic as get_optimized_health_data)
                health_data = {
                    "date_range": {"start": start_date, "end": end_date},
                    "data": {}
                }
                
                # Get activities
                try:
                    activities = garmin_client.get_activities_by_date(start_date, end_date, "")
                    health_data["data"]["activities"] = activities if activities else []
                except Exception:
                    health_data["data"]["activities"] = []
                
                # Get daily data
                start = datetime.datetime.strptime(start_date, "%Y-%m-%d")
                end = datetime.datetime.strptime(end_date, "%Y-%m-%d")
                current = start
                
                daily_data = []
                while current <= end:
                    date_str = current.strftime("%Y-%m-%d")
                    day_data = {"date": date_str}
                    
                    try:
                        sleep = garmin_client.get_sleep_data(date_str)
                        day_data["sleep"] = sleep if sleep else None
                    except Exception:
                        day_data["sleep"] = None
                    
                    try:
                        stress = garmin_client.get_stress_data(date_str)
                        day_data["stress"] = stress if stress else None
                    except Exception:
                        day_data["stress"] = None
                    
                    try:
                        battery = garmin_client.get_body_battery(date_str, date_str)
                        day_data["body_battery"] = battery if battery else None
                    except Exception:
                        day_data["body_battery"] = None
                    
                    try:
                        readiness = garmin_client.get_training_readiness(date_str)
                        day_data["training_readiness"] = readiness if readiness else None
                    except Exception:
                        day_data["training_readiness"] = None
                    
                    try:
                        stats = garmin_client.get_stats(date_str)
                        day_data["stats"] = stats if stats else None
                    except Exception:
                        day_data["stats"] = None
                    
                    daily_data.append(day_data)
                    current += datetime.timedelta(days=1)
                
                health_data["data"]["daily_summary"] = daily_data
                
                try:
                    max_metrics = garmin_client.get_max_metrics(end_date)
                    health_data["data"]["max_metrics"] = max_metrics if max_metrics else None
                except Exception:
                    health_data["data"]["max_metrics"] = None
            else:
                health_data = json.loads(health_data_json) if isinstance(health_data_json, str) else health_data_json
            
            # Analyze the data
            recommendations = {
                "context": context,
                "focus_area": focus_area or "general",
                "analysis": {},
                "training_recommendations": [],
                "diet_recommendations": [],
                "recovery_recommendations": []
            }
            
            # Extract key metrics from health data
            daily_summary = health_data.get("data", {}).get("daily_summary", [])
            activities = health_data.get("data", {}).get("activities", [])
            
            # Analyze sleep patterns
            sleep_scores = []
            avg_sleep_duration = 0
            for day in daily_summary:
                sleep = day.get("sleep")
                if sleep and isinstance(sleep, dict):
                    # Extract sleep duration if available
                    if "sleepTimeSeconds" in sleep:
                        sleep_scores.append(sleep.get("sleepTimeSeconds", 0) / 3600)  # Convert to hours
                    elif "sleepQuality" in sleep:
                        sleep_scores.append(sleep.get("sleepQuality", {}).get("overallSleepValue", 0))
            
            if sleep_scores:
                avg_sleep_duration = sum(sleep_scores) / len(sleep_scores)
                recommendations["analysis"]["average_sleep_hours"] = round(avg_sleep_duration, 1)
            
            # Analyze body battery trends
            body_battery_values = []
            for day in daily_summary:
                battery = day.get("body_battery")
                if battery and isinstance(battery, dict):
                    # Extract body battery values
                    if isinstance(battery, list) and len(battery) > 0:
                        for entry in battery:
                            if isinstance(entry, dict) and "bodyBatteryValue" in entry:
                                body_battery_values.append(entry.get("bodyBatteryValue", 0))
            
            if body_battery_values:
                avg_body_battery = sum(body_battery_values) / len(body_battery_values)
                recommendations["analysis"]["average_body_battery"] = round(avg_body_battery, 1)
            
            # Analyze training readiness
            readiness_scores = []
            for day in daily_summary:
                readiness = day.get("training_readiness")
                if readiness and isinstance(readiness, dict):
                    if "trainingReadiness" in readiness:
                        readiness_scores.append(readiness.get("trainingReadiness", {}).get("value", 0))
            
            if readiness_scores:
                avg_readiness = sum(readiness_scores) / len(readiness_scores)
                recommendations["analysis"]["average_training_readiness"] = round(avg_readiness, 1)
            
            # Analyze activity volume
            activity_count = len(activities) if isinstance(activities, list) else 0
            recommendations["analysis"]["activity_count"] = activity_count
            
            # Generate recommendations based on analysis
            # Training recommendations
            if avg_sleep_duration < 7:
                recommendations["training_recommendations"].append(
                    "Consider reducing training intensity - your average sleep is below 7 hours, "
                    "which may indicate insufficient recovery."
                )
            elif avg_sleep_duration >= 8:
                recommendations["training_recommendations"].append(
                    "Good sleep duration! You're well-rested for training. Consider maintaining or "
                    "slightly increasing training volume."
                )
            
            if body_battery_values and avg_body_battery < 50:
                recommendations["training_recommendations"].append(
                    "Your body battery is consistently low. Focus on recovery activities like "
                    "light walking, yoga, or complete rest days."
                )
            elif body_battery_values and avg_body_battery > 70:
                recommendations["training_recommendations"].append(
                    "Excellent body battery levels! This is a good time for high-intensity training sessions."
                )
            
            if readiness_scores and avg_readiness < 50:
                recommendations["training_recommendations"].append(
                    "Training readiness is low. Prioritize recovery: easy pace workouts, stretching, "
                    "and adequate rest between sessions."
                )
            elif readiness_scores and avg_readiness > 70:
                recommendations["training_recommendations"].append(
                    "High training readiness detected! Ideal time for challenging workouts, "
                    "intervals, or long-distance training."
                )
            
            if activity_count == 0:
                recommendations["training_recommendations"].append(
                    "No activities recorded in this period. Start with light to moderate intensity "
                    "activities and gradually build up."
                )
            elif activity_count > 5:
                recommendations["training_recommendations"].append(
                    f"You've been very active ({activity_count} activities). Ensure you're including "
                    "adequate rest days to prevent overtraining."
                )
            
            # Diet recommendations
            if avg_sleep_duration < 7:
                recommendations["diet_recommendations"].append(
                    "Prioritize foods rich in magnesium and tryptophan (nuts, seeds, turkey, bananas) "
                    "to support better sleep quality."
                )
            
            if body_battery_values and avg_body_battery < 50:
                recommendations["diet_recommendations"].append(
                    "Focus on nutrient-dense foods: complex carbohydrates for sustained energy, "
                    "lean proteins for recovery, and plenty of fruits/vegetables for micronutrients."
                )
            
            if activity_count > 0:
                recommendations["diet_recommendations"].append(
                    "Ensure adequate protein intake (1.6-2.2g per kg body weight) to support "
                    "muscle recovery and adaptation from your training."
                )
                recommendations["diet_recommendations"].append(
                    "Time your carbohydrate intake around workouts - consume 30-60g carbs 1-2 hours "
                    "before exercise and replenish within 30 minutes post-workout."
                )
            
            # Recovery recommendations
            if avg_sleep_duration < 7 or (body_battery_values and avg_body_battery < 50):
                recommendations["recovery_recommendations"].append(
                    "Prioritize sleep hygiene: maintain consistent sleep schedule, reduce screen time "
                    "before bed, and create a dark, cool sleeping environment."
                )
            
            if readiness_scores and avg_readiness < 50:
                recommendations["recovery_recommendations"].append(
                    "Consider active recovery: light stretching, foam rolling, or gentle yoga. "
                    "Stay hydrated and ensure adequate rest between training sessions."
                )
            
            # Context-specific recommendations
            context_lower = context.lower()
            if "marathon" in context_lower or "endurance" in context_lower:
                recommendations["training_recommendations"].append(
                    "For endurance training: gradually increase weekly mileage by 10%, include "
                    "one long run per week, and maintain easy pace for 80% of training."
                )
                recommendations["diet_recommendations"].append(
                    "Endurance focus: Increase carbohydrate intake to 6-10g per kg body weight. "
                    "Focus on whole grains, fruits, and starchy vegetables."
                )
            
            if "performance" in context_lower or "improve" in context_lower:
                recommendations["training_recommendations"].append(
                    "Include structured interval training: 1-2 high-intensity sessions per week "
                    "with adequate recovery days between."
                )
            
            if "tired" in context_lower or "fatigue" in context_lower or "recovery" in context_lower:
                recommendations["training_recommendations"].append(
                    "Reduce training intensity and volume. Focus on low-intensity activities "
                    "or complete rest until energy levels improve."
                )
                recommendations["diet_recommendations"].append(
                    "Support recovery with anti-inflammatory foods: fatty fish, berries, leafy greens, "
                    "and adequate hydration (35ml per kg body weight)."
                )
            
            if focus_area == "weight_loss":
                recommendations["diet_recommendations"].append(
                    "Create a moderate calorie deficit (300-500 kcal/day). Prioritize protein "
                    "to preserve muscle mass during weight loss."
                )
                recommendations["training_recommendations"].append(
                    "Combine strength training with cardiovascular exercise. Maintain training "
                    "intensity to preserve muscle mass."
                )
            
            return _to_json_str(recommendations)
        except Exception as e:
            return f"Error generating recommendations: {str(e)}"

    @app.tool()
    async def get_period_summary(
        period: str,
        anchor_date: Optional[str] = None,
        include_activities: bool = True,
        include_sleep: bool = True,
        include_stress: bool = True,
        include_body_battery: bool = True,
        include_training_readiness: bool = True,
        include_hrv: bool = False,
        include_stats: bool = True,
        activity_type: str = ""
    ) -> str:
        """Single-pane daily/weekly/monthly summary with rollups and per-day details.
        
        Args:
            period: One of "daily", "weekly", "monthly"
            anchor_date: Reference date in YYYY-MM-DD (defaults to today, local)
            include_activities: Include activities in the period
            include_sleep: Include sleep per-day
            include_stress: Include stress per-day
            include_body_battery: Include body battery per-day
            include_training_readiness: Include training readiness per-day
            include_hrv: Include HRV per-day (heavier)
            include_stats: Include daily stats (steps, calories, etc.)
            activity_type: Optional filter for activities (e.g., "running", "cycling")
        """
        try:
            if period not in ("daily", "weekly", "monthly"):
                return "Invalid period. Must be one of: daily, weekly, monthly."

            start_date, end_date, anchor_used = _resolve_anchor_period(period, anchor_date)

            # Build response skeleton
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d")
            anchor_str = anchor_used.strftime("%Y-%m-%d")

            result = {
                "period": period,
                "date_range": {
                    "start": start_str,
                    "end": end_str,
                    "anchor": anchor_str,
                    "anchor_input": anchor_date,
                },
                "aggregates": {},
                "data": {
                    "activities": [],
                    "daily": []
                }
            }

            # Activities for the entire period
            if include_activities:
                try:
                    activities = garmin_client.get_activities_by_date(
                        start_str, end_str, activity_type
                    )
                    result["data"]["activities"] = activities if activities else []
                except Exception as e:
                    result["data"]["activities"] = f"Error: {str(e)}"

            # Per-day collection
            start_dt = datetime.datetime.strptime(start_str, "%Y-%m-%d")
            end_dt = datetime.datetime.strptime(end_str, "%Y-%m-%d")
            current = start_dt

            # For aggregates
            total_steps = 0
            steps_days = 0
            total_sleep_hours = 0.0
            sleep_days = 0
            readiness_values: List[float] = []
            body_battery_values: List[float] = []
            hrv_values: List[float] = []

            # Activity aggregates (best-effort based on available fields)
            total_activities = 0
            total_distance_m = 0.0
            total_duration_s = 0.0

            # Pre-index activities by date for quicker rollups (best-effort)
            activities_by_date = {}
            try:
                if include_activities and isinstance(result["data"]["activities"], list):
                    for act in result["data"]["activities"]:
                        # Try to resolve local start date string
                        start_time = None
                        if isinstance(act, dict):
                            start_time = act.get("startTimeLocal") or act.get("startTimeGMT")
                        if isinstance(start_time, str) and len(start_time) >= 10:
                            dstr = start_time[:10]
                            activities_by_date.setdefault(dstr, []).append(act)
            except Exception:
                activities_by_date = {}

            while current <= end_dt:
                d = current.strftime("%Y-%m-%d")
                day = {"date": d}

                # Sleep
                if include_sleep:
                    try:
                        sleep = garmin_client.get_sleep_data(d)
                        day["sleep"] = sleep if sleep else None
                        # Extract approximate hours
                        hours = 0.0
                        if isinstance(sleep, dict):
                            daily_sleep = sleep.get("dailySleepDTO", sleep)
                            if isinstance(daily_sleep, dict):
                                secs = daily_sleep.get("sleepTimeSeconds")
                                if isinstance(secs, (int, float)):
                                    hours = float(secs) / 3600.0
                        if hours > 0:
                            total_sleep_hours += hours
                            sleep_days += 1
                    except Exception:
                        day["sleep"] = None

                # Stress
                if include_stress:
                    try:
                        stress = garmin_client.get_stress_data(d)
                        day["stress"] = stress if stress else None
                    except Exception:
                        day["stress"] = None

                # Body battery (single day range)
                if include_body_battery:
                    try:
                        bb = garmin_client.get_body_battery(d, d)
                        day["body_battery"] = bb if bb else None
                        # Aggregate best-effort
                        if isinstance(bb, list):
                            for entry in bb:
                                if isinstance(entry, dict):
                                    val = entry.get("bodyBatteryValue")
                                    if isinstance(val, (int, float)):
                                        body_battery_values.append(float(val))
                    except Exception:
                        day["body_battery"] = None

                # Training readiness
                if include_training_readiness:
                    try:
                        tr = garmin_client.get_training_readiness(d)
                        day["training_readiness"] = tr if tr else None
                        if isinstance(tr, dict):
                            if "trainingReadiness" in tr and isinstance(tr["trainingReadiness"], dict):
                                val = tr["trainingReadiness"].get("value")
                                if isinstance(val, (int, float)):
                                    readiness_values.append(float(val))
                    except Exception:
                        day["training_readiness"] = None

                # HRV
                if include_hrv:
                    try:
                        hrv = garmin_client.get_hrv_data(d)
                        day["hrv"] = hrv if hrv else None
                        if isinstance(hrv, dict):
                            val = hrv.get("avgHrv") or hrv.get("average")
                            if isinstance(val, (int, float)):
                                hrv_values.append(float(val))
                    except Exception:
                        day["hrv"] = None

                # Stats (includes steps)
                if include_stats:
                    try:
                        stats = garmin_client.get_stats(d)
                        day["stats"] = stats if stats else None
                        if isinstance(stats, dict):
                            steps = stats.get("steps") or stats.get("totalSteps") or stats.get("stepCount")
                            if isinstance(steps, (int, float)):
                                total_steps += int(steps)
                                steps_days += 1
                    except Exception:
                        day["stats"] = None

                # Per-day activity quick rollup (best-effort)
                day_acts = activities_by_date.get(d, [])
                if day_acts:
                    day["activities"] = [a for a in day_acts]
                result["data"]["daily"].append(day)
                current += datetime.timedelta(days=1)

            # Activity aggregates across the entire period
            if include_activities and isinstance(result["data"]["activities"], list):
                total_activities = len(result["data"]["activities"])
                for act in result["data"]["activities"]:
                    if not isinstance(act, dict):
                        continue
                    dist = act.get("distance") or act.get("distanceInMeters")
                    dur = act.get("duration") or act.get("durationInSeconds")
                    if isinstance(dist, (int, float)):
                        total_distance_m += float(dist)
                    if isinstance(dur, (int, float)):
                        total_duration_s += float(dur)

            # Build aggregates
            aggregates = {
                "total_activities": total_activities,
                "total_distance_m": round(total_distance_m, 2),
                "total_duration_s": round(total_duration_s, 2),
            }
            if steps_days > 0:
                aggregates["total_steps"] = int(total_steps)
                aggregates["avg_steps_per_day"] = int(round(total_steps / steps_days))
            if sleep_days > 0:
                aggregates["avg_sleep_hours"] = round(total_sleep_hours / sleep_days, 2)
            if readiness_values:
                aggregates["avg_training_readiness"] = round(sum(readiness_values) / len(readiness_values), 2)
            if body_battery_values:
                aggregates["avg_body_battery"] = round(sum(body_battery_values) / len(body_battery_values), 2)
            if hrv_values:
                aggregates["avg_hrv"] = round(sum(hrv_values) / len(hrv_values), 2)

            result["aggregates"] = aggregates
            return _to_json_str(result)
        except Exception as e:
            return f"Error retrieving period summary: {str(e)}"

    @app.tool()
    async def get_trends(
        start_date: str,
        end_date: str,
        include: Optional[List[str]] = None
    ) -> str:
        """Return trends and deltas for key metrics over a range, plus 7/28-day averages if available.
        
        Args:
            start_date: YYYY-MM-DD
            end_date: YYYY-MM-DD
            include: any of ["rhr","hrv","sleep","steps","body_battery","weight","vo2max"]
        """
        try:
            include = include or ["rhr","hrv","sleep","steps","body_battery"]
            try:
                start, end = _resolve_date_range(start_date, end_date)
            except ValueError:
                return "Unable to resolve date range. Provide valid dates or relative phrases (e.g., 'last 28 days')."
            start_str = start.strftime("%Y-%m-%d")
            end_str = end.strftime("%Y-%m-%d")
            
            # Build daily series
            series = []
            current = start
            while current <= end:
                d = current.strftime("%Y-%m-%d")
                entry = {"date": d}
                # RHR
                if "rhr" in include:
                    try:
                        rhr = garmin_client.get_rhr_day(d)
                        if isinstance(rhr, dict):
                            entry["rhr"] = rhr.get("restingHeartRate")
                    except Exception:
                        entry["rhr"] = None
                # HRV
                if "hrv" in include:
                    try:
                        hrv = garmin_client.get_hrv_data(d)
                        if isinstance(hrv, dict):
                            entry["hrv"] = hrv.get("avgHrv") or hrv.get("average")
                    except Exception:
                        entry["hrv"] = None
                # Sleep (hours)
                if "sleep" in include:
                    try:
                        sleep = garmin_client.get_sleep_data(d)
                        hours = None
                        if isinstance(sleep, dict):
                            daily_sleep = sleep.get("dailySleepDTO", sleep)
                            secs = daily_sleep.get("sleepTimeSeconds") if isinstance(daily_sleep, dict) else None
                            if isinstance(secs, (int, float)):
                                hours = round(float(secs)/3600.0, 2)
                        entry["sleep_hours"] = hours
                    except Exception:
                        entry["sleep_hours"] = None
                # Steps
                if "steps" in include:
                    try:
                        stats = garmin_client.get_stats(d)
                        steps = None
                        if isinstance(stats, dict):
                            steps = stats.get("steps") or stats.get("totalSteps") or stats.get("stepCount")
                        entry["steps"] = int(steps) if isinstance(steps, (int, float)) else None
                    except Exception:
                        entry["steps"] = None
                # Body Battery (avg of day)
                if "body_battery" in include:
                    try:
                        bb = garmin_client.get_body_battery(d, d)
                        avg_bb = None
                        if isinstance(bb, list) and bb:
                            vals = []
                            for row in bb:
                                if isinstance(row, dict):
                                    val = row.get("bodyBatteryValue")
                                    if isinstance(val, (int, float)):
                                        vals.append(float(val))
                            if vals:
                                avg_bb = round(sum(vals)/len(vals), 2)
                        entry["body_battery_avg"] = avg_bb
                    except Exception:
                        entry["body_battery_avg"] = None
                # Weight (from body composition single day)
                if "weight" in include:
                    try:
                        body = garmin_client.get_body_composition(d)
                        weight = None
                        if isinstance(body, list) and body:
                            # pick first reading of the day
                            b0 = body[0]
                            if isinstance(b0, dict):
                                weight = b0.get("weight")
                        entry["weight_kg"] = weight
                    except Exception:
                        entry["weight_kg"] = None
                # VO2Max / fitness age snapshot via max_metrics
                if "vo2max" in include:
                    try:
                        mm = garmin_client.get_max_metrics(d)
                        entry["vo2max"] = mm.get("vo2Max") if isinstance(mm, dict) else None
                        entry["fitness_age"] = mm.get("fitnessAge") if isinstance(mm, dict) else None
                    except Exception:
                        entry["vo2max"] = None
                        entry["fitness_age"] = None
                series.append(entry)
                current += datetime.timedelta(days=1)

            # Compute simple deltas and rolling averages
            def _extract(key: str):
                vals = [(e["date"], e.get(key)) for e in series]
                return [(d, v) for (d, v) in vals if isinstance(v, (int, float))]

            trends = {
                "range": {"start": start_str, "end": end_str, "input": {"start": start_date, "end": end_date}},
                "series": series,
                "deltas": {},
                "rolling": {},
            }
            keys = []
            if "rhr" in include: keys.append("rhr")
            if "hrv" in include: keys.append("hrv")
            if "sleep" in include: keys.append("sleep_hours")
            if "steps" in include: keys.append("steps")
            if "body_battery" in include: keys.append("body_battery_avg")
            if "weight" in include: keys.append("weight_kg")
            if "vo2max" in include: keys.append("vo2max")

            for k in keys:
                pts = _extract(k)
                if pts:
                    first = pts[0][1]
                    last = pts[-1][1]
                    trends["deltas"][k] = round(last - first, 2) if isinstance(first, (int, float)) and isinstance(last, (int, float)) else None
                    # 7/28-day averages
                    if len(pts) >= 1:
                        last7 = [v for (_, v) in pts[-7:]]
                        trends["rolling"].setdefault(k, {})["avg_7d"] = round(sum(last7)/len(last7), 2) if last7 else None
                    if len(pts) >= 1:
                        last28 = [v for (_, v) in pts[-28:]]
                        trends["rolling"][k]["avg_28d"] = round(sum(last28)/len(last28), 2) if last28 else None

            return _to_json_str(trends)
        except Exception as e:
            return f"Error retrieving trends: {str(e)}"

    @app.tool()
    async def detect_anomalies(
        start_date: str,
        end_date: str,
        rhr_bpm_increase: int = 5,
        hrv_ms_drop: int = 15,
        sleep_hours_min: float = 6.0,
        steps_drop_pct: float = 30.0
    ) -> str:
        """Detect common wellness anomalies over a range using simple heuristics.
        
        Flags when:
            - Resting HR rises ≥ rhr_bpm_increase vs prior 7-day avg
            - HRV drops ≥ hrv_ms_drop vs prior 7-day avg
            - Sleep hours < sleep_hours_min
            - Steps drop ≥ steps_drop_pct% vs prior 7-day avg
        """
        try:
            # Reuse get_trends-like collection for needed metrics
            include = ["rhr","hrv","sleep","steps"]
            try:
                start, end = _resolve_date_range(start_date, end_date)
            except ValueError:
                return "Unable to resolve date range. Provide valid dates or relative phrases (e.g., 'last week')."

            daily = []
            current = start
            while current <= end:
                d = current.strftime("%Y-%m-%d")
                rec = {"date": d}
                try:
                    rhr = garmin_client.get_rhr_day(d)
                    rec["rhr"] = rhr.get("restingHeartRate") if isinstance(rhr, dict) else None
                except Exception:
                    rec["rhr"] = None
                try:
                    hrv = garmin_client.get_hrv_data(d)
                    rec["hrv"] = hrv.get("avgHrv") or hrv.get("average") if isinstance(hrv, dict) else None
                except Exception:
                    rec["hrv"] = None
                try:
                    sleep = garmin_client.get_sleep_data(d)
                    hours = None
                    if isinstance(sleep, dict):
                        daily_sleep = sleep.get("dailySleepDTO", sleep)
                        secs = daily_sleep.get("sleepTimeSeconds") if isinstance(daily_sleep, dict) else None
                        if isinstance(secs, (int, float)):
                            hours = round(float(secs)/3600.0, 2)
                    rec["sleep_hours"] = hours
                except Exception:
                    rec["sleep_hours"] = None
                try:
                    stats = garmin_client.get_stats(d)
                    steps = None
                    if isinstance(stats, dict):
                        steps = stats.get("steps") or stats.get("totalSteps") or stats.get("stepCount")
                    rec["steps"] = int(steps) if isinstance(steps, (int, float)) else None
                except Exception:
                    rec["steps"] = None
                daily.append(rec)
                current += datetime.timedelta(days=1)

            # Rolling 7-day baselines computed per day
            anomalies = []
            for idx, rec in enumerate(daily):
                window = daily[max(0, idx-7):idx]  # prior 7 days
                baseline = {}
                for key in ("rhr","hrv","steps"):
                    vals = [w.get(key) for w in window if isinstance(w.get(key), (int, float))]
                    baseline[key] = (sum(vals)/len(vals)) if vals else None
                rec_flags = []
                if isinstance(rec.get("rhr"), (int, float)) and isinstance(baseline.get("rhr"), (int, float)):
                    if rec["rhr"] - baseline["rhr"] >= rhr_bpm_increase:
                        rec_flags.append("rhr_elevated")
                if isinstance(rec.get("hrv"), (int, float)) and isinstance(baseline.get("hrv"), (int, float)):
                    if baseline["hrv"] - rec["hrv"] >= hrv_ms_drop:
                        rec_flags.append("hrv_depressed")
                if isinstance(rec.get("sleep_hours"), (int, float)) and rec["sleep_hours"] < sleep_hours_min:
                    rec_flags.append("sleep_short")
                if isinstance(rec.get("steps"), (int, float)) and isinstance(baseline.get("steps"), (int, float)) and baseline["steps"] > 0:
                    drop_pct = 100.0 * (baseline["steps"] - rec["steps"]) / baseline["steps"]
                    if drop_pct >= steps_drop_pct:
                        rec_flags.append("steps_downturn")
                if rec_flags:
                    anomalies.append({"date": rec["date"], "flags": rec_flags})

            return _to_json_str({
                "range": {
                    "start": start.strftime("%Y-%m-%d"),
                    "end": end.strftime("%Y-%m-%d"),
                    "input": {"start": start_date, "end": end_date},
                },
                "anomalies": anomalies
            })
        except Exception as e:
            return f"Error detecting anomalies: {str(e)}"

    @app.tool()
    async def get_readiness_breakdown(date: str) -> str:
        """Provide a simple readiness breakdown (0-100) from available components for a given date.
        
        Components:
          - sleep_hours target 8h (0-100)
          - body_battery avg (0-100)
          - HRV scaled (heuristic: 0-100 by mapping 20-100 ms)
          - stress inverse (if present) 0-100
        """
        try:
            resolved_date = _parse_single_date(date)
            if not resolved_date:
                rel = _resolve_relative_range(date)
                if rel:
                    resolved_date = rel[1]
            if not resolved_date:
                return "Invalid date. Provide YYYY-MM-DD or relative phrases (e.g., 'today', 'last week')."
            date_str = resolved_date.strftime("%Y-%m-%d")

            # Sleep
            sleep_score = None
            try:
                sleep = garmin_client.get_sleep_data(date_str)
                hours = None
                if isinstance(sleep, dict):
                    daily_sleep = sleep.get("dailySleepDTO", sleep)
                    secs = daily_sleep.get("sleepTimeSeconds") if isinstance(daily_sleep, dict) else None
                    if isinstance(secs, (int, float)):
                        hours = float(secs)/3600.0
                if isinstance(hours, float):
                    sleep_score = max(0.0, min(100.0, (hours/8.0)*100.0))
            except Exception:
                pass

            # Body battery average
            bb_score = None
            try:
                bb = garmin_client.get_body_battery(date_str, date_str)
                if isinstance(bb, list) and bb:
                    vals = [float(row.get("bodyBatteryValue")) for row in bb if isinstance(row, dict) and isinstance(row.get("bodyBatteryValue"), (int, float))]
                    if vals:
                        bb_score = sum(vals)/len(vals)
            except Exception:
                pass

            # HRV heuristic scale 20-100 ms -> 0-100
            hrv_score = None
            try:
                hrv = garmin_client.get_hrv_data(date_str)
                val = None
                if isinstance(hrv, dict):
                    val = hrv.get("avgHrv") or hrv.get("average")
                if isinstance(val, (int, float)):
                    v = float(val)
                    # Map 20ms -> 0, 100ms -> 100
                    hrv_score = max(0.0, min(100.0, (v - 20.0) / (100.0 - 20.0) * 100.0))
            except Exception:
                pass

            # Stress inverse (if daily value present)
            stress_score = None
            try:
                stress = garmin_client.get_stress_data(date_str)
                # Heuristic: if stress has avg or total, map 0-100 inversely
                val = None
                if isinstance(stress, dict):
                    val = stress.get("avgStressLevel") or stress.get("stressLevel")
                if isinstance(val, (int, float)):
                    stress_score = max(0.0, min(100.0, 100.0 - float(val)))
            except Exception:
                pass

            # Combine available components equally
            comps = [s for s in [sleep_score, bb_score, hrv_score, stress_score] if isinstance(s, (int, float))]
            readiness = round(sum(comps)/len(comps), 2) if comps else None

            return _to_json_str({
                "date": date_str,
                "input": date,
                "components": {
                    "sleep_score": sleep_score,
                    "body_battery_score": bb_score,
                    "hrv_score": hrv_score,
                    "stress_inverse_score": stress_score
                },
                "readiness_score": readiness
            })
        except Exception as e:
            return f"Error computing readiness breakdown: {str(e)}"

    @app.tool()
    async def get_data_completeness(start_date: str, end_date: str) -> str:
        """Score data completeness for each day and overall across key signals: sleep, steps, HR, HRV, body battery."""
        try:
            try:
                start, end = _resolve_date_range(start_date, end_date)
            except ValueError:
                return "Unable to resolve date range. Provide valid dates or relative phrases (e.g., 'last month')."
            current = start
            per_day = []
            present_days = 0
            total_days = 0
            while current <= end:
                d = current.strftime("%Y-%m-%d")
                total_days += 1
                have = {}
                # sleep
                try:
                    sleep = garmin_client.get_sleep_data(d)
                    have["sleep"] = bool(sleep)
                except Exception:
                    have["sleep"] = False
                # steps (from stats)
                try:
                    stats = garmin_client.get_stats(d)
                    steps = stats.get("steps") if isinstance(stats, dict) else None
                    have["steps"] = isinstance(steps, (int, float))
                except Exception:
                    have["steps"] = False
                # HRV
                try:
                    hrv = garmin_client.get_hrv_data(d)
                    hv = hrv.get("avgHrv") or hrv.get("average") if isinstance(hrv, dict) else None
                    have["hrv"] = isinstance(hv, (int, float))
                except Exception:
                    have["hrv"] = False
                # Body battery
                try:
                    bb = garmin_client.get_body_battery(d, d)
                    have["body_battery"] = isinstance(bb, list) and len(bb) > 0
                except Exception:
                    have["body_battery"] = False
                # Heart rate day summary
                try:
                    hr = garmin_client.get_heart_rates(d)
                    have["hr"] = bool(hr)
                except Exception:
                    have["hr"] = False
                completeness = sum(1 for v in have.values() if v) / 5.0
                if completeness >= 0.6:
                    present_days += 1
                per_day.append({"date": d, "signals": have, "completeness": round(completeness, 2)})
                current += datetime.timedelta(days=1)
            overall = round(present_days / total_days, 2) if total_days > 0 else 0.0
            return _to_json_str({
                "range": {
                    "start": start.strftime("%Y-%m-%d"),
                    "end": end.strftime("%Y-%m-%d"),
                    "input": {"start": start_date, "end": end_date},
                },
                "overall": overall,
                "daily": per_day
            })
        except Exception as e:
            return f"Error computing data completeness: {str(e)}"

    @app.tool()
    async def get_hydration_guidance(
        weight_kg: float,
        training_minutes: int = 0,
        temperature_c: Optional[float] = None
    ) -> str:
        """Provide hydration target guidance (ml) based on weight and training duration, with optional heat factor.
        
        Baseline: 35 ml/kg/day
        Training: +500 ml per 60 min (pro-rate)
        Heat factor: +10% if temperature_c >= 25, +20% if >= 30
        """
        try:
            baseline_ml = 35.0 * float(weight_kg)
            training_ml = 500.0 * (float(training_minutes) / 60.0)
            total_ml = baseline_ml + training_ml
            heat_multiplier = 1.0
            if isinstance(temperature_c, (int, float)):
                t = float(temperature_c)
                if t >= 30.0:
                    heat_multiplier = 1.2
                elif t >= 25.0:
                    heat_multiplier = 1.1
            target_ml = int(round(total_ml * heat_multiplier))
            return _to_json_str({
                "inputs": {"weight_kg": weight_kg, "training_minutes": training_minutes, "temperature_c": temperature_c},
                "baseline_ml": int(round(baseline_ml)),
                "training_ml": int(round(training_ml)),
                "heat_multiplier": heat_multiplier,
                "target_ml": target_ml
            })
        except Exception as e:
            return f"Error computing hydration guidance: {str(e)}"

    @app.tool()
    async def get_coach_cues(period: str, anchor_date: Optional[str] = None) -> str:
        """Provide concise coach cues for a given period using high-signal metrics available.
        
        Uses sleep hours, body battery avg, training readiness, steps trend (vs prior 7d), and activity count.
        """
        try:
            if period not in ("daily", "weekly", "monthly"):
                return "Invalid period. Must be one of: daily, weekly, monthly."
            start_date, end_date, anchor_used = _resolve_anchor_period(period, anchor_date)

            # Collect minimal data
            sd, ed = start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
            # Activities
            acts = []
            try:
                acts = garmin_client.get_activities_by_date(sd, ed, "")
            except Exception:
                acts = []
            activity_count = len(acts) if isinstance(acts, list) else 0

            # Per-day metrics
            cur = start_date
            sleep_hours = []
            bb_values = []
            readiness_scores = []
            steps_series = []
            while cur <= end_date:
                d = cur.strftime("%Y-%m-%d")
                # sleep
                try:
                    sleep = garmin_client.get_sleep_data(d)
                    if isinstance(sleep, dict):
                        ds = sleep.get("dailySleepDTO", sleep)
                        secs = ds.get("sleepTimeSeconds") if isinstance(ds, dict) else None
                        if isinstance(secs, (int, float)):
                            sleep_hours.append(float(secs)/3600.0)
                except Exception:
                    pass
                # body battery
                try:
                    bb = garmin_client.get_body_battery(d, d)
                    if isinstance(bb, list):
                        vals = [float(row.get("bodyBatteryValue")) for row in bb if isinstance(row, dict) and isinstance(row.get("bodyBatteryValue"), (int, float))]
                        if vals:
                            bb_values.append(sum(vals)/len(vals))
                except Exception:
                    pass
                # readiness
                try:
                    tr = garmin_client.get_training_readiness(d)
                    if isinstance(tr, dict) and "trainingReadiness" in tr and isinstance(tr["trainingReadiness"], dict):
                        val = tr["trainingReadiness"].get("value")
                        if isinstance(val, (int, float)):
                            readiness_scores.append(float(val))
                except Exception:
                    pass
                # steps
                try:
                    stats = garmin_client.get_stats(d)
                    steps = stats.get("steps") or stats.get("totalSteps") or stats.get("stepCount") if isinstance(stats, dict) else None
                    if isinstance(steps, (int, float)):
                        steps_series.append(int(steps))
                except Exception:
                    pass
                cur += datetime.timedelta(days=1)

            cues = []
            avg_sleep = sum(sleep_hours)/len(sleep_hours) if sleep_hours else None
            avg_bb = sum(bb_values)/len(bb_values) if bb_values else None
            avg_readiness = sum(readiness_scores)/len(readiness_scores) if readiness_scores else None
            # Steps trend vs prior 7 days before start
            prior_steps_avg = None
            try:
                prior_end = start_date - datetime.timedelta(days=1)
                prior_start = prior_end - datetime.timedelta(days=6)
                if prior_start <= prior_end:
                    cur_prior = prior_start
                    prior_steps = []
                    while cur_prior <= prior_end:
                        d = cur_prior.strftime("%Y-%m-%d")
                        stats = garmin_client.get_stats(d)
                        steps = stats.get("steps") or stats.get("totalSteps") or stats.get("stepCount") if isinstance(stats, dict) else None
                        if isinstance(steps, (int, float)):
                            prior_steps.append(int(steps))
                        cur_prior += datetime.timedelta(days=1)
                    if prior_steps:
                        prior_steps_avg = sum(prior_steps)/len(prior_steps)
            except Exception:
                pass
            steps_change_pct = None
            if steps_series and isinstance(prior_steps_avg, (int, float)) and prior_steps_avg > 0:
                curr_avg = sum(steps_series)/len(steps_series)
                steps_change_pct = round(100.0 * (curr_avg - prior_steps_avg) / prior_steps_avg, 1)

            # Generate cues
            if isinstance(avg_sleep, float):
                if avg_sleep < 7.0:
                    cues.append("Sleep is below 7h on average; prioritize an easy day or mobility.")
                elif avg_sleep >= 8.0:
                    cues.append("Sleep is strong; you can sustain or slightly increase intensity.")
            if isinstance(avg_bb, float):
                if avg_bb < 50:
                    cues.append("Body battery is low; bias toward recovery work or rest.")
                elif avg_bb > 70:
                    cues.append("Body battery is high; green light for quality sessions.")
            if isinstance(avg_readiness, float):
                if avg_readiness < 50:
                    cues.append("Training readiness is low; reduce intensity and focus on recovery.")
                elif avg_readiness > 70:
                    cues.append("Training readiness is high; schedule harder workouts now.")
            if isinstance(steps_change_pct, float):
                if steps_change_pct <= -30.0:
                    cues.append("Activity volume down >30% vs prior week; rebuild gradually to avoid detraining.")
                elif steps_change_pct >= 20.0:
                    cues.append("Activity volume up notably; ensure adequate recovery to avoid overuse.")
            if activity_count == 0:
                cues.append("No activities recorded; start with light sessions and ramp conservatively.")

            summary = {
                "period": period,
                "date_range": {
                    "start": sd,
                    "end": ed,
                    "anchor": anchor_used.strftime("%Y-%m-%d"),
                    "anchor_input": anchor_date,
                },
                "signals": {
                    "avg_sleep_hours": round(avg_sleep, 2) if isinstance(avg_sleep, float) else None,
                    "avg_body_battery": round(avg_bb, 2) if isinstance(avg_bb, float) else None,
                    "avg_training_readiness": round(avg_readiness, 2) if isinstance(avg_readiness, float) else None,
                    "steps_change_pct_vs_prior7": steps_change_pct,
                    "activity_count": activity_count
                },
                "coach_cues": cues
            }
            return _to_json_str(summary)
        except Exception as e:
            return f"Error generating coach cues: {str(e)}"

    return app


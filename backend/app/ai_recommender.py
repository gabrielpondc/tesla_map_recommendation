from __future__ import annotations

import json
import os
from typing import List, Optional, Sequence
from urllib.request import Request, urlopen

from .models import (
    AIPlanRecommendation,
    ChargeStop,
    PlanRequest,
    RouteData,
    StationCandidate,
    StationType,
    Strategy,
)


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "charge_to_percent": {"type": "integer", "enum": [80, 90, 100]},
        "max_detour_km": {"type": "number", "minimum": 1, "maximum": 50},
        "reserve_range_km": {"type": "number", "minimum": 0, "maximum": 500},
        "preferred_station_ids": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
        },
        "search_progress_km": {
            "type": "array",
            "items": {"type": "number", "minimum": 0},
            "maxItems": 12,
        },
        "reason_codes": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "station_gap",
                    "first_leg_gap",
                    "exit_distance",
                    "charge_target",
                    "candidate_quality",
                ],
            },
            "maxItems": 5,
        },
    },
    "required": [
        "charge_to_percent",
        "max_detour_km",
        "reserve_range_km",
        "preferred_station_ids",
        "search_progress_km",
        "reason_codes",
    ],
    "additionalProperties": False,
}


class AIRecommendationError(RuntimeError):
    pass


class AIRecommender:
    def __init__(self) -> None:
        self.base_url = os.getenv("AI_API_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("AI_API_KEY", "")
        self.model = os.getenv("AI_MODEL", "gpt-5.4")
        try:
            configured_timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "18"))
        except ValueError:
            configured_timeout = 18
        self.timeout_seconds = max(5, min(60, configured_timeout))

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @staticmethod
    def _compact_candidates(
        candidates: Sequence[StationCandidate],
        plan_request: PlanRequest,
        baseline_stops: Sequence[ChargeStop],
    ) -> List[dict]:
        # Every Tesla candidate is included, as requested. Tesla mode is an
        # exclusive filter: public/service-area chargers are never selectable AI
        # candidates, regardless of baseline feasibility. Toll-station names stay
        # server-side; the AI receives only numeric access distance/time fields.
        tesla = [item for item in candidates if item.station_type == StationType.TESLA_SUPERCHARGER]
        other = [item for item in candidates if item.station_type != StationType.TESLA_SUPERCHARGER]
        other.sort(key=lambda item: (item.progress_km, item.distance_to_route_km))
        if plan_request.strategy == Strategy.TESLA_PRIORITY:
            other = []
        ordered = sorted([*tesla, *other[:60]], key=lambda item: (item.progress_km, item.distance_to_route_km))
        baseline_by_id = {stop.id: stop for stop in baseline_stops}

        def charge_minutes(item: StationCandidate, arrival_range: float, target_percent: int) -> int:
            target_range = plan_request.full_range_km * target_percent / 100
            added_range = max(0, target_range - arrival_range)
            average_kw = 115 if item.station_type == StationType.TESLA_SUPERCHARGER else 85
            return max(8, round(added_range * 0.172 / average_kw * 60 + 5))

        result: List[dict] = []
        for item in ordered:
            baseline = baseline_by_id.get(item.id)
            exit_drive = item.exit_distance_km or item.distance_to_route_km
            entry_drive = item.entry_distance_km or exit_drive
            first_arrival_range = max(0, plan_request.initial_range_km - item.progress_km - exit_drive)
            arrival_range = baseline.arrival_range_km if baseline else first_arrival_range
            result.append({
                "id": item.id,
                "name": item.name,
                "type": item.station_type.value,
                "progress_km": item.progress_km,
                "exit_drive_km": exit_drive,
                "exit_drive_min": item.exit_duration_min,
                "entry_drive_km": entry_drive,
                "entry_drive_min": item.entry_duration_min,
                "highway_round_trip_km": round(exit_drive + entry_drive, 1),
                "highway_round_trip_min": item.exit_duration_min + item.entry_duration_min,
                "baseline_selected": baseline is not None,
                "arrival_range_km": round(arrival_range, 1),
                "arrival_percent": max(0, round(arrival_range / plan_request.full_range_km * 100)),
                "charge_minutes": {
                    "to_80_percent": charge_minutes(item, arrival_range, 80),
                    "to_90_percent": charge_minutes(item, arrival_range, 90),
                    "to_100_percent": charge_minutes(item, arrival_range, 100),
                },
            })
        return result

    def recommend(
        self,
        plan_request: PlanRequest,
        route: RouteData,
        candidates: Sequence[StationCandidate],
        baseline_stops: Sequence[ChargeStop] = (),
    ) -> Optional[AIPlanRecommendation]:
        if not self.enabled:
            return None
        compact_candidates = self._compact_candidates(
            candidates, plan_request, baseline_stops,
        )
        input_data = {
            "route_distance_km": route.distance_km,
            "initial_range_km": plan_request.initial_range_km,
            "base_reserve_km": plan_request.reserve_range_km,
            "full_range_km": plan_request.full_range_km,
            "requested_charge_to_percent": plan_request.charge_to_percent,
            "requested_max_detour_km": plan_request.max_detour_km,
            "strategy": plan_request.strategy.value,
            "recommendation_mode": plan_request.recommendation_mode.value,
            "constraints": {
                "reserve_must_not_be_lower_than_base": True,
                "prefer_20_km_above_base_when_feasible": True,
                "charge_options": [80, 90, 100],
                "maximum_automatic_detour_km": 30,
                "station_ids_must_come_from_candidates": True,
                "tesla_priority_is_tesla_only": True,
                "toll_stations_are_not_selectable_candidates": True,
            },
            "tesla_candidate_count": sum(
                candidate.station_type == StationType.TESLA_SUPERCHARGER for candidate in candidates
            ),
            "candidates": compact_candidates,
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an EV route-planning fallback optimizer. Output only JSON that matches the schema. "
                        "Never invent a station ID. Preserve at least base_reserve_km and prefer 20 km more reserve "
                        "when the route permits it. Use the provided highway round-trip time, charging time, and "
                        "arrival battery data. Minimize the number of stops: if one charge can reach the destination, "
                        "never recommend another stop. Never select a Tesla station whose exit or entry drive data is "
                        "missing when a feasible station with complete round-trip data exists. Recommend route progress "
                        "positions only for gaps where another verified fast charger should be searched."
                        " When strategy is tesla_priority, select Tesla Superchargers only. Public chargers and "
                        "motorway service-area chargers are forbidden even if an all-Tesla sequence is incomplete; "
                        "use search_progress_km to report a Tesla coverage gap instead."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(input_data, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ev_route_recommendation",
                    "strict": True,
                    "schema": RESPONSE_SCHEMA,
                },
            },
        }
        raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_request = Request(
            f"{self.base_url}/chat/completions",
            data=raw_body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(http_request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            data = json.loads(content)
        # Gateways may surface a read timeout as TimeoutError, socket.timeout,
        # ssl.SSLError, or another OSError depending on the TLS stack. AI is an
        # optional optimizer, so every request/parse failure must become the
        # same controlled fallback instead of aborting the route itself.
        except Exception as exc:
            raise AIRecommendationError("AI JSON recommendation failed") from exc

        # Validate against the exact JSON candidate set sent to the AI, not the
        # wider internal fallback pool retained by the deterministic planner.
        valid_ids = {candidate["id"] for candidate in compact_candidates}
        data["preferred_station_ids"] = [
            station_id for station_id in data.get("preferred_station_ids", []) if station_id in valid_ids
        ]
        data["search_progress_km"] = sorted({
            round(float(progress), 1)
            for progress in data.get("search_progress_km", [])
            if 0 < float(progress) < route.distance_km
        })
        data["charge_to_percent"] = max(plan_request.charge_to_percent, int(data["charge_to_percent"]))
        data["max_detour_km"] = max(
            plan_request.max_detour_km,
            min(30.0, float(data["max_detour_km"])),
        )
        max_reserve = max(
            plan_request.reserve_range_km,
            min(
                plan_request.initial_range_km - 0.1,
                plan_request.full_range_km * data["charge_to_percent"] / 100 - 0.1,
            ),
        )
        data["reserve_range_km"] = min(
            max_reserve,
            max(plan_request.reserve_range_km, float(data["reserve_range_km"])),
        )
        try:
            return AIPlanRecommendation(model=self.model, **data)
        except Exception as exc:
            raise AIRecommendationError("AI JSON recommendation was rejected") from exc

from __future__ import annotations

import re
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, validator


class StationType(str, Enum):
    SERVICE_AREA = "service_area"
    TESLA_SUPERCHARGER = "tesla_supercharger"
    CHARGING_STATION = "charging_station"


class Strategy(str, Enum):
    BALANCED = "balanced"
    TESLA_PRIORITY = "tesla_priority"
    SERVICE_AREA = "service_area"


class RecommendationMode(str, Enum):
    ALGORITHM = "algorithm"
    AI = "ai"


def is_tesla_supercharger_name(name: str) -> bool:
    """Return true only for Tesla Superchargers, not destination chargers."""
    return bool(re.search(
        r"特斯拉.*超级充电|tesla.*supercharger",
        name or "",
        flags=re.IGNORECASE,
    ))


class GeoPoint(BaseModel):
    lng: float = Field(..., ge=-180, le=180)
    lat: float = Field(..., ge=-90, le=90)


class Place(GeoPoint):
    name: str = Field(..., min_length=1, max_length=120)
    address: str = ""


class RouteData(BaseModel):
    distance_km: float
    duration_min: int
    tolls_yuan: float = 0
    polyline: List[GeoPoint]


class PlanRequest(BaseModel):
    origin: Place
    destination: Place
    initial_range_km: float = Field(260, ge=20, le=1200)
    reserve_range_km: float = Field(50, ge=0, le=500)
    full_range_km: float = Field(560, ge=100, le=1200)
    charge_to_percent: int = Field(80, ge=40, le=100)
    strategy: Strategy = Strategy.BALANCED
    recommendation_mode: RecommendationMode = RecommendationMode.ALGORITHM
    map_provider: Literal["amap", "baidu"] = "amap"
    max_detour_km: float = Field(12, ge=1, le=50)
    client_route: Optional[RouteData] = None
    client_candidates: List[dict] = Field(default_factory=list)
    client_route_includes_stops: bool = False

    @validator("reserve_range_km")
    def reserve_must_be_reasonable(cls, value: float, values):
        initial = values.get("initial_range_km")
        if initial is not None and value >= initial:
            raise ValueError("预留续航必须小于起始续航")
        return value

    @validator("charge_to_percent")
    def charged_range_must_exceed_reserve(cls, value: int, values):
        reserve = values.get("reserve_range_km", 0)
        full = values.get("full_range_km", 560)
        if full * value / 100 <= reserve:
            raise ValueError("充电后的目标续航必须大于预留续航")
        return value


class StationCandidate(Place):
    id: str
    station_type: StationType
    progress_km: float = 0
    distance_to_route_km: float = 0
    exit_name: str = ""
    exit_distance_km: float = 0
    exit_duration_min: int = 0
    entry_name: str = ""
    entry_distance_km: float = 0
    entry_duration_min: int = 0
    arrival_detour_km: Optional[float] = None
    departure_detour_km: Optional[float] = None
    source: str = "amap"


class ChargeStop(StationCandidate):
    sequence: int
    leg_distance_km: float
    arrival_range_km: float
    charge_from_percent: int
    charge_to_percent: int
    charge_minutes: int
    added_range_km: float
    route_progress_percent: int


class TripLeg(BaseModel):
    sequence: int
    from_name: str
    to_name: str
    distance_km: float
    duration_min: int
    departure_range_km: float
    arrival_range_km: float


class PlanSummary(BaseModel):
    direct_distance_km: float
    planned_distance_km: float
    drive_duration_min: int
    charge_duration_min: int
    total_duration_min: int
    charging_stops: int
    arrival_range_km: float
    estimated_cost_yuan: float
    requested_reserve_km: float
    planned_reserve_km: float
    planned_charge_to_percent: int
    planned_max_detour_km: float
    uses_recommended_locations: bool = False
    ai_assisted: bool = False


class AIPlanRecommendation(BaseModel):
    model: str
    applied: bool = True
    charge_to_percent: int = Field(..., ge=40, le=100)
    max_detour_km: float = Field(..., ge=1, le=50)
    reserve_range_km: float = Field(..., ge=0, le=500)
    preferred_station_ids: List[str] = Field(default_factory=list, max_length=12)
    search_progress_km: List[float] = Field(default_factory=list, max_length=12)
    reason_codes: List[Literal[
        "station_gap",
        "first_leg_gap",
        "exit_distance",
        "charge_target",
        "candidate_quality",
    ]] = Field(default_factory=list, max_length=5)


class PlanResponse(BaseModel):
    mode: str
    summary: PlanSummary
    route: RouteData
    stops: List[ChargeStop]
    legs: List[TripLeg]
    candidates: List[StationCandidate]
    notices: List[str] = []
    ai_recommendation: Optional[AIPlanRecommendation] = None


class BaiduCandidateRequest(BaseModel):
    route: RouteData
    max_detour_km: float = Field(12, ge=1, le=50)
    map_provider: Literal["amap", "baidu"] = "baidu"


class Suggestion(BaseModel):
    name: str
    address: str = ""
    district: str = ""
    lng: Optional[float] = None
    lat: Optional[float] = None


class AppConfig(BaseModel):
    map_provider: Literal["amap", "baidu"] = "amap"
    map_enabled: bool = False
    amap_enabled: bool
    amap_js_key: str = ""
    amap_security_code: str = ""
    baidu_enabled: bool = False
    baidu_ak: str = ""
    baidu_server_enabled: bool = False
    mode: str
    ai_enabled: bool = False
    ai_model: str = ""

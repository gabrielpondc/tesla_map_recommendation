from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

from .models import (
    GeoPoint,
    Place,
    RouteData,
    StationCandidate,
    StationType,
    Suggestion,
    is_tesla_supercharger_name,
)


AMAP_BASE = "https://restapi.amap.com"


class AmapError(RuntimeError):
    pass


def haversine_km(a: GeoPoint, b: GeoPoint) -> float:
    radius = 6371.0088
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def parse_location(value: str) -> Tuple[float, float]:
    lng, lat = value.split(",", 1)
    return float(lng), float(lat)


def dedupe_points(points: Iterable[GeoPoint]) -> List[GeoPoint]:
    result: List[GeoPoint] = []
    for point in points:
        if not result or haversine_km(result[-1], point) > 0.02:
            result.append(point)
    return result


@dataclass
class Projection:
    progress_km: float
    distance_km: float


def project_on_route(point: GeoPoint, polyline: Sequence[GeoPoint]) -> Projection:
    if len(polyline) < 2:
        return Projection(0, haversine_km(point, polyline[0]))
    best_distance = float("inf")
    best_progress = 0.0
    traversed = 0.0
    ref_lat = math.radians(point.lat)
    x_scale = 111.32 * math.cos(ref_lat)
    y_scale = 110.57
    px, py = point.lng * x_scale, point.lat * y_scale
    for start, end in zip(polyline, polyline[1:]):
        sx, sy = start.lng * x_scale, start.lat * y_scale
        ex, ey = end.lng * x_scale, end.lat * y_scale
        vx, vy = ex - sx, ey - sy
        length_sq = vx * vx + vy * vy
        ratio = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - sx) * vx + (py - sy) * vy) / length_sq))
        nx, ny = sx + ratio * vx, sy + ratio * vy
        distance = math.hypot(px - nx, py - ny)
        segment_km = haversine_km(start, end)
        if distance < best_distance:
            best_distance = distance
            best_progress = traversed + segment_km * ratio
        traversed += segment_km
    return Projection(best_progress, best_distance)


class AmapClient:
    def __init__(self, key: str | None = None):
        self.key = key or os.getenv("AMAP_WEB_KEY", "")

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _get(self, path: str, params: Dict[str, object]) -> Dict:
        if not self.key:
            raise AmapError("尚未配置 AMAP_WEB_KEY")
        query = {**params, "key": self.key, "output": "JSON"}
        url = f"{AMAP_BASE}{path}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"User-Agent": "DianTu-EV-Planner/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise AmapError(f"高德地图服务暂时不可用：{exc}") from exc
        if str(payload.get("status")) != "1":
            raise AmapError(payload.get("info") or payload.get("infocode") or "高德 API 返回异常")
        return payload

    def suggest(self, keywords: str, city: str = "全国") -> List[Suggestion]:
        payload = self._get("/v3/assistant/inputtips", {
            "keywords": keywords,
            "city": city,
            "citylimit": "false",
            "datatype": "all",
        })
        result: List[Suggestion] = []
        for tip in payload.get("tips", []):
            location = tip.get("location")
            if not isinstance(location, str) or "," not in location:
                continue
            lng, lat = parse_location(location)
            result.append(Suggestion(
                name=tip.get("name") or keywords,
                address=tip.get("address") if isinstance(tip.get("address"), str) else "",
                district=tip.get("district") or "",
                lng=lng,
                lat=lat,
            ))
        return result[:8]

    def _convert_gps(self, point: GeoPoint) -> GeoPoint:
        payload = self._get("/v3/assistant/coordinate/convert", {
            "locations": f"{point.lng},{point.lat}",
            "coordsys": "gps",
        })
        locations = payload.get("locations", "")
        if not isinstance(locations, str) or "," not in locations:
            return point
        lng, lat = parse_location(locations)
        return GeoPoint(lng=lng, lat=lat)

    def reverse_geocode(self, point: GeoPoint, from_gps: bool = False) -> Place:
        converted = self._convert_gps(point) if from_gps else point
        payload = self._get("/v3/geocode/regeo", {
            "location": f"{converted.lng},{converted.lat}",
            "extensions": "base",
        })
        regeocode = payload.get("regeocode", {})
        component = regeocode.get("addressComponent", {})
        name = regeocode.get("formatted_address") or "当前位置"
        return Place(
            name=name,
            address=component.get("district") or "",
            lng=converted.lng,
            lat=converted.lat,
        )

    def route(self, origin: GeoPoint, destination: GeoPoint) -> RouteData:
        payload = self._get("/v3/direction/driving", {
            "origin": f"{origin.lng},{origin.lat}",
            "destination": f"{destination.lng},{destination.lat}",
            "strategy": 10,
            "extensions": "base",
        })
        paths = payload.get("route", {}).get("paths", [])
        if not paths:
            raise AmapError("未找到可行的驾车路线")
        path = paths[0]
        points: List[GeoPoint] = []
        for step in path.get("steps", []):
            for raw in str(step.get("polyline", "")).split(";"):
                if "," in raw:
                    lng, lat = parse_location(raw)
                    points.append(GeoPoint(lng=lng, lat=lat))
        points = dedupe_points(points)
        if len(points) < 2:
            points = [GeoPoint(lng=origin.lng, lat=origin.lat), GeoPoint(lng=destination.lng, lat=destination.lat)]
        return RouteData(
            distance_km=round(float(path.get("distance", 0)) / 1000, 1),
            duration_min=max(1, round(float(path.get("duration", 0)) / 60)),
            tolls_yuan=round(float(path.get("tolls", 0) or 0), 1),
            polyline=points,
        )

    def search_around(self, point: GeoPoint, keywords: str, radius_m: int = 12000) -> List[Dict]:
        payload = self._get("/v3/place/around", {
            "location": f"{point.lng},{point.lat}",
            "keywords": keywords,
            "radius": radius_m,
            "sortrule": "distance",
            "offset": 20,
            "page": 1,
            "extensions": "base",
        })
        return payload.get("pois", [])

    def find_chargers(self, route: RouteData, max_detour_km: float) -> List[StationCandidate]:
        if not route.polyline:
            return []
        sample_count = max(2, min(18, math.ceil(route.distance_km / 45)))
        indices = sorted({round(i * (len(route.polyline) - 1) / sample_count) for i in range(sample_count + 1)})
        pois: Dict[str, Dict] = {}
        searches = (
            ("高速服务区 充电站", StationType.SERVICE_AREA),
            ("特斯拉超级充电站", StationType.TESLA_SUPERCHARGER),
        )
        for index in indices:
            point = route.polyline[index]
            for keywords, kind in searches:
                try:
                    for poi in self.search_around(point, keywords, int(max_detour_km * 1000)):
                        raw_location = poi.get("location")
                        if not isinstance(raw_location, str) or "," not in raw_location:
                            continue
                        name = poi.get("name") or "充电站"
                        inferred = kind
                        if is_tesla_supercharger_name(name):
                            inferred = StationType.TESLA_SUPERCHARGER
                        elif "服务区" in name:
                            inferred = StationType.SERVICE_AREA
                        poi["_kind"] = inferred
                        pois[str(poi.get("id") or f"{name}-{raw_location}")] = poi
                except AmapError:
                    continue

        raw_polyline_km = sum(haversine_km(a, b) for a, b in zip(route.polyline, route.polyline[1:]))
        candidates: List[StationCandidate] = []
        for poi_id, poi in pois.items():
            lng, lat = parse_location(poi["location"])
            point = GeoPoint(lng=lng, lat=lat)
            projection = project_on_route(point, route.polyline)
            scaled_progress = projection.progress_km
            if raw_polyline_km:
                scaled_progress *= route.distance_km / raw_polyline_km
            if projection.distance_km <= max_detour_km and 3 < scaled_progress < route.distance_km - 3:
                candidates.append(StationCandidate(
                    id=poi_id,
                    name=poi.get("name") or "充电站",
                    address=poi.get("address") if isinstance(poi.get("address"), str) else "",
                    lng=lng,
                    lat=lat,
                    station_type=poi["_kind"],
                    progress_km=round(scaled_progress, 1),
                    distance_to_route_km=round(projection.distance_km, 1),
                ))
        return sorted(candidates, key=lambda item: (item.progress_km, item.distance_to_route_km))

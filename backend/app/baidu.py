from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Sequence, Tuple

from .amap import haversine_km, project_on_route
from .models import (
    GeoPoint,
    RouteData,
    StationCandidate,
    StationType,
    Suggestion,
    is_tesla_supercharger_name,
)


BAIDU_API_BASE = "https://api.map.baidu.com"


def gcj_to_baidu(point: GeoPoint) -> GeoPoint:
    z = math.sqrt(point.lng * point.lng + point.lat * point.lat) + 0.00002 * math.sin(point.lat * math.pi)
    theta = math.atan2(point.lat, point.lng) + 0.000003 * math.cos(point.lng * math.pi)
    return GeoPoint(lng=z * math.cos(theta) + 0.0065, lat=z * math.sin(theta) + 0.006)


def baidu_to_gcj(point: GeoPoint) -> GeoPoint:
    x = point.lng - 0.0065
    y = point.lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * math.pi)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * math.pi)
    return GeoPoint(lng=z * math.cos(theta), lat=z * math.sin(theta))


class BaiduWebError(RuntimeError):
    pass


class BaiduWebClient:
    def __init__(self, key: str | None = None):
        self.key = key or os.getenv("BAIDU_WEB_AK", "")

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _get(self, path: str, params: Dict[str, object], timeout: int = 10) -> Dict:
        if not self.key:
            raise BaiduWebError("尚未配置 BAIDU_WEB_AK")
        query = urllib.parse.urlencode({**params, "ak": self.key})
        request = urllib.request.Request(
            f"{BAIDU_API_BASE}{path}?{query}",
            headers={"User-Agent": "DianTu-EV-Planner/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise BaiduWebError(f"百度服务端地图暂时不可用：{exc}") from exc
        if int(payload.get("status", -1)) != 0:
            raise BaiduWebError(payload.get("message") or "百度服务端地图返回异常")
        return payload

    def search_nearby(self, point: GeoPoint, query: str, radius_m: int) -> List[Dict]:
        payload = self._get("/place/v2/search", {
            "query": query,
            "location": f"{point.lat},{point.lng}",
            "radius": min(50000, max(1000, radius_m)),
            "radius_limit": "true",
            "scope": 2,
            "page_size": 20,
            "page_num": 0,
            "output": "json",
        })
        return payload.get("results", [])

    def suggest(
        self,
        query: str,
        region: str = "全国",
        map_provider: str = "baidu",
    ) -> List[Suggestion]:
        payload = self._get("/place/v2/suggestion", {
            "query": query,
            "region": region or "全国",
            "city_limit": "false",
            "output": "json",
        })
        suggestions: List[Suggestion] = []
        for raw in payload.get("result", []):
            point = self._point(raw)
            if not point:
                continue
            if map_provider == "amap":
                point = baidu_to_gcj(point)
            address = str(raw.get("address") or "")
            if not address:
                address = "".join(str(raw.get(field) or "") for field in ("province", "city", "district"))
            suggestions.append(Suggestion(
                name=str(raw.get("name") or query),
                address=address,
                district=str(raw.get("district") or ""),
                lng=point.lng,
                lat=point.lat,
            ))
        return suggestions[:8]

    def driving(self, origin: GeoPoint, destination: GeoPoint) -> Tuple[float, int]:
        payload = self._get("/directionlite/v1/driving", {
            "origin": f"{origin.lat},{origin.lng}",
            "destination": f"{destination.lat},{destination.lng}",
        })
        routes = payload.get("result", {}).get("routes", [])
        if not routes:
            raise BaiduWebError("百度未返回驾车距离")
        route = routes[0]
        return round(float(route.get("distance", 0)) / 1000, 1), max(
            1, round(float(route.get("duration", 0)) / 60),
        )

    @staticmethod
    def _point(raw: Dict) -> GeoPoint | None:
        location = raw.get("location") or {}
        try:
            return GeoPoint(lng=float(location["lng"]), lat=float(location["lat"]))
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _distributed(items: Sequence[StationCandidate], limit: int) -> List[StationCandidate]:
        if len(items) <= limit:
            return list(items)
        return [items[round(index * (len(items) - 1) / (limit - 1))] for index in range(limit)]

    def _enrich_tesla(
        self,
        candidate: StationCandidate,
        road_points: Sequence[Tuple[str, GeoPoint]],
    ) -> StationCandidate:
        if not road_points:
            return candidate
        nearest_name, nearest = min(
            road_points,
            key=lambda item: haversine_km(candidate, item[1]),
        )
        updates: Dict[str, object] = {}
        try:
            distance, duration = self.driving(nearest, candidate)
            updates.update({
                "exit_name": nearest_name,
                "exit_distance_km": distance,
                "exit_duration_min": duration,
                "distance_to_route_km": distance,
            })
        except BaiduWebError:
            pass
        try:
            distance, duration = self.driving(candidate, nearest)
            updates.update({
                "entry_name": nearest_name,
                "entry_distance_km": distance,
                "entry_duration_min": duration,
            })
        except BaiduWebError:
            pass
        return candidate.model_copy(update=updates)

    def find_chargers(
        self,
        route: RouteData,
        max_detour_km: float,
        map_provider: str = "baidu",
    ) -> List[StationCandidate]:
        if not route.polyline:
            return []
        search_route = route.model_copy(update={
            "polyline": (
                [gcj_to_baidu(point) for point in route.polyline]
                if map_provider == "amap"
                else route.polyline
            ),
        })
        sample_count = min(24, max(4, math.ceil(route.distance_km / 90)))
        indices = sorted({
            round(index * (len(search_route.polyline) - 1) / sample_count)
            for index in range(sample_count + 1)
        })
        spacing_km = route.distance_km / sample_count
        radius_m = round(min(50000, max(
            30000,
            (spacing_km * 0.65 + max(max_detour_km, 20)) * 1000,
        )))
        searches = (
            ("特斯拉超级充电站", "tesla"),
            ("充电站", "general"),
            ("收费站", "road"),
        )
        jobs = [
            (search_route.polyline[index], query, kind)
            for index in indices
            for query, kind in searches
        ]
        groups: List[Tuple[str, List[Dict]]] = []
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {
                executor.submit(self.search_nearby, point, query, radius_m): kind
                for point, query, kind in jobs
            }
            for future in as_completed(futures):
                try:
                    groups.append((futures[future], future.result()))
                except BaiduWebError:
                    continue

        raw_polyline_km = sum(
            haversine_km(left, right)
            for left, right in zip(search_route.polyline, search_route.polyline[1:])
        )
        seen: set[str] = set()
        candidates: List[StationCandidate] = []
        road_seen: set[str] = set()
        road_points: List[Tuple[str, GeoPoint]] = []
        for kind, results in groups:
            for raw in results:
                name = str(raw.get("name") or "")
                point = self._point(raw)
                if not point:
                    continue
                uid = str(raw.get("uid") or f"{name}-{point.lng}-{point.lat}")
                if kind == "road":
                    if uid not in road_seen and any(token in name for token in ("收费站", "高速出口", "高速入口")):
                        road_seen.add(uid)
                        road_points.append((name, point))
                    continue
                is_tesla = is_tesla_supercharger_name(name)
                is_service = "服务区" in name
                if kind == "tesla" and not is_tesla:
                    continue
                if "充电" not in name and not is_tesla:
                    continue
                candidate_id = f"baidu-web-{uid}"
                if candidate_id in seen:
                    continue
                seen.add(candidate_id)
                projection = project_on_route(point, search_route.polyline)
                progress = projection.progress_km
                if raw_polyline_km:
                    progress *= route.distance_km / raw_polyline_km
                if not (3 < progress < route.distance_km - 3) or projection.distance_km > 50:
                    continue
                station_type = (
                    StationType.TESLA_SUPERCHARGER if is_tesla
                    else StationType.SERVICE_AREA if is_service
                    else StationType.CHARGING_STATION
                )
                candidates.append(StationCandidate(
                    id=candidate_id,
                    name=name,
                    address=str(raw.get("address") or ""),
                    lng=point.lng,
                    lat=point.lat,
                    station_type=station_type,
                    progress_km=round(progress, 1),
                    distance_to_route_km=round(projection.distance_km, 1),
                    source="baidu_web",
                ))

        candidates.sort(key=lambda item: (item.progress_km, item.distance_to_route_km))
        tesla = [item for item in candidates if item.station_type == StationType.TESLA_SUPERCHARGER]
        to_enrich = self._distributed(tesla, 12)
        enriched: Dict[str, StationCandidate] = {}
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {
                executor.submit(self._enrich_tesla, candidate, road_points): candidate.id
                for candidate in to_enrich
            }
            for future in as_completed(futures):
                try:
                    enriched[futures[future]] = future.result()
                except Exception:
                    continue
        result = [enriched.get(candidate.id, candidate) for candidate in candidates]
        if map_provider != "amap":
            return result
        converted: List[StationCandidate] = []
        for candidate in result:
            point = baidu_to_gcj(candidate)
            converted.append(candidate.model_copy(update={
                "lng": point.lng,
                "lat": point.lat,
                "source": "amap_baidu_web",
            }))
        return converted

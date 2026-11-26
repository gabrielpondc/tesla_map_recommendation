from __future__ import annotations

import hashlib
import heapq
import math
from typing import Dict, List, Optional, Sequence, Tuple

from .ai_recommender import AIRecommendationError, AIRecommender
from .amap import AmapClient, AmapError, haversine_km, project_on_route
from .models import (
    AIPlanRecommendation,
    ChargeStop,
    GeoPoint,
    PlanRequest,
    PlanResponse,
    PlanSummary,
    RecommendationMode,
    RouteData,
    StationCandidate,
    StationType,
    Strategy,
    TripLeg,
)


def _interpolate(origin: GeoPoint, destination: GeoPoint, count: int = 40) -> List[GeoPoint]:
    # A gentle curve makes demo-mode route visualization clearer than a single line.
    points: List[GeoPoint] = []
    for index in range(count + 1):
        ratio = index / count
        bend = math.sin(ratio * math.pi) * 0.055
        points.append(GeoPoint(
            lng=origin.lng + (destination.lng - origin.lng) * ratio + bend,
            lat=origin.lat + (destination.lat - origin.lat) * ratio - bend * 0.24,
        ))
    return points


def demo_route(request: PlanRequest) -> RouteData:
    straight = haversine_km(request.origin, request.destination)
    distance = max(8, straight * 1.16)
    return RouteData(
        distance_km=round(distance, 1),
        duration_min=max(12, round(distance / 87 * 60)),
        tolls_yuan=round(distance * 0.42, 1),
        polyline=_interpolate(request.origin, request.destination),
    )


def _point_at(polyline: Sequence[GeoPoint], ratio: float) -> GeoPoint:
    position = ratio * (len(polyline) - 1)
    left = int(position)
    right = min(len(polyline) - 1, left + 1)
    part = position - left
    return GeoPoint(
        lng=polyline[left].lng + (polyline[right].lng - polyline[left].lng) * part,
        lat=polyline[left].lat + (polyline[right].lat - polyline[left].lat) * part,
    )


def demo_candidates(route: RouteData) -> List[StationCandidate]:
    service_names = ["南湖服务区", "长安服务区", "临平服务区", "绍兴服务区", "太湖服务区"]
    tesla_names = ["湖州银泰城", "德清中心", "萧山万象汇", "诸暨印象城", "苏州中心"]
    if route.distance_km < 30:
        return []
    spacing = 115.0
    count = min(len(service_names), max(2, math.floor(route.distance_km / spacing) + 1))
    result: List[StationCandidate] = []
    for index in range(count):
        ratio = (index + 1) / (count + 1)
        point = _point_at(route.polyline, ratio)
        pair = [
            (f"{service_names[index]}充电站", StationType.SERVICE_AREA, 0.6, point),
            (
                f"特斯拉超级充电站 · {tesla_names[index]}",
                StationType.TESLA_SUPERCHARGER,
                3.2 + index * 0.3,
                GeoPoint(lng=point.lng + 0.025, lat=point.lat - 0.012),
            ),
        ]
        for name, kind, detour, station_point in pair:
            token = hashlib.sha1(f"{name}-{station_point.lng}-{station_point.lat}".encode()).hexdigest()[:10]
            result.append(StationCandidate(
                id=f"demo-{token}",
                name=name,
                address="演示候选地点 · 仅用于无实时数据时的功能预览",
                lng=station_point.lng,
                lat=station_point.lat,
                station_type=kind,
                progress_km=round(route.distance_km * ratio, 1),
                distance_to_route_km=detour,
                source="demo",
            ))
    return result


def _station_penalty(candidate: StationCandidate, strategy: Strategy, preferred: bool = False) -> float:
    exit_drive = candidate.exit_distance_km or candidate.distance_to_route_km
    entry_drive = candidate.entry_distance_km or exit_drive
    detour_cost = exit_drive + entry_drive
    unverified_exit_cost = 25 if (
        candidate.station_type == StationType.TESLA_SUPERCHARGER
        and candidate.source.endswith("_js")
        and candidate.exit_distance_km <= 0
    ) else 0
    if strategy == Strategy.TESLA_PRIORITY:
        preference = 0 if candidate.station_type == StationType.TESLA_SUPERCHARGER else 38
    elif strategy == Strategy.SERVICE_AREA:
        preference = 0 if candidate.station_type == StationType.SERVICE_AREA else 42
    else:
        preference = 0 if candidate.station_type == StationType.TESLA_SUPERCHARGER else 8
    if candidate.source == "ai_planning_gap":
        unconfirmed_cost = 4000
    elif candidate.source == "planning_gap":
        unconfirmed_cost = 5000
    else:
        unconfirmed_cost = 0
    preferred_discount = 25 if preferred else 0
    return 100 + detour_cost + preference + unverified_exit_cost + unconfirmed_cost - preferred_discount


def _leg_distance(previous: StationCandidate | None, current: StationCandidate | None, total: float) -> float:
    previous_progress = previous.progress_km if previous else 0
    current_progress = current.progress_km if current else total
    leave_previous = _departure_detour(previous) if previous else 0
    reach_current = _arrival_detour(current) if current else 0
    return current_progress - previous_progress + leave_previous + reach_current


def _arrival_detour(candidate: StationCandidate) -> float:
    if candidate.arrival_detour_km is not None:
        return candidate.arrival_detour_km
    return candidate.exit_distance_km or candidate.distance_to_route_km


def _departure_detour(candidate: StationCandidate) -> float:
    if candidate.departure_detour_km is not None:
        return candidate.departure_detour_km
    return candidate.entry_distance_km or candidate.exit_distance_km or candidate.distance_to_route_km


def select_stops(
    request: PlanRequest,
    route: RouteData,
    candidates: Sequence[StationCandidate],
    preferred_station_ids: Sequence[str] = (),
) -> List[StationCandidate]:
    """Find the lowest-cost feasible stop sequence on a forward-only route graph."""
    ordered = sorted(candidates, key=lambda item: (item.progress_km, item.distance_to_route_km))
    nodes: List[StationCandidate | None] = [None, *ordered, None]
    end_index = len(nodes) - 1
    distances = [float("inf")] * len(nodes)
    parents = [-1] * len(nodes)
    distances[0] = 0
    queue: List[Tuple[float, int]] = [(0, 0)]
    preferred_ids = set(preferred_station_ids)

    while queue:
        cost, index = heapq.heappop(queue)
        if cost != distances[index]:
            continue
        if index == end_index:
            break
        previous = nodes[index]
        available = request.initial_range_km if index == 0 else request.full_range_km * request.charge_to_percent / 100
        for next_index in range(index + 1, len(nodes)):
            current = nodes[next_index]
            previous_progress = previous.progress_km if previous else 0
            current_progress = current.progress_km if current else route.distance_km
            leave_previous = _departure_detour(previous) if previous else 0
            # The reserve is required on arrival at another charging stop. At
            # the final destination, use the remaining drivable range instead
            # of inserting a redundant last-minute charging stop merely to
            # preserve the same charging-stop buffer.
            max_leg = available if current is None else available - request.reserve_range_km
            if current_progress - previous_progress + leave_previous > max_leg + 1e-6:
                continue
            leg = _leg_distance(previous, current, route.distance_km)
            if leg > max_leg + 1e-6:
                continue
            next_cost = cost + (
                0 if current is None
                else _station_penalty(current, request.strategy, current.id in preferred_ids)
            )
            if next_cost < distances[next_index]:
                distances[next_index] = next_cost
                parents[next_index] = index
                heapq.heappush(queue, (next_cost, next_index))

    if not math.isfinite(distances[end_index]):
        raise ValueError(
            "当前续航设置无法覆盖充电站之间的距离。请降低预留续航、提高充电目标，或增加最大下高速距离。"
        )

    chosen: List[StationCandidate] = []
    cursor = parents[end_index]
    while cursor > 0:
        chosen.append(nodes[cursor])  # type: ignore[arg-type]
        cursor = parents[cursor]
    chosen.reverse()
    return chosen


def remove_redundant_stops(
    request: PlanRequest,
    route: RouteData,
    selected: Sequence[StationCandidate],
) -> List[StationCandidate]:
    """Remove any stop that can be skipped without violating range rules."""
    result = list(selected)
    changed = True
    while changed:
        changed = False
        for index in range(len(result) - 1, -1, -1):
            previous = result[index - 1] if index > 0 else None
            following = result[index + 1] if index + 1 < len(result) else None
            available = (
                request.initial_range_km
                if previous is None
                else request.full_range_km * request.charge_to_percent / 100
            )
            required_buffer = request.reserve_range_km if following is not None else 0
            if _leg_distance(previous, following, route.distance_km) <= available - required_buffer + 1e-6:
                del result[index]
                changed = True
                break
    return result


def _candidate_detour(candidate: StationCandidate) -> float:
    if candidate.station_type == StationType.TESLA_SUPERCHARGER:
        exit_drive = candidate.exit_distance_km or candidate.distance_to_route_km
        entry_drive = candidate.entry_distance_km or exit_drive
        return max(exit_drive, entry_drive)
    return candidate.distance_to_route_km


def _has_complete_highway_round_trip(candidate: StationCandidate) -> bool:
    return not (
        candidate.station_type == StationType.TESLA_SUPERCHARGER
        and (candidate.source.endswith("_js") or candidate.source.endswith("_web"))
        and (candidate.exit_distance_km <= 0 or candidate.entry_distance_km <= 0)
    )


def _detour_options(requested: float) -> List[float]:
    # Prefer the user's limit, but a real charger farther from the motorway is
    # safer and more useful than a synthetic "station pending" placeholder.
    upper = max(requested, 50.0)
    result = [float(requested)]
    cursor = requested + 5
    while cursor < upper - 1e-6:
        result.append(round(cursor, 1))
        cursor += 5
    if upper > result[-1] + 1e-6:
        result.append(round(upper, 1))
    return result


def _charge_options(requested: int) -> List[int]:
    return sorted({requested, *[target for target in (90, 100) if target > requested]})


def _reserve_options(request: PlanRequest, charge_percent: int) -> Tuple[List[float], float]:
    target_range = request.full_range_km * charge_percent / 100
    safety_recommendation = math.ceil(max(
        request.reserve_range_km + 20,
        request.full_range_km * 0.10,
    ) / 5) * 5
    upper_cap = min(request.initial_range_km - 5, target_range - 5)
    recommended = max(
        request.reserve_range_km,
        min(safety_recommendation, math.floor(upper_cap / 5) * 5),
    )
    result: List[float] = []
    cursor = recommended
    while cursor > request.reserve_range_km:
        result.append(round(cursor, 1))
        cursor -= 5
    result.append(request.reserve_range_km)
    return result, recommended


def _emergency_reserve_options(requested: float) -> List[float]:
    """Lower the ideal reserve only as a Tesla-only last resort."""
    if requested <= 5:
        return []
    result: List[float] = []
    cursor = requested - 5
    while cursor >= 5 - 1e-6:
        result.append(round(cursor, 1))
        cursor -= 5
    if result and result[-1] > 5 + 1e-6:
        result.append(5.0)
    return result


def _planning_gap_candidates(
    request: PlanRequest,
    route: RouteData,
    charge_percent: int,
    reserve_km: float,
) -> List[StationCandidate]:
    """Create explicitly unconfirmed search zones only as a no-error last resort."""
    first_leg = request.initial_range_km - reserve_km
    charged_leg = request.full_range_km * charge_percent / 100 - reserve_km
    if first_leg <= 0 or charged_leg <= 0:
        return []
    progress = 0.0
    available = first_leg
    result: List[StationCandidate] = []
    tesla_only = request.strategy == Strategy.TESLA_PRIORITY
    while route.distance_km - progress > available + 1e-6:
        # Leave 15% headroom so rounding and final road refinement do not put
        # the suggested search zone exactly on the range boundary.
        next_progress = progress + max(0.01, available * 0.85)
        if next_progress >= route.distance_km:
            break
        point = _point_at(route.polyline, next_progress / route.distance_km)
        result.append(StationCandidate(
            id=f"planning-gap-{len(result) + 1}-{round(next_progress)}",
            name=(
                f"第 {len(result) + 1} 个 Tesla 超充检索区段（站点待确认）"
                if tesla_only
                else f"第 {len(result) + 1} 补能区段（站点待确认）"
            ),
            address=(
                "该路段暂未检索到已确认的 Tesla 超充；Tesla-only 模式不会改用公共快充或高速服务区充电站"
                if tesla_only
                else "该路段未检索到足够的已确认充电站，请在出发前从当前地图或 Tesla App 选择实际快充站"
            ),
            lng=point.lng,
            lat=point.lat,
            station_type=StationType.CHARGING_STATION,
            progress_km=round(next_progress, 1),
            distance_to_route_km=0,
            source="planning_gap",
        ))
        progress = next_progress
        available = charged_leg
        if len(result) >= 200:
            break
    return result


def _ai_planning_gap_candidates(
    request: PlanRequest,
    route: RouteData,
    recommendation: Optional[AIPlanRecommendation],
) -> List[StationCandidate]:
    if not recommendation:
        return []
    result: List[StationCandidate] = []
    tesla_only = request.strategy == Strategy.TESLA_PRIORITY
    for index, progress in enumerate(recommendation.search_progress_km, start=1):
        if not 0 < progress < route.distance_km:
            continue
        point = _point_at(route.polyline, progress / route.distance_km)
        result.append(StationCandidate(
            id=f"ai-planning-gap-{index}-{round(progress)}",
            name=(
                f"GPT 推荐 Tesla 超充检索区段 {index}（站点待确认）"
                if tesla_only
                else f"GPT 推荐补能区段 {index}（站点待确认）"
            ),
            address=(
                "GPT 仅建议在此路段继续确认 Tesla 超充，不会改用公共快充或高速服务区充电站"
                if tesla_only
                else "GPT 仅推荐在此路段继续搜索快充；实际站点必须从当前地图或 Tesla App 确认"
            ),
            lng=point.lng,
            lat=point.lat,
            station_type=StationType.CHARGING_STATION,
            progress_km=round(progress, 1),
            distance_to_route_km=0,
            source="ai_planning_gap",
        ))
    return result


def _solve_with_automatic_adjustments(
    request: PlanRequest,
    route: RouteData,
    candidates: Sequence[StationCandidate],
    allow_unconfirmed: bool = True,
    ai_recommendation: Optional[AIPlanRecommendation] = None,
) -> Optional[Tuple[List[StationCandidate], int, float, float, float, bool]]:
    """Solve without lowering the user's base reserve.

    Prefer the user's exit-distance limit. Within each distance tier, increase
    the charge target only when necessary. For each combination, use the
    highest feasible recommended reserve, never less than the requested base.
    """
    recommended_for_requested = request.reserve_range_km
    confirmed_candidates = [
        candidate for candidate in candidates
        if candidate.source not in {"planning_gap", "ai_planning_gap"}
    ]
    complete_candidates = [candidate for candidate in confirmed_candidates if _has_complete_highway_round_trip(candidate)]
    preferred_type = (
        StationType.TESLA_SUPERCHARGER
        if request.strategy == Strategy.TESLA_PRIORITY
        else StationType.SERVICE_AREA
        if request.strategy == Strategy.SERVICE_AREA
        else None
    )
    preferred_complete = [
        candidate for candidate in complete_candidates
        if candidate.station_type == preferred_type
    ]
    preferred_confirmed = [
        candidate for candidate in confirmed_candidates
        if candidate.station_type == preferred_type
    ]
    if request.strategy == Strategy.TESLA_PRIORITY:
        # Tesla mode is an exclusive filter, not a soft preference. Never fall
        # back to public or service-area chargers, even when they are nearer.
        raw_pools = [preferred_complete, preferred_confirmed]
    elif preferred_type:
        raw_pools = [preferred_complete, preferred_confirmed, complete_candidates, confirmed_candidates]
    else:
        raw_pools = [complete_candidates, confirmed_candidates]
    candidate_pools = []
    seen_pools = set()
    for pool in raw_pools:
        signature = tuple(candidate.id for candidate in pool)
        if pool and signature not in seen_pools:
            candidate_pools.append(pool)
            seen_pools.add(signature)
    if not candidate_pools:
        candidate_pools = [[]]
    for candidate_pool in candidate_pools:
        for detour in _detour_options(request.max_detour_km):
            eligible = [
                candidate for candidate in candidate_pool
                if _candidate_detour(candidate) <= detour + 1e-6
            ]
            for charge_percent in _charge_options(request.charge_to_percent):
                reserves, recommended = _reserve_options(request, charge_percent)
                if charge_percent == request.charge_to_percent:
                    recommended_for_requested = recommended
                for reserve in reserves:
                    adjusted = request.model_copy(update={
                        "charge_to_percent": charge_percent,
                        "reserve_range_km": reserve,
                        "max_detour_km": detour,
                    })
                    try:
                        selected = select_stops(
                            adjusted,
                            route,
                            eligible,
                            ai_recommendation.preferred_station_ids if ai_recommendation else (),
                        )
                        return selected, charge_percent, detour, reserve, recommended, False
                    except ValueError:
                        continue

    if request.strategy == Strategy.TESLA_PRIORITY:
        # The configured reserve is an ideal target, not a hard reason to insert
        # an unconfirmed location. After exhausting all normal detour/charge
        # combinations, progressively lower it while still requiring positive
        # range on arrival at every real Tesla Supercharger.
        emergency_reserves = _emergency_reserve_options(request.reserve_range_km)
        for candidate_pool in candidate_pools:
            for detour in _detour_options(request.max_detour_km):
                eligible = [
                    candidate for candidate in candidate_pool
                    if _candidate_detour(candidate) <= detour + 1e-6
                ]
                for charge_percent in _charge_options(request.charge_to_percent):
                    _, recommended = _reserve_options(request, charge_percent)
                    for reserve in emergency_reserves:
                        adjusted = request.model_copy(update={
                            "charge_to_percent": charge_percent,
                            "reserve_range_km": reserve,
                            "max_detour_km": detour,
                        })
                        try:
                            selected = select_stops(
                                adjusted,
                                route,
                                eligible,
                                ai_recommendation.preferred_station_ids if ai_recommendation else (),
                            )
                            return selected, charge_percent, detour, reserve, recommended, False
                        except ValueError:
                            continue

    if not allow_unconfirmed:
        return None

    # A map provider can occasionally return an incomplete POI set. Keep the
    # route usable and make the uncertainty explicit instead of returning 422.
    charge_percent = ai_recommendation.charge_to_percent if ai_recommendation else 100
    detour = ai_recommendation.max_detour_km if ai_recommendation else max(request.max_detour_km, 30.0)
    default_reserves, recommended = _reserve_options(request, charge_percent)
    reserves = default_reserves
    if ai_recommendation:
        target_range = request.full_range_km * charge_percent / 100
        upper_cap = min(request.initial_range_km - 0.1, target_range - 0.1)
        ai_reserve = min(max(ai_recommendation.reserve_range_km, recommended), upper_cap)
        ai_reserve = max(request.reserve_range_km, ai_reserve)
        reserves = []
        cursor = ai_reserve
        while cursor > request.reserve_range_km:
            reserves.append(round(cursor, 1))
            cursor -= 5
        reserves.append(request.reserve_range_km)
    for reserve in reserves:
        gap_candidates = _planning_gap_candidates(request, route, charge_percent, reserve)
        ai_gap_candidates = _ai_planning_gap_candidates(request, route, ai_recommendation)
        confirmed_fallback = (
            preferred_confirmed
            if request.strategy == Strategy.TESLA_PRIORITY
            else confirmed_candidates
        )
        adjusted = request.model_copy(update={
            "charge_to_percent": charge_percent,
            "reserve_range_km": reserve,
            "max_detour_km": detour,
        })
        try:
            selected = select_stops(
                adjusted,
                route,
                [*confirmed_fallback, *ai_gap_candidates, *gap_candidates],
                ai_recommendation.preferred_station_ids if ai_recommendation else (),
            )
            return selected, charge_percent, detour, reserve, recommended, True
        except ValueError:
            continue

    # This can only happen with internally inconsistent range inputs. Returning
    # a readable route is still preferable to exposing the former "no solution"
    # error; the synthetic zones make every positive-range leg explicit.
    gap_candidates = _planning_gap_candidates(
        request, route, 100, request.reserve_range_km,
    )
    return gap_candidates, 100, detour, request.reserve_range_km, recommended_for_requested, True


def _preview_charge_stops(
    request: PlanRequest,
    route: RouteData,
    selected: Sequence[StationCandidate],
) -> List[ChargeStop]:
    previews: List[ChargeStop] = []
    previous: StationCandidate | None = None
    departure_range = request.initial_range_km
    target_range = request.full_range_km * request.charge_to_percent / 100
    for sequence, station in enumerate(selected, start=1):
        leg_distance = _leg_distance(previous, station, route.distance_km)
        arrival_range = max(0, departure_range - leg_distance)
        start_percent = max(0, round(arrival_range / request.full_range_km * 100))
        added = max(0, target_range - arrival_range)
        average_kw = 115 if station.station_type == StationType.TESLA_SUPERCHARGER else 85
        charge_minutes = max(8, round(added * 0.172 / average_kw * 60 + 5))
        previews.append(ChargeStop(
            **station.model_dump(),
            sequence=sequence,
            leg_distance_km=round(leg_distance, 1),
            arrival_range_km=round(arrival_range, 1),
            charge_from_percent=start_percent,
            charge_to_percent=request.charge_to_percent,
            charge_minutes=charge_minutes,
            added_range_km=round(added, 1),
            route_progress_percent=round(station.progress_km / route.distance_km * 100),
        ))
        departure_range = target_range
        previous = station
    return previews


def _ai_sequence_solution(
    request: PlanRequest,
    route: RouteData,
    candidates: Sequence[StationCandidate],
    recommendation: AIPlanRecommendation,
) -> Optional[Tuple[List[StationCandidate], int, float, float, float, bool]]:
    adjusted = request.model_copy(update={
        "charge_to_percent": recommendation.charge_to_percent,
        "reserve_range_km": recommendation.reserve_range_km,
        "max_detour_km": recommendation.max_detour_km,
    })
    by_id = {
        candidate.id: candidate for candidate in candidates
        if candidate.source not in {"planning_gap", "ai_planning_gap"}
        and _has_complete_highway_round_trip(candidate)
        and _candidate_detour(candidate) <= recommendation.max_detour_km + 1e-6
    }
    if recommendation.preferred_station_ids:
        if any(station_id not in by_id for station_id in recommendation.preferred_station_ids):
            return None
        chosen = [by_id[station_id] for station_id in recommendation.preferred_station_ids]
        if len({station.id for station in chosen}) != len(chosen):
            return None
        if any(left.progress_km >= right.progress_km for left, right in zip(chosen, chosen[1:])):
            return None
    else:
        chosen = []

    previous: StationCandidate | None = None
    for station in chosen:
        available = (
            adjusted.initial_range_km
            if previous is None
            else adjusted.full_range_km * adjusted.charge_to_percent / 100
        )
        if _leg_distance(previous, station, route.distance_km) > available - adjusted.reserve_range_km + 1e-6:
            return None
        previous = station
    final_available = (
        adjusted.initial_range_km
        if previous is None
        else adjusted.full_range_km * adjusted.charge_to_percent / 100
    )
    if _leg_distance(previous, None, route.distance_km) > final_available + 1e-6:
        return None
    chosen = remove_redundant_stops(adjusted, route, chosen)
    return (
        chosen,
        adjusted.charge_to_percent,
        adjusted.max_detour_km,
        adjusted.reserve_range_km,
        adjusted.reserve_range_km,
        False,
    )


def _preferred_station_type(strategy: Strategy) -> Optional[StationType]:
    if strategy == Strategy.TESLA_PRIORITY:
        return StationType.TESLA_SUPERCHARGER
    if strategy == Strategy.SERVICE_AREA:
        return StationType.SERVICE_AREA
    return None


def _uses_only_preferred_stations(
    solution: Optional[Tuple[List[StationCandidate], int, float, float, float, bool]],
    strategy: Strategy,
) -> bool:
    preferred_type = _preferred_station_type(strategy)
    return bool(
        solution is not None
        and preferred_type is not None
        and all(station.station_type == preferred_type for station in solution[0])
    )


def build_plan(request: PlanRequest, client: AmapClient) -> PlanResponse:
    notices: List[str] = []
    mode = request.map_provider
    provider_name = "百度" if request.map_provider == "baidu" else "高德"
    if request.client_route:
        route = request.client_route
        raw_polyline_km = sum(haversine_km(a, b) for a, b in zip(route.polyline, route.polyline[1:]))
        candidates = []
        for raw in request.client_candidates:
            try:
                candidate = StationCandidate(**raw)
            except Exception:
                continue
            projection = project_on_route(candidate, route.polyline)
            progress = projection.progress_km
            if raw_polyline_km:
                progress *= route.distance_km / raw_polyline_km
            measured_arrival_detour = candidate.exit_distance_km or projection.distance_km
            measured_departure_detour = candidate.entry_distance_km or measured_arrival_detour
            measured_detour = max(measured_arrival_detour, measured_departure_detour)
            # Once the selected stops are real waypoints, their detour is
            # already included in route.distance_km. Keep exit_distance_km for
            # display/filtering, but do not add it to the leg a second time.
            leg_arrival_detour = projection.distance_km if request.client_route_includes_stops else measured_arrival_detour
            leg_departure_detour = projection.distance_km if request.client_route_includes_stops else measured_departure_detour
            # Retain all real candidates needed by the automatic 50 km
            # fallback. Selection still starts at the user's chosen limit.
            corridor_limit = max(50.0, request.max_detour_km)
            if (
                3 < progress < route.distance_km - 3
                and projection.distance_km <= corridor_limit
                and measured_detour <= corridor_limit
            ):
                candidates.append(candidate.model_copy(update={
                    "progress_km": round(progress, 1),
                    "distance_to_route_km": round(leg_arrival_detour, 1),
                    "arrival_detour_km": round(leg_arrival_detour, 1),
                    "departure_detour_km": round(leg_departure_detour, 1),
                    "source": candidate.source or f"{request.map_provider}_js",
                }))
        candidates.sort(key=lambda item: (item.progress_km, item.distance_to_route_km))
        if not candidates and route.distance_km > request.initial_range_km - request.reserve_range_km:
            notices.append(f"已取得{provider_name}真实道路，但沿途地点检索未返回足够的已确认充电站，系统将标出需要继续确认的补能区段。")
        else:
            notices.append(f"道路与沿途地点来自{provider_name}地图，续航方案由 Python 后端计算。")
    elif request.map_provider == "amap" and client.enabled:
        try:
            route = client.route(request.origin, request.destination)
            candidates = client.find_chargers(route, max(request.max_detour_km, 30.0))
        except AmapError as exc:
            mode = "demo"
            route = demo_route(request)
            candidates = demo_candidates(route)
            notices.append(f"高德地图数据暂时不可用，已切换演示规划：{exc}")
    else:
        mode = "demo"
        route = demo_route(request)
        candidates = demo_candidates(route)
        notices.append(f"当前为演示模式；配置{provider_name}地图 Key 后将自动使用真实道路和充电站数据。")

    if not candidates and route.distance_km > request.initial_range_km - request.reserve_range_km:
        if mode in {"amap", "baidu"}:
            notices.append("沿途暂未取得已确认充电站，系统会继续给出补能区段，不会中断路线规划。")
        else:
            candidates = demo_candidates(route)

    baseline_solution = _solve_with_automatic_adjustments(
        request, route, candidates, allow_unconfirmed=False,
    )
    baseline_is_preferred_only = _uses_only_preferred_stations(
        baseline_solution, request.strategy,
    )
    solution = baseline_solution
    ai_recommendation: Optional[AIPlanRecommendation] = None
    ai_applied = False

    if request.recommendation_mode == RecommendationMode.AI:
        baseline_previews: List[ChargeStop] = []
        if baseline_solution is not None:
            (
                baseline_selected,
                baseline_charge,
                baseline_detour,
                baseline_reserve,
                _,
                _,
            ) = baseline_solution
            baseline_request = request.model_copy(update={
                "charge_to_percent": baseline_charge,
                "reserve_range_km": baseline_reserve,
                "max_detour_km": baseline_detour,
            })
            baseline_selected = remove_redundant_stops(baseline_request, route, baseline_selected)
            baseline_previews = _preview_charge_stops(baseline_request, route, baseline_selected)
        try:
            ai_recommendation = AIRecommender().recommend(
                request, route, candidates, baseline_previews,
            )
        except AIRecommendationError:
            notices.append("AI 推荐暂时不可用，已自动使用传统算法完成规划。")
        if ai_recommendation:
            ai_solution = _ai_sequence_solution(request, route, candidates, ai_recommendation)
            if ai_solution is None:
                ai_request = request.model_copy(update={
                    "charge_to_percent": ai_recommendation.charge_to_percent,
                    "reserve_range_km": ai_recommendation.reserve_range_km,
                    "max_detour_km": ai_recommendation.max_detour_km,
                })
                ai_solution = _solve_with_automatic_adjustments(
                    ai_request,
                    route,
                    candidates,
                    allow_unconfirmed=False,
                    ai_recommendation=ai_recommendation,
                )
            violates_tesla_only = (
                request.strategy == Strategy.TESLA_PRIORITY
                and ai_solution is not None
                and not _uses_only_preferred_stations(ai_solution, request.strategy)
            )
            violates_preferred_baseline = (
                request.strategy != Strategy.TESLA_PRIORITY
                and baseline_is_preferred_only
                and ai_solution is not None
                and not _uses_only_preferred_stations(ai_solution, request.strategy)
            )
            if violates_tesla_only or violates_preferred_baseline:
                ai_solution = None
                notices.append(
                    "AI 方案包含非 Tesla 站点，已被 Tesla-only 硬限制拒绝。"
                    if request.strategy == Strategy.TESLA_PRIORITY
                    else "AI 方案破坏了当前充电偏好，已保留可行的全服务区方案。"
                )
            if ai_solution is not None:
                solution = ai_solution
                ai_applied = True

    if solution is None:
        solution = _solve_with_automatic_adjustments(
            request,
            route,
            candidates,
            allow_unconfirmed=True,
            ai_recommendation=(
                ai_recommendation
                if request.recommendation_mode == RecommendationMode.AI
                else None
            ),
        )
        ai_applied = ai_recommendation is not None
    assert solution is not None
    (
        selected,
        effective_charge_percent,
        effective_detour_km,
        effective_reserve,
        recommended_reserve,
        uses_recommended_locations,
    ) = solution
    effective_request = request.model_copy(update={
        "charge_to_percent": effective_charge_percent,
        "reserve_range_km": effective_reserve,
        "max_detour_km": effective_detour_km,
    })
    selected = remove_redundant_stops(effective_request, route, selected)
    target_range = request.full_range_km * effective_charge_percent / 100
    if ai_recommendation:
        ai_recommendation = ai_recommendation.model_copy(update={
            "applied": ai_applied,
            "charge_to_percent": effective_charge_percent,
            "max_detour_km": effective_detour_km,
            "reserve_range_km": effective_reserve,
        })

    selected_gap_ids = {
        station.id for station in selected
        if station.source in {"planning_gap", "ai_planning_gap"}
    }
    if selected_gap_ids:
        existing_ids = {candidate.id for candidate in candidates}
        candidates.extend(station for station in selected if station.id not in existing_ids)
        candidates.sort(key=lambda item: (item.progress_km, item.distance_to_route_km))
        notices.append(
            f"{provider_name}返回的已确认站点仍有空档；行程中的“补能区段（站点待确认）”是继续搜索实际快充站的位置范围，不会冒充真实站点。"
        )
    if ai_recommendation and ai_applied:
        notices.append("已采用 AI JSON 结构化推荐；全部超充数据、到站电量和充电时间已参与比较，推荐结果已由后端复核。")
    elif request.recommendation_mode == RecommendationMode.AI and ai_recommendation:
        notices.append("AI 返回的站点组合未通过续航复核，本次已自动采用传统算法的可行方案。")
    if effective_charge_percent > request.charge_to_percent:
        notices.append(
            f"为覆盖较长的站间距离，系统已把每站充电目标从 {request.charge_to_percent}% 自动提高到 {effective_charge_percent}%。"
        )
    if effective_detour_km > request.max_detour_km:
        notices.append(
            f"在 {request.max_detour_km:g} km 范围内没有完整可行组合，系统已自动放宽到 {effective_detour_km:g} km，并仍优先选择距高速出口更近的站。"
        )
    if effective_reserve < request.reserve_range_km:
        notices.append(
            f"为坚持 Tesla-only 并使用已确认超充，系统把本次最低到站余量从理想值 {request.reserve_range_km:g} km 逐级调整为 {effective_reserve:g} km；每段均已通过续航复核。"
        )
    elif effective_reserve > request.reserve_range_km:
        notices.append(
            f"到充电站的基础安全余量为 {request.reserve_range_km:g} km；结合车辆满电续航，推荐并采用 {effective_reserve:g} km。"
        )
    elif recommended_reserve > request.reserve_range_km:
        notices.append(
            f"系统建议到充电站保留 {recommended_reserve:g} km，但受沿途站点分布限制，本次按基础余量 {request.reserve_range_km:g} km 规划。"
        )
    stops: List[ChargeStop] = []
    legs: List[TripLeg] = []
    previous_station: StationCandidate | None = None
    previous_name = request.origin.name
    departure_range = request.initial_range_km
    total_drive_distance = 0.0
    total_charge_minutes = 0

    for sequence, station in enumerate([*selected, None], start=1):
        leg_distance = _leg_distance(previous_station, station, route.distance_km)
        total_drive_distance += leg_distance
        arrival_range = max(0, departure_range - leg_distance)
        to_name = station.name if station else request.destination.name
        legs.append(TripLeg(
            sequence=sequence,
            from_name=previous_name,
            to_name=to_name,
            distance_km=round(leg_distance, 1),
            duration_min=max(1, round(leg_distance / 87 * 60)),
            departure_range_km=round(departure_range, 1),
            arrival_range_km=round(arrival_range, 1),
        ))
        if station:
            start_percent = max(2, round(arrival_range / request.full_range_km * 100))
            added = max(0, target_range - arrival_range)
            # Typical Supercharger curve approximation plus a small service-area overhead.
            average_kw = 115 if station.station_type == StationType.TESLA_SUPERCHARGER else 85
            battery_added_kwh = added * 0.172
            charge_minutes = max(8, round(battery_added_kwh / average_kw * 60 + 5))
            total_charge_minutes += charge_minutes
            stops.append(ChargeStop(
                **station.model_dump(),
                sequence=sequence,
                leg_distance_km=round(leg_distance, 1),
                arrival_range_km=round(arrival_range, 1),
                charge_from_percent=start_percent,
                charge_to_percent=effective_charge_percent,
                charge_minutes=charge_minutes,
                added_range_km=round(added, 1),
                route_progress_percent=round(station.progress_km / route.distance_km * 100),
            ))
            departure_range = target_range
            previous_station = station
            previous_name = station.name

    arrival = legs[-1].arrival_range_km if legs else request.initial_range_km
    drive_minutes = sum(leg.duration_min for leg in legs)
    estimated_energy_kwh = total_drive_distance * 0.172
    summary = PlanSummary(
        direct_distance_km=route.distance_km,
        planned_distance_km=round(total_drive_distance, 1),
        drive_duration_min=drive_minutes,
        charge_duration_min=total_charge_minutes,
        total_duration_min=drive_minutes + total_charge_minutes,
        charging_stops=len(stops),
        arrival_range_km=round(arrival, 1),
        estimated_cost_yuan=round(estimated_energy_kwh * 1.55 + route.tolls_yuan, 1),
        requested_reserve_km=request.reserve_range_km,
        planned_reserve_km=effective_reserve,
        planned_charge_to_percent=effective_charge_percent,
        planned_max_detour_km=effective_detour_km,
        uses_recommended_locations=uses_recommended_locations,
        ai_assisted=ai_recommendation is not None and ai_applied,
    )
    if stops:
        if effective_reserve == request.reserve_range_km and recommended_reserve == request.reserve_range_km:
            notices.append(f"已达到基础安全余量 {request.reserve_range_km:g} km。")
    else:
        notices.append("当前起始续航足够直达，无需途中充电。")
    if any(stop.station_type == StationType.TESLA_SUPERCHARGER for stop in stops):
        if any(stop.exit_distance_km > 0 for stop in stops):
            notices.append(f"下高速出口到超充、充电后返回高速入口的距离与时间均按{provider_name}实际驾车路线计算，并优先选择往返更短的方案。")
        else:
            notices.append("未取得出口道路距离时使用路线走廊估算；出发前请在 Tesla App 确认站点状态。")
    if request.strategy == Strategy.TESLA_PRIORITY:
        if any(stop.source in {"planning_gap", "ai_planning_gap"} for stop in stops):
            notices.append("已启用 Tesla-only 模式：暂未确认的区段只会继续搜索 Tesla 超充，不会改用公共快充或高速服务区充电站。")
        else:
            notices.append("已启用 Tesla-only 模式：本次所有充电站均为已确认的 Tesla 超级充电站。")

    return PlanResponse(
        mode=mode,
        summary=summary,
        route=route,
        stops=stops,
        legs=legs,
        candidates=candidates,
        notices=notices,
        ai_recommendation=ai_recommendation,
    )

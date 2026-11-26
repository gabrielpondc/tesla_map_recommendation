import unittest
from unittest.mock import patch

from app.amap import AmapClient
from app.models import (
    AIPlanRecommendation,
    GeoPoint,
    Place,
    PlanRequest,
    RecommendationMode,
    RouteData,
    StationCandidate,
    StationType,
    Strategy,
)
from app.planner import build_plan, select_stops


class PlannerTest(unittest.TestCase):
    def request(self, **overrides):
        values = dict(
            origin=Place(name="上海", lng=121.4737, lat=31.2304),
            destination=Place(name="合肥", lng=117.2902, lat=31.7992),
            initial_range_km=150,
            reserve_range_km=40,
            full_range_km=560,
            charge_to_percent=80,
            strategy=Strategy.BALANCED,
            max_detour_km=12,
        )
        values.update(overrides)
        return PlanRequest(**values)

    def test_plan_keeps_reserve_and_adds_charge_stop(self):
        result = build_plan(self.request(), AmapClient(key=""))
        self.assertEqual(result.mode, "demo")
        self.assertGreaterEqual(len(result.stops), 1)
        self.assertTrue(all(stop.arrival_range_km >= 40 for stop in result.stops))
        self.assertGreaterEqual(result.summary.arrival_range_km, 40)

    def test_short_trip_needs_no_charge(self):
        result = build_plan(self.request(
            destination=Place(name="苏州", lng=120.6720, lat=31.3174),
            initial_range_km=400,
        ), AmapClient(key=""))
        self.assertEqual(result.summary.charging_stops, 0)
        self.assertGreater(result.summary.arrival_range_km, 40)

    def test_tesla_strategy_prefers_supercharger(self):
        result = build_plan(self.request(strategy=Strategy.TESLA_PRIORITY), AmapClient(key=""))
        self.assertTrue(any(stop.station_type.value == "tesla_supercharger" for stop in result.stops))

    def test_long_trip_calculates_multiple_stops(self):
        result = build_plan(self.request(
            destination=Place(name="武汉", lng=114.3055, lat=30.5928),
            initial_range_km=210,
            full_range_km=420,
            charge_to_percent=70,
            strategy=Strategy.TESLA_PRIORITY,
        ), AmapClient(key=""))
        self.assertGreaterEqual(result.summary.charging_stops, 2)
        self.assertTrue(all(stop.arrival_range_km >= 40 for stop in result.stops))
        self.assertGreaterEqual(result.summary.arrival_range_km, 0)

    def test_tesla_candidates_prefer_shortest_exit_drive(self):
        request = self.request(initial_range_km=160, reserve_range_km=40)
        route = RouteData(
            distance_km=300,
            duration_min=210,
            polyline=[GeoPoint(lng=121, lat=31), GeoPoint(lng=118, lat=31)],
        )
        farther = StationCandidate(
            id="far", name="较远超充", lng=120, lat=31, station_type=StationType.TESLA_SUPERCHARGER,
            progress_km=100, distance_to_route_km=8, exit_name="甲收费站", exit_distance_km=8,
        )
        nearer = StationCandidate(
            id="near", name="最近超充", lng=120, lat=31, station_type=StationType.TESLA_SUPERCHARGER,
            progress_km=100, distance_to_route_km=2, exit_name="乙收费站", exit_distance_km=2,
        )
        chosen = select_stops(request, route, [farther, nearer])
        self.assertEqual([station.id for station in chosen], ["near"])

    def test_tesla_candidates_compare_full_highway_round_trip(self):
        request = self.request(initial_range_km=180, reserve_range_km=40)
        route = RouteData(
            distance_km=300,
            duration_min=210,
            polyline=[GeoPoint(lng=121, lat=31), GeoPoint(lng=118, lat=31)],
        )
        short_exit_long_return = StationCandidate(
            id="one-way-short", name="去程近但回程远", lng=120, lat=31,
            station_type=StationType.TESLA_SUPERCHARGER,
            progress_km=100, distance_to_route_km=1,
            exit_name="甲出口", exit_distance_km=1,
            entry_name="甲入口", entry_distance_km=20,
        )
        best_round_trip = StationCandidate(
            id="round-trip", name="往返更短", lng=120, lat=31,
            station_type=StationType.TESLA_SUPERCHARGER,
            progress_km=100, distance_to_route_km=5,
            exit_name="乙出口", exit_distance_km=5,
            entry_name="乙入口", entry_distance_km=5,
        )
        chosen = select_stops(request, route, [short_exit_long_return, best_round_trip])
        self.assertEqual([station.id for station in chosen], ["round-trip"])

    def test_tesla_priority_tries_all_tesla_route_before_nearer_public_station(self):
        route = RouteData(
            distance_km=300,
            duration_min=210,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=103, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=103, lat=30),
            initial_range_km=170,
            strategy=Strategy.TESLA_PRIORITY,
            client_route=route,
            client_candidates=[
                StationCandidate(
                    id="tesla", name="特斯拉超级充电站", lng=100.9, lat=30,
                    station_type=StationType.TESLA_SUPERCHARGER,
                    exit_name="较远出口", exit_distance_km=30,
                    entry_name="较远入口", entry_distance_km=30,
                ).model_dump(),
                StationCandidate(
                    id="public", name="路边公共快充", lng=100.9, lat=30,
                    station_type=StationType.CHARGING_STATION,
                ).model_dump(),
            ],
        ), AmapClient(key=""))
        self.assertEqual([stop.id for stop in result.stops], ["tesla"])

    def test_tesla_only_never_falls_back_to_reachable_public_station(self):
        route = RouteData(
            distance_km=420,
            duration_min=300,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=104.2, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=104.2, lat=30),
            initial_range_km=130,
            full_range_km=300,
            strategy=Strategy.TESLA_PRIORITY,
            client_route=route,
            client_candidates=[
                StationCandidate(
                    id="public", name="可到达的国家电网快充", lng=100.8, lat=30,
                    station_type=StationType.CHARGING_STATION,
                ).model_dump(),
                StationCandidate(
                    id="service", name="可到达的高速服务区充电站", lng=100.9, lat=30,
                    station_type=StationType.SERVICE_AREA,
                ).model_dump(),
                StationCandidate(
                    id="late-tesla", name="较后的特斯拉超级充电站", lng=102.5, lat=30,
                    station_type=StationType.TESLA_SUPERCHARGER,
                ).model_dump(),
            ],
        ), AmapClient(key=""))
        self.assertNotIn("public", [stop.id for stop in result.stops])
        self.assertNotIn("service", [stop.id for stop in result.stops])
        self.assertTrue(all(
            stop.station_type == StationType.TESLA_SUPERCHARGER
            or stop.source in {"planning_gap", "ai_planning_gap"}
            for stop in result.stops
        ))
        self.assertTrue(any("Tesla-only 模式" in notice for notice in result.notices))

    def test_tesla_only_lowers_ideal_reserve_before_using_a_search_gap(self):
        route = RouteData(
            distance_km=700,
            duration_min=490,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=107, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=107, lat=30),
            initial_range_km=459,
            full_range_km=459,
            reserve_range_km=40,
            strategy=Strategy.TESLA_PRIORITY,
            client_route=route,
            client_candidates=[
                StationCandidate(
                    id="tesla-one", name="Tesla 超充一", lng=101, lat=30,
                    station_type=StationType.TESLA_SUPERCHARGER,
                ).model_dump(),
                StationCandidate(
                    id="public-middle", name="中间的公共快充", lng=103, lat=30,
                    station_type=StationType.CHARGING_STATION,
                ).model_dump(),
                StationCandidate(
                    id="tesla-two", name="Tesla 超充二", lng=105.2, lat=30,
                    station_type=StationType.TESLA_SUPERCHARGER,
                ).model_dump(),
            ],
        ), AmapClient(key=""))
        self.assertEqual([stop.id for stop in result.stops], ["tesla-one", "tesla-two"])
        self.assertEqual(result.summary.planned_charge_to_percent, 100)
        self.assertEqual(result.summary.planned_reserve_km, 35)
        self.assertFalse(result.summary.uses_recommended_locations)
        self.assertTrue(any("最低到站余量" in notice for notice in result.notices))

    def test_real_plan_prefers_station_with_complete_exit_and_entry_data(self):
        route = RouteData(
            distance_km=300,
            duration_min=210,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=103, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=103, lat=30),
            initial_range_km=150,
            client_route=route,
            client_candidates=[
                StationCandidate(
                    id="missing", name="往返数据缺失", lng=101, lat=30,
                    station_type=StationType.TESLA_SUPERCHARGER, source="amap_js",
                ).model_dump(),
                StationCandidate(
                    id="complete", name="往返数据完整", lng=101, lat=30,
                    station_type=StationType.TESLA_SUPERCHARGER, source="amap_js",
                    exit_name="完整出口", exit_distance_km=2, exit_duration_min=3,
                    entry_name="完整入口", entry_distance_km=3, entry_duration_min=4,
                ).model_dump(),
            ],
        ), AmapClient(key=""))
        self.assertEqual([stop.id for stop in result.stops], ["complete"])

    def test_does_not_add_final_stop_when_previous_charge_reaches_destination(self):
        route = RouteData(
            distance_km=545.2,
            duration_min=380,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=105.452, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=105.452, lat=30),
            initial_range_km=459,
            reserve_range_km=100,
            full_range_km=459,
            charge_to_percent=90,
            client_route=route,
            client_candidates=[
                StationCandidate(
                    id="needed", name="第一站", lng=102.304, lat=30,
                    station_type=StationType.TESLA_SUPERCHARGER,
                ).model_dump(),
                StationCandidate(
                    id="redundant", name="终点前多余站", lng=105.364, lat=30,
                    station_type=StationType.TESLA_SUPERCHARGER,
                ).model_dump(),
            ],
        ), AmapClient(key=""))
        self.assertEqual([stop.id for stop in result.stops], ["needed"])
        self.assertGreater(result.summary.arrival_range_km, 0)

    @patch("app.planner.AIRecommender.recommend")
    def test_ai_mode_receives_baseline_and_applies_verified_station_ids(self, recommend):
        recommend.return_value = AIPlanRecommendation(
            model="test-model",
            charge_to_percent=90,
            max_detour_km=12,
            reserve_range_km=40,
            preferred_station_ids=["ai-choice"],
            search_progress_km=[],
            reason_codes=["candidate_quality"],
        )
        route = RouteData(
            distance_km=300,
            duration_min=210,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=103, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=103, lat=30),
            initial_range_km=150,
            recommendation_mode=RecommendationMode.AI,
            client_route=route,
            client_candidates=[StationCandidate(
                id="ai-choice", name="AI选择超充", lng=101, lat=30,
                station_type=StationType.TESLA_SUPERCHARGER,
                exit_name="测试出口", exit_distance_km=2, exit_duration_min=3,
                entry_name="测试入口", entry_distance_km=3, entry_duration_min=4,
            ).model_dump()],
        ), AmapClient(key=""))
        self.assertEqual([stop.id for stop in result.stops], ["ai-choice"])
        self.assertTrue(result.summary.ai_assisted)
        self.assertTrue(result.ai_recommendation.applied)
        baseline_stops = recommend.call_args.args[3]
        self.assertTrue(baseline_stops)
        self.assertGreaterEqual(baseline_stops[0].arrival_range_km, 40)
        self.assertGreater(baseline_stops[0].charge_minutes, 0)

    @patch("app.planner.AIRecommender.recommend")
    def test_ai_cannot_replace_feasible_all_tesla_route_with_public_station(self, recommend):
        recommend.return_value = AIPlanRecommendation(
            model="test-model",
            charge_to_percent=90,
            max_detour_km=12,
            reserve_range_km=40,
            preferred_station_ids=["public"],
            search_progress_km=[],
            reason_codes=["candidate_quality"],
        )
        route = RouteData(
            distance_km=300,
            duration_min=210,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=103, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=103, lat=30),
            initial_range_km=150,
            strategy=Strategy.TESLA_PRIORITY,
            recommendation_mode=RecommendationMode.AI,
            client_route=route,
            client_candidates=[
                StationCandidate(
                    id="tesla", name="特斯拉超级充电站", lng=101, lat=30,
                    station_type=StationType.TESLA_SUPERCHARGER,
                    exit_name="Tesla 出口", exit_distance_km=3,
                    entry_name="Tesla 入口", entry_distance_km=3,
                ).model_dump(),
                StationCandidate(
                    id="public", name="高速服务区公共快充", lng=101, lat=30,
                    station_type=StationType.SERVICE_AREA,
                ).model_dump(),
            ],
        ), AmapClient(key=""))
        self.assertEqual([stop.id for stop in result.stops], ["tesla"])
        self.assertFalse(result.summary.ai_assisted)
        self.assertFalse(result.ai_recommendation.applied)
        self.assertTrue(any("Tesla-only 硬限制拒绝" in notice for notice in result.notices))

    @patch("app.planner.AIRecommender.recommend")
    def test_traditional_mode_does_not_call_ai(self, recommend):
        build_plan(self.request(), AmapClient(key=""))
        recommend.assert_not_called()

    def test_reserve_recommendation_increases_above_base_value(self):
        request = PlanRequest(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=103, lat=30),
            initial_range_km=170,
            reserve_range_km=40,
            full_range_km=560,
            charge_to_percent=80,
            strategy=Strategy.TESLA_PRIORITY,
            max_detour_km=12,
            client_route=RouteData(
                distance_km=300,
                duration_min=210,
                polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=103, lat=30)],
            ),
            client_route_includes_stops=True,
            client_candidates=[
                StationCandidate(id="one", name="超充一", lng=101, lat=30, station_type=StationType.TESLA_SUPERCHARGER).model_dump(),
                StationCandidate(id="two", name="超充二", lng=102, lat=30, station_type=StationType.TESLA_SUPERCHARGER).model_dump(),
            ],
        )
        result = build_plan(request, AmapClient(key=""))
        self.assertEqual(result.summary.requested_reserve_km, 40)
        self.assertEqual(result.summary.planned_reserve_km, 60)
        self.assertGreaterEqual(result.summary.charging_stops, 1)

    def test_public_fast_charger_fills_gap_when_tesla_is_not_reachable(self):
        route = RouteData(
            distance_km=420,
            duration_min=300,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=104.2, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=104.2, lat=30),
            initial_range_km=130,
            full_range_km=300,
            client_route=route,
            client_candidates=[
                StationCandidate(
                    id="public", name="公共快充", lng=100.8, lat=30,
                    station_type=StationType.CHARGING_STATION,
                ).model_dump(),
                StationCandidate(
                    id="tesla", name="Tesla 超充", lng=102.5, lat=30,
                    station_type=StationType.TESLA_SUPERCHARGER,
                ).model_dump(),
            ],
        ), AmapClient(key=""))
        self.assertIn(StationType.CHARGING_STATION, [stop.station_type for stop in result.stops])
        self.assertFalse(result.summary.uses_recommended_locations)

    def test_charge_target_is_raised_automatically_for_long_station_gap(self):
        route = RouteData(
            distance_km=520,
            duration_min=365,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=105.2, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=105.2, lat=30),
            initial_range_km=200,
            full_range_km=400,
            charge_to_percent=80,
            client_route=route,
            client_candidates=[StationCandidate(
                id="only", name="唯一快充", lng=101.5, lat=30,
                station_type=StationType.CHARGING_STATION,
            ).model_dump()],
        ), AmapClient(key=""))
        self.assertEqual(result.summary.planned_charge_to_percent, 100)
        self.assertTrue(all(stop.charge_to_percent == 100 for stop in result.stops))
        self.assertTrue(any("自动提高到 100%" in notice for notice in result.notices))

    def test_exit_limit_is_relaxed_automatically(self):
        route = RouteData(
            distance_km=300,
            duration_min=210,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=103, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=103, lat=30),
            initial_range_km=150,
            strategy=Strategy.TESLA_PRIORITY,
            client_route=route,
            client_candidates=[
                StationCandidate(
                    id="near-public", name="当前范围内的公共快充", lng=100.9, lat=30,
                    station_type=StationType.CHARGING_STATION,
                ).model_dump(),
                StationCandidate(
                    id="outside", name="下高速范围外的 Tesla 超充", lng=100.9, lat=30,
                    station_type=StationType.TESLA_SUPERCHARGER,
                    exit_name="某收费站", exit_distance_km=15,
                ).model_dump(),
            ],
        ), AmapClient(key=""))
        self.assertEqual(result.summary.planned_max_detour_km, 17)
        self.assertEqual([stop.id for stop in result.stops], ["outside"])
        self.assertGreaterEqual(result.summary.planned_reserve_km, 40)

    def test_real_station_up_to_fifty_km_is_used_before_search_zone(self):
        route = RouteData(
            distance_km=300,
            duration_min=210,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=103, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=103, lat=30),
            initial_range_km=170,
            client_route=route,
            client_candidates=[StationCandidate(
                id="wider-real", name="较远但真实的快充", lng=100.7, lat=30.3,
                station_type=StationType.CHARGING_STATION,
            ).model_dump()],
        ), AmapClient(key=""))
        self.assertEqual([stop.id for stop in result.stops], ["wider-real"])
        self.assertFalse(result.summary.uses_recommended_locations)
        self.assertEqual(result.summary.planned_max_detour_km, 37)

    def test_far_candidate_does_not_hide_later_reachable_candidate(self):
        request = self.request(initial_range_km=150, reserve_range_km=40)
        route = RouteData(
            distance_km=300,
            duration_min=210,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=103, lat=30)],
        )
        far = StationCandidate(
            id="far", name="绕行很远", lng=101, lat=30,
            station_type=StationType.CHARGING_STATION,
            progress_km=100, distance_to_route_km=30,
        )
        reachable = StationCandidate(
            id="reachable", name="稍后但更近", lng=101.01, lat=30,
            station_type=StationType.CHARGING_STATION,
            progress_km=101, distance_to_route_km=1,
        )
        chosen = select_stops(request, route, [far, reachable])
        self.assertEqual([station.id for station in chosen], ["reachable"])

    def test_missing_pois_returns_explicit_search_zones_instead_of_error(self):
        route = RouteData(
            distance_km=600,
            duration_min=420,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=106, lat=30)],
        )
        result = build_plan(self.request(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=106, lat=30),
            initial_range_km=150,
            full_range_km=400,
            client_route=route,
            client_candidates=[],
        ), AmapClient(key=""))
        self.assertTrue(result.summary.uses_recommended_locations)
        self.assertTrue(result.stops)
        self.assertTrue(all(stop.source == "planning_gap" for stop in result.stops))
        self.assertGreaterEqual(min(stop.arrival_range_km for stop in result.stops), 40)
        self.assertGreaterEqual(result.summary.arrival_range_km, 40)
        self.assertEqual(result.summary.planned_reserve_km, 60)


if __name__ == "__main__":
    unittest.main()

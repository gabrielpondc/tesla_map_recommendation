import json
import unittest
from unittest.mock import patch

from app.ai_recommender import AIRecommendationError, AIRecommender
from app.models import (
    ChargeStop,
    GeoPoint,
    Place,
    PlanRequest,
    RouteData,
    StationCandidate,
    StationType,
    Strategy,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class AIRecommenderTest(unittest.TestCase):
    def setUp(self):
        self.request = PlanRequest(
            origin=Place(name="起点", lng=100, lat=30),
            destination=Place(name="终点", lng=105, lat=30),
            initial_range_km=180,
            reserve_range_km=40,
            full_range_km=400,
            charge_to_percent=80,
            max_detour_km=12,
        )
        self.route = RouteData(
            distance_km=500,
            duration_min=350,
            polyline=[GeoPoint(lng=100, lat=30), GeoPoint(lng=105, lat=30)],
        )
        self.candidates = [StationCandidate(
            id="real-station", name="真实快充", lng=102, lat=30,
            station_type=StationType.TESLA_SUPERCHARGER,
            progress_km=200,
            exit_name="测试出口", exit_distance_km=3, exit_duration_min=5,
            entry_name="测试入口", entry_distance_km=4, entry_duration_min=6,
        )]
        self.environment = {
            "AI_API_BASE_URL": "https://example.invalid/v1",
            "AI_API_KEY": "test-key",
            "AI_MODEL": "gpt-5.4",
        }

    def test_uses_strict_json_schema_and_rejects_unknown_station_ids(self):
        content = json.dumps({
            "charge_to_percent": 100,
            "max_detour_km": 25,
            "reserve_range_km": 50,
            "preferred_station_ids": ["made-up", "real-station"],
            "search_progress_km": [140, 900],
            "reason_codes": ["station_gap", "charge_target"],
        })
        response = FakeResponse({"choices": [{"message": {"content": content}}]})
        captured = {}

        def fake_urlopen(http_request, timeout):
            captured["payload"] = json.loads(http_request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return response

        with patch.dict("os.environ", self.environment, clear=False), patch(
            "app.ai_recommender.urlopen", side_effect=fake_urlopen,
        ):
            recommendation = AIRecommender().recommend(self.request, self.route, self.candidates)

        self.assertEqual(captured["payload"]["response_format"]["type"], "json_schema")
        self.assertTrue(captured["payload"]["response_format"]["json_schema"]["strict"])
        ai_input = json.loads(captured["payload"]["messages"][1]["content"])
        self.assertEqual(ai_input["tesla_candidate_count"], 1)
        self.assertEqual(ai_input["candidates"][0]["highway_round_trip_km"], 7)
        self.assertEqual(ai_input["candidates"][0]["highway_round_trip_min"], 11)
        self.assertIn("arrival_range_km", ai_input["candidates"][0])
        self.assertIn("to_90_percent", ai_input["candidates"][0]["charge_minutes"])
        self.assertEqual(recommendation.preferred_station_ids, ["real-station"])
        self.assertEqual(recommendation.search_progress_km, [140.0])
        self.assertGreaterEqual(recommendation.reserve_range_km, 40)

    def test_natural_language_response_is_rejected(self):
        response = FakeResponse({"choices": [{"message": {"content": "建议充到100%"}}]})
        with patch.dict("os.environ", self.environment, clear=False), patch(
            "app.ai_recommender.urlopen", return_value=response,
        ):
            with self.assertRaises(AIRecommendationError):
                AIRecommender().recommend(self.request, self.route, self.candidates)

    def test_all_tesla_baseline_hides_public_and_service_area_candidates_from_ai(self):
        request = self.request.model_copy(update={"strategy": Strategy.TESLA_PRIORITY})
        tesla = self.candidates[0]
        public = StationCandidate(
            id="public", name="公共快充", lng=103, lat=30,
            station_type=StationType.CHARGING_STATION,
            progress_km=300,
        )
        service = StationCandidate(
            id="service", name="高速服务区充电站", lng=104, lat=30,
            station_type=StationType.SERVICE_AREA,
            progress_km=400,
        )
        baseline = ChargeStop(
            **tesla.model_dump(),
            sequence=1,
            leg_distance_km=203,
            arrival_range_km=40,
            charge_from_percent=10,
            charge_to_percent=80,
            charge_minutes=30,
            added_range_km=280,
            route_progress_percent=40,
        )

        compact = AIRecommender._compact_candidates(
            [tesla, public, service], request, [baseline],
        )
        compact_without_baseline = AIRecommender._compact_candidates(
            [tesla, public, service], request, [],
        )

        self.assertEqual([candidate["id"] for candidate in compact], ["real-station"])
        self.assertEqual([candidate["id"] for candidate in compact_without_baseline], ["real-station"])
        self.assertNotIn("exit_name", compact[0])
        self.assertNotIn("entry_name", compact[0])
        self.assertEqual(compact[0]["highway_round_trip_km"], 7)

        content = json.dumps({
            "charge_to_percent": 80,
            "max_detour_km": 12,
            "reserve_range_km": 40,
            "preferred_station_ids": ["public", "real-station"],
            "search_progress_km": [],
            "reason_codes": ["candidate_quality"],
        })
        response = FakeResponse({"choices": [{"message": {"content": content}}]})
        with patch.dict("os.environ", self.environment, clear=False), patch(
            "app.ai_recommender.urlopen", return_value=response,
        ):
            recommendation = AIRecommender().recommend(
                request, self.route, [tesla, public, service], [baseline],
            )
        self.assertEqual(recommendation.preferred_station_ids, ["real-station"])

    def test_read_timeout_is_converted_to_controlled_fallback(self):
        environment = {**self.environment, "AI_TIMEOUT_SECONDS": "12"}
        with patch.dict("os.environ", environment, clear=False), patch(
            "app.ai_recommender.urlopen", side_effect=OSError("The read operation timed out"),
        ) as mocked:
            with self.assertRaises(AIRecommendationError):
                AIRecommender().recommend(self.request, self.route, self.candidates)
        self.assertEqual(mocked.call_args.kwargs["timeout"], 12)


if __name__ == "__main__":
    unittest.main()

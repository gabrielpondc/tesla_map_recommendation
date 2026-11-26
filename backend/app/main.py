from __future__ import annotations

import os
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .amap import AmapClient, AmapError
from .baidu import BaiduWebClient
from .models import AppConfig, BaiduCandidateRequest, GeoPoint, Place, PlanRequest, PlanResponse, StationCandidate, Suggestion
from .planner import build_plan


def load_dotenv() -> None:
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()
client = AmapClient()
baidu_client = BaiduWebClient()
map_provider = os.getenv("MAP_PROVIDER", "amap").strip().lower()
if map_provider not in {"amap", "baidu"}:
    map_provider = "amap"
app = FastAPI(title="电途 EV Route Planner", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


DEMO_PLACES = [
    Suggestion(name="上海市人民广场", address="上海市黄浦区人民大道", district="黄浦区", lng=121.4737, lat=31.2304),
    Suggestion(name="杭州西湖风景名胜区", address="浙江省杭州市西湖区", district="西湖区", lng=120.1302, lat=30.2596),
    Suggestion(name="南京南站", address="江苏省南京市雨花台区", district="雨花台区", lng=118.7965, lat=31.9680),
    Suggestion(name="苏州中心", address="江苏省苏州市工业园区", district="工业园区", lng=120.6720, lat=31.3174),
    Suggestion(name="宁波天一广场", address="浙江省宁波市海曙区", district="海曙区", lng=121.5500, lat=29.8746),
    Suggestion(name="合肥南站", address="安徽省合肥市包河区", district="包河区", lng=117.2902, lat=31.7992),
]


@app.get("/api/health")
def health():
    selected_enabled = bool(
        os.getenv("BAIDU_AK", "")
        if map_provider == "baidu"
        else (os.getenv("AMAP_JS_KEY", "") or client.enabled)
    )
    return {"status": "ok", "mode": map_provider if selected_enabled else "demo", "map_provider": map_provider}


@app.get("/api/config", response_model=AppConfig)
def config():
    js_key = os.getenv("AMAP_JS_KEY", "")
    baidu_ak = os.getenv("BAIDU_AK", "")
    selected_enabled = bool(baidu_ak if map_provider == "baidu" else (js_key or client.enabled))
    return AppConfig(
        map_provider=map_provider,
        map_enabled=selected_enabled,
        amap_enabled=bool(js_key),
        amap_js_key=js_key,
        amap_security_code=os.getenv("AMAP_SECURITY_CODE", ""),
        baidu_enabled=bool(baidu_ak),
        baidu_ak=baidu_ak,
        baidu_server_enabled=baidu_client.enabled,
        mode=map_provider if selected_enabled else "demo",
        ai_enabled=bool(os.getenv("AI_API_BASE_URL", "") and os.getenv("AI_API_KEY", "")),
        ai_model=os.getenv("AI_MODEL", "") if os.getenv("AI_API_KEY", "") else "",
    )


@app.get("/api/places/suggest", response_model=List[Suggestion])
def suggest(q: str = Query(..., min_length=1, max_length=80), city: str = "全国"):
    if baidu_client.enabled:
        try:
            return baidu_client.suggest(q, city, map_provider)
        except Exception:
            pass
    if map_provider == "amap" and client.enabled:
        try:
            return client.suggest(q, city)
        except AmapError:
            pass
    needle = q.lower()
    matched = [place for place in DEMO_PLACES if needle in place.name.lower() or needle in place.address.lower()]
    return matched


@app.get("/api/places/reverse", response_model=Place)
def reverse(lng: float, lat: float):
    point = GeoPoint(lng=lng, lat=lat)
    if map_provider == "amap" and client.enabled:
        try:
            return client.reverse_geocode(point, from_gps=True)
        except AmapError:
            pass
    return Place(name="我的位置", address=f"{lat:.5f}, {lng:.5f}", lng=lng, lat=lat)


@app.post("/api/plan", response_model=PlanResponse)
def plan(request: PlanRequest):
    try:
        return build_plan(request, client)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"路线规划失败：{exc}") from exc


@app.post("/api/map/baidu/candidates", response_model=List[StationCandidate])
def baidu_candidates(request: BaiduCandidateRequest):
    if not baidu_client.enabled:
        raise HTTPException(status_code=503, detail="百度服务端地点检索未配置")
    try:
        return baidu_client.find_chargers(
            request.route, request.max_detour_km, request.map_provider,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"百度沿途充电站检索失败：{exc}") from exc

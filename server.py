"""멍트립 — 반려동물 동반 관광지 웹앱.
한국관광공사 TourAPI 4.0 반려동물 동반여행 서비스(KorPetTourService2) 프록시 + 정적 서빙.
주의: 지역 필터는 areaCode가 아니라 법정동 코드(lDongRegnCd)를 쓴다 — areaCode는 대부분 빈값.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv(Path(__file__).parent / ".env")

DB_PATH = Path(__file__).parent / "mungtrip.db"
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row

API_KEY = os.environ["TOUR_API_KEY"]
TMAP_KEY = os.environ.get("TMAP_APP_KEY", "")
BASE = "https://apis.data.go.kr/B551011/KorPetTourService2"
COMMON = {"MobileOS": "WEB", "MobileApp": "mungtrip", "_type": "json"}

app = FastAPI(title="멍트립")

_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 600  # 10분


async def _get(op: str, **params) -> dict:
    key = op + str(sorted(params.items()))
    now = time.time()
    if key in _cache and now - _cache[key][0] < CACHE_TTL:
        return _cache[key][1]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{BASE}/{op}", params={"serviceKey": API_KEY, **COMMON, **params})
    r.raise_for_status()
    try:
        body = r.json()["response"]["body"]
    except Exception:
        raise HTTPException(502, f"TourAPI 응답 오류: {r.text[:200]}")
    _cache[key] = (now, body)
    return body


def _items(body: dict) -> list[dict]:
    items = body.get("items") or {}
    if not isinstance(items, dict):
        return []
    item = items.get("item") or []
    return item if isinstance(item, list) else [item]


def _norm(it: dict) -> dict:
    return {
        "id": it.get("contentid"),
        "title": it.get("title"),
        "addr": (it.get("addr1") or "").strip(),
        "img": it.get("firstimage") or "",
        "mapx": it.get("mapx"),
        "mapy": it.get("mapy"),
        "type": it.get("contenttypeid"),
        "tel": it.get("tel") or "",
    }


@app.get("/api/places")
async def places(
    region: str = Query("", description="법정동 시도코드 (11=서울...)"),
    type: str = Query("", description="contentTypeId (12=관광지...)"),
    keyword: str = Query(""),
    sort: str = Query("reco", description="reco=추천순 | dist=거리순(lat/lng 필수) | name=이름순"),
    lat: float = Query(0.0),
    lng: float = Query(0.0),
    page: int = Query(1, ge=1),
    size: int = Query(12, ge=1, le=50),
):
    """로컬 DB(관광공사 전량 + 문화정보원 병합) 조회.

    추천순 = 이미지 보유 우선 + rowid(수집 시 관광공사 arrange=Q 순서 = 수정일·대표이미지 우선) 유지.
    거리순 = 위경도 근사 제곱거리로 정렬(정렬용으로 충분), 표시 거리는 하버사인으로 계산.
    """
    import math

    where, args = [], []
    if region:
        where.append("region = ?")
        args.append(region)
    if type:
        where.append("type = ?")
        args.append(type)
    if keyword.strip():
        where.append("(title LIKE ? OR addr LIKE ?)")
        kw = f"%{keyword.strip()}%"
        args += [kw, kw]

    order = "ORDER BY (img = '') ASC, rowid ASC"  # reco 기본
    order_args: list = []
    if sort == "name":
        order = "ORDER BY title ASC"
    elif sort == "dist" and lat and lng:
        where.append("lat > 0")
        coslat = math.cos(math.radians(lat))
        order = "ORDER BY (lat - ?) * (lat - ?) + (lng - ?) * (lng - ?) * ? ASC"
        order_args = [lat, lat, lng, lng, coslat * coslat]

    cond = ("WHERE " + " AND ".join(where)) if where else ""
    total = _db.execute(f"SELECT COUNT(*) FROM places {cond}", args).fetchone()[0]
    rows = _db.execute(
        f"SELECT id, src, title, addr, tel, img, lat, lng, type FROM places {cond} "
        f"{order} LIMIT ? OFFSET ?",
        args + order_args + [size, (page - 1) * size],
    ).fetchall()

    def hav(r):
        if not (sort == "dist" and lat and lng and r["lat"]):
            return None
        p1, p2 = math.radians(lat), math.radians(r["lat"])
        dp, dl = math.radians(r["lat"] - lat), math.radians(r["lng"] - lng)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return round(2 * 6371000 * math.asin(math.sqrt(a)))

    return {
        "total": total,
        "page": page,
        "items": [
            {
                "id": r["id"], "src": r["src"], "title": r["title"], "addr": r["addr"],
                "img": r["img"], "mapx": r["lng"], "mapy": r["lat"],
                "type": r["type"], "tel": r["tel"], "dist": hav(r),
            }
            for r in rows
        ],
    }


# contentTypeId별 이용안내(detailIntro2) 필드 → 라벨
INTRO_FIELDS: dict[str, dict[str, str]] = {
    "12": {"infocenter": "문의", "usetime": "이용 시간", "restdate": "쉬는 날", "parking": "주차", "expguide": "체험 안내"},
    "14": {"infocenterculture": "문의", "usetimeculture": "이용 시간", "restdateculture": "쉬는 날", "parkingculture": "주차", "usefee": "이용 요금"},
    "28": {"infocenterleports": "문의", "usetimeleports": "이용 시간", "restdateleports": "쉬는 날", "parkingleports": "주차", "usefeeleports": "이용 요금"},
    "32": {"infocenterlodging": "문의", "checkintime": "체크인", "checkouttime": "체크아웃", "parkinglodging": "주차", "reservationurl": "예약"},
    "38": {"infocentershopping": "문의", "opentime": "영업 시간", "restdateshopping": "쉬는 날", "parkingshopping": "주차", "saleitem": "판매 품목"},
    "39": {"infocenterfood": "문의", "opentimefood": "영업 시간", "restdatefood": "쉬는 날", "parkingfood": "주차", "firstmenu": "대표 메뉴", "treatmenu": "취급 메뉴"},
}

PET_FIELDS = {
    "acmpyTypeCd": "동반 유형",
    "acmpyPsblCpam": "동반 가능 동물",
    "acmpyNeedMtr": "동반 시 필요사항",
    "etcAcmpyInfo": "기타 동반 정보",
    "relaPosesFclty": "구비 시설",
    "relaFrnshPrdlst": "비치 품목",
    "relaPurcPrdlst": "구매 가능 품목",
    "relaRntlPrdlst": "대여 가능 품목",
    "relaAcdntRiskMtr": "사고 예방 안내",
}


def _nearby(sql: str, lat: float, lng: float, radius_m: int, limit: int = 3) -> list[dict]:
    """바운딩박스 후보 조회(sql은 lat/lng BETWEEN 4개 파라미터를 받아야 함) → 하버사인 정렬 상위 N."""
    import math

    if not lat or not lng:
        return []
    dlat = radius_m / 111000
    dlng = radius_m / (111000 * math.cos(math.radians(lat)))
    rows = _db.execute(sql, (lat - dlat, lat + dlat, lng - dlng, lng + dlng)).fetchall()

    def dist(r):
        p1, p2 = math.radians(lat), math.radians(r["lat"])
        dp, dl = math.radians(r["lat"] - lat), math.radians(r["lng"] - lng)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * 6371000 * math.asin(math.sqrt(a))

    cand = sorted(((dist(r), r) for r in rows), key=lambda x: x[0])
    return [{**dict(r), "dist": round(d)} for d, r in cand[:limit] if d <= radius_m]


PLAYGROUND_SQL = (
    "SELECT id, title AS name, lat, lng FROM places "
    "WHERE (title LIKE '%반려견놀이터%' OR title LIKE '%애견놀이터%' "
    "OR title LIKE '%반려동물놀이터%' OR title LIKE '%강아지놀이터%') "
    "AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?"
)


def _amenities(lat: float, lng: float, exclude_id: str = "") -> dict:
    toilets = _nearby(
        "SELECT DISTINCT name, open_time AS open, lat, lng FROM toilets "
        "WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?", lat, lng, 1000)
    waters = _nearby(
        "SELECT DISTINCT name, lat, lng FROM waters "
        "WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?", lat, lng, 1500)
    grounds = [
        g for g in _nearby(PLAYGROUND_SQL, lat, lng, 10000)
        if g["id"] != exclude_id
    ][:3]
    return {"toilets": toilets, "waters": waters, "grounds": grounds}


def _kcisa_extras(row) -> tuple[list[dict], list[dict]]:
    """DB의 문화정보원 필드 → (pet 행, intro 행)."""
    pet = [
        {"label": label, "value": row[col]}
        for col, label in [
            ("k_size", "입장 가능 동물 크기"), ("k_restrict", "동반 제한사항"), ("k_inout", "동반 가능 구역"),
        ]
        if row[col]
    ]
    intro = [
        {"label": label, "value": row[col]}
        for col, label in [
            ("k_time", "운영 시간"), ("k_rest", "휴무일"), ("k_parking", "주차"), ("k_fee", "이용 요금"),
        ]
        if row[col]
    ]
    return pet, intro


@app.get("/api/place/{content_id}")
async def place_detail(content_id: str):
    db_row = _db.execute("SELECT * FROM places WHERE id = ?", (content_id,)).fetchone()

    # 문화정보원 단독 장소 — DB만으로 응답
    if content_id.startswith("k"):
        if not db_row:
            raise HTTPException(404, "장소를 찾을 수 없어요")
        pet, intro = _kcisa_extras(db_row)
        return {
            "id": content_id,
            "type": db_row["type"],
            "title": db_row["title"],
            "addr": db_row["addr"],
            "img": "",
            "tel": db_row["tel"],
            "homepage": db_row["k_homepage"],
            "overview": db_row["k_desc"],
            "mapx": db_row["lng"],
            "mapy": db_row["lat"],
            "images": [],
            "intro": intro,
            "pet": [{"label": "동반 가능 여부", "value": "동반 가능 (문화정보원 확인)"}] + pet,
            **_amenities(db_row["lat"], db_row["lng"], exclude_id=content_id),
            "source": "한국문화정보원",
        }

    common, pet, images = await asyncio.gather(
        _get("detailCommon2", contentId=content_id),
        _get("detailPetTour2", contentId=content_id),
        _get("detailImage2", contentId=content_id, imageYN="Y", numOfRows=8),
    )
    c = (_items(common) or [{}])[0]
    p = (_items(pet) or [{}])[0]

    ctype = c.get("contenttypeid", "")
    intro_rows: list[dict] = []
    if ctype in INTRO_FIELDS:
        intro = await _get("detailIntro2", contentId=content_id, contentTypeId=ctype)
        i = (_items(intro) or [{}])[0]
        intro_rows = [
            {"label": label, "value": (i.get(field) or "").strip()}
            for field, label in INTRO_FIELDS[ctype].items()
            if (i.get(field) or "").strip()
        ]

    pet_rows = [
        {"label": label, "value": p.get(field, "").strip()}
        for field, label in PET_FIELDS.items()
        if (p.get(field) or "").strip()
    ]
    # 중복 병합된 장소면 문화정보원 필드로 보강 (이미 있는 라벨은 건너뜀)
    if db_row and db_row["k_restrict"] is not None:
        k_pet, k_intro = _kcisa_extras(db_row)
        have = {r["label"] for r in pet_rows}
        pet_rows += [r for r in k_pet if r["label"] not in have]
        have_i = {r["label"] for r in intro_rows}
        intro_rows += [r for r in k_intro if r["label"] not in have_i]

    return {
        "id": content_id,
        "type": ctype,
        "title": c.get("title"),
        "addr": (c.get("addr1") or "").strip(),
        "img": c.get("firstimage") or "",
        "tel": c.get("tel") or "",
        "homepage": c.get("homepage") or "",
        "overview": c.get("overview") or "",
        "mapx": c.get("mapx"),
        "mapy": c.get("mapy"),
        "images": [im.get("originimgurl") for im in _items(images) if im.get("originimgurl")],
        "intro": intro_rows,
        "pet": pet_rows,
        **_amenities(
            float(c.get("mapy") or 0), float(c.get("mapx") or 0), exclude_id=content_id),
        "source": "한국관광공사",
    }


@app.get("/api/route")
async def route(
    sx: float = Query(..., description="출발 경도"),
    sy: float = Query(..., description="출발 위도"),
    ex: float = Query(..., description="도착 경도"),
    ey: float = Query(..., description="도착 위도"),
):
    """T맵 자동차 경로안내 — 좌표를 소수 4자리로 반올림해 캐시 적중률 확보(~10m 정밀도)."""
    if not TMAP_KEY:
        raise HTTPException(503, "TMAP_APP_KEY 미설정")
    sx, sy, ex, ey = round(sx, 4), round(sy, 4), round(ex, 4), round(ey, 4)
    cache_key = f"tmap:{sx},{sy},{ex},{ey}"
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key][0] < CACHE_TTL:
        return _cache[cache_key][1]

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://apis.openapi.sk.com/tmap/routes?version=1",
            headers={"appKey": TMAP_KEY},
            json={
                "startX": str(sx), "startY": str(sy),
                "endX": str(ex), "endY": str(ey),
                "reqCoordType": "WGS84GEO", "resCoordType": "WGS84GEO",
            },
        )
    if r.status_code != 200:
        raise HTTPException(502, f"T맵 응답 오류({r.status_code})")
    data = r.json()
    features = data.get("features") or []
    if not features:
        raise HTTPException(404, "경로를 찾을 수 없어요")
    props = features[0]["properties"]
    path = [
        [lat, lng]
        for f in features
        if f["geometry"]["type"] == "LineString"
        for lng, lat in f["geometry"]["coordinates"]
    ]
    result = {
        "distance": props.get("totalDistance", 0),  # m
        "time": props.get("totalTime", 0),          # s
        "path": path,
    }
    _cache[cache_key] = (now, result)
    return result


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/place/{content_id}")
async def place_page(content_id: str):
    return FileResponse(Path(__file__).parent / "static" / "place.html")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

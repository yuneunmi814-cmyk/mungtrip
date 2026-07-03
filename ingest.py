"""멍트립 데이터 수집·병합 — TourAPI 전량 + 한국문화정보원 CSV → mungtrip.db

실행: .venv/bin/python ingest.py [문화정보원CSV경로]
- TourAPI(KorPetTourService2) 전 페이지 수집 (~98콜, 1~2분)
- 문화정보원 CSV에서 여행 관련(반려동반여행·식당카페) + 동반가능 Y만 추출
- 중복 제거: 정규화 이름 동일 AND 좌표 100m 이내 → 관광공사 우선, 문화정보원 필드는 보강으로 병합
"""
from __future__ import annotations

import csv
import math
import os
import re
import sqlite3
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

API_KEY = os.environ["TOUR_API_KEY"]
BASE = "https://apis.data.go.kr/B551011/KorPetTourService2"
DB = Path(__file__).parent / "mungtrip.db"

# 시도 명칭 → 법정동 시도코드 (TourAPI lDongRegnCd와 동일 체계)
SIDO_CODE = {
    "서울특별시": "11", "부산광역시": "26", "대구광역시": "27", "인천광역시": "28",
    "광주광역시": "29", "대전광역시": "30", "울산광역시": "31", "세종특별자치시": "36",
    "경기도": "41", "강원특별자치도": "51", "강원도": "51",
    "충청북도": "43", "충청남도": "44",
    "전북특별자치도": "52", "전라북도": "52", "전라남도": "46",
    "경상북도": "47", "경상남도": "48", "제주특별자치도": "50", "제주도": "50",
}

# 문화정보원 카테고리3 → contentTypeId 버킷
KCISA_TYPE = {
    "여행지": "12", "박물관": "14", "미술관": "14", "문예회관": "14",
    "카페": "39", "식당": "39", "펜션": "32", "호텔": "32",
}


def norm_name(s: str) -> str:
    """이름 정규화 — 공백·괄호·특수문자 제거."""
    s = re.sub(r"\([^)]*\)", "", s or "")
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", s).lower()


def dist_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _get_page(client: httpx.Client, page: int) -> httpx.Response:
    """페이지 조회 — 일시 오류(타임아웃 등)는 3회까지 재시도."""
    import time as _t
    for attempt in range(3):
        try:
            r = client.get(f"{BASE}/areaBasedList2", params={
                "serviceKey": API_KEY, "MobileOS": "ETC", "MobileApp": "mungtrip",
                "_type": "json", "numOfRows": 100, "pageNo": page, "arrange": "Q",
            })
            r.raise_for_status()
            return r
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError):
            if attempt == 2:
                raise
            _t.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def fetch_tour_all() -> list[dict]:
    out, page = [], 1
    with httpx.Client(timeout=30) as client:
        while True:
            r = _get_page(client, page)
            body = r.json()["response"]["body"]
            items = body.get("items") or {}
            batch = items.get("item") or [] if isinstance(items, dict) else []
            if not batch:
                break
            out.extend(batch)
            total = body.get("totalCount", 0)
            print(f"  TourAPI {len(out):,}/{total:,}", end="\r")
            if len(out) >= total:
                break
            page += 1
    print()
    return out


def fetch_toilets() -> list[dict]:
    """행안부 전국공중화장실 표준데이터 — 좌표 있는 것만."""
    import time as _t
    out, page = [], 1
    with httpx.Client(timeout=30) as client:
        while True:
            for attempt in range(3):
                try:
                    r = client.get(
                        "https://api.data.go.kr/openapi/tn_pubr_public_toilet_api",
                        params={"serviceKey": API_KEY, "pageNo": page, "numOfRows": 1000, "type": "json"},
                    )
                    r.raise_for_status()
                    break
                except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError):
                    if attempt == 2:
                        raise
                    _t.sleep(5 * (attempt + 1))
            body = r.json()["response"]["body"]
            items = body.get("items") or []
            if not items:
                break
            out.extend(items)
            total = int(body.get("totalCount", 0))
            print(f"  화장실 {len(out):,}/{total:,}", end="\r")
            if len(out) >= total:
                break
            page += 1
    print()
    ok = []
    for t in out:
        try:
            lat, lng = float(t.get("latitude") or ""), float(t.get("longitude") or "")
        except ValueError:
            continue
        if not (33 < lat < 39 and 124 < lng < 132):  # 한반도 밖 좌표 오류 제거
            continue
        ok.append({
            "name": (t.get("toiletNm") or "공중화장실").strip(),
            "addr": (t.get("rdnmadr") or t.get("lnmadr") or "").strip(),
            "open_time": (t.get("openTime") or "").replace("null", "").strip(),
            "lat": lat, "lng": lng,
        })
    return ok


def save_toilets(con: sqlite3.Connection) -> int:
    toilets = fetch_toilets()
    con.execute("DROP TABLE IF EXISTS toilets")
    con.execute("CREATE TABLE toilets (name TEXT, addr TEXT, open_time TEXT, lat REAL, lng REAL)")
    con.executemany(
        "INSERT INTO toilets VALUES (:name, :addr, :open_time, :lat, :lng)", toilets)
    con.execute("CREATE INDEX idx_toilet_lat ON toilets(lat)")
    con.commit()
    return len(toilets)


def fetch_park_waters() -> list[dict]:
    """전국도시공원 표준데이터에서 편익시설에 음수대/음수전이 명시된 공원만 추출."""
    import time as _t
    out, page = [], 1
    with httpx.Client(timeout=30) as client:
        while True:
            for attempt in range(3):
                try:
                    r = client.get(
                        "https://api.data.go.kr/openapi/tn_pubr_public_cty_park_info_api",
                        params={"serviceKey": API_KEY, "pageNo": page, "numOfRows": 1000, "type": "json"},
                    )
                    r.raise_for_status()
                    break
                except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError):
                    if attempt == 2:
                        raise
                    _t.sleep(5 * (attempt + 1))
            body = r.json()["response"]["body"]
            items = body.get("items") or []
            if not items:
                break
            out.extend(items)
            total = int(body.get("totalCount", 0))
            print(f"  공원 {len(out):,}/{total:,}", end="\r")
            if len(out) >= total:
                break
            page += 1
    print()
    ok = []
    for p in out:
        if "음수" not in (p.get("cnvnncFclty") or ""):
            continue
        try:
            lat, lng = float(p.get("latitude") or ""), float(p.get("longitude") or "")
        except ValueError:
            continue
        if not (33 < lat < 39 and 124 < lng < 132):
            continue
        ok.append({
            "name": (p.get("parkNm") or "공원").strip(),
            "addr": (p.get("rdnmadr") or p.get("lnmadr") or "").strip(),
            "lat": lat, "lng": lng,
        })
    return ok


def save_waters(con: sqlite3.Connection) -> int:
    waters = fetch_park_waters()
    con.execute("DROP TABLE IF EXISTS waters")
    con.execute("CREATE TABLE waters (name TEXT, addr TEXT, lat REAL, lng REAL)")
    con.executemany("INSERT INTO waters VALUES (:name, :addr, :lat, :lng)", waters)
    con.execute("CREATE INDEX idx_water_lat ON waters(lat)")
    con.commit()
    return len(waters)


def load_kcisa(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [
        r for r in rows
        if r.get("카테고리2") in ("반려동반여행", "반려동물식당카페")
        and (r.get("반려동물 동반 가능정보") or "").strip() == "Y"
        and (r.get("위도") or "").strip() and (r.get("경도") or "").strip()
    ]


def main() -> None:
    if "--toilets-only" in sys.argv:  # 화장실만 갱신 (개발용)
        con = sqlite3.connect(DB)
        n = save_toilets(con)
        print(f"화장실 {n:,}건 저장 완료")
        con.close()
        return

    if "--waters-only" in sys.argv:  # 음수대(공원)만 갱신 (개발용)
        con = sqlite3.connect(DB)
        n = save_waters(con)
        print(f"음수대 보유 공원 {n:,}건 저장 완료")
        con.close()
        return

    csv_path = sys.argv[1] if len(sys.argv) > 1 else "pet_facilities.csv"

    print("1) TourAPI 전량 수집…")
    tour = fetch_tour_all()
    print(f"   → {len(tour):,}건")

    print("2) 문화정보원 CSV 로드…")
    kcisa = load_kcisa(csv_path)
    print(f"   → 동반가능 여행시설 {len(kcisa):,}건")

    con = sqlite3.connect(DB)
    con.execute("DROP TABLE IF EXISTS places")
    con.execute("""CREATE TABLE places (
        id TEXT PRIMARY KEY, src TEXT, title TEXT, addr TEXT, tel TEXT, img TEXT,
        lat REAL, lng REAL, type TEXT, region TEXT,
        k_size TEXT, k_restrict TEXT, k_inout TEXT,
        k_time TEXT, k_rest TEXT, k_parking TEXT, k_fee TEXT,
        k_homepage TEXT, k_desc TEXT
    )""")

    grid: dict[tuple, list] = {}  # (norm_name, lat/lng 셀) 빠른 중복 조회용
    def grid_key(name, lat, lng):
        return (name, round(lat, 2), round(lng, 2))  # ~1km 셀

    n_tour = 0
    for it in tour:
        try:
            lat, lng = float(it.get("mapy") or 0), float(it.get("mapx") or 0)
        except ValueError:
            lat, lng = 0, 0
        row = {
            "id": it["contentid"], "src": "tour", "title": it.get("title") or "",
            "addr": (it.get("addr1") or "").strip(), "tel": it.get("tel") or "",
            "img": it.get("firstimage") or "", "lat": lat, "lng": lng,
            "type": it.get("contenttypeid") or "", "region": it.get("lDongRegnCd") or "",
        }
        con.execute(
            "INSERT OR IGNORE INTO places (id,src,title,addr,tel,img,lat,lng,type,region) "
            "VALUES (:id,:src,:title,:addr,:tel,:img,:lat,:lng,:type,:region)", row)
        nm = norm_name(row["title"])
        if nm and lat:
            # 주변 4셀에 모두 등록할 필요는 없음 — 조회 시 이웃 셀 검사
            grid.setdefault(grid_key(nm, lat, lng), []).append((row["id"], lat, lng))
        n_tour += 1

    n_new, n_merged = 0, 0
    for i, r in enumerate(kcisa):
        lat, lng = float(r["위도"]), float(r["경도"])
        nm = norm_name(r["시설명"])
        inout = "/".join(filter(None, [
            "실내" if (r.get("장소(실내) 여부") or "").strip() == "Y" else "",
            "실외" if (r.get("장소(실외)여부") or "").strip() == "Y" else "",
        ]))
        kf = {
            "k_size": (r.get("입장 가능 동물 크기") or "").replace("해당없음", "").strip(),
            "k_restrict": (r.get("반려동물 제한사항") or "").replace("해당없음", "").strip(),
            "k_inout": inout,
            "k_time": (r.get("운영시간") or "").strip(),
            "k_rest": (r.get("휴무일") or "").strip(),
            "k_parking": (r.get("주차 가능여부") or "").strip(),
            "k_fee": (r.get("입장(이용료)가격 정보") or "").strip(),
            "k_homepage": (r.get("홈페이지") or "").strip(),
            "k_desc": (r.get("기본 정보_장소설명") or "").strip(),
        }

        # 중복 검사: 같은 정규화 이름 + 100m 이내 (이웃 셀 포함)
        dup_id = None
        for dx in (-0.01, 0, 0.01):
            for dy in (-0.01, 0, 0.01):
                for tid, tlat, tlng in grid.get((nm, round(lat + dx, 2), round(lng + dy, 2)), []):
                    if dist_m(lat, lng, tlat, tlng) < 100:
                        dup_id = tid
                        break
                if dup_id:
                    break
            if dup_id:
                break

        if dup_id:  # 관광공사 행에 문화정보원 필드 보강
            con.execute(
                "UPDATE places SET k_size=:k_size, k_restrict=:k_restrict, k_inout=:k_inout, "
                "k_time=:k_time, k_rest=:k_rest, k_parking=:k_parking, k_fee=:k_fee, "
                "k_homepage=:k_homepage, k_desc=:k_desc WHERE id=:id",
                {**kf, "id": dup_id})
            n_merged += 1
        else:
            con.execute(
                "INSERT INTO places VALUES (:id,:src,:title,:addr,:tel,:img,:lat,:lng,:type,:region,"
                ":k_size,:k_restrict,:k_inout,:k_time,:k_rest,:k_parking,:k_fee,:k_homepage,:k_desc)",
                {
                    "id": f"k{i}", "src": "kcisa", "title": r["시설명"],
                    "addr": (r.get("도로명주소") or r.get("지번주소") or "").strip(),
                    "tel": (r.get("전화번호") or "").strip(), "img": "",
                    "lat": lat, "lng": lng,
                    "type": KCISA_TYPE.get(r.get("카테고리3", ""), "12"),
                    "region": SIDO_CODE.get((r.get("시도 명칭") or "").strip(), ""),
                    **kf,
                })
            n_new += 1

    con.execute("CREATE INDEX idx_region ON places(region)")
    con.execute("CREATE INDEX idx_type ON places(type)")
    con.commit()

    total = con.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    print(f"3) 완료 — 관광공사 {n_tour:,} + 문화정보원 신규 {n_new:,} (중복병합 {n_merged:,}) = 총 {total:,}건")

    print("4) 공중화장실 수집…")
    n_toilet = save_toilets(con)
    print(f"   → {n_toilet:,}건 (좌표 보유분)")

    print("5) 음수대 보유 공원 수집…")
    n_water = save_waters(con)
    print(f"   → {n_water:,}건")
    print(f"   DB: {DB}")
    con.close()


if __name__ == "__main__":
    main()

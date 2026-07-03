# 멍트립 🐾

반려동물과 함께 떠나는 여행지 찾기 — 한국관광공사 TourAPI 4.0 **반려동물 동반여행 서비스(KorPetTourService2)** 기반.

## 실행

```bash
.venv/bin/uvicorn server:app --port 8330
# → http://localhost:8330
```

의존성 설치: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
API 키: `.env`의 `TOUR_API_KEY` (data.go.kr TourAPI 활용신청 키, 축제로와 공유)

## 데이터

`mungtrip.db` (SQLite) — 두 소스 병합, 총 **12,062곳**:
- 한국관광공사 TourAPI 반려동물 동반여행 9,768곳 (사진·소개글·상세 API 연동)
- 한국문화정보원 「전국 반려동물 동반 가능 문화시설」 중 동반가능(Y) 여행시설 2,381곳
  - 신규 2,294 + 중복 87곳은 관광공사 행에 동반조건(크기·제한·구역) 보강
  - 중복 기준: 정규화 이름 동일 AND 좌표 100m 이내
- 주변 편의시설 (상세페이지, 직선거리·지도 마커):
  - 🚻 행안부 「전국공중화장실 표준데이터」 27,026곳 — 1km 내 3곳 (회색 마커)
  - 🚰 「전국도시공원 표준데이터」 중 편익시설에 음수대/음수전 명시된 공원 769곳 — 1.5km 내 3곳 (파란 마커). 공원 데이터에 음수대 기재가 성긴 편이라 커버리지 제한적
  - 🐕 반려견놀이터 — 자체 places 테이블에서 이름 패턴(반려견/애견/강아지놀이터) 매칭, 10km 내 3곳, 상세페이지 링크 (초록 마커)

갱신: `./refresh.sh` — CSV 재다운로드(실패 시 기존 파일) → ingest.py → DB 검증(1만 건 미만이면 중단) → Fly 배포 → 라이브 확인. 로그는 `refresh.log`.
**매월 1일 오전 5시 자동 실행** (Claude Code 예약작업 `mungtrip-monthly-refresh` — 앱이 꺼져 있으면 다음 실행 시 수행).

## 구조

- `ingest.py` — TourAPI 전량 수집 + 문화정보원 CSV 병합 → mungtrip.db 생성
- `server.py` — FastAPI. 목록은 SQLite, 상세는 TourAPI 실시간(+DB 보강) + 정적 서빙
  - `GET /api/places?region=&type=&keyword=&page=` — 목록 (SQLite, 이미지 보유 우선 정렬)
  - `GET /api/place/{id}` — 상세 (detailCommon2 + detailPetTour2 + detailImage2 병합, 타입별 detailIntro2 이용안내)
  - `GET /place/{id}` — 상세 페이지 서빙
  - `GET /api/route?sx&sy&ex&ey` — T맵 자동차 길찾기 프록시(좌표 4자리 반올림 캐시). 키는 `TMAP_APP_KEY`
- `static/index.html` — 목록 페이지. 원티드 디자인 시스템 토큰 적용(`style-guides/wanted-style-guide.md`), 라이트 테마
  - 목록/지도 토글 — 지도는 Leaflet+OSM, 현재 필터 기준 최신 50곳 마커, 카드·마커 클릭 시 상세 페이지 이동
- `static/place.html` — 상세 페이지. 이미지 갤러리(썸네일 스왑) + 반려동물 동반 안내(최상단) + 소개(더보기 접기) + 이용 안내(타입별) + 위치 지도 + **T맵 길찾기**(현재위치→목적지 경로를 지도에 그리고 거리·시간 표시, 모바일이면 `tmap://` 앱 딥링크 버튼 추가) + 카카오맵/홈페이지 버튼

## 배포 (Fly.io)

```bash
flyctl apps create mungtrip
flyctl secrets set TOUR_API_KEY=<키>
flyctl deploy
```

`Dockerfile`·`fly.toml` 준비돼 있음(도쿄 리전, auto-stop으로 비용 최소화). `.env`는 이미지에 포함되지 않으므로 키는 반드시 `fly secrets`로.

## API 주의사항 (삽질 방지)

- 서비스명은 **KorPetTourService2** — 구버전(1/무접미사)은 폐기됨
- 지역 필터는 `areaCode`가 아니라 **`lDongRegnCd`(법정동 시도코드)** — areaCode는 대부분 빈값이라 서울이 59건밖에 안 나옴. lDongRegnCd=11이면 3,180건
  - 11 서울, 26 부산, 27 대구, 28 인천, 29 광주, 30 대전, 31 울산, 36 세종, 41 경기, 51 강원, 43 충북, 44 충남, 52 전북, 46 전남, 47 경북, 48 경남, 50 제주
- 전국 데이터 약 9,768곳 (2026-07 기준)
- 반려동물 동반 정보 필드: `acmpyTypeCd`(동반유형), `acmpyPsblCpam`(가능동물), `acmpyNeedMtr`(필요사항), `etcAcmpyInfo`, `relaPosesFclty`, `relaFrnshPrdlst`, `relaPurcPrdlst`, `relaRntlPrdlst`, `relaAcdntRiskMtr`

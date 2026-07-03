#!/bin/zsh
# 멍트립 월간 데이터 갱신 — CSV 재다운로드 → ingest → 검증 → Fly 배포
# 사용: ./refresh.sh   (로그: refresh.log)
set -eo pipefail
cd "$(dirname "$0")"
LOG="refresh.log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"; }

log "=== 갱신 시작 ==="

# 1) 문화정보원 CSV 최신본 시도 (다운로드 링크는 페이지에서 매번 파싱 — 파일 갱신 시 ID가 바뀜)
PAGE_URL="https://www.data.go.kr/data/15111389/fileData.do"
PARAMS=$(curl -s "$PAGE_URL" -A "Mozilla/5.0" | grep -oE 'atchFileId=FILE_[0-9]+&fileDetailSn=[0-9]+' | head -1 || true)
if [ -n "$PARAMS" ]; then
  curl -sL "https://www.data.go.kr/cmm/cmm/fileDownload.do?$PARAMS" -A "Mozilla/5.0" -o pet_facilities_new.csv
  # 검증: 10MB 이상 + 헤더에 '시설명' 포함
  SIZE=$(stat -f%z pet_facilities_new.csv 2>/dev/null || echo 0)
  if [ "$SIZE" -gt 10000000 ] && head -1 pet_facilities_new.csv | grep -q "시설명"; then
    mv pet_facilities_new.csv pet_facilities.csv
    log "CSV 최신본 다운로드 성공 ($(( SIZE / 1048576 ))MB)"
  else
    rm -f pet_facilities_new.csv
    log "CSV 다운로드 검증 실패 → 기존 파일 사용"
  fi
else
  log "다운로드 링크 파싱 실패 → 기존 파일 사용"
fi

# 2) 수집·병합 (TourAPI 전량 + CSV → mungtrip.db) — 실패 시 여기서 중단(pipefail)
if ! .venv/bin/python ingest.py pet_facilities.csv 2>&1 | tail -3 | tee -a "$LOG"; then
  log "❌ 수집 실패 — 배포하지 않고 중단 (기존 DB·라이브는 그대로 유지됨)"
  exit 1
fi

# 3) DB 검증 — 1만 건 미만이면 배포 중단(수집 실패 가능성)
COUNT=$(sqlite3 mungtrip.db "SELECT COUNT(*) FROM places")
if [ "$COUNT" -lt 10000 ]; then
  log "❌ DB ${COUNT}건 — 비정상이라 배포 중단"
  exit 1
fi
log "DB 검증 통과: ${COUNT}건"

# 4) 배포 + 라이브 확인
/opt/homebrew/bin/flyctl deploy --ha=false 2>&1 | tail -2 | tee -a "$LOG"
LIVE=$(curl -s "https://mungtrip.projectyoon.com/api/places?size=1" | python3 -c "import json,sys; print(json.load(sys.stdin)['total'])" 2>/dev/null || echo "확인실패")
log "✅ 갱신 완료 — 라이브 총 ${LIVE}건"

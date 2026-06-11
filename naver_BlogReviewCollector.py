"""
naver_BlogReviewCollector.py
================================
네이버 플레이스 블로그 리뷰 수집기 (범용) — GraphQL API 방식

[2026-06 재작성 배경]
    기존 방식(블로그 탭 DOM에서 링크 스크래핑 → 개별 블로그 글 방문)은 동작 불능 상태였음:
      - 블로그 탭은 초기 10개만 SSR로 렌더하고, 이후는 lazy-load(IntersectionObserver)로
        불러오는데 자동화 환경에서 스크롤로 트리거되지 않아 항상 10개에서 멈춤.
      - "더보기" 텍스트 탐지가 가게 헤더 메뉴(place_thumb)를 오탐.
      - 부모 6단계 조상을 카드로 간주해 날짜를 페이지 전체에서 첫 매치로 잘못 추출.

    실측으로 확인한 사실(피탕김탕 31992305 기준):
      - 블로그 리뷰는 POST https://api.place.naver.com/graphql 의 `fsasReviews` 쿼리로 옴.
      - 응답 item에 본문(contents), url, 작성일(createdString), 작성자, 네이버예약여부 등 포함.
      - contents는 ~1300자 상한 프리뷰(짧은 글은 전문). 리뷰 분석에는 충분.
      - maxItemCount(=API가 접근 허용하는 상한, 약 128개)까지만 페이징 가능. total은 수천이어도
        최근순 약 128개만 받을 수 있음 → 18~24개월 목표 수집에는 충분.

    → 그래서 DOM 크롤링을 폐기하고 GraphQL API 페이지네이션으로 전환.
      Playwright는 쿠키/Referer/anti-bot 컨텍스트 확보용으로만 유지하고,
      실제 호출은 page.evaluate() 안의 fetch로 수행(동일 출처 쿠키 자동 첨부, 403 회피).

사용법:
    python naver_BlogReviewCollector.py
    실행 후 음식점 URL(또는 Place ID)과 이름을 입력합니다.

출력 파일 (raw\ 폴더):
    {음식점명}_blog_raw.json  — 분석용 (가중치·제외·플래그 포함)
    {음식점명}_blog_raw.txt   — 사용자 확인용 (본문 수록)

사전 준비:
    pip install playwright
    playwright install chromium

────────────────────────────────────────────────────────────
[수집 목표 및 종료 조건]
  목표: 최신순으로 유효 리뷰(대가성 제외 후) 최소 30개, 최대 100개.

  Phase 1 — 18개월 이내 우선 수집
    - 최신순 순회 중 18개월 컷오프를 처음 넘는 순간 판단:
        유효 ≥ 30 → 즉시 종료(목표 달성). 유효 < 30 → Phase 2 진입.
  Phase 2 — 18~24개월 (신선도 ×0.5)
    - 24개월 컷오프를 넘으면 즉시 종료. 유효 합계 100 도달 시 즉시 종료.
  그 외: API가 주는 maxItemCount까지 모두 소진하면 종료.

[가중치 설계 메모 — 방문자 리뷰와의 차이]
  1. 길이 기반 품질 보너스 없음(블로그는 대부분 장문 + 협찬일수록 길어짐).
     본문이 MIN_BODY_CHARS 미만이면 extraction_warning 플래그(제외 아님).
  2. 대가성 필터 2단계: HARD(자동 제외) / SOFT(플래그만).
     ⚠️ 협찬 고지가 이미지로만 들어간 경우는 텍스트로 탐지 불가.
  3. 신선도: 18개월 이내 ×1.0 / 18~24개월 ×0.5 / 24개월 초과 미수집.
  4. image_count(thumbnailCount)·hashtag_count·has_phone·has_naver_reservation 은
     자동 감점하지 않고 메타데이터/플래그로만 출력(후속 판단에 위임).
────────────────────────────────────────────────────────────
"""

import asyncio
import html
import json
import re
import os
import random
from datetime import datetime, timedelta
from playwright.async_api import async_playwright


# ─────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────

OUTPUT_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw")
os.makedirs(OUTPUT_DIR, exist_ok=True)
_NOW           = datetime.now()
COLLECTED_AT   = _NOW.strftime("%Y-%m-%dT%H:%M:%S+09:00")

MIN_TARGET      = 30    # 유효 리뷰 최소 목표 (미달 시 24개월까지 확장)
MAX_VALID       = 100   # 유효 리뷰 최대 수집 (도달 시 즉시 종료)
MIN_BODY_CHARS  = 300   # 이 미만이면 추출 실패 의심(extraction_warning). '불성실' 아님.

API_URL         = "https://api.place.naver.com/graphql"
API_DISPLAY     = 50    # 한 번에 요청할 리뷰 수 (API 허용 범위)
API_MAX_PAGES   = 30    # 페이지 절대 상한 (maxItemCount로 보통 더 빨리 종료)

# 페이지 사이 지연(초) — 과도한 호출 완화
PAGE_DELAY_MIN  = 0.6
PAGE_DELAY_MAX  = 1.4

# 대가성: 확정 제외(HARD)
PAID_KEYWORDS_HARD = [
    "체험단", "협찬", "원고료", "서포터즈", "기자단",
    "쿠팡 파트너스", "파트너스 활동", "수수료를 제공", "수수료를 지급",
    "무상으로 제공받", "대가를 받고", "유료광고", "유료 광고",
    "소정의 원고료", "소정의 수수료", "광고비를 지원",
]
# 대가성: 의심 플래그(SOFT) — 제외하지 않고 플래그만
PAID_KEYWORDS_SOFT = [
    "제공받아", "제공받았", "제공받은", "소정의",
    "제휴", "협업", "앰버서더", "앰배서더", "광고", "지원받",
]

# GraphQL 쿼리 (page.evaluate 안에서 사용)
GQL_QUERY = (
    "query getFsasReviews($input: FsasReviewsInput){"
    " fsasReviews(input:$input){ total maxItemCount"
    " items{ url home title contents createdString date authorName name"
    " hasNaverReservation bySmartEditor3 thumbnailCount reviewId } } }"
)


# ─────────────────────────────────────────────
# 기간 컷오프
# ─────────────────────────────────────────────

def _months_ago(months: int) -> datetime:
    y, m = divmod(_NOW.month - months, 12)
    if m <= 0:
        m += 12
        y -= 1
    return _NOW.replace(year=_NOW.year + y, month=m, day=min(_NOW.day, 28))

CUTOFF_18M = _months_ago(18)   # 18개월 초과 → 신선도 페널티 ×0.5
CUTOFF_24M = _months_ago(24)   # 24개월 초과 → 수집 중단


# ─────────────────────────────────────────────
# URL 파싱 → Place ID 추출
# ─────────────────────────────────────────────

def parse_input(user_input: str) -> str | None:
    s = user_input.strip()
    if re.fullmatch(r"\d+", s):
        return s
    m = re.search(r"/(?:place|restaurant)/(\d+)", s)
    if m:
        return m.group(1)
    if s.startswith("http"):
        return s   # 단축 URL 등 → 브라우저 리다이렉트로 처리
    return None


# ─────────────────────────────────────────────
# 사용자 입력 인터페이스
# ─────────────────────────────────────────────

def prompt_restaurants() -> list[dict]:
    print("=" * 64)
    print("네이버 플레이스 블로그 리뷰 수집기 (GraphQL API)")
    print(f"실행 시각  : {COLLECTED_AT}")
    print(f"18개월 기준: {CUTOFF_18M.strftime('%Y-%m-%d')} 이후")
    print(f"24개월 기준: {CUTOFF_24M.strftime('%Y-%m-%d')} 이후")
    print(f"수집 목표  : 유효 리뷰 최소 {MIN_TARGET}개 / 최대 {MAX_VALID}개")
    print("=" * 64)
    print()
    print("수집할 음식점 정보를 입력하세요.")
    print("URL 형식: 네이버 플레이스 URL, 단축 URL(naver.me), 또는 Place ID 숫자")
    print("입력 완료 후 빈 줄에서 Enter를 누르면 수집을 시작합니다.")
    print()

    restaurants = []
    idx = 1
    while True:
        print(f"[{idx}번 음식점]")
        while True:
            url_input = input("  URL 또는 Place ID (입력 없이 Enter → 수집 시작): ").strip()
            if not url_input:
                break
            parsed = parse_input(url_input)
            if parsed is None:
                print("  ❌ 인식할 수 없는 형식입니다. 다시 입력해주세요.")
                continue
            place_id_or_url = parsed
            break
        if not url_input:
            break
        while True:
            name = input("  음식점 이름 (파일명에 사용됩니다): ").strip()
            if name:
                break
            print("  ❌ 이름을 입력해주세요.")
        restaurants.append({"place_id_or_url": place_id_or_url, "name": name})
        print(f"  ✅ 추가됨: {name} ({place_id_or_url})")
        print()
        idx += 1

    if not restaurants:
        print("입력된 음식점이 없습니다. 종료합니다.")
    return restaurants


# ─────────────────────────────────────────────
# 날짜 파싱
#   1순위: createdString "26.6.10.수" (YY.M.D.요일)
#   폴백 : "2024년 5월 3일" / "2024.5.3" / "N일 전" (date 필드)
# ─────────────────────────────────────────────

def parse_created_string(s: str) -> datetime | None:
    if not s:
        return None
    m = re.match(r"\s*(\d{2})\.(\d{1,2})\.(\d{1,2})", s.strip())
    if m:
        return _safe_date(2000 + int(m.group(1)), m.group(2), m.group(3))
    return None


def parse_blog_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    s = date_str.strip()

    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
    if m:
        return _safe_date(m.group(1), m.group(2), m.group(3))

    m = re.search(r"(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})", s)
    if m:
        return _safe_date(m.group(1), m.group(2), m.group(3))

    m = re.search(r"(\d+)\s*(일|주|개월|달|년)\s*전", s)
    if m:
        n = int(m.group(1))
        days = {"일": 1, "주": 7, "개월": 30, "달": 30, "년": 365}[m.group(2)] * n
        return _NOW - timedelta(days=days)

    return None


def _safe_date(y, mo, d) -> datetime | None:
    try:
        return datetime(int(y), int(mo), int(d))
    except (ValueError, TypeError):
        return None


def resolve_date(item: dict) -> datetime | None:
    """createdString 우선, 실패 시 date(상대표기) 폴백."""
    return parse_created_string(item.get("createdString", "")) \
        or parse_blog_date(item.get("date", ""))


# ─────────────────────────────────────────────
# blog_id / log_no 추출 (url에서)
# ─────────────────────────────────────────────

def parse_blog_url(url: str) -> tuple[str, str]:
    m = re.search(r"blog\.naver\.com/([^/?]+)/(\d+)", url or "")
    if m:
        return m.group(1), m.group(2)
    return "", ""


# ─────────────────────────────────────────────
# 가중치/필터 계산 (블로그 전용)
# ─────────────────────────────────────────────

def calc_weight(body: str, dt: datetime | None) -> dict:
    char_count = len(re.sub(r"\s", "", body))

    weight = 1.0
    recency_penalty = False
    exclude_reason  = None
    flags           = []

    # 확정 제외: 대가성(HARD)
    for kw in PAID_KEYWORDS_HARD:
        if kw in body:
            exclude_reason = f"paid_hard:{kw}"
            break

    # 플래그: 대가성 의심(SOFT)
    soft_hits = [kw for kw in PAID_KEYWORDS_SOFT if kw in body]
    if soft_hits:
        flags.append("paid_suspect:" + ",".join(soft_hits))

    # 플래그: 본문 추출 의심
    if char_count < MIN_BODY_CHARS:
        flags.append(f"extraction_warning:{char_count}자")

    # 신선도 가중치
    if not exclude_reason and dt and dt < CUTOFF_18M:
        weight *= 0.5
        recency_penalty = True

    return {
        "char_count":      char_count,
        "weight":          round(weight, 2),
        "recency_penalty": recency_penalty,
        "exclude_reason":  exclude_reason,
        "flags":           flags,
    }


# ─────────────────────────────────────────────
# GraphQL API 호출 (page.evaluate 안의 fetch)
# ─────────────────────────────────────────────

async def fetch_page(page, business_id: str, page_no: int) -> dict:
    """fsasReviews 한 페이지 요청 → {total, maxItemCount, items}."""
    js = """
        async ({businessId, pageNo, display, query}) => {
            const body = [{
                operationName: "getFsasReviews",
                variables: { input: {
                    businessId, businessType: "restaurant",
                    buyWithMyMoneyType: false, deviceType: "mobile",
                    display, excludeGdids: [], page: pageNo,
                    query: null, reviewSort: "recent"
                }},
                query
            }];
            const res = await fetch("https://api.place.naver.com/graphql", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body)
            });
            const j = await res.json();
            if (j[0] && j[0].errors) return { error: j[0].errors[0].message };
            const d = j[0] && j[0].data && j[0].data.fsasReviews;
            return d || { error: "no_data" };
        }
    """
    return await page.evaluate(js, {
        "businessId": business_id, "pageNo": page_no,
        "display": API_DISPLAY, "query": GQL_QUERY,
    })


# ─────────────────────────────────────────────
# 전체 수집 오케스트레이션
# ─────────────────────────────────────────────

STOP_TARGET_MET  = "target_met:18개월이내_30개이상"
STOP_MAX_REACHED = "max_reached:유효리뷰_100개도달"
STOP_24M_WALL    = "24m_wall:24개월초과_도달"
STOP_API_END     = "api_end:maxItemCount_소진"


async def collect(place_id_or_url: str, name: str) -> tuple[list[dict], str, str]:
    print(f"\n[{name}] 수집 시작")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 390, "height": 844},
            locale="ko-KR",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        # place_id 확정 (단축 URL이면 리다이렉트로 확인)
        if re.fullmatch(r"\d+", place_id_or_url):
            place_id = place_id_or_url
        else:
            await page.goto(place_id_or_url)
            await page.wait_for_timeout(2000)
            m = re.search(r"/(?:place|restaurant)/(\d+)", page.url)
            place_id = m.group(1) if m else "unknown"

        if place_id == "unknown":
            print("  ❌ Place ID를 확인하지 못했습니다.")
            await context.close(); await browser.close()
            return [], "unknown", STOP_API_END

        # 쿠키/Referer 컨텍스트 확보를 위해 블로그 리뷰 탭을 한 번 방문
        warm_url = f"https://m.place.naver.com/restaurant/{place_id}/review/ugc?reviewSort=recent"
        await page.goto(warm_url)
        await page.wait_for_timeout(2000)

        # API 페이지네이션
        items       = []
        valid_cnt   = 0
        in_phase2   = False
        stop_reason = STOP_API_END
        max_item    = None

        for page_no in range(API_MAX_PAGES):
            data = await fetch_page(page, place_id, page_no)
            if data.get("error"):
                print(f"\n  ⚠️ API 오류(page {page_no}): {data['error']}")
                break
            if max_item is None:
                max_item = data.get("maxItemCount", 0)
                print(f"  API 응답: total {data.get('total')} / "
                      f"접근가능 maxItemCount {max_item}")

            batch = data.get("items", []) or []
            if not batch:
                stop_reason = STOP_API_END
                break

            done = False
            for it in batch:
                dt = resolve_date(it)

                # 18개월 경계 첫 통과 판단
                if dt and not in_phase2 and dt < CUTOFF_18M:
                    in_phase2 = True
                    if valid_cnt >= MIN_TARGET:
                        stop_reason = STOP_TARGET_MET
                        print(f"\n  Phase1 종료: 유효 {valid_cnt}개 ≥ {MIN_TARGET}개")
                        done = True
                        break
                    print(f"\n  Phase2 진입: 유효 {valid_cnt}개 < {MIN_TARGET}개 "
                          f"→ 18~24개월 구간 탐색")

                # 24개월 벽
                if dt and dt < CUTOFF_24M:
                    stop_reason = STOP_24M_WALL
                    print(f"\n  24개월 벽 도달 → 중단 (유효 {valid_cnt}개)")
                    done = True
                    break

                items.append({**it, "_dt": dt})

                is_paid = any(kw in (it.get("contents") or "") for kw in PAID_KEYWORDS_HARD)
                if not is_paid:
                    valid_cnt += 1

                if valid_cnt >= MAX_VALID:
                    stop_reason = STOP_MAX_REACHED
                    print(f"\n  최대 {MAX_VALID}개 도달 → 종료")
                    done = True
                    break

            print(f"  수집 중... 누적 {len(items)}개 / 유효 {valid_cnt}개 "
                  f"({'Phase2' if in_phase2 else 'Phase1'})", end="\r")

            if done:
                break

            # maxItemCount 소진 판단
            if max_item and (page_no + 1) * API_DISPLAY >= max_item:
                stop_reason = STOP_API_END
                print(f"\n  maxItemCount({max_item}) 소진 → 종료")
                break

            await page.wait_for_timeout(
                int(random.uniform(PAGE_DELAY_MIN, PAGE_DELAY_MAX) * 1000)
            )

        print(f"\n  [완료] 수집 {len(items)}개 / 유효 {valid_cnt}개 / 종료사유: {stop_reason}")
        await context.close()
        await browser.close()

    return items, place_id, stop_reason


# ─────────────────────────────────────────────
# JSON 저장 (분석용)
# ─────────────────────────────────────────────

def save_json(name: str, place_id: str, raw: list[dict], stop_reason: str) -> dict:
    reviews  = []
    excluded = []

    for seq, r in enumerate(raw, 1):
        body = html.unescape(r.get("contents") or "")
        dt   = r.get("_dt")
        w    = calc_weight(body, dt)
        blog_id, log_no = parse_blog_url(r.get("url", ""))

        hashtag_count = len(re.findall(r"#\S+", body))
        has_phone     = bool(re.search(r"0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}", body))

        entry = {
            "id":                  seq,
            "blog_id":             blog_id,
            "log_no":              log_no or r.get("reviewId", ""),
            "url":                 r.get("url", ""),
            "date":                dt.strftime("%Y-%m-%d") if dt else "",
            "date_raw":            r.get("createdString") or r.get("date", ""),
            "title":               html.unescape(r.get("title", "")),
            "author":              r.get("authorName") or r.get("name", ""),
            "body":                body,
            "char_count":          w["char_count"],
            "image_count":         int(r.get("thumbnailCount") or 0),
            "hashtag_count":       hashtag_count,
            "has_phone":           has_phone,
            "has_naver_reservation": bool(r.get("hasNaverReservation")),
            "recency_penalty":     w["recency_penalty"],
            "weight":              w["weight"],
            "flags":               w["flags"],
        }
        if w["exclude_reason"]:
            entry["exclude_reason"] = w["exclude_reason"]
            excluded.append(entry)
        else:
            reviews.append(entry)

    count_18m   = sum(1 for r in reviews if not r["recency_penalty"])
    count_18_24 = sum(1 for r in reviews if r["recency_penalty"])
    dates = [r["date"] for r in reviews if re.match(r"\d{4}-\d{2}-\d{2}", r["date"])]

    phase2_entered = stop_reason != STOP_TARGET_MET and count_18_24 > 0
    min_target_met = count_18m >= MIN_TARGET

    result = {
        "restaurant":   name,
        "place_id":     place_id,
        "collected_at": COLLECTED_AT,
        "review_type":  "blog",
        "source":       "place_graphql_fsasReviews",
        "cutoff_18m":   CUTOFF_18M.strftime("%Y-%m-%d"),
        "cutoff_24m":   CUTOFF_24M.strftime("%Y-%m-%d"),
        "stop_reason":  stop_reason,
        "reviews":      reviews,
        "excluded":     excluded,
        "summary": {
            "total_valid":          len(reviews),
            "count_within_18m":     count_18m,
            "count_18m_to_24m":     count_18_24,
            "phase2_entered":       phase2_entered,
            "phase2_reason":        f"18개월 이내 {count_18m}개 < 목표 {MIN_TARGET}개" if phase2_entered else None,
            "min_target_met":       min_target_met,
            "max_target_met":       len(reviews) >= MAX_VALID,
            "total_excluded":       len(excluded),
            "excluded_paid_hard":   sum(1 for e in excluded if e.get("exclude_reason", "").startswith("paid_hard")),
            "flag_paid_suspect":    sum(1 for r in reviews if any(f.startswith("paid_suspect") for f in r["flags"])),
            "flag_extraction_warn": sum(1 for r in reviews if any(f.startswith("extraction_warning") for f in r["flags"])),
            "has_naver_reservation_count": sum(1 for r in reviews if r["has_naver_reservation"]),
            "date_oldest":          min(dates) if dates else "unknown",
            "date_newest":          max(dates) if dates else "unknown",
        },
    }

    path = os.path.join(OUTPUT_DIR, f"{name}_blog_raw.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"  JSON 저장: {path}")
    s = result["summary"]
    print(f"  유효 {s['total_valid']}개 "
          f"(18개월이내 {s['count_within_18m']}개 ×1.0 / "
          f"18~24개월 {s['count_18m_to_24m']}개 ×0.5) / 제외 {s['total_excluded']}개")
    if s["flag_paid_suspect"]:
        print(f"  ⚠️  대가성 의심 플래그(검토 필요): {s['flag_paid_suspect']}개")
    if s["flag_extraction_warn"]:
        print(f"  ⚠️  본문 추출 의심 플래그(점검 필요): {s['flag_extraction_warn']}개")
    return result


# ─────────────────────────────────────────────
# TXT 저장 (사용자 확인용)
# ─────────────────────────────────────────────

def save_txt(name: str, place_id: str, result: dict):
    s = result["summary"]
    lines = []
    lines.append("=" * 64)
    lines.append(f"{name} (Place ID: {place_id}) — 블로그 리뷰 RAW 데이터")
    lines.append(f"수집 일시  : {COLLECTED_AT}")
    lines.append(f"수집 방식  : 네이버 플레이스 GraphQL(fsasReviews)")
    lines.append(f"18개월 기준: {result['cutoff_18m']} 이후")
    lines.append(f"24개월 기준: {result['cutoff_24m']} 이후")
    lines.append(f"종료 사유  : {result['stop_reason']}")
    lines.append("=" * 64)
    lines.append("")
    lines.append("[수집 요약]")
    lines.append(f"  유효 리뷰 수        : {s['total_valid']}개")
    lines.append(f"    - 18개월 이내     : {s['count_within_18m']}개  (가중치 ×1.0)")
    lines.append(f"    - 18~24개월       : {s['count_18m_to_24m']}개  (가중치 ×0.5)")
    lines.append(f"  제외 리뷰 수        : {s['total_excluded']}개")
    lines.append(f"    - 대가성(확정)    : {s['excluded_paid_hard']}개")
    lines.append(f"  ⚠️ 대가성 의심 플래그: {s['flag_paid_suspect']}개 (제외 안 함 / 검토 권장)")
    lines.append(f"  ⚠️ 추출 의심 플래그  : {s['flag_extraction_warn']}개 (본문 점검 권장)")
    lines.append(f"  네이버예약 연동      : {s['has_naver_reservation_count']}개")
    lines.append(f"  수집 기간           : {s['date_oldest']} ~ {s['date_newest']}")

    if s["phase2_entered"]:
        lines.append(f"  ⚠️ Phase2 진입      : {s['phase2_reason']}")
        lines.append(f"     → 18~24개월 구간도 수집됨 (신선도 ×0.5 적용)")
    else:
        lines.append(f"  Phase1 완료         : 18개월 이내 {s['count_within_18m']}개 ≥ {MIN_TARGET}개 달성")

    lines.append(f"  최소 목표({MIN_TARGET}개)      : {'✅ 달성' if s['min_target_met'] else '❌ 미달성 (24개월 이내 리뷰 부족)'}")
    lines.append(f"  최대 수집({MAX_VALID}개)      : {'✅ 도달' if s['max_target_met'] else '미도달'}")
    lines.append("")
    lines.append("  ※ 본문(contents)은 네이버 API 프리뷰(약 1,300자 상한)입니다.")
    lines.append("    짧은 글은 전문, 긴 글은 앞부분만 수록 — 리뷰 분석에는 충분합니다.")
    lines.append("")

    if result["excluded"]:
        lines.append("=" * 64)
        lines.append("[제외된 리뷰 목록]")
        lines.append("=" * 64)
        for r in result["excluded"]:
            lines.append(f"[{r['id']:04d}] {r['author']} | {r['date']} | 제외사유: {r['exclude_reason']}")
            lines.append(f"  제목: {r['title']}")
            lines.append(f"  URL : {r['url']}")
            lines.append("")

    lines.append("=" * 64)
    lines.append(f"[블로그 리뷰 전체 {s['total_valid']}개 — 최신순]")
    lines.append("=" * 64)
    lines.append("")
    for r in result["reviews"]:
        flag_str  = f"  [플래그: {', '.join(r['flags'])}]" if r["flags"] else ""
        phase_tag = " (Phase2 ×0.5)" if r["recency_penalty"] else " (Phase1 ×1.0)"
        nav_tag   = " / 네이버예약" if r["has_naver_reservation"] else ""
        lines.append(f"[{r['id']:04d}] {r['author']} | {r['date']} | 가중치 {r['weight']}{phase_tag}{flag_str}")
        lines.append(f"  제목   : {r['title']}")
        lines.append(f"  URL    : {r['url']}")
        lines.append(f"  메타   : {r['char_count']}자 / 이미지 {r['image_count']}개 / "
                     f"해시태그 {r['hashtag_count']}개 / 전화 {'있음' if r['has_phone'] else '없음'}{nav_tag}")
        lines.append(f"  본문   : {r['body']}")
        lines.append("")

    path = os.path.join(OUTPUT_DIR, f"{name}_blog_raw.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  TXT 저장: {path}")


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

async def main():
    restaurants = prompt_restaurants()
    if not restaurants:
        return

    print()
    print(f"총 {len(restaurants)}개 음식점 수집을 시작합니다.")
    print("=" * 64)

    for r in restaurants:
        raw, place_id, stop_reason = await collect(r["place_id_or_url"], r["name"])
        if not raw:
            print(f"\n❌ [{r['name']}] 수집 실패 또는 결과 없음\n")
            continue
        result = save_json(r["name"], place_id, raw, stop_reason)
        save_txt(r["name"], place_id, result)
        print(f"\n✅ [{r['name']}] 완료\n")

    print("=" * 64)
    print(f"모든 수집 완료. 저장 위치: {OUTPUT_DIR}")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())

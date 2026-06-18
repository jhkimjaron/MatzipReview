"""
kakao_ReviewCollector.py
================================
카카오맵 통합 수집기 (naver_ReviewCollector.py 의 카카오 대응판)

수집 항목:
    1. 매장 기본정보 (영업시간·메뉴·편의시설 등)        ← place_info 필드로 저장
    2. 카카오맵 후기 (별점 ★ + 강점태그 + 작성자 신뢰도)   ← review_type "kakao"
    3. 카카오 블로그 리뷰 (blog.naver.com 연동 글)          ← review_type "blog"

네이버와의 차이 (장점):
    - 리뷰가 DOM이 아니라 **공개 JSON API**로 제공 → 셀렉터 깨질 일 없고 빠름.
    - **로그인·브라우저 불필요** (순수 urllib, 추가 의존성 0).
    - 카카오 후기는 **star_rating(★1~5)** 보유 → 가중 평균 별점 분석 가능.
    - **registered_at(정확한 일시)** 제공 → 시계열 분석 정밀.
    - 작성자 신뢰도(리뷰수·등급) 메타 제공 → 가중치에 반영.

내부 API (place-api.map.kakao.com, 필수 헤더 pf/appversion/Accept/Referer):
    매장정보  : GET /places/panel3/{confirmId}
    후기 메타 : GET /places/reviews/kakaomap/meta/{confirmId}
    후기 목록 : GET /places/tab/reviews/kakaomap/{confirmId}?order=LATEST&only_photo_review=false
                페이지네이션: &previous_last_review_id={직전페이지 마지막 review_id} (20개/페이지, has_next=false까지)
    블로그    : GET /places/tab/reviews/blog/{confirmId}?order=LATEST&page={N}  (10개/페이지)

사용법:
    python kakao_ReviewCollector.py
    실행 후 카카오맵 장소 URL(place.map.kakao.com/{id}) 또는 confirm ID 숫자를 입력.

출력 파일 (raw/ 폴더):
    {음식점명}_kakao_visitor.json / .txt    — 카카오맵 후기 + place_info
    {음식점명}_kakao_blog.json / .txt       — 카카오 블로그 리뷰(전문) + place_info

사전 준비: 없음 (표준 라이브러리만 사용)
"""

import html
import json
import re
import os
import time
import random
import urllib.request
import urllib.error
from datetime import datetime, timedelta


# ─────────────────────────────────────────────
# 공통 상수
# ─────────────────────────────────────────────

_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
JSON_DIR     = os.path.join(_BASE_DIR, "reviews_json")   # 분석 입력용 JSON
TXT_DIR      = os.path.join(_BASE_DIR, "reviews_txt")    # 사람 확인용 TXT
os.makedirs(JSON_DIR, exist_ok=True)
os.makedirs(TXT_DIR, exist_ok=True)
_NOW         = datetime.now()
COLLECTED_AT = _NOW.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _months_ago(months: int) -> datetime:
    y, m = divmod(_NOW.month - months, 12)
    if m <= 0:
        m += 12
        y -= 1
    return _NOW.replace(year=_NOW.year + y, month=m, day=min(_NOW.day, 28))


CUTOFF_3M  = _months_ago(3)
CUTOFF_12M = _months_ago(12)
CUTOFF_24M = _months_ago(24)

API_BASE = "https://place-api.map.kakao.com"
API_HEADERS = {
    "Accept":     "application/json, text/plain, */*",
    "pf":         "PC",
    "appversion": "6.6.0",
    "Referer":    "https://place.map.kakao.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
}

# 강점태그 id→이름 (meta/description 에서 동적 확보, 실패 시 폴백)
STRENGTH_FALLBACK = {5: "맛", 1: "가성비", 2: "친절", 3: "분위기", 4: "주차"}

# ── 카카오 후기 상수 (네이버 방문자와 동일 기준) ──
DEFAULT_K_TARGET = 150   # 카카오 후기 기본 목표(유효 리뷰 기준)
K_MIN_CHARS      = 15    # 공백제외 15자 미만(0자 포함) → 제외
K_MEDIUM_CHARS   = 40    # 공백제외 40~99자 → ×1.2
K_PREMIUM_CHARS  = 100   # 공백제외 100자 이상 → ×1.5
K_PAGE_SIZE      = 20
K_MAX_PAGES      = 120   # 안전 상한 (20×120 = 2400건)
K_PAGE_DELAY     = (0.4, 0.9)

# 카카오 후기는 영수증/방문 인증 기반이라 협찬 본문이 드물지만 방어적으로 스캔
K_PAID_KEYWORDS = ["체험단", "협찬", "원고료", "유료광고", "유료 광고", "광고비"]

# ── 블로그 리뷰 상수 ──
DEFAULT_B_TARGET = 50
B_MIN_BODY_CHARS = 300   # 전문 추출 후에도 이보다 짧으면 추출 의심 경고선
B_PAGE_SIZE      = 10
B_MAX_PAGES      = 60
B_PAGE_DELAY     = (0.5, 1.1)

# 블로그 본문 전문 스크랩 (blog.naver.com 직접 fetch)
B_FULLTEXT_CAP   = 12000      # 전문 본문 저장 상한(자) — 너무 긴 글 방어
B_FULLTEXT_DELAY = (0.4, 0.9) # 블로그 페이지 fetch 사이 지연
BLOG_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                   "Version/16.0 Mobile/15E148 Safari/604.1"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

B_PAID_HARD = [
    "체험단", "협찬", "원고료", "서포터즈", "기자단",
    "쿠팡 파트너스", "파트너스 활동", "수수료를 제공", "수수료를 지급",
    "무상으로 제공받", "대가를 받고", "유료광고", "유료 광고",
    "소정의 원고료", "소정의 수수료", "광고비를 지원",
]
B_PAID_SOFT = [
    "제공받아", "제공받았", "제공받은", "소정의",
    "제휴", "협업", "앰버서더", "앰배서더", "광고", "지원받",
]

K_STOP_TARGET_MET = "target_met:유효리뷰_목표도달"
K_STOP_24M_WALL   = "24m_wall:24개월초과_도달"
K_STOP_API_END    = "api_end:마지막페이지_도달"


# ─────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────

def _get_json(url: str, retries: int = 3):
    """카카오 내부 API GET → JSON. 일시 오류는 재시도."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=API_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError) as e:
            last_err = e
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"API 호출 실패 ({retries}회): {url}\n  사유: {last_err}")


def parse_input(user_input: str) -> str | None:
    """입력에서 카카오 confirm_id 추출. 숫자 / place URL 지원."""
    s = user_input.strip()
    if re.fullmatch(r"\d+", s):
        return s
    # place.map.kakao.com/16421356  또는  map.kakao.com/.../16421356
    m = re.search(r"place\.map\.kakao\.com/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&](?:itemId|confirmId)=(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"/(\d{5,})", s)        # 마지막 보루: 5자리 이상 숫자 경로
    if m:
        return m.group(1)
    return None


def _safe_date(y, mo, d) -> datetime | None:
    try:
        return datetime(int(y), int(mo), int(d))
    except (ValueError, TypeError):
        return None


def _parse_dt(s: str) -> datetime | None:
    """'2026-06-17 11:09:00' / '2026-05-12 22:34:00' → datetime(date)."""
    if not s:
        return None
    m = re.match(r"(\d{4})[-.](\d{1,2})[-.](\d{1,2})", s.strip())
    return _safe_date(m.group(1), m.group(2), m.group(3)) if m else None


# ─────────────────────────────────────────────
# 사용자 입력 인터페이스
# ─────────────────────────────────────────────

def prompt_restaurants() -> list[dict]:
    print("=" * 64)
    print("카카오맵 통합 수집기")
    print(f"실행 시각  : {COLLECTED_AT}")
    print("가중치 기준: 최근 3개월 ×1.3 / 12~24개월 ×0.5 / 24개월 초과 제외")
    print("=" * 64)
    print()
    print("수집할 음식점 정보를 입력하세요.")
    print("입력 형식: 카카오맵 장소 URL(place.map.kakao.com/숫자) 또는 confirm ID 숫자")
    print("입력 완료 후 빈 줄에서 Enter를 누르면 수집을 시작합니다.")
    print()

    restaurants = []
    idx = 1
    while True:
        print(f"[{idx}번 음식점]")
        while True:
            url_input = input("  URL 또는 confirm ID (입력 없이 Enter → 수집 시작): ").strip()
            if not url_input:
                break
            parsed = parse_input(url_input)
            if parsed is None:
                print("  ❌ 인식할 수 없는 형식입니다. 다시 입력해주세요.")
                continue
            confirm_id = parsed
            break
        if not url_input:
            break
        while True:
            name = input("  음식점 이름 (파일명에 사용됩니다): ").strip()
            if name:
                break
            print("  ❌ 이름을 입력해주세요.")
        restaurants.append({"confirm_id": confirm_id, "name": name})
        print(f"  ✅ 추가됨: {name} (confirm_id={confirm_id})")
        print()
        idx += 1

    if not restaurants:
        print("입력된 음식점이 없습니다. 종료합니다.")
    return restaurants


def prompt_mode() -> str:
    print()
    print("수집 모드를 선택하세요:")
    print("  1. 전체 (매장정보 + 카카오 후기 + 블로그 리뷰)")
    print("  2. 카카오 후기만 (매장정보 포함)")
    print("  3. 블로그 리뷰만  (매장정보 포함)")
    while True:
        choice = input("선택 [1/2/3] (기본값 1): ").strip()
        if choice in ("", "1"):
            return "all"
        if choice == "2":
            return "kakao"
        if choice == "3":
            return "blog"
        print("  ❌ 1, 2, 3 중 하나를 입력해주세요.")


def _ask_count(label: str, default: int) -> int:
    while True:
        raw = input(f"  {label} (기본 {default}개, Enter=기본값): ").strip()
        if not raw:
            return default
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        print("  ❌ 1 이상의 숫자를 입력해주세요.")


def prompt_review_counts(mode: str) -> tuple[int, int]:
    """수집할 유효(분석 대상) 리뷰 개수를 한 번만 입력받아 모든 음식점에 동일 적용."""
    print()
    print("─" * 64)
    print(f"기본 수집 리뷰 수는 카카오 후기 {DEFAULT_K_TARGET}개, 블로그 {DEFAULT_B_TARGET}개입니다.")
    print("※ 입력 개수 = 광고·불성실 등 '제외 대상을 뺀 유효 리뷰' 기준입니다.")
    print("※ 여러 음식점을 수집할 경우 모두 같은 개수로 수집됩니다.")
    k_target = DEFAULT_K_TARGET
    b_target = DEFAULT_B_TARGET
    if mode in ("all", "kakao"):
        k_target = _ask_count("카카오 후기 개수", DEFAULT_K_TARGET)
    if mode in ("all", "blog"):
        b_target = _ask_count("블로그 리뷰 개수", DEFAULT_B_TARGET)
    print(f"  → 카카오 후기 {k_target}개 / 블로그 {b_target}개 (유효 리뷰 기준)로 수집합니다.")
    print("─" * 64)
    return k_target, b_target


# ─────────────────────────────────────────────
# ① 매장 기본정보 수집 (panel3)
# ─────────────────────────────────────────────

def _parse_open_hours(open_hours: dict, result: dict):
    """open_hours.all.periods[].days[] → hours/break_time/regular_holiday."""
    if not isinstance(open_hours, dict):
        return
    all_block = open_hours.get("all") or {}
    raw_lines = []

    for period in (all_block.get("periods") or []):
        title = period.get("period_title", "")
        for day in (period.get("days") or []):
            dow = day.get("day_of_the_week", "")
            on  = day.get("on_days") or {}
            desc = on.get("start_end_time_desc", "")
            raw_lines.append(f"{dow} {desc}".strip())
            # "11:00 ~ 21:00" 형태
            times = re.findall(r"(\d{1,2}:\d{2})\s*[~\-]\s*(\d{1,2}:\d{2})", desc)
            if times:
                result["hours"].append({
                    "day": dow, "open": times[0][0], "close": times[0][1],
                })
            # 브레이크타임 표기가 별도 키로 올 경우 대비
            bt = on.get("break_time_desc") or ""
            mbt = re.search(r"(\d{1,2}:\d{2})\s*[~\-]\s*(\d{1,2}:\d{2})", bt)
            if mbt and not result["break_time"]:
                result["break_time"] = f"{mbt.group(1)} - {mbt.group(2)}"
        if title and title not in (result["raw_hours_text"] or ""):
            pass

    off = all_block.get("all_days_off_info")
    if off:
        result["regular_holiday"] = off
    if raw_lines:
        result["raw_hours_text"] = "\n".join(raw_lines)


def collect_place_info(confirm_id: str, name: str) -> dict:
    print("  ▷ 매장정보 수집 중...", end="\r")

    result = {
        "confirm_id":            confirm_id,
        "collected_at":          COLLECTED_AT,
        "name":                  name,
        "category":              None,
        "address":               None,
        "phone":                 None,
        "homepage":              None,
        "hours":                 [],
        "break_time":            None,
        "last_order":            None,
        "regular_holiday":       None,
        "parking":               None,
        "facilities":            [],
        "kakao_booking":         False,
        "average_score":         None,   # 카카오 후기 평균 별점 (카카오 고유)
        "total_kakao_reviews":   None,
        "total_blog_reviews":    None,
        "menus":                 [],
        "raw_hours_text":        None,
        "errors":                [],
    }

    try:
        panel = _get_json(f"{API_BASE}/places/panel3/{confirm_id}")
    except Exception as e:
        result["errors"].append(f"panel3: {e}")
        print(f"  ⚠️ 매장정보 수집 실패: {e}")
        return result

    # ── summary: 카테고리/주소/전화 ──
    try:
        summ = panel.get("summary") or {}
        cat = summ.get("category") or {}
        cat_path = " > ".join([p for p in (cat.get("name1"), cat.get("name2"), cat.get("name3")) if p])
        result["category"] = cat.get("name") or (cat_path or None)
        if cat_path and result["category"] and result["category"] not in cat_path:
            result["category"] = f"{cat_path} ({result['category']})"
        elif cat_path:
            result["category"] = cat_path

        addr = summ.get("address") or {}
        result["address"] = addr.get("road") or addr.get("disp") or addr.get("jibun")

        phones = summ.get("phone_numbers") or []
        if phones and isinstance(phones, list):
            result["phone"] = phones[0].get("tel")

        result["homepage"] = summ.get("homepage") or summ.get("url") or None
    except Exception as e:
        result["errors"].append(f"summary: {e}")

    # ── 영업시간 ──
    try:
        _parse_open_hours(panel.get("open_hours") or {}, result)
    except Exception as e:
        result["errors"].append(f"open_hours: {e}")

    # ── 편의시설 / 주차 ──
    try:
        add = panel.get("place_add_info") or {}
        facs = list(add.get("tags") or [])
        flag = add.get("facilities") or {}
        flag_map = {
            "is_parking": "주차", "is_pet": "반려동물 동반",
            "is_fordisabled": "장애인 편의", "is_kidzone": "키즈존",
        }
        for k, label in flag_map.items():
            if flag.get(k):
                facs.append(label)
        # ai_mate 시설 아이콘 텍스트
        ai = (add.get("ai_mate") or {}).get("store_facility_icons") or []
        for ic in ai:
            t = ic.get("text")
            if t and t not in facs:
                facs.append(t)
        # 중복 제거 (순서 유지)
        seen = set()
        result["facilities"] = [f for f in facs if not (f in seen or seen.add(f))]

        spi = add.get("simple_parking_infos") or {}
        if spi.get("summary"):
            result["parking"] = spi["summary"]
        elif flag.get("is_parking"):
            result["parking"] = "주차 가능"
    except Exception as e:
        result["errors"].append(f"place_add_info: {e}")

    # ── 메뉴 ──
    try:
        menu_block = ((panel.get("menu") or {}).get("menus") or {})
        items = menu_block.get("items") or []
        menus = []
        for it in items:
            mname = (it.get("name") or "").strip()
            if not mname:
                continue
            price = it.get("price")
            price_str = f"{int(price):,}원" if isinstance(price, (int, float)) and price else None
            desc = "추천메뉴" if it.get("is_recommend") else None
            menus.append({"name": mname, "price": price_str, "description": desc})
        result["menus"] = menus[:60]
    except Exception as e:
        result["errors"].append(f"menu: {e}")

    # ── 리뷰 총수 / 평균별점 / 예약 ──
    try:
        kr = (panel.get("kakaomap_review") or {}).get("score_set") or {}
        result["total_kakao_reviews"] = kr.get("review_count")
        result["average_score"]       = kr.get("average_score")
        br = panel.get("blog_review") or {}
        result["total_blog_reviews"]  = br.get("review_count")
        # 카카오 예약 버튼 존재 여부 (있으면 표기)
        flat = json.dumps(panel, ensure_ascii=False)
        result["kakao_booking"] = bool(re.search(r"예약|booking", flat)) and "reservation" in flat.lower()
    except Exception as e:
        result["errors"].append(f"review_meta: {e}")

    miss = [k for k in ("address", "hours", "menus") if not result.get(k)]
    miss_note = f"  (누락: {', '.join(miss)})" if miss else ""
    print(f"  ✓ 매장정보       메뉴 {len(result['menus'])}개 · 영업시간 {len(result['hours'])}일 · "
          f"전체평균 ★{result['average_score']}{miss_note}")
    return result


def format_place_info_txt(info: dict) -> list[str]:
    lines = []
    lines.append("=" * 64)
    lines.append("[매장 기본정보]")
    lines.append("=" * 64)
    lines.append(f"  카테고리   : {info.get('category') or '(미확인)'}")
    lines.append(f"  주소       : {info.get('address')  or '(미확인)'}")
    lines.append(f"  전화번호   : {info.get('phone')    or '(미확인)'}")
    lines.append(f"  홈페이지   : {info.get('homepage') or '(미확인)'}")
    lines.append(f"  평균 별점  : {('★' + str(info['average_score'])) if info.get('average_score') is not None else '(미확인)'}")
    lines.append(f"  정기휴무   : {info.get('regular_holiday') or '(미확인)'}")
    lines.append(f"  브레이크   : {info.get('break_time') or '(미확인)'}")
    lines.append(f"  주차       : {info.get('parking') or '(미확인)'}")
    tk = info.get('total_kakao_reviews')
    tb = info.get('total_blog_reviews')
    lines.append(f"  카카오후기 : 총 {tk:,}개" if tk else "  카카오후기 : (미확인)")
    lines.append(f"  블로그리뷰 : 총 {tb:,}개" if tb else "  블로그리뷰 : (미확인)")

    if info.get("hours"):
        lines.append("  영업시간   :")
        for h in info["hours"]:
            lines.append(f"    {h['day']:<8} {h['open']} - {h['close']}")
    elif info.get("raw_hours_text"):
        lines.append(f"  영업시간(원문): {info['raw_hours_text'][:120]}")

    if info.get("facilities"):
        lines.append(f"  편의시설   : {' / '.join(info['facilities'])}")

    if info.get("menus"):
        lines.append("")
        lines.append("  [메뉴·가격]")
        for m in info["menus"]:
            price = f" — {m['price']}" if m.get("price") else ""
            desc  = f"  ({m['description']})" if m.get("description") else ""
            lines.append(f"    {m['name']}{price}{desc}")

    if info.get("errors"):
        lines.append("")
        lines.append(f"  ⚠️ 수집 오류: {'; '.join(info['errors'])}")
    lines.append("")
    return lines


# ─────────────────────────────────────────────
# ② 카카오 후기 (별점 + 강점태그 + 작성자 신뢰도)
# ─────────────────────────────────────────────

def fetch_strength_map(confirm_id: str) -> dict:
    """meta API 에서 강점태그 id→이름 매핑 확보 (실패 시 폴백)."""
    try:
        meta = _get_json(f"{API_BASE}/places/reviews/kakaomap/meta/{confirm_id}")
        desc = meta.get("strength_description") or []
        m = {d["id"]: d["name"] for d in desc if "id" in d and "name" in d}
        return m or dict(STRENGTH_FALLBACK)
    except Exception:
        return dict(STRENGTH_FALLBACK)


def _calc_weight_kakao(content: str, dt: datetime | None) -> dict:
    """카카오 후기 가중치 — 네이버 방문자와 동일 기준 (별점은 rating으로 분리).

    - 신선도 : 3개월 이내 ×1.3 / 12~24개월 ×0.5 / 24개월 초과 제외
    - 길이   : 공백제외 15자 미만 제외 / 40~99자 ×1.2 / 100자 이상 ×1.5
    - 협찬   : 키워드 포함 → 제외
    """
    char_count = len(re.sub(r"\s", "", content))   # 공백제외 기준
    weight          = 1.0
    recency_bonus   = False
    recency_penalty = False
    quality_bonus   = False
    exclude_reason  = None

    for kw in K_PAID_KEYWORDS:
        if kw in content:
            exclude_reason = f"paid:{kw}"
            break

    if not exclude_reason and char_count < K_MIN_CHARS:
        exclude_reason = "insincere"

    if not exclude_reason and dt and dt < CUTOFF_24M:
        exclude_reason = "too_old:24개월_초과"

    if not exclude_reason:
        if dt:
            if dt >= CUTOFF_3M:
                weight        *= 1.3
                recency_bonus  = True
            elif dt < CUTOFF_12M:
                weight        *= 0.5
                recency_penalty = True
        if char_count >= K_PREMIUM_CHARS:
            weight        *= 1.5
            quality_bonus  = True
        elif char_count >= K_MEDIUM_CHARS:
            weight        *= 1.2
            quality_bonus  = True

    return {
        "char_count":      char_count,
        "weight":          round(weight, 2),
        "recency_bonus":   recency_bonus,
        "recency_penalty": recency_penalty,
        "quality_bonus":   quality_bonus,
        "exclude_reason":  exclude_reason,
    }


def collect_kakao_reviews(confirm_id: str, name: str, target: int) -> tuple[list, str, dict]:
    base = (f"{API_BASE}/places/tab/reviews/kakaomap/{confirm_id}"
            f"?order=LATEST&only_photo_review=false")
    print(f"  ▷ 카카오 후기 (목표 {target}개)")

    items       = []
    seen_ids    = set()
    valid_cnt   = 0
    prev_last   = None
    stop_reason = K_STOP_API_END
    score_set   = {}

    for page_no in range(K_MAX_PAGES):
        url = base + (f"&previous_last_review_id={prev_last}" if prev_last else "")
        try:
            data = _get_json(url)
        except Exception as e:
            print(f"\n  ⚠️ API 오류(page {page_no}): {e}")
            break

        if not score_set:
            score_set = data.get("score_set") or {}

        batch = data.get("reviews") or []
        if not batch:
            break

        new_in_batch = 0
        done = False
        for it in batch:
            rid = it.get("review_id")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            new_in_batch += 1

            dt = _parse_dt(it.get("registered_at", ""))
            if dt and dt < CUTOFF_24M:
                stop_reason = K_STOP_24M_WALL
                print(f"\n  24개월 벽 도달 → 중단 (유효 {valid_cnt}개 / 목표 {target}개)")
                done = True
                break

            items.append(it)

            content = it.get("contents") or ""
            is_paid = any(kw in content for kw in K_PAID_KEYWORDS)
            too_short = len(re.sub(r"\s", "", content)) < K_MIN_CHARS   # 공백제외 15자 미만 → 제외
            if not is_paid and not too_short:
                valid_cnt += 1

            if valid_cnt >= target:
                stop_reason = K_STOP_TARGET_MET
                print(f"\n  목표 달성: 유효 {valid_cnt}개 ≥ {target}개 → 종료")
                done = True
                break

        print(f"  수집 중... 누적 {len(items)}개 / 유효 {valid_cnt}개 (목표 {target})", end="\r")

        if done:
            break
        if new_in_batch == 0:        # 진행 없음 (중복만) → 무한루프 방지
            break

        prev_last = batch[-1].get("review_id")
        if not data.get("has_next"):
            stop_reason = K_STOP_API_END
            print(f"\n  마지막 페이지 도달 → 종료 (유효 {valid_cnt}개)")
            break

        time.sleep(random.uniform(*K_PAGE_DELAY))

    return items, stop_reason, score_set


def save_kakao_json(name: str, confirm_id: str, raw: list, stop_reason: str,
                    place_info: dict, score_set: dict,
                    strength_map: dict, target: int) -> dict:
    reviews  = []
    excluded = []

    for seq, r in enumerate(raw, 1):
        content = (r.get("contents") or "").strip()
        dt      = _parse_dt(r.get("registered_at", ""))
        photo_count  = int(r.get("photo_count") or 0)
        strength_ids = r.get("strength_ids") or []
        owner = (r.get("meta") or {}).get("owner") or {}
        like_count = (r.get("meta") or {}).get("like_count", 0)
        owner_pick = (r.get("meta") or {}).get("is_place_owner_pick", False)

        w = _calc_weight_kakao(content, dt)
        strength_names = [strength_map.get(sid, str(sid)) for sid in strength_ids]

        entry = {
            "id":              seq,
            "review_id":       r.get("review_id"),
            "author":          owner.get("nickname", ""),
            "date":            dt.strftime("%Y-%m-%d") if dt else "",
            "date_raw":        r.get("registered_at", ""),
            "content":         content,
            "char_count":      w["char_count"],
            "rating":          r.get("star_rating"),       # ★1~5 (네이버 방문자는 항상 null)
            "strength_ids":    strength_ids,
            "keywords":        strength_names,             # analyze_reviews 재사용 (가중 빈도)
            "tags":            strength_names,
            "photo_count":     photo_count,
            "like_count":      like_count,
            "is_owner_pick":   owner_pick,
            "recency_bonus":   w["recency_bonus"],
            "recency_penalty": w["recency_penalty"],
            "quality_bonus":   w["quality_bonus"],
            "weight":          w["weight"],
        }
        if w["exclude_reason"]:
            entry["exclude_reason"] = w["exclude_reason"]
            excluded.append(entry)
        else:
            reviews.append(entry)

    over = max(0, len(reviews) - target)
    if over:
        reviews = reviews[:target]

    count_within_3m  = sum(1 for r in reviews if r["recency_bonus"])
    count_3m_to_12m  = sum(1 for r in reviews if not r["recency_bonus"] and not r["recency_penalty"])
    count_12m_to_24m = sum(1 for r in reviews if r["recency_penalty"])
    dates = [r["date"] for r in reviews if re.match(r"\d{4}-\d{2}-\d{2}", r["date"])]

    # 별점 분포
    rating_dist = {str(s): sum(1 for r in reviews if r["rating"] == s) for s in range(1, 6)}

    result = {
        "restaurant":   name,
        "place_id":     confirm_id,
        "confirm_id":   confirm_id,
        "collected_at": COLLECTED_AT,
        "review_type":  "kakao",
        "source":       "place_api_kakaomap_reviews",
        "cutoff_3m":    CUTOFF_3M.strftime("%Y-%m-%d"),
        "cutoff_12m":   CUTOFF_12M.strftime("%Y-%m-%d"),
        "cutoff_24m":   CUTOFF_24M.strftime("%Y-%m-%d"),
        "stop_reason":  stop_reason,
        "place_info":   place_info,
        "reviews":      reviews,
        "excluded":     excluded,
        "summary": {
            "target":                target,
            "total_kakao_count":     score_set.get("review_count") or place_info.get("total_kakao_reviews"),
            "average_score_overall": score_set.get("average_score") or place_info.get("average_score"),
            "total_collected":       len(reviews),
            "total_loaded":          len(reviews) + len(excluded) + over,
            "count_within_3m":       count_within_3m,
            "count_3m_to_12m":       count_3m_to_12m,
            "count_12m_to_24m":      count_12m_to_24m,
            "recency_bonus_count":   count_within_3m,
            "recency_penalty_count": count_12m_to_24m,          # analyze_reviews 신뢰도 메모용
            "quality_bonus_count":   sum(1 for r in reviews if r["quality_bonus"]),
            "rating_distribution":   rating_dist,
            "avg_rating_collected":  round(sum(r["rating"] for r in reviews if r["rating"]) /
                                           max(1, sum(1 for r in reviews if r["rating"])), 2) if reviews else None,
            "total_excluded":        len(excluded),
            "excluded_paid":         sum(1 for e in excluded if e.get("exclude_reason", "").startswith("paid")),
            "excluded_insincere":    sum(1 for e in excluded if e.get("exclude_reason") == "insincere"),
            "excluded_too_old":      sum(1 for e in excluded if e.get("exclude_reason", "").startswith("too_old")),
            "date_oldest":           min(dates) if dates else "unknown",
            "date_newest":           max(dates) if dates else "unknown",
            "target_met":            len(reviews) >= target,
        },
    }

    path = os.path.join(JSON_DIR, f"{name}_kakao_visitor.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    s = result["summary"]
    print(f"  ✓ 카카오 유효 {s['total_collected']}개   "
          f"최근3개월 {s['count_within_3m']} · 12~24개월 {s['count_12m_to_24m']} · 제외 {s['total_excluded']} · "
          f"수집평균 ★{s['avg_rating_collected']}")
    return result


def save_kakao_txt(name: str, confirm_id: str, result: dict):
    s = result["summary"]
    lines = []
    lines.extend(format_place_info_txt(result.get("place_info", {})))

    lines.append("=" * 64)
    lines.append(f"{name} (confirm_id: {confirm_id}) — 카카오 후기 RAW 데이터")
    lines.append(f"수집 일시  : {COLLECTED_AT}")
    lines.append(f"수집 방식  : 카카오 place-api (tab/reviews/kakaomap, order=LATEST)")
    lines.append(f"3개월 기준 : {result['cutoff_3m']} 이후 (×1.3)")
    lines.append(f"12개월 기준: {result['cutoff_12m']} 이전 (×0.5)")
    lines.append(f"24개월 기준: {result['cutoff_24m']} 이전 (제외)")
    lines.append(f"종료 사유  : {result['stop_reason']}")
    lines.append("=" * 64)
    lines.append("")
    lines.append("[수집 요약]")
    tk = s.get("total_kakao_count")
    lines.append(f"  카카오 전체 후기  : {tk:,}개" if tk else "  카카오 전체 후기  : (확인불가)")
    lines.append(f"  전체 평균 별점    : ★{s.get('average_score_overall')}")
    lines.append(f"  수집 유효 후기    : {s['total_collected']}개  (분석 대상)")
    lines.append(f"    - 3개월 이내    : {s['count_within_3m']}개  (가중치 ×1.3)")
    lines.append(f"    - 3~12개월      : {s['count_3m_to_12m']}개  (가중치 ×1.0)")
    lines.append(f"    - 12~24개월     : {s['count_12m_to_24m']}개  (가중치 ×0.5)")
    lines.append(f"  수집분 평균 별점  : ★{s['avg_rating_collected']}")
    lines.append(f"  별점 분포         : " +
                 " / ".join(f"★{k} {v}개" for k, v in sorted(s['rating_distribution'].items(), reverse=True)))
    lines.append(f"  40~99자(×1.2)     : {s['quality_bonus_count'] - sum(1 for r in result['reviews'] if r['char_count'] >= K_PREMIUM_CHARS)}개")
    lines.append(f"  100자이상(×1.5)   : {sum(1 for r in result['reviews'] if r['char_count'] >= K_PREMIUM_CHARS)}개")
    lines.append(f"  제외 리뷰 수      : {s['total_excluded']}개")
    lines.append(f"    - 대가성        : {s['excluded_paid']}개")
    lines.append(f"    - 15자미만      : {s['excluded_insincere']}개")
    lines.append(f"    - 24개월 초과   : {s['excluded_too_old']}개")
    lines.append(f"  수집 기간         : {s['date_oldest']} ~ {s['date_newest']}")
    lines.append(f"  목표({s['target']}개)       : {'✅ 달성' if s['target_met'] else '❌ 미달성'}")
    lines.append("")

    lines.append("=" * 64)
    lines.append(f"[카카오 후기 전체 {s['total_collected']}개 — 최신순]")
    lines.append("=" * 64)
    lines.append("")
    for r in result["reviews"]:
        wi = []
        if r.get("recency_bonus"):  wi.append("3개월이내×1.3")
        if r["recency_penalty"]:    wi.append("12~24개월×0.5")
        if r["quality_bonus"]:
            tier = "100자이상×1.5" if r["char_count"] >= K_PREMIUM_CHARS else "40~99자×1.2"
            wi.append(tier)
        wstr = f"  [가중치:{r['weight']}" + (f" / {', '.join(wi)}]" if wi else "]")
        lines.append(f"[{r['id']:04d}] {r['author']} | {r['date']} | ★{r['rating']}{wstr}")
        if r["keywords"]:
            lines.append(f"  강점태그: {', '.join(r['keywords'])}")
        meta_bits = [f"사진 {r['photo_count']}개", f"좋아요 {r['like_count']}"]
        if r["is_owner_pick"]:
            meta_bits.append("사장님픽")
        lines.append(f"  메타   : {' / '.join(str(b) for b in meta_bits)}")
        lines.append(f"  본문   : {r['content']}")
        lines.append("")

    path = os.path.join(TXT_DIR, f"{name}_kakao_visitor.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ─────────────────────────────────────────────
# ③ 카카오 블로그 리뷰 (blog.naver.com 연동)
# ─────────────────────────────────────────────

def _parse_blog_url(url: str) -> tuple[str, str]:
    m = re.search(r"blog\.naver\.com/([^/?]+)/(\d+)", url or "")
    if m:
        return m.group(1), m.group(2)
    return "", ""


def _strip_html(fragment: str) -> str:
    """HTML 조각 → 평문. 블록 태그는 줄바꿈으로, 나머지 태그는 제거."""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", fragment)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|blockquote)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)            # 남은 태그 제거
    s = html.unescape(s)
    s = s.replace("​", "")              # zero-width space (네이버 본문에 다수)
    s = re.sub(r"[ \t ]+", " ", s)       # 가로 공백 압축
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)   # 빈 줄 3개↑ → 2개
    return s.strip()


def fetch_blog_fulltext(blog_id: str, log_no: str) -> str | None:
    """blog.naver.com 모바일 페이지에서 본문 전문을 추출. 실패 시 None.

    네이버 블로그 본문 컨테이너:
      - 스마트에디터 ONE: <div class="se-main-container"> ... </div>
      - 구버전        : <div id="postViewArea"> ... </div>
    위 컨테이너가 안 잡히면 None을 반환해 호출부가 API 프리뷰로 폴백한다.
    """
    if not blog_id or not log_no:
        return None
    url = f"https://m.blog.naver.com/{blog_id}/{log_no}"
    try:
        req = urllib.request.Request(url, headers=BLOG_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            page = resp.read().decode(charset, errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, ValueError):
        return None

    # 1) 스마트에디터 ONE: se-text-paragraph 문단을 모아 붙인다(가장 정확)
    paras = re.findall(
        r'(?is)<p[^>]*class="[^"]*se-text-paragraph[^"]*"[^>]*>(.*?)</p>', page)
    if paras:
        text = _strip_html("\n".join(paras))
        if len(re.sub(r"\s", "", text)) >= 50:
            return text[:B_FULLTEXT_CAP]

    # 2) se-main-container 통째로 추출 → 태그 제거
    m = re.search(r'(?is)<div class="se-main-container">(.*?)</div>\s*'
                  r'<div class="(?:se_paragraph|blog_footer|post_footer)', page)
    if not m:
        m = re.search(r'(?is)<div class="se-main-container">(.*)', page)
    if m:
        text = _strip_html(m.group(1))
        if len(re.sub(r"\s", "", text)) >= 50:
            return text[:B_FULLTEXT_CAP]

    # 3) 구버전 postViewArea
    m = re.search(r'(?is)<div[^>]+id="postViewArea"[^>]*>(.*?)<!--\s*//', page)
    if not m:
        m = re.search(r'(?is)<div[^>]+id="postViewArea"[^>]*>(.*)', page)
    if m:
        text = _strip_html(m.group(1))
        if len(re.sub(r"\s", "", text)) >= 50:
            return text[:B_FULLTEXT_CAP]

    return None


def _calc_weight_blog(body: str, dt: datetime | None) -> dict:
    """블로그 가중치 — 신선도는 방문자 리뷰와 동일 기준.

    - 신선도 : 3개월 이내 ×1.3 / 12~24개월 ×0.5 / 24개월 초과 제외
    - 협찬   : 확정 키워드 → 제외, 의심 키워드 → 플래그(제외 안 함)
    - 길이   : 보너스 없음. 300자 미만은 추출 의심 플래그만.
    """
    char_count = len(re.sub(r"\s", "", body))
    weight          = 1.0
    recency_bonus   = False
    recency_penalty = False
    exclude_reason  = None
    flags           = []

    for kw in B_PAID_HARD:
        if kw in body:
            exclude_reason = f"paid_hard:{kw}"
            break

    soft_hits = [kw for kw in B_PAID_SOFT if kw in body]
    if soft_hits:
        flags.append("paid_suspect:" + ",".join(soft_hits))

    if char_count < B_MIN_BODY_CHARS:
        flags.append(f"extraction_warning:{char_count}자")

    if not exclude_reason and dt and dt < CUTOFF_24M:
        exclude_reason = "too_old:24개월_초과"

    if not exclude_reason and dt:
        if dt >= CUTOFF_3M:
            weight        *= 1.3
            recency_bonus  = True
        elif dt < CUTOFF_12M:
            weight        *= 0.5
            recency_penalty = True

    return {
        "char_count":      char_count,
        "weight":          round(weight, 2),
        "recency_bonus":   recency_bonus,
        "recency_penalty": recency_penalty,
        "exclude_reason":  exclude_reason,
        "flags":           flags,
    }


def collect_blog(confirm_id: str, name: str, target: int) -> tuple[list, str, int]:
    base = f"{API_BASE}/places/tab/reviews/blog/{confirm_id}?order=LATEST"
    print(f"  ▷ 블로그 리뷰 (목표 {target}개)")

    items       = []
    seen        = set()
    valid_cnt   = 0
    stop_reason = K_STOP_API_END
    total_count = None

    for page_no in range(1, B_MAX_PAGES + 1):
        try:
            data = _get_json(base + f"&page={page_no}")
        except Exception as e:
            print(f"\n  ⚠️ API 오류(page {page_no}): {e}")
            break

        if total_count is None:
            total_count = data.get("review_count")
        batch = data.get("reviews") or []
        if not batch:
            break

        done = False
        for it in batch:
            rid = it.get("review_id")
            if rid in seen:
                continue
            seen.add(rid)

            dt = _parse_dt(it.get("registered_at", ""))
            if dt and dt < CUTOFF_24M:
                stop_reason = K_STOP_24M_WALL
                print(f"\n  24개월 벽 도달 → 중단 (유효 {valid_cnt}개 / 목표 {target}개)")
                done = True
                break

            items.append(it)
            body = html.unescape(it.get("contents") or "")
            if not any(kw in body for kw in B_PAID_HARD):
                valid_cnt += 1

            if valid_cnt >= target:
                stop_reason = K_STOP_TARGET_MET
                print(f"\n  목표 달성: 유효 {valid_cnt}개 ≥ {target}개 → 종료")
                done = True
                break

        print(f"  수집 중... 누적 {len(items)}개 / 유효 {valid_cnt}개 (목표 {target})", end="\r")

        if done:
            break
        if len(batch) < B_PAGE_SIZE:     # 마지막 페이지
            stop_reason = K_STOP_API_END
            print(f"\n  마지막 페이지 도달 → 종료 (유효 {valid_cnt}개)")
            break

        time.sleep(random.uniform(*B_PAGE_DELAY))

    return items, stop_reason, total_count


def save_blog_json(name: str, confirm_id: str, raw: list, stop_reason: str,
                   place_info: dict, total_count: int | None, target: int) -> dict:
    reviews  = []
    excluded = []

    total = len(raw)
    for seq, r in enumerate(raw, 1):
        dt      = _parse_dt(r.get("registered_at", ""))
        blog_id, log_no = _parse_blog_url(r.get("origin_url", ""))
        preview = html.unescape(r.get("contents") or "")

        full = fetch_blog_fulltext(blog_id, log_no)
        fulltext_ok = bool(full)
        body = full if fulltext_ok else preview
        print(f"  본문 추출 중 {seq}/{total} ...", end="\r")
        if seq < total:
            time.sleep(random.uniform(*B_FULLTEXT_DELAY))

        w       = _calc_weight_blog(body, dt)
        flags   = list(w["flags"])
        if not fulltext_ok:
            flags.append("fulltext_failed")

        hashtag_count = len(re.findall(r"#\S+", body))
        has_phone     = bool(re.search(r"0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}", body))

        entry = {
            "id":            seq,
            "blog_id":       blog_id,
            "log_no":        log_no or str(r.get("review_id", "")),
            "url":           r.get("origin_url", ""),
            "date":          dt.strftime("%Y-%m-%d") if dt else "",
            "date_raw":      r.get("registered_at", ""),
            "title":         html.unescape(r.get("title", "")),
            "author":        r.get("author", ""),
            "body":          body,
            "body_source":   "fulltext" if fulltext_ok else "preview",
            "char_count":    w["char_count"],
            "image_count":   int(r.get("photo_count") or 0),
            "hashtag_count": hashtag_count,
            "has_phone":     has_phone,
            "recency_bonus":   w["recency_bonus"],
            "recency_penalty": w["recency_penalty"],
            "weight":        w["weight"],
            "flags":         flags,
        }
        if w["exclude_reason"]:
            entry["exclude_reason"] = w["exclude_reason"]
            excluded.append(entry)
        else:
            reviews.append(entry)
    print()

    if target and len(reviews) > target:
        reviews = reviews[:target]

    count_within_3m  = sum(1 for r in reviews if r["recency_bonus"])
    count_3m_to_12m  = sum(1 for r in reviews if not r["recency_bonus"] and not r["recency_penalty"])
    count_12m_to_24m = sum(1 for r in reviews if r["recency_penalty"])
    dates = [r["date"] for r in reviews if re.match(r"\d{4}-\d{2}-\d{2}", r["date"])]

    result = {
        "restaurant":   name,
        "place_id":     confirm_id,
        "confirm_id":   confirm_id,
        "collected_at": COLLECTED_AT,
        "review_type":  "blog",
        "source":       "place_api_blog_reviews",
        "cutoff_3m":    CUTOFF_3M.strftime("%Y-%m-%d"),
        "cutoff_12m":   CUTOFF_12M.strftime("%Y-%m-%d"),
        "cutoff_24m":   CUTOFF_24M.strftime("%Y-%m-%d"),
        "stop_reason":  stop_reason,
        "place_info":   place_info,
        "reviews":      reviews,
        "excluded":     excluded,
        "summary": {
            "target":               target,
            "total_blog_count":     total_count or place_info.get("total_blog_reviews"),
            "total_valid":          len(reviews),
            "total_collected":      len(reviews),
            "count_within_3m":      count_within_3m,
            "count_3m_to_12m":      count_3m_to_12m,
            "count_12m_to_24m":     count_12m_to_24m,
            "recency_bonus_count":  count_within_3m,
            "recency_penalty_count": count_12m_to_24m,
            "phase2_entered":       stop_reason != K_STOP_TARGET_MET and count_12m_to_24m > 0,
            "target_met":           len(reviews) >= target,
            "total_excluded":       len(excluded),
            "excluded_paid_hard":   sum(1 for e in excluded if e.get("exclude_reason", "").startswith("paid_hard")),
            "excluded_too_old":     sum(1 for e in excluded if e.get("exclude_reason", "").startswith("too_old")),
            "flag_paid_suspect":    sum(1 for r in reviews if any(f.startswith("paid_suspect") for f in r["flags"])),
            "flag_extraction_warn": sum(1 for r in reviews if any(f.startswith("extraction_warning") for f in r["flags"])),
            "fulltext_count":       sum(1 for r in reviews if r.get("body_source") == "fulltext"),
            "fulltext_failed_count": sum(1 for r in reviews if r.get("body_source") != "fulltext"),
            "date_oldest":          min(dates) if dates else "unknown",
            "date_newest":          max(dates) if dates else "unknown",
        },
    }

    path = os.path.join(JSON_DIR, f"{name}_kakao_blog.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    s = result["summary"]
    warn = []
    if s["flag_paid_suspect"]:
        warn.append(f"대가성의심 {s['flag_paid_suspect']}")
    if s["fulltext_failed_count"]:
        warn.append(f"전문실패 {s['fulltext_failed_count']}")
    warn_note = f"   ⚠️ {' · '.join(warn)}" if warn else ""
    print(f"  ✓ 블로그 유효 {s['total_valid']}개   "
          f"최근3개월 {s['count_within_3m']} · 12~24개월 {s['count_12m_to_24m']} · 제외 {s['total_excluded']}{warn_note}")
    return result


def save_blog_txt(name: str, confirm_id: str, result: dict):
    s = result["summary"]
    lines = []
    lines.extend(format_place_info_txt(result.get("place_info", {})))

    lines.append("=" * 64)
    lines.append(f"{name} (confirm_id: {confirm_id}) — 카카오 블로그 리뷰 RAW 데이터")
    lines.append(f"수집 일시  : {COLLECTED_AT}")
    lines.append(f"수집 방식  : 카카오 place-api (tab/reviews/blog, order=LATEST)")
    lines.append(f"3개월 기준 : {result['cutoff_3m']} 이후 (×1.3)")
    lines.append(f"12개월 기준: {result['cutoff_12m']} 이전 (×0.5)")
    lines.append(f"24개월 기준: {result['cutoff_24m']} 이전 (제외)")
    lines.append(f"종료 사유  : {result['stop_reason']}")
    lines.append("=" * 64)
    lines.append("")
    lines.append("[수집 요약]")
    tb = s.get("total_blog_count")
    lines.append(f"  카카오 전체 블로그 : {tb:,}개" if tb else "  카카오 전체 블로그 : (확인불가)")
    lines.append(f"  수집 유효 리뷰      : {s['total_valid']}개  (분석 대상)")
    lines.append(f"    - 3개월 이내      : {s['count_within_3m']}개  (가중치 ×1.3)")
    lines.append(f"    - 3~12개월        : {s['count_3m_to_12m']}개  (가중치 ×1.0)")
    lines.append(f"    - 12~24개월       : {s['count_12m_to_24m']}개  (가중치 ×0.5)")
    lines.append(f"  제외 리뷰 수        : {s['total_excluded']}개")
    lines.append(f"    - 대가성(확정)    : {s['excluded_paid_hard']}개")
    lines.append(f"  ⚠️ 대가성 의심 플래그: {s['flag_paid_suspect']}개")
    lines.append(f"  ⚠️ 추출 의심 플래그  : {s['flag_extraction_warn']}개")
    lines.append(f"  본문 전문 추출      : {s.get('fulltext_count', 0)}개 성공 / "
                 f"{s.get('fulltext_failed_count', 0)}개 프리뷰 폴백")
    lines.append(f"  수집 기간           : {s['date_oldest']} ~ {s['date_newest']}")
    lines.append(f"  목표({s['target']}개)        : {'✅ 달성' if s['target_met'] else '❌ 미달성'}")
    lines.append("")
    lines.append("  ※ 본문은 blog.naver.com 원문에서 추출한 전문입니다.")
    lines.append("     (추출 실패 시 카카오 API 프리뷰로 폴백 → 플래그 fulltext_failed)")
    lines.append("")

    lines.append("=" * 64)
    lines.append(f"[블로그 리뷰 전체 {s['total_valid']}개 — 최신순]")
    lines.append("=" * 64)
    lines.append("")
    for r in result["reviews"]:
        flag_str  = f"  [플래그: {', '.join(r['flags'])}]" if r["flags"] else ""
        if r.get("recency_bonus"):    phase_tag = " (3개월이내×1.3)"
        elif r["recency_penalty"]:    phase_tag = " (12~24개월×0.5)"
        else:                         phase_tag = " (×1.0)"
        lines.append(f"[{r['id']:04d}] {r['author']} | {r['date']} | 가중치 {r['weight']}{phase_tag}{flag_str}")
        lines.append(f"  제목   : {r['title']}")
        lines.append(f"  URL    : {r['url']}")
        src = "전문" if r.get("body_source") == "fulltext" else "프리뷰폴백"
        lines.append(f"  메타   : {r['char_count']}자 / 이미지 {r['image_count']}개 / "
                     f"해시태그 {r['hashtag_count']}개 / 전화 {'있음' if r['has_phone'] else '없음'} / 본문={src}")
        lines.append(f"  본문   : {r['body']}")
        lines.append("")

    path = os.path.join(TXT_DIR, f"{name}_kakao_blog.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ─────────────────────────────────────────────
# 최종 요약 출력 헬퍼
# ─────────────────────────────────────────────

def _pad(s: str, width: int) -> str:
    """ASCII=1칸, 그 외(한글·이모지)=2칸으로 계산한 우측 패딩."""
    disp = sum(1 if ord(c) < 128 else 2 for c in s)
    return s + " " * max(0, width - disp)


def _fail_note_visitor(result: dict) -> str:
    s = result["summary"]
    parts = []
    total = s.get("total_naver_count") or s.get("total_kakao_count")
    if isinstance(total, int) and total < s["target"]:
        parts.append(f"플레이스 리뷰 {total}개뿐")
    if s.get("excluded_too_old", 0):
        parts.append(f"24개월초과 {s['excluded_too_old']}개 제외")
    if s.get("excluded_paid", 0):
        parts.append(f"대가성 {s['excluded_paid']}개 제외")
    return " / ".join(parts) if parts else "유효 리뷰 부족"


def _fail_note_blog(result: dict) -> str:
    stop = result.get("stop_reason", "")
    note = stop.split(":", 1)[-1].replace("_", " ") if ":" in stop else stop
    paid = result["summary"].get("excluded_paid_hard", 0)
    if paid:
        note += f" / 협찬 {paid}개 제외"
    return note


def _print_final_summary(log: list, elapsed: float) -> None:
    mins, secs  = divmod(int(elapsed), 60)
    n_rest      = len({e["name"] for e in log})
    n_total     = len(log)
    n_ok        = sum(1 for e in log if e["target_met"])

    W = 70
    print("\n" + "═" * W)
    print("  수집 최종 요약")
    print("═" * W)
    print(f"  소요 시간 : {mins}분 {secs}초")
    print(f"  처리 식당 : {n_rest}개  /  수집 유형 합계 : {n_total}건")
    print()
    print("  " + _pad("음식점", 20) + _pad("종류", 8) + _pad("결과", 8) + _pad("수집/목표", 11) + "실패 사유")
    print("  " + "─" * (W - 2))
    for e in log:
        mark = "✅ 달성" if e["target_met"] else "❌ 미달"
        coll = f"{e['collected']}/{e['target']}"
        print("  " + _pad(e["name"], 20) + _pad(e["type"], 8) + _pad(mark, 8) + _pad(coll, 11) + e.get("note", ""))
    print("  " + "─" * (W - 2))
    print(f"  ✅ 달성 {n_ok}/{n_total}건   ❌ 미달 {n_total - n_ok}/{n_total}건   저장위치: JSON={JSON_DIR} / TXT={TXT_DIR}")
    print("═" * W + "\n")


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def main():
    start_time  = time.time()
    results_log = []

    restaurants = prompt_restaurants()
    if not restaurants:
        return

    mode = prompt_mode()
    do_kakao = mode in ("all", "kakao")
    do_blog  = mode in ("all", "blog")

    k_target, b_target = prompt_review_counts(mode)

    print()
    print(f"총 {len(restaurants)}개 음식점 / 모드: {mode} / "
          f"카카오 {k_target}개 · 블로그 {b_target}개 (유효 기준)")
    print("=" * 64)

    total_rest = len(restaurants)
    for idx, r in enumerate(restaurants, 1):
        name       = r["name"]
        confirm_id = r["confirm_id"]
        prog       = f"[{idx}/{total_rest}]"

        print("\n" + "━" * 64)
        print(f"▶ {prog} {name}")
        print("━" * 64)

        # ① 매장정보
        place_info = collect_place_info(confirm_id, name)

        # ② 카카오 후기
        if do_kakao:
            strength_map = fetch_strength_map(confirm_id)
            raw_k, stop_k, score_set = collect_kakao_reviews(confirm_id, name, k_target)
            if raw_k:
                result = save_kakao_json(name, confirm_id, raw_k, stop_k,
                                         place_info, score_set, strength_map, k_target)
                save_kakao_txt(name, confirm_id, result)
                s = result["summary"]
                results_log.append({
                    "name": name, "type": "카카오",
                    "target_met": s["target_met"],
                    "collected": s["total_collected"], "target": s["target"],
                    "note": _fail_note_visitor(result) if not s["target_met"] else "",
                })
            else:
                print(f"  ❌ 카카오 후기 수집 실패 또는 결과 없음")
                results_log.append({"name": name, "type": "카카오", "target_met": False,
                                    "collected": 0, "target": k_target, "note": "수집 결과 없음"})

        # ③ 블로그 리뷰
        if do_blog:
            raw_b, stop_b, total_b = collect_blog(confirm_id, name, b_target)
            if raw_b:
                result = save_blog_json(name, confirm_id, raw_b, stop_b,
                                        place_info, total_b, b_target)
                save_blog_txt(name, confirm_id, result)
                s = result["summary"]
                results_log.append({
                    "name": name, "type": "블로그",
                    "target_met": s["target_met"],
                    "collected": s.get("total_valid", s.get("total_collected", 0)), "target": s["target"],
                    "note": _fail_note_blog(result) if not s["target_met"] else "",
                })
            else:
                print(f"  ❌ 블로그 리뷰 수집 실패 또는 결과 없음")
                results_log.append({"name": name, "type": "블로그", "target_met": False,
                                    "collected": 0, "target": b_target, "note": "수집 결과 없음"})

    _print_final_summary(results_log, time.time() - start_time)


if __name__ == "__main__":
    main()

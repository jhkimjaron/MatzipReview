"""
naver_ReviewCollector.py
================================
네이버 플레이스 통합 수집기

수집 항목:
    1. 매장 기본정보 (영업시간·메뉴·편의시설 등)  ← place_info 필드로 저장
    2. 방문자 리뷰 (DOM 스크래핑)
    3. 블로그 리뷰  (GraphQL API 페이지네이션)

사용법:
    python naver_ReviewCollector.py

실행하면 음식점 URL/Place ID와 이름을 입력하고,
수집 모드(전체 / 방문자만 / 블로그만)를 선택합니다.

출력 파일 (raw/ 폴더):
    {음식점명}_naver_visitor.json  — 방문자 리뷰 + place_info
    {음식점명}_naver_visitor.txt
    {음식점명}_naver_blog.json     — 블로그 리뷰(전문) + place_info
    {음식점명}_naver_blog.txt

사전 준비:
    pip install playwright
    playwright install chromium
"""

import asyncio
import html
import json
import re
import os
import random
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from playwright.async_api import async_playwright


# ─────────────────────────────────────────────
# 공통 상수
# ─────────────────────────────────────────────

_BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
JSON_DIR      = os.path.join(_BASE_DIR, "reviews_json")   # 분석 입력용 JSON
TXT_DIR       = os.path.join(_BASE_DIR, "reviews_txt")    # 사람 확인용 TXT
os.makedirs(JSON_DIR, exist_ok=True)
os.makedirs(TXT_DIR, exist_ok=True)
_NOW          = datetime.now()
COLLECTED_AT  = _NOW.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _months_ago(months: int) -> datetime:
    y, m = divmod(_NOW.month - months, 12)
    if m <= 0:
        m += 12
        y -= 1
    return _NOW.replace(year=_NOW.year + y, month=m, day=min(_NOW.day, 28))


CUTOFF_3M  = _months_ago(3)
CUTOFF_12M = _months_ago(12)
CUTOFF_24M = _months_ago(24)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
BROWSER_VIEWPORT = {"width": 390, "height": 844}

# ── 방문자 리뷰 상수 ──
DEFAULT_V_TARGET  = 150   # 방문자 리뷰 기본 목표(유효 리뷰 기준). 실행 시 사용자 입력으로 덮어씀
V_MIN_CHARS       = 15    # 공백제외 15자 미만(0자 포함) → 제외
V_MEDIUM_CHARS    = 40    # 공백제외 40~99자 → ×1.2
V_PREMIUM_CHARS   = 100   # 공백제외 100자 이상 → ×1.5
V_MAX_REVIEWS     = 800   # DOM 로딩 안전 상한 (목표가 매우 클 때 대비)
V_NO_CHANGE_LIMIT = 6

V_PAID_KEYWORDS = [
    "리뷰이벤트", "홍보", "광고", "체험단", "협찬",
    "무상 제공", "이벤트 당첨", "기자단",
]

# ── 블로그 리뷰 상수 ──
DEFAULT_B_TARGET = 50    # 블로그 리뷰 기본 목표(유효 리뷰 기준). 실행 시 사용자 입력으로 덮어씀
B_MIN_BODY_CHARS = 300   # 전문 추출 후에도 이보다 짧으면 추출 의심 경고선
B_API_DISPLAY    = 50
B_API_MAX_PAGES  = 30
B_COLD_RETRY     = 4    # 첫 페이지 cold 빈응답(total/max=0) 시 재시도 횟수
B_PAGE_DELAY_MIN = 0.6
B_PAGE_DELAY_MAX = 1.4

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

B_GQL_QUERY = (
    "query getFsasReviews($input: FsasReviewsInput){"
    " fsasReviews(input:$input){ total maxItemCount"
    " items{ url home title contents createdString date authorName name"
    " hasNaverReservation bySmartEditor3 thumbnailCount reviewId } } }"
)

B_STOP_TARGET_MET  = "target_met:유효리뷰_목표도달"
B_STOP_24M_WALL    = "24m_wall:24개월초과_도달"
B_STOP_API_END     = "api_end:maxItemCount_소진"


# ─────────────────────────────────────────────
# 공통 유틸
# ─────────────────────────────────────────────

def parse_input(user_input: str) -> str | None:
    s = user_input.strip()
    if re.fullmatch(r"\d+", s):
        return s
    m = re.search(r"/(?:place|restaurant)/(\d+)", s)
    if m:
        return m.group(1)
    if s.startswith("http"):
        return s
    return None


def _safe_date(y, mo, d) -> datetime | None:
    try:
        return datetime(int(y), int(mo), int(d))
    except (ValueError, TypeError):
        return None


async def make_browser_context(pw):
    browser = await pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        user_agent=BROWSER_UA,
        viewport=BROWSER_VIEWPORT,
        locale="ko-KR",
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context


async def resolve_place_id(page, place_id_or_url: str) -> str:
    """단축 URL이면 브라우저 리다이렉트로 place_id 확정."""
    if re.fullmatch(r"\d+", place_id_or_url):
        return place_id_or_url
    await page.goto(place_id_or_url)
    await page.wait_for_timeout(2000)
    m = re.search(r"/(?:place|restaurant)/(\d+)", page.url)
    return m.group(1) if m else "unknown"


# ─────────────────────────────────────────────
# 사용자 입력 인터페이스
# ─────────────────────────────────────────────

def prompt_restaurants() -> list[dict]:
    print("=" * 64)
    print("네이버 플레이스 통합 수집기")
    print(f"실행 시각  : {COLLECTED_AT}")
    print("가중치 기준: 최근 3개월 ×1.3 / 12~24개월 ×0.5 / 24개월 초과 제외")
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


def prompt_mode() -> str:
    print()
    print("수집 모드를 선택하세요:")
    print("  1. 전체 (매장정보 + 방문자 리뷰 + 블로그 리뷰)")
    print("  2. 방문자 리뷰만 (매장정보 포함)")
    print("  3. 블로그 리뷰만  (매장정보 포함)")
    while True:
        choice = input("선택 [1/2/3] (기본값 1): ").strip()
        if choice in ("", "1"):
            return "all"
        if choice == "2":
            return "visitor"
        if choice == "3":
            return "blog"
        print("  ❌ 1, 2, 3 중 하나를 입력해주세요.")


def _ask_count(label: str, default: int) -> int:
    """리뷰 개수 1건 입력받기. 빈 입력은 기본값, 잘못된 입력은 재요청."""
    while True:
        raw = input(f"  {label} (기본 {default}개, Enter=기본값): ").strip()
        if not raw:
            return default
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        print("  ❌ 1 이상의 숫자를 입력해주세요.")


def prompt_review_counts(mode: str) -> tuple[int, int]:
    """수집할 리뷰 개수를 한 번만 입력받아 모든 음식점에 동일 적용한다.
    여기서 입력하는 개수는 '광고·불성실 등 제외 후의 유효(분석 대상) 리뷰' 기준이다."""
    print()
    print("─" * 64)
    print(f"기본 수집 리뷰 수는 방문자 리뷰 {DEFAULT_V_TARGET}개, 블로그 리뷰 {DEFAULT_B_TARGET}개입니다.")
    print("각각 몇 개씩 수집할까요?")
    print("※ 입력 개수 = 광고·불성실 등 '제외 대상을 뺀 유효 리뷰' 기준입니다.")
    print("※ 여러 음식점을 수집할 경우 모두 같은 개수로 수집됩니다.")
    v_target = DEFAULT_V_TARGET
    b_target = DEFAULT_B_TARGET
    if mode in ("all", "visitor"):
        v_target = _ask_count("방문자 리뷰 개수", DEFAULT_V_TARGET)
    if mode in ("all", "blog"):
        b_target = _ask_count("블로그 리뷰 개수", DEFAULT_B_TARGET)
    print(f"  → 방문자 {v_target}개 / 블로그 {b_target}개 (유효 리뷰 기준)로 수집합니다.")
    print("─" * 64)
    return v_target, b_target


# ─────────────────────────────────────────────
# ① 매장 기본정보 수집
# ─────────────────────────────────────────────

async def collect_place_info(place_id: str, name: str) -> dict:
    """
    네이버 플레이스 홈 탭 + 메뉴 탭에서 매장 기본정보를 수집한다.
    홈 탭에서 모든 운영 정보를 수집하고, 메뉴 탭은 메뉴 목록만 담당한다.
    """
    print("  ▷ 매장정보 수집 중...", end="\r")

    home_url = f"https://m.place.naver.com/restaurant/{place_id}/home"
    menu_url = f"https://m.place.naver.com/restaurant/{place_id}/menu/list"

    result = {
        "place_id":              place_id,
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
        "naver_booking":         False,
        "total_visitor_reviews": None,
        "total_blog_reviews":    None,
        "menus":                 [],
        "raw_hours_text":        None,
        "errors":                [],
    }

    async with async_playwright() as pw:
        browser, context = await make_browser_context(pw)
        page = await context.new_page()

        # ── 홈 탭: 모든 운영정보 ──
        try:
            await page.goto(home_url)
            await page.wait_for_timeout(2500)

            # 영업시간 "펼쳐보기" 클릭
            await page.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll('span._UCia'));
                    const hoursBtn = btns.find(b => {
                        const p = b.closest('.O8qbU');
                        return p && p.textContent.includes('영업시간');
                    });
                    if (hoursBtn) hoursBtn.click();
                }
            """)
            await page.wait_for_timeout(800)

            extracted = await page.evaluate("""
                () => {
                    // ── 카테고리 ──
                    const catEl = document.querySelector('span.lnJFt') ||
                                  document.querySelector('span.DJTkF');
                    const category = catEl ? catEl.innerText.trim() : null;

                    // ── 전화번호 ──
                    const phoneEl = document.querySelector('a[href^="tel:"]');
                    const phone = phoneEl ? phoneEl.href.replace('tel:', '') : null;

                    // ── 주소 ──
                    const addrEl = document.querySelector('span.pz7wy') ||
                                   document.querySelector('.O8qbU.tQY7D a.PkgBl');
                    const address = addrEl ? addrEl.innerText.trim() : null;

                    // ── 홈페이지/SNS ──
                    const hpBlock = document.querySelector('.O8qbU.yIPfO');
                    let homepage = null;
                    if (hpBlock) {
                        const hpLink = hpBlock.querySelector('a');
                        homepage = hpLink ? hpLink.href : hpBlock.innerText.trim().split('\\n')[0];
                    }

                    // ── 네이버예약 ──
                    const hasBooking = !!(
                        document.querySelector('a[href*="booking.naver"]') ||
                        document.querySelector('button[class*="reserve"]')
                    );

                    // ── 총 리뷰 수 ──
                    let totalVisitor = null, totalBlog = null;
                    document.querySelectorAll('span.PXMot').forEach(el => {
                        const t = el.innerText.trim();
                        const vm = t.match(/방문자 리뷰([\d,]+)/);
                        const bm = t.match(/블로그 리뷰([\d,]+)/);
                        if (vm) totalVisitor = parseInt(vm[1].replace(/,/g, ''), 10);
                        if (bm) totalBlog    = parseInt(bm[1].replace(/,/g, ''), 10);
                    });

                    // ── 영업시간: 요일별 행 파싱 ──
                    const hoursRows = [];
                    document.querySelectorAll('.w9QyJ').forEach(row => {
                        const dayEl    = row.querySelector('.i8cJw');
                        const detailEl = row.querySelector('.H3ua4');
                        if (!dayEl || !detailEl) return;
                        hoursRows.push({
                            day:        dayEl.innerText.trim(),
                            detailHTML: detailEl.innerHTML,
                            detailText: detailEl.innerText.trim(),
                        });
                    });

                    // ── 편의시설 ──
                    const facBlock = document.querySelector('.O8qbU.Uv6Eo');
                    const facilityText = facBlock ? facBlock.innerText.trim() : null;

                    return {
                        category, phone, address, homepage, hasBooking,
                        totalVisitor, totalBlog, hoursRows, facilityText,
                    };
                }
            """)

            result["category"]              = extracted.get("category")
            result["phone"]                 = extracted.get("phone")
            result["address"]               = extracted.get("address")
            result["homepage"]              = extracted.get("homepage")
            result["naver_booking"]         = extracted.get("hasBooking", False)
            result["total_visitor_reviews"] = extracted.get("totalVisitor")
            result["total_blog_reviews"]    = extracted.get("totalBlog")

            # 편의시설 파싱 (쉼표 구분)
            fac_text = extracted.get("facilityText") or ""
            # 레이블 "편의" 접두어 제거
            fac_text = re.sub(r"^편의\s*", "", fac_text)
            fac_list = [f.strip() for f in fac_text.split(",") if f.strip()]
            result["facilities"] = fac_list
            # 주차 정보는 시설 목록에서 추출
            for f in fac_list:
                if "주차" in f:
                    result["parking"] = f
                    break

            # 영업시간 행 파싱
            hours_rows = extracted.get("hoursRows") or []
            raw_lines = []
            for row in hours_rows:
                day = row["day"]
                detail_html = row.get("detailHTML", "")
                detail_text = row.get("detailText", "")
                raw_lines.append(f"{day} {detail_text}")

                if "정기휴무" in detail_text:
                    # 휴무 요일이 여러 개일 수 있으므로 모두 누적한다(중복 방지)
                    txt = detail_text.strip()
                    if not result["regular_holiday"]:
                        result["regular_holiday"] = txt
                    elif txt not in result["regular_holiday"]:
                        result["regular_holiday"] += " / " + txt
                    continue

                # <br> 기준으로 분리
                lines = [l.strip() for l in re.split(r"<br\s*/?>", detail_html) if l.strip()]
                open_close = None
                for line in lines:
                    clean = re.sub(r"<[^>]+>", "", line).strip()
                    if re.match(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$", clean):
                        open_close = clean
                    elif "브레이크타임" in clean and not result["break_time"]:
                        m = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", clean)
                        if m:
                            result["break_time"] = f"{m.group(1)} - {m.group(2)}"
                    elif "라스트오더" in clean and not result["last_order"]:
                        m = re.search(r"(\d{1,2}:\d{2})", clean)
                        if m:
                            result["last_order"] = m.group(1)

                if open_close:
                    m = re.match(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", open_close)
                    if m:
                        result["hours"].append({
                            "day": day, "open": m.group(1), "close": m.group(2)
                        })

            result["raw_hours_text"] = "\n".join(raw_lines) if raw_lines else None

        except Exception as e:
            result["errors"].append(f"home_tab: {e}")

        # ── 메뉴 탭 ──
        try:
            await page.goto(menu_url)
            await page.wait_for_timeout(2500)

            menus = await page.evaluate("""
                () => {
                    const items = [];
                    document.querySelectorAll('li.E2jtL').forEach(card => {
                        const nameEl  = card.querySelector('.lPzHi') ||
                                        card.querySelector('[class*="name"]');
                        const priceEl = card.querySelector('span.p2H02') ||
                                        card.querySelector('[class*="price"]');
                        const descEl  = card.querySelector('.okI98') ||
                                        card.querySelector('[class*="desc"]');
                        const menuName  = nameEl  ? nameEl.innerText.trim() : null;
                        const menuPrice = priceEl ? priceEl.innerText.trim() : null;
                        const menuDesc  = descEl  ? descEl.innerText.trim() : null;
                        if (menuName && menuName.length > 1 && menuName.length < 60) {
                            items.push({
                                name:        menuName,
                                price:       menuPrice,
                                description: (menuDesc && menuDesc !== menuName) ? menuDesc : null,
                            });
                        }
                    });
                    const seen = new Set();
                    return items.filter(it => {
                        if (seen.has(it.name)) return false;
                        seen.add(it.name);
                        return true;
                    });
                }
            """)
            result["menus"] = menus[:60]
        except Exception as e:
            result["errors"].append(f"menu_tab: {e}")

        await context.close()
        await browser.close()

    miss = [k for k in ("address", "hours", "menus") if not result.get(k)]
    miss_note = f"  (누락: {', '.join(miss)})" if miss else ""
    print(f"  ✓ 매장정보       메뉴 {len(result['menus'])}개 · 영업시간 {len(result['hours'])}일{miss_note}")
    return result


def parse_hours_text(text: str, result: dict):
    """
    영업시간 raw 텍스트에서 요일별 시간·브레이크타임·라스트오더·정기휴무 파싱.
    네이버 표기 예:
        "매일 11:00 - 21:00\n브레이크타임 15:00 - 16:30\n라스트오더 20:10"
        "월요일 휴무\n화~일 11:00 - 21:00"
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    day_pattern = re.compile(
        r"(매일|월|화|수|목|금|토|일|평일|주말|월~금|화~일|월~일|[월화수목금토일]+[~,\s]*[월화수목금토일]*)"
        r"[^0-9]*(\d{1,2}:\d{2})\s*[-~]\s*(\d{1,2}:\d{2})"
    )
    break_pattern    = re.compile(r"브레이크.{0,4}(\d{1,2}:\d{2})\s*[-~]\s*(\d{1,2}:\d{2})")
    lo_pattern       = re.compile(r"(?:라스트오더|L\.?O\.?)\s*(\d{1,2}:\d{2})")
    holiday_pattern  = re.compile(r"(매주\s*)?([월화수목금토일]+요일|매일)\s*휴무")

    for line in lines:
        m = day_pattern.search(line)
        if m:
            result["hours"].append({
                "day":   m.group(1).strip(),
                "open":  m.group(2),
                "close": m.group(3),
            })
            continue

        m = break_pattern.search(line)
        if m and not result["break_time"]:
            result["break_time"] = f"{m.group(1)} - {m.group(2)}"
            continue

        m = lo_pattern.search(line)
        if m and not result["last_order"]:
            result["last_order"] = m.group(1)
            continue

        m = holiday_pattern.search(line)
        if m:
            # 휴무 요일이 여러 개일 수 있으므로 모두 누적한다(중복 방지)
            if not result["regular_holiday"]:
                result["regular_holiday"] = line
            elif line not in result["regular_holiday"]:
                result["regular_holiday"] += " / " + line
            continue

        # "휴무" 단독 언급
        if "휴무" in line:
            seg = line[:60]
            if not result["regular_holiday"]:
                result["regular_holiday"] = seg
            elif seg not in result["regular_holiday"]:
                result["regular_holiday"] += " / " + seg


def format_place_info_txt(info: dict) -> list[str]:
    lines = []
    lines.append("=" * 64)
    lines.append("[매장 기본정보]")
    lines.append("=" * 64)
    lines.append(f"  카테고리   : {info.get('category') or '(미확인)'}")
    lines.append(f"  주소       : {info.get('address')  or '(미확인)'}")
    lines.append(f"  전화번호   : {info.get('phone')    or '(미확인)'}")
    lines.append(f"  홈페이지   : {info.get('homepage') or '(미확인)'}")
    lines.append(f"  네이버예약 : {'가능' if info.get('naver_booking') else '없음'}")
    lines.append(f"  정기휴무   : {info.get('regular_holiday') or '(미확인)'}")
    lines.append(f"  브레이크   : {info.get('break_time')      or '(미확인)'}")
    lines.append(f"  라스트오더 : {info.get('last_order')      or '(미확인)'}")
    lines.append(f"  주차       : {info.get('parking')         or '(미확인)'}")
    tv = info.get('total_visitor_reviews')
    tb = info.get('total_blog_reviews')
    lines.append(f"  방문자리뷰 : 네이버 총 {tv:,}개" if tv else "  방문자리뷰 : (미확인)")
    lines.append(f"  블로그리뷰 : 네이버 총 {tb:,}개" if tb else "  블로그리뷰 : (미확인)")

    if info.get("hours"):
        lines.append("  영업시간   :")
        for h in info["hours"]:
            lines.append(f"    {h['day']:<6} {h['open']} - {h['close']}")
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
# ② 방문자 리뷰
# ─────────────────────────────────────────────

def _parse_korean_date(date_str: str) -> datetime | None:
    m = re.match(r"(\d{4})년\s+(\d+)월\s+(\d+)일", date_str)
    if m:
        return _safe_date(m.group(1), m.group(2), m.group(3))
    return None


def _calc_weight_visitor(content: str, date_str: str) -> dict:
    char_count = len(re.sub(r"\s", "", content))   # 공백제외 기준
    dt         = _parse_korean_date(date_str)
    date_iso   = dt.strftime("%Y-%m-%d") if dt else date_str

    weight          = 1.0
    recency_bonus   = False
    recency_penalty = False
    quality_bonus   = False
    exclude_reason  = None

    for kw in V_PAID_KEYWORDS:
        if kw in content:
            exclude_reason = f"paid:{kw}"
            break

    if not exclude_reason and char_count < V_MIN_CHARS:   # 0자 포함, 공백포함 15자 미만
        exclude_reason = "insincere"

    if not exclude_reason and dt and dt < CUTOFF_24M:
        exclude_reason = "too_old:24개월_초과"

    if not exclude_reason:
        if dt:
            if dt >= CUTOFF_3M:             # 3개월 이내 → 최신 보너스
                weight        *= 1.3
                recency_bonus  = True
            elif dt < CUTOFF_12M:           # 12~24개월 → 신선도 감점
                weight        *= 0.5
                recency_penalty = True
        if char_count >= V_PREMIUM_CHARS:   # 100자 이상
            weight        *= 1.5
            quality_bonus  = True
        elif char_count >= V_MEDIUM_CHARS:  # 40~99자
            weight        *= 1.2
            quality_bonus  = True

    return {
        "date_iso":        date_iso,
        "char_count":      char_count,
        "weight":          round(weight, 2),
        "recency_bonus":   recency_bonus,
        "recency_penalty": recency_penalty,
        "quality_bonus":   quality_bonus,
        "exclude_reason":  exclude_reason,
    }


# 방문자 리뷰 DOM 추출 JS — 로딩 중 유효 개수 집계와 최종 추출에 공용으로 사용
_VISITOR_EXTRACT_JS = r"""
    () => {
        const ul = document.querySelector('ul.OTi6Q');
        if (!ul) return [];
        return Array.from(ul.querySelectorAll('li.EjjAW')).map((el, i) => {
            const contentDiv = el.querySelector('div.pui__vn15t2');
            let content = contentDiv ? contentDiv.innerText.trim() : '';
            content = content.replace(/\n?더보기\s*$/, '').trim();

            let date = '';
            el.querySelectorAll('span.pui__blind').forEach(s => {
                if (/\d{4}년/.test(s.innerText)) date = s.innerText.trim();
            });

            const tags = Array.from(el.querySelectorAll('span.pui__V8F9nN'))
                .map(s => s.innerText.trim()).filter(Boolean);
            const keywords = Array.from(el.querySelectorAll('span.pui__jhpEyP'))
                .map(s => s.innerText.trim()).filter(Boolean);
            const authorEl = el.querySelector('span.pui__NMi-Dp');
            const author   = authorEl ? authorEl.innerText.trim() : '';

            return { seq: i + 1, author, date, content, tags, keywords };
        });
    }
"""


async def _count_valid_visitor(page) -> int:
    """현재 로드된 방문자 리뷰 중 '제외 대상이 아닌 유효 리뷰' 수를 센다."""
    loaded = await page.evaluate(_VISITOR_EXTRACT_JS)
    return sum(
        1 for r in loaded
        if not _calc_weight_visitor(r["content"], r["date"])["exclude_reason"]
    )


async def collect_visitor(place_id: str, name: str, target: int) -> list[dict]:
    url = f"https://m.place.naver.com/restaurant/{place_id}/review/visitor?reviewSort=recent"
    print(f"  ▷ 방문자 리뷰 (목표 {target}개)")

    async with async_playwright() as pw:
        browser, context = await make_browser_context(pw)
        page = await context.new_page()
        await page.goto(url)
        await page.wait_for_timeout(2000)

        prev_count   = 0
        no_change    = 0
        total_clicks = 0
        valid_cnt    = 0

        while True:
            btn = await page.query_selector("a.fvwqf")
            if btn:
                await page.evaluate("el => el.click()", btn)
                total_clicks += 1
                await page.wait_for_timeout(1500)
            else:
                await page.evaluate("window.scrollBy(0, 600)")
                await page.wait_for_timeout(1000)

            count = await page.evaluate("""
                () => {
                    const ul = document.querySelector('ul.OTi6Q');
                    return ul ? ul.querySelectorAll('li.EjjAW').length : 0;
                }
            """)

            # 로드 수가 늘었을 때만 유효 리뷰 수 재집계 (불필요한 추출 방지)
            if count != prev_count:
                valid_cnt = await _count_valid_visitor(page)
            print(f"  로딩 중... 로드 {count}개 / 유효 {valid_cnt}개 (목표 {target})", end="\r")

            # ① 목표 유효 개수 도달 → 중단
            if valid_cnt >= target:
                print(f"\n  목표 달성: 유효 {valid_cnt}개 ≥ {target}개 → 로딩 중단")
                break
            # ② 안전 상한 도달 → 중단
            if count >= V_MAX_REVIEWS:
                print(f"\n  상한 도달: 로드 {count}개 → 로딩 중단 (유효 {valid_cnt}개)")
                break
            # ③ 더 이상 로드되지 않음 → 중단
            if count == prev_count:
                no_change += 1
                if no_change >= V_NO_CHANGE_LIMIT:
                    print(f"\n  더 이상 로드되지 않음 → 중단 (유효 {valid_cnt}개 < 목표 {target}개)")
                    break
            else:
                no_change  = 0
                prev_count = count

        await page.evaluate("""
            () => {
                document.querySelectorAll('a.pui__GStJHb, div.pui__vn15t2, span.pui__V8F9nN')
                    .forEach(el => {
                        el.style.overflow        = 'visible';
                        el.style.webkitLineClamp = 'unset';
                        el.style.display         = 'block';
                        el.style.maxHeight       = 'none';
                    });
            }
        """)
        await page.wait_for_timeout(500)

        raw = await page.evaluate(_VISITOR_EXTRACT_JS)

        # 네이버 표시 총 리뷰 수 (탭 헤더 또는 섹션 카운트에서 추출)
        total_naver_count = await page.evaluate("""
            () => {
                const candidates = [
                    document.querySelector('.place_section_count em'),
                    document.querySelector('em.place_section_count'),
                    document.querySelector('[class*="ReviewCount"] em'),
                    document.querySelector('[class*="reviewCount"] em'),
                    [...document.querySelectorAll('em, strong')].find(el => {
                        const p = el.parentElement;
                        return p && /방문자.{0,2}리뷰/.test(p.innerText);
                    }),
                ];
                for (const el of candidates) {
                    if (!el) continue;
                    const n = parseInt(el.innerText.replace(/[^0-9]/g, ''));
                    if (!isNaN(n) && n > 0) return n;
                }
                return null;
            }
        """)

        await context.close()
        await browser.close()

    return raw, total_naver_count


def save_visitor_json(name: str, place_id: str, raw: list[dict],
                      place_info: dict, total_naver_count: int | None,
                      target: int) -> dict:
    reviews  = []
    excluded = []

    for r in raw:
        w = _calc_weight_visitor(r["content"], r["date"])
        entry = {
            "id":              r["seq"],
            "author":          r["author"],
            "date":            w["date_iso"],
            "date_raw":        r["date"],
            "content":         r["content"],
            "char_count":      w["char_count"],
            "rating":          None,
            "tags":            r["tags"],
            "keywords":        r["keywords"],
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

    # 목표 = '제외 후 유효 리뷰' 개수. 초과분(최신순 뒤쪽)은 잘라 정확히 target개로 맞춘다.
    over_collected = max(0, len(reviews) - target)
    if over_collected:
        reviews = reviews[:target]

    count_within_3m  = sum(1 for r in reviews if r["recency_bonus"])
    count_3m_to_12m  = sum(1 for r in reviews if not r["recency_bonus"] and not r["recency_penalty"])
    count_12m_to_24m = sum(1 for r in reviews if r["recency_penalty"])
    dates = [r["date"] for r in reviews if re.match(r"\d{4}-\d{2}-\d{2}", r["date"])]
    total_loaded    = len(reviews) + len(excluded) + over_collected  # DOM에서 로드한 총 건수

    result = {
        "restaurant":   name,
        "place_id":     place_id,
        "collected_at": COLLECTED_AT,
        "review_type":  "visitor",
        "cutoff_3m":    CUTOFF_3M.strftime("%Y-%m-%d"),
        "cutoff_12m":   CUTOFF_12M.strftime("%Y-%m-%d"),
        "cutoff_24m":   CUTOFF_24M.strftime("%Y-%m-%d"),
        "place_info":   place_info,
        "reviews":      reviews,
        "excluded":     excluded,
        "summary": {
            "target":                 target,
            "total_naver_count":      total_naver_count,
            "total_loaded":           total_loaded,
            "total_collected":        len(reviews),
            "count_within_3m":        count_within_3m,
            "count_3m_to_12m":        count_3m_to_12m,
            "count_12m_to_24m":       count_12m_to_24m,
            "recency_bonus_count":    count_within_3m,
            "recency_penalty_count":  count_12m_to_24m,    # analyze_reviews.py 신뢰도 메모용
            "total_excluded":         len(excluded),
            "excluded_paid":          sum(1 for e in excluded if e.get("exclude_reason", "").startswith("paid")),
            "excluded_insincere":     sum(1 for e in excluded if e.get("exclude_reason") == "insincere"),
            "excluded_too_old":       sum(1 for e in excluded if e.get("exclude_reason", "").startswith("too_old")),
            "quality_bonus_count":    sum(1 for r in reviews if r["quality_bonus"]),
            "date_oldest":            min(dates) if dates else "unknown",
            "date_newest":            max(dates) if dates else "unknown",
            "target_met":             len(reviews) >= target,
        },
    }

    path = os.path.join(JSON_DIR, f"{name}_naver_visitor.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    s = result["summary"]
    print(f"  ✓ 방문자 유효 {len(reviews)}개   "
          f"최근3개월 {s['count_within_3m']} · 12~24개월 {s['count_12m_to_24m']} · 제외 {len(excluded)}")
    return result


def save_visitor_txt(name: str, place_id: str, result: dict):
    s = result["summary"]
    lines = []
    lines.extend(format_place_info_txt(result.get("place_info", {})))

    lines.append("=" * 64)
    lines.append(f"{name} (Place ID: {place_id}) — 방문자 리뷰 RAW 데이터")
    lines.append(f"수집 일시  : {COLLECTED_AT}")
    lines.append(f"수집 URL   : https://m.place.naver.com/restaurant/{place_id}/review/visitor?reviewSort=recent")
    lines.append(f"3개월 기준 : {result['cutoff_3m']} 이후 (×1.3)")
    lines.append(f"12개월 기준: {result['cutoff_12m']} 이전 (×0.5)")
    lines.append(f"24개월 기준: {result['cutoff_24m']} 이전 (제외)")
    lines.append("=" * 64)
    lines.append("")
    lines.append("[수집 요약]")
    naver_total = s.get("total_naver_count")
    naver_str   = f"{naver_total:,}개" if naver_total else "(확인불가)"
    lines.append(f"  네이버 전체 리뷰  : {naver_str}")
    lines.append(f"  DOM 로드 수       : {s.get('total_loaded', '?')}개  (상한 {V_MAX_REVIEWS}개)")
    lines.append(f"  유효 리뷰 수      : {s['total_collected']}개  (분석 대상)")
    lines.append(f"    - 3개월 이내    : {s['count_within_3m']}개  (가중치 ×1.3)")
    lines.append(f"    - 3~12개월      : {s['count_3m_to_12m']}개  (가중치 ×1.0)")
    lines.append(f"    - 12~24개월     : {s['count_12m_to_24m']}개  (가중치 ×0.5)")
    lines.append(f"  제외 리뷰 수      : {s['total_excluded']}개")
    lines.append(f"    - 대가성        : {s['excluded_paid']}개")
    lines.append(f"    - 불성실(15자↓) : {s['excluded_insincere']}개")
    lines.append(f"    - 24개월 초과   : {s['excluded_too_old']}개")
    lines.append(f"  40~99자(×1.2)     : {s['quality_bonus_count'] - sum(1 for r in result['reviews'] if r['char_count'] >= V_PREMIUM_CHARS)}개")
    lines.append(f"  100자이상(×1.5)   : {sum(1 for r in result['reviews'] if r['char_count'] >= V_PREMIUM_CHARS)}개")
    lines.append(f"  수집 기간         : {s['date_oldest']} ~ {s['date_newest']}")
    lines.append(f"  목표({s.get('target', '?')}개)       : {'✅ 달성' if s['target_met'] else '❌ 미달성'}")
    lines.append("")

    lines.append("=" * 64)
    lines.append(f"[방문자 리뷰 전체 {s['total_collected']}개 — 최신순]")
    lines.append("=" * 64)
    lines.append("")
    for r in result["reviews"]:
        weight_info = []
        if r.get("recency_bonus"):   weight_info.append("3개월이내×1.3")
        if r["recency_penalty"]:     weight_info.append("12~24개월×0.5")
        if r["quality_bonus"]:
            tier = "100자이상×1.5" if r["char_count"] >= V_PREMIUM_CHARS else "40~99자×1.2"
            weight_info.append(tier)
        weight_str = f"  [가중치:{r['weight']}" + (f" / {', '.join(weight_info)}]" if weight_info else "]")
        lines.append(f"[{r['id']:04d}] 작성자: {r['author']} | 날짜: {r['date']}{weight_str}")
        if r["tags"]:
            lines.append(f"  태그   : {' / '.join(r['tags'])}")
        if r["keywords"]:
            lines.append(f"  키워드 : {', '.join(r['keywords'])}")
        lines.append(f"  본문   : {r['content']}")
        lines.append("")

    path = os.path.join(TXT_DIR, f"{name}_naver_visitor.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ─────────────────────────────────────────────
# ③ 블로그 리뷰
# ─────────────────────────────────────────────

def _parse_created_string(s: str) -> datetime | None:
    if not s:
        return None
    m = re.match(r"\s*(\d{2})\.(\d{1,2})\.(\d{1,2})", s.strip())
    if m:
        return _safe_date(2000 + int(m.group(1)), m.group(2), m.group(3))
    return None


def _parse_blog_date(date_str: str) -> datetime | None:
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
        n    = int(m.group(1))
        days = {"일": 1, "주": 7, "개월": 30, "달": 30, "년": 365}[m.group(2)] * n
        return _NOW - timedelta(days=days)
    return None


def _resolve_blog_date(item: dict) -> datetime | None:
    return (_parse_created_string(item.get("createdString", ""))
            or _parse_blog_date(item.get("date", "")))


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
    s = re.sub(r"[ \t ]+", " ", s)      # 가로 공백 압축
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)  # 빈 줄 3개↑ → 2개
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


async def _fetch_blog_page(page, business_id: str, page_no: int) -> dict:
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
    params = {
        "businessId": business_id,
        "pageNo":     page_no,
        "display":    B_API_DISPLAY,
        "query":      B_GQL_QUERY,
    }
    data = await page.evaluate(js, params)

    # cold 응답 방어: 첫 페이지에서 errors 없이 total/maxItemCount가 0이면
    # (네이버 GraphQL이 세션 미준비 시 빈 정상응답을 주는 간헐적 현상) 재시도한다.
    if page_no == 0:
        retry = 0
        while (not data.get("error")
               and not (data.get("total") or 0)
               and not (data.get("maxItemCount") or 0)
               and retry < B_COLD_RETRY):
            retry += 1
            print(f"  ⚠️ 블로그 API 빈 응답(cold) → 재시도 {retry}/{B_COLD_RETRY}", end="\r")
            await page.wait_for_timeout(1500 * retry)
            data = await page.evaluate(js, params)
    return data


async def collect_blog(place_id: str, name: str, target: int) -> tuple[list[dict], str]:
    print(f"  ▷ 블로그 리뷰 (목표 {target}개)")

    async with async_playwright() as pw:
        browser, context = await make_browser_context(pw)
        page = await context.new_page()

        warm_url = f"https://m.place.naver.com/restaurant/{place_id}/review/ugc?reviewSort=recent"
        await page.goto(warm_url)
        await page.wait_for_timeout(2000)

        items              = []
        valid_cnt          = 0
        stop_reason        = B_STOP_API_END
        max_item           = None
        total_naver_count  = None   # 네이버 전체 블로그 리뷰 수
        api_max_accessible = None   # API 접근 가능 상한

        for page_no in range(B_API_MAX_PAGES):
            data = await _fetch_blog_page(page, place_id, page_no)
            if data.get("error"):
                print(f"\n  ⚠️ API 오류(page {page_no}): {data['error']}")
                break
            if max_item is None:
                max_item           = data.get("maxItemCount", 0)
                total_naver_count  = data.get("total")
                api_max_accessible = max_item

            batch = data.get("items", []) or []
            if not batch:
                break

            done = False
            for it in batch:
                dt = _resolve_blog_date(it)

                # 24개월 초과 도달 시 중단 (목표 미달이라도 데이터 신선도 한계)
                if dt and dt < CUTOFF_24M:
                    stop_reason = B_STOP_24M_WALL
                    print(f"\n  24개월 벽 도달 → 중단 (유효 {valid_cnt}개 / 목표 {target}개)")
                    done = True
                    break

                items.append({**it, "_dt": dt})

                is_paid = any(kw in (it.get("contents") or "") for kw in B_PAID_HARD)
                if not is_paid:
                    valid_cnt += 1

                # 목표 = '대가성(확정) 제외 후 유효 리뷰' 개수 도달 시 종료
                if valid_cnt >= target:
                    stop_reason = B_STOP_TARGET_MET
                    print(f"\n  목표 달성: 유효 {valid_cnt}개 ≥ {target}개 → 종료")
                    done = True
                    break

            print(f"  수집 중... 누적 {len(items)}개 / 유효 {valid_cnt}개 (목표 {target})", end="\r")

            if done:
                break

            if max_item and (page_no + 1) * B_API_DISPLAY >= max_item:
                stop_reason = B_STOP_API_END
                print(f"\n  maxItemCount({max_item}) 소진 → 종료")
                break

            await page.wait_for_timeout(
                int(random.uniform(B_PAGE_DELAY_MIN, B_PAGE_DELAY_MAX) * 1000)
            )

        await context.close()
        await browser.close()

    return items, stop_reason, total_naver_count, api_max_accessible


def save_blog_json(name: str, place_id: str, raw: list[dict], stop_reason: str,
                   place_info: dict, total_naver_count: int | None,
                   api_max_accessible: int | None, target: int) -> dict:
    reviews  = []
    excluded = []

    total = len(raw)
    for seq, r in enumerate(raw, 1):
        dt      = r.get("_dt")
        blog_id, log_no = _parse_blog_url(r.get("url", ""))
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
            "id":                  seq,
            "blog_id":             blog_id,
            "log_no":              log_no or r.get("reviewId", ""),
            "url":                 r.get("url", ""),
            "date":                dt.strftime("%Y-%m-%d") if dt else "",
            "date_raw":            r.get("createdString") or r.get("date", ""),
            "title":               html.unescape(r.get("title", "")),
            "author":              r.get("authorName") or r.get("name", ""),
            "body":                body,
            "body_source":         "fulltext" if fulltext_ok else "preview",
            "char_count":          w["char_count"],
            "image_count":         int(r.get("thumbnailCount") or 0),
            "hashtag_count":       hashtag_count,
            "has_phone":           has_phone,
            "has_naver_reservation": bool(r.get("hasNaverReservation")),
            "recency_bonus":       w["recency_bonus"],
            "recency_penalty":     w["recency_penalty"],
            "weight":              w["weight"],
            "flags":               flags,
        }
        if w["exclude_reason"]:
            entry["exclude_reason"] = w["exclude_reason"]
            excluded.append(entry)
        else:
            reviews.append(entry)
    print()

    # 목표 = '대가성 제외 후 유효 리뷰' 개수. 초과분(최신순 뒤쪽)은 잘라 정확히 target개로 맞춘다.
    if target and len(reviews) > target:
        reviews = reviews[:target]

    count_within_3m  = sum(1 for r in reviews if r["recency_bonus"])
    count_3m_to_12m  = sum(1 for r in reviews if not r["recency_bonus"] and not r["recency_penalty"])
    count_12m_to_24m = sum(1 for r in reviews if r["recency_penalty"])
    dates = [r["date"] for r in reviews if re.match(r"\d{4}-\d{2}-\d{2}", r["date"])]

    phase2_entered = stop_reason != B_STOP_TARGET_MET and count_12m_to_24m > 0
    target_met     = len(reviews) >= target

    result = {
        "restaurant":   name,
        "place_id":     place_id,
        "collected_at": COLLECTED_AT,
        "review_type":  "blog",
        "source":       "place_graphql_fsasReviews",
        "cutoff_3m":    CUTOFF_3M.strftime("%Y-%m-%d"),
        "cutoff_12m":   CUTOFF_12M.strftime("%Y-%m-%d"),
        "cutoff_24m":   CUTOFF_24M.strftime("%Y-%m-%d"),
        "stop_reason":  stop_reason,
        "place_info":   place_info,
        "reviews":      reviews,
        "excluded":     excluded,
        "summary": {
            "target":               target,                # 요청된 유효 리뷰 목표 개수
            "total_naver_count":    total_naver_count,    # 네이버 플레이스 표시 전체 수
            "api_max_accessible":   api_max_accessible,   # API 접근 가능 상한 (약 128)
            "total_valid":          len(reviews),
            "count_within_3m":      count_within_3m,
            "count_3m_to_12m":      count_3m_to_12m,
            "count_12m_to_24m":     count_12m_to_24m,
            "recency_bonus_count":  count_within_3m,
            "recency_penalty_count": count_12m_to_24m,    # analyze_reviews 신뢰도 메모용
            "phase2_entered":       phase2_entered,
            "phase2_reason":        f"12개월 이내 {count_within_3m + count_3m_to_12m}개 (목표 {target}개 중)" if phase2_entered else None,
            "target_met":           target_met,
            "total_excluded":       len(excluded),
            "excluded_paid_hard":   sum(1 for e in excluded if e.get("exclude_reason", "").startswith("paid_hard")),
            "excluded_too_old":     sum(1 for e in excluded if e.get("exclude_reason", "").startswith("too_old")),
            "flag_paid_suspect":    sum(1 for r in reviews if any(f.startswith("paid_suspect") for f in r["flags"])),
            "flag_extraction_warn": sum(1 for r in reviews if any(f.startswith("extraction_warning") for f in r["flags"])),
            "fulltext_count":       sum(1 for r in reviews if r.get("body_source") == "fulltext"),
            "fulltext_failed_count": sum(1 for r in reviews if r.get("body_source") != "fulltext"),
            "has_naver_reservation_count": sum(1 for r in reviews if r["has_naver_reservation"]),
            "date_oldest":          min(dates) if dates else "unknown",
            "date_newest":          max(dates) if dates else "unknown",
        },
    }

    path = os.path.join(JSON_DIR, f"{name}_naver_blog.json")
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


def save_blog_txt(name: str, place_id: str, result: dict):
    s = result["summary"]
    lines = []
    lines.extend(format_place_info_txt(result.get("place_info", {})))

    lines.append("=" * 64)
    lines.append(f"{name} (Place ID: {place_id}) — 블로그 리뷰 RAW 데이터")
    lines.append(f"수집 일시  : {COLLECTED_AT}")
    lines.append(f"수집 방식  : 네이버 플레이스 GraphQL(fsasReviews) + 본문 전문 추출")
    lines.append(f"3개월 기준 : {result['cutoff_3m']} 이후 (×1.3)")
    lines.append(f"12개월 기준: {result['cutoff_12m']} 이전 (×0.5)")
    lines.append(f"24개월 기준: {result['cutoff_24m']} 이전 (제외)")
    lines.append(f"종료 사유  : {result['stop_reason']}")
    lines.append("=" * 64)
    lines.append("")
    lines.append("[수집 요약]")
    naver_total = s.get("total_naver_count")
    api_max     = s.get("api_max_accessible")
    naver_str   = f"{naver_total:,}개" if naver_total else "(확인불가)"
    api_str     = f"{api_max:,}개" if api_max else "?"
    lines.append(f"  네이버 전체 리뷰    : {naver_str}")
    lines.append(f"  API 접근 가능 상한  : {api_str}  (최신순, 네이버 제한)")
    lines.append(f"  수집 유효 리뷰      : {s['total_valid']}개  (분석 대상)")
    lines.append(f"    - 3개월 이내      : {s['count_within_3m']}개  (가중치 ×1.3)")
    lines.append(f"    - 3~12개월        : {s['count_3m_to_12m']}개  (가중치 ×1.0)")
    lines.append(f"    - 12~24개월       : {s['count_12m_to_24m']}개  (가중치 ×0.5)")
    lines.append(f"  제외 리뷰 수        : {s['total_excluded']}개")
    lines.append(f"    - 대가성(확정)    : {s['excluded_paid_hard']}개")
    lines.append(f"    - 24개월 초과     : {s.get('excluded_too_old', 0)}개")
    lines.append(f"  ⚠️ 대가성 의심 플래그: {s['flag_paid_suspect']}개")
    lines.append(f"  ⚠️ 추출 의심 플래그  : {s['flag_extraction_warn']}개")
    lines.append(f"  본문 전문 추출      : {s.get('fulltext_count', 0)}개 성공 / "
                 f"{s.get('fulltext_failed_count', 0)}개 프리뷰 폴백")
    lines.append(f"  네이버예약 연동      : {s['has_naver_reservation_count']}개")
    lines.append(f"  수집 기간           : {s['date_oldest']} ~ {s['date_newest']}")
    if s["phase2_entered"]:
        lines.append(f"  ⚠️ 12개월 이내 부족  : {s['phase2_reason']}")
    lines.append(f"  목표({s.get('target', '?')}개)        : {'✅ 달성' if s['target_met'] else '❌ 미달성'}")
    lines.append("")
    lines.append("  ※ 본문은 blog.naver.com 원문에서 추출한 전문입니다.")
    lines.append("     (추출 실패 시 네이버 API 프리뷰로 폴백 → 플래그 fulltext_failed)")
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
                     f"해시태그 {r['hashtag_count']}개 / 전화 {'있음' if r['has_phone'] else '없음'}"
                     f"{' / 네이버예약' if r['has_naver_reservation'] else ''} / 본문={src}")
        lines.append(f"  본문   : {r['body']}")
        lines.append("")

    path = os.path.join(TXT_DIR, f"{name}_naver_blog.txt")
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

async def main():
    start_time  = time.time()
    results_log = []

    restaurants = prompt_restaurants()
    if not restaurants:
        return

    mode = prompt_mode()
    do_visitor = mode in ("all", "visitor")
    do_blog    = mode in ("all", "blog")

    # 수집 개수는 한 번만 입력받아 모든 음식점에 동일 적용 (유효 리뷰 기준)
    v_target, b_target = prompt_review_counts(mode)

    print()
    print(f"총 {len(restaurants)}개 음식점 / 모드: {mode} / "
          f"방문자 {v_target}개 · 블로그 {b_target}개 (유효 기준)")
    print("=" * 64)

    total_rest = len(restaurants)
    for idx, r in enumerate(restaurants, 1):
        name           = r["name"]
        place_id_input = r["place_id_or_url"]
        prog           = f"[{idx}/{total_rest}]"

        print("\n" + "━" * 64)
        print(f"▶ {prog} {name}")
        print("━" * 64)

        # place_id 확정 (단축 URL 처리)
        if re.fullmatch(r"\d+", place_id_input):
            place_id = place_id_input
        else:
            print("  ▷ Place ID 확인 중...", end="\r")
            async with async_playwright() as pw:
                browser, context = await make_browser_context(pw)
                page = await context.new_page()
                place_id = await resolve_place_id(page, place_id_input)
                await context.close()
                await browser.close()
            if place_id == "unknown":
                print(f"  ❌ {prog} Place ID 확인 실패. 건너뜁니다.")
                if do_visitor:
                    results_log.append({"name": name, "type": "방문자", "target_met": False,
                                        "collected": 0, "target": v_target, "note": "Place ID 확인 실패"})
                if do_blog:
                    results_log.append({"name": name, "type": "블로그", "target_met": False,
                                        "collected": 0, "target": b_target, "note": "Place ID 확인 실패"})
                continue

        # ① 매장정보 수집 (리뷰 수집 전 1회)
        place_info = await collect_place_info(place_id, name)

        # ② 방문자 리뷰
        if do_visitor:
            raw_v, total_v = await collect_visitor(place_id, name, v_target)
            result = save_visitor_json(name, place_id, raw_v, place_info, total_v, v_target)
            save_visitor_txt(name, place_id, result)
            s = result["summary"]
            results_log.append({
                "name": name, "type": "방문자",
                "target_met": s["target_met"],
                "collected": s["total_collected"], "target": s["target"],
                "note": _fail_note_visitor(result) if not s["target_met"] else "",
            })

        # ③ 블로그 리뷰
        if do_blog:
            raw_b, stop_reason, total_b, max_b = await collect_blog(place_id, name, b_target)
            if raw_b:
                result = save_blog_json(name, place_id, raw_b, stop_reason,
                                        place_info, total_b, max_b, b_target)
                save_blog_txt(name, place_id, result)
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
    asyncio.run(main())

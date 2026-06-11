"""
naver_VisitorReviewCollector.py
================================
네이버 플레이스 방문자 리뷰 수집기 (범용)

사용법:
    python naver_VisitorReviewCollector.py

실행하면 음식점 URL을 직접 입력하는 방식으로 동작합니다.
여러 음식점을 한 번에 수집할 수 있으며, 입력을 마치면 순서대로 수집합니다.

출력 파일 (스크립트와 같은 폴더에 저장):
    {음식점명}_visitor_raw.json  — 분석용 (가중치, 제외 리뷰 포함)
    {음식점명}_visitor_raw.txt   — 사용자 확인용 (전체 본문 수록)

사전 준비:
    pip install playwright
    playwright install chromium
"""

import asyncio
import json
import re
import os
from datetime import datetime
from playwright.async_api import async_playwright


# ─────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────

OUTPUT_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw")
os.makedirs(OUTPUT_DIR, exist_ok=True)
_NOW           = datetime.now()
COLLECTED_AT   = _NOW.strftime("%Y-%m-%dT%H:%M:%S+09:00")

TARGET_COUNT   = 150   # 18개월 이내 리뷰 목표 수량
MIN_CHARS      = 20    # 불성실 리뷰 기준 (공백 제외 글자 수)
QUALITY_CHARS  = 120   # 고품질 리뷰 기준 (공백 제외 글자 수)
MAX_REVIEWS    = 500   # 리뷰 수 상한 (초과 시 자동 중단)
NO_CHANGE_LIMIT = 6    # 연속으로 리뷰 수 변화 없으면 로딩 종료

PAID_KEYWORDS = [
    "리뷰이벤트", "홍보", "광고", "체험단", "협찬",
    "무상 제공", "이벤트 당첨", "기자단",
]


# ─────────────────────────────────────────────
# 기간 컷오프 (실행 시점 기준 자동 계산)
# ─────────────────────────────────────────────

def _months_ago(months: int) -> datetime:
    y, m = divmod(_NOW.month - months, 12)
    if m <= 0:
        m += 12
        y -= 1
    return _NOW.replace(year=_NOW.year + y, month=m, day=min(_NOW.day, 28))

CUTOFF_18M = _months_ago(18)   # 18개월 초과 → 가중치 ×0.5
CUTOFF_24M = _months_ago(24)   # 24개월 초과 → 수집 제외


# ─────────────────────────────────────────────
# URL 파싱 → Place ID / 음식점명 추출
# ─────────────────────────────────────────────

def parse_input(user_input: str) -> tuple[str, str] | None:
    """
    입력값에서 (place_id, name) 추출.
    지원 형식:
      - https://naver.me/xxxxxx          (단축 URL → place_id 없음, 브라우저 리다이렉트로 확인)
      - https://map.naver.com/p/entry/place/12345678
      - https://m.place.naver.com/restaurant/12345678
      - 12345678                          (Place ID 직접 입력)
    name은 URL에서 추출 불가능하므로 별도로 입력받음.
    반환: (place_id_or_url, name)
    """
    s = user_input.strip()

    # Place ID 직접 입력 (숫자만)
    if re.fullmatch(r"\d+", s):
        return s, None

    # map.naver.com 또는 m.place.naver.com URL에서 ID 추출
    m = re.search(r"/(?:place|restaurant)/(\d+)", s)
    if m:
        return m.group(1), None

    # 단축 URL (naver.me) 또는 기타 URL → 그대로 반환 (브라우저에서 리다이렉트 처리)
    if s.startswith("http"):
        return s, None

    return None


# ─────────────────────────────────────────────
# 사용자 입력 인터페이스
# ─────────────────────────────────────────────

def prompt_restaurants() -> list[dict]:
    """
    터미널에서 음식점 URL과 이름을 입력받아 리스트로 반환.
    """
    print("=" * 64)
    print("네이버 플레이스 방문자 리뷰 수집기")
    print(f"실행 시각  : {COLLECTED_AT}")
    print(f"18개월 기준: {CUTOFF_18M.strftime('%Y-%m-%d')} 이후")
    print(f"24개월 기준: {CUTOFF_24M.strftime('%Y-%m-%d')} 이후")
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

        # URL 입력
        while True:
            url_input = input("  URL 또는 Place ID (입력 없이 Enter → 수집 시작): ").strip()
            if not url_input:
                break
            parsed = parse_input(url_input)
            if parsed is None:
                print("  ❌ 인식할 수 없는 형식입니다. 다시 입력해주세요.")
                continue
            place_id_or_url = parsed[0]
            break

        if not url_input:
            break

        # 음식점 이름 입력
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
# ─────────────────────────────────────────────

def parse_korean_date(date_str: str) -> datetime | None:
    m = re.match(r"(\d{4})년\s+(\d+)월\s+(\d+)일", date_str)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


# ─────────────────────────────────────────────
# 가중치 계산
# ─────────────────────────────────────────────

def calc_weight(content: str, date_str: str) -> dict:
    char_count = len(re.sub(r"\s", "", content))
    dt = parse_korean_date(date_str)
    date_iso = dt.strftime("%Y-%m-%d") if dt else date_str

    weight = 1.0
    recency_penalty = False
    quality_bonus   = False
    exclude_reason  = None

    # 제외 조건 1: 대가성 키워드
    for kw in PAID_KEYWORDS:
        if kw in content:
            exclude_reason = f"paid:{kw}"
            break

    # 제외 조건 2: 불성실 (본문 있는데 20자 미만)
    if not exclude_reason and char_count > 0 and char_count < MIN_CHARS:
        exclude_reason = "insincere"

    # 제외 조건 3: 24개월 초과
    if not exclude_reason and dt and dt < CUTOFF_24M:
        exclude_reason = "too_old:24개월_초과"

    # 가중치 계산 (유효 리뷰만)
    if not exclude_reason:
        if dt and dt < CUTOFF_18M:
            weight *= 0.5   # 18개월 초과 ~ 24개월 이내: 신선도 페널티
            recency_penalty = True
        if char_count >= QUALITY_CHARS:
            weight *= 1.5   # 120자 이상: 고품질 보너스
            quality_bonus = True

    return {
        "date_iso":        date_iso,
        "char_count":      char_count,
        "weight":          round(weight, 2),
        "recency_penalty": recency_penalty,
        "quality_bonus":   quality_bonus,
        "exclude_reason":  exclude_reason,
    }


# ─────────────────────────────────────────────
# 리뷰 수집 (Playwright)
# ─────────────────────────────────────────────

async def collect(place_id_or_url: str, name: str) -> list[dict]:
    # Place ID이면 URL 조립, 아니면 그대로 사용
    if re.fullmatch(r"\d+", place_id_or_url):
        url = f"https://m.place.naver.com/restaurant/{place_id_or_url}/review/visitor?reviewSort=recent"
    else:
        # 단축 URL 등 → 브라우저가 리다이렉트 처리
        url = place_id_or_url

    print(f"\n[{name}] 수집 시작 → {url}")

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
        await page.goto(url)
        await page.wait_for_timeout(2000)

        # 단축 URL이었을 경우 리다이렉트 후 리뷰 탭으로 이동
        current_url = page.url
        place_id_match = re.search(r"/(?:place|restaurant)/(\d+)", current_url)
        if place_id_match:
            place_id = place_id_match.group(1)
            review_url = f"https://m.place.naver.com/restaurant/{place_id}/review/visitor?reviewSort=recent"
            if current_url != review_url:
                await page.goto(review_url)
                await page.wait_for_timeout(2000)
        else:
            place_id = "unknown"

        # 더보기 버튼 반복 클릭으로 리뷰 로딩
        prev_count   = 0
        no_change    = 0
        total_clicks = 0

        while True:
            btn = await page.query_selector("a.fvwqf")
            if btn:
                await btn.click()
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

            print(f"  로딩 중... {count}개", end="\r")

            if count >= MAX_REVIEWS:
                print(f"\n  상한 도달: {count}개 → 로딩 중단")
                break

            if count == prev_count:
                no_change += 1
                if no_change >= NO_CHANGE_LIMIT:
                    break
            else:
                no_change  = 0
                prev_count = count

        print(f"\n  로딩 완료: {prev_count}개 (클릭 {total_clicks}회)")

        # CSS 잘림 제거 (리뷰 전문 노출)
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

        # 전체 리뷰 추출
        raw = await page.evaluate("""
            () => {
                const ul = document.querySelector('ul.OTi6Q');
                if (!ul) return [];
                return Array.from(ul.querySelectorAll('li.EjjAW')).map((el, i) => {
                    const contentDiv = el.querySelector('div.pui__vn15t2');
                    let content = contentDiv ? contentDiv.innerText.trim() : '';
                    content = content.replace(/\\n?더보기\\s*$/, '').trim();

                    let date = '';
                    el.querySelectorAll('span.pui__blind').forEach(s => {
                        if (/\\d{4}년/.test(s.innerText)) date = s.innerText.trim();
                    });

                    const tags = Array.from(el.querySelectorAll('span.pui__V8F9nN'))
                        .map(s => s.innerText.trim()).filter(Boolean);

                    const keywords = Array.from(el.querySelectorAll('span.pui__jhpEyP'))
                        .map(s => s.innerText.trim()).filter(Boolean);

                    const authorEl = el.querySelector('span.pui__NMi-Dp');
                    const author = authorEl ? authorEl.innerText.trim() : '';

                    return { seq: i + 1, author, date, content, tags, keywords };
                });
            }
        """)

        await context.close()
        await browser.close()

    # place_id 보정 (단축 URL 입력 케이스)
    if place_id == "unknown" and re.fullmatch(r"\d+", place_id_or_url):
        place_id = place_id_or_url

    print(f"  추출 완료: {len(raw)}개  (Place ID: {place_id})")
    return raw, place_id


# ─────────────────────────────────────────────
# JSON 저장 (분석용)
# ─────────────────────────────────────────────

def save_json(name: str, place_id: str, raw: list[dict]) -> dict:
    reviews  = []
    excluded = []

    for r in raw:
        w = calc_weight(r["content"], r["date"])
        entry = {
            "id":              r["seq"],
            "author":          r["author"],
            "date":            w["date_iso"],
            "date_raw":        r["date"],
            "content":         r["content"],
            "char_count":      w["char_count"],
            "rating":          None,  # 네이버 방문자 리뷰는 별점 없음
            "tags":            r["tags"],
            "keywords":        r["keywords"],
            "recency_penalty": w["recency_penalty"],
            "quality_bonus":   w["quality_bonus"],
            "weight":          w["weight"],
        }
        if w["exclude_reason"]:
            entry["exclude_reason"] = w["exclude_reason"]
            excluded.append(entry)
        else:
            reviews.append(entry)

    count_18m       = sum(1 for r in reviews if not r["recency_penalty"])
    extended_to_24m = count_18m < TARGET_COUNT
    dates = [r["date"] for r in reviews if re.match(r"\d{4}-\d{2}-\d{2}", r["date"])]

    result = {
        "restaurant":   name,
        "place_id":     place_id,
        "collected_at": COLLECTED_AT,
        "review_type":  "visitor",
        "cutoff_18m":   CUTOFF_18M.strftime("%Y-%m-%d"),
        "cutoff_24m":   CUTOFF_24M.strftime("%Y-%m-%d"),
        "reviews":      reviews,
        "excluded":     excluded,
        "summary": {
            "total_collected":        len(reviews),
            "count_within_18m":       count_18m,
            "count_18m_to_24m":       sum(1 for r in reviews if r["recency_penalty"]),
            "extended_to_24m":        extended_to_24m,
            "extended_to_24m_reason": f"18개월 이내 리뷰 {count_18m}개 < 목표 {TARGET_COUNT}개" if extended_to_24m else None,
            "total_excluded":         len(excluded),
            "excluded_paid":          sum(1 for e in excluded if e.get("exclude_reason", "").startswith("paid")),
            "excluded_insincere":     sum(1 for e in excluded if e.get("exclude_reason") == "insincere"),
            "excluded_too_old":       sum(1 for e in excluded if e.get("exclude_reason", "").startswith("too_old")),
            "quality_bonus_count":    sum(1 for r in reviews if r["quality_bonus"]),
            "date_oldest":            min(dates) if dates else "unknown",
            "date_newest":            max(dates) if dates else "unknown",
            "target_met":             len(reviews) >= TARGET_COUNT,
        },
    }

    path = os.path.join(OUTPUT_DIR, f"{name}_visitor_raw.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"  JSON 저장: {path}")
    print(f"  유효 {len(reviews)}개 (18개월 이내 {count_18m}개 / 18~24개월 {len(reviews)-count_18m}개) / 제외 {len(excluded)}개")
    if extended_to_24m:
        print(f"  ⚠️  18개월 이내 {count_18m}개 < {TARGET_COUNT}개 → 24개월까지 확장 수집됨")
    return result


# ─────────────────────────────────────────────
# TXT 저장 (사용자 확인용 — 전체 본문 수록)
# ─────────────────────────────────────────────

def save_txt(name: str, place_id: str, result: dict):
    s = result["summary"]
    lines = []

    lines.append("=" * 64)
    lines.append(f"{name} (Place ID: {place_id}) — 방문자 리뷰 RAW 데이터")
    lines.append(f"수집 일시  : {COLLECTED_AT}")
    lines.append(f"수집 URL   : https://m.place.naver.com/restaurant/{place_id}/review/visitor?reviewSort=recent")
    lines.append(f"18개월 기준: {result['cutoff_18m']} 이후")
    lines.append(f"24개월 기준: {result['cutoff_24m']} 이후")
    lines.append("=" * 64)
    lines.append("")
    lines.append("[수집 요약]")
    lines.append(f"  유효 리뷰 수      : {s['total_collected']}개")
    lines.append(f"    - 18개월 이내   : {s['count_within_18m']}개  (가중치 ×1.0)")
    lines.append(f"    - 18~24개월     : {s['count_18m_to_24m']}개  (가중치 ×0.5)")
    lines.append(f"  제외 리뷰 수      : {s['total_excluded']}개")
    lines.append(f"    - 대가성        : {s['excluded_paid']}개")
    lines.append(f"    - 불성실(20자↓) : {s['excluded_insincere']}개")
    lines.append(f"    - 24개월 초과   : {s['excluded_too_old']}개")
    lines.append(f"  120자 이상(×1.5)  : {s['quality_bonus_count']}개")
    lines.append(f"  수집 기간         : {s['date_oldest']} ~ {s['date_newest']}")
    lines.append(f"  목표(150개+)      : {'✅ 달성' if s['target_met'] else '❌ 미달성'}")
    if s["extended_to_24m"]:
        lines.append(f"  ⚠️  24개월 확장   : {s['extended_to_24m_reason']}")
    else:
        lines.append(f"  수집 범위         : 18개월 이내만 수집 (목표 달성)")
    lines.append("")

    # 제외 리뷰 목록
    if result["excluded"]:
        lines.append("=" * 64)
        lines.append("[제외된 리뷰 목록]")
        lines.append("=" * 64)
        for r in result["excluded"]:
            lines.append(f"[{r['id']:04d}] {r['author']} | {r['date']} | 제외사유: {r['exclude_reason']}")
            lines.append(f"  본문: {r['content'][:80]}{'...' if len(r['content']) > 80 else ''}")
            lines.append("")

    # 전체 리뷰 본문
    lines.append("=" * 64)
    lines.append(f"[방문자 리뷰 전체 {s['total_collected']}개 — 최신순]")
    lines.append("=" * 64)
    lines.append("")

    for r in result["reviews"]:
        weight_info = []
        if r["recency_penalty"]: weight_info.append("18~24개월×0.5")
        if r["quality_bonus"]:   weight_info.append("120자이상×1.5")
        weight_str = f"  [가중치:{r['weight']}" + (f" / {', '.join(weight_info)}]" if weight_info else "]")

        lines.append(f"[{r['id']:04d}] 작성자: {r['author']} | 날짜: {r['date']}{weight_str}")
        if r["tags"]:
            lines.append(f"  태그   : {' / '.join(r['tags'])}")
        if r["keywords"]:
            lines.append(f"  키워드 : {', '.join(r['keywords'])}")
        lines.append(f"  본문   : {r['content']}")
        lines.append("")

    path = os.path.join(OUTPUT_DIR, f"{name}_visitor_raw.txt")
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
        raw, place_id = await collect(r["place_id_or_url"], r["name"])
        result = save_json(r["name"], place_id, raw)
        save_txt(r["name"], place_id, result)
        print(f"\n✅ [{r['name']}] 완료\n")

    print("=" * 64)
    print(f"모든 수집 완료. 저장 위치: {OUTPUT_DIR}")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())

"""
리뷰 분석 전처리 스크립트 (재사용)
====================================
역할: raw.json을 받아 토큰 효율적인 '정량 집계 + 대표 샘플'만 출력한다.
      Claude Code는 이 출력만 읽고 정성 분석을 수행한다(전체 JSON을 컨텍스트에 올리지 않음).

사용:
    python analyze_reviews.py "reviews_json\\피탕김탕_naver_visitor.json"
    python analyze_reviews.py <json경로> [--samples 12]

원칙(CLAUDE.md §3,§4 준수):
    - exclude_reason 보유 리뷰는 제외(excluded[]는 무시, reviews[]도 방어적 재확인).
    - weight는 재계산하지 않고 그대로 사용한다(영향력 가중치).
    - 의견은 제거/변형하지 않고 모두 수렴하되 영향력 크기만 weight로 반영.
"""

import sys
import json
import re
import argparse
from collections import Counter, defaultdict

# ─── 감성 사전 (1차 추정용 — 경계 사례는 Claude가 본문으로 검증) ───
POS_WORDS = [
    "맛있", "맛집", "좋았", "좋아요", "최고", "추천", "친절", "깔끔", "신선", "푸짐",
    "재방문", "또 가", "또가", "만족", "정갈", "분위기 좋", "가성비", "훌륭", "괜찮",
    "넉넉", "부드럽", "고소", "감동", "단골", "정성", "양 많",
]
NEG_WORDS = [
    "별로", "실망", "불친절", "최악", "비싸", "비쌈", "짜", "싱겁", "느끼", "위생",
    "더럽", "다신 안", "다시 안", "안 가", "안가", "맛없", "질겨", "딱딱", "불쾌",
    "느림", "오래 기다", "오래기다", "웨이팅", "양 적", "양이 적", "아쉬", "그닥",
    "그저", "퍽퍽", "비위생",
]

# ─── 메뉴 후보 사전 (등장 시 카운트; 필요 시 음식점별로 보강) ───
MENU_HINTS = [
    "탕수육", "김탕", "피탕", "짜장", "짬뽕", "볶음밥", "군만두", "탕면", "유린기",
    "깐풍기", "양장피", "팔보채", "기스면", "울면", "고추잡채", "꽃빵", "마파두부",
    "라조기", "동파육", "냉채", "삼선", "공깃밥", "셋트", "세트", "코스",
]


def load_reviews(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("reviews", data if isinstance(data, list) else [])
    # 방어적 재확인: exclude_reason 있는 것은 제외
    reviews = [r for r in raw if not r.get("exclude_reason")]
    # 본문 필드 정규화: 방문자는 content, 블로그는 body → 둘 다 content로 통일
    for r in reviews:
        if not r.get("content"):
            r["content"] = r.get("body", "")
    return data, reviews


def sentiment(content):
    """가중 전 단순 라벨: pos / neg / neu (사전 기반 1차 추정)."""
    p = sum(content.count(w) for w in POS_WORDS)
    n = sum(content.count(w) for w in NEG_WORDS)
    if p > n:
        return "pos"
    if n > p:
        return "neg"
    return "neu"


def weighted_counter(items_per_review, weights):
    c = Counter()
    for items, w in zip(items_per_review, weights):
        for it in items:
            c[it] += w
    return c


def month_of(date_str):
    m = re.match(r"(\d{4})-(\d{2})", date_str or "")
    return f"{m.group(1)}-{m.group(2)}" if m else "unknown"


def analyze(path, n_samples=12):
    data, reviews = load_reviews(path)
    summary = data.get("summary", {})
    n = len(reviews)

    if n == 0:
        print(json.dumps({"error": "분석 가능한 리뷰가 없습니다", "path": path},
                         ensure_ascii=False, indent=2))
        return

    weights = [r.get("weight", 1.0) for r in reviews]
    total_w = sum(weights)

    # ── 가중 감성 분포 ──
    sent_w = Counter()
    for r, w in zip(reviews, weights):
        sent_w[sentiment(r.get("content", ""))] += w
    sent_pct = {k: round(100 * sent_w[k] / total_w, 1) for k in ("pos", "neg", "neu")}

    # ── 가중 평균 평점 (별점 있을 때만) ──
    rated = [(r["rating"], w) for r, w in zip(reviews, weights)
             if r.get("rating") is not None]
    avg_rating = (round(sum(rt * w for rt, w in rated) / sum(w for _, w in rated), 2)
                  if rated else None)

    # ── 키워드 / 태그 가중 빈도 ──
    kw = weighted_counter([r.get("keywords", []) for r in reviews], weights)
    tg = weighted_counter([r.get("tags", []) for r in reviews], weights)

    # ── 메뉴 언급 가중 빈도 ──
    menu = Counter()
    for r, w in zip(reviews, weights):
        c = r.get("content", "")
        for m in MENU_HINTS:
            if m in c:
                menu[m] += w

    # ── 월별 분포 + 최신/과거 감성 ──
    by_month = defaultdict(lambda: {"count": 0, "weight": 0.0})
    for r, w in zip(reviews, weights):
        mk = month_of(r.get("date"))
        by_month[mk]["count"] += 1
        by_month[mk]["weight"] += w
    months_sorted = sorted(m for m in by_month if m != "unknown")
    half = len(reviews) // 2
    ordered = sorted(reviews, key=lambda r: r.get("date", ""))
    old_half, new_half = ordered[:half], ordered[half:]

    def half_sent(group):
        if not group:
            return {}
        tw = sum(r.get("weight", 1.0) for r in group)
        sc = Counter()
        for r in group:
            sc[sentiment(r.get("content", ""))] += r.get("weight", 1.0)
        return {k: round(100 * sc[k] / tw, 1) for k in ("pos", "neg", "neu")}

    # ── 신뢰도 메모 ──
    rp = summary.get("recency_penalty_count")
    rp_pct = round(100 * rp / n, 1) if isinstance(rp, int) and n else None

    # ── 대표 리뷰 샘플 (weight 상위 + 긍/부정 양극단) ──
    def slim(r):
        return {"id": r.get("id"), "date": r.get("date"), "weight": r.get("weight"),
                "sent": sentiment(r.get("content", "")),
                "char_count": r.get("char_count"),
                "content": r.get("content", "")}

    top_w = sorted(reviews, key=lambda r: r.get("weight", 0), reverse=True)[:n_samples // 2]
    pos = [r for r in reviews if sentiment(r.get("content", "")) == "pos"]
    neg = [r for r in reviews if sentiment(r.get("content", "")) == "neg"]
    pos_top = sorted(pos, key=lambda r: r.get("weight", 0), reverse=True)[:n_samples // 4]
    neg_top = sorted(neg, key=lambda r: r.get("weight", 0), reverse=True)[:n_samples // 4]
    seen, samples = set(), []
    for r in top_w + neg_top + pos_top:
        if r.get("id") not in seen:
            seen.add(r.get("id"))
            samples.append(slim(r))

    out = {
        "restaurant": data.get("restaurant"),
        "review_type": data.get("review_type"),
        "n_reviews": n,
        "total_weight": round(total_w, 2),
        "avg_rating_weighted": avg_rating,
        "sentiment_pct_weighted": sent_pct,
        "top_keywords_weighted": kw.most_common(15),
        "top_tags_weighted": tg.most_common(15),
        "menu_mentions_weighted": menu.most_common(15),
        "by_month": {m: {"count": by_month[m]["count"],
                         "weight": round(by_month[m]["weight"], 2)}
                     for m in months_sorted},
        "trend": {"old_half_sentiment": half_sent(old_half),
                  "new_half_sentiment": half_sent(new_half)},
        "reliability": {
            "recency_penalty_count": rp,
            "recency_penalty_pct": rp_pct,
            "quality_bonus_count": summary.get("quality_bonus_count"),
            "target_met": summary.get("target_met"),
            "date_range": [summary.get("date_oldest"), summary.get("date_newest")],
            "total_excluded": summary.get("total_excluded"),
        },
        "representative_samples": samples,
        "_note": "sentiment은 사전 기반 1차 추정. 경계 사례는 본문으로 검증할 것.",
    }
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--samples", type=int, default=12)
    args = ap.parse_args()
    analyze(args.json_path, args.samples)

# 네이버 음식점 리뷰 분석 프로젝트

이 프로젝트에서 Claude Code의 역할은 **"분석"만** 담당한다. 리뷰 수집은 로컬에서
`naver_ReviewCollector.py`로 별도 수행되며, 그 산출물(JSON)을 입력으로 받아 분석한다.

> 이 문서는 Claude Code가 직접 생성·관리한다. 스키마가 바뀌거나(수집 스크립트 수정)
> 분석 항목이 추가되면 **이 CLAUDE.md를 즉시 수정해 최신 상태로 유지**한다.

---

## 1. 프로젝트 구조

```
C:\Users\jh960\Desktop\리뷰분석\
├── CLAUDE.md                              ← 이 파일 (Claude가 관리)
├── naver_ReviewCollector.py               ← **통합 수집 스크립트** (매장정보 + 방문자 + 블로그)
├── analyze_reviews.py                     ← 분석 전처리 스크립트 (Claude가 재사용)
├── raw\                                   ← **수집된 raw 파일 저장 폴더 (수집기가 자동 저장)**
│   ├── {음식점 이름}_visitor_raw.json     (방문자 리뷰 + place_info, **분석 입력**)
│   ├── {음식점 이름}_visitor_raw.txt      (사람 확인용)
│   ├── {음식점 이름}_blog_raw.json        (블로그 리뷰 + place_info, 있을 경우만)
│   └── {음식점 이름}_blog_raw.txt
└── analyzeReport\                         ← 분석 리포트 HTML 저장 폴더
    └── {음식점 이름}_분석리포트.html
```

- 사용자가 **"{음식점 이름} 분석해줘"** 라고 하면 `raw\` 폴더의 `*_raw.json`을 읽어 분석한다.
- **`.txt`는 분석 입력으로 절대 사용하지 않는다.** 단, 사용자가 직접 요청하거나 특별히 필요한 경우에만 참조한다.
- 실제 파일 위치는 분석 직전 `Glob`로 확인한다 (`raw\**\*_raw.json`).

---

## 2. JSON 스키마 (수집 단계에서 이미 생성됨)

가중치·필터는 **수집 단계에서 이미 계산**되어 있다. 재계산하지 말고 필드 값을 그대로 쓴다.

### 최상위 객체
| 필드 | 의미 |
|------|------|
| `restaurant` | 음식점 이름 |
| `place_id` | 네이버 플레이스 ID |
| `collected_at` | 수집 시각 (ISO8601) |
| `review_type` | `"visitor"` 또는 `"blog"` |
| `place_info` | **매장 기본정보 객체** (아래 참조) |
| `reviews` | **분석 대상 리뷰 배열** (exclude_reason 없는 것만) |
| `excluded` | 제외된 리뷰 배열 (exclude_reason 보유) — **분석에서 무시** |
| `summary` | 정량 요약 객체 (아래) |

### place_info 객체 (방문자·블로그 JSON 공통)
수집기: `naver_ReviewCollector.py` — 수집 1회 후 visitor/blog JSON 양쪽에 동일하게 포함.

| 필드 | 의미 |
|------|------|
| `category` | 네이버 플레이스 카테고리 |
| `address` | 도로명 주소 |
| `phone` | 전화번호 |
| `homepage` | 홈페이지/SNS URL (인스타그램 등, 없으면 null) |
| `naver_booking` | 네이버예약 버튼 존재 여부 (bool) |
| `regular_holiday` | 정기휴무 텍스트 (예: "정기휴무 (매주 월요일)") |
| `break_time` | 브레이크타임 (예: "15:00 - 16:30") |
| `last_order` | 라스트오더 시각 (예: "20:10") |
| `parking` | 주차 정보 텍스트 (편의시설 목록에서 추출) |
| `hours` | 요일별 영업시간 배열 `[{day, open, close}]` |
| `raw_hours_text` | 영업시간 원문 텍스트 (파싱 실패 시 참조용) |
| `facilities` | 편의시설 목록 (쉼표 구분, 예: ["포장", "무선 인터넷", "주차"]) |
| `total_visitor_reviews` | 네이버 플레이스 표시 방문자 리뷰 총 수 (int, 없으면 null) |
| `total_blog_reviews` | 네이버 플레이스 표시 블로그 리뷰 총 수 (int, 없으면 null) |
| `menus` | 메뉴·가격 목록 `[{name, price, description}]` (최대 60개) |
| `errors` | 수집 중 발생한 오류 목록 (빈 배열이면 정상) |

> ⚠️ `place_info`는 DOM/API 선택자가 네이버 업데이트로 바뀔 수 있다.
> `errors` 배열이 비어 있어도 일부 필드가 `null`일 수 있으니 분석 전 값 존재 여부를 확인한다.
> 필드가 `null`이거나 `hours`가 빈 배열이면 `raw_hours_text`를 참조한다.

> ⚠️ 수집 스크립트가 **이미** `exclude_reason` 보유 리뷰를 `excluded[]`로 분리해 둔다.
> 따라서 분석은 `reviews[]`만 읽으면 되며, `excluded[]`는 신뢰도 메모 외에는 쓰지 않는다.
> 만약 향후 스키마가 단일 배열로 바뀌면, `exclude_reason` 필드 유무로 직접 필터링한다.

### (A) 방문자 리뷰 객체 — `review_type: "visitor"`
수집기: `naver_ReviewCollector.py` — `collect_visitor()` (플레이스 방문자 리뷰 탭 DOM)

| 필드 | 의미 |
|------|------|
| `id` | 리뷰 순번 |
| `author` | 작성자 |
| `date` / `date_raw` | 작성일 `YYYY-MM-DD` / 원본 한글 날짜 |
| `content` | 리뷰 본문 전문 |
| `char_count` | 공백 제외 글자 수 |
| `rating` | 별점 (방문자 리뷰는 항상 `null`) |
| `tags` / `keywords` | 네이버 제공 태그·키워드 |
| `recency_penalty` | `true`면 18개월 초과 → weight ×0.5 적용됨 |
| `quality_bonus` | `true`면 120자 이상 → weight ×1.5 적용됨 |
| `weight` | 위 조건이 곱연산된 **최종 가중치** |
| `exclude_reason` | (보통 `excluded[]`에만 존재) `paid:*` / `insincere` / `too_old:*` |

### (B) 블로그 리뷰 객체 — `review_type: "blog"`
수집기: `naver_BlogReviewCollector.py` (**플레이스 GraphQL `fsasReviews` API**, 2026-06 재작성)

| 필드 | 의미 |
|------|------|
| `id` | 리뷰 순번 |
| `author` | 블로그 작성자명 |
| `date` / `date_raw` | 작성일 `YYYY-MM-DD` / 원본(`createdString` 예: "26.6.10.수") |
| `title` | 블로그 글 제목 (HTML 엔티티 디코딩됨) |
| `url` / `blog_id` / `log_no` | 원문 링크 및 식별자 |
| `body` | 블로그 본문 — **API 프리뷰(~1300자 상한)**. 짧은 글은 전문, 긴 글은 앞부분만 |
| `char_count` | 공백 제외 글자 수 |
| `image_count` | 첨부 이미지 수(`thumbnailCount`) — 자동 감점 안 함, 메타데이터 |
| `hashtag_count` / `has_phone` | 해시태그 수 / 전화번호 포함 여부 (협찬 판단 보조) |
| `has_naver_reservation` | 네이버예약 연동 글 여부 |
| `recency_penalty` | `true`면 18개월 초과 → weight ×0.5 |
| `weight` | 최종 가중치 (**블로그는 신선도만 반영**, 길이 보너스 없음) |
| `flags` | `paid_suspect:*`(의심·제외 안 함) / `extraction_warning:N자`(본문 추출 의심) |
| `exclude_reason` | (보통 `excluded[]`에만 존재) `paid_hard:*` |

> ⚠️ 블로그 본문(`body`)은 전문이 아니라 프리뷰일 수 있다(긴 글). 메뉴·감성·운영정보 추출엔 충분하나,
> "본문에 X가 없다"는 단정은 피한다. `content`가 아니라 **`body`** 필드임에 주의.
> `analyze_reviews.py`는 visitor의 `content`를 읽으므로, 블로그 분석 시 `body`를 함께 처리하도록 보강 필요.

### `summary` 객체
- **공통**: `total_excluded`, `date_oldest`, `date_newest`, `count_within_18m`, `count_18m_to_24m`
- **방문자**: `total_collected`, `excluded_paid`, `excluded_insincere`, `excluded_too_old`,
  `quality_bonus_count`, `target_met`, `extended_to_24m`, `extended_to_24m_reason`
- **블로그**: `total_valid`, `excluded_paid_hard`, `flag_paid_suspect`, `flag_extraction_warn`,
  `has_naver_reservation_count`, `phase2_entered`, `phase2_reason`, `min_target_met`, `max_target_met`
  (최상위에 `source: "place_graphql_fsasReviews"`, `stop_reason`도 포함)

> ⚠️ 블로그는 네이버 API가 **최근순 약 128개(`maxItemCount`)까지만** 접근 허용. total이 수천이어도
> 그 이상은 못 받는다 → 18~24개월 목표 수집엔 충분하나 "전수"가 아님을 신뢰도 메모에 반영한다.

> ⚠️ 스키마 변경(2026-06): 구버전 방문자 `recency_penalty_count`는 더 이상 없다.
> 18개월 초과 비율은 `count_18m_to_24m`(또는 리뷰별 `recency_penalty` 합)로 계산하고,
> 신뢰도 메모는 `count_within_18m` / `date_range`를 우선 근거로 삼는다.

---

## 3. 분석 원칙 (반드시 준수)

1. **`exclude_reason` 보유 리뷰는 분석에서 완전히 제외한다.** (수집 단계의 `excluded[]`가 이에 해당)
2. **각 리뷰의 `weight`를 영향력 가중치로 사용한다.**
   - 가중치는 의견을 제거·변형하지 **않는다.** 추천/비추천 의견은 **모두 수렴**하되,
     전체 평가에 미치는 **영향력의 크기만** weight로 조정한다.
   - weight는 수집 단계 값을 그대로 쓴다. **재계산 금지.**
3. **방문자 리뷰와 블로그 리뷰를 구분해 분석**하되, 마지막에 **종합 의견**도 제시한다.

---

## 4. 토큰 효율 (중요)

`raw.json`은 리뷰 수백 개 규모로 클 수 있다. **전체를 컨텍스트에 그대로 올리지 않는다.**
정량 집계는 `analyze_reviews.py`(재사용 스크립트)로 코드에서 처리하고, Claude는 그 출력 요약과
**선별된 대표 리뷰 샘플만** 읽는다.

### 처리 순서
1. JSON 로드 → `excluded[]` 무시, `reviews[]`만 사용 (방어적으로 `exclude_reason` 재확인).
2. **정량 집계를 코드로 계산:**
   - weight 합계, 가중 평균 평점(별점 있을 때)
   - 가중 기준 긍정/부정/중립 분포 (감성 사전 기반 1차 추정)
   - `keywords`·`tags` 등장 빈도 (weight 반영)
   - 메뉴명 언급 빈도 (weight 반영)
   - 월별 리뷰 분포 및 최신/과거 구간 평가 변화
3. **정성 분석**(뉘앙스, 대표 의견)은 스크립트가 뽑아준 **대표 리뷰 샘플만** 읽는다.
   - 샘플 선정 기준: weight 상위 + 긍/부정 양극단 + 메뉴별 대표.

### 재사용 스크립트
`analyze_reviews.py <json경로>` → 정량 집계 JSON과 대표 샘플을 stdout으로 출력한다.
새 음식점에도 동일 스크립트를 재사용한다. 스키마가 바뀌면 스크립트와 본 문서를 함께 갱신한다.

실행 예:
```
python analyze_reviews.py "피탕김탕\피탕김탕_visitor_raw.json"
```

---

## 5. 분석 출력 형식

항목별 소제목 + 핵심 수치 표 + **마지막 3줄 이내 총평**으로 구성한다.

1. **전반적 평가** — 가중치 반영 긍정/부정/중립 비율
2. **주요 키워드** — 메뉴·분위기·서비스 (긍정/부정 구분)
3. **메뉴 분석** — 언급 빈도 높은 메뉴 및 평가 요약
   - **특정 메뉴 강력 추천**: 여러 리뷰에서 반복 칭찬·강추되는 시그니처 메뉴를 따로 짚는다.
4. **반복 부정 이슈** — 주문 누락/오배송, 불청결·위생, 웨이팅 과다 등 **여러 건에서 반복 확인되는**
   부정 패턴만 추린다. 1~2건 단발성은 제외하고, 반복성·최근성(시계열)을 함께 표기한다.
5. **영업 정보(인지 필요)** — 비정기 휴무, 조기 마감, **조기 재료 소진**, 브레이크타임, 좌식 여부,
   주차, 결제 수단 등 방문 전 알아야 할 운영 사실. 리뷰 본문/태그에서 근거를 인용한다.
6. **예약·대기 방식** — 테이블링/캐치테이블/네이버예약 등 **온라인 예약 가능 여부**, 또는 현장 대기(웨이팅)
   방식인지 설명. 네이버 태그의 `예약 없이 이용`·`대기 시간 …` 분포를 근거로 대기 강도를 추정한다.
7. **시간대별 트렌드** — 최신 vs 과거 평가 변화
8. **신뢰도 메모** — `recency_penalty` 비율이 높으면(예: 30%↑) 데이터 신뢰도 주의 명시.
   `target_met`이 false거나 표본이 작아도 함께 경고.
9. **추천 / 비추천 / 이용팁** — ①추천하는 이유 ②비추천(주의)하는 이유 ③방문 전 이용팁을
   각각 bullet로 정리한다. 데이터에 근거하되 사용자가 바로 행동에 옮길 수 있게 구체적으로.
10. **종합 의견** — 방문자 + 블로그 통합 평가

---

## 6. 운영 메모 (Claude 자가관리)

- 현재 수집 스크립트는 **방문자 리뷰만** 수집한다(`rating` 항상 null, `channel_bonus` 미생성).
  블로그 수집이 추가되면 이 섹션과 §2 스키마를 갱신한다.
- 감성 분포는 사전 기반 1차 추정이므로, 경계 사례는 반드시 대표 샘플 본문으로 검증한다.
- 분석 결과를 단정적으로 제시하기 전, 표본 수와 `recency_penalty` 비율을 항상 확인한다.

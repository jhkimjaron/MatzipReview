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
├── naver_ReviewCollector.py               ← **네이버 통합 수집기** (매장정보 + 방문자 + 블로그, Playwright)
├── kakao_ReviewCollector.py               ← **카카오 통합 수집기** (매장정보 + 후기 + 블로그, urllib/API)
├── analyze_reviews.py                     ← 분석 전처리 스크립트 (네이버·카카오 공용 재사용)
├── reviews_json\                          ← **분석 입력용 JSON 폴더 (수집기가 자동 저장)**
│   ├── {음식점 이름}_naver_visitor.json   (네이버 방문자 리뷰 + place_info, **분석 입력**)
│   ├── {음식점 이름}_naver_blog.json      (네이버 블로그 리뷰(전문) + place_info, 있을 경우만)
│   ├── {음식점 이름}_kakao_visitor.json   (**카카오맵 후기** + place_info, 별점★ 보유)
│   └── {음식점 이름}_kakao_blog.json      (카카오 연동 블로그 리뷰 **전문** + place_info)
├── reviews_txt\                           ← **사람 확인용 TXT 폴더 (분석 입력 아님)**
│   ├── {음식점 이름}_naver_visitor.txt
│   ├── {음식점 이름}_naver_blog.txt
│   ├── {음식점 이름}_kakao_visitor.txt
│   └── {음식점 이름}_kakao_blog.txt
└── Place_Report\                          ← 분석 리포트 HTML 저장 폴더
    └── {음식점 이름}_분석리포트.html
```

> **폴더 분리(2026-06)**: 분석 입력 JSON은 `reviews_json\`, 사람 확인용 TXT는 `reviews_txt\`에
> 각각 저장된다(수집기 `JSON_DIR`/`TXT_DIR`). 분석은 **`reviews_json\`만** 읽는다.

> **출처 구분**: 파일명을 `{이름}_{플랫폼}_{종류}` 형식으로 통일한다 —
> `_naver_visitor` / `_naver_blog` / `_kakao_visitor`(카카오 후기) / `_kakao_blog`.
> 같은 음식점을 네이버·카카오 양쪽에서 수집해도 파일이 충돌하지 않는다.
> JSON 내부 `review_type`은 `visitor`/`blog`/`kakao`로 식별한다(파일명과 별개).

- 사용자가 **"{음식점 이름} 분석해줘"** 라고 하면 `reviews_json\` 폴더의 해당 음식점 `*.json`을 읽어 분석한다.
- **`reviews_txt\`의 `.txt`는 분석 입력으로 절대 사용하지 않는다.** 단, 사용자가 직접 요청하거나 특별히 필요한 경우에만 참조한다.
- 실제 파일 위치는 분석 직전 `Glob`로 확인한다 (`reviews_json\**\*.json`).

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
| `char_count` | **공백 포함** 글자 수 (2026-06 변경) |
| `rating` | 별점 (방문자 리뷰는 항상 `null`) |
| `tags` / `keywords` | 네이버 제공 태그·키워드 |
| `recency_bonus` | `true`면 3개월 이내 → ×1.3 |
| `recency_penalty` | `true`면 12~24개월 → ×0.5 |
| `quality_bonus` | `true`면 40자↑ → ×1.2 / 100자↑ → ×1.5 (공백포함 티어) |
| `weight` | 위 조건이 곱연산된 **최종 가중치** |
| `exclude_reason` | (보통 `excluded[]`에만 존재) `paid:*` / `insincere`(15자 미만) / `too_old:*` |

### (B) 블로그 리뷰 객체 — `review_type: "blog"`
수집기: `naver_BlogReviewCollector.py` (**플레이스 GraphQL `fsasReviews` API**, 2026-06 재작성)

| 필드 | 의미 |
|------|------|
| `id` | 리뷰 순번 |
| `author` | 블로그 작성자명 |
| `date` / `date_raw` | 작성일 `YYYY-MM-DD` / 원본(`createdString` 예: "26.6.10.수") |
| `title` | 블로그 글 제목 (HTML 엔티티 디코딩됨) |
| `url` / `blog_id` / `log_no` | 원문 링크 및 식별자 |
| `body` | 블로그 본문 — **blog.naver.com 원문 전문**(2026-06 변경). 최대 12000자 |
| `body_source` | `"fulltext"`(전문 추출 성공) / `"preview"`(실패→API 프리뷰 폴백, `flags`에 `fulltext_failed`) |
| `char_count` | 공백 제외 글자 수 |
| `image_count` | 첨부 이미지 수(`thumbnailCount`) — 자동 감점 안 함, 메타데이터 |
| `hashtag_count` / `has_phone` | 해시태그 수 / 전화번호 포함 여부 (협찬 판단 보조) |
| `has_naver_reservation` | 네이버예약 연동 글 여부 |
| `recency_bonus` | `true`면 3개월 이내 → ×1.3 |
| `recency_penalty` | `true`면 12~24개월 → ×0.5 |
| `weight` | 최종 가중치 (**블로그는 신선도만 반영**, 길이 보너스 없음) |
| `flags` | `paid_suspect:*`(의심·제외 안 함) / `extraction_warning:N자` / `fulltext_failed` |
| `exclude_reason` | (보통 `excluded[]`에만 존재) `paid_hard:*` / `too_old:24개월_초과` |

> ⚠️ 블로그 신선도 가중치는 방문자와 동일(2026-06): 3개월 이내 ×1.3 / 12~24개월 ×0.5 / 24개월 초과 제외.
> `body`는 **전문**이다(blog.naver.com 직접 추출). `body_source: "preview"`인 글만 프리뷰 폴백이므로
> 그 경우에만 "긴 글 앞부분만"이 적용된다. `content`가 아니라 **`body`** 필드임에 주의.
> `analyze_reviews.py`는 `body`를 `content`로 정규화해 함께 처리한다.

### `summary` 객체
- **공통**: `target`, `total_excluded`, `date_oldest`, `date_newest`,
  `count_within_3m`, `count_3m_to_12m`, `count_12m_to_24m`,
  `recency_bonus_count`, `recency_penalty_count`(= `count_12m_to_24m`, analyze_reviews 신뢰도용),
  `quality_bonus_count`
  - `target` = 수집 시 사용자가 지정한 **유효 리뷰 목표 개수**(미입력 시 기본값 방문자 150 / 블로그 50).
    `reviews[]`는 정확히 이 개수로 맞춰져 있다(초과 수집분은 최신순 뒤쪽부터 잘림). 목표보다 적으면 데이터 부족.
- **방문자**: `total_collected`, `total_loaded`, `excluded_paid`, `excluded_insincere`(15자미만),
  `excluded_too_old`, `target_met`
- **블로그**: `total_valid`, `excluded_paid_hard`, `flag_paid_suspect`, `flag_extraction_warn`,
  `has_naver_reservation_count`, `phase2_entered`, `phase2_reason`, `target_met`
  (최상위에 `source: "place_graphql_fsasReviews"`, `stop_reason`도 포함)

> ⚠️ 수집 개수 변경(2026-06): `naver_ReviewCollector.py`는 실행 시 방문자/블로그 **유효 리뷰 목표 개수**를
> 한 번만 입력받아(빈 입력=기본값) 모든 음식점에 동일 적용한다. 입력 N은 **광고·불성실 등 제외 후의
> 분석 대상 리뷰** 기준이며, 수집기가 N개의 유효 리뷰가 모일 때까지 로딩한 뒤 정확히 N개로 잘라 저장한다.
> 따라서 `reviews[]` 길이 ≈ `summary.target`이다.

> ⚠️ 블로그는 네이버 API가 **최근순 약 128개(`maxItemCount`)까지만** 접근 허용. total이 수천이어도
> 그 이상은 못 받는다 → 신뢰도 메모에 반영한다.

> ⚠️ 가중치 기준 변경(2026-06): 방문자·카카오 날짜 기준이 18M→12M(감점)/3M이내(보너스)로 변경됐다.
> `recency_penalty_count`는 12~24개월 리뷰 수이며 analyze_reviews.py가 신뢰도 메모에 사용한다.
> 분석 시 12개월 초과 비율이 높으면 주의 명시. `char_count`는 **공백포함** 기준으로 변경됐다.

---

## 2-K. 카카오 스키마 (kakao_ReviewCollector.py, 2026-06 신설)

수집기: `kakao_ReviewCollector.py` — **카카오 내부 API(`place-api.map.kakao.com`) 직접 호출**.
브라우저·로그인 불필요(순수 `urllib`). 헤더 `pf:PC` / `appversion` / `Accept` / `Referer` 필수.
네이버와 **출력 스키마를 의도적으로 호환**시켜 `analyze_reviews.py`를 그대로 재사용한다.

### 최상위 (네이버와 공통 + 카카오 추가)
- `place_id` = `confirm_id`(카카오 플레이스 ID). `confirm_id` 필드도 별도 보유.
- `review_type` = `"kakao"`(카카오 후기) 또는 `"blog"`(카카오 연동 블로그).
- `source` = `"place_api_kakaomap_reviews"` / `"place_api_blog_reviews"`, `stop_reason` 포함.

### place_info (네이버와 동일 키 + 카카오 추가)
- 네이버와 같은 키: `category`(대>중>소 경로), `address`, `phone`, `hours[]`, `break_time`,
  `regular_holiday`(예: "매주 일요일, 공휴일"), `parking`, `facilities[]`, `menus[]`, `raw_hours_text`, `errors[]`.
- **카카오 추가**: `average_score`(전체 평균 별점), `total_kakao_reviews`, `total_blog_reviews`, `kakao_booking`.
- ⚠️ `homepage`/`last_order`는 카카오 panel3에서 안 나올 수 있다(보통 `null`).

### (C) 카카오 후기 객체 — `review_type: "kakao"`
| 필드 | 의미 |
|------|------|
| `id` / `review_id` | 순번 / 카카오 리뷰 고유 ID |
| `author` | 작성자 닉네임 |
| `date` / `date_raw` | `YYYY-MM-DD` / 원본 `registered_at`("YYYY-MM-DD HH:MM:SS", **정확한 일시**) |
| `content` | 후기 본문 (보통 짧음, 한두 줄 많음) |
| `char_count` | 공백 제외 글자 수 |
| **`rating`** | **별점 ★1~5 (네이버 방문자는 항상 null이던 값 — 카카오는 채워짐)** |
| `strength_ids` / `keywords` / `tags` | 강점태그 id / 이름(맛·가성비·친절·분위기·주차). keywords·tags는 분석 재사용용 동일 값 |
| `photo_count` / `like_count` | 첨부 사진 수 / 좋아요 수 (메타데이터, 가중치 미반영) |
| `is_owner_pick` | 사장님픽 여부 |
| `recency_bonus` | `true`면 3개월 이내 → ×1.3 |
| `recency_penalty` | `true`면 12~24개월 → ×0.5 |
| `quality_bonus` | `true`면 공백제외 40자↑ → ×1.2 / 100자↑ → ×1.5 |
| `weight` | 위 조건이 곱연산된 **최종 가중치** |
| `exclude_reason` | (`excluded[]`에만) `paid:*` / `insincere`(공백제외 15자 미만) / `too_old:*` |

> ⚠️ **카카오 후기와 네이버 방문자는 동일한 가중치 기준을 사용한다(2026-06 통합).**
> 별점은 *의견*이므로 weight가 아니라 `rating`으로 분리한다(영향력≠의견 원칙).
> 작성자 신뢰도/사진 가중치는 네이버에서 구할 수 없어 폐기.
> weight는 수집 단계 값을 그대로 사용(재계산 금지). `char_count`는 **공백제외** 기준.

### (D) 카카오 블로그 객체 — `review_type: "blog"`, `source: place_api_blog_reviews`
- 네이버 블로그와 거의 동일: `id`, `blog_id`/`log_no`(origin_url=blog.naver.com에서 파싱), `url`,
  `date`/`date_raw`, `title`, `author`, `body`, `body_source`, `char_count`, `image_count`,
  `hashtag_count`, `has_phone`, `recency_bonus`, `recency_penalty`, `weight`,
  `flags`(`paid_suspect`/`extraction_warning`/`fulltext_failed`),
  `exclude_reason`(`paid_hard:*` / `too_old:24개월_초과`). 협찬·신선도 로직은 네이버 블로그와 동일.
- **신선도 가중치는 방문자와 동일**: 3개월 이내 ×1.3 / 12~24개월 ×0.5 / 24개월 초과 제외.
- **`body`는 전문(全文)이다(2026-06 변경)**: 수집기가 `blog.naver.com` 모바일 페이지에 직접 접속해
  본문을 추출한다(카카오 API 프리뷰가 아님). 최대 `B_FULLTEXT_CAP`(12000자)까지 저장.
- **`body_source`**: `"fulltext"`(전문 추출 성공) / `"preview"`(추출 실패 → 카카오 프리뷰 폴백, `flags`에 `fulltext_failed`).
  → 전문이므로 `paid_hard` 협찬 감지가 정확해지고(글 하단 고지까지 포착), 메뉴·감성 추출 근거가 강해진다.

### summary (카카오)
- **공통**: `target`, `total_collected`, `count_within_3m`, `count_3m_to_12m`, `count_12m_to_24m`,
  `recency_bonus_count`, **`recency_penalty_count`**(=12~24개월 수, analyze_reviews 신뢰도 메모용),
  `date_oldest`, `date_newest`, `target_met`, `total_excluded`.
- **카카오 후기**: `total_kakao_count`, `average_score_overall`, `avg_rating_collected`,
  `rating_distribution`(★1~5 개수), `quality_bonus_count`, `excluded_paid/insincere/too_old`.
- **카카오 블로그**: `total_blog_count`, `total_valid`, `excluded_paid_hard`, `excluded_too_old`, `flag_paid_suspect`, `flag_extraction_warn`,
  `fulltext_count`(전문 추출 성공 수), `fulltext_failed_count`(프리뷰 폴백 수), `phase2_entered`.

> ⚠️ **페이지네이션 한계**: 카카오 후기는 `previous_last_review_id` 커서(20개/페이지), 블로그는 `page` 파라미터(10개/페이지).
> 둘 다 `order=LATEST` 최신순으로 24개월 벽 또는 목표 도달까지 수집. 네이버 블로그의 128개 하드 상한 같은
> 제약은 확인되지 않았으나, 매우 깊은 과거까지의 전수 보장은 아니다 → 신뢰도 메모에 반영.

> ⚠️ **분석 시**: 카카오는 `rating`이 채워지므로 `analyze_reviews.py`의 `avg_rating_weighted`가 유효하다.
> 네이버 방문자(별점 null)와 비교 시 이 차이를 감안한다. 카카오 본문은 짧아 감성 사전 1차 추정이
> 중립으로 쏠리기 쉬우니, 별점 분포·강점태그(`tags`)·대표 샘플 본문을 함께 근거로 삼는다.

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
python analyze_reviews.py "reviews_json\피탕김탕_naver_visitor.json"
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
- **수집 개수**: 수집기는 실행 시 방문자/블로그 유효 리뷰 목표를 1회 입력받는다(기본 150/50, 빈 입력=기본값).
  목표는 '제외 후 유효 리뷰' 기준이며 `reviews[]`는 정확히 `summary.target`개로 맞춰진다. 분석 시 표본 크기는
  `summary.target`이 아니라 실제 `reviews[]` 길이(=`total_collected`/`total_valid`)로 확인한다(목표 미달 가능).

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

이 프로젝트는 두 가지 역할을 합니다.

1. **MCP 서버** (`server/`) — Claude AI에게 LH·IH 공고 조회 도구를 노출
2. **배치 처리** (`batch/`) — LH·IH 공고를 주기적으로 조회하여 Notion DB에 저장 (PDF 첨부파일 포함)

대상 데이터: 인천 지역 LH(한국토지주택공사) 및 IH(인천도시공사) 임대주택 공고 (한국 공공데이터포털 API)

## Tech Stack

- **Python 3.14** (실행 명령어: `py`, `python` 미동작)
- **FastMCP 3.0.2** — MCP 서버 프레임워크 (`server/`)
- **HTTPx** — 비동기 HTTP 클라이언트
- **notion-client 3.0.0** — Notion API 클라이언트 (`batch/`, AsyncClient 사용 — 비동기 I/O)
- **python-dotenv** — `.env` 파일 로드
- **BeautifulSoup4** — HTML 파싱 (공고 상세 페이지 스크래핑)

## Directory Structure

```
.mcp.json               # Claude Code MCP 설정 (gitignore됨, API 키 포함)
.env                    # 환경변수 (gitignore됨)
config.py               # 환경변수 일원화 (.env 로딩 + 공유 상수)
http_utils.py           # HTTP 재시도 유틸리티 (exponential backoff)
lh_api.py               # 공통 LH API 로직 (server/와 batch/ 공유)
ih_api.py               # IH(인천도시공사) API 로직 (server/와 batch/ 공유)
doc_processor.py        # 공고 상세 페이지 스크래핑 (PDF URL 추출)
server/
└── lh_mcp.py           # FastMCP 서버 — LH·IH 도구 6개
batch/
├── main.py             # 배치 진입점 — LH + IH + PDF 스크래핑 + 리포트
├── notion_base.py      # Notion 공통 로직 (Client 지연초기화, 헬퍼, 페이지네이션, DB 생성)
├── notion_writer.py    # LH Notion DB upsert (PAN_ID 기준, notion_base 사용)
├── ih_notion_writer.py # IH Notion DB upsert (link 기준, notion_base 사용)
├── report_writer.py    # 배치 실행 리포트 Notion DB 생성
├── setup_scheduler.py  # Windows Task Scheduler 등록
└── requirements.txt    # notion-client, httpx, python-dotenv, bs4
```

## Running

```bash
# 모든 명령은 프로젝트 루트에서 실행 (python -m 모듈 방식)

# MCP 서버 직접 실행 (.venv 절대 경로 필수)
.venv/Scripts/python.exe -m server.lh_mcp

# 배치 단독 실행
py -m batch.main

# Task Scheduler 등록 (최초 1회, 매일 09:00)
py -m batch.setup_scheduler
```

## MCP Server (`server/lh_mcp.py`)

- `FastMCP("LH_Incheon_Notice_Server")` 인스턴스
- 6개 도구 노출:
  - `get_incheon_lh_notices()` — LH 공고 + 공급정보 조회
  - `get_ih_notices()` — IH 공고 전체 페이지 조회 (fetch_all_ih_notices 사용)
  - `get_notice_summary()` — LH+IH 공고 현황 요약 (최근 N일)
  - `search_all_notices()` — LH+IH 통합 키워드 검색
  - `get_upcoming_deadlines()` — 마감 임박 LH 공고 (D-N일 이내, D-day 순 정렬)
  - `get_supply_detail()` — 특정 LH 공고의 공급정보 상세 조회
- 환경변수 `OPEN_API_KEY` 필요 (`.mcp.json`의 `env` 필드에 설정, 시작 시 `validate_env` 검증)

**`get_incheon_lh_notices` 파라미터:**

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `tp_code` | `13` | 공고유형 (아래 유효 코드 표 참조) |
| `cnp_code` | `28` | 지역코드 (28=인천) |
| `status` | `공고중` | 공고상태. 실측값: `공고중`/`접수중`/`접수마감`. 빈 문자열은 PAN_SS 파라미터를 제외(전체). |
| `keyword` | `""` | 공고명 필터 키워드 (API 미지원 — 클라이언트 사이드 필터링) |
| `lookback_days` | `0` | 0이면 날짜 파라미터 없이 현재 활성 공고 포함 조회. 양수이면 과거 마감 공고 조회 (활성 제외됨). |
| `limit` | `100` | 페이지당 공고 수 (API 최대값 실측 확인, 이 이상 올려도 동일) |

**`get_notice_summary` 파라미터:**

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `days` | `30` | 조회 기간 (일) |

LH는 `LH_TP_CODES`(13+06) 전체 + 인천+전국 공고 자동 검색.

**`search_all_notices` 파라미터:**

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `keyword` | (필수) | 검색 키워드 |
| `days` | `365` | 조회 기간 (일) |
| `category` | `""` | IH 공고 구분 (분양/임대) |

LH는 `LH_TP_CODES`(13+06) 전체 + 인천+전국 공고에서 키워드 검색.

**`get_upcoming_deadlines` 파라미터:**

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `days` | `7` | 마감까지 남은 일수 |

LH는 `LH_TP_CODES`(13+06) 전체 + 인천+전국 공고 중 마감 임박 건 표시.

**`get_supply_detail` 파라미터:**

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `pan_id` | (필수) | 공고 ID |
| `spl_inf_tp_cd` | (필수) | 공급정보유형코드 (공고 목록의 SPL_INF_TP_CD) |
| `ccr_cnnt_sys_ds_cd` | (필수) | 시스템구분코드 (공고 목록의 CCR_CNNT_SYS_DS_CD) |
| `tp_code` | `13` | 공고유형코드 |

**MCP 내부 헬퍼:**
- `_gather_lh_notices(days, tp_codes=None, **kwargs)` → `tuple[list, list[str]]` — 활성+과거 2회 조회 × tp_code별 → `dedup_by_pan_id()` 병합. 부분 실패 시 경고 리스트 반환. `tp_codes` 전달 시 다중 유형 검색.
- `_gather_all_lh_notices(days, tp_codes, **kwargs)` → `tuple[list, list[str]]` — 인천(CNP_CD=28) + 전국(CNP_CD 없음) 이중 조회 → 전국 결과는 `filter_region_relevant()`로 인천 관련+전국 대상만 필터 → `dedup_by_pan_id()` 병합. 부분 실패 경고를 MCP 응답에 포함. 인천 조회 우선.
- `_format_lh_notice_header(notice)` — LH 공고 헤더 생성 (PAN_DT·SPL/CCR 코드 포함)
- `_format_supply_rows()` — 공급정보를 마크다운 테이블로 변환 (건수 표시)

**MCP 응답 포맷 규칙:**
- 모든 도구: `## 제목 (N건)` 최상위 헤더 + 총 건수 표시
- LH 공고: `### [상태] 공고명 (ID)` + `유형 | 공고일 | 기간` + `상세URL | 공급조회: SPL=, CCR=` + 공급정보 조회 실패 시 에러 표시
- IH 공고: `### [구분] 제목` + `유형 | 날짜 | 링크`
- 공급정보: 마크다운 테이블 (컬럼 라벨 1회, 토큰 효율)
- 구분자: 파이프 `|` 통일

**MCP 설정 (`.mcp.json`):**
- `notion` 서버: `npx @notionhq/notion-mcp-server` (NOTION_TOKEN 필요)
- `lh` 서버: `.venv/Scripts/python.exe -m server.lh_mcp` + `cwd` 루트 (OPEN_API_KEY 필요)

## HTTP Retry (`http_utils.py`)

- `request_with_retry(client, method, url, **kwargs)` — 모든 API 호출에 적용
- 재시도 대상: HTTP 429, 500, 502, 503, 504 + `httpx.TimeoutException` + `httpx.ConnectError`
- 전략: 최대 3회, exponential backoff (2s → 4s → 8s)

## LH API

**엔드포인트:**
- 공고 목록: `http://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1`
- 공급정보: `http://apis.data.go.kr/B552555/lhLeaseNoticeSplInfo1/getLeaseNoticeSplInfo1`

**요청 파라미터 (공고 목록):**

| 파라미터 | 값 | 설명 |
|---|---|---|
| `UPP_AIS_TP_CD` | `13` | 공고유형코드 |
| `CNP_CD` | `28` | 지역코드. **빈 문자열이면 파라미터 제외 → 전국 조회.** 전세임대 등 전국 공고는 CNP_CD=28에 미반환됨 |
| `PAN_SS` | `공고중` | 공고상태. 빈 문자열 전달 시 0건 반환 (파라미터 자체를 제외해야 전체 조회됨) |
| `PAN_ST_DT` / `PAN_ED_DT` | `YYYY.MM.DD` | 조회 기간. **있으면 활성 공고(공고중/접수중) 제외됨** → 현재 활성 공고 조회 시 제외 필수 |
| `PG_SZ` | `100` | 페이지 크기 (API 실측 최대값) |

**응답 날짜 필드 (요청 파라미터명과 다름):**
- 요청: `PAN_ST_DT` / `PAN_ED_DT` → 응답: `PAN_NT_ST_DT` (공고시작일) / `CLSG_DT` (마감일) / `PAN_DT` (공고일자)

**유효한 UPP_AIS_TP_CD (인천 기준, 01~25 전수 확인):**

| 코드 | 유형명 | 365일 건수 |
|---|---|---|
| `01` | 택지/용지 공급 | ~60건 |
| `05` | 공공분양주택 | ~3건 |
| `06` | 임대주택 (행복주택, 국민임대 등) | ~70건 |
| `13` | 매입/전세임대 | ~52건 |
| `22` | 상가 분양/임대 | ~42건 |

**응답 구조:** list 형태 `[{dsSch:[...]}, {dsList:[...]}]` — `_extract_ds_list(data, key)` 헬퍼로 파싱

**공급정보 파라미터:** `SPL_INF_TP_CD`, `CCR_CNNT_SYS_DS_CD`를 공고 목록 item에서 가져와야 함 (빈값 전달 시 데이터 없음)

**공급정보 독립 조회:** `lh_api.fetch_supply_detail(pan_id, spl_inf_tp_cd, ccr_cnnt_sys_ds_cd, tp_code)` — MCP `get_supply_detail` 도구에서 사용

**PAN_ID 중복 제거:** `lh_api.dedup_by_pan_id(*notice_lists)` — MCP `_gather_lh_notices()`와 배치 `run_lh_batch()`에서 공유

**전국 조회 필터:** `lh_api.filter_region_relevant(notices, region, nationwide_codes)` — 전국 조회 결과에서 대상 지역 관련만 추출. 판별 기준 (OR): CNP_CD_NM에 region 포함, PAN_NM에 region 포함, AIS_TP_CD가 nationwide_codes에 포함. MCP `_gather_all_lh_notices()`와 배치 `run_lh_batch()`에서 공유

**전국 공고 필터링:** CNP_CD 없이 전국 조회 후 `filter_region_relevant()`로 인천 관련만 추출. 필터 기준 (OR): ① CNP_CD_NM에 "인천" 포함 ② PAN_NM에 "인천" 포함 ③ AIS_TP_CD가 `NATIONWIDE_AIS_CODES`에 포함 (17=전세임대: 거주지 기반 전국 제도). 전국 조회 결과의 CNP_CD_NM은 첫 번째 지역으로 채워지는 API 특성이 있어, 유형코드(AIS_TP_CD) 기반 판별이 필수.

**AIS_TP_CD 매핑:** 07=국민임대, 08=공공임대, 09=영구임대, 10=행복주택, 17=전세임대, 26=매입임대, 36=집주인임대

## IH API (`ih_api.py`)

**엔드포인트:**
- 분양/임대 공고: `https://apis.data.go.kr/B552831/ih/slls-posts`

**요청 파라미터:**

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `serviceKey` | `OPEN_API_KEY` | 공공데이터포털 API 키 |
| `numOfRows` | `30` | 페이지당 건수 (API 최대값 30) |
| `pageNo` | `1` | 페이지 번호 |
| `startCrtrYmd` | (필수) | 조회 시작일 (`YYYY-MM-DD`) |
| `endCrtrYmd` | (필수) | 조회 종료일 (`YYYY-MM-DD`) |
| `sj` | `""` | 공고 제목 필터 키워드 (서버 사이드) |
| `seNm` | `""` | 공고 구분 (`분양`/`임대`, 빈 문자열이면 전체) |

**응답 구조 (Swagger 명세):** `{ header: {resultCode, resultMsg}, body: {pageNo, numOfRows, totalPageNo, totalCount, posts: [...]} }`
**응답 필드:** `tyNm`(유형명), `seNm`(구분), `crtYmd`(등록일), `sj`(제목), `link`(URL) — 고유 ID 없음
**link 정규화:** `normalize_link()` — scheme 통일(https), trailing slash 제거, fragment 제거, query parameter 정렬. API 조회 시 자동 적용.

**MCP 도구 (`get_ih_notices`) 기본값:**
- `start_date`: 빈 문자열 → MCP 도구에서 1년 전으로 자동 설정
- `end_date`: 빈 문자열 → MCP 도구에서 오늘로 자동 설정
- `keyword`: 공고 제목 필터
- `category`: 공고 구분 (분양/임대)
- `fetch_all_ih_notices` 사용 — 전체 페이지 자동 순회 (numOfRows/pageNo 파라미터 제거됨)

## Batch (`batch/`)

**LH 배치:**
- `config.LH_TP_CODES = ["13", "06"]` — 매입/전세임대 + 임대주택(행복주택, 국민임대 등)
- 인천(CNP_CD=28) + 전국(CNP_CD 없음) 이중 조회 → 전국 결과는 `filter_region_relevant()`로 인천 관련+전국 대상만 필터 → `dedup_by_pan_id()` 병합
- `status=""` — 공고중+접수중 모든 활성 공고 포착 (접수중 누락 방지)
- 키워드 필터 없음 (두 유형 모두 거의 100% 입주자 모집 공고)
- `upsert_all()` 반환: `{"new", "updated", "closed", "failed", "new_notices", "failed_notices"}`
- Notion DB: "LH 인천 임대주택 공고" (`NOTION_DATABASE_ID`)

**IH 배치:**
- 최근 `IH_LOOKBACK_DAYS`(90)일 입주자 모집 공고 조회
- server-side `sj="입주자"` + client-side `_is_recruitment_notice()` 필터 (모집+공고 포함, 노이즈 키워드 제외)
- 노이즈 키워드: 마감, 취소, 결과, 계약, 입주안내, 변경, 정정
- `ih_api.fetch_all_ih_notices()` → `ih_upsert_all(notices)`
- `upsert_all()` 반환: `{"new", "updated", "closed", "failed", "new_notices", "failed_notices"}` (LH와 동일 구조)
- `close_expired_notices(active_links, page_cache)` — API 조회 결과에 없는 공고를 "마감" 처리 (차집합 비교)
- Notion DB: "IH 인천도시공사 분양임대 공고" (`IH_NOTION_DATABASE_ID`)
- DB 스키마에 "상태" select 속성 포함 (모집중/마감)
- upsert 식별자: `link` (고유 ID 없음)

**첨부파일 스크래핑 (`doc_processor.py`):**
- 공통 스켈레톤 `_scrape_detail(url, extract_links_fn, client=None)` + 사이트별 링크 추출 전략
- `create_scrape_client()` — 스크래핑용 httpx 클라이언트 팩토리 (배치에서 공유 클라이언트 생성)
- `_extract_lh_links()`: `javascript:fileDownLoad('파일ID')` 정규식 파싱 → `https://apply.lh.or.kr/lhapply/lhFile.do?fileid=ID` URL 재구성
- `_extract_ih_links()`: `FileDown` 서블릿 패턴 + `<a>` 텍스트/href 확장자로 파일 링크 식별
- 공개 API: `scrape_lh_detail(dtl_url, client=None)`, `scrape_ih_detail(link_url, client=None)` — 내부적으로 `_scrape_detail` 위임. `client` 전달 시 공유 클라이언트 사용.
- 반환: `{"files": [{"name": "파일명", "url": "다운로드URL"}, ...], "html_text": str}`
- 본문 텍스트: 최대 5000자 (`_MAX_TEXT_LENGTH`) 통일 적용
- Notion DB "첨부파일" files 속성에 external URL로 저장 (파일명 직접 사용)
- best-effort: 스크래핑 실패 시 빈 리스트, 공고 upsert에 영향 없음
- `Semaphore(3)` 병렬 스크래핑 + 1초 딜레이로 정부 사이트 rate limit 준수 (LH/IH 각각)

**배치 리포트:**
- `report_writer.py` — 배치 실행 결과를 Notion DB에 자동 기록
- Notion DB: "배치 실행 리포트" (`REPORT_DATABASE_ID`, 최초 실행 시 자동 생성)
- 기록 항목: 실행일시, 소요시간, LH 신규·업데이트·마감·실패·공급실패, IH 신규·업데이트·마감·실패 건수, 상태(성공/부분실패/실패)
- 신규 공고 및 실패 공고 목록을 페이지 본문에 bullet list로 포함

**공통:**
- `notion_base.py` — Notion AsyncClient 지연 초기화 (`_RetryAsyncClient` — rate limit 자동 재시도), `rich_text`/`select`/`query_db`/`paginate_query`/`get_or_create_database` 비동기 공통 함수 제공
- `get_or_create_database(env_key, db_name, db_properties, title_name="공고명")` — `title_name`으로 title 속성명 지정 가능, 기존 DB에 누락 속성 자동 추가 (`_ensure_db_properties`)
- **Notion AsyncClient**: `_RetryAsyncClient(AsyncClient)` — `APIErrorCode.RateLimited` 시 exponential backoff 자동 재시도 (최대 3회). 모든 Notion API 호출이 비동기로 실행되어 LH/IH 배치 병렬화 가능
- `config.py` — `.env` 로딩 1회, `OPEN_API_KEY`/`NOTION_TOKEN`/`NOTION_PARENT_PAGE_ID` 일원화, `LH_TP_CODES` 공유 상수, `TARGET_REGION`/`NATIONWIDE_AIS_CODES` 전국 필터 설정, `validate_env()` 환경변수 사전 검증
- **LH+IH 병렬 실행**: `asyncio.gather(run_lh_batch(), run_ih_batch())` — 서로 다른 API/Notion DB 사용하므로 충돌 없음
- Notion DB는 최초 실행 시 자동 생성, DB ID를 `.env`에 저장
- **Zero-result guard**: `close_expired_notices()`에서 API 결과 0건 + Notion 활성 공고 존재 시 마감 처리 건너뜀 (API 장애 시 전량 마감 방지)
- **Failed notices 추적**: upsert 실패 공고를 `failed_notices` 리스트로 반환 → 리포트에 포함
- **배치 실패 알림**: 리포트 상태가 "성공"이 아니면 리포트 페이지에 Notion 코멘트 자동 생성 → Notion 알림으로 즉시 통보
- **환경변수 사전 검증**: `config.validate_env()` — 배치/MCP 시작 시 필수 환경변수 일괄 검증, 누락 시 즉시 EnvironmentError
- **공급정보 실패 추적**: `_fetch_supply()` 실패 시 `supply_error` 키에 에러 메시지 포함 → MCP 응답·배치 리포트(`LH공급실패` 속성)에서 확인 가능
- **MCP 부분 실패 경고**: `_gather_lh_notices`/`_gather_all_lh_notices` 반환 타입 `tuple[list, list[str]]` — 일부 API 실패 시 경고 메시지를 MCP 응답 하단에 `⚠` 표시
- **블록 해시 기반 스킵**: `_블록해시` rich_text 속성에 supply_details MD5 해시 저장 → 업데이트 시 해시 비교하여 변경 없으면 `_replace_page_blocks()` 생략 (API 호출 80% 감소)
- **IH 페이지네이션 병렬화**: `fetch_all_ih_notices()` — 첫 페이지 조회 후 나머지 `asyncio.gather()`로 병렬 조회
- **LH API 클라이언트 공유**: `fetch_lh_notices(client=)` 파라미터로 공유 httpx.AsyncClient 전달 (배치에서 8개 조회 시 TLS 재사용)
- **도서지역 제외 필터**: `config.EXCLUDE_SUBREGIONS` 키워드 set → `lh_api.exclude_subregions()` + `filter_region_relevant(exclude_keywords=)`로 PAN_NM 매칭. 인천 직접 조회·전국 조회 양쪽에 적용. 배치·MCP 공유

**날짜 기반 알림:**
- `공고기간` date 속성(start+end)으로 Notion 캘린더 뷰 + 내장 알림 활용
- 코멘트 기반 알림(`notify_upcoming.py`) 제거 — Notion 내장 자동화/리마인더로 대체
- LH: `공고기간` = `PAN_NT_ST_DT`(시작) ~ `CLSG_DT`(마감) date range
- IH: `등록일` = `crtYmd` date (마감일 정보 없음)
- 캘린더 뷰: LH DB "공고 캘린더" (공고기간 기준), IH DB "공고 캘린더" (등록일 기준)

## Environment Variables (`.env`)

```
OPEN_API_KEY=...          # 공공데이터포털 API 키
NOTION_TOKEN=...          # Notion 통합 토큰
NOTION_PARENT_PAGE_ID=... # Notion 부모 페이지 ID
NOTION_DATABASE_ID=...    # LH Notion DB ID (배치 최초 실행 후 자동 저장)
IH_NOTION_DATABASE_ID=... # IH Notion DB ID (배치 최초 실행 후 자동 저장)
REPORT_DATABASE_ID=...    # 배치 리포트 Notion DB ID (배치 최초 실행 후 자동 저장)
```

## Work Principles

- **분석 우선 (Analyze Before Act)**: 코드 변경 요청을 받으면 즉시 구현하지 말고, 먼저 관련 코드를 읽고 구조·중복도·영향 범위를 근거 기반으로 분석하여 보고한다. 분석 결과 실익이 있을 때만 구현을 진행한다.

## Environment & Configuration

- `.mcp.json`, `.env` — gitignore됨 (API 키 포함)
- MCP 패키지: `.venv`에 직접 설치 (fastmcp, httpx)
- Batch 패키지: `batch/requirements.txt`로 설치
- IDE: PyCharm, 포매터: Black

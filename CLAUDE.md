# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

이 프로젝트는 세 가지 역할을 합니다.

1. **MCP 서버** (`server/`) — Claude AI에게 LH·IH 공고 조회 + AI 분석 도구를 노출
2. **배치 처리** (`batch/`) — LH·IH 공고를 주기적으로 조회하여 Notion DB에 저장 + 문서 임베딩
3. **AI 분석** (root modules) — Ollama 기반 의미 검색·문서 분석·자격 매칭

대상 데이터: 인천 지역 LH(한국토지주택공사) 및 IH(인천도시공사) 임대주택 공고 (한국 공공데이터포털 API)

## Tech Stack

- **Python 3.14** (실행 명령어: `py`, `python` 미동작)
- **FastMCP 3.0.2** — MCP 서버 프레임워크 (`server/`)
- **HTTPx** — 비동기 HTTP 클라이언트
- **notion-client 3.0.0** — Notion API 클라이언트 (`batch/`)
- **python-dotenv** — `.env` 파일 로드
- **Ollama** — 로컬 AI (임베딩: qwen3-embedding:4b, 비전: qwen2.5-vl/qwen3-vl)
- **NumPy** — 벡터 연산 (코사인 유사도)
- **PyMuPDF** — PDF 텍스트/이미지 추출
- **BeautifulSoup4** — HTML 파싱 (공고 상세 페이지 스크래핑)

## Directory Structure

```
.mcp.json               # Claude Code MCP 설정 (gitignore됨, API 키 포함)
.env                    # 환경변수 (gitignore됨)
config.py               # 환경변수 일원화 (.env 로딩 + 공유 상수 + Ollama 설정)
http_utils.py           # HTTP 재시도 유틸리티 (exponential backoff)
lh_api.py               # 공통 LH API 로직 (server/와 batch/ 공유)
ih_api.py               # IH(인천도시공사) API 로직 (server/와 batch/ 공유)
ollama_client.py        # Ollama HTTP 클라이언트 (임베딩·비전·생성, GPU lock)
vector_store.py         # SQLite + NumPy 벡터 저장소 (매트릭스 캐시 검색)
text_chunker.py         # 한국어 공고 텍스트 청킹 (섹션 기반 + 고정 크기)
doc_processor.py        # 문서 다운로드·PDF 추출·HTML 파싱 (순수 I/O)
data/                   # 런타임 데이터 (gitignore됨)
├── vector.db           # SQLite 벡터 DB (공고·청크·임베딩)
└── docs/               # 다운로드된 PDF·이미지 캐시
server/
└── lh_mcp.py           # FastMCP 서버 — LH·IH 도구 6개 + AI 도구 4개
batch/
├── main.py             # 배치 진입점 — LH + IH + 문서처리 + 리포트
├── doc_pipeline.py     # 문서 처리 파이프라인 (스크래핑→추출→청킹→임베딩)
├── notion_base.py      # Notion 공통 로직 (Client 지연초기화, 헬퍼, 페이지네이션, DB 생성)
├── notion_writer.py    # LH Notion DB upsert (PAN_ID 기준, notion_base 사용)
├── ih_notion_writer.py # IH Notion DB upsert (link 기준, notion_base 사용)
├── notify_upcoming.py  # 마감 임박 공고 Notion 코멘트 알림
├── report_writer.py    # 배치 실행 리포트 Notion DB 생성
├── setup_scheduler.py  # Windows Task Scheduler 등록
└── requirements.txt    # notion-client, httpx, python-dotenv, numpy, pymupdf, bs4
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
- 10개 도구 노출 (기존 6개 + AI 4개):
  - `get_incheon_lh_notices()` — LH 공고 + 공급정보 조회
  - `get_ih_notices()` — IH 공고 전체 페이지 조회 (fetch_all_ih_notices 사용)
  - `get_notice_summary()` — LH+IH 공고 현황 요약 (최근 N일)
  - `search_all_notices()` — LH+IH 통합 키워드 검색
  - `get_upcoming_deadlines()` — 마감 임박 LH 공고 (D-N일 이내, D-day 순 정렬)
  - `get_supply_detail()` — 특정 LH 공고의 공급정보 상세 조회
  - `semantic_search()` — 자연어 의미 기반 공고 검색 (Ollama 필요)
  - `analyze_notice()` — 특정 공고 심층 분석 (PDF 자격요건·소득기준 추출)
  - `match_eligibility()` — 사용자 조건으로 적합 공고 매칭
  - `find_similar_notices()` — 유사 공고 발견
- 환경변수 `OPEN_API_KEY` 필요 (`.mcp.json`의 `env` 필드에 설정, 시작 시 `validate_env` 검증)
- AI 도구는 Ollama 미연결 시 안내 메시지 반환 (graceful degradation)

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
| `tp_code` | `13` | LH 공고유형코드 |

**`search_all_notices` 파라미터:**

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `keyword` | (필수) | 검색 키워드 |
| `days` | `365` | 조회 기간 (일) |
| `category` | `""` | IH 공고 구분 (분양/임대) |

**`get_upcoming_deadlines` 파라미터:**

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `days` | `7` | 마감까지 남은 일수 |
| `tp_code` | `13` | LH 공고유형코드 |

**`get_supply_detail` 파라미터:**

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `pan_id` | (필수) | 공고 ID |
| `spl_inf_tp_cd` | (필수) | 공급정보유형코드 (공고 목록의 SPL_INF_TP_CD) |
| `ccr_cnnt_sys_ds_cd` | (필수) | 시스템구분코드 (공고 목록의 CCR_CNNT_SYS_DS_CD) |
| `tp_code` | `13` | 공고유형코드 |

**MCP 내부 헬퍼:**
- `_gather_lh_notices(days, **kwargs)` — 활성(lookback_days=0) + 과거(lookback_days=days) 2회 조회 → PAN_ID 중복 제거 병합. LH API 날짜 파라미터 존재 시 활성 공고 제외 문제 해결.
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
| `CNP_CD` | `28` | 지역코드 |
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
- `LH_TP_CODES = ["13", "06"]` — 매입/전세임대 + 임대주택(행복주택, 국민임대 등)
- `asyncio.gather`로 두 유형 병렬 조회 → PAN_ID 기준 중복 제거 병합
- 키워드 필터 없음 (두 유형 모두 거의 100% 입주자 모집 공고)
- `upsert_all()` 반환: `{"new", "updated", "closed", "failed", "new_notices", "failed_notices"}`
- Notion DB: "LH 인천 임대주택 공고" (`NOTION_DATABASE_ID`)

**IH 배치:**
- 최근 90일 입주자 모집 공고 조회
- server-side `sj="입주자"` + client-side `_is_recruitment_notice()` 필터 (모집+공고 포함, 노이즈 키워드 제외)
- 노이즈 키워드: 마감, 취소, 결과, 계약, 입주안내, 변경, 정정
- `ih_api.fetch_all_ih_notices()` → `ih_upsert_all(notices)`
- `upsert_all()` 반환: `{"new", "updated", "closed", "failed", "new_notices", "failed_notices"}` (LH와 동일 구조)
- `close_expired_notices(active_links, page_cache)` — API 조회 결과에 없는 공고를 "마감" 처리 (차집합 비교)
- Notion DB: "IH 인천도시공사 분양임대 공고" (`IH_NOTION_DATABASE_ID`)
- DB 스키마에 "상태" select 속성 포함 (모집중/마감)
- upsert 식별자: `link` (고유 ID 없음)

**배치 리포트:**
- `report_writer.py` — 배치 실행 결과를 Notion DB에 자동 기록
- Notion DB: "배치 실행 리포트" (`REPORT_DATABASE_ID`, 최초 실행 시 자동 생성)
- 기록 항목: 실행일시, 소요시간, LH 신규·업데이트·마감·실패, IH 신규·업데이트·마감·실패 건수, 상태(성공/부분실패/실패)
- 신규 공고 및 실패 공고 목록을 페이지 본문에 bullet list로 포함

**공통:**
- `notion_base.py` — Notion 클라이언트 지연 초기화, `rich_text`/`select`/`query_db`/`paginate_query`/`get_or_create_database` 공통 함수 제공
- `get_or_create_database(env_key, db_name, db_properties, title_name="공고명")` — `title_name`으로 title 속성명 지정 가능, 기존 DB에 누락 속성 자동 추가 (`_ensure_db_properties`)
- `config.py` — `.env` 로딩 1회, `OPEN_API_KEY`/`NOTION_TOKEN`/`NOTION_PARENT_PAGE_ID` 일원화, `validate_env()` 환경변수 사전 검증
- Notion DB는 최초 실행 시 자동 생성, DB ID를 `.env`에 저장
- **Zero-result guard**: `close_expired_notices()`에서 API 결과 0건 + Notion 활성 공고 존재 시 마감 처리 건너뜀 (API 장애 시 전량 마감 방지)
- **Failed notices 추적**: upsert 실패 공고를 `failed_notices` 리스트로 반환 → 리포트에 포함
- **배치 실패 알림**: 리포트 상태가 "성공"이 아니면 리포트 페이지에 Notion 코멘트 자동 생성 → Notion 알림으로 즉시 통보
- **환경변수 사전 검증**: `config.validate_env()` — 배치/MCP 시작 시 필수 환경변수 일괄 검증, 누락 시 즉시 EnvironmentError
- **공급정보 실패 추적**: `_fetch_supply()` 실패 시 `supply_error` 키에 에러 메시지 포함 → MCP 응답·배치 로그에서 확인 가능

**알림 기능:**
- `notify_upcoming.py` — 접수마감일 7일 이내 공고에 Notion 코멘트 알림 생성
- LH만 대상 (IH는 마감일 정보 없음)
- `알림완료` checkbox로 중복 알림 방지 (upsert 시 False 초기화 → 공고 업데이트 시 재알림)
- 배치 리포트에 "LH알림" 건수 포함

## Environment Variables (`.env`)

```
OPEN_API_KEY=...          # 공공데이터포털 API 키
NOTION_TOKEN=...          # Notion 통합 토큰
NOTION_PARENT_PAGE_ID=... # Notion 부모 페이지 ID
NOTION_DATABASE_ID=...    # LH Notion DB ID (배치 최초 실행 후 자동 저장)
IH_NOTION_DATABASE_ID=... # IH Notion DB ID (배치 최초 실행 후 자동 저장)
REPORT_DATABASE_ID=...    # 배치 리포트 Notion DB ID (배치 최초 실행 후 자동 저장)

# Optional — Ollama AI (기본값 있음)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBED_MODEL=qwen3-embedding:4b
OLLAMA_VISION_MODEL=                  # 빈값=자동감지 (qwen3-vl > qwen2.5-vl > llava)
```

## Ollama AI Integration

**핵심 원칙**: Ollama는 선택적 enhancement — 미실행 시 기존 기능 100% 정상 동작

### 모듈 구조

| 모듈 | 역할 |
|---|---|
| `ollama_client.py` | Ollama HTTP 클라이언트 (embed·vision·generate), GPU lock, 비전 모델 자동감지 |
| `vector_store.py` | SQLite + NumPy 벡터 저장소, 매트릭스 캐시 기반 고속 검색 |
| `text_chunker.py` | 한국어 공고 PDF 섹션 기반 텍스트 청킹 |
| `doc_processor.py` | PDF 텍스트/이미지 추출, 상세 페이지 스크래핑 |
| `batch/doc_pipeline.py` | 배치 문서 처리 오케스트레이션 |

### 벡터 검색 흐름

```
공고 수집 (API) → 상세 페이지 스크래핑 → PDF 다운로드
→ PyMuPDF 텍스트 추출 → 섹션 기반 청킹
→ qwen3-embedding:4b 임베딩 → SQLite 저장
→ MCP 도구로 의미 기반 검색·분석·매칭
```

### MCP AI 도구 (4개)

| 도구 | 설명 | 핵심 파라미터 |
|---|---|---|
| `semantic_search` | 자연어 의미 기반 공고 검색 | query, top_k, section, source |
| `analyze_notice` | 공고 심층 분석 (PDF 자격요건 추출) | notice_id, url, source |
| `match_eligibility` | 사용자 조건으로 적합 공고 매칭 | conditions, top_k |
| `find_similar_notices` | 유사 공고 발견 | notice_id, source, top_k |

### 비전 모델 활용

- 비전 모델 우선순위: qwen3-vl > qwen2.5-vl > llava > moondream
- 평면도 분석: PDF 내 이미지 → 비전 모델로 주택형·방 수·면적 추출
- 스캔 PDF OCR: 텍스트 추출 실패 시 페이지를 이미지로 변환 → 비전 모델 OCR
- 비전 모델 미설치 시 이미지 분석만 skip (텍스트 임베딩은 정상 동작)

### 벡터 DB 스키마

- `notices` 테이블: notice_id, source, title, url, content_hash (변경감지), status
- `chunks` 테이블: notice_id FK, text, section, source_type, page, embedding (BLOB)
- 섹션 분류: eligibility, income, units, schedule, rent, other, body
- 소스 유형: pdf, html, supply, vision, title
- 매트릭스 캐시: 전체 임베딩을 NumPy (N,dim) 배열로 캐시, 단일 matmul로 유사도 계산

## Work Principles

- **분석 우선 (Analyze Before Act)**: 코드 변경 요청을 받으면 즉시 구현하지 말고, 먼저 관련 코드를 읽고 구조·중복도·영향 범위를 근거 기반으로 분석하여 보고한다. 분석 결과 실익이 있을 때만 구현을 진행한다.

## Environment & Configuration

- `.mcp.json`, `.env` — gitignore됨 (API 키 포함)
- MCP 패키지: `.venv`에 직접 설치 (fastmcp, httpx)
- Batch 패키지: `batch/requirements.txt`로 설치
- IDE: PyCharm, 포매터: Black

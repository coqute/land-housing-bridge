# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

이 프로젝트는 두 가지 역할을 합니다.

1. **MCP 서버** (`server/`) — Claude AI에게 LH·IH 공고 조회 도구를 노출
2. **배치 처리** (`batch/`) — LH·IH 공고를 주기적으로 조회하여 Notion DB에 저장

대상 데이터: 인천 지역 LH(한국토지주택공사) 및 IH(인천도시공사) 임대주택 공고 (한국 공공데이터포털 API)

## Tech Stack

- **Python 3.14** (실행 명령어: `py`, `python` 미동작)
- **FastMCP 3.0.2** — MCP 서버 프레임워크 (`server/`)
- **HTTPx** — 비동기 HTTP 클라이언트
- **notion-client 3.0.0** — Notion API 클라이언트 (`batch/`)
- **python-dotenv** — `.env` 파일 로드

## Directory Structure

```
.mcp.json               # Claude Code MCP 설정 (gitignore됨, API 키 포함)
.env                    # 환경변수 (gitignore됨)
lh_api.py               # 공통 LH API 로직 (server/와 batch/ 공유)
ih_api.py               # IH(인천도시공사) API 로직 (server/와 batch/ 공유)
server/
└── lh_mcp.py           # FastMCP 서버 — LH·IH AI 도구 노출용
batch/
├── main.py             # 배치 진입점 — LH + IH 순차 실행
├── lh_fetcher.py       # lh_api.py 재사용 래퍼
├── ih_fetcher.py       # ih_api.py 재사용 래퍼
├── notion_writer.py    # LH Notion DB upsert (PAN_ID 기준)
├── ih_notion_writer.py # IH Notion DB upsert (link 기준)
├── setup_scheduler.py  # Windows Task Scheduler 등록
└── requirements.txt    # notion-client, httpx, python-dotenv
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
- `@mcp.tool()` 데코레이터로 `get_incheon_lh_notices()`, `get_ih_notices()` 도구 노출
- `lh_api.fetch_lh_notices()`, `ih_api.fetch_ih_notices()` 호출 후 텍스트로 포맷하여 반환
- 환경변수 `OPEN_API_KEY` 필요 (`.mcp.json`의 `env` 필드에 설정)

**도구 파라미터 기본값:**

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `tp_code` | `13` | 공고유형 (아래 유효 코드 표 참조) |
| `cnp_code` | `28` | 지역코드 (28=인천) |
| `status` | `공고중` | 공고상태. 실측값: `공고중`/`접수중`/`접수마감`. 빈 문자열은 PAN_SS 파라미터를 제외(전체). |
| `keyword` | `""` | 공고명 필터 키워드 (API 미지원 — 클라이언트 사이드 필터링) |
| `lookback_days` | `0` | 0이면 날짜 파라미터 없이 현재 활성 공고 포함 조회. 양수이면 과거 마감 공고 조회 (활성 제외됨). |
| `limit` | `100` | 페이지당 공고 수 (API 최대값 실측 확인, 이 이상 올려도 동일) |

**MCP 설정 (`.mcp.json`):**
- `notion` 서버: `npx @notionhq/notion-mcp-server` (NOTION_TOKEN 필요)
- `lh` 서버: `.venv/Scripts/python.exe -m server.lh_mcp` + `cwd` 루트 (OPEN_API_KEY 필요)

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
- 요청: `PAN_ST_DT` / `PAN_ED_DT` → 응답: `PAN_NT_ST_DT` (공고시작일) / `CLSG_DT` (마감일)

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

## IH API (`ih_api.py`)

**엔드포인트:**
- 분양/임대 공고: `https://apis.data.go.kr/B552831/ih/slls-posts`

**요청 파라미터:**

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `serviceKey` | `OPEN_API_KEY` | 공공데이터포털 API 키 |
| `numOfRows` | `100` | 페이지당 건수 |
| `pageNo` | `1` | 페이지 번호 |
| `startCrtrYmd` | (필수) | 조회 시작일 (`YYYY-MM-DD`) |
| `endCrtrYmd` | (필수) | 조회 종료일 (`YYYY-MM-DD`) |
| `sj` | `""` | 공고 제목 필터 키워드 (서버 사이드) |
| `seNm` | `""` | 공고 구분 (`분양`/`임대`, 빈 문자열이면 전체) |

**응답 구조 (Swagger 명세):** `{ header: {resultCode, resultMsg}, body: {pageNo, numOfRows, totalPageNo, totalCount, posts: [...]} }`
**응답 필드:** `tyNm`(유형명), `seNm`(구분), `crtYmd`(등록일), `sj`(제목), `link`(URL) — 고유 ID 없음

**MCP 도구 (`get_ih_notices`) 기본값:**
- `start_date`: 빈 문자열 → MCP 도구에서 1년 전으로 자동 설정
- `end_date`: 빈 문자열 → MCP 도구에서 오늘로 자동 설정
- `keyword`: 공고 제목 필터
- `category`: 공고 구분 (분양/임대)

## Batch (`batch/`)

**LH 배치:**
- `KEYWORD_FILTER = "신혼"` — 공고명 필터 키워드
- `fetch_lh_notices()` → 신혼 키워드 필터링 → `lh_upsert_all(notices)`
- Notion DB: "LH 인천 임대주택 공고" (`NOTION_DATABASE_ID`)

**IH 배치:**
- 최근 1년 전체 공고 조회 (키워드 필터 없음)
- `fetch_ih_notices()` → `ih_upsert_all(notices)`
- Notion DB: "IH 인천도시공사 분양임대 공고" (`IH_NOTION_DATABASE_ID`)
- upsert 식별자: `link` (고유 ID 없음)

**공통:** Notion DB는 최초 실행 시 자동 생성, DB ID를 `.env`에 저장

## Environment Variables (`.env`)

```
OPEN_API_KEY=...          # 공공데이터포털 API 키
NOTION_TOKEN=...          # Notion 통합 토큰
NOTION_PARENT_PAGE_ID=... # Notion 부모 페이지 ID
NOTION_DATABASE_ID=...    # LH Notion DB ID (배치 최초 실행 후 자동 저장)
IH_NOTION_DATABASE_ID=... # IH Notion DB ID (배치 최초 실행 후 자동 저장)
```

## Work Principles

- **분석 우선 (Analyze Before Act)**: 코드 변경 요청을 받으면 즉시 구현하지 말고, 먼저 관련 코드를 읽고 구조·중복도·영향 범위를 근거 기반으로 분석하여 보고한다. 분석 결과 실익이 있을 때만 구현을 진행한다.

## Environment & Configuration

- `.mcp.json`, `.env` — gitignore됨 (API 키 포함)
- MCP 패키지: `.venv`에 직접 설치 (fastmcp, httpx)
- Batch 패키지: `batch/requirements.txt`로 설치
- IDE: PyCharm, 포매터: Black

# Land Housing Bridge

인천 지역 LH(한국토지주택공사)·IH(인천도시공사) 임대주택 공고를 한국 공공데이터포털 API에서 조회하여 Claude AI 도구로 노출하고, Notion DB에 자동 저장하는 브릿지 시스템입니다.

## 주요 기능

### MCP 서버 (`server/`)
Claude AI에게 공고 조회 도구를 제공합니다.

- **`get_incheon_lh_notices`** — LH 임대공고 + 공급정보 조회
  - 공고유형·지역·상태·키워드 필터링
  - 공급정보 동적 컬럼 매핑
- **`get_ih_notices`** — IH 분양/임대 공고 조회
  - 날짜 범위·키워드·구분(분양/임대) 필터링

### 배치 처리 (`batch/`)
공고를 주기적으로 조회하여 Notion DB에 저장합니다.

- **LH 배치** — 매입임대 공고 중 '신혼' 키워드 필터 → Notion DB upsert (PAN_ID 기준)
  - 공급정보를 Notion 테이블 블록으로 변환
  - API에서 사라진 '공고중' 항목을 자동으로 '공고마감' 처리
- **IH 배치** — 최근 1년 전체 공고 → Notion DB upsert (link 기준)
- **자동 실행** — Windows Task Scheduler로 매일 09:00 실행

## 디렉토리 구조

```
lh_api.py               # LH API 공통 로직 (server/, batch/ 공유)
ih_api.py               # IH API 공통 로직 (server/, batch/ 공유)
server/
└── lh_mcp.py           # FastMCP 서버 — AI 도구 노출
batch/
├── main.py             # 배치 진입점 — LH + IH 순차 실행
├── lh_fetcher.py       # lh_api.py 재사용 래퍼
├── ih_fetcher.py       # ih_api.py 재사용 래퍼
├── notion_writer.py    # LH Notion DB upsert
├── ih_notion_writer.py # IH Notion DB upsert
├── setup_scheduler.py  # Windows Task Scheduler 등록
└── requirements.txt
```

## 기술 스택

- **Python 3.14**
- **FastMCP 3.0.2** — MCP 서버 프레임워크
- **HTTPx** — 비동기 HTTP 클라이언트
- **notion-client 3.0.0** — Notion API 클라이언트
- **python-dotenv** — 환경변수 관리

## 설치 및 실행

### 환경 설정

`.env` 파일을 프로젝트 루트에 생성합니다.

```env
OPEN_API_KEY=...          # 공공데이터포털 API 키
NOTION_TOKEN=...          # Notion 통합 토큰
NOTION_PARENT_PAGE_ID=... # Notion 부모 페이지 ID
```

> `NOTION_DATABASE_ID`, `IH_NOTION_DATABASE_ID`는 배치 최초 실행 시 자동 생성·저장됩니다.

### 의존성 설치

```bash
# MCP 서버용 (가상환경)
python -m venv .venv
.venv/Scripts/pip install fastmcp httpx python-dotenv

# 배치용
pip install -r batch/requirements.txt
```

### 실행

```bash
# MCP 서버
.venv/Scripts/python.exe -m server.lh_mcp

# 배치 단독 실행
py -m batch.main

# Windows Task Scheduler 등록 (매일 09:00 자동 실행)
py -m batch.setup_scheduler
```

## 데이터 소스

| 소스 | API | 대상 |
|---|---|---|
| LH (한국토지주택공사) | [공공데이터포털](https://www.data.go.kr/) | 매입임대·전세임대·임대주택 공고 |
| IH (인천도시공사) | [공공데이터포털](https://www.data.go.kr/) | 분양·임대 공고 |

## 라이선스

[MIT License](LICENSE)

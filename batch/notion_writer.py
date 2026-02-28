import os
import logging
from datetime import datetime, timezone
from notion_client import Client
from dotenv import load_dotenv, set_key

ENV_FILE = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(dotenv_path=ENV_FILE)

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")
DB_NAME = "LH 인천 임대주택 공고"

if not NOTION_TOKEN:
    raise EnvironmentError("NOTION_TOKEN 환경변수가 설정되지 않았습니다.")

# notion-version 2022-06-28 명시 → notion.request() 정상 동작
# (3.x 기본값 2025-09-03은 databases/{id}/query 경로가 변경되어 실패)
notion = Client(auth=NOTION_TOKEN, notion_version="2022-06-28")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Notion DB 스키마
# ---------------------------------------------------------------------------
DB_PROPERTIES = {
    "공고ID":    {"rich_text": {}},
    "공고유형":  {"select": {}},
    "지역":      {"select": {}},
    "공고상태":  {"select": {}},
    "공고기간":  {"rich_text": {}},
    "상세URL":   {"url": {}},
    "수집일시":  {"date": {}},
}


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------
def _rich_text(content: str) -> list:
    return [{"type": "text", "text": {"content": content or ""}}]


def _select(value: str) -> dict:
    return {"select": {"name": value}} if value else {"select": None}


def _build_properties(notice: dict, collected_at: str) -> dict:
    period = f"{notice.get('PAN_NT_ST_DT', '')} ~ {notice.get('CLSG_DT', '')}"
    return {
        "공고명":   {"title": _rich_text(notice.get("PAN_NM", ""))},
        "공고ID":   {"rich_text": _rich_text(notice.get("PAN_ID", ""))},
        "공고유형": _select(notice.get("AIS_TP_CD_NM", "")),
        "지역":     _select(notice.get("CNP_CD_NM", "")),
        "공고상태": _select(notice.get("PAN_SS", "")),
        "공고기간": {"rich_text": _rich_text(period)},
        "상세URL":  {"url": notice.get("DTL_URL") or None},
        "수집일시": {"date": {"start": collected_at}},
    }


def _build_supply_blocks(supply_details: list[dict], supply_columns: dict = None) -> list[dict]:
    """공급유형 상세를 Notion 테이블 블록으로 변환.

    supply_columns: API에서 받은 컬럼 정의 {"필드명": "한글명", ...} (dsList01Nm 기반)
                    없으면 첫 번째 행의 키를 컬럼명으로 동적 사용.
    """
    blocks: list[dict] = [
        {
            "type": "heading_2",
            "heading_2": {"rich_text": _rich_text("공급 유형 상세")},
        }
    ]

    if not supply_details:
        blocks.append({
            "type": "paragraph",
            "paragraph": {"rich_text": _rich_text("공급 유형 정보 없음")},
        })
        return blocks

    def _cell(text: str) -> list:
        return [{"type": "text", "text": {"content": str(text or "")}}]

    # 컬럼 정의: API 제공(dsList01Nm)이 있으면 사용, 없으면 첫 번째 행의 키를 그대로 사용
    # (tp_code별 하드코딩 제거 — 실제 데이터 키를 컬럼명으로 동적 사용)
    if supply_columns:
        fields = list(supply_columns.keys())
        col_names = list(supply_columns.values())
    else:
        fields = list(supply_details[0].keys())
        col_names = fields  # 한글명 정보 없으므로 필드명 그대로 표시

    header_row = {
        "type": "table_row",
        "table_row": {"cells": [_cell(name) for name in col_names]},
    }
    data_rows = [
        {
            "type": "table_row",
            "table_row": {"cells": [_cell(d.get(f, "")) for f in fields]},
        }
        for d in supply_details
    ]

    blocks.append({
        "type": "table",
        "table": {
            "table_width": len(fields),
            "has_column_header": True,
            "has_row_header": False,
            "children": [header_row] + data_rows,
        },
    })
    return blocks


# ---------------------------------------------------------------------------
# DB 생성 / 조회 (notion.request() 사용, notion_version=2022-06-28 필수)
# ---------------------------------------------------------------------------
def get_or_create_database() -> str:
    """NOTION_DATABASE_ID 환경변수가 있으면 그대로 사용, 없으면 신규 생성 후 .env에 저장"""
    db_id = os.getenv("NOTION_DATABASE_ID", "").strip().strip("'\"")
    if db_id:
        return db_id

    logger.info(f"Notion DB '{DB_NAME}' 생성 중...")
    response = notion.databases.create(
        parent={"type": "page_id", "page_id": NOTION_PARENT_PAGE_ID},
        title=[{"type": "text", "text": {"content": DB_NAME}}],
        is_inline=True,
        properties={"공고명": {"title": {}}, **DB_PROPERTIES},
    )
    db_id = response["id"]
    set_key(ENV_FILE, "NOTION_DATABASE_ID", db_id)
    logger.info(f"Notion DB 생성 완료 (인라인) - ID: {db_id}")
    return db_id


def _query_db(db_id: str, body: dict) -> dict:
    """DB query (notion.request() 사용)"""
    return notion.request(
        path=f"databases/{db_id}/query",
        method="POST",
        body=body,
    )


def _get_all_pan_id_page_map(db_id: str) -> dict[str, dict]:
    """Notion DB의 모든 페이지를 {PAN_ID: {"page_id": ..., "status": ...}} 형태로 반환.

    페이지네이션을 처리하여 100건 이상의 DB도 전체 조회합니다.
    upsert_all() 시작 시 1번만 호출하여 이후 개별 쿼리를 제거합니다.
    """
    pages = {}
    cursor = None

    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor

        data = _query_db(db_id, body)

        for page in data.get("results", []):
            pan_id_prop = page.get("properties", {}).get("공고ID", {})
            pan_id_list = pan_id_prop.get("rich_text", [])
            pan_id = pan_id_list[0]["text"]["content"] if pan_id_list else ""
            if pan_id:
                status_prop = page.get("properties", {}).get("공고상태", {})
                status = (status_prop.get("select") or {}).get("name", "")
                pages[pan_id] = {
                    "page_id": page["id"],
                    "status": status,
                }

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return pages


def _replace_page_blocks(page_id: str, new_blocks: list[dict]):
    """페이지의 기존 블록 전체 삭제 후 새 블록으로 교체 (페이지네이션 처리)"""
    try:
        cursor = None
        while True:
            kw = {"start_cursor": cursor} if cursor else {}
            existing = notion.blocks.children.list(block_id=page_id, **kw)
            for block in existing.get("results", []):
                notion.blocks.delete(block_id=block["id"])
            if not existing.get("has_more"):
                break
            cursor = existing.get("next_cursor")
    except Exception as e:
        logger.warning(f"기존 블록 삭제 실패 (page_id={page_id}): {e}")

    if new_blocks:
        notion.blocks.children.append(block_id=page_id, children=new_blocks)


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------
def upsert_notice(db_id: str, notice: dict, page_cache: dict[str, dict] | None = None):
    """공고 1건을 Notion DB에 upsert합니다.

    page_cache: {PAN_ID: {"page_id": ..., "status": ...}} 딕셔너리.
                제공 시 개별 DB 쿼리 없이 캐시를 사용합니다.
    """
    collected_at = datetime.now(tz=timezone.utc).isoformat()
    properties = _build_properties(notice, collected_at)
    supply_blocks = _build_supply_blocks(
        notice.get("supply_details", []),
        notice.get("supply_columns"),
    )

    pan_id = notice["PAN_ID"]
    if page_cache is not None:
        cached = page_cache.get(pan_id)
        existing_page_id = cached["page_id"] if cached else None
    else:
        result = _query_db(db_id, {
            "filter": {"property": "공고ID", "rich_text": {"equals": pan_id}}
        })
        results = result.get("results", [])
        existing_page_id = results[0]["id"] if results else None

    if existing_page_id:
        notion.pages.update(page_id=existing_page_id, properties=properties)
        _replace_page_blocks(existing_page_id, supply_blocks)
        logger.info(f"  [업데이트] {notice['PAN_NM']} (PAN_ID={pan_id})")
    else:
        notion.pages.create(
            parent={"type": "database_id", "database_id": db_id},
            properties=properties,
            children=supply_blocks,
        )
        logger.info(f"  [신규등록] {notice['PAN_NM']} (PAN_ID={pan_id})")


def close_expired_notices(db_id: str, current_pan_ids: set[str], page_cache: dict[str, dict] | None = None):
    """Notion에서 공고중이지만 현재 API에 없는 공고를 공고마감으로 업데이트.

    page_cache: {PAN_ID: {"page_id": ..., "status": ...}} 딕셔너리.
                제공 시 별도 DB 쿼리 없이 캐시를 사용합니다.
    """
    if page_cache is not None:
        active_in_notion = {
            pan_id: info["page_id"]
            for pan_id, info in page_cache.items()
            if info["status"] == "공고중"
        }
    else:
        active_in_notion = {}
        cursor = None
        while True:
            body: dict = {
                "filter": {"property": "공고상태", "select": {"equals": "공고중"}},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor
            data = _query_db(db_id, body)
            for page in data.get("results", []):
                pan_id_prop = page.get("properties", {}).get("공고ID", {})
                pan_id_list = pan_id_prop.get("rich_text", [])
                pan_id = pan_id_list[0]["text"]["content"] if pan_id_list else ""
                if pan_id:
                    active_in_notion[pan_id] = page["id"]
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

    expired = {pan_id: page_id for pan_id, page_id in active_in_notion.items()
               if pan_id not in current_pan_ids}

    if not expired:
        return

    logger.info(f"마감 처리 대상: {len(expired)}건")
    for pan_id, page_id in expired.items():
        try:
            notion.pages.update(
                page_id=page_id,
                properties={"공고상태": _select("공고마감")},
            )
            logger.info(f"  [공고마감] PAN_ID={pan_id}")
        except Exception as e:
            logger.error(f"  [오류] 마감 처리 실패 (PAN_ID={pan_id}): {e}")


def upsert_all(notices: list[dict]):
    """공고 목록 전체를 Notion DB에 upsert하고, 마감된 공고는 상태 업데이트.

    DB 쿼리를 시작 시 1번만 수행하여 Notion API 호출을 최소화합니다.
    (기존: 공고 N건 × 1회 + close_expired 1회 = N+1회 → 개선: 1회)
    """
    db_id = get_or_create_database()
    current_pan_ids = {n["PAN_ID"] for n in notices}

    # DB 전체를 1번 조회하여 이후 upsert와 close_expired에 재활용
    logger.info("Notion DB 전체 조회 중...")
    page_cache = _get_all_pan_id_page_map(db_id)
    logger.info(f"기존 등록 공고 수: {len(page_cache)}건")

    success, failed = 0, 0

    for notice in notices:
        try:
            upsert_notice(db_id, notice, page_cache=page_cache)
            success += 1
        except Exception as e:
            logger.error(f"  [오류] {notice.get('PAN_NM', '?')} (PAN_ID={notice.get('PAN_ID', '?')}): {e}")
            failed += 1

    # 공고중이었지만 현재 API에 없는 항목 → 공고마감 처리 (캐시 재활용)
    close_expired_notices(db_id, current_pan_ids, page_cache=page_cache)

    logger.info(f"Notion 저장 완료 - 성공: {success}, 실패: {failed}")

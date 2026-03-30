import hashlib
import json
import logging
from datetime import datetime, timezone
from .notion_base import (
    get_notion_client, rich_text, select, query_db, paginate_query,
    get_or_create_database,
)

logger = logging.getLogger(__name__)

DB_NAME = "LH 인천 임대주택 공고"

# ---------------------------------------------------------------------------
# Notion DB 스키마
# ---------------------------------------------------------------------------
DB_PROPERTIES = {
    "공고ID":    {"rich_text": {}},
    "공고유형":  {"select": {}},
    "지역":      {"select": {}},
    "공고상태":  {"select": {}},
    "공고기간":  {"date": {}},
    "상세URL":   {"url": {}},
    "수집일시":  {"date": {}},
    "첨부파일":  {"files": {}},
    "_블록해시": {"rich_text": {}},
}


# ---------------------------------------------------------------------------
# LH 고유 로직
# ---------------------------------------------------------------------------
def _build_properties(notice: dict, collected_at: str) -> dict:
    start_dt = notice.get("PAN_NT_ST_DT", "").replace(".", "-") or None
    end_dt = notice.get("CLSG_DT", "").replace(".", "-") or None
    if start_dt:
        period_date = {"date": {"start": start_dt, "end": end_dt}}
    elif end_dt:
        period_date = {"date": {"start": end_dt}}
    else:
        period_date = {"date": None}
    return {
        "공고명":   {"title": rich_text(notice.get("PAN_NM", ""))},
        "공고ID":   {"rich_text": rich_text(notice.get("PAN_ID", ""))},
        "공고유형": select(notice.get("AIS_TP_CD_NM", "")),
        "지역":     select(notice.get("CNP_CD_NM", "")),
        "공고상태": select(notice.get("PAN_SS", "")),
        "공고기간": period_date,
        "상세URL":  {"url": notice.get("DTL_URL") or None},
        "수집일시": {"date": {"start": collected_at}},
        "첨부파일": {"files": [
            {"type": "external", "name": f.get("name", "file"),
             "external": {"url": f["url"]}}
            for f in notice.get("_pdf_urls", [])
            if isinstance(f, dict) and f.get("url")
        ]},
    }


def _build_supply_blocks(supply_details: list[dict], supply_columns: dict = None) -> list[dict]:
    """공급유형 상세를 Notion 테이블 블록으로 변환."""
    blocks: list[dict] = [
        {
            "type": "heading_2",
            "heading_2": {"rich_text": rich_text("공급 유형 상세")},
        }
    ]

    if not supply_details:
        blocks.append({
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text("공급 유형 정보 없음")},
        })
        return blocks

    def _cell(text: str) -> list:
        return [{"type": "text", "text": {"content": str(text or "")}}]

    if supply_columns:
        fields = list(supply_columns.keys())
        col_names = list(supply_columns.values())
    else:
        fields = list(supply_details[0].keys())
        col_names = fields

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


def _compute_supply_hash(supply_details: list[dict], supply_columns: dict | None) -> str:
    """공급정보의 해시를 계산하여 변경 감지에 사용."""
    data = json.dumps({"d": supply_details, "c": supply_columns}, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(data.encode()).hexdigest()[:16]


async def _replace_page_blocks(page_id: str, new_blocks: list[dict]):
    """페이지의 기존 블록 전체 삭제 후 새 블록으로 교체 (페이지네이션 처리)"""
    notion = get_notion_client()
    try:
        cursor = None
        while True:
            kw = {"start_cursor": cursor} if cursor else {}
            existing = await notion.blocks.children.list(block_id=page_id, **kw)
            for block in existing.get("results", []):
                await notion.blocks.delete(block_id=block["id"])
            if not existing.get("has_more"):
                break
            cursor = existing.get("next_cursor")
    except Exception as e:
        logger.warning(f"기존 블록 삭제 실패 (page_id={page_id}): {e}")

    if new_blocks:
        await notion.blocks.children.append(block_id=page_id, children=new_blocks)


# ---------------------------------------------------------------------------
# 페이지 캐시 빌드
# ---------------------------------------------------------------------------
async def _get_all_pan_id_page_map(db_id: str) -> dict[str, dict]:
    """Notion DB의 모든 페이지를 {PAN_ID: {"page_id": ..., "status": ..., "blocks_hash": ...}} 형태로 반환."""
    pages = {}
    for page in await paginate_query(db_id):
        props = page.get("properties", {})
        pan_id_list = props.get("공고ID", {}).get("rich_text", [])
        pan_id = pan_id_list[0].get("plain_text", "") if pan_id_list else ""
        if pan_id:
            status = (props.get("공고상태", {}).get("select") or {}).get("name", "")
            hash_list = props.get("_블록해시", {}).get("rich_text", [])
            blocks_hash = hash_list[0].get("plain_text", "") if hash_list else ""
            pages[pan_id] = {
                "page_id": page["id"],
                "status": status,
                "blocks_hash": blocks_hash,
            }
    return pages


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------
async def upsert_notice(db_id: str, notice: dict, page_cache: dict[str, dict] | None = None):
    """공고 1건을 Notion DB에 upsert합니다."""
    notion = get_notion_client()
    collected_at = datetime.now(tz=timezone.utc).isoformat()
    properties = _build_properties(notice, collected_at)

    supply_details = notice.get("supply_details", [])
    supply_columns = notice.get("supply_columns")
    supply_blocks = _build_supply_blocks(supply_details, supply_columns)
    new_hash = _compute_supply_hash(supply_details, supply_columns)
    properties["_블록해시"] = {"rich_text": rich_text(new_hash)}

    pan_id = notice["PAN_ID"]
    if page_cache is not None:
        cached = page_cache.get(pan_id)
        existing_page_id = cached["page_id"] if cached else None
    else:
        result = await query_db(db_id, {
            "filter": {"property": "공고ID", "rich_text": {"equals": pan_id}}
        })
        results = result.get("results", [])
        existing_page_id = results[0]["id"] if results else None
        cached = None

    if existing_page_id:
        await notion.pages.update(page_id=existing_page_id, properties=properties)
        cached_hash = (cached or {}).get("blocks_hash", "")
        if new_hash != cached_hash:
            await _replace_page_blocks(existing_page_id, supply_blocks)
        logger.info(f"  [업데이트] {notice['PAN_NM']} (PAN_ID={pan_id})")
        return False
    else:
        await notion.pages.create(
            parent={"type": "database_id", "database_id": db_id},
            properties=properties,
            children=supply_blocks,
        )
        logger.info(f"  [신규등록] {notice['PAN_NM']} (PAN_ID={pan_id})")
        return True


async def close_expired_notices(db_id: str, current_pan_ids: set[str], page_cache: dict[str, dict] | None = None):
    """Notion에서 공고중이지만 현재 API에 없는 공고를 공고마감으로 업데이트."""
    notion = get_notion_client()
    if page_cache is not None:
        active_in_notion = {
            pan_id: info["page_id"]
            for pan_id, info in page_cache.items()
            if info["status"] == "공고중"
        }
    else:
        active_in_notion = {}
        body_base = {"filter": {"property": "공고상태", "select": {"equals": "공고중"}}}
        for page in await paginate_query(db_id, body_base):
            pan_id_prop = page.get("properties", {}).get("공고ID", {})
            pan_id_list = pan_id_prop.get("rich_text", [])
            pan_id = pan_id_list[0].get("plain_text", "") if pan_id_list else ""
            if pan_id:
                active_in_notion[pan_id] = page["id"]

    # Zero-result guard: API 결과 0건인데 Notion에 활성 공고가 있으면 장애 의심
    if not current_pan_ids and active_in_notion:
        logger.warning(f"Zero-result guard: API 결과 0건, Notion 활성 공고 {len(active_in_notion)}건 — 마감 처리 건너뜀")
        return 0

    expired = {pan_id: page_id for pan_id, page_id in active_in_notion.items()
               if pan_id not in current_pan_ids}

    if not expired:
        return 0

    closed = 0
    logger.info(f"마감 처리 대상: {len(expired)}건")
    for pan_id, page_id in expired.items():
        try:
            await notion.pages.update(
                page_id=page_id,
                properties={"공고상태": select("공고마감")},
            )
            logger.info(f"  [공고마감] PAN_ID={pan_id}")
            closed += 1
        except Exception as e:
            logger.error(f"  [오류] 마감 처리 실패 (PAN_ID={pan_id}): {e}")
    return closed


async def upsert_all(notices: list[dict]) -> dict:
    """공고 목록 전체를 Notion DB에 upsert하고, 마감된 공고는 상태 업데이트.

    Returns:
        dict: {"new": int, "updated": int, "closed": int, "failed": int, "new_notices": list}
    """
    db_id = await get_or_create_database("NOTION_DATABASE_ID", DB_NAME, DB_PROPERTIES)
    current_pan_ids = {n["PAN_ID"] for n in notices}

    logger.info("Notion DB 전체 조회 중...")
    page_cache = await _get_all_pan_id_page_map(db_id)
    logger.info(f"기존 등록 공고 수: {len(page_cache)}건")

    new, updated, failed = 0, 0, 0
    new_notices: list[dict] = []
    failed_notices: list[dict] = []

    for notice in notices:
        try:
            is_new = await upsert_notice(db_id, notice, page_cache=page_cache)
            if is_new:
                new += 1
                new_notices.append(notice)
            else:
                updated += 1
        except Exception as e:
            logger.error(f"  [오류] {notice.get('PAN_NM', '?')} (PAN_ID={notice.get('PAN_ID', '?')}): {e}")
            failed += 1
            failed_notices.append({
                "PAN_ID": notice.get("PAN_ID", ""),
                "PAN_NM": notice.get("PAN_NM", ""),
                "error": str(e),
            })

    supply_errors = sum(1 for n in notices if n.get("supply_error"))
    if supply_errors:
        logger.warning(f"공급정보 조회 실패: {supply_errors}건")

    closed = await close_expired_notices(db_id, current_pan_ids, page_cache=page_cache)

    logger.info(f"Notion 저장 완료 - 신규: {new}, 업데이트: {updated}, 마감: {closed}, 실패: {failed}")
    return {
        "new": new, "updated": updated, "closed": closed, "failed": failed,
        "supply_errors": supply_errors,
        "new_notices": new_notices, "failed_notices": failed_notices,
    }

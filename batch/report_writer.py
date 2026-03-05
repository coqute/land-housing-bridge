"""배치 실행 리포트를 Notion DB에 생성합니다."""
import logging
from datetime import datetime, timezone
from .notion_base import get_notion_client, rich_text, select, get_or_create_database

logger = logging.getLogger(__name__)

DB_NAME = "배치 실행 리포트"

DB_PROPERTIES = {
    "실행일시":     {"date": {}},
    "소요시간":     {"rich_text": {}},
    "LH신규":       {"number": {}},
    "LH업데이트":   {"number": {}},
    "LH마감":       {"number": {}},
    "IH신규":       {"number": {}},
    "IH업데이트":   {"number": {}},
    "IH마감":       {"number": {}},
    "LH실패":       {"number": {}},
    "IH실패":       {"number": {}},
    "상태":         {"select": {}},
}


def _determine_status(lh_ok: bool, ih_ok: bool) -> str:
    if lh_ok and ih_ok:
        return "성공"
    if not lh_ok and not ih_ok:
        return "실패"
    return "부분실패"


_MAX_DETAIL_ITEMS = 20  # Notion API 100-block 제한 방어 (heading 포함 여유 확보)


def _append_section(blocks: list[dict], heading: str, items: list, format_fn) -> None:
    """heading + bullet list 섹션을 blocks에 추가 (_MAX_DETAIL_ITEMS 초과 시 overflow 표시)."""
    if not items:
        return
    blocks.append({
        "type": "heading_3",
        "heading_3": {"rich_text": rich_text(f"{heading} ({len(items)}건)")},
    })
    for item in items[:_MAX_DETAIL_ITEMS]:
        blocks.append({
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": rich_text(format_fn(item))},
        })
    if len(items) > _MAX_DETAIL_ITEMS:
        blocks.append({
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text(f"... 외 {len(items) - _MAX_DETAIL_ITEMS}건")},
        })


def _build_detail_blocks(lh_result: dict | None, ih_result: dict | None) -> list[dict]:
    """신규·실패 공고 목록을 bulleted_list_item 블록으로 생성.

    Notion API children 제한(100 블록)을 초과하지 않도록 카테고리별 최대 _MAX_DETAIL_ITEMS건만 포함.
    """
    blocks: list[dict] = []
    lh = lh_result or {}
    ih = ih_result or {}

    _append_section(blocks, "LH 신규 공고", lh.get("new_notices", []),
                    lambda n: f"{n.get('PAN_NM', '')} (ID: {n.get('PAN_ID', '')})")
    _append_section(blocks, "IH 신규 공고", ih.get("new_notices", []),
                    lambda n: n.get("sj", ""))
    _append_section(blocks, "LH 실패 공고", lh.get("failed_notices", []),
                    lambda n: f"{n.get('PAN_NM', '')} (ID: {n.get('PAN_ID', '')}) — {n.get('error', '')}")
    _append_section(blocks, "IH 실패 공고", ih.get("failed_notices", []),
                    lambda n: f"{n.get('sj', '')} — {n.get('error', '')}")

    if not blocks:
        blocks.append({
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text("신규 공고 없음")},
        })

    return blocks


def write_report(
    lh_result: dict | None,
    ih_result: dict | None,
    elapsed_seconds: float,
    lh_ok: bool,
    ih_ok: bool,
):
    """배치 실행 리포트 1건을 Notion DB에 생성합니다."""
    db_id = get_or_create_database(
        "REPORT_DATABASE_ID", DB_NAME, DB_PROPERTIES, title_name="리포트명",
    )

    now = datetime.now(tz=timezone.utc)
    title = now.strftime("%Y-%m-%d %H:%M") + " 배치 리포트"
    elapsed_str = f"{elapsed_seconds:.1f}초"
    status = _determine_status(lh_ok, ih_ok)

    lh = lh_result or {}
    ih = ih_result or {}

    properties = {
        "리포트명":   {"title": rich_text(title)},
        "실행일시":   {"date": {"start": now.isoformat()}},
        "소요시간":   {"rich_text": rich_text(elapsed_str)},
        "LH신규":     {"number": lh.get("new", 0)},
        "LH업데이트": {"number": lh.get("updated", 0)},
        "LH마감":     {"number": lh.get("closed", 0)},
        "IH신규":     {"number": ih.get("new", 0)},
        "IH업데이트": {"number": ih.get("updated", 0)},
        "IH마감":     {"number": ih.get("closed", 0)},
        "LH실패":     {"number": lh.get("failed", 0)},
        "IH실패":     {"number": ih.get("failed", 0)},
        "상태":       select(status),
    }

    detail_blocks = _build_detail_blocks(lh_result, ih_result)

    notion = get_notion_client()
    notion.pages.create(
        parent={"type": "database_id", "database_id": db_id},
        properties=properties,
        children=detail_blocks,
    )
    logger.info(f"배치 리포트 생성 완료: {title}")

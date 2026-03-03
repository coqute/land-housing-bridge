"""IH(인천도시공사) 공고를 Notion DB에 저장합니다.

LH notion_writer.py와 동일한 패턴이지만, IH 고유 스키마를 사용합니다.
- 고유 ID 없음 → link(공고 URL)를 upsert 식별자로 사용
- 공고상태 없음 → close_expired 불필요
- 공급정보 없음 → supply_blocks 불필요
"""
import logging
from datetime import datetime, timezone
from .notion_base import (
    get_notion_client, rich_text, select, query_db, paginate_query,
    get_or_create_database,
)

logger = logging.getLogger(__name__)

DB_NAME = "IH 인천도시공사 분양임대 공고"

# ---------------------------------------------------------------------------
# Notion DB 스키마
# ---------------------------------------------------------------------------
DB_PROPERTIES = {
    "공고구분": {"select": {}},
    "유형":     {"select": {}},
    "등록일":   {"rich_text": {}},
    "링크":     {"url": {}},
    "수집일시": {"date": {}},
}


# ---------------------------------------------------------------------------
# IH 고유 로직
# ---------------------------------------------------------------------------
def _build_properties(notice: dict, collected_at: str) -> dict:
    return {
        "공고명":   {"title": rich_text(notice.get("sj", ""))},
        "공고구분": select(notice.get("seNm", "")),
        "유형":     select(notice.get("tyNm", "")),
        "등록일":   {"rich_text": rich_text(notice.get("crtYmd", ""))},
        "링크":     {"url": notice.get("link") or None},
        "수집일시": {"date": {"start": collected_at}},
    }


# ---------------------------------------------------------------------------
# 페이지 캐시 빌드
# ---------------------------------------------------------------------------
def _get_all_link_page_map(db_id: str) -> dict[str, str]:
    """Notion DB의 모든 페이지를 {link: page_id} 형태로 반환."""
    pages = {}
    for page in paginate_query(db_id):
        link_prop = page.get("properties", {}).get("링크", {})
        link = link_prop.get("url", "")
        if link:
            pages[link] = page["id"]
    return pages


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------
def upsert_notice(db_id: str, notice: dict, page_cache: dict[str, str] | None = None):
    """공고 1건을 Notion DB에 upsert합니다."""
    notion = get_notion_client()
    collected_at = datetime.now(tz=timezone.utc).isoformat()
    properties = _build_properties(notice, collected_at)

    link = notice.get("link", "")
    title = notice.get("sj", "")

    if page_cache is not None:
        existing_page_id = page_cache.get(link)
    else:
        if link:
            result = query_db(db_id, {
                "filter": {"property": "링크", "url": {"equals": link}}
            })
            results = result.get("results", [])
            existing_page_id = results[0]["id"] if results else None
        else:
            existing_page_id = None

    if existing_page_id:
        notion.pages.update(page_id=existing_page_id, properties=properties)
        logger.info(f"  [업데이트] {title}")
    else:
        notion.pages.create(
            parent={"type": "database_id", "database_id": db_id},
            properties=properties,
        )
        logger.info(f"  [신규등록] {title}")


def upsert_all(notices: list[dict]):
    """공고 목록 전체를 Notion DB에 upsert합니다."""
    db_id = get_or_create_database("IH_NOTION_DATABASE_ID", DB_NAME, DB_PROPERTIES)

    logger.info("IH Notion DB 전체 조회 중...")
    page_cache = _get_all_link_page_map(db_id)
    logger.info(f"기존 등록 공고 수: {len(page_cache)}건")

    success, failed = 0, 0

    for notice in notices:
        try:
            upsert_notice(db_id, notice, page_cache=page_cache)
            success += 1
        except Exception as e:
            logger.error(f"  [오류] {notice.get('sj', '?')}: {e}")
            failed += 1

    logger.info(f"IH Notion 저장 완료 - 성공: {success}, 실패: {failed}")

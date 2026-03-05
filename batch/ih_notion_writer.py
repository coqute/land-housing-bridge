"""IH(인천도시공사) 공고를 Notion DB에 저장합니다.

LH notion_writer.py와 동일한 패턴이지만, IH 고유 스키마를 사용합니다.
- 고유 ID 없음 → link(공고 URL)를 upsert 식별자로 사용
- 공고상태 없음 → API 조회 결과 차집합으로 만료 판별
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
    "상태":     {"select": {}},
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
        "상태":     select("모집중"),
        "수집일시": {"date": {"start": collected_at}},
    }


# ---------------------------------------------------------------------------
# 페이지 캐시 빌드
# ---------------------------------------------------------------------------
def _get_all_link_page_map(db_id: str) -> dict[str, dict]:
    """Notion DB의 모든 페이지를 {link: {"id": page_id, "status": str}} 형태로 반환."""
    pages = {}
    for page in paginate_query(db_id):
        props = page.get("properties", {})
        link = props.get("링크", {}).get("url", "")
        if link:
            status_sel = props.get("상태", {}).get("select")
            status = status_sel.get("name", "") if status_sel else ""
            pages[link] = {"id": page["id"], "status": status}
    return pages


# ---------------------------------------------------------------------------
# 만료 처리
# ---------------------------------------------------------------------------
def close_expired_notices(
    active_links: set[str], page_cache: dict[str, dict],
) -> int:
    """DB의 '모집중' 공고 중 active_links에 없는 것을 '마감' 처리.

    API 조회 결과(active_links)에 포함되지 않으면 만료로 판별합니다.
    - 조회 범위(90일) 밖으로 밀려남
    - API에서 삭제됨
    - 제목 변경으로 필터 탈락 (예: "모집 공고" → "모집 결과")
    """
    notion = get_notion_client()
    closed = 0

    for link, info in page_cache.items():
        if link not in active_links and info["status"] != "마감":
            notion.pages.update(
                page_id=info["id"],
                properties={"상태": select("마감")},
            )
            closed += 1

    if closed:
        logger.info(f"IH 만료 처리: {closed}건")
    return closed


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------
def upsert_notice(db_id: str, notice: dict, page_cache: dict[str, dict] | None = None):
    """공고 1건을 Notion DB에 upsert합니다."""
    notion = get_notion_client()
    collected_at = datetime.now(tz=timezone.utc).isoformat()
    properties = _build_properties(notice, collected_at)

    link = notice.get("link", "")
    title = notice.get("sj", "")

    if page_cache is not None:
        info = page_cache.get(link)
        existing_page_id = info["id"] if info else None
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
        return False
    else:
        notion.pages.create(
            parent={"type": "database_id", "database_id": db_id},
            properties=properties,
        )
        logger.info(f"  [신규등록] {title}")
        return True


def upsert_all(notices: list[dict]) -> dict:
    """공고 목록 전체를 Notion DB에 upsert합니다.

    Returns:
        dict: {"new": int, "updated": int, "closed": int, "failed": int, "new_notices": list}
    """
    db_id = get_or_create_database("IH_NOTION_DATABASE_ID", DB_NAME, DB_PROPERTIES)

    logger.info("IH Notion DB 전체 조회 중...")
    page_cache = _get_all_link_page_map(db_id)
    logger.info(f"기존 등록 공고 수: {len(page_cache)}건")

    new, updated, failed = 0, 0, 0
    new_notices: list[dict] = []

    for notice in notices:
        try:
            is_new = upsert_notice(db_id, notice, page_cache=page_cache)
            if is_new:
                new += 1
                new_notices.append(notice)
            else:
                updated += 1
        except Exception as e:
            logger.error(f"  [오류] {notice.get('sj', '?')}: {e}")
            failed += 1

    active_links = {n.get("link") for n in notices if n.get("link")}
    closed = close_expired_notices(active_links, page_cache)

    logger.info(f"IH Notion 저장 완료 - 신규: {new}, 업데이트: {updated}, 마감: {closed}, 실패: {failed}")
    return {"new": new, "updated": updated, "closed": closed, "failed": failed, "new_notices": new_notices}

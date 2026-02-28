"""IH(인천도시공사) 공고를 Notion DB에 저장합니다.

LH notion_writer.py와 동일한 패턴이지만, IH 고유 스키마를 사용합니다.
- 고유 ID 없음 → link(공고 URL)를 upsert 식별자로 사용
- 공고상태 없음 → close_expired 불필요
- 공급정보 없음 → supply_blocks 불필요
"""
import os
import logging
from datetime import datetime, timezone
from notion_client import Client
from dotenv import load_dotenv, set_key

ENV_FILE = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(dotenv_path=ENV_FILE)

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")
DB_NAME = "IH 인천도시공사 분양임대 공고"

if not NOTION_TOKEN:
    raise EnvironmentError("NOTION_TOKEN 환경변수가 설정되지 않았습니다.")

notion = Client(auth=NOTION_TOKEN, notion_version="2022-06-28")

logger = logging.getLogger(__name__)


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
# 내부 헬퍼
# ---------------------------------------------------------------------------
def _rich_text(content: str) -> list:
    return [{"type": "text", "text": {"content": content or ""}}]


def _select(value: str) -> dict:
    return {"select": {"name": value}} if value else {"select": None}


def _build_properties(notice: dict, collected_at: str) -> dict:
    return {
        "공고명":   {"title": _rich_text(notice.get("sj", ""))},
        "공고구분": _select(notice.get("seNm", "")),
        "유형":     _select(notice.get("tyNm", "")),
        "등록일":   {"rich_text": _rich_text(notice.get("crtYmd", ""))},
        "링크":     {"url": notice.get("link") or None},
        "수집일시": {"date": {"start": collected_at}},
    }


# ---------------------------------------------------------------------------
# DB 생성 / 조회
# ---------------------------------------------------------------------------
def get_or_create_database() -> str:
    """IH_NOTION_DATABASE_ID 환경변수가 있으면 그대로 사용, 없으면 신규 생성 후 .env에 저장"""
    db_id = os.getenv("IH_NOTION_DATABASE_ID", "").strip().strip("'\"")
    if db_id:
        return db_id

    logger.info(f"Notion DB '{DB_NAME}' 생성 중...")
    # notion.databases.create()는 notion_version=2022-06-28에서 properties 전달 실패
    # → notion.request()로 직접 호출
    response = notion.request(
        path="databases",
        method="POST",
        body={
            "parent": {"type": "page_id", "page_id": NOTION_PARENT_PAGE_ID},
            "title": [{"type": "text", "text": {"content": DB_NAME}}],
            "is_inline": True,
            "properties": {"공고명": {"title": {}}, **DB_PROPERTIES},
        },
    )
    db_id = response["id"]
    set_key(ENV_FILE, "IH_NOTION_DATABASE_ID", db_id)
    logger.info(f"Notion DB 생성 완료 (인라인) - ID: {db_id}")
    return db_id


def _query_db(db_id: str, body: dict) -> dict:
    return notion.request(
        path=f"databases/{db_id}/query",
        method="POST",
        body=body,
    )


def _get_all_link_page_map(db_id: str) -> dict[str, str]:
    """Notion DB의 모든 페이지를 {link: page_id} 형태로 반환.

    IH 공고는 고유 ID가 없으므로 link(공고 URL)를 식별자로 사용합니다.
    """
    pages = {}
    cursor = None

    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor

        data = _query_db(db_id, body)

        for page in data.get("results", []):
            link_prop = page.get("properties", {}).get("링크", {})
            link = link_prop.get("url", "")
            if link:
                pages[link] = page["id"]

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return pages


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------
def upsert_notice(db_id: str, notice: dict, page_cache: dict[str, str] | None = None):
    """공고 1건을 Notion DB에 upsert합니다.

    page_cache: {link: page_id} 딕셔너리. 제공 시 개별 DB 쿼리 없이 캐시를 사용합니다.
    """
    collected_at = datetime.now(tz=timezone.utc).isoformat()
    properties = _build_properties(notice, collected_at)

    link = notice.get("link", "")
    title = notice.get("sj", "")

    if page_cache is not None:
        existing_page_id = page_cache.get(link)
    else:
        if link:
            result = _query_db(db_id, {
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
    db_id = get_or_create_database()

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

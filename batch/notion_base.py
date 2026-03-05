"""Notion 공통 로직 — notion_writer.py, ih_notion_writer.py에서 공유"""
import os
import logging
from dotenv import set_key
from config import NOTION_TOKEN, NOTION_PARENT_PAGE_ID

logger = logging.getLogger(__name__)

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')

_notion_client = None


def get_notion_client():
    """Notion 클라이언트 지연 초기화 (import 시 crash 방지)"""
    global _notion_client
    if _notion_client is None:
        if not NOTION_TOKEN:
            raise EnvironmentError("NOTION_TOKEN 환경변수가 설정되지 않았습니다.")
        from notion_client import Client
        _notion_client = Client(auth=NOTION_TOKEN, notion_version="2022-06-28")
    return _notion_client


def rich_text(content: str) -> list:
    return [{"type": "text", "text": {"content": content or ""}}]


def select(value: str) -> dict:
    return {"select": {"name": value}} if value else {"select": None}


def query_db(db_id: str, body: dict) -> dict:
    """DB query (notion.request() 사용)"""
    return get_notion_client().request(
        path=f"databases/{db_id}/query",
        method="POST",
        body=body,
    )


def paginate_query(db_id: str, body_base: dict | None = None) -> list[dict]:
    """DB를 페이지네이션으로 전체 조회하여 모든 page를 반환"""
    all_pages = []
    cursor = None

    while True:
        body = {"page_size": 100, **(body_base or {})}
        if cursor:
            body["start_cursor"] = cursor

        data = query_db(db_id, body)
        all_pages.extend(data.get("results", []))

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return all_pages


_checked_dbs: set[str] = set()


def _ensure_db_properties(db_id: str, expected_properties: dict):
    """기존 DB에 누락된 속성이 있으면 추가 (스키마 drift 방지, 프로세스당 1회)"""
    if db_id in _checked_dbs:
        return
    _checked_dbs.add(db_id)

    notion = get_notion_client()
    db_info = notion.request(path=f"databases/{db_id}", method="GET")
    existing = set(db_info.get("properties", {}).keys())
    missing = {k: v for k, v in expected_properties.items() if k not in existing}
    if missing:
        notion.request(
            path=f"databases/{db_id}", method="PATCH",
            body={"properties": missing},
        )
        logger.info(f"DB 속성 추가: {list(missing.keys())}")


def get_or_create_database(env_key: str, db_name: str, db_properties: dict, title_name: str = "공고명") -> str:
    """env_key 환경변수에 DB ID가 있으면 반환, 없으면 신규 생성 후 .env에 저장"""
    db_id = os.getenv(env_key, "").strip().strip("'\"")
    if db_id:
        _ensure_db_properties(db_id, db_properties)
        return db_id

    notion = get_notion_client()
    logger.info(f"Notion DB '{db_name}' 생성 중...")
    response = notion.request(
        path="databases",
        method="POST",
        body={
            "parent": {"type": "page_id", "page_id": NOTION_PARENT_PAGE_ID},
            "title": [{"type": "text", "text": {"content": db_name}}],
            "is_inline": True,
            "properties": {title_name: {"title": {}}, **db_properties},
        },
    )
    db_id = response["id"]
    set_key(ENV_FILE, env_key, db_id)
    logger.info(f"Notion DB 생성 완료 (인라인) - ID: {db_id}")
    return db_id

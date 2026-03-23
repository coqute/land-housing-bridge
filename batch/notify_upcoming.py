"""마감 임박 공고에 Notion 코멘트 알림을 생성합니다."""
import logging
from datetime import datetime, timedelta
from .notion_base import get_notion_client, query_db

logger = logging.getLogger(__name__)


def _parse_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None


def send_deadline_notifications(db_id: str, label: str = "LH") -> int:
    """접수마감일이 7일 이내인 미알림 공고에 코멘트를 생성합니다.

    Returns:
        int: 알림 발송 건수
    """
    notion = get_notion_client()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    deadline = today + timedelta(days=7)

    body = {
        "filter": {
            "and": [
                {"property": "접수마감일", "date": {"on_or_after": today.strftime("%Y-%m-%d")}},
                {"property": "접수마감일", "date": {"on_or_before": deadline.strftime("%Y-%m-%d")}},
                {"property": "알림완료", "checkbox": {"equals": False}},
            ]
        }
    }

    results = query_db(db_id, body).get("results", [])
    if not results:
        return 0

    notified = 0
    for page in results:
        page_id = page["id"]
        props = page.get("properties", {})

        title_prop = props.get("공고명", {}).get("title", [])
        title = title_prop[0]["text"]["content"] if title_prop else "제목 없음"

        deadline_prop = props.get("접수마감일", {}).get("date")
        if not deadline_prop or not deadline_prop.get("start"):
            continue
        deadline_str = deadline_prop["start"]
        deadline_dt = _parse_date(deadline_str)
        if not deadline_dt:
            continue

        d_day = (deadline_dt - today).days

        try:
            notion.comments.create(
                parent={"page_id": page_id},
                rich_text=[{
                    "type": "text",
                    "text": {"content": f"접수 마감 D-{d_day}일 알림 -- 마감일: {deadline_str}"}
                }],
            )
            notion.pages.update(
                page_id=page_id,
                properties={"알림완료": {"checkbox": True}},
            )
            logger.info(f"  [{label} 알림] {title} (D-{d_day})")
            notified += 1
        except Exception as e:
            logger.error(f"  [{label} 알림 실패] {title}: {e}")

    if notified:
        logger.info(f"{label} 마감 알림 발송: {notified}건")
    return notified

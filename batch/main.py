import asyncio
import logging
import os
import sys

from .lh_fetcher import fetch_lh_notices
from .notion_writer import upsert_all as lh_upsert_all
from .ih_fetcher import fetch_all_ih_notices
from .ih_notion_writer import upsert_all as ih_upsert_all

# ---------------------------------------------------------------------------
# 로깅 설정 (콘솔 + 파일 동시 출력)
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "batch.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


KEYWORD_FILTER = "신혼"


async def run_lh_batch():
    """LH 공고 배치: 매입임대 + 신혼 키워드 필터 → Notion DB upsert"""
    logger.info("-" * 40)
    logger.info("LH 배치 시작")

    try:
        notices = await fetch_lh_notices(keyword=KEYWORD_FILTER)
    except Exception as e:
        logger.error(f"LH API 조회 실패: {e}")
        return False

    logger.info(f"'{KEYWORD_FILTER}' 필터 조회 결과: {len(notices)}건")

    if not notices:
        logger.info("LH 해당 공고 없음.")
        return True

    try:
        lh_upsert_all(notices)
    except Exception as e:
        logger.error(f"LH Notion 저장 중 오류: {e}")
        return False

    logger.info("LH 배치 완료")
    return True


async def run_ih_batch():
    """IH 공고 배치: 최근 1년 전체 조회 → Notion DB upsert"""
    logger.info("-" * 40)
    logger.info("IH 배치 시작")

    from datetime import datetime, timedelta
    today = datetime.now()
    start_date = (today - timedelta(days=365)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    try:
        notices = await fetch_all_ih_notices(
            startCrtrYmd=start_date,
            endCrtrYmd=end_date,
            seNm="임대",
            tyNm="일반임대",
        )
    except Exception as e:
        logger.error(f"IH API 조회 실패: {e}")
        return False

    logger.info(f"IH 조회 결과: {len(notices)}건")

    if not notices:
        logger.info("IH 해당 공고 없음.")
        return True

    try:
        ih_upsert_all(notices)
    except Exception as e:
        logger.error(f"IH Notion 저장 중 오류: {e}")
        return False

    logger.info("IH 배치 완료")
    return True


async def main():
    logger.info("=" * 50)
    logger.info("인천 임대주택 공고 배치 시작 (LH + IH)")

    lh_ok = await run_lh_batch()
    ih_ok = await run_ih_batch()

    if not lh_ok or not ih_ok:
        failed = []
        if not lh_ok:
            failed.append("LH")
        if not ih_ok:
            failed.append("IH")
        logger.error(f"배치 일부 실패: {', '.join(failed)}")
        sys.exit(1)

    logger.info("인천 임대주택 공고 배치 완료 (LH + IH)")
    logger.info("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())

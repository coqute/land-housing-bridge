import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta

from lh_api import fetch_lh_notices
from .notion_writer import upsert_all as lh_upsert_all
from ih_api import fetch_all_ih_notices
from .ih_notion_writer import upsert_all as ih_upsert_all
from .report_writer import write_report

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


LH_TP_CODES = ["13", "06"]  # 매입/전세임대 + 임대주택(행복주택, 국민임대 등)

_NOISE_KEYWORDS = ("마감", "취소", "결과", "계약", "입주안내", "변경", "정정")


def _is_recruitment_notice(sj: str) -> bool:
    """입주자 모집 공고 여부 판별 (모집+공고 포함, 노이즈 키워드 제외)"""
    return "모집" in sj and "공고" in sj and not any(kw in sj for kw in _NOISE_KEYWORDS)


async def run_lh_batch():
    """LH 공고 배치: 매입임대 + 임대주택 → Notion DB upsert

    Returns:
        tuple[bool, dict | None]: (성공여부, upsert 결과)
    """
    logger.info("-" * 40)
    logger.info("LH 배치 시작")

    try:
        results = await asyncio.gather(
            *(fetch_lh_notices(tp_code=tp) for tp in LH_TP_CODES)
        )
    except Exception as e:
        logger.error(f"LH API 조회 실패: {e}")
        return False, None

    # PAN_ID 기준 중복 제거 병합
    seen: dict[str, dict] = {}
    for batch in results:
        for n in batch:
            pid = n.get("PAN_ID", "")
            if pid and pid not in seen:
                seen[pid] = n
    notices = list(seen.values())

    logger.info(f"LH 조회 결과: {len(notices)}건 (tp_code={','.join(LH_TP_CODES)})")

    if not notices:
        logger.info("LH 해당 공고 없음.")
        return True, {"new": 0, "updated": 0, "closed": 0, "failed": 0, "new_notices": [], "failed_notices": []}

    try:
        result = lh_upsert_all(notices)
    except Exception as e:
        logger.error(f"LH Notion 저장 중 오류: {e}")
        return False, None

    logger.info("LH 배치 완료")
    return True, result


async def run_ih_batch():
    """IH 공고 배치: 최근 90일 입주자 모집 공고 → Notion DB upsert

    Returns:
        tuple[bool, dict | None]: (성공여부, upsert 결과)
    """
    logger.info("-" * 40)
    logger.info("IH 배치 시작")

    today = datetime.now()
    start_date = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    try:
        notices = await fetch_all_ih_notices(
            startCrtrYmd=start_date,
            endCrtrYmd=end_date,
            sj="입주자",
        )
    except Exception as e:
        logger.error(f"IH API 조회 실패: {e}")
        return False, None

    # 입주자 모집 공고만 필터 (마감안내, 모집결과, 취소 등 노이즈 제외)
    raw_count = len(notices)
    notices = [n for n in notices if _is_recruitment_notice(n.get("sj", ""))]
    logger.info(f"IH 조회 결과: {raw_count}건 → 모집공고 필터 후 {len(notices)}건")

    try:
        result = ih_upsert_all(notices)
    except Exception as e:
        logger.error(f"IH Notion 저장 중 오류: {e}")
        return False, None

    logger.info("IH 배치 완료")
    return True, result


async def main():
    logger.info("=" * 50)
    logger.info("인천 임대주택 공고 배치 시작 (LH + IH)")
    start_time = time.time()

    lh_ok, lh_result = await run_lh_batch()
    ih_ok, ih_result = await run_ih_batch()

    elapsed = time.time() - start_time

    try:
        write_report(lh_result, ih_result, elapsed, lh_ok, ih_ok)
    except Exception as e:
        logger.error(f"배치 리포트 생성 실패: {e}")

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

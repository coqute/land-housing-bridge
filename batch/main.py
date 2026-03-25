import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta

from config import validate_env, LH_TP_CODES
from lh_api import fetch_lh_notices, dedup_by_pan_id
from .notion_writer import upsert_all as lh_upsert_all
from ih_api import fetch_all_ih_notices
from .ih_notion_writer import upsert_all as ih_upsert_all
from .report_writer import write_report
from .notify_upcoming import send_deadline_notifications
from doc_processor import scrape_lh_detail, scrape_ih_detail, create_scrape_client

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


IH_LOOKBACK_DAYS = 90

_NOISE_KEYWORDS = ("마감", "취소", "결과", "계약", "입주안내", "변경", "정정")
_SCRAPE_DELAY = 1.0  # 정부 사이트 rate limit 예의
_SCRAPE_CONCURRENCY = 3  # 스크래핑 동시 요청 수 (LH/IH 각각)


def _is_recruitment_notice(notice: dict) -> bool:
    """임대주택 입주자 모집 공고 여부 판별.

    - 제목에 "모집" + "공고" 포함, 노이즈 키워드 미포함
    - tyNm(유형명)에 "임대" 포함 필수 (분양 아파트 잔여세대 등 제외)
    """
    sj = notice.get("sj", "")
    tyNm = notice.get("tyNm", "")
    return (
        "모집" in sj
        and "공고" in sj
        and "임대" in tyNm
        and not any(kw in sj for kw in _NOISE_KEYWORDS)
    )


async def _scrape_pdf_urls(notices: list[dict], source: str) -> None:
    """공고 목록의 상세 페이지를 스크래핑하여 PDF URL을 notice dict에 추가.

    best-effort: 스크래핑 실패 시 빈 리스트 설정, 배치 진행에 영향 없음.
    Semaphore로 동시 요청 수를 제한하여 정부 사이트 rate limit 준수.
    """
    sem = asyncio.Semaphore(_SCRAPE_CONCURRENCY)

    async def _scrape_one(notice, client):
        url = notice.get("DTL_URL", "") if source == "lh" else notice.get("link", "")
        if not url:
            notice["_pdf_urls"] = []
            return
        async with sem:
            try:
                scraper = scrape_lh_detail if source == "lh" else scrape_ih_detail
                detail = await scraper(url, client)
                notice["_pdf_urls"] = detail.get("files", [])
                await asyncio.sleep(_SCRAPE_DELAY)
            except Exception as e:
                logger.warning(f"첨부파일 스크래핑 실패 ({source}): {e}")
                notice["_pdf_urls"] = []

    async with create_scrape_client() as client:
        await asyncio.gather(*[_scrape_one(n, client) for n in notices])

    scraped = sum(1 for n in notices if n.get("_pdf_urls"))
    logger.info(f"{source.upper()} 첨부파일 스크래핑: {scraped}/{len(notices)}건 성공")


async def run_lh_batch():
    """LH 공고 배치: 매입임대 + 임대주택 → Notion DB upsert

    Returns:
        tuple[bool, dict | None]: (성공여부, upsert 결과)
    """
    logger.info("-" * 40)
    logger.info("LH 배치 시작")

    try:
        # 인천 지역(CNP_CD=28) + 전국(CNP_CD 없음) 이중 조회
        regional = [fetch_lh_notices(tp_code=tp, status="", cnp_code="28") for tp in LH_TP_CODES]
        national = [fetch_lh_notices(tp_code=tp, status="", cnp_code="") for tp in LH_TP_CODES]

        all_results = await asyncio.gather(*(regional + national), return_exceptions=True)

        valid = []
        for r in all_results:
            if isinstance(r, Exception):
                logger.warning(f"LH API 조회 일부 실패: {r}")
            else:
                valid.append(r)

        if not valid:
            logger.error("LH API 조회 전체 실패")
            return False, None

        notices = dedup_by_pan_id(*valid)
    except Exception as e:
        logger.error(f"LH API 조회 실패: {e}")
        return False, None

    logger.info(f"LH 조회 결과: {len(notices)}건 (tp_code={','.join(LH_TP_CODES)}, 인천+전국)")

    if not notices:
        logger.info("LH 해당 공고 없음.")
        return True, {"new": 0, "updated": 0, "closed": 0, "failed": 0, "new_notices": [], "failed_notices": []}

    await _scrape_pdf_urls(notices, "lh")

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
    start_date = (today - timedelta(days=IH_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    try:
        notices = await fetch_all_ih_notices(
            startCrtrYmd=start_date,
            endCrtrYmd=end_date,
            sj="입주자",
            seNm="임대",
        )
    except Exception as e:
        logger.error(f"IH API 조회 실패: {e}")
        return False, None

    # 입주자 모집 공고만 필터 (마감안내, 모집결과, 취소 등 노이즈 제외)
    raw_count = len(notices)
    notices = [n for n in notices if _is_recruitment_notice(n)]
    logger.info(f"IH 조회 결과: {raw_count}건 → 모집공고 필터 후 {len(notices)}건")

    await _scrape_pdf_urls(notices, "ih")

    try:
        result = ih_upsert_all(notices)
    except Exception as e:
        logger.error(f"IH Notion 저장 중 오류: {e}")
        return False, None

    logger.info("IH 배치 완료")
    return True, result


async def main():
    validate_env(["OPEN_API_KEY", "NOTION_TOKEN", "NOTION_PARENT_PAGE_ID"])

    logger.info("=" * 50)
    logger.info("인천 임대주택 공고 배치 시작 (LH + IH)")
    start_time = time.time()

    try:
        (lh_ok, lh_result), (ih_ok, ih_result) = await asyncio.gather(
            run_lh_batch(), run_ih_batch()
        )
    except Exception as e:
        logger.error(f"배치 실행 중 예상치 못한 오류: {e}")
        lh_ok, lh_result = False, None
        ih_ok, ih_result = False, None

    # 알림 단계 (LH만 — IH는 마감일 없음)
    lh_notified = 0
    if lh_ok:
        try:
            lh_db_id = os.getenv("NOTION_DATABASE_ID", "").strip()
            if lh_db_id:
                lh_notified = send_deadline_notifications(lh_db_id, "LH")
        except Exception as e:
            logger.error(f"LH 마감 알림 처리 실패: {e}")

    elapsed = time.time() - start_time

    try:
        write_report(lh_result, ih_result, elapsed, lh_ok, ih_ok,
                      lh_notified=lh_notified)
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

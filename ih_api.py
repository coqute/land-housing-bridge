"""공통 IH(인천도시공사) API 로직 — server/lh_mcp.py 와 batch/ 에서 사용합니다."""
import os
import logging
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

API_KEY = os.getenv("OPEN_API_KEY")

NOTICE_URL = "https://apis.data.go.kr/B552831/ih/slls-posts"

logger = logging.getLogger(__name__)


async def fetch_ih_notices(
    numOfRows: int = 30,
    pageNo: int = 1,
    startCrtrYmd: str = "",
    endCrtrYmd: str = "",
    sj: str = "",
    seNm: str = "",
) -> list[dict]:
    """IH 분양임대 공고문을 조회합니다.

    Args:
        numOfRows: 페이지당 건수 (기본값 30, API 최대값)
        pageNo: 페이지 번호 (기본값 1)
        startCrtrYmd: 조회 시작일 (YYYY-MM-DD, 필수)
        endCrtrYmd: 조회 종료일 (YYYY-MM-DD, 필수)
        sj: 공고 제목 필터 키워드
        seNm: 공고 구분 (분양/임대, 빈 문자열이면 전체)

    Returns:
        list of dict: 각 공고의 tyNm, seNm, crtYmd, sj, link 등
    """
    if not API_KEY:
        raise EnvironmentError("OPEN_API_KEY 환경변수가 설정되지 않았습니다.")

    if not startCrtrYmd or not endCrtrYmd:
        raise ValueError("startCrtrYmd와 endCrtrYmd는 필수 파라미터입니다.")

    params: dict = {
        "serviceKey": API_KEY,
        "numOfRows": numOfRows,
        "pageNo": pageNo,
        "startCrtrYmd": startCrtrYmd,
        "endCrtrYmd": endCrtrYmd,
    }

    if sj:
        params["sj"] = sj
    if seNm:
        params["seNm"] = seNm

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(NOTICE_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    # Swagger 명세 응답 구조:
    # { header: {resultCode, resultMsg}, body: {pageNo, numOfRows, totalPageNo, totalCount, posts: [...]} }
    body = data.get("body", data)
    items = body.get("posts", []) or body.get("data", [])
    if not isinstance(items, list):
        return [], 0

    total_pages = body.get("totalPageNo", 1)
    return items, total_pages


async def fetch_all_ih_notices(
    startCrtrYmd: str = "",
    endCrtrYmd: str = "",
    sj: str = "",
    seNm: str = "",
) -> list[dict]:
    """IH 공고를 전체 페이지 순회하여 모두 조회합니다 (배치용).

    API numOfRows 최대값이 30이므로 페이지네이션으로 전체 수집합니다.
    """
    all_items: list[dict] = []
    page = 1

    while True:
        items, total_pages = await fetch_ih_notices(
            numOfRows=30,
            pageNo=page,
            startCrtrYmd=startCrtrYmd,
            endCrtrYmd=endCrtrYmd,
            sj=sj,
            seNm=seNm,
        )
        all_items.extend(items)
        logger.info(f"IH API 페이지 {page}/{total_pages} 조회: {len(items)}건")

        if page >= total_pages or not items:
            break
        page += 1

    return all_items

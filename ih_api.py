"""공통 IH(인천도시공사) API 로직 — server/lh_mcp.py 와 batch/ 에서 사용합니다."""
import asyncio
import logging
from urllib.parse import urlparse, urlencode, parse_qs
import httpx
from config import OPEN_API_KEY as API_KEY
from http_utils import request_with_retry

NOTICE_URL = "https://apis.data.go.kr/B552831/ih/slls-posts"

logger = logging.getLogger(__name__)


def normalize_link(url: str) -> str:
    """IH 공고 link를 정규화하여 중복 비교 정확도를 높입니다."""
    if not url:
        return url
    parsed = urlparse(url)
    scheme = "https"
    path = parsed.path.rstrip("/")
    query = urlencode(sorted(parse_qs(parsed.query, keep_blank_values=True).items(),
                             key=lambda x: x[0]),
                      doseq=True)
    return f"{scheme}://{parsed.netloc}{path}{'?' + query if query else ''}"


async def fetch_ih_notices(
    numOfRows: int = 30,
    pageNo: int = 1,
    startCrtrYmd: str = "",
    endCrtrYmd: str = "",
    sj: str = "",
    seNm: str = "",
    client: httpx.AsyncClient | None = None,
) -> tuple[list[dict], int]:
    """IH 분양임대 공고문을 조회합니다.

    Args:
        numOfRows: 페이지당 건수 (기본값 30, API 최대값)
        pageNo: 페이지 번호 (기본값 1)
        startCrtrYmd: 조회 시작일 (YYYY-MM-DD, 필수)
        endCrtrYmd: 조회 종료일 (YYYY-MM-DD, 필수)
        sj: 공고 제목 필터 키워드
        seNm: 공고 구분 (분양/임대, 빈 문자열이면 전체)
        client: 외부 httpx.AsyncClient (None이면 내부 생성)

    Returns:
        tuple[list[dict], int]: (공고 목록, 전체 페이지 수)
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

    async def _do_request(c: httpx.AsyncClient):
        resp = await request_with_retry(c, "GET", NOTICE_URL, params=params)
        return resp.json()

    if client:
        data = await _do_request(client)
    else:
        async with httpx.AsyncClient(timeout=30.0) as c:
            data = await _do_request(c)

    # Swagger 명세 응답 구조:
    # { header: {resultCode, resultMsg}, body: {pageNo, numOfRows, totalPageNo, totalCount, posts: [...]} }
    body = data.get("body", data)
    items = body.get("posts", []) or body.get("data", [])
    if not isinstance(items, list):
        return [], 0

    for item in items:
        if item.get("link"):
            item["link"] = normalize_link(item["link"])

    total_pages = body.get("totalPageNo", 1)
    return items, total_pages


async def fetch_all_ih_notices(
    startCrtrYmd: str = "",
    endCrtrYmd: str = "",
    sj: str = "",
    seNm: str = "",
    tyNm: str = "",
) -> list[dict]:
    """IH 공고를 전체 페이지 순회하여 모두 조회합니다 (배치용).

    API numOfRows 최대값이 30이므로 페이지네이션으로 전체 수집합니다.

    Args:
        tyNm: 유형명 클라이언트 사이드 필터 (예: '일반임대'). API 미지원 → 조회 후 필터링.
    """
    common_kw = dict(numOfRows=30, startCrtrYmd=startCrtrYmd, endCrtrYmd=endCrtrYmd, sj=sj, seNm=seNm)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 첫 페이지 조회 → total_pages 확인
        items_p1, total_pages = await fetch_ih_notices(pageNo=1, client=client, **common_kw)
        all_items = list(items_p1)
        logger.info(f"IH API 페이지 1/{total_pages} 조회: {len(items_p1)}건")

        # 나머지 페이지 병렬 조회
        if total_pages > 1 and items_p1:
            remaining = await asyncio.gather(*[
                fetch_ih_notices(pageNo=p, client=client, **common_kw)
                for p in range(2, total_pages + 1)
            ], return_exceptions=True)
            for i, r in enumerate(remaining, start=2):
                if isinstance(r, Exception):
                    logger.warning(f"IH API 페이지 {i} 조회 실패: {r}")
                else:
                    items, _ = r
                    all_items.extend(items)
                    logger.info(f"IH API 페이지 {i}/{total_pages} 조회: {len(items)}건")

    if tyNm:
        all_items = [item for item in all_items if item.get("tyNm") == tyNm]

    return all_items

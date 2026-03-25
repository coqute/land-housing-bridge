"""공고 상세 페이지 스크래핑 — 첨부파일 URL 추출.

AI 호출 없음 — 순수 I/O 모듈.
"""

import logging
import re
from urllib.parse import urljoin

import httpx

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_SCRAPE_TIMEOUT = 15.0
_MAX_TEXT_LENGTH = 5000

# 문서 확장자 (텍스트 기반 파일 탐지용)
_DOC_EXTS = (".pdf", ".hwp", ".hwpx", ".xlsx", ".docx", ".zip")

_LH_BASE = "https://apply.lh.or.kr"


def create_scrape_client() -> httpx.AsyncClient:
    """스크래핑용 httpx 클라이언트 생성 (배치에서 공유 클라이언트로 사용)."""
    return httpx.AsyncClient(
        timeout=_SCRAPE_TIMEOUT,
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )


# ---------------------------------------------------------------------------
# 링크 추출 전략 (사이트별)
# ---------------------------------------------------------------------------
def _extract_lh_links(soup, base_url: str) -> list[dict]:
    """LH 청약플러스: javascript:fileDownLoad('파일ID') 패턴 + 확장자 fallback."""
    files = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"fileDownLoad\(['\"](\d+)['\"]\)", href)
        if m:
            file_id = m.group(1)
            name = a.get_text(strip=True) or f"file_{file_id}"
            url = f"{_LH_BASE}/lhapply/lhFile.do?fileid={file_id}"
            files.append({"name": name, "url": url})
            continue

        # 표준 href에 확장자가 있는 경우 (fallback)
        if any(ext in href.lower() for ext in _DOC_EXTS):
            abs_url = urljoin(base_url, href) if not href.startswith("http") else href
            name = a.get_text(strip=True) or href.rsplit("/", 1)[-1]
            files.append({"name": name, "url": abs_url})
    return files


def _extract_ih_links(soup, base_url: str) -> list[dict]:
    """IH 인천도시공사: FileDown 서블릿 패턴 + 텍스트/href 확장자."""
    files = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        name = a.get_text(strip=True)

        # FileDown/fileDown/download 서블릿 패턴
        if re.search(r"(?:file|File)Down|download", href, re.I):
            abs_url = urljoin(base_url, href) if not href.startswith("http") else href
            files.append({"name": name or "file", "url": abs_url})
            continue

        # <a> 텍스트에 문서 확장자가 있는 경우
        if name and any(name.lower().endswith(ext) for ext in _DOC_EXTS):
            abs_url = urljoin(base_url, href) if not href.startswith("http") else href
            files.append({"name": name, "url": abs_url})
            continue

        # href에 확장자가 있는 경우 (fallback)
        if any(ext in href.lower() for ext in _DOC_EXTS):
            abs_url = urljoin(base_url, href) if not href.startswith("http") else href
            files.append({"name": name or href.rsplit("/", 1)[-1], "url": abs_url})
    return files


# ---------------------------------------------------------------------------
# 공통 스크래핑 스켈레톤
# ---------------------------------------------------------------------------
async def _scrape_detail(url: str, extract_links, client: httpx.AsyncClient | None = None) -> dict:
    """상세 페이지에서 첨부파일 URL + 본문 텍스트를 추출하는 공통 로직."""
    result = {"files": [], "html_text": ""}

    if not url:
        return result

    try:
        if BeautifulSoup is None:
            logger.warning("beautifulsoup4 미설치 — HTML 파싱 건너뜀")
            return result

        if client:
            resp = await client.get(url)
            resp.raise_for_status()
        else:
            async with create_scrape_client() as c:
                resp = await c.get(url)
                resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        result["files"] = extract_links(soup, url)

        # 본문 텍스트 추출
        body = soup.find("div", class_=re.compile(r"cont|detail|view|body", re.I))
        text = body.get_text(separator="\n", strip=True) if body else soup.get_text(separator="\n", strip=True)
        result["html_text"] = text[:_MAX_TEXT_LENGTH]

    except (httpx.HTTPError, Exception) as e:
        logger.warning(f"상세 페이지 스크래핑 실패: {url} → {e}")

    return result


# ---------------------------------------------------------------------------
# 공개 API (호출부 변경 없음)
# ---------------------------------------------------------------------------
async def scrape_lh_detail(dtl_url: str, client: httpx.AsyncClient | None = None) -> dict:
    """LH 청약플러스 상세 페이지에서 첨부파일 URL 추출."""
    return await _scrape_detail(dtl_url, _extract_lh_links, client)


async def scrape_ih_detail(link_url: str, client: httpx.AsyncClient | None = None) -> dict:
    """IH 공고 페이지에서 첨부파일 URL 추출."""
    return await _scrape_detail(link_url, _extract_ih_links, client)

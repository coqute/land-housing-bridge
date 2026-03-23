"""문서 다운로드 + PDF 텍스트/이미지 추출 + HTML 파싱.

AI 호출 없음 — 순수 I/O 모듈. Ollama 의존성 없이 독립 동작.
"""

import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_DOCS_DIR = os.path.join(_PROJECT_ROOT, "data", "docs")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_DOWNLOAD_TIMEOUT = 30.0
_SCRAPE_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# 캐시 관리
# ---------------------------------------------------------------------------
def cache_dir(notice_id: str) -> str:
    """공고별 캐시 디렉토리 경로 (없으면 생성)."""
    safe_id = re.sub(r'[<>:"/\\|?*]', "_", notice_id)
    path = os.path.join(_DOCS_DIR, safe_id)
    os.makedirs(path, exist_ok=True)
    return path


def is_cached(notice_id: str, filename: str) -> bool:
    """파일 캐시 존재 여부."""
    path = os.path.join(cache_dir(notice_id), filename)
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _save_meta(notice_id: str, meta: dict) -> None:
    """메타 정보 저장."""
    path = os.path.join(cache_dir(notice_id), "meta.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _load_meta(notice_id: str) -> dict | None:
    """메타 정보 로드."""
    path = os.path.join(cache_dir(notice_id), "meta.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 다운로드
# ---------------------------------------------------------------------------
async def download_file(url: str, dest_path: str) -> str | None:
    """파일 다운로드 (캐시 존재 시 skip).

    Returns:
        저장 경로 또는 None (실패 시)
    """
    if os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
        logger.debug(f"캐시 사용: {dest_path}")
        return dest_path

    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        async with httpx.AsyncClient(
            timeout=_DOWNLOAD_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                f.write(resp.content)
            logger.info(f"다운로드 완료: {dest_path} ({len(resp.content)} bytes)")
            return dest_path
    except (httpx.HTTPError, OSError) as e:
        logger.warning(f"다운로드 실패: {url} → {e}")
        return None


# ---------------------------------------------------------------------------
# LH 상세 페이지 스크래핑
# ---------------------------------------------------------------------------
async def scrape_lh_detail(dtl_url: str) -> dict:
    """LH 청약플러스 상세 페이지에서 PDF/이미지 URL 추출.

    Returns:
        {"pdfs": [url, ...], "images": [url, ...], "html_text": str}
    """
    result = {"pdfs": [], "images": [], "html_text": ""}

    if not dtl_url:
        return result

    try:
        from bs4 import BeautifulSoup

        async with httpx.AsyncClient(
            timeout=_SCRAPE_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        ) as client:
            resp = await client.get(dtl_url)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # PDF 링크 추출 (href에 .pdf/.hwp 포함)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(ext in href.lower() for ext in [".pdf", ".hwp", ".hwpx"]):
                # 상대 URL → 절대 URL
                if href.startswith("/"):
                    from urllib.parse import urljoin
                    href = urljoin(dtl_url, href)
                result["pdfs"].append(href)

        # 이미지 추출 (공고 본문 영역의 이미지)
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if any(ext in src.lower() for ext in [".jpg", ".jpeg", ".png", ".gif"]):
                if src.startswith("/"):
                    from urllib.parse import urljoin
                    src = urljoin(dtl_url, src)
                # 아이콘/로고 제외 (파일명 기반)
                if not any(kw in src.lower() for kw in ["icon", "logo", "btn", "arrow"]):
                    result["images"].append(src)

        # 본문 텍스트 추출
        body = soup.find("div", class_=re.compile(r"cont|detail|view|body", re.I))
        if body:
            result["html_text"] = body.get_text(separator="\n", strip=True)
        else:
            result["html_text"] = soup.get_text(separator="\n", strip=True)[:5000]

    except ImportError:
        logger.warning("beautifulsoup4 미설치 — HTML 파싱 건너뜀")
    except (httpx.HTTPError, Exception) as e:
        logger.warning(f"LH 상세 페이지 스크래핑 실패: {dtl_url} → {e}")

    return result


# ---------------------------------------------------------------------------
# IH 상세 페이지 스크래핑
# ---------------------------------------------------------------------------
async def scrape_ih_detail(link_url: str) -> dict:
    """IH 공고 페이지에서 본문 텍스트 + 첨부 URL 추출.

    Returns:
        {"body_text": str, "attachments": [url, ...]}
    """
    result = {"body_text": "", "attachments": []}

    if not link_url:
        return result

    try:
        from bs4 import BeautifulSoup

        async with httpx.AsyncClient(
            timeout=_SCRAPE_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        ) as client:
            resp = await client.get(link_url)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # 첨부파일 링크
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(ext in href.lower() for ext in [".pdf", ".hwp", ".hwpx", ".xlsx", ".docx"]):
                if href.startswith("/"):
                    from urllib.parse import urljoin
                    href = urljoin(link_url, href)
                result["attachments"].append(href)

        # 본문 텍스트
        body = soup.find("div", class_=re.compile(r"cont|detail|view|body", re.I))
        if body:
            result["body_text"] = body.get_text(separator="\n", strip=True)
        else:
            result["body_text"] = soup.get_text(separator="\n", strip=True)[:5000]

    except ImportError:
        logger.warning("beautifulsoup4 미설치 — HTML 파싱 건너뜀")
    except (httpx.HTTPError, Exception) as e:
        logger.warning(f"IH 상세 페이지 스크래핑 실패: {link_url} → {e}")

    return result


# ---------------------------------------------------------------------------
# PDF 처리
# ---------------------------------------------------------------------------
def extract_pdf_text(pdf_path: str) -> list[dict]:
    """PDF에서 페이지별 텍스트 추출.

    Returns:
        [{"page": 1, "text": "..."}]
    """
    if not os.path.isfile(pdf_path):
        return []

    try:
        import pymupdf

        pages = []
        with pymupdf.open(pdf_path) as doc:
            for i, page in enumerate(doc):
                text = page.get_text("text")
                if text and text.strip():
                    pages.append({"page": i + 1, "text": text.strip()})
        logger.debug(f"PDF 텍스트 추출: {pdf_path} → {len(pages)}페이지")
        return pages
    except ImportError:
        logger.warning("pymupdf 미설치 — PDF 텍스트 추출 불가")
        return []
    except Exception as e:
        logger.warning(f"PDF 텍스트 추출 실패: {pdf_path} → {e}")
        return []


def extract_pdf_images(pdf_path: str, min_size: int = 100) -> list[dict]:
    """PDF에서 이미지 추출 (장식 이미지 제외).

    Args:
        pdf_path: PDF 파일 경로
        min_size: 최소 이미지 크기 (가로/세로 픽셀, 이하 제외)

    Returns:
        [{"page": 1, "index": 0, "bytes": b"...", "width": int, "height": int, "ext": "png"}]
    """
    if not os.path.isfile(pdf_path):
        return []

    try:
        import pymupdf

        images = []
        with pymupdf.open(pdf_path) as doc:
            for page_num, page in enumerate(doc):
                for img_idx, img in enumerate(page.get_images(full=True)):
                    xref = img[0]
                    try:
                        pix = pymupdf.Pixmap(doc, xref)
                        if pix.width < min_size or pix.height < min_size:
                            continue
                        # CMYK → RGB 변환
                        if pix.n > 4:
                            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                        img_bytes = pix.tobytes("png")
                        images.append({
                            "page": page_num + 1,
                            "index": img_idx,
                            "bytes": img_bytes,
                            "width": pix.width,
                            "height": pix.height,
                            "ext": "png",
                        })
                    except Exception:
                        continue

        logger.debug(f"PDF 이미지 추출: {pdf_path} → {len(images)}건 (min_size={min_size})")
        return images
    except ImportError:
        logger.warning("pymupdf 미설치 — PDF 이미지 추출 불가")
        return []
    except Exception as e:
        logger.warning(f"PDF 이미지 추출 실패: {pdf_path} → {e}")
        return []


def pdf_page_to_image(pdf_path: str, page_num: int = 0, dpi: int = 300) -> bytes | None:
    """PDF 페이지를 PNG 이미지로 변환 (OCR 폴백용).

    Args:
        pdf_path: PDF 파일 경로
        page_num: 페이지 번호 (0-indexed)
        dpi: 해상도

    Returns:
        PNG 이미지 바이트 또는 None
    """
    try:
        import pymupdf

        with pymupdf.open(pdf_path) as doc:
            if page_num >= len(doc):
                return None
            page = doc[page_num]
            mat = pymupdf.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            return pix.tobytes("png")
    except ImportError:
        logger.warning("pymupdf 미설치 — PDF→이미지 변환 불가")
        return None
    except Exception as e:
        logger.warning(f"PDF→이미지 변환 실패: {pdf_path} p{page_num} → {e}")
        return None

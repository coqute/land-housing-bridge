"""배치 문서 처리 파이프라인 — 공고 PDF/이미지 다운로드, 텍스트 추출, 임베딩 저장.

main.py에서 Notion upsert 후 호출. 실패해도 core 배치에 영향 없음.
"""

import asyncio
import hashlib
import logging
import os

from doc_processor import (
    cache_dir,
    download_file,
    extract_pdf_images,
    extract_pdf_text,
    pdf_page_to_image,
    scrape_ih_detail,
    scrape_lh_detail,
)
from ih_api import normalize_link
from ollama_client import (
    analyze_image,
    check_ollama,
    embed_texts,
    is_vision_available,
)
from text_chunker import chunk_notice_text, compose_ih_text, compose_lh_text
from vector_store import content_hash, init_db, needs_update, store_chunks, upsert_notice

logger = logging.getLogger(__name__)

# 평면도 분석 프롬프트
_FLOOR_PLAN_PROMPT = (
    "이 주택 평면도를 분석하세요. 다음 정보를 추출하세요:\n"
    "- 주택형 (전용면적)\n"
    "- 방 수, 화장실 수\n"
    "- 구조 특징 (발코니, 드레스룸 등)\n"
    "- 전체적인 평면 구성 설명"
)

# 스캔 PDF OCR 프롬프트
_OCR_PROMPT = "이 문서 이미지의 한국어 텍스트를 모두 정확하게 추출하세요. 표가 있으면 표 형식을 유지하세요."

# 스크래핑 딜레이 (정부 사이트 예의)
_SCRAPE_DELAY = 1.0


async def process_notices(
    lh_notices: list[dict],
    ih_notices: list[dict],
) -> dict:
    """LH + IH 공고의 문서를 처리하고 임베딩을 생성.

    Args:
        lh_notices: LH API 응답 공고 리스트
        ih_notices: IH API 응답 공고 리스트 (필터 후)

    Returns:
        {"processed", "skipped", "failed", "chunks", "images_analyzed", "errors"}
    """
    stats = {
        "processed": 0,
        "skipped": 0,
        "failed": 0,
        "chunks": 0,
        "images_analyzed": 0,
        "errors": [],
    }

    if not await check_ollama():
        logger.info("Ollama 미연결 — 문서 처리 건너뜀")
        return stats

    init_db()
    vision_ok = await is_vision_available()

    # LH 공고 처리
    for notice in lh_notices:
        try:
            result = await _process_lh_notice(notice, vision_ok)
            _merge_stats(stats, result)
            await asyncio.sleep(_SCRAPE_DELAY)
        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append(f"LH {notice.get('PAN_ID', '?')}: {e}")
            logger.warning(f"LH 공고 문서 처리 실패: {e}")

    # IH 공고 처리
    for notice in ih_notices:
        try:
            result = await _process_ih_notice(notice, vision_ok)
            _merge_stats(stats, result)
            await asyncio.sleep(_SCRAPE_DELAY)
        except Exception as e:
            stats["failed"] += 1
            nid = _ih_notice_id(notice)
            stats["errors"].append(f"IH {nid[:20]}: {e}")
            logger.warning(f"IH 공고 문서 처리 실패: {e}")

    logger.info(
        f"문서 처리 완료: 처리 {stats['processed']}, 건너뜀 {stats['skipped']}, "
        f"실패 {stats['failed']}, 청크 {stats['chunks']}, 이미지분석 {stats['images_analyzed']}"
    )
    return stats


# ---------------------------------------------------------------------------
# LH 공고 처리
# ---------------------------------------------------------------------------
async def _process_lh_notice(notice: dict, vision_ok: bool) -> dict:
    """LH 공고 1건 처리."""
    pan_id = notice.get("PAN_ID", "")
    pan_nm = notice.get("PAN_NM", "")
    dtl_url = notice.get("DTL_URL", "")
    supply_details = notice.get("supply_details", [])
    supply_columns = notice.get("supply_columns", {})

    if not pan_id:
        return {"failed": 1, "errors": ["PAN_ID 없음"]}

    # 컨텐츠 해시 = 제목 + 공급정보 요약 + DTL_URL
    raw_content = compose_lh_text(notice) + str(supply_details)
    c_hash = content_hash(raw_content)

    # 변경 감지
    changed = upsert_notice(pan_id, "lh", pan_nm, dtl_url, c_hash)
    if not changed:
        return {"skipped": 1}

    all_chunks = []

    # 1) 메타데이터 청크
    meta_text = compose_lh_text(notice)
    all_chunks.append({
        "text": meta_text,
        "section": "body",
        "source_type": "title",
        "page": None,
    })

    # 2) 공급정보 청크
    if supply_details:
        supply_text = _format_supply_text(supply_columns, supply_details)
        if supply_text:
            all_chunks.append({
                "text": supply_text,
                "section": "units",
                "source_type": "supply",
                "page": None,
            })

    # 3) 상세 페이지 스크래핑 → PDF 다운로드 → 텍스트 추출
    images_analyzed = 0
    if dtl_url:
        detail = await scrape_lh_detail(dtl_url)

        # PDF 처리
        for pdf_idx, pdf_url in enumerate(detail.get("pdfs", [])):
            pdf_path = os.path.join(cache_dir(f"lh_{pan_id}"), f"doc_{pdf_idx}.pdf")
            downloaded = await download_file(pdf_url, pdf_path)
            if not downloaded:
                continue

            pages = extract_pdf_text(downloaded)
            if pages:
                full_text = "\n\n".join(p["text"] for p in pages)
                pdf_chunks = chunk_notice_text(full_text, pan_id, "pdf")
                all_chunks.extend(pdf_chunks)
            else:
                # 텍스트 없는 스캔 PDF → 비전 OCR 폴백
                if vision_ok:
                    ocr_chunks = await _ocr_pdf_pages(downloaded, pan_id)
                    all_chunks.extend(ocr_chunks)

            # 평면도 이미지 추출 + 비전 분석
            if vision_ok:
                pdf_images = extract_pdf_images(downloaded, min_size=200)
                for img_data in pdf_images:
                    analysis = await analyze_image(img_data["bytes"], _FLOOR_PLAN_PROMPT)
                    if analysis:
                        all_chunks.append({
                            "text": analysis,
                            "section": "units",
                            "source_type": "vision",
                            "page": img_data["page"],
                        })
                        images_analyzed += 1

        # HTML 본문 (PDF 없을 때 폴백)
        if not detail.get("pdfs") and detail.get("html_text"):
            html_chunks = chunk_notice_text(detail["html_text"], pan_id, "html")
            all_chunks.extend(html_chunks)

    # 4) 임베딩 생성 + 저장
    return await _embed_and_store(pan_id, all_chunks, images_analyzed)


# ---------------------------------------------------------------------------
# IH 공고 처리
# ---------------------------------------------------------------------------
async def _process_ih_notice(notice: dict, vision_ok: bool) -> dict:
    """IH 공고 1건 처리."""
    link = notice.get("link", "")
    sj = notice.get("sj", "")
    nid = _ih_notice_id(notice)

    if not nid:
        return {"failed": 1, "errors": ["IH link 없음"]}

    # 컨텐츠 해시
    raw_content = compose_ih_text(notice)
    c_hash = content_hash(raw_content)

    changed = upsert_notice(nid, "ih", sj, link, c_hash)
    if not changed:
        return {"skipped": 1}

    all_chunks = []

    # 1) 메타데이터 청크
    meta_text = compose_ih_text(notice)
    all_chunks.append({
        "text": meta_text,
        "section": "body",
        "source_type": "title",
        "page": None,
    })

    # 2) 상세 페이지 스크래핑
    images_analyzed = 0
    if link:
        detail = await scrape_ih_detail(link)

        if detail.get("body_text"):
            body_chunks = chunk_notice_text(detail["body_text"], nid, "html")
            all_chunks.extend(body_chunks)

        # 첨부파일 PDF 처리
        for att_idx, att_url in enumerate(detail.get("attachments", [])):
            if not att_url.lower().endswith(".pdf"):
                continue
            pdf_path = os.path.join(cache_dir(f"ih_{nid[:20]}"), f"att_{att_idx}.pdf")
            downloaded = await download_file(att_url, pdf_path)
            if not downloaded:
                continue

            pages = extract_pdf_text(downloaded)
            if pages:
                full_text = "\n\n".join(p["text"] for p in pages)
                pdf_chunks = chunk_notice_text(full_text, nid, "pdf")
                all_chunks.extend(pdf_chunks)
            elif vision_ok:
                ocr_chunks = await _ocr_pdf_pages(downloaded, nid)
                all_chunks.extend(ocr_chunks)

    # 3) 임베딩 + 저장
    return await _embed_and_store(nid, all_chunks, images_analyzed)


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------
def _ih_notice_id(notice: dict) -> str:
    """IH 공고의 고유 ID 생성 (normalized link의 SHA-256 앞 16자)."""
    link = normalize_link(notice.get("link", ""))
    if not link:
        return ""
    return hashlib.sha256(link.encode("utf-8")).hexdigest()[:16]


def _format_supply_text(columns: dict, details: list[dict]) -> str:
    """공급정보를 임베딩용 텍스트로 포맷."""
    if not details:
        return ""

    lines = ["공급정보:"]
    labels = list(columns.values()) if columns else list(details[0].keys())
    fields = list(columns.keys()) if columns else list(details[0].keys())

    for d in details:
        vals = [str(d.get(f, "")).strip() for f in fields if d.get(f)]
        if vals:
            lines.append(" | ".join(vals))

    return "\n".join(lines)


async def _embed_and_store(notice_id: str, chunks: list[dict], images_analyzed: int) -> dict:
    """청크 리스트를 임베딩하고 벡터 스토어에 저장."""
    if not chunks:
        return {"processed": 1, "chunks": 0, "images_analyzed": images_analyzed}

    texts = [c["text"] for c in chunks]
    embeddings = await embed_texts(texts)

    if embeddings is None:
        # 임베딩 실패해도 텍스트 청크는 저장 (나중에 재임베딩 가능)
        logger.warning(f"임베딩 실패 — 텍스트만 저장: {notice_id}")
        store_chunks(notice_id, chunks, embeddings=None)
        return {"processed": 1, "chunks": len(chunks), "images_analyzed": images_analyzed}

    stored = store_chunks(notice_id, chunks, embeddings)
    return {"processed": 1, "chunks": stored, "images_analyzed": images_analyzed}


async def _ocr_pdf_pages(pdf_path: str, notice_id: str, max_pages: int = 5) -> list[dict]:
    """스캔 PDF의 페이지를 이미지로 변환 후 비전 OCR."""
    chunks = []
    for page_num in range(max_pages):
        img_bytes = pdf_page_to_image(pdf_path, page_num)
        if img_bytes is None:
            break
        text = await analyze_image(img_bytes, _OCR_PROMPT)
        if text and text.strip():
            page_chunks = chunk_notice_text(text, notice_id, "vision", page=page_num + 1)
            chunks.extend(page_chunks)
    return chunks


def _merge_stats(base: dict, result: dict) -> None:
    """결과 통계를 base에 병합."""
    for key in ("processed", "skipped", "failed", "chunks", "images_analyzed"):
        base[key] = base.get(key, 0) + result.get(key, 0)
    if result.get("errors"):
        base.setdefault("errors", []).extend(result["errors"])

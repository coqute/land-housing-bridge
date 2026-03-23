"""한국어 공공주택 공고 전용 텍스트 청킹.

섹션 기반 분할 (1차) → 고정 크기 폴백 (2차).
한국 주택 공고 PDF의 반복 섹션 헤더를 인식하여 의미 단위로 분할한다.
"""

import re

# ---------------------------------------------------------------------------
# 섹션 헤더 매핑 (한국 주택 공고 PDF 표준 구조)
# ---------------------------------------------------------------------------
_SECTION_MAP: list[tuple[str, list[str]]] = [
    ("eligibility", ["입주자격", "자격요건", "신청자격", "입주대상", "신청대상", "대상자"]),
    ("income", ["소득기준", "소득요건", "소득조건", "자산기준", "자산요건"]),
    ("units", ["공급대상", "공급물량", "공급세대", "공급규모", "세대수", "주택형"]),
    ("schedule", ["신청일정", "접수기간", "모집일정", "접수일정", "공급일정", "일정안내"]),
    ("rent", ["임대조건", "임대료", "보증금", "월임대료", "납부조건", "분양가"]),
    ("other", ["기타사항", "유의사항", "참고사항", "안내사항", "문의처", "제출서류"]),
]

# 섹션 헤더 패턴: 숫자/로마자 + 마침표/괄호 + 공백 + 키워드, 또는 키워드만 단독
_all_keywords = [kw for _, keywords in _SECTION_MAP for kw in keywords]
_SECTION_PATTERN = re.compile(
    r"(?:^|\n)\s*"
    r"(?:[\d\uFF10-\uFF19]+[\.\)]\s*|[IVXivx]+[\.\)]\s*|[가-힣][\.\)]\s*|[\u2460-\u2473]\s*)?"
    r"(" + "|".join(re.escape(kw) for kw in _all_keywords) + r")",
    re.MULTILINE,
)

_MAX_CHUNK_CHARS = 800
_OVERLAP_CHARS = 100


# ---------------------------------------------------------------------------
# 섹션 분류
# ---------------------------------------------------------------------------
def _classify_section(text: str) -> str:
    """텍스트 내 키워드로 섹션 분류."""
    for section, keywords in _SECTION_MAP:
        for kw in keywords:
            if kw in text:
                return section
    return "other"


# ---------------------------------------------------------------------------
# 섹션 기반 청킹
# ---------------------------------------------------------------------------
def _chunk_by_sections(text: str) -> list[dict] | None:
    """섹션 헤더를 기준으로 텍스트를 분할. 2개 미만 섹션이면 None (폴백 필요)."""
    matches = list(_SECTION_PATTERN.finditer(text))
    if len(matches) < 2:
        return None

    sections = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_text = text[start:end].strip()
        if not section_text:
            continue

        section_name = _classify_section(match.group(1))
        sections.append({
            "text": section_text,
            "section": section_name,
            "char_offset": start,
        })

    # 각 섹션이 너무 크면 고정 크기로 재분할
    result = []
    for sec in sections:
        if len(sec["text"]) > _MAX_CHUNK_CHARS:
            sub_chunks = _chunk_fixed_size(sec["text"])
            for sc in sub_chunks:
                sc["section"] = sec["section"]
                sc["char_offset"] = sec["char_offset"] + sc["char_offset"]
                result.append(sc)
        else:
            result.append(sec)

    return result if result else None


# ---------------------------------------------------------------------------
# 고정 크기 청킹 (폴백)
# ---------------------------------------------------------------------------
def _chunk_fixed_size(text: str) -> list[dict]:
    """문장 경계를 존중하는 고정 크기 청킹."""
    if len(text) <= _MAX_CHUNK_CHARS:
        return [{"text": text, "section": "body", "char_offset": 0}]

    # 문장 분할 (한국어: 마침표+공백, 줄바꿈)
    sentences = re.split(r"(?<=[.!?。])\s+|\n+", text)

    chunks = []
    current_text = ""
    current_offset = 0
    pos = 0

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            pos += 1
            continue

        if len(current_text) + len(sentence) + 1 > _MAX_CHUNK_CHARS and current_text:
            chunks.append({
                "text": current_text.strip(),
                "section": "body",
                "char_offset": current_offset,
            })
            # overlap: 마지막 문장 일부를 다음 청크에 포함
            overlap_start = max(0, len(current_text) - _OVERLAP_CHARS)
            overlap_text = current_text[overlap_start:]
            current_offset = pos - len(overlap_text)
            current_text = overlap_text + " " + sentence
        else:
            if not current_text:
                current_offset = pos
            current_text = (current_text + " " + sentence).strip()

        pos += len(sentence) + 1

    if current_text.strip():
        chunks.append({
            "text": current_text.strip(),
            "section": "body",
            "char_offset": current_offset,
        })

    return chunks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def chunk_notice_text(
    text: str,
    notice_id: str,
    source_type: str = "pdf",
    page: int | None = None,
) -> list[dict]:
    """공고 텍스트를 청크로 분할.

    1차: 섹션 기반 (한국 주택 공고 구조 인식)
    2차: 고정 크기 폴백 (800자, 100자 overlap)

    Args:
        text: 원본 텍스트
        notice_id: 공고 ID
        source_type: 소스 유형 (pdf/html/supply/vision/title)
        page: PDF 페이지 번호

    Returns:
        [{"text", "section", "source_type", "page", "char_offset", "notice_id"}]
    """
    text = text.strip()
    if not text:
        return []

    # 짧은 텍스트는 단일 청크
    if len(text) <= _MAX_CHUNK_CHARS:
        return [{
            "text": text,
            "section": _classify_section(text),
            "source_type": source_type,
            "page": page,
            "char_offset": 0,
            "notice_id": notice_id,
        }]

    # 섹션 기반 → 실패 시 고정 크기
    chunks = _chunk_by_sections(text)
    if chunks is None:
        chunks = _chunk_fixed_size(text)

    # 메타데이터 부착
    for chunk in chunks:
        chunk["source_type"] = source_type
        chunk["page"] = page
        chunk["notice_id"] = notice_id
        if "section" not in chunk:
            chunk["section"] = "body"

    return chunks


def compose_lh_text(notice: dict) -> str:
    """LH 공고 메타데이터를 임베딩용 텍스트로 조합."""
    parts = []

    ais_tp = notice.get("AIS_TP_CD_NM", "")
    pan_nm = notice.get("PAN_NM", "")
    if ais_tp:
        parts.append(f"[{ais_tp}] {pan_nm}")
    else:
        parts.append(pan_nm)

    pan_ss = notice.get("PAN_SS", "")
    start_dt = notice.get("PAN_NT_ST_DT", "")
    end_dt = notice.get("CLSG_DT", "")
    if pan_ss or start_dt:
        meta = []
        if pan_ss:
            meta.append(f"상태: {pan_ss}")
        if start_dt and end_dt:
            meta.append(f"기간: {start_dt} ~ {end_dt}")
        parts.append(" | ".join(meta))

    # 공급정보 요약 (있으면)
    supply = notice.get("supply_details", [])
    if supply:
        supply_cols = notice.get("supply_columns", {})
        summaries = []
        for item in supply[:5]:  # 최대 5건
            vals = [str(v).strip() for v in item.values() if v is not None and str(v).strip()]
            if vals:
                summaries.append(" ".join(vals[:3]))
        if summaries:
            parts.append("공급: " + " | ".join(summaries))

    return "\n".join(parts)


def compose_ih_text(notice: dict) -> str:
    """IH 공고 메타데이터를 임베딩용 텍스트로 조합."""
    parts = []

    se = notice.get("seNm", "")
    ty = notice.get("tyNm", "")
    sj = notice.get("sj", "")

    header = f"[{se}/{ty}] {sj}" if se and ty else sj
    parts.append(header)

    crt = notice.get("crtYmd", "")
    if crt:
        parts.append(f"등록일: {crt}")

    return "\n".join(parts)

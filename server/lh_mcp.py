"""FastMCP 서버 — lh_api.py / ih_api.py 공통 로직을 사용하여 AI 도구를 노출합니다.

실행: py -m server.lh_mcp  (프로젝트 루트에서)
"""
import asyncio
import logging
from datetime import datetime, timedelta

from fastmcp import FastMCP
from config import validate_env
from lh_api import fetch_lh_notices, fetch_supply_detail
from ih_api import fetch_all_ih_notices

validate_env(["OPEN_API_KEY"])

logger = logging.getLogger(__name__)

mcp = FastMCP("LH_Incheon_Notice_Server")


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------
def _date_range(days: int) -> tuple[str, str]:
    """오늘 기준 N일 전~오늘 날짜 범위를 (start, end) YYYY-MM-DD 문자열로 반환."""
    today = datetime.now()
    return (today - timedelta(days=days)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


async def _gather_lh_notices(days: int, **kwargs) -> list[dict]:
    """활성 + 과거 LH 공고를 PAN_ID 중복 제거하여 병합 반환.

    LH API는 날짜 파라미터가 있으면 활성 공고(공고중/접수중)가 제외되므로,
    활성(lookback_days=0)과 과거(lookback_days=days) 2회 조회 후 병합한다.
    하나라도 성공하면 결과 반환, 모두 실패하면 첫 예외를 raise.
    """
    # status/lookback_days는 내부에서 제어 — kwargs 충돌 방지
    kwargs.pop("status", None)
    kwargs.pop("lookback_days", None)

    tasks = [fetch_lh_notices(status="", lookback_days=0, **kwargs)]
    if days > 0:
        tasks.append(fetch_lh_notices(status="", lookback_days=days, **kwargs))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    seen: dict[str, dict] = {}
    first_error = None
    for r in results:
        if isinstance(r, Exception):
            first_error = first_error or r
            continue
        for n in r:
            pid = n.get("PAN_ID", "")
            if pid and pid not in seen:
                seen[pid] = n

    if not seen and first_error:
        raise first_error

    return list(seen.values())


def _format_lh_notice_header(notice: dict) -> list[str]:
    """LH 공고 1건의 헤더 라인을 생성."""
    pan_ss = notice.get("PAN_SS", "")
    pan_nm = notice.get("PAN_NM", "")
    pan_id = notice.get("PAN_ID", "")
    ais_tp = notice.get("AIS_TP_CD_NM", "")
    pan_dt = notice.get("PAN_DT", "")
    start_dt = notice.get("PAN_NT_ST_DT", "")
    end_dt = notice.get("CLSG_DT", "")
    dtl_url = notice.get("DTL_URL", "")
    spl_tp = notice.get("SPL_INF_TP_CD", "")
    ccr_cd = notice.get("CCR_CNNT_SYS_DS_CD", "")

    date_info = f"기간: {start_dt} ~ {end_dt}"
    if pan_dt:
        date_info = f"공고일: {pan_dt} | {date_info}"

    lines = [
        f"### [{pan_ss}] {pan_nm} (ID: {pan_id})",
        f"  - 유형: {ais_tp} | {date_info}",
    ]
    if dtl_url:
        detail_line = f"  - 상세: {dtl_url}"
        if spl_tp and ccr_cd:
            detail_line += f" | 공급조회: SPL={spl_tp}, CCR={ccr_cd}"
        lines.append(detail_line)
    elif spl_tp and ccr_cd:
        lines.append(f"  - 공급조회: SPL={spl_tp}, CCR={ccr_cd}")

    supply_error = notice.get("supply_error")
    if supply_error:
        lines.append(f"  - 공급정보 조회 실패: {supply_error}")

    return lines


def _format_supply_rows(supply_columns: dict, supply_details: list[dict]) -> list[str]:
    """공급정보 상세를 마크다운 테이블로 변환."""
    if not supply_details:
        return []

    lines = [f"  공급정보 ({len(supply_details)}건)"]

    if supply_columns:
        fields = list(supply_columns.keys())
        labels = list(supply_columns.values())
    else:
        first = supply_details[0]
        fields = [k for k, v in first.items() if v is not None and str(v).strip()]
        labels = fields[:]

    lines.append("  | " + " | ".join(labels) + " |")
    lines.append("  |" + "|".join(["---"] * len(labels)) + "|")

    for d in supply_details:
        vals = [str(d.get(f, "")).strip() for f in fields]
        lines.append("  | " + " | ".join(vals) + " |")

    return lines


@mcp.tool()
async def get_incheon_lh_notices(
    limit: int = 100,
    page: int = 1,
    tp_code: str = "13",
    cnp_code: str = "28",
    status: str = "공고중",
    keyword: str = "",
    lookback_days: int = 0,
) -> str:
    """
    인천 지역 LH 임대 공고를 조회하고 공급 정보를 함께 반환합니다.

    Args:
        limit: 조회할 공고 수 (기본값 100)
        page: 페이지 번호 (기본값 1)
        tp_code: 공고유형코드 (기본값 13 매입/전세임대, 06 임대주택)
        cnp_code: 지역코드 (기본값 28 인천)
        status: 공고상태 (기본값 공고중, 전체=빈문자열, 접수중도 별도 상태로 존재)
        keyword: 공고명 필터 키워드 (예: 신혼, 청년)
        lookback_days: 0이면 현재 활성 공고 포함 조회(기본값).
                       양수이면 해당 일수만큼 과거 마감 공고 조회 (활성 공고 제외됨).
    """
    try:
        notices = await fetch_lh_notices(
            limit=limit,
            page=page,
            tp_code=tp_code,
            cnp_code=cnp_code,
            status=status,
            lookback_days=lookback_days,
            keyword=keyword,
        )
    except Exception as e:
        return f"오류: {e}"

    if not notices:
        if keyword:
            return f"'{keyword}' 키워드에 해당하는 공고를 찾을 수 없습니다."
        return "인천 지역의 최신 공고를 찾을 수 없습니다."

    results = [f"## LH 인천 공고 ({len(notices)}건)\n"]
    for notice in notices:
        supply_columns = notice.get("supply_columns", {})
        supply_details = notice.get("supply_details", [])

        results.extend(_format_lh_notice_header(notice))

        if supply_details:
            results.extend(_format_supply_rows(supply_columns, supply_details))

        results.append("")

    return "\n".join(results)


@mcp.tool()
async def get_ih_notices(
    start_date: str = "",
    end_date: str = "",
    keyword: str = "",
    category: str = "",
) -> str:
    """
    인천도시공사(IH) 분양/임대 공고문을 조회합니다. 전체 페이지를 자동 순회합니다.

    Args:
        start_date: 조회 시작일 (YYYY-MM-DD, 기본값 1년 전)
        end_date: 조회 종료일 (YYYY-MM-DD, 기본값 오늘)
        keyword: 공고 제목 필터 키워드
        category: 공고 구분 (분양/임대, 빈문자열이면 전체)
    """
    if not start_date or not end_date:
        default_start, default_end = _date_range(365)
        start_date = start_date or default_start
        end_date = end_date or default_end

    try:
        notices = await fetch_all_ih_notices(
            startCrtrYmd=start_date,
            endCrtrYmd=end_date,
            sj=keyword,
            seNm=category,
        )
    except Exception as e:
        return f"오류: {e}"

    if not notices:
        msg = "IH 공고를 찾을 수 없습니다."
        if keyword:
            msg = f"'{keyword}' 키워드에 해당하는 IH 공고를 찾을 수 없습니다."
        return msg

    results = [f"## IH 공고 ({len(notices)}건)\n"]
    for notice in notices:
        se = notice.get("seNm", "")
        title = notice.get("sj", "")
        ty = notice.get("tyNm", "")
        date = notice.get("crtYmd", "")
        link = notice.get("link", "")

        results.append(f"### [{se}] {title}")
        results.append(f"  - 유형: {ty} | 날짜: {date} | 링크: {link}")
        results.append("")

    return "\n".join(results)


@mcp.tool()
async def get_notice_summary(
    days: int = 30,
    tp_code: str = "13",
) -> str:
    """
    최근 N일간 LH + IH 공고 현황 요약을 반환합니다.

    Args:
        days: 조회 기간 (기본값 30일)
        tp_code: LH 공고유형코드 (기본값 13 매입/전세임대)
    """
    start_date, end_date = _date_range(days)

    lh_task = _gather_lh_notices(days, tp_code=tp_code)
    ih_task = fetch_all_ih_notices(startCrtrYmd=start_date, endCrtrYmd=end_date)

    results = await asyncio.gather(lh_task, ih_task, return_exceptions=True)
    lines = [f"## 최근 {days}일 공고 현황\n"]

    # LH
    if isinstance(results[0], Exception):
        lines.append(f"### LH 공고\n- 조회 실패: {results[0]}\n")
    else:
        lh_notices = results[0]
        status_count: dict[str, int] = {}
        for n in lh_notices:
            s = n.get("PAN_SS", "기타")
            status_count[s] = status_count.get(s, 0) + 1
        lines.append(f"### LH 공고 ({len(lh_notices)}건)")
        for s, c in status_count.items():
            lines.append(f"- {s}: {c}건")
        lines.append("")

    # IH
    if isinstance(results[1], Exception):
        lines.append(f"### IH 공고\n- 조회 실패: {results[1]}\n")
    else:
        ih_notices = results[1]
        se_count: dict[str, int] = {}
        ty_count: dict[str, int] = {}
        for n in ih_notices:
            se = n.get("seNm", "기타")
            ty = n.get("tyNm", "기타")
            se_count[se] = se_count.get(se, 0) + 1
            ty_count[ty] = ty_count.get(ty, 0) + 1
        lines.append(f"### IH 공고 ({len(ih_notices)}건)")
        lines.append("**구분별:**")
        for s, c in se_count.items():
            lines.append(f"- {s}: {c}건")
        lines.append("**유형별:**")
        for t, c in ty_count.items():
            lines.append(f"- {t}: {c}건")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def search_all_notices(
    keyword: str,
    days: int = 365,
    category: str = "",
    semantic: bool = False,
) -> str:
    """
    LH + IH 통합 키워드 검색을 수행합니다.

    Args:
        keyword: 검색 키워드 (필수)
        days: 조회 기간 (기본값 365일)
        category: IH 공고 구분 필터 (분양/임대, 빈문자열이면 전체)
        semantic: True이면 의미 기반 검색 결과를 병합합니다 (Ollama 필요)
    """
    if not keyword or not keyword.strip():
        return "오류: 검색 키워드를 입력해주세요."

    keyword = keyword.strip()
    start_date, end_date = _date_range(days)

    lh_task = _gather_lh_notices(days, keyword=keyword)
    ih_task = fetch_all_ih_notices(
        startCrtrYmd=start_date, endCrtrYmd=end_date, sj=keyword, seNm=category,
    )

    results = await asyncio.gather(lh_task, ih_task, return_exceptions=True)
    lines = [f"## '{keyword}' 통합 검색 결과\n"]

    # LH
    if isinstance(results[0], Exception):
        lines.append(f"### LH\n- 조회 실패: {results[0]}\n")
    else:
        lh_notices = results[0]
        lines.append(f"### LH ({len(lh_notices)}건)")
        for n in lh_notices:
            header = _format_lh_notice_header(n)
            # search 결과는 bullet 형식 — heading을 bullet로 변환
            lines.append(header[0].replace("### ", "- "))
            lines.extend(header[1:])
        lines.append("")

    # IH
    if isinstance(results[1], Exception):
        lines.append(f"### IH\n- 조회 실패: {results[1]}\n")
    else:
        ih_notices = results[1]
        lines.append(f"### IH ({len(ih_notices)}건)")
        for n in ih_notices:
            se = n.get("seNm", "")
            title = n.get("sj", "")
            ty = n.get("tyNm", "")
            date = n.get("crtYmd", "")
            link = n.get("link", "")
            lines.append(f"- [{se}] {title}")
            lines.append(f"  - 유형: {ty} | 날짜: {date} | 링크: {link}")
        lines.append("")

    # 시맨틱 검색 병합 (옵션)
    if semantic:
        try:
            from ollama_client import check_ollama, embed_texts
            from vector_store import search as vs_search

            if await check_ollama():
                emb = await embed_texts([keyword])
                if emb:
                    sem_results = vs_search(emb[0], top_k=10, min_score=0.3)
                    if sem_results:
                        lines.append(f"\n### 의미 검색 추가 결과 ({len(sem_results)}건)")
                        seen_ids = set()
                        for r in sem_results:
                            nid = r["notice_id"]
                            if nid in seen_ids:
                                continue
                            seen_ids.add(nid)
                            src = r["source"].upper()
                            lines.append(f"- [{src}] [{r['section']}] {r['title']} (유사도: {r['score']:.2f})")
                            text_preview = r["text"][:200].replace("\n", " ")
                            lines.append(f"  - {text_preview}")
                        lines.append("")
            else:
                lines.append("\n*의미 검색: Ollama 미연결*")
        except Exception as e:
            lines.append(f"\n*의미 검색 오류: {e}*")

    return "\n".join(lines)


@mcp.tool()
async def get_upcoming_deadlines(
    days: int = 7,
    tp_code: str = "13",
) -> str:
    """
    마감 임박 LH 공고를 D-day 순으로 반환합니다. IH는 마감일 정보가 없어 LH만 대상입니다.

    Args:
        days: 마감까지 남은 일수 (기본값 7일 이내)
        tp_code: 공고유형코드 (기본값 13 매입/전세임대, 06 임대주택)
    """
    try:
        notices = await _gather_lh_notices(0, tp_code=tp_code)
    except Exception as e:
        return f"오류: {e}"

    today = datetime.now()
    upcoming = []
    for n in notices:
        clsg = n.get("CLSG_DT", "")
        if not clsg:
            continue
        try:
            deadline = datetime.strptime(clsg.replace("-", "."), "%Y.%m.%d")
        except ValueError:
            continue
        d_day = (deadline - today).days
        if 0 <= d_day <= days:
            n["_d_day"] = d_day
            upcoming.append(n)

    if not upcoming:
        return f"마감 {days}일 이내 LH 공고가 없습니다."

    upcoming.sort(key=lambda x: x["_d_day"])

    lines = [f"## 마감 임박 LH 공고 ({len(upcoming)}건, D-{days}일 이내)\n"]
    for n in upcoming:
        d = n["_d_day"]
        lines.append(f"**D-{d}일**")
        lines.extend(_format_lh_notice_header(n))
        lines.append("")

    lines.append("*IH 공고는 마감일 정보가 없어 포함되지 않습니다.*")
    return "\n".join(lines)


@mcp.tool()
async def get_supply_detail(
    pan_id: str,
    spl_inf_tp_cd: str,
    ccr_cnnt_sys_ds_cd: str,
    tp_code: str = "13",
) -> str:
    """
    특정 LH 공고의 공급정보 상세를 조회합니다.

    get_incheon_lh_notices 또는 search_all_notices 결과의 SPL_INF_TP_CD, CCR_CNNT_SYS_DS_CD 값을 사용하세요.

    Args:
        pan_id: 공고 ID
        spl_inf_tp_cd: 공급정보유형코드 (공고 목록의 SPL_INF_TP_CD)
        ccr_cnnt_sys_ds_cd: 시스템구분코드 (공고 목록의 CCR_CNNT_SYS_DS_CD)
        tp_code: 공고유형코드 (기본값 13)
    """
    try:
        result = await fetch_supply_detail(
            pan_id=pan_id,
            spl_inf_tp_cd=spl_inf_tp_cd,
            ccr_cnnt_sys_ds_cd=ccr_cnnt_sys_ds_cd,
            tp_code=tp_code,
        )
    except Exception as e:
        return f"오류: {e}"

    supply_columns = result.get("supply_columns", {})
    supply_details = result.get("supply_details", [])

    if not supply_details:
        return f"공고 ID {pan_id}의 공급정보가 없습니다."

    lines = [f"## 공급정보 상세 (PAN_ID: {pan_id}, {len(supply_details)}건)\n"]
    lines.extend(_format_supply_rows(supply_columns, supply_details))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AI 도구 (Ollama 의존 — 미연결 시 안내 메시지 반환)
# ---------------------------------------------------------------------------

@mcp.tool()
async def semantic_search(
    query: str,
    top_k: int = 10,
    section: str = "",
    source: str = "",
) -> str:
    """
    공고 전체 내용(PDF, 자격요건, 공급정보)에서 자연어 의미 기반 검색합니다.
    키워드 매칭이 아닌 의미적 유사도로 검색하므로 자연어로 검색할 수 있습니다.
    Ollama + qwen3-embedding이 실행 중이어야 합니다.

    Args:
        query: 자연어 검색 쿼리 (필수, 예: "신혼부부 소득기준", "전용 59제곱미터 매입임대")
        top_k: 반환할 결과 수 (기본값 10)
        section: 섹션 필터 (eligibility/income/units/schedule/rent/other, 빈문자열이면 전체)
        source: 소스 필터 (lh/ih, 빈문자열이면 전체)
    """
    if not query or not query.strip():
        return "오류: 검색 쿼리를 입력해주세요."

    try:
        from ollama_client import check_ollama, embed_texts
        from vector_store import search as vs_search, get_stats
    except ImportError as e:
        return f"오류: 필요 모듈 없음 — {e}"

    if not await check_ollama():
        return (
            "Ollama 미연결 — 의미 검색을 사용할 수 없습니다.\n"
            f"대안: search_all_notices(keyword='{query}')로 키워드 검색을 사용하세요."
        )

    stats = get_stats()
    if stats["embedded_chunks"] == 0:
        return "벡터 DB가 비어 있습니다. 배치(py -m batch.main)를 먼저 실행하세요."

    emb = await embed_texts([query.strip()])
    if not emb:
        return "임베딩 생성 실패"

    results = vs_search(
        emb[0], top_k=top_k,
        section=section or None,
        source=source or None,
        min_score=0.2,
    )

    if not results:
        return f"'{query}'와 유사한 공고를 찾을 수 없습니다."

    # 결과를 notice_id별 그룹핑
    grouped: dict[str, list[dict]] = {}
    for r in results:
        grouped.setdefault(r["notice_id"], []).append(r)

    lines = [f"## 의미 검색 결과 ({len(grouped)}건 공고, {len(results)}건 매칭) — \"{query}\"\n"]

    for nid, chunks in grouped.items():
        best = chunks[0]
        src = best["source"].upper()
        lines.append(f"### [{src}] {best['title']} (유사도: {best['score']:.2f})")
        if best.get("url"):
            lines.append(f"  - 링크: {best['url']}")
        for c in chunks[:3]:
            section_label = c["section"]
            src_type = c["source_type"]
            text_preview = c["text"][:300].replace("\n", " ")
            lines.append(f"  - [{section_label}/{src_type}] {text_preview}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def analyze_notice(
    notice_id: str = "",
    url: str = "",
    source: str = "lh",
) -> str:
    """
    특정 공고를 심층 분석합니다. PDF에서 자격요건, 소득기준, 공급세대 정보를 추출합니다.
    이미 분석된 공고는 즉시 반환, 미분석 공고는 실시간 처리합니다.

    Args:
        notice_id: LH 공고 PAN_ID (source=lh일 때)
        url: IH 공고 URL (source=ih일 때, notice_id 대신 사용 가능)
        source: 공고 소스 (lh 또는 ih, 기본값 lh)
    """
    if not notice_id and not url:
        return "오류: notice_id 또는 url을 입력해주세요."

    try:
        from vector_store import get_notice_chunks, get_notice_info
    except ImportError as e:
        return f"오류: 필요 모듈 없음 — {e}"

    # IH URL → notice_id 변환
    if url and not notice_id:
        import hashlib
        from ih_api import normalize_link
        notice_id = hashlib.sha256(normalize_link(url).encode()).hexdigest()[:16]

    # 기존 청크 확인
    chunks = get_notice_chunks(notice_id)

    if not chunks:
        # on-demand 처리 시도
        try:
            from ollama_client import check_ollama
            if not await check_ollama():
                return "Ollama 미연결 — 공고 분석을 위해 Ollama가 필요합니다."

            return await _on_demand_analyze(notice_id, source, url)
        except Exception as e:
            return f"실시간 분석 실패: {e}"

    # 섹션별 정리
    notice_info = get_notice_info(notice_id)
    title = notice_info["title"] if notice_info else notice_id
    src = (notice_info["source"] if notice_info else source).upper()

    lines = [f"## 공고 심층 분석: [{src}] {title}\n"]

    section_labels = {
        "eligibility": "자격요건",
        "income": "소득기준",
        "units": "공급세대",
        "schedule": "일정",
        "rent": "임대조건",
        "body": "본문",
        "other": "기타",
    }

    sections_found: dict[str, list[dict]] = {}
    for c in chunks:
        sections_found.setdefault(c["section"], []).append(c)

    for section_key, label in section_labels.items():
        if section_key in sections_found:
            lines.append(f"### {label}")
            for c in sections_found[section_key]:
                src_type = c["source_type"]
                page_info = f" (p{c['page']})" if c.get("page") else ""
                lines.append(f"*[{src_type}{page_info}]*")
                lines.append(c["text"][:800])
                lines.append("")

    source_types = set(c["source_type"] for c in chunks)
    lines.append(f"*분석 소스: {', '.join(source_types)} — {len(chunks)}개 청크*")

    return "\n".join(lines)


async def _on_demand_analyze(notice_id: str, source: str, url: str) -> str:
    """미분석 공고를 실시간으로 처리."""
    from batch.doc_pipeline import _process_lh_notice, _process_ih_notice
    from ollama_client import is_vision_available
    from vector_store import get_notice_chunks

    vision_ok = await is_vision_available()

    if source == "lh":
        # LH API에서 공고 다시 조회
        try:
            notices = await fetch_lh_notices(status="", lookback_days=365)
            target = next((n for n in notices if n.get("PAN_ID") == notice_id), None)
            if not target:
                return f"LH 공고 ID {notice_id}를 찾을 수 없습니다."
            await _process_lh_notice(target, vision_ok)
        except Exception as e:
            return f"LH 공고 처리 실패: {e}"
    else:
        if url:
            notice = {"link": url, "sj": "", "seNm": "", "tyNm": "", "crtYmd": ""}
            await _process_ih_notice(notice, vision_ok)
        else:
            return "IH 공고 분석에는 url이 필요합니다."

    chunks = get_notice_chunks(notice_id)
    if chunks:
        return await analyze_notice(notice_id=notice_id, source=source)
    return "공고 분석 데이터를 생성할 수 없습니다."


@mcp.tool()
async def match_eligibility(
    conditions: str,
    top_k: int = 5,
) -> str:
    """
    사용자 조건에 맞는 공고를 찾습니다. 자격요건과 소득기준 섹션에서 의미 매칭합니다.

    Args:
        conditions: 사용자 조건 (자연어, 예: "신혼부부, 연소득 5천만원, 인천 거주 3년")
        top_k: 반환할 공고 수 (기본값 5)
    """
    if not conditions or not conditions.strip():
        return "오류: 조건을 입력해주세요."

    try:
        from ollama_client import check_ollama, embed_texts
        from vector_store import search as vs_search
    except ImportError as e:
        return f"오류: 필요 모듈 없음 — {e}"

    if not await check_ollama():
        return "Ollama 미연결 — 자격 매칭을 사용할 수 없습니다."

    emb = await embed_texts([conditions.strip()])
    if not emb:
        return "임베딩 생성 실패"

    # 자격요건 + 소득기준 섹션에서 검색
    eligibility_results = vs_search(emb[0], top_k=top_k * 5, section="eligibility", min_score=0.2)
    income_results = vs_search(emb[0], top_k=top_k * 5, section="income", min_score=0.2)

    # notice_id별 그룹핑 + 평균 점수
    groups: dict[str, dict] = {}
    for r in eligibility_results + income_results:
        nid = r["notice_id"]
        if nid not in groups:
            groups[nid] = {
                "notice_id": nid,
                "title": r["title"],
                "source": r["source"],
                "url": r["url"],
                "scores": [],
                "chunks": [],
            }
        groups[nid]["scores"].append(r["score"])
        if len(groups[nid]["chunks"]) < 3:
            groups[nid]["chunks"].append(r)

    if not groups:
        return f"'{conditions}'에 매칭되는 공고를 찾을 수 없습니다."

    # 평균 점수 정렬
    ranked = sorted(groups.values(), key=lambda g: sum(g["scores"]) / len(g["scores"]), reverse=True)[:top_k]

    lines = [f"## 자격 매칭 결과 ({len(ranked)}건) — \"{conditions}\"\n"]

    for g in ranked:
        avg = sum(g["scores"]) / len(g["scores"])
        src = g["source"].upper()
        lines.append(f"### [{src}] {g['title']} (적합도: {avg:.2f})")
        if g.get("url"):
            lines.append(f"  - 링크: {g['url']}")
        for c in g["chunks"]:
            text_preview = c["text"][:300].replace("\n", " ")
            lines.append(f"  - [{c['section']}] {text_preview}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def find_similar_notices(
    notice_id: str,
    source: str = "lh",
    top_k: int = 5,
) -> str:
    """
    특정 공고와 유사한 다른 공고를 찾습니다.

    Args:
        notice_id: 기준 공고 ID (LH: PAN_ID, IH: 공고 링크 해시)
        source: 기준 공고 소스 (lh/ih, 기본값 lh)
        top_k: 반환할 유사 공고 수 (기본값 5)
    """
    try:
        from vector_store import get_notice_embedding, search_by_notice, get_notice_info
    except ImportError as e:
        return f"오류: 필요 모듈 없음 — {e}"

    # 기준 공고 임베딩 가져오기
    avg_emb = get_notice_embedding(notice_id)

    if avg_emb is None:
        # on-demand 임베딩 시도
        try:
            from ollama_client import check_ollama, embed_texts
            from text_chunker import compose_lh_text, compose_ih_text

            if not await check_ollama():
                return "Ollama 미연결 — 공고 임베딩이 필요합니다."

            # API에서 공고 조회 → 텍스트 조합 → 임베딩
            if source == "lh":
                notices = await fetch_lh_notices(status="", lookback_days=365)
                target = next((n for n in notices if n.get("PAN_ID") == notice_id), None)
                if not target:
                    return f"공고 ID {notice_id}를 찾을 수 없습니다."
                text = compose_lh_text(target)
            else:
                return "IH 유사 공고 검색에는 사전 임베딩이 필요합니다 (배치 실행 후 사용)."

            emb = await embed_texts([text])
            if not emb:
                return "임베딩 생성 실패"
            avg_emb = emb[0]
        except Exception as e:
            return f"임베딩 생성 실패: {e}"

    # 유사 공고 검색
    results = search_by_notice(avg_emb, top_k=top_k, exclude_notice_ids={notice_id})

    if not results:
        return "유사한 공고를 찾을 수 없습니다."

    # 기준 공고 정보
    base_info = get_notice_info(notice_id)
    base_title = base_info["title"] if base_info else notice_id

    lines = [f"## '{base_title}' 유사 공고 ({len(results)}건)\n"]

    for r in results:
        src = r["source"].upper()
        lines.append(f"### [{src}] {r['title']} (유사도: {r['avg_score']:.2f})")
        if r.get("url"):
            lines.append(f"  - 링크: {r['url']}")
        for c in r.get("top_chunks", [])[:2]:
            text_preview = c["text"][:200].replace("\n", " ")
            lines.append(f"  - [{c['section']}] {text_preview}")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()

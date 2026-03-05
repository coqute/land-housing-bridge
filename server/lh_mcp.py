"""FastMCP 서버 — lh_api.py / ih_api.py 공통 로직을 사용하여 AI 도구를 노출합니다.

실행: py -m server.lh_mcp  (프로젝트 루트에서)
"""
import asyncio
from datetime import datetime, timedelta

from fastmcp import FastMCP
from lh_api import fetch_lh_notices, fetch_supply_detail
from ih_api import fetch_ih_notices, fetch_all_ih_notices

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


def _format_supply_rows(supply_columns: dict, supply_details: list[dict]) -> list[str]:
    """공급정보 상세를 마크다운 bullet 행 리스트로 변환."""
    rows = []
    for d in supply_details:
        if supply_columns:
            row = " | ".join(
                f"{label}: {d.get(field, '')}"
                for field, label in supply_columns.items()
            )
        else:
            row = " | ".join(
                f"{k}: {v}" for k, v in d.items()
                if v is not None and str(v).strip()
            )
        rows.append(f"  - {row}")
    return rows


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

    results = []
    for notice in notices:
        pan_ss = notice.get("PAN_SS", "")
        pan_name = notice.get("PAN_NM", "")
        pan_id = notice.get("PAN_ID", "")
        supply_columns = notice.get("supply_columns", {})
        supply_details = notice.get("supply_details", [])

        results.append(f"### [{pan_ss}] {pan_name} (ID: {pan_id})")

        if supply_details:
            results.extend(_format_supply_rows(supply_columns, supply_details))
        else:
            results.append("  - 공급 상세 정보 없음")

        results.append("")

    return "\n".join(results)


@mcp.tool()
async def get_ih_notices(
    numOfRows: int = 30,
    pageNo: int = 1,
    start_date: str = "",
    end_date: str = "",
    keyword: str = "",
    category: str = "",
) -> str:
    """
    인천도시공사(IH) 분양/임대 공고문을 조회합니다.

    Args:
        numOfRows: 페이지당 건수 (기본값 100)
        pageNo: 페이지 번호 (기본값 1)
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
        notices, _ = await fetch_ih_notices(
            numOfRows=numOfRows,
            pageNo=pageNo,
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

    results = []
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
) -> str:
    """
    LH + IH 통합 키워드 검색을 수행합니다.

    Args:
        keyword: 검색 키워드 (필수)
        days: 조회 기간 (기본값 365일)
        category: IH 공고 구분 필터 (분양/임대, 빈문자열이면 전체)
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
            pan_ss = n.get("PAN_SS", "")
            pan_nm = n.get("PAN_NM", "")
            pan_id = n.get("PAN_ID", "")
            spl_tp = n.get("SPL_INF_TP_CD", "")
            ccr_cd = n.get("CCR_CNNT_SYS_DS_CD", "")
            lines.append(f"- [{pan_ss}] {pan_nm} (ID: {pan_id}, SPL: {spl_tp}, CCR: {ccr_cd})")
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
            lines.append(f"- [{se}] {title} (유형: {ty}, 날짜: {date}, 링크: {link})")
        lines.append("")

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

    lines = [f"## 공급정보 상세 (PAN_ID: {pan_id})\n"]
    lines.extend(_format_supply_rows(supply_columns, supply_details))

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()

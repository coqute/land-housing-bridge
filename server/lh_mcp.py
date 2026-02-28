"""FastMCP 서버 — lh_api.py / ih_api.py 공통 로직을 사용하여 AI 도구를 노출합니다.

실행: py -m server.lh_mcp  (프로젝트 루트에서)
"""
from datetime import datetime, timedelta

from fastmcp import FastMCP
from lh_api import fetch_lh_notices
from ih_api import fetch_ih_notices

mcp = FastMCP("LH_Incheon_Notice_Server")


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
    except EnvironmentError as e:
        return f"오류: {e}"
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
            if supply_columns:
                for d in supply_details:
                    row = " | ".join(
                        f"{label}: {d.get(field, '')}"
                        for field, label in supply_columns.items()
                    )
                    results.append(f"  - {row}")
            else:
                for d in supply_details:
                    row = " | ".join(
                        f"{k}: {v}" for k, v in d.items()
                        if v is not None and str(v).strip()
                    )
                    results.append(f"  - {row}")
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
    today = datetime.now()
    if not end_date:
        end_date = today.strftime("%Y-%m-%d")
    if not start_date:
        start_date = (today - timedelta(days=365)).strftime("%Y-%m-%d")

    try:
        notices, _ = await fetch_ih_notices(
            numOfRows=numOfRows,
            pageNo=pageNo,
            startCrtrYmd=start_date,
            endCrtrYmd=end_date,
            sj=keyword,
            seNm=category,
        )
    except (EnvironmentError, ValueError) as e:
        return f"오류: {e}"
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


if __name__ == "__main__":
    mcp.run()

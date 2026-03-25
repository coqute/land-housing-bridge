"""공통 LH API 로직 — server/lh_mcp.py 와 batch/ 양쪽에서 공유합니다."""
import asyncio
import logging
import httpx
from datetime import datetime, timedelta
from config import OPEN_API_KEY as API_KEY
from http_utils import request_with_retry

NOTICE_URL = "http://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"
SUPPLY_URL = "http://apis.data.go.kr/B552555/lhLeaseNoticeSplInfo1/getLeaseNoticeSplInfo1"

# 공급정보 API 동시 요청 수 제한 (429 Too Many Requests 방지)
_SUPPLY_SEMAPHORE = asyncio.Semaphore(5)

logger = logging.getLogger(__name__)


def dedup_by_pan_id(*notice_lists: list[dict]) -> list[dict]:
    """PAN_ID 기준 중복 제거 병합 (먼저 나온 항목 우선)."""
    seen: dict[str, dict] = {}
    for notices in notice_lists:
        for n in notices:
            pid = n.get("PAN_ID", "")
            if pid and pid not in seen:
                seen[pid] = n
    return list(seen.values())



def _extract_ds_list(response_data, key: str = 'dsList') -> list:
    """API 응답(list 또는 dict)에서 지정 키의 배열을 안전하게 추출"""
    if isinstance(response_data, list):
        for item in response_data:
            if isinstance(item, dict) and key in item:
                return item[key] or []
        return []
    if isinstance(response_data, dict):
        return response_data.get(key, [])
    return []


def _extract_supply_list(response_data) -> list:
    """공급정보 API 응답에서 dsList01 배열을 추출 (없으면 dsList 시도)"""
    result = _extract_ds_list(response_data, 'dsList01')
    if not result:
        result = _extract_ds_list(response_data, 'dsList')
    return result


async def _fetch_supply(client: httpx.AsyncClient, item: dict, tp_code: str) -> tuple[dict, list, str | None]:
    """공고 1건의 공급정보를 조회.

    SPL_INF_TP_CD 또는 CCR_CNNT_SYS_DS_CD가 없으면 API 호출 없이 빈값 반환.
    Semaphore로 동시 요청 수를 제한하여 429 Too Many Requests 방지.

    Returns:
        tuple[dict, list, str | None]: (컬럼 정보, 상세 목록, 에러 메시지 또는 None)
    """
    pan_id = item.get('PAN_ID', '')
    spl_tp = item.get('SPL_INF_TP_CD', '')
    ccr_cd = item.get('CCR_CNNT_SYS_DS_CD', '')

    if not spl_tp or not ccr_cd:
        return {}, [], None

    try:
        async with _SUPPLY_SEMAPHORE:
            supply_resp = await request_with_retry(client, "GET", SUPPLY_URL, params={
                "ServiceKey": API_KEY,
                "SPL_INF_TP_CD": spl_tp,
                "CCR_CNNT_SYS_DS_CD": ccr_cd,
                "PAN_ID": pan_id,
                "UPP_AIS_TP_CD": tp_code,
            })
        supply_data = supply_resp.json()
        cols = _extract_ds_list(supply_data, 'dsList01Nm')
        supply_columns = cols[0] if cols else {}
        supply_details = _extract_supply_list(supply_data)
        return supply_columns, supply_details, None
    except Exception as e:
        logger.warning(f"공급정보 조회 실패 (PAN_ID={pan_id}): {e}")
        return {}, [], str(e)


async def fetch_lh_notices(
    limit: int = 100,
    page: int = 1,
    tp_code: str = '13',
    cnp_code: str = '28',
    status: str = '공고중',
    lookback_days: int = 0,
    keyword: str = '',
) -> list[dict]:
    """LH 임대공고 목록을 조회하고, 공고별 공급유형 상세 정보를 함께 반환합니다.

    status: PAN_SS 필터값. 비어있으면 파라미터 자체를 제외하여 전체 조회.
            API 실측 상태값: '공고중', '접수중', '접수마감'
    lookback_days: 0이면 날짜 파라미터를 제외하여 현재 활성 공고 포함 조회.
                   0보다 크면 PAN_ST_DT/PAN_ED_DT로 과거 마감 공고 조회.
                   주의: 날짜 파라미터가 있으면 현재 활성 공고(공고중/접수중)가 제외됨.
    keyword: 지정 시 공고 목록 조회 직후 필터링하여
             필터된 건에 대해서만 공급정보 API를 병렬 호출합니다.

    Returns:
        list of dict: 각 공고의 기본 정보 + supply_columns + supply_details
    """
    if not API_KEY:
        raise EnvironmentError("OPEN_API_KEY 환경변수가 설정되지 않았습니다.")

    notice_params: dict = {
        "ServiceKey": API_KEY,
        "PG_SZ": limit,
        "PAGE": page,
        "UPP_AIS_TP_CD": tp_code,
    }

    # cnp_code 비어있으면 CNP_CD 제외 → 전국 조회
    if cnp_code:
        notice_params["CNP_CD"] = cnp_code

    # status가 비어있으면 PAN_SS 파라미터 제외 (빈 문자열 전달 시 0건 반환되는 API 버그 회피)
    if status:
        notice_params["PAN_SS"] = status

    # lookback_days > 0이면 날짜 파라미터 포함 (과거 마감 공고 조회)
    # lookback_days = 0이면 날짜 파라미터 제외 → 현재 활성 공고(공고중/접수중) 포함
    if lookback_days > 0:
        today = datetime.now()
        notice_params["PAN_ST_DT"] = (today - timedelta(days=lookback_days)).strftime('%Y.%m.%d')
        notice_params["PAN_ED_DT"] = today.strftime('%Y.%m.%d')

    async with httpx.AsyncClient(timeout=30.0) as client:
        notice_resp = await request_with_retry(client, "GET", NOTICE_URL, params=notice_params)
        notice_data = notice_resp.json()

        raw_list = _extract_ds_list(notice_data)
        if not raw_list:
            return []

        if keyword:
            raw_list = [n for n in raw_list if keyword in n.get('PAN_NM', '')]

        if not raw_list:
            return []

        supply_tasks = [_fetch_supply(client, item, tp_code) for item in raw_list]
        supply_results = await asyncio.gather(*supply_tasks)

        results = []
        for item, (supply_columns, supply_details, supply_error) in zip(raw_list, supply_results):
            results.append({
                "PAN_ID": item.get('PAN_ID', ''),
                "PAN_NM": item.get('PAN_NM', ''),
                "AIS_TP_CD_NM": item.get('AIS_TP_CD_NM', ''),
                "CNP_CD_NM": item.get('CNP_CD_NM', ''),
                "PAN_SS": item.get('PAN_SS', ''),
                "PAN_NT_ST_DT": item.get('PAN_NT_ST_DT', ''),   # 공고 시작일 (응답 필드명)
                "CLSG_DT": item.get('CLSG_DT', ''),             # 공고 마감일 (응답 필드명)
                "PAN_DT": item.get('PAN_DT', ''),               # 공고일자
                "DTL_URL": item.get('DTL_URL', ''),
                "SPL_INF_TP_CD": item.get('SPL_INF_TP_CD', ''),
                "CCR_CNNT_SYS_DS_CD": item.get('CCR_CNNT_SYS_DS_CD', ''),
                "supply_columns": supply_columns,
                "supply_details": supply_details,
                "supply_error": supply_error,
            })

        return results


async def fetch_supply_detail(
    pan_id: str,
    spl_inf_tp_cd: str,
    ccr_cnnt_sys_ds_cd: str,
    tp_code: str = "13",
) -> dict:
    """특정 LH 공고의 공급정보 상세를 조회합니다 (MCP 도구용).

    Returns:
        dict: {"supply_columns": dict, "supply_details": list}
    """
    item = {
        "PAN_ID": pan_id,
        "SPL_INF_TP_CD": spl_inf_tp_cd,
        "CCR_CNNT_SYS_DS_CD": ccr_cnnt_sys_ds_cd,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        columns, details, error = await _fetch_supply(client, item, tp_code)
    return {"supply_columns": columns, "supply_details": details, "supply_error": error}

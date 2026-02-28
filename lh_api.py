"""공통 LH API 로직 — server/lh_mcp.py 와 batch/lh_fetcher.py 양쪽에서 공유합니다."""
import os
import asyncio
import logging
import httpx
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

API_KEY = os.getenv("OPEN_API_KEY")

NOTICE_URL = "http://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"
SUPPLY_URL = "http://apis.data.go.kr/B552555/lhLeaseNoticeSplInfo1/getLeaseNoticeSplInfo1"

# 공급정보 API 동시 요청 수 제한 (429 Too Many Requests 방지)
_SUPPLY_SEMAPHORE = asyncio.Semaphore(5)

logger = logging.getLogger(__name__)


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


async def _fetch_supply(client: httpx.AsyncClient, item: dict, tp_code: str) -> tuple[dict, list]:
    """공고 1건의 공급정보를 조회.

    SPL_INF_TP_CD 또는 CCR_CNNT_SYS_DS_CD가 없으면 API 호출 없이 빈값 반환.
    Semaphore로 동시 요청 수를 제한하여 429 Too Many Requests 방지.
    """
    pan_id = item.get('PAN_ID', '')
    spl_tp = item.get('SPL_INF_TP_CD', '')
    ccr_cd = item.get('CCR_CNNT_SYS_DS_CD', '')

    if not spl_tp or not ccr_cd:
        return {}, []

    try:
        async with _SUPPLY_SEMAPHORE:
            supply_resp = await client.get(SUPPLY_URL, params={
                "ServiceKey": API_KEY,
                "SPL_INF_TP_CD": spl_tp,
                "CCR_CNNT_SYS_DS_CD": ccr_cd,
                "PAN_ID": pan_id,
                "UPP_AIS_TP_CD": tp_code,
            })
            supply_resp.raise_for_status()
        supply_data = supply_resp.json()
        cols = _extract_ds_list(supply_data, 'dsList01Nm')
        supply_columns = cols[0] if cols else {}
        supply_details = _extract_supply_list(supply_data)
        return supply_columns, supply_details
    except Exception as e:
        logger.warning(f"공급정보 조회 실패 (PAN_ID={pan_id}): {e}")
        return {}, []


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
        "CNP_CD": cnp_code,
    }

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
        notice_resp = await client.get(NOTICE_URL, params=notice_params)
        notice_resp.raise_for_status()
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
        for item, (supply_columns, supply_details) in zip(raw_list, supply_results):
            results.append({
                "PAN_ID": item.get('PAN_ID', ''),
                "PAN_NM": item.get('PAN_NM', ''),
                "AIS_TP_CD_NM": item.get('AIS_TP_CD_NM', ''),
                "CNP_CD_NM": item.get('CNP_CD_NM', ''),
                "PAN_SS": item.get('PAN_SS', ''),
                "PAN_NT_ST_DT": item.get('PAN_NT_ST_DT', ''),   # 공고 시작일 (응답 필드명)
                "CLSG_DT": item.get('CLSG_DT', ''),             # 공고 마감일 (응답 필드명)
                "DTL_URL": item.get('DTL_URL', ''),
                "supply_columns": supply_columns,
                "supply_details": supply_details,
            })

        return results

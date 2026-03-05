"""HTTP 재시도 유틸리티 — API 일시 장애 시 자동 재시도."""
import asyncio
import logging
import httpx

logger = logging.getLogger(__name__)

RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BASE_DELAY = 2  # seconds


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    """HTTP 요청을 재시도 로직과 함께 실행합니다.

    재시도 대상: HTTP 429, 500, 502, 503, 504 + httpx.TimeoutException
    전략: 최대 3회, exponential backoff (2s → 4s → 8s)
    """
    last_exc = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            resp = await getattr(client, method.lower())(url, **kwargs)
            if resp.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES:
                delay = BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"HTTP {resp.status_code} — {delay}초 후 재시도 ({attempt + 1}/{MAX_RETRIES}): {url}"
                )
                await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                delay = BASE_DELAY * (2 ** attempt)
                label = "Timeout" if isinstance(e, httpx.TimeoutException) else "ConnectError"
                logger.warning(
                    f"{label} — {delay}초 후 재시도 ({attempt + 1}/{MAX_RETRIES}): {url}"
                )
                await asyncio.sleep(delay)
            else:
                raise
    raise last_exc  # unreachable in normal flow, safety net

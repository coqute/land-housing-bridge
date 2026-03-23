"""Ollama HTTP 클라이언트 — 임베딩·비전·텍스트 생성의 단일 진입점.

모든 public 함수는 Ollama 미실행 시 None/False를 반환한다 (graceful degradation).
GPU 공유를 위해 _gpu_lock으로 embed/vision 동시 실행을 방지한다.
"""

import asyncio
import base64
import logging

import httpx

from config import OLLAMA_BASE_URL, OLLAMA_EMBED_MODEL, OLLAMA_VISION_MODEL
from http_utils import request_with_retry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 내부 상태
# ---------------------------------------------------------------------------
_gpu_lock = asyncio.Lock()
_cached_models: list[str] | None = None

# 비전 모델 우선순위 (자동 감지용, 한국어 성능 기준)
_VISION_MODEL_PRIORITY = [
    "qwen3-vl",
    "qwen2.5-vl",
    "llava",
    "moondream",
]

# 모델별 최적 프롬프트 프리픽스 (한국어 문서 분석 최적화)
_VISION_PROMPT_PREFIX = {
    "qwen3-vl": "한국어로 답변하세요. ",
    "qwen2.5-vl": "한국어로 답변하세요. ",
    "llava": "Answer in Korean. ",
    "moondream": "",
}

# 타임아웃 설정 (비전 모델은 텍스트보다 오래 걸림)
_EMBED_TIMEOUT = 60.0
_VISION_TIMEOUT = 120.0
_GENERATE_TIMEOUT = 120.0
_HEALTH_TIMEOUT = 5.0
_EMBED_BATCH_SIZE = 32


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------
def _client(timeout: float = 30.0) -> httpx.AsyncClient:
    """요청별 AsyncClient 생성 (connection pooling은 httpx 내부 처리)."""
    return httpx.AsyncClient(base_url=OLLAMA_BASE_URL, timeout=timeout)


async def _refresh_models() -> list[str]:
    """Ollama에서 사용 가능한 모델 목록을 갱신."""
    global _cached_models
    try:
        async with _client(timeout=_HEALTH_TIMEOUT) as client:
            resp = await client.get("/api/tags")
            resp.raise_for_status()
            data = resp.json()
            _cached_models = [m["name"] for m in data.get("models", [])]
            return _cached_models
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
        logger.debug(f"Ollama 모델 목록 조회 실패: {e}")
        _cached_models = []
        return []


def _resolve_vision_model() -> str | None:
    """사용 가능한 비전 모델을 우선순위에 따라 선택."""
    if OLLAMA_VISION_MODEL:
        if _cached_models and any(OLLAMA_VISION_MODEL in m for m in _cached_models):
            return OLLAMA_VISION_MODEL
        return OLLAMA_VISION_MODEL  # 설정값 존중 (실패는 호출 시점에 처리)

    if not _cached_models:
        return None

    for candidate in _VISION_MODEL_PRIORITY:
        for installed in _cached_models:
            if candidate in installed:
                logger.info(f"비전 모델 자동 감지: {installed}")
                return installed
    return None


def _get_vision_prefix(model: str) -> str:
    """모델에 맞는 프롬프트 프리픽스 반환."""
    for key, prefix in _VISION_PROMPT_PREFIX.items():
        if key in model:
            return prefix
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def check_ollama() -> bool:
    """Ollama 서버 가용 여부 확인 + 모델 목록 캐시."""
    models = await _refresh_models()
    if models:
        logger.debug(f"Ollama 사용 가능 — {len(models)}개 모델: {', '.join(models)}")
        return True
    return False


async def list_models() -> list[str]:
    """사용 가능한 모델 목록 반환 (캐시 우선)."""
    if _cached_models is None:
        await _refresh_models()
    return _cached_models or []


async def is_embed_available() -> bool:
    """임베딩 모델 사용 가능 여부."""
    models = await list_models()
    return any(OLLAMA_EMBED_MODEL in m for m in models)


async def is_vision_available() -> bool:
    """비전 모델 사용 가능 여부."""
    if _cached_models is None:
        await _refresh_models()
    return _resolve_vision_model() is not None


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """텍스트 리스트를 임베딩 벡터로 변환.

    Args:
        texts: 임베딩할 텍스트 리스트

    Returns:
        임베딩 벡터 리스트 (각 벡터는 float 리스트). 실패 시 None.
    """
    if not texts:
        return []

    all_embeddings: list[list[float]] = []

    async with _gpu_lock:
        async with _client(timeout=_EMBED_TIMEOUT) as client:
            for i in range(0, len(texts), _EMBED_BATCH_SIZE):
                batch = texts[i : i + _EMBED_BATCH_SIZE]
                try:
                    resp = await request_with_retry(
                        client, "post", f"{OLLAMA_BASE_URL}/api/embed",
                        json={"model": OLLAMA_EMBED_MODEL, "input": batch},
                    )
                    data = resp.json()
                    embeddings = data.get("embeddings", [])
                    if len(embeddings) != len(batch):
                        logger.error(
                            f"임베딩 수 불일치: 요청 {len(batch)}건, 응답 {len(embeddings)}건"
                        )
                        return None
                    all_embeddings.extend(embeddings)
                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    logger.warning(f"Ollama 임베딩 실패: {e}")
                    return None
                except httpx.HTTPStatusError as e:
                    logger.error(f"Ollama 임베딩 HTTP 오류: {e.response.status_code}")
                    return None

    return all_embeddings


async def analyze_image(
    image_bytes: bytes,
    prompt: str,
    format_schema: dict | None = None,
) -> str | None:
    """이미지를 비전 모델로 분석.

    Args:
        image_bytes: 이미지 바이트 데이터
        prompt: 분석 프롬프트
        format_schema: JSON 스키마 (structured output, optional)

    Returns:
        분석 결과 텍스트. 실패 시 None.
    """
    model = _resolve_vision_model()
    if not model:
        logger.info("비전 모델 미설치 — 이미지 분석 건너뜀 (ollama pull qwen2.5-vl)")
        return None

    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    prefix = _get_vision_prefix(model)
    full_prompt = prefix + prompt

    payload: dict = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": full_prompt,
                "images": [img_b64],
            }
        ],
        "stream": False,
    }
    if format_schema:
        payload["format"] = format_schema

    async with _gpu_lock:
        try:
            async with _client(timeout=_VISION_TIMEOUT) as client:
                resp = await request_with_retry(
                    client, "post", f"{OLLAMA_BASE_URL}/api/chat", json=payload,
                )
                data = resp.json()
                message = data.get("message", {})
                return message.get("content", "")
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"Ollama 비전 분석 실패: {e}")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(f"Ollama 비전 HTTP 오류: {e.response.status_code}")
            return None


async def generate(
    prompt: str,
    format_schema: dict | None = None,
    model: str | None = None,
) -> str | None:
    """텍스트 생성 (structured output 지원).

    Args:
        prompt: 생성 프롬프트
        format_schema: JSON 스키마 (structured output, optional)
        model: 모델 지정 (기본: OLLAMA_EMBED_MODEL의 base 모델 또는 첫 번째 사용 가능 모델)

    Returns:
        생성 결과 텍스트. 실패 시 None.
    """
    if not model:
        models = await list_models()
        # 임베딩 전용 모델이 아닌 생성 모델 찾기
        gen_models = [m for m in models if "embed" not in m.lower()]
        model = gen_models[0] if gen_models else (models[0] if models else None)
        if not model:
            logger.info("Ollama 생성 모델 없음")
            return None

    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if format_schema:
        payload["format"] = format_schema

    async with _gpu_lock:
        try:
            async with _client(timeout=_GENERATE_TIMEOUT) as client:
                resp = await request_with_retry(
                    client, "post", f"{OLLAMA_BASE_URL}/api/generate", json=payload,
                )
                data = resp.json()
                return data.get("response", "")
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"Ollama 텍스트 생성 실패: {e}")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(f"Ollama 생성 HTTP 오류: {e.response.status_code}")
            return None

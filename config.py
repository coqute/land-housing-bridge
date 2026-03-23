"""환경변수 일원화 — 프로젝트 전체에서 공유"""
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

OPEN_API_KEY = os.getenv("OPEN_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")

# Ollama AI (optional — 미설정 시 기본값 사용, validate_env 대상 아님)
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "qwen3-embedding:4b")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "")


def validate_env(required: list[str]) -> None:
    """필수 환경변수를 일괄 검증합니다. 누락 시 EnvironmentError를 raise합니다."""
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise EnvironmentError(f"필수 환경변수 누락: {', '.join(missing)}")

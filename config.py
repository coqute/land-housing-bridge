"""환경변수 일원화 — 프로젝트 전체에서 공유"""
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

OPEN_API_KEY = os.getenv("OPEN_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")

# LH 공고유형코드 — 배치·MCP 공유
LH_TP_CODES = ["13", "06"]  # 매입/전세임대 + 임대주택(행복주택, 국민임대 등)


def validate_env(required: list[str]) -> None:
    """필수 환경변수를 일괄 검증합니다. 누락 시 EnvironmentError를 raise합니다."""
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise EnvironmentError(f"필수 환경변수 누락: {', '.join(missing)}")

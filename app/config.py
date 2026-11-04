import os
from pathlib import Path
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Base directory for the project
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file
load_dotenv(dotenv_path=BASE_DIR / ".env")


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


class Settings(BaseSettings):
    # AI provider credentials. Only DeepSeek and Claude Opus are supported.
    AI_API_KEY: str = _env_first("AI_API_KEY", "DEEPSEEK_API_KEY")
    DEEPSEEK_API_KEY: str = _env_first("DEEPSEEK_API_KEY", "AI_API_KEY")
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-ai/deepseek-v4.1-flash-maas")
    OPUS_MODEL: str = os.getenv("OPUS_MODEL", "claude-opus-4-6-thinking")
    OPUS_BIN: str = os.getenv("OPUS_BIN", "")
    OPUS_BENCHMARK_ENABLED: bool = os.getenv("OPUS_BENCHMARK_ENABLED", "true").lower() in ("true", "1", "yes")

    # Legacy Vertex settings retained for the DeepSeek MaaS client only.
    VERTEX_AI: bool = os.getenv("VERTEX_AI", "true").lower() in ("true", "1", "yes")
    GOOGLE_CLOUD_PROJECT: str = os.getenv("GOOGLE_CLOUD_PROJECT", "super-512620")

    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "7000"))

    WORKSPACE_DIR: str = os.getenv("WORKSPACE_DIR", "./trading_workspace")
    AGENT_MAX_STEPS: int = int(os.getenv("AGENT_MAX_STEPS", "25"))
    COMMAND_TIMEOUT_SECONDS: int = int(os.getenv("COMMAND_TIMEOUT_SECONDS", "120"))

    # Initial Test Account Capital (Default: ₹10,000 INR)
    INITIAL_CAPITAL_INR: float = float(os.getenv("INITIAL_CAPITAL_INR", "10000.0"))

    # Trading Mode: "paper" (dummy trading on VM) or "live" (real broker API)
    TRADING_MODE: str = os.getenv("TRADING_MODE", "paper")

    # Fast Caching & Storage
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "./trading_workspace/trading_data.db")
    AUTO_EXECUTE_DEFAULT: bool = os.getenv("AUTO_EXECUTE_DEFAULT", "true").lower() in ("true", "1", "yes")
    ENFORCE_MARKET_HOURS: bool = os.getenv("ENFORCE_MARKET_HOURS", "true").lower() in ("true", "1", "yes")

    # Hard risk limits (calibrated for ₹10,000 capital accounts)
    MAX_DAILY_LOSS_PCT: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0"))
    MAX_POSITION_PCT: float = float(os.getenv("MAX_POSITION_PCT", "0.90"))
    MAX_TRADES_PER_DAY: int = int(os.getenv("MAX_TRADES_PER_DAY", "6"))
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    RISK_PER_TRADE_PCT: float = float(os.getenv("RISK_PER_TRADE_PCT", "0.75"))
    TARGET_NET_PROFIT_PCT: float = float(os.getenv("TARGET_NET_PROFIT_PCT", "1.0"))
    MIN_CONFIDENCE: int = int(os.getenv("MIN_CONFIDENCE", "70"))
    PAPER_SLIPPAGE_BPS: float = float(os.getenv("PAPER_SLIPPAGE_BPS", "5.0"))
    SQUARE_OFF_TIME_IST: str = os.getenv("SQUARE_OFF_TIME_IST", "15:20")

    # Fee-aware entry filter. A candidate is only tradable when its expected
    # gross move to target covers the round-trip transaction cost by at least
    # this multiple. 1.0 disables the filter (any edge that merely breaks even
    # after fees is allowed). Higher values demand a larger edge over costs.
    MIN_EDGE_TO_FEE_RATIO: float = float(os.getenv("MIN_EDGE_TO_FEE_RATIO", "1.0"))

    # Fee-to-risk ceiling. Reject a candidate when round-trip transaction cost
    # exceeds this percentage of the rupee risk budget (entry-stop)*qty. Small
    # positions and tight stops cannot amortise the fixed cost, so they are fee
    # sinks. 100.0 disables the filter. Default 25 was walk-forward validated on
    # 1y/1h data: it improved net P&L and profit factor in both halves of the
    # sample by dropping fee-sink trades (e.g. low-priced ETFs).
    MAX_FEE_TO_RISK_PCT: float = float(os.getenv("MAX_FEE_TO_RISK_PCT", "25.0"))

    # Security & Dashboard Login Authentication
    AUTH_ENABLED: bool = os.getenv("AUTH_ENABLED", "true").lower() in ("true", "1", "yes")
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "crimson2026")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "quant-terminal-secure-jwt-secret-key-2026")

    @property
    def absolute_workspace_dir(self) -> Path:
        ws = Path(self.WORKSPACE_DIR)
        if ws.is_absolute():
            return ws
        return (BASE_DIR / ws).resolve()

    @property
    def absolute_db_path(self) -> Path:
        db = Path(self.DATABASE_PATH)
        if db.is_absolute():
            return db
        return (BASE_DIR / db).resolve()

    class Config:
        env_file = ".env"
        extra = "allow"


settings = Settings()

# Ensure workspace directory exists
settings.absolute_workspace_dir.mkdir(parents=True, exist_ok=True)

DEFAULT_ACCOUNT_ID = "deepseek_flash"
ACCOUNT_IDS = ("opus_flash", "deepseek_flash")
ACCOUNT_NAMES = {
    "opus_flash": "Claude Opus 4.6 Thinking",
    "deepseek_flash": "DeepSeek 4.1 Flash MaaS",
}


def create_genai_client(api_key: str = None, location: str = None):
    """Instantiate the Google GenAI client used for DeepSeek MaaS on Vertex AI."""
    from google import genai

    key = api_key or settings.DEEPSEEK_API_KEY
    proj = getattr(settings, "GOOGLE_CLOUD_PROJECT", "super-512620")
    if location or "deepseek" in str(location or ""):
        return genai.Client(vertexai=settings.VERTEX_AI, api_key=key, project=proj, location="global")
    return genai.Client(vertexai=settings.VERTEX_AI, api_key=key)

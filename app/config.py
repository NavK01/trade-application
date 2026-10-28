import os
from pathlib import Path
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Base directory for the project
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file
load_dotenv(dotenv_path=BASE_DIR / ".env")

class Settings(BaseSettings):
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_AGENT_MODEL: str = os.getenv("GEMINI_AGENT_MODEL", "gemini-3.8-flash")
    GEMINI_CHAT_MODEL: str = os.getenv("GEMINI_CHAT_MODEL", "gemini-3.8-flash")
    GEMINI_LIVE_MODEL: str = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-flash")
    GEMINI_VOICE_NAME: str = os.getenv("GEMINI_VOICE_NAME", "Puck")
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
    AUTO_EXECUTE_DEFAULT: bool = os.getenv("AUTO_EXECUTE_DEFAULT", "false").lower() in ("true", "1", "yes")

    # Security & Dashboard Login Authentication
    AUTH_ENABLED: bool = os.getenv("AUTH_ENABLED", "true").lower() in ("true", "1", "yes")
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "crimson2026")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "gemini-quant-super-secure-jwt-secret-key-2026")

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

def create_genai_client(api_key: str = None, location: str = None):
    """Instantiate Google GenAI Client with Vertex AI support for Gemini & DeepSeek."""
    from google import genai
    key = api_key or settings.GEMINI_API_KEY
    proj = getattr(settings, "GOOGLE_CLOUD_PROJECT", "super-512620")
    if location or "deepseek" in str(location or ""):
        return genai.Client(vertexai=settings.VERTEX_AI, api_key=key, project=proj, location="global")
    return genai.Client(vertexai=settings.VERTEX_AI, api_key=key)

"""Configuration management for BathAgent."""

import os
from pathlib import Path
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
    else:
        load_dotenv()
except ImportError:
    pass


class Settings:
    # LLM Settings
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # Database Settings
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_PORT", "5432"))
    DB_USER: str = os.getenv("DB_USER", "postgres")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "postgres")
    DB_NAME: str = os.getenv("DB_NAME", "bathstuff")
    USE_SQLITE: bool = os.getenv("USE_SQLITE", "false").lower() == "true"
    SQLITE_PATH: str = os.getenv("SQLITE_PATH", "bathstuff.db")

    # MCP Toolbox for Databases
    TOOLBOX_URL: str = os.getenv("TOOLBOX_URL", "http://localhost:5000")

    # Wildfire Proxy Settings
    WILDFIRE_URL: str = os.getenv("WILDFIRE_URL", "http://localhost:8787")
    WILDFIRE_API_TOKEN: str = os.getenv("WILDFIRE_API_TOKEN", "")

    # Application Settings
    APP_TITLE: str = "BathAgent - BathStuff Operations AI Assistant"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"


settings = Settings()

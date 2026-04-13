from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "127.0.0.1"
    app_port: int = 8080
    app_env: str = "development"
    log_level: str = "INFO"

    session_secret: str = Field(..., min_length=32)
    site2_enc_key: str = Field(..., min_length=32)

    site1_base_url: str = "https://api-ita.smashit.tw"
    site2_firebase_api_key: str = "AIzaSyCeFhJuKTm0UTOncIjNJ3YbUIsvspI-p-A"
    site2_firebase_project: str = "ntust-grading"
    site3_apps_script_url: str = (
        "https://script.google.com/macros/s/"
        "AKfycbwQs_qm7GS-P3nshz4pgyjZ_XJslpl3BF_1t9UIKFYQRn49z8M_TIf36Qm0XRR79mi3Vw/exec"
    )

    database_url: str = "sqlite+aiosqlite:///./net_grading.db"

    @property
    def cookie_secure(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()

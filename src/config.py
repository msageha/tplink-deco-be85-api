from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Maps to USERNAME / PASSWORD in .env.
    # USERNAME is the TP-Link account email; it is NOT used by the local API
    # (kept for reference). Local auth relies on PASSWORD + the "admin" account.
    username: str
    password: str

    deco_host: str = "http://172.16.1.1"
    # Local account name used to build the request-signature hash.
    account: str = "admin"
    verify_ssl: bool = False
    timeout: int = 30


@lru_cache
def get_settings() -> Settings:
    # username/password are populated from the environment / .env by
    # pydantic-settings; the static checker can't see that.
    return Settings()  # ty: ignore[missing-argument]

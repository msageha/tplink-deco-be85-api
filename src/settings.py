from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    password: str = Field(description="Deco の管理パスワード (TP-Link ID のパスワード)")
    deco_host: str = Field(default="http://172.16.1.1", description="Deco の URL")
    account: str = Field(
        default="admin", description="署名 hash に使うローカル管理アカウント名"
    )
    verify_ssl: bool = Field(default=False, description="HTTPS 時に証明書を検証するか")
    timeout: int = Field(default=30, description="ルーターへの HTTP タイムアウト (秒)")


settings = Settings()

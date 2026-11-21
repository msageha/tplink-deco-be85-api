from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 必須の値が欠けたときの validation エラーには入力値の一覧が付き、PASSWORD がログに平文で出るので隠す。
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    password: str = Field(description="Deco の管理パスワード (TP-Link ID のパスワード)")
    deco_host: str = Field(description="Deco の URL")
    account: str = Field(
        default="admin", description="署名 hash に使うローカル管理アカウント名"
    )
    verify_ssl: bool = Field(default=False, description="HTTPS 時に証明書を検証するか")
    timeout: int = Field(default=30, description="ルーターへの HTTP タイムアウト (秒)")


# 必須の password と deco_host は環境変数から読む。pyright は pydantic-settings の読み込みを知らず、引数の不足と誤検知する。
settings = Settings()  # pyright: ignore[reportCallIssue]

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 必須の値が欠けたときの validation エラーには入力値の一覧が付き、PASSWORD がログに平文で出るので隠す。
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    # password と tplink_email が空のまま起動すると、呼び出しのたびに失敗する認証を送り、
    # ルーターの失敗回数の上限を消費するので、空の値は拒否する。
    password: str = Field(
        min_length=1, description="Deco の管理パスワード (TP-Link ID のパスワード)"
    )
    deco_host: str = Field(description="Deco の URL")
    tplink_email: str = Field(
        min_length=1,
        description="Deco のオーナーの TP-Link ID の email。Deco アプリと同じ TMP の接続の認証に使う",
    )
    account: str = Field(
        default="admin", description="署名 hash に使うローカル管理アカウント名"
    )
    verify_ssl: bool = Field(default=False, description="HTTPS 時に証明書を検証するか")
    timeout: int = Field(
        default=30, description="ルーターへの HTTP・TMP のタイムアウト (秒)"
    )
    tmp_host_key: str = Field(
        pattern=r"^[a-z0-9@.-]+ [A-Za-z0-9+/]+={0,2}$",
        description=(
            "信頼する Deco の TMP (SSH) の ECDSA の host key。`known_hosts` と同じ `<鍵の種類> <base64>` の形で、"
            "`ssh-keyscan -p 20001 -t ecdsa <Deco のアドレス>` の出力から取る。一致しない接続先には"
            "パスワードを送らない"
        ),
    )
    tmp_port: int = Field(
        default=20001,
        ge=1,
        le=65535,
        description="Deco の TMP (SSH) の port。port forward などで別の port から届くときに変える",
    )


# 必須の password・deco_host・tplink_email・tmp_host_key は環境変数から読む。pyright は pydantic-settings の読み込みを知らず、引数の不足と誤検知する。
settings = Settings()  # pyright: ignore[reportCallIssue]

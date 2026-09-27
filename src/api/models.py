"""API の request / response モデル。"""

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WifiBand(StrEnum):
    band2_4 = "band2_4"
    band5_1 = "band5_1"
    band6 = "band6"


class WifiNetwork(StrEnum):
    host = "host"
    guest = "guest"


class Operation(StrEnum):
    """Deco エンドポイントが受け付ける operation。"""

    read = "read"
    write = "write"
    load = "load"
    list = "list"
    get = "get"
    set = "set"
    add = "add"
    edit = "edit"
    remove = "remove"
    operate = "operate"


class _RouterResponse(BaseModel):
    """ルーター応答のモデル。ファームウェア差で増える未知フィールドも捨てずに返す。"""

    model_config = ConfigDict(extra="allow")


class DecoNode(_RouterResponse):
    mac: str | None = Field(
        default=None, description="ノードの MAC アドレス (reboot の指定に使う)"
    )
    role: str | None = Field(default=None, description="master / slave")
    device_model: str | None = Field(default=None, description="機種名 (例: BE85)")
    device_type: str | None = Field(default=None, description="機器種別")
    hardware_ver: str | None = Field(default=None, description="ハードウェアバージョン")
    software_ver: str | None = Field(
        default=None, description="ファームウェアバージョン"
    )
    device_ip: str | None = Field(default=None, description="ノードの LAN IP")
    nickname: str | None = Field(
        default=None, description="設置場所の名前 (平文にデコード済み)"
    )
    online: bool | None = Field(default=None, description="オンラインか")
    inet_status: str | None = Field(
        default=None, description="ノードから見たインターネット到達性"
    )


class ClientDevice(_RouterResponse):
    mac: str | None = Field(default=None, description="MAC アドレス")
    ip: str | None = Field(default=None, description="IP アドレス")
    name: str | None = Field(
        default=None, description="クライアント名 (平文にデコード済み)"
    )
    online: bool | None = Field(default=None, description="オンラインか")
    interface: str | None = Field(default=None, description="接続インターフェース")
    connection_type: str | None = Field(
        default=None, description="接続種別 (例: band5)"
    )
    wire_type: str | None = Field(default=None, description="有線 / 無線")
    client_type: str | None = Field(
        default=None, description="ルーターが判定した機器カテゴリ"
    )
    down_speed: int | None = Field(default=None, description="下り速度")
    up_speed: int | None = Field(default=None, description="上り速度")
    access_host: bool | None = Field(
        default=None, description="ルーターへのアクセス許可"
    )


class Performance(_RouterResponse):
    cpu_usage: float | None = Field(default=None, description="CPU 使用率")
    mem_usage: float | None = Field(default=None, description="メモリ使用率")


class DeviceMode(_RouterResponse):
    region: Any = Field(
        default=None, description='地域。文字列または {"device": "JP"} のような dict'
    )
    workmode: str | None = Field(default=None, description="動作モード")
    sysmode: str | None = Field(default=None, description="システムモード")


class TimeSettings(_RouterResponse):
    time: str | None = Field(default=None, description="現在時刻")
    date: str | None = Field(default=None, description="現在日付")
    timezone: str | None = Field(default=None, description="タイムゾーン")
    tz_region: str | None = Field(default=None, description="タイムゾーンの地域")
    continent: str | None = Field(default=None, description="大陸")
    dst_status: Any = Field(default=None, description="夏時間の状態")


class MacClone(_RouterResponse):
    enable: Any = Field(default=None, description="MAC クローンの有効状態")


class WirelessPower(_RouterResponse):
    support_dfs: bool | None = Field(default=None, description="DFS 対応か")


class CloudDeviceInfo(_RouterResponse):
    cloudUserName: str | None = Field(default=None, description="紐付いた TP-Link ID")  # noqa: N815 - ルーター定義の JSON キー
    role: int | None = Field(default=None, description="クラウド上の役割")
    model: str | None = Field(default=None, description="機種名")


class DashboardSummary(BaseModel):
    internet_online: bool = Field(description="WAN IPv4 アドレスを持っているか")
    connection_type: str | None = Field(description="WAN の接続方式 (例: dhcp)")
    wan_ipv4: str | None = Field(description="WAN IPv4 アドレス")
    cpu_usage: float | None = Field(description="メイン Deco の CPU 使用率")
    mem_usage: float | None = Field(description="メイン Deco のメモリ使用率")
    deco_count: int = Field(description="Deco ノード数")
    online_clients: int = Field(description="オンラインのクライアント数")
    decos: list[DecoNode] = Field(description="Deco ノード一覧")


class WlanBandToggle(BaseModel):
    band: WifiBand = Field(description="対象バンド")
    network: WifiNetwork = Field(
        default=WifiNetwork.host, description="host (通常) / guest (ゲスト)"
    )
    enable: bool = Field(description="有効にするか")


class WirelessSettings(BaseModel):
    """band × network 1 つ分の Wi-Fi 設定。指定したフィールドだけを書き込む。"""

    model_config = ConfigDict(extra="forbid")

    enable: bool | None = Field(default=None, description="Wi-Fi を有効にするか")
    ssid: str | None = Field(default=None, description="SSID (平文)")
    password: str | None = Field(default=None, description="パスワード (平文)")
    enable_hide_ssid: bool | None = Field(default=None, description="SSID を隠すか")
    channel: int | None = Field(default=None, description="チャンネル")
    channel_width: str | None = Field(default=None, description="チャンネル幅")
    mode: str | None = Field(default=None, description="無線モード")


class WirelessConfigUpdate(BaseModel):
    band: WifiBand = Field(description="対象バンド")
    network: WifiNetwork = Field(
        default=WifiNetwork.host, description="host (通常) / guest (ゲスト)"
    )
    settings: WirelessSettings = Field(description="変更するフィールド (1 つ以上)")

    @model_validator(mode="after")
    def _require_a_setting(self) -> Self:
        if not self.settings.model_dump(exclude_none=True):
            raise ValueError("settings must include at least one field to update")
        return self


class RebootRequest(BaseModel):
    confirm: bool = Field(description="誤操作防止。true のときだけ再起動する")
    macs: list[str] | None = Field(
        default=None, description="再起動するノードの MAC。省略時は全ノード"
    )


class RawRequest(BaseModel):
    """任意の Deco エンドポイントへのパススルー。"""

    path: str = Field(
        pattern=r"^[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*(\?form=[A-Za-z0-9_]+)?$",
        description="Deco API の相対パス (例: admin/network?form=internet)。英数字と _、区切りの / と 1 つの ?form= のみ",
        examples=["admin/network?form=internet", "admin/device?form=mode"],
    )
    operation: Operation = Field(
        default=Operation.read, description="ルーターへ渡す operation"
    )
    params: dict[str, Any] | None = Field(
        default=None, description="ルーターへそのまま渡す params"
    )

    @field_validator("path", mode="before")
    @classmethod
    def _strip_leading_slash(cls, value: Any) -> Any:
        return value.strip().lstrip("/") if isinstance(value, str) else value

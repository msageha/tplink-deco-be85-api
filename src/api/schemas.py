import re
from base64 import b64encode
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deco import decode_name


class _Lenient(BaseModel):
    """Base model that keeps unknown router fields instead of dropping them."""

    model_config = ConfigDict(extra="allow")


class WifiBand(StrEnum):
    band2_4 = "band2_4"
    band5_1 = "band5_1"
    band6 = "band6"


class WifiNetwork(StrEnum):
    host = "host"
    guest = "guest"


class Operation(StrEnum):
    """Operations accepted by Deco endpoints (used by the raw passthrough)."""

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


class DecoNode(_Lenient):
    mac: str | None = None
    role: str | None = None
    device_model: str | None = None
    device_type: str | None = None
    hardware_ver: str | None = None
    software_ver: str | None = None
    device_ip: str | None = None
    nickname: str | None = None
    online: bool | None = None
    inet_status: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "DecoNode":
        data = dict(raw)
        if "nickname" in data:
            data["nickname"] = decode_name(data["nickname"])
        if "custom_nickname" in data:
            data["custom_nickname"] = decode_name(data["custom_nickname"])
        return cls.model_validate(data)


class ClientDevice(_Lenient):
    mac: str | None = None
    ip: str | None = None
    name: str | None = None
    online: bool | None = None
    interface: str | None = None
    connection_type: str | None = None
    wire_type: str | None = None
    client_type: str | None = None
    down_speed: int | None = None
    up_speed: int | None = None
    access_host: bool | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "ClientDevice":
        data = dict(raw)
        if "name" in data:
            data["name"] = decode_name(data["name"])
        return cls.model_validate(data)


class Performance(_Lenient):
    cpu_usage: float | None = None
    mem_usage: float | None = None


class DeviceMode(_Lenient):
    region: Any = None  # may be a string or a dict like {"device": "JP"}
    workmode: str | None = None
    sysmode: str | None = None


class TimeSettings(_Lenient):
    time: str | None = None
    date: str | None = None
    timezone: str | None = None
    tz_region: str | None = None
    continent: str | None = None
    dst_status: Any | None = None


class MacClone(_Lenient):
    enable: Any = None


class WirelessPower(_Lenient):
    support_dfs: bool | None = None


class CloudDeviceInfo(_Lenient):
    cloudUserName: str | None = None  # noqa: N815 - router-defined JSON key
    role: int | None = None
    model: str | None = None


class DashboardSummary(BaseModel):
    internet_online: bool | None = None
    connection_type: str | None = None
    wan_ipv4: str | None = None
    cpu_usage: float | None = None
    mem_usage: float | None = None
    deco_count: int = 0
    online_clients: int = 0
    decos: list[DecoNode] = Field(default_factory=list)


class WlanBandToggle(BaseModel):
    band: WifiBand
    network: WifiNetwork = WifiNetwork.host
    enable: bool


class WirelessSettings(BaseModel):
    """Per band+network Wi-Fi settings. Only provided fields are written.

    ``ssid`` / ``password`` are given in plain text and base64-encoded before
    being sent (the router stores/returns them base64-encoded).
    """

    model_config = ConfigDict(extra="forbid")

    enable: bool | None = None
    ssid: str | None = None
    password: str | None = None
    enable_hide_ssid: bool | None = None
    channel: int | None = None
    channel_width: str | None = None
    mode: str | None = None


class WirelessConfigUpdate(BaseModel):
    """Structured write to ``admin/wireless?form=wlan``."""

    band: WifiBand
    network: WifiNetwork = WifiNetwork.host
    settings: WirelessSettings

    @model_validator(mode="after")
    def _require_one_field(self) -> "WirelessConfigUpdate":
        if not self.settings.model_dump(exclude_none=True):
            raise ValueError("settings must include at least one field to update")
        return self

    def to_params(self) -> dict[str, Any]:
        fields = self.settings.model_dump(exclude_none=True)
        for key in ("ssid", "password"):
            if key in fields:
                fields[key] = b64encode(str(fields[key]).encode()).decode()
        return {self.band.value: {self.network.value: fields}}

    def summary(self) -> dict[str, Any]:
        """Echo of what is being changed, with the password masked."""
        fields = self.settings.model_dump(exclude_none=True)
        if "password" in fields:
            fields["password"] = "***"
        return {self.band.value: {self.network.value: fields}}


class RebootRequest(BaseModel):
    confirm: bool = Field(description="Must be true to actually reboot")
    macs: list[str] | None = Field(
        default=None,
        description="Specific Deco MACs to reboot; defaults to all units when omitted",
    )


_PATH_RE = re.compile(r"^[a-zA-Z0-9_]+(/[a-zA-Z0-9_]+)*(\?form=[a-zA-Z0-9_]+)?$")


class RawRequest(BaseModel):
    """Generic passthrough to any Deco endpoint."""

    path: str = Field(
        description="Relative Deco API path, e.g. 'admin/network?form=internet'",
        examples=["admin/network?form=internet", "admin/device?form=mode"],
    )
    operation: Operation = Operation.read
    params: dict[str, Any] | None = Field(
        default=None, description="Optional params object passed through to the router"
    )

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        v = v.strip().lstrip("/")
        if "://" in v or ".." in v:
            raise ValueError(
                "path must be a relative Deco API path (no scheme, no '..')"
            )
        if not _PATH_RE.match(v):
            raise ValueError(
                "invalid path; expected like 'admin/<module>?form=<form>' "
                "(letters, digits, underscore and a single ?form=)"
            )
        return v

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"operation": self.operation.value}
        if self.params is not None:
            payload["params"] = self.params
        return payload

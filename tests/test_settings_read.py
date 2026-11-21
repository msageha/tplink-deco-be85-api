"""
ルーターの設定・状態を 1 つの luci form から読む GET endpoint。

各 endpoint は対応する form を読み、ルーターの応答をそのまま返す。モデルを定義した endpoint は
宣言したフィールドを型どおりに返し、未知のフィールドも保持する。
"""

from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given, settings, strategies as st

from tests.harness import Stack
from tests.strategies import (
    as_json,
    json_values,
    ok_envelope,
    ok_envelope_styles,
    unknown_fields,
)

_text = st.none() | st.text()
_int = st.none() | st.integers()
_bool = st.none() | st.booleans()
_float = st.none() | st.floats(allow_nan=False, allow_infinity=False)


@dataclass(frozen=True)
class _ReadEndpoint:
    endpoint: str
    router_path: str
    operation: str = "read"
    # モデルが宣言したフィールドと値の生成。None なら応答をそのまま返す endpoint。
    model_fields: dict[str, st.SearchStrategy[Any]] | None = None
    # ルーターへ送る params。全ノード共通の値を読む form は device_mac=default を要する。
    params: dict[str, str] | None = None


_READ_ENDPOINTS = [
    _ReadEndpoint("/api/network/wan", "admin/network?form=wan_ipv4"),
    _ReadEndpoint("/api/network/internet", "admin/network?form=internet"),
    _ReadEndpoint(
        "/api/network/lan",
        "admin/network?form=lan_ip",
        params={"device_mac": "default"},
    ),
    _ReadEndpoint(
        "/api/network/ipv6",
        "admin/network?form=ipv6",
        params={"device_mac": "default"},
    ),
    _ReadEndpoint(
        "/api/network/performance",
        "admin/network?form=performance",
        model_fields={"cpu_usage": _float, "mem_usage": _float},
    ),
    _ReadEndpoint(
        "/api/network/mac-clone",
        "admin/network?form=mac_clone",
        model_fields={"enable": json_values},
    ),
    _ReadEndpoint("/api/network/wan-mode", "admin/network?form=wan_mode"),
    _ReadEndpoint("/api/network/dhcp-dial", "admin/network?form=dhcp_dial"),
    _ReadEndpoint("/api/network/igmp", "admin/network?form=igmp_setting"),
    _ReadEndpoint("/api/network/fast-xmit", "admin/network?form=fast_xmit_setting"),
    _ReadEndpoint("/api/network/vlan", "admin/network?form=vlan"),
    _ReadEndpoint("/api/network/ddns", "admin/cloud?form=ddns", "get"),
    _ReadEndpoint("/api/wireless", "admin/wireless?form=wlan"),
    _ReadEndpoint(
        "/api/wireless/power",
        "admin/wireless?form=power",
        model_fields={"support_dfs": _bool},
    ),
    _ReadEndpoint("/api/wireless/beamforming", "admin/wireless?form=beamforming"),
    _ReadEndpoint("/api/wireless/operation-mode", "admin/wireless?form=operation_mode"),
    _ReadEndpoint("/api/wireless/bridge", "admin/wireless?form=bridge"),
    _ReadEndpoint("/api/wireless/roaming", "admin/wireless?form=ieee80211r"),
    _ReadEndpoint("/api/wireless/bandwidth", "admin/wireless?form=bandwidth_enhance"),
    _ReadEndpoint(
        "/api/device/mode",
        "admin/device?form=mode",
        model_fields={"region": json_values, "workmode": _text, "sysmode": _text},
    ),
    _ReadEndpoint(
        "/api/device/time",
        "admin/device?form=timesetting",
        model_fields={
            "time": _text,
            "date": _text,
            "timezone": _text,
            "tz_region": _text,
            "continent": _text,
            "dst_status": json_values,
        },
    ),
    _ReadEndpoint(
        "/api/device/speedtest",
        "admin/device?form=speedtest",
        model_fields={
            "down_speed": _int,
            "up_speed": _int,
            "last_speed_test_time": _int,
            "ever_tested": _bool,
            "status": _text,
        },
    ),
    _ReadEndpoint(
        "/api/cloud/device-info",
        "admin/cloud_account?form=get_deviceInfo",
        model_fields={"cloudUserName": _text, "role": _int, "model": _text},
    ),
    _ReadEndpoint("/api/cloud/login-status", "admin/cloud_account?form=check_login"),
    _ReadEndpoint(
        "/api/system/component-info", "admin/web?form=extra_component_info", "get"
    ),
    _ReadEndpoint(
        "/api/system/switch-list", "admin/component_control?form=switch_list"
    ),
]


# endpoint ごとに違うのは form の配線と応答モデルで、応答を返す処理は共通なので、入力数を絞る。
@pytest.mark.parametrize("read", _READ_ENDPOINTS, ids=lambda r: r.endpoint)
@settings(max_examples=25)
@given(data=st.data(), style=ok_envelope_styles)
def test_read_endpoint_returns_router_fields(
    *, stack: Stack, read: _ReadEndpoint, data: st.DataObject, style: str
) -> None:
    stack.reset()
    if read.model_fields is None:
        fields = data.draw(st.dictionaries(st.text(), json_values, max_size=4))
    else:
        known = st.fixed_dictionaries({}, optional=read.model_fields)
        unknown = unknown_fields(known=read.model_fields)
        fields = data.draw(known) | data.draw(unknown)
    stack.router.respond(
        read.router_path,
        envelope=ok_envelope(fields, style=style),
        operation=read.operation,
    )

    response = stack.api.get(read.endpoint)

    assert response.status_code == 200
    body = response.json()
    # モデルが宣言したフィールドは、ルーターの応答に無ければ null で返る。
    declared = dict.fromkeys(read.model_fields or {})
    assert as_json(body) == as_json(declared | fields)
    # 読むだけで、ほかの form や operation (write 等) を呼ばない。
    assert {(c.path, c.operation, as_json(c.params)) for c in stack.router.calls} == {
        (read.router_path, read.operation, as_json(read.params))
    }


def test_wireless_returns_ssid_and_password_in_base64(stack: Stack) -> None:
    wlan = {
        "band5_1": {
            "host": {"enable": True, "ssid": "SG9tZQ==", "password": "c2VjcmV0"},
            "guest": {"enable": False, "ssid": "R3Vlc3Q=", "password": ""},
        }
    }
    stack.reset()
    stack.router.respond_result("admin/wireless?form=wlan", result=wlan)

    response = stack.api.get("/api/wireless")

    assert response.status_code == 200
    assert as_json(response.json()) == as_json(wlan)


@given(log_types=json_values, style=ok_envelope_styles)
def test_log_types_returns_router_result(
    *, stack: Stack, log_types: Any, style: str
) -> None:
    stack.reset()
    stack.router.respond(
        "admin/log_export?form=types", envelope=ok_envelope(log_types, style=style)
    )

    response = stack.api.get("/api/system/log-types")

    assert response.status_code == 200
    assert as_json(response.json()) == as_json(log_types)

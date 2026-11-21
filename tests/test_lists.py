"""
ルーターの一覧を返す endpoint: /api/devices・/api/clients・/api/clients/blocked・/api/system/firmware・/api/dashboard。

ルーターは名前のフィールドを base64 で返し、API は平文にして返す。未知のフィールドは保持する。
"""

import json
from base64 import b64encode
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given, strategies as st

from tests.harness import RouterCall, Stack
from tests.strategies import ok_envelope, ok_envelope_styles, unknown_fields

_DEVICE_LIST = "admin/device?form=device_list"
_CLIENT_LIST = "admin/client?form=client_list"


_text = st.none() | st.text()
_int = st.none() | st.integers()
_bool = st.none() | st.booleans()

# 応答モデルが宣言したフィールド (mac を除く) の値の生成
_DECO_NODE = {
    "nickname": _text,
    "role": _text,
    "device_model": _text,
    "device_type": _text,
    "hardware_ver": _text,
    "software_ver": _text,
    "device_ip": _text,
    "online": _bool,
    "inet_status": _text,
}
_CLIENT_DEVICE = {
    "name": _text,
    "ip": _text,
    "online": _bool,
    "interface": _text,
    "connection_type": _text,
    "wire_type": _text,
    "client_type": _text,
    "down_speed": _int,
    "up_speed": _int,
    "access_host": _bool,
}
_FIRMWARE_STATUS = {
    "new_version": _text,
    "device_model": _text,
    "software_ver": _text,
    "need_to_upgrade": _bool,
    "need_force_upgrade": _bool,
    "release_date": _text,
}


@dataclass(frozen=True)
class _NamedList:
    endpoint: str
    router_path: str
    operation: str
    list_key: str
    # ルーターが base64 で返し、API が平文にするフィールド
    names: tuple[str, ...]
    # 応答モデルが宣言したフィールド (mac を除く)
    model_fields: dict[str, st.SearchStrategy[Any]]
    # ルーターが一覧を配列でなく、キー付きの table (空なら {}) で返しうるか
    table_form: bool = False
    params: dict[str, str] | None = None


_DEVICES = _NamedList(
    "/api/devices",
    _DEVICE_LIST,
    "read",
    "device_list",
    ("nickname", "custom_nickname"),
    _DECO_NODE,
)
_CLIENTS = _NamedList(
    "/api/clients",
    _CLIENT_LIST,
    "read",
    "client_list",
    ("name",),
    _CLIENT_DEVICE,
    params={"device_mac": "default"},
)
_NAMED_LISTS = [
    _DEVICES,
    _CLIENTS,
    _NamedList(
        "/api/clients/blocked",
        "admin/client?form=black_list",
        "list",
        "client_list",
        ("name",),
        _CLIENT_DEVICE,
        table_form=True,
    ),
    _NamedList(
        "/api/system/firmware",
        "admin/cloud?form=firmware_status",
        "check",
        "fw_list",
        ("new_version",),
        _FIRMWARE_STATUS,
    ),
]


@st.composite
def _items(draw: st.DrawFn, *, named_list: _NamedList) -> list[dict[str, Any]]:
    """平文の項目 (宣言したフィールドの一部と未知のフィールド)。同じ mac の行も作る。"""
    macs = draw(st.lists(st.text(min_size=1), max_size=3))
    macs += draw(st.lists(st.sampled_from(macs), max_size=2)) if macs else []
    optional = dict.fromkeys(named_list.names, _text) | named_list.model_fields
    fields = st.fixed_dictionaries({}, optional=optional)
    unknown = unknown_fields(known={"mac", *optional})
    return [{"mac": mac} | draw(fields) | draw(unknown) for mac in macs]


def _expected(items: list[dict[str, Any]], *, named_list: _NamedList) -> list[str]:
    """API が返すべき行。モデルが宣言したフィールドは、ルーターの応答に無ければ null で返る。"""
    declared = dict.fromkeys(named_list.model_fields)
    return _rows([declared | item for item in items])


def _encode_names(item: dict[str, Any], *, names: tuple[str, ...]) -> dict[str, Any]:
    return {
        k: b64encode(v.encode()).decode() if k in names and v is not None else v
        for k, v in item.items()
    }


def _rows(items: list[dict[str, Any]]) -> list[str]:
    """行を、件数を保ったまま順序に依らない形で比べるための表現。"""
    return sorted(json.dumps(item, sort_keys=True) for item in items)


@pytest.mark.parametrize("named_list", _NAMED_LISTS, ids=lambda n: n.endpoint)
@given(data=st.data())
def test_list_returns_every_row_with_names_decoded_and_unknown_fields_kept(
    *, stack: Stack, named_list: _NamedList, data: st.DataObject
) -> None:
    stack.reset()
    items = data.draw(_items(named_list=named_list))
    encoded = [_encode_names(i, names=named_list.names) for i in items]
    as_table = named_list.table_form and data.draw(st.booleans(), label="as_table")
    rows = {str(n): i for n, i in enumerate(encoded)} if as_table else encoded
    stack.router.respond(
        named_list.router_path,
        envelope=ok_envelope(
            {named_list.list_key: rows}, style=data.draw(ok_envelope_styles)
        ),
        operation=named_list.operation,
    )

    response = stack.api.get(named_list.endpoint)

    assert response.status_code == 200
    assert _rows(response.json()) == _expected(items, named_list=named_list)
    call = RouterCall(named_list.router_path, named_list.operation, named_list.params)
    assert call in stack.router.calls


@given(clients=_items(named_list=_CLIENTS))
def test_online_only_filters_to_online_clients(
    *, stack: Stack, clients: list[dict[str, Any]]
) -> None:
    stack.reset()
    stack.router.respond_result(
        _CLIENT_LIST,
        result={"client_list": [_encode_names(c, names=("name",)) for c in clients]},
    )

    online = stack.api.get("/api/clients", params={"online_only": "true"})
    everyone = stack.api.get("/api/clients", params={"online_only": "false"})

    assert online.status_code == everyone.status_code == 200
    online_clients = [c for c in clients if c.get("online")]
    assert _rows(online.json()) == _expected(online_clients, named_list=_CLIENTS)
    assert _rows(everyone.json()) == _expected(clients, named_list=_CLIENTS)


@pytest.mark.parametrize(
    "name",
    ["plain name", "/w=="],
    ids=["not-base64", "not-utf8"],
)
def test_name_that_cannot_be_decoded_is_returned_unchanged(
    *, stack: Stack, name: str
) -> None:
    """base64 でない名前や、復号しても UTF-8 でない名前でも、一覧の取得は失敗しない。"""
    stack.reset()
    stack.router.respond_result(
        _CLIENT_LIST, result={"client_list": [{"mac": "m", "name": name}]}
    )

    response = stack.api.get("/api/clients")

    assert response.status_code == 200
    assert [c["name"] for c in response.json()] == [name]


_optional_text = st.none() | st.text()
_usage = st.none() | st.floats(allow_nan=False, allow_infinity=False)


@given(
    devices=_items(named_list=_DEVICES),
    clients=st.lists(st.fixed_dictionaries({"online": st.booleans()}), max_size=6),
    cpu=_usage,
    mem=_usage,
    wan_ip=_optional_text,
    dial_type=_optional_text,
)
def test_dashboard_summarizes_router_state(  # noqa: PLR0913 - 生成する入力ごとの引数
    *,
    stack: Stack,
    devices: list[dict[str, Any]],
    clients: list[dict[str, Any]],
    cpu: float | None,
    mem: float | None,
    wan_ip: str | None,
    dial_type: str | None,
) -> None:
    stack.reset()
    stack.router.respond_result(
        _DEVICE_LIST,
        result={
            "device_list": [_encode_names(d, names=_DEVICES.names) for d in devices]
        },
    )
    stack.router.respond_result(_CLIENT_LIST, result={"client_list": clients})
    stack.router.respond_result(
        "admin/network?form=performance", result={"cpu_usage": cpu, "mem_usage": mem}
    )
    stack.router.respond_result(
        "admin/network?form=wan_ipv4",
        result={"wan": {"ip_info": {"ip": wan_ip}, "dial_type": dial_type}},
    )

    response = stack.api.get("/api/dashboard")

    assert response.status_code == 200
    summary = response.json()
    assert summary["internet_online"] is bool(wan_ip)
    assert summary["wan_ipv4"] == wan_ip
    assert summary["connection_type"] == dial_type
    assert summary["cpu_usage"] == cpu
    assert summary["mem_usage"] == mem
    assert summary["deco_count"] == len(devices)
    assert summary["online_clients"] == sum(c["online"] for c in clients)
    assert _rows(summary["decos"]) == _expected(devices, named_list=_DEVICES)


def test_dashboard_with_wan_not_object_is_502(stack: Stack) -> None:
    """回線の状態を読めないときは、オフラインとして返さずに 502 にする。"""
    stack.reset()
    stack.router.respond_result(_DEVICE_LIST, result={"device_list": []})
    stack.router.respond_result(_CLIENT_LIST, result={"client_list": []})
    stack.router.respond_result("admin/network?form=performance", result={})
    stack.router.respond_result("admin/network?form=wan_ipv4", result={"wan": "down"})

    response = stack.api.get("/api/dashboard")

    assert response.status_code == 502


def test_dashboard_reads_empty_lua_table_as_no_wan(stack: Stack) -> None:
    stack.reset()
    stack.router.respond_result(_DEVICE_LIST, result={"device_list": []})
    stack.router.respond_result(_CLIENT_LIST, result={"client_list": []})
    stack.router.respond_result("admin/network?form=performance", result={})
    stack.router.respond_result("admin/network?form=wan_ipv4", result={"wan": []})

    response = stack.api.get("/api/dashboard")

    assert response.status_code == 200
    assert response.json()["internet_online"] is False

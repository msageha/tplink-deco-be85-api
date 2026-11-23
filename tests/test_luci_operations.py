"""
POST /api/luci/<module>/<form>/<operation>: luci の Web API の操作。

各 endpoint は OpenAPI の `x-deco-luci-path` の form を `x-deco-operation` の operation で呼び、
成功応答の本体 (`result` または `data`) を返す。`x-deco-destructive` が true の操作は
`confirm: true` がなければルーターに送らない。
"""

from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given, strategies as st

from tests.harness import RouterCall, Stack
from tests.strategies import as_json, json_values, ok_envelope, ok_envelope_styles


@dataclass(frozen=True)
class _Operation:
    path: str
    router_path: str
    operation: str
    destructive: bool


def _operations(stack: Stack) -> list[_Operation]:
    paths = stack.api.get("/openapi.json").json()["paths"]
    return [
        _Operation(
            path,
            spec["post"]["x-deco-luci-path"],
            spec["post"]["x-deco-operation"],
            spec["post"]["x-deco-destructive"],
        )
        for path, spec in paths.items()
        if path.startswith("/api/luci/")
    ]


_params = st.none() | st.dictionaries(st.text(), json_values, max_size=3)


@pytest.mark.parametrize(
    ("path", "router_path", "operation"),
    [
        (
            "/api/luci/client/traffic_stat/list",
            "admin/client?form=traffic_stat",
            "list",
        ),
        (
            "/api/luci/client/addr_reservation/getlist",
            "admin/client?form=addr_reservation",
            "getlist",
        ),
        (
            "/api/luci/network/flow_control/read",
            "admin/network?form=flow_control",
            "read",
        ),
        ("/api/luci/locale/lang/read", "locale?form=lang", "read"),
    ],
)
def test_luci_operation_calls_the_form_and_operation_in_its_path(
    *, stack: Stack, path: str, router_path: str, operation: str
) -> None:
    stack.reset()
    stack.router.respond_result(router_path, result={"ok": True}, operation=operation)

    response = stack.api.post(path, json={})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert stack.router.calls == [RouterCall(router_path, operation, None)]


def test_luci_operations_do_not_expose_credentials_or_login(stack: Stack) -> None:
    """管理パスワードや cloud の token を返す read と、ログインの手順には endpoint が無い。"""
    stack.reset()
    targets = {(op.router_path, op.operation) for op in _operations(stack)}

    assert len(targets) > 100
    for hidden in [
        ("admin/administration?form=account", "read"),
        ("admin/administration?form=recovery", "read"),
        ("admin/cloud_account?form=get_token", "read"),
        ("admin/cloud_account?form=user_login", "read"),
        ("login?form=login", "login"),
    ]:
        assert hidden not in targets


@given(
    data=st.data(),
    params=_params,
    result=json_values,
    style=ok_envelope_styles,
)
def test_luci_operation_sends_params_and_returns_result(
    *,
    stack: Stack,
    data: st.DataObject,
    params: dict[str, Any] | None,
    result: Any,
    style: str,
) -> None:
    stack.reset()
    operation = data.draw(st.sampled_from(_operations(stack)))
    stack.router.respond(
        operation.router_path,
        envelope=ok_envelope(result, style=style),
        operation=operation.operation,
    )
    body: dict[str, Any] = {} if params is None else {"params": params}
    if operation.destructive:
        body["confirm"] = True

    response = stack.api.post(operation.path, json=body)

    assert response.status_code == 200
    assert as_json(response.json()) == as_json(result)
    assert [(c.path, c.operation, as_json(c.params)) for c in stack.router.calls] == [
        (operation.router_path, operation.operation, as_json(params))
    ]


@given(
    envelope=st.one_of(
        st.fixed_dictionaries(
            {"error_code": st.integers().filter(lambda c: c != 0), "msg": json_values}
        ),
        st.fixed_dictionaries(
            {"success": st.just(value=False), "errorcode": json_values}
        ),
    )
)
def test_luci_operation_with_router_error_is_502(
    *, stack: Stack, envelope: dict[str, Any]
) -> None:
    stack.reset()
    stack.router.respond(
        "admin/client?form=traffic_stat", envelope=envelope, operation="list"
    )

    response = stack.api.post("/api/luci/client/traffic_stat/list", json={})

    assert response.status_code == 502


@pytest.mark.parametrize(
    ("path", "router_path", "operation"),
    [
        ("/api/luci/device/system/factory", "admin/device?form=system", "factory"),
        (
            "/api/luci/cloud/firmware_status/upgrade",
            "admin/cloud?form=firmware_status",
            "upgrade",
        ),
        (
            "/api/luci/administration/account/write",
            "admin/administration?form=account",
            "write",
        ),
        (
            "/api/luci/network/wan_ipv4/disconnect",
            "admin/network?form=wan_ipv4",
            "disconnect",
        ),
        (
            "/api/luci/wireless/operation_mode/write",
            "admin/wireless?form=operation_mode",
            "write",
        ),
        ("/api/luci/network/lan_ip/write", "admin/network?form=lan_ip", "write"),
    ],
)
def test_destructive_luci_operation_runs_only_with_confirm_true(
    *, stack: Stack, path: str, router_path: str, operation: str
) -> None:
    stack.reset()
    stack.router.respond_result(router_path, result={}, operation=operation)

    assert stack.api.post(path, json={}).status_code == 422
    assert stack.api.post(path, json={"confirm": 1}).status_code == 422
    assert stack.api.post(path, json={"confirm": False}).status_code == 400
    assert stack.router.calls == []

    response = stack.api.post(path, json={"confirm": True})

    assert response.status_code == 200
    assert stack.router.calls == [RouterCall(router_path, operation, None)]

"""
POST /api/app/<controller>/<form>/<operation>: Deco アプリの経路 (TMP) の操作。

各 endpoint は OpenAPI の `x-deco-tmp-opcode` の opcode を呼び、ルーターの `result` を返す。
`x-deco-destructive` が true の操作は `confirm: true` がなければルーターに送らない。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import example, given, strategies as st

from tests.fake_tmp import DISCONNECT, FRAGMENT_SIZE, BusinessError, FakeTmp
from tests.harness import ROUTER_PASSWORD, FakeDeco, Stack, run_api
from tests.strategies import as_json, json_values


@dataclass(frozen=True)
class _Operation:
    path: str
    opcode: int
    destructive: bool


def _operations(stack: Stack) -> list[_Operation]:
    paths = stack.api.get("/openapi.json").json()["paths"]
    return [
        _Operation(
            path, spec["post"]["x-deco-tmp-opcode"], spec["post"]["x-deco-destructive"]
        )
        for path, spec in paths.items()
        if path.startswith("/api/app/")
    ]


def _operation(stack: Stack, *, path: str) -> _Operation:
    return next(op for op in _operations(stack) if op.path == path)


_params = st.none() | st.dictionaries(st.text(), json_values, max_size=3)


@pytest.mark.parametrize(
    ("path", "opcode"),
    [
        # Deco アプリが同じ操作に送る opcode。
        ("/api/app/client/client_list/read", 0x4012),
        ("/api/app/client/addr_reservation/getlist", 0x40C0),
        ("/api/app/nat/vs/getlist", 0x40B0),
        ("/api/app/device/led/read", 0x401A),
        ("/api/app/internet_backup/internet_backup/get", 0x43E0),
    ],
)
def test_app_operations_call_the_opcode_the_deco_app_uses(
    *, stack: Stack, path: str, opcode: int
) -> None:
    stack.reset()
    stack.app.respond_result(opcode, result={"ok": True})

    response = stack.api.post(path, json={})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert [call.opcode for call in stack.app.calls] == [opcode]


def test_app_operations_do_not_expose_debug_or_mesh_internal_opcodes(
    stack: Stack,
) -> None:
    """任意のファイルを読める debug・試験用の opcode や、ノード間の同期の opcode には endpoint が無い。"""
    stack.reset()
    paths = {op.path for op in _operations(stack)}

    assert len(paths) > 300
    assert not any(
        path.startswith(("/api/app/debug/", "/api/app/sync/", "/api/app/auto_test/"))
        for path in paths
    )
    assert "/api/app/iot_device/zigbee/testcmd" not in paths
    assert stack.api.post("/api/app/debug/qlog/read", json={}).status_code == 404


@given(data=st.data(), params=_params, result=json_values)
def test_app_operation_sends_params_and_returns_result(
    *, stack: Stack, data: st.DataObject, params: dict[str, Any] | None, result: Any
) -> None:
    stack.reset()
    operation = data.draw(st.sampled_from(_operations(stack)))
    stack.app.respond_result(operation.opcode, result=result)
    body: dict[str, Any] = {} if params is None else {"params": params}
    if operation.destructive:
        body["confirm"] = True

    response = stack.api.post(operation.path, json=body)

    assert response.status_code == 200
    assert as_json(response.json()) == as_json(result)
    [call] = stack.app.calls
    assert call.opcode == operation.opcode
    assert as_json(call.body.get("params")) == as_json(params)
    assert ("params" in call.body) == (params is not None)
    assert type(call.body["config_version"]) is int


def test_app_operation_without_body_sends_no_params(stack: Stack) -> None:
    stack.reset()
    stack.app.respond_result(0x401A, result={"enable": True})

    response = stack.api.post_bytes(
        "/api/app/device/led/read", body=b"", content_type="application/json"
    )

    assert response.status_code == 200
    [call] = stack.app.calls
    assert "params" not in call.body


# fake が返す応答の本文 (envelope の JSON) のうち、result.text 以外の部分の長さ。
_ENVELOPE_OVERHEAD = len(json.dumps({"error_code": 0, "result": {"text": ""}}))


@given(size=st.integers(min_value=_ENVELOPE_OVERHEAD, max_value=FRAGMENT_SIZE * 4))
@example(size=FRAGMENT_SIZE)
@example(size=FRAGMENT_SIZE + 1)
@example(size=FRAGMENT_SIZE * 2)
def test_app_operation_reassembles_a_response_split_by_the_router(
    *, stack: Stack, size: int
) -> None:
    """ルーターが分割して返す長い応答 (本文が size byte) も、1 つの result として返す。"""
    stack.reset()
    result = {"text": "x" * (size - _ENVELOPE_OVERHEAD)}
    stack.app.respond_result(0x401A, result=result)

    response = stack.api.post("/api/app/device/led/read", json={})

    assert response.status_code == 200
    assert response.json() == result


@given(error_code=st.integers().filter(lambda code: code != 0))
def test_app_operation_with_router_error_code_is_502(
    *, stack: Stack, error_code: int
) -> None:
    stack.reset()
    stack.app.respond(0x401A, envelope={"error_code": error_code})

    response = stack.api.post("/api/app/device/led/read", json={})

    assert response.status_code == 502
    assert response.json()["error_code"] == error_code


@pytest.mark.parametrize(
    "envelope",
    [
        {"result": {}},
        {"error_code": "0", "result": {}},
        {"error_code": False, "result": {}},
        [],
    ],
)
def test_app_operation_without_numeric_success_code_is_502(
    *, stack: Stack, envelope: Any
) -> None:
    stack.reset()
    stack.app.respond(0x401A, envelope=envelope)

    response = stack.api.post("/api/app/device/led/read", json={})

    assert response.status_code == 502


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"error_code": 0, "result": NaN}',
        b'{"error_code": 0, "result": "\\ud800"}',
    ],
)
def test_app_operation_with_response_not_json_value_is_502(
    *, stack: Stack, body: bytes
) -> None:
    stack.reset()
    stack.app.respond(0x401A, envelope=body)

    response = stack.api.post("/api/app/device/led/read", json={})

    assert response.status_code == 502


def test_app_operation_rejected_in_business_header_is_502(stack: Stack) -> None:
    stack.reset()
    stack.app.respond(0x401A, envelope=BusinessError(3))

    response = stack.api.post("/api/app/device/led/read", json={})

    assert response.status_code == 502
    assert response.json()["error_code"] == 3


def test_app_session_is_reused_across_requests(stack: Stack) -> None:
    stack.reset_logged_out()
    stack.app.respond_result(0x401A, result={})

    for _ in range(3):
        assert stack.api.post("/api/app/device/led/read", json={}).status_code == 200

    assert stack.app.logins == 1
    assert stack.api.get("/api/health").json()["tmp_connected"] is True


def test_app_session_closed_by_router_is_reopened_on_next_request(
    stack: Stack,
) -> None:
    stack.reset_logged_out()
    stack.app.respond_result(0x401A, result={"enable": True})
    assert stack.api.post("/api/app/device/led/read", json={}).status_code == 200
    stack.drop_tmp_sessions()

    response = stack.api.post("/api/app/device/led/read", json={})

    assert response.status_code == 200
    assert response.json() == {"enable": True}
    assert stack.app.logins == 2


def test_app_session_lost_during_request_is_504_without_resending(
    stack: Stack,
) -> None:
    """要求が届いたか分からないので再送しない (write を二重に適用しない)。次の要求は接続し直す。"""
    stack.reset_logged_out()
    stack.app.respond(0x401B, envelope=DISCONNECT)

    response = stack.api.post(
        "/api/app/device/led/write", json={"params": {"enable": False}}
    )

    assert response.status_code == 504
    assert len(stack.app.calls) == 1
    stack.app.respond_result(0x401B, result={})
    assert (
        stack.api.post("/api/app/device/led/write", json={"params": {}}).status_code
        == 200
    )


@pytest.mark.parametrize("field", ["email", "password"])
def test_app_login_rejected_is_401_and_not_retried_until_restart(
    *, tmp_path: Path, field: str
) -> None:
    """
    資格情報を拒否されたら 401 を返し、以後の要求でも認証を送らない。

    誤った資格情報での再試行はルーターの失敗回数の上限を消費するため。拒否はサーバーのプロセスが
    覚えるので、共有の API サーバーとは別に起動する。
    """
    with (
        FakeDeco.serve() as router,
        FakeTmp.serve(password=ROUTER_PASSWORD) as app,
        run_api(
            deco_host=router.url, tmp_port=app.port, log_path=tmp_path / "uvicorn.log"
        ) as api,
    ):
        setattr(app, field, "other@example.com" if field == "email" else "other")

        first = api.post("/api/app/device/led/read", json={})
        second = api.post("/api/app/device/led/read", json={})

        assert (first.status_code, second.status_code) == (401, 401)
        assert app.auth_attempts == 1
        assert app.calls == []


def test_app_unreachable_router_is_504(stack: Stack) -> None:
    stack.reset_logged_out()
    stack.app.reachable = False

    response = stack.api.post("/api/app/device/led/read", json={})

    assert response.status_code == 504


@pytest.mark.parametrize(
    "path",
    [
        "/api/app/device/system/reboot",
        "/api/app/cloud/system/unbind",
        "/api/app/cloud/firmware/upgrade",
        "/api/app/device/sysmode/write",
        "/api/app/security/history/clear",
        "/api/app/security/history/remove",
        "/api/app/network/lan_ip/write",
        "/api/app/network/wan_ipv4/write",
        "/api/app/network/vlan/write",
        "/api/app/network/wan_isp/set",
        "/api/app/device/fixed_wan_port/set",
    ],
)
def test_destructive_app_operation_runs_only_with_confirm_true(
    *, stack: Stack, path: str
) -> None:
    stack.reset()
    operation = _operation(stack, path=path)
    assert operation.destructive
    stack.app.respond_result(operation.opcode, result={})

    assert stack.api.post(path, json={}).status_code == 422
    assert stack.api.post(path, json={"confirm": "true"}).status_code == 422
    assert stack.api.post(path, json={"confirm": False}).status_code == 400
    assert stack.app.calls == []

    response = stack.api.post(path, json={"confirm": True, "params": {"x": 1}})

    assert response.status_code == 200
    assert [(c.opcode, c.body["params"]) for c in stack.app.calls] == [
        (operation.opcode, {"x": 1})
    ]


def test_app_params_not_json_values_are_rejected_without_contacting_router(
    stack: Stack,
) -> None:
    stack.reset()

    response = stack.api.post_bytes(
        "/api/app/device/led/write",
        body=b'{"params": {"value": NaN}}',
        content_type="application/json",
    )

    assert response.status_code == 422
    assert stack.app.calls == []


def test_app_operation_with_inconsistent_fragments_is_502(stack: Stack) -> None:
    """分割された応答の offset が受け取った長さと合わなければ、つながずに失敗にする。"""
    stack.reset()
    stack.app.fragment_skew = 1
    stack.app.respond_result(0x401A, result={"text": "x" * (FRAGMENT_SIZE * 2)})

    response = stack.api.post("/api/app/device/led/read", json={})

    assert response.status_code == 502


@pytest.mark.parametrize("corrupt", ["packet", "body"])
@pytest.mark.parametrize("size", [10, FRAGMENT_SIZE * 2])
def test_app_operation_with_corrupt_crc_is_502(
    *, stack: Stack, corrupt: str, size: int
) -> None:
    """TMP の packet か本文全体の CRC32 が合わない応答は、分割の有無によらず受け付けない。"""
    stack.reset()
    stack.app.corrupt = corrupt
    stack.app.respond_result(0x401A, result={"text": "x" * size})

    response = stack.api.post("/api/app/device/led/read", json={})

    assert response.status_code == 502


def test_app_login_interrupted_by_router_is_504_not_401(stack: Stack) -> None:
    """認証の結果が返らないまま切れたときは、資格情報の不一致ではなく通信の失敗として返す。"""
    stack.reset_logged_out()
    stack.app.drops_during_auth = True

    response = stack.api.post("/api/app/device/led/read", json={})

    assert response.status_code == 504


# 1 つの TMP packet に載る要求の本文の上限 (payload 長 16 bit から business header の 20 byte を引いた長さ)。
_MAX_REQUEST_BODY = 0xFFFF - 20


def _params_with_body_size(size: int) -> dict[str, Any]:
    """送られる要求の本文 (config_version と params の JSON) がちょうど size byte になる params。"""
    overhead = len(
        json.dumps({"config_version": 1_700_000_000_000, "params": {"v": ""}})
    )
    return {"v": "x" * (size - overhead)}


def test_app_params_filling_one_packet_are_sent(stack: Stack) -> None:
    stack.reset()
    stack.app.respond_result(0x401B, result={})

    response = stack.api.post(
        "/api/app/device/led/write",
        json={"params": _params_with_body_size(_MAX_REQUEST_BODY)},
    )

    assert response.status_code == 200
    [call] = stack.app.calls
    assert len(json.dumps(call.body)) == _MAX_REQUEST_BODY


@pytest.mark.parametrize("size", [_MAX_REQUEST_BODY + 1, 70_000])
def test_app_params_too_large_for_one_packet_are_413_without_contacting_router(
    *, stack: Stack, size: int
) -> None:
    stack.reset_logged_out()

    response = stack.api.post(
        "/api/app/device/led/write", json={"params": _params_with_body_size(size)}
    )

    assert response.status_code == 413
    assert stack.app.logins == 0
    assert stack.app.calls == []

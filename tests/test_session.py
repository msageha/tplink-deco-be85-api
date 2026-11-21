"""ルーターへのログインセッション: /api/health・/api/login・/api/logout と、失効時の再ログイン、失敗時の status。"""

import json
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

from tests.harness import (
    FORBIDDEN,
    LOGIN_PAGE,
    UNAUTHORIZED,
    FakeDeco,
    HttpReply,
    RouterCall,
    Stack,
    run_api,
)
from tests.strategies import json_values

_WAN = "admin/network?form=wan_ipv4"


def test_started_server_contacts_router_only_when_needed(tmp_path: Path) -> None:
    """起動しただけではルーターにログインせず、最初の要求で 1 回だけログインする。"""
    with (
        FakeDeco.serve() as router,
        run_api(deco_host=router.url, log_path=tmp_path / "uvicorn.log") as api,
    ):
        assert api.get("/api/health").json()["logged_in"] is False
        assert router.arrivals == []
        router.respond_result(_WAN, result={"wan": {}})

        assert api.get("/api/network/wan").status_code == 200
        assert router.logins == 1


def test_health_reports_logged_out_without_contacting_router(stack: Stack) -> None:
    stack.reset_logged_out()

    response = stack.api.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "host": stack.router.url,
        "logged_in": False,
    }
    assert stack.router.arrivals == []


def test_login_and_logout_toggle_the_router_session(stack: Stack) -> None:
    stack.reset_logged_out()

    login = stack.api.post("/api/login")
    assert login.status_code == 200
    assert login.json() == {"logged_in": True}
    assert stack.router.logged_in
    stack.reset()
    assert stack.api.get("/api/health").json()["logged_in"] is True
    assert stack.router.arrivals == []

    logout = stack.api.post("/api/logout")
    assert logout.status_code == 200
    assert logout.json() == {"logged_in": False}
    assert not stack.router.logged_in
    assert stack.api.get("/api/health").json()["logged_in"] is False


def test_requests_log_in_once_and_reuse_the_session(stack: Stack) -> None:
    stack.reset_logged_out()
    stack.router.respond_result(_WAN, result={"wan": {}})

    assert stack.api.get("/api/network/wan").status_code == 200
    assert stack.api.get("/api/network/wan").status_code == 200

    assert stack.router.logins == 1
    assert len(stack.router.calls) == 2


@pytest.mark.parametrize(
    "rejection", [LOGIN_PAGE, UNAUTHORIZED, FORBIDDEN], ids=["html", "401", "403"]
)
def test_expired_session_is_renewed_and_request_resent_once(
    *, stack: Stack, rejection: HttpReply
) -> None:
    stack.reset_logged_out()
    wan = {"wan": {"ip_info": {"ip": "192.0.2.1"}}}
    stack.router.respond_result(_WAN, result=wan)
    stack.router.rejection = rejection
    assert stack.api.post("/api/login").status_code == 200
    stack.router.expire_session()

    response = stack.api.get("/api/network/wan")

    assert response.status_code == 200
    assert response.json() == wan
    assert stack.router.logins == 2
    # 再ログインでは AES の鍵と IV をどちらも作り直す
    keys = stack.router.session_keys
    assert len({key for key, _ in keys}) == len({iv for _, iv in keys}) == 2
    # 失効したセッションでの 1 回と、再ログイン後の再送 1 回
    assert stack.router.arrivals.count(_WAN) == 2
    assert stack.router.calls == [RouterCall(_WAN, "read", None)]


def test_request_rejected_again_after_relogin_is_502(stack: Stack) -> None:
    """再ログインは 1 回だけで、ログイン試行の回数制限に向けて繰り返さない。"""
    stack.reset_logged_out()
    stack.router.accepts_sessions = False

    response = stack.api.get("/api/network/wan")

    assert response.status_code == 502
    assert stack.router.logins == 2
    assert stack.router.arrivals.count(_WAN) == 2


def test_wrong_password_is_401(stack: Stack) -> None:
    stack.reset_logged_out()
    stack.router.password = "changed-on-the-router"

    assert stack.api.post("/api/login").status_code == 401
    assert stack.api.get("/api/network/wan").status_code == 401
    assert stack.api.get("/api/health").json()["logged_in"] is False


def test_login_without_session_cookie_is_401(stack: Stack) -> None:
    stack.reset_logged_out()
    stack.router.sets_cookie = False

    assert stack.api.post("/api/login").status_code == 401
    assert stack.api.get("/api/health").json()["logged_in"] is False


def test_unencrypted_envelope_is_accepted(stack: Stack) -> None:
    stack.reset()
    stack.router.respond_raw(
        _WAN, reply=HttpReply.json({"error_code": 0, "result": {"wan": {}}})
    )

    response = stack.api.get("/api/network/wan")

    assert response.status_code == 200
    assert response.json() == {"wan": {}}


def test_undecryptable_response_is_502(stack: Stack) -> None:
    stack.reset()
    stack.router.respond_raw(_WAN, reply=HttpReply.json({"data": "not a ciphertext"}))

    response = stack.api.get("/api/network/wan")

    assert response.status_code == 502


_PERFORMANCE = "admin/network?form=performance"
_DEVICE_LIST = "admin/device?form=device_list"
# 応答に出てはいけない値の目印
_SECRET = "value-from-router"


@pytest.mark.parametrize(
    ("endpoint", "router_path", "envelope"),
    [
        ("/api/network/wan", _WAN, {"error_code": 0, "result": {"x": "\ud800"}}),
        (
            "/api/network/performance",
            _PERFORMANCE,
            {"error_code": 0, "result": {"x": "\ud800"}},
        ),
        ("/api/network/wan", _WAN, {"error_code": 0, "result": {"x": float("nan")}}),
        ("/api/network/wan", _WAN, [_SECRET]),
        ("/api/network/wan", _WAN, {"error_code": 0, "result": [_SECRET]}),
        (
            "/api/network/performance",
            _PERFORMANCE,
            {"error_code": 0, "result": {"cpu_usage": _SECRET}},
        ),
        (
            "/api/devices",
            _DEVICE_LIST,
            {"error_code": 0, "result": {"device_list": [_SECRET]}},
        ),
        ("/api/network/wan", _WAN, {"error_code": "E1", "msg": "failed"}),
        ("/api/network/wan", _WAN, {"error_code": False, "result": {}}),
        (
            "/api/network/performance",
            _PERFORMANCE,
            {"error_code": 0, "result": {"cpu_usage": "NaN"}},
        ),
    ],
    ids=[
        "lone-surrogate",
        "lone-surrogate-in-model",
        "nan",
        "envelope-not-object",
        "result-not-object",
        "model-type-mismatch",
        "list-item-not-object",
        "error-code-not-number",
        "error-code-false",
        "usage-not-finite",
    ],
)
def test_malformed_router_response_is_502(
    *, stack: Stack, endpoint: str, router_path: str, envelope: object
) -> None:
    """不正な応答は 502 にし、error_code には数値だけを、detail にはモデルに合わない値を載せない。"""
    stack.reset()
    stack.router.respond(router_path, envelope=envelope)

    response = stack.api.get(endpoint)

    assert response.status_code == 502
    body = response.json()
    assert body["detail"]
    assert body["error_code"] is None
    assert _SECRET not in response.text


def test_http_error_from_router_is_502_with_its_error_code(stack: Stack) -> None:
    stack.reset()
    stack.router.respond_raw(
        _WAN,
        reply=HttpReply(
            500,
            "application/json",
            json.dumps({"error_code": 0, "result": {}}).encode(),
        ),
    )

    response = stack.api.get("/api/network/wan")

    assert response.status_code == 502
    assert response.json()["error_code"] == 0


def test_rejection_body_in_any_charset_is_502(stack: Stack) -> None:
    """拒否の本文がどの文字コードを宣言していても、エラー応答を作れる。"""
    stack.reset_logged_out()
    stack.router.accepts_sessions = False
    stack.router.rejection = HttpReply(
        200, "text/html; charset=unicode_escape", b"\\ud800"
    )

    response = stack.api.get("/api/network/wan")

    assert response.status_code == 502


def test_unreachable_router_is_504(stack: Stack) -> None:
    stack.reset_logged_out()
    stack.router.reachable = False

    assert stack.api.post("/api/login").status_code == 504
    assert stack.api.get("/api/network/wan").status_code == 504


def test_unreachable_router_is_504_with_an_established_session(stack: Stack) -> None:
    stack.reset_logged_out()
    assert stack.api.post("/api/login").status_code == 200
    stack.router.reachable = False

    response = stack.api.get("/api/network/wan")

    assert response.status_code == 504
    # セッションの stok は URL に入るが、エラーには載せない
    assert ";stok=" not in response.text


def test_connection_refused_is_504_without_the_session_token(tmp_path: Path) -> None:
    """接続を拒否されたときの例外には stok を含む URL が入るが、エラー応答には載せない。"""
    with (
        FakeDeco.serve() as router,
        run_api(deco_host=router.url, log_path=tmp_path / "uvicorn.log") as api,
    ):
        assert api.post("/api/login").status_code == 200
        router.stop()

        response = api.get("/api/network/wan")

        assert response.status_code == 504
        assert ";stok=" not in response.text


def test_logout_succeeds_while_router_is_unreachable(stack: Stack) -> None:
    stack.reset_logged_out()
    assert stack.api.post("/api/login").status_code == 200
    stack.router.reachable = False

    response = stack.api.post("/api/logout")

    assert response.status_code == 200
    assert response.json() == {"logged_in": False}
    assert stack.api.get("/api/health").json()["logged_in"] is False


@given(
    error_code=st.integers().filter(lambda code: code != 0),
    details=json_values,
)
def test_router_error_is_502_with_its_error_code(
    *, stack: Stack, error_code: int, details: object
) -> None:
    stack.reset()
    stack.router.respond(_WAN, envelope={"error_code": error_code, "msg": details})

    response = stack.api.get("/api/network/wan")

    assert response.status_code == 502
    body = response.json()
    assert body["error_code"] == error_code
    assert body["detail"]


@given(errorcode=json_values)
def test_router_failure_in_success_style_is_502(
    *, stack: Stack, errorcode: object
) -> None:
    stack.reset()
    stack.router.respond(_WAN, envelope={"success": False, "errorcode": errorcode})

    response = stack.api.get("/api/network/wan")

    assert response.status_code == 502
    body = response.json()
    assert body["detail"]
    assert "error_code" in body

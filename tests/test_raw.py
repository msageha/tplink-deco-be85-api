"""POST /api/raw: 任意の luci endpoint への passthrough。"""

from typing import Any

from hypothesis import example, given, strategies as st

from tests.harness import RouterCall, Stack
from tests.strategies import as_json, json_values

_OPERATIONS = [
    "read",
    "write",
    "load",
    "list",
    "get",
    "set",
    "add",
    "edit",
    "remove",
    "operate",
    "check",
    "build",
]
_WORD = "[A-Za-z0-9_]+"
_paths = st.from_regex(rf"{_WORD}(/{_WORD})*(\?form={_WORD})?", fullmatch=True)


@given(
    path=_paths,
    leading_slash=st.booleans(),
    operation=st.sampled_from(_OPERATIONS),
    params=st.none() | st.dictionaries(st.text(), json_values, max_size=3),
    envelope=st.one_of(
        st.dictionaries(st.text(), json_values, max_size=4),
        st.fixed_dictionaries({"error_code": st.integers(), "msg": json_values}),
        st.fixed_dictionaries({"success": st.booleans(), "data": json_values}),
    ),
)
def test_raw_sends_request_as_given_and_returns_envelope_unchanged(  # noqa: PLR0913 - 生成する入力ごとの引数
    *,
    stack: Stack,
    path: str,
    leading_slash: bool,
    operation: str,
    params: dict[str, Any] | None,
    envelope: dict[str, Any],
) -> None:
    """ルーターがエラーを返しても、envelope をそのまま 200 で返す。"""
    stack.reset()
    stack.router.respond(path, envelope=envelope, operation=operation)
    body: dict[str, Any] = {
        "path": f"/{path}" if leading_slash else path,
        "operation": operation,
    }
    if params is not None:
        body["params"] = params

    response = stack.api.post("/api/raw", json=body)

    assert response.status_code == 200
    assert as_json(response.json()) == as_json(envelope)
    assert [(c.path, c.operation, as_json(c.params)) for c in stack.router.calls] == [
        (path, operation, as_json(params))
    ]


def test_raw_defaults_to_read(stack: Stack) -> None:
    stack.reset()
    stack.router.respond_result("admin/device?form=mode", result={"sysmode": "router"})

    response = stack.api.post("/api/raw", json={"path": "admin/device?form=mode"})

    assert response.json() == {"error_code": 0, "result": {"sysmode": "router"}}
    assert stack.router.calls == [RouterCall("admin/device?form=mode", "read", None)]


_ALLOWED_IN_PATH = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_/"
)


@st.composite
def _unsafe_paths(draw: st.DrawFn) -> str:
    """有効な path の内側に、path に使えない文字 (`.`・`:`・`&`・空白など) を 1 つ挟む。"""
    path = draw(_paths.filter(lambda p: len(p) >= 2))
    position = draw(st.integers(min_value=1, max_value=len(path) - 1))
    char = draw(st.characters(exclude_characters=_ALLOWED_IN_PATH))
    return path[:position] + char + path[position:]


@given(path=_unsafe_paths(), operation=st.sampled_from(_OPERATIONS))
@example(path="", operation="read")
@example(path="admin\ud800/x", operation="read")
@example(path="admin/", operation="read")
@example(path="admin//x", operation="read")
@example(path="admin/x?form=", operation="read")
@example(path="admin/x?form=a?form=b", operation="read")
@example(path="admin/x?form=y&z=1", operation="read")
@example(path="ad min/x", operation="read")
@example(path="admin/../etc", operation="read")
@example(path="http://evil/x", operation="read")
def test_raw_rejects_unsafe_path_without_contacting_router(
    *, stack: Stack, path: str, operation: str
) -> None:
    stack.reset()

    response = stack.api.post("/api/raw", json={"path": path, "operation": operation})

    assert response.status_code == 422
    assert stack.router.arrivals == []


@given(operation=st.text().filter(lambda op: op not in _OPERATIONS))
@example(operation="destroy")
@example(operation="")
def test_raw_rejects_unknown_operation_without_contacting_router(
    *, stack: Stack, operation: str
) -> None:
    stack.reset()

    response = stack.api.post(
        "/api/raw", json={"path": "admin/device?form=mode", "operation": operation}
    )

    assert response.status_code == 422
    assert stack.router.arrivals == []


def test_raw_params_with_text_not_valid_unicode_are_rejected(stack: Stack) -> None:
    stack.reset()

    response = stack.api.post(
        "/api/raw",
        json={"path": "admin/device?form=mode", "params": {"name": "\ud800"}},
    )

    assert response.status_code == 422
    assert stack.router.arrivals == []


def test_raw_with_envelope_not_object_is_502(stack: Stack) -> None:
    stack.reset()
    stack.router.respond("admin/device?form=mode", envelope=None)

    response = stack.api.post("/api/raw", json={"path": "admin/device?form=mode"})

    assert response.status_code == 502


def test_raw_params_with_nan_are_rejected(stack: Stack) -> None:
    stack.reset()

    response = stack.api.post_bytes(
        "/api/raw",
        body=b'{"path": "admin/device?form=mode", "params": {"value": NaN}}',
        content_type="application/json",
    )

    assert response.status_code == 422
    assert stack.router.arrivals == []


def test_raw_body_not_json_is_422(stack: Stack) -> None:
    stack.reset()

    response = stack.api.post_bytes(
        "/api/raw", body=b"path=admin/device?form=mode", content_type="text/plain"
    )

    assert response.status_code == 422
    assert stack.router.arrivals == []

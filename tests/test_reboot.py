"""POST /api/reboot: confirm=true のときだけ、指定した (省略時は全) ノードを再起動させる。"""

from typing import Any

import pytest
from hypothesis import given, strategies as st

from tests.harness import HttpReply, Stack

_REBOOT = "admin/device?form=system"
_DEVICE_LIST = "admin/device?form=device_list"


def _rebooted_macs(stack: Stack) -> list[list[str]]:
    """ルーターが受けた再起動の指示ごとの、対象の MAC (順序に依らない形)。"""
    return [
        sorted(entry["mac"] for entry in c.params["mac_list"])
        for c in stack.router.calls
        if c.path == _REBOOT and c.operation == "reboot"
    ]


@given(macs=st.lists(st.text(min_size=1), min_size=1, max_size=5))
def test_reboot_targets_the_given_nodes(*, stack: Stack, macs: list[str]) -> None:
    stack.reset()
    stack.router.respond_result(_REBOOT, result={}, operation="reboot")

    response = stack.api.post("/api/reboot", json={"confirm": True, "macs": macs})

    assert response.status_code == 200
    assert sorted(response.json()["rebooting"]) == sorted(macs)
    assert _rebooted_macs(stack) == [sorted(macs)]


@given(
    devices=st.lists(
        st.fixed_dictionaries({}, optional={"mac": st.none() | st.text()}),
        max_size=5,
    ),
    body=st.sampled_from(
        [
            {"confirm": True},
            {"confirm": True, "macs": []},
            {"confirm": True, "macs": None},
        ]
    ),
)
def test_reboot_without_macs_targets_every_node(
    *, stack: Stack, devices: list[dict[str, str]], body: dict[str, Any]
) -> None:
    stack.reset()
    stack.router.respond_result(_DEVICE_LIST, result={"device_list": devices})
    stack.router.respond_result(_REBOOT, result={}, operation="reboot")
    # MAC が無い (null・空文字の) ノードは指定できないので対象外
    macs = sorted(d["mac"] for d in devices if d.get("mac"))

    response = stack.api.post("/api/reboot", json=body)

    if macs:
        assert response.status_code == 200
        assert sorted(response.json()["rebooting"]) == macs
        assert _rebooted_macs(stack) == [macs]
    else:
        assert response.status_code == 404
        assert _rebooted_macs(stack) == []


def test_reboot_rejected_by_router_is_502_and_not_retried(stack: Stack) -> None:
    stack.reset()
    stack.router.respond(
        _REBOOT, envelope={"error_code": -40101, "msg": "rejected"}, operation="reboot"
    )

    response = stack.api.post("/api/reboot", json={"confirm": True, "macs": ["m"]})

    assert response.status_code == 502
    assert response.json()["error_code"] == -40101
    assert _rebooted_macs(stack) == [["m"]]


@pytest.mark.parametrize(
    "body",
    [{"confirm": False}, {"confirm": False, "macs": ["m"]}],
    ids=["false", "false-with-macs"],
)
def test_reboot_with_confirm_false_is_400_without_contacting_router(
    *, stack: Stack, body: dict[str, Any]
) -> None:
    stack.reset()

    response = stack.api.post("/api/reboot", json=body)

    assert response.status_code == 400
    assert stack.router.arrivals == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"macs": ["m"]},
        {"confirm": "true", "macs": ["m"]},
        {"confirm": 1, "macs": ["m"]},
        {"confirm": True, "macs": "m"},
        {"confirm": True, "macs": ["\ud800"]},
    ],
    ids=[
        "omitted",
        "omitted-with-macs",
        "string-true",
        "number-1",
        "macs-not-list",
        "mac-not-valid-unicode",
    ],
)
def test_invalid_reboot_request_is_422_without_contacting_router(
    *, stack: Stack, body: dict[str, Any]
) -> None:
    """confirm は JSON の true だけを受け付ける。"""
    stack.reset()

    response = stack.api.post("/api/reboot", json=body)

    assert response.status_code == 422
    assert stack.router.arrivals == []


@pytest.mark.parametrize(
    "reply",
    [
        HttpReply(500, "text/plain", b"Internal Server Error"),
        HttpReply(200, "application/json", b"{broken"),
    ],
    ids=["http-500", "broken-json"],
)
def test_reboot_with_unreadable_answer_is_502_and_not_resent(
    *, stack: Stack, reply: HttpReply
) -> None:
    """ルーターの応答を読めなくても、再起動の指示を再送しない (二重に適用しうるため)。"""
    stack.reset()
    stack.router.respond_raw(_REBOOT, reply=reply, operation="reboot")

    response = stack.api.post("/api/reboot", json={"confirm": True, "macs": ["m"]})

    assert response.status_code == 502
    assert stack.router.arrivals.count(_REBOOT) == 1


def test_reboot_all_uses_macs_even_if_other_fields_are_malformed(stack: Stack) -> None:
    stack.reset()
    stack.router.respond_result(
        _DEVICE_LIST, result={"device_list": [{"mac": "m", "online": "yes?"}]}
    )
    stack.router.respond_result(_REBOOT, result={}, operation="reboot")

    response = stack.api.post("/api/reboot", json={"confirm": True})

    assert response.status_code == 200
    assert _rebooted_macs(stack) == [["m"]]


def test_reboot_all_with_malformed_mac_is_502_without_rebooting(stack: Stack) -> None:
    stack.reset()
    stack.router.respond_result(_DEVICE_LIST, result={"device_list": [{"mac": 1}]})
    stack.router.respond_result(_REBOOT, result={}, operation="reboot")

    response = stack.api.post("/api/reboot", json={"confirm": True})

    assert response.status_code == 502
    assert _rebooted_macs(stack) == []

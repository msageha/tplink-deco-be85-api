"""
Wi-Fi 設定の書き込み: POST /api/wireless (ON / OFF) と POST /api/wireless/config。

どちらも Deco アプリと同じ TMP の経路 (wlan の write の opcode) で書き込む。
"""

from base64 import b64encode
from itertools import product
from typing import Any

import pytest
from hypothesis import example, given, strategies as st

from tests.harness import Stack
from tests.strategies import as_json, json_values, merged

# Deco アプリが Wi-Fi の設定の書き込みに送る opcode。
_WLAN_WRITE = 0x4008
_BANDS = ["band2_4", "band5_1", "band6"]
_NETWORKS = ["host", "guest"]
# band の radio に書く設定と、TMP での名前。
_RADIO_FIELDS = {"channel": "channel", "channel_width": "htmode", "mode": "hwmode"}
_SETTING_FIELDS = {
    "enable": st.booleans(),
    "ssid": st.text(),
    "password": st.text(),
    "enable_hide_ssid": st.booleans(),
    "channel": st.integers(),
    "channel_width": st.text(),
    "mode": st.text(),
}


def _b64(text: str) -> str:
    return b64encode(text.encode()).decode()


def _wlan_writes(stack: Stack) -> list[Any]:
    return [c.body.get("params") for c in stack.app.calls if c.opcode == _WLAN_WRITE]


@pytest.mark.parametrize(
    ("band", "network", "enable"), list(product(_BANDS, _NETWORKS, [True, False]))
)
def test_toggle_writes_enable_for_the_band_and_network(
    *, stack: Stack, band: str, network: str, enable: bool
) -> None:
    stack.reset()
    stack.app.respond_result(_WLAN_WRITE, result={})

    response = stack.api.post(
        "/api/wireless", json={"band": band, "network": network, "enable": enable}
    )

    assert response.status_code == 200
    written = {band: {network: {"enable": enable}}}
    assert as_json(response.json()) == as_json({"updated": written})
    assert as_json(_wlan_writes(stack)) == as_json([written])


@pytest.mark.parametrize(
    ("endpoint", "body"),
    [
        ("/api/wireless", {"band": "band6", "enable": True}),
        ("/api/wireless/config", {"band": "band6", "settings": {"enable": True}}),
    ],
)
def test_write_rejected_by_router_is_502_and_not_retried(
    *, stack: Stack, endpoint: str, body: dict[str, Any]
) -> None:
    stack.reset()
    stack.app.respond(_WLAN_WRITE, envelope={"error_code": -40101, "msg": "rejected"})

    response = stack.api.post(endpoint, json=body)

    assert response.status_code == 502
    assert response.json()["error_code"] == -40101
    assert len(_wlan_writes(stack)) == 1


def test_toggle_targets_host_network_by_default(stack: Stack) -> None:
    stack.reset()
    stack.app.respond_result(_WLAN_WRITE, result={})

    response = stack.api.post("/api/wireless", json={"band": "band6", "enable": False})

    assert response.status_code == 200
    assert as_json(_wlan_writes(stack)) == as_json(
        [{"band6": {"host": {"enable": False}}}]
    )


@pytest.mark.parametrize(
    "body",
    [
        {"band": "band9", "enable": True},
        {"band": "band6", "network": "office", "enable": True},
        {"band": "band6"},
        {"band": "band6", "enable": "maybe"},
        {"band": "band6", "enable": None},
        {"enable": True},
    ],
    ids=[
        "unknown-band",
        "unknown-network",
        "no-enable",
        "enable-not-bool",
        "enable-null",
        "no-band",
    ],
)
def test_invalid_toggle_is_rejected_without_contacting_router(
    *, stack: Stack, body: dict[str, Any]
) -> None:
    stack.reset()

    response = stack.api.post("/api/wireless", json=body)

    assert response.status_code == 422
    assert stack.router.arrivals == []
    assert stack.app.calls == []


@st.composite
def _settings(draw: st.DrawFn) -> dict[str, Any]:
    """値のあるフィールドを 1 つ以上含み、ほかのフィールドは省略・null・値のどれかにする。"""
    required = draw(st.sampled_from(list(_SETTING_FIELDS)))
    return draw(
        st.fixed_dictionaries(
            {required: _SETTING_FIELDS[required]},
            optional={
                k: st.none() | values
                for k, values in _SETTING_FIELDS.items()
                if k != required
            },
        )
    )


@given(
    band=st.sampled_from(_BANDS),
    network=st.none() | st.sampled_from(_NETWORKS),
    settings=_settings(),
    result=json_values,
)
def test_config_writes_given_fields_with_ssid_and_password_in_base64(
    *,
    stack: Stack,
    band: str,
    network: str | None,
    settings: dict[str, Any],
    result: Any,
) -> None:
    """
    null のフィールドは書き込まず、network の省略は host を指す。

    channel / channel_width / mode は network によらず band の radio に TMP の名前で書く。
    """
    stack.reset()
    stack.app.respond_result(_WLAN_WRITE, result=result)
    body = {"band": band, "settings": settings}
    if network is not None:
        body["network"] = network

    response = stack.api.post("/api/wireless/config", json=body)

    assert response.status_code == 200
    target = network or "host"
    given_fields = {k: v for k, v in settings.items() if v is not None}
    network_sent = {
        k: _b64(v) if k in {"ssid", "password"} else v
        for k, v in given_fields.items()
        if k not in _RADIO_FIELDS
    }
    radio_sent = {
        _RADIO_FIELDS[k]: str(v) if k == "channel" else v
        for k, v in given_fields.items()
        if k in _RADIO_FIELDS
    }
    sent = ({target: network_sent} if network_sent else {}) | (
        {"radio": radio_sent} if radio_sent else {}
    )
    assert as_json(_wlan_writes(stack)) == as_json([{band: sent}])
    shown = given_fields | ({"password": "***"} if "password" in given_fields else {})
    assert as_json(response.json()) == as_json(
        {"updated": {band: {target: shown}}, "result": result}
    )


_invalid_config = st.one_of(
    # 書き込むフィールドが無い (null だけの指定を含む)
    st.fixed_dictionaries(
        {
            "band": st.sampled_from(_BANDS),
            "settings": st.fixed_dictionaries(
                {}, optional=dict.fromkeys(_SETTING_FIELDS, st.none())
            ),
        }
    ),
    # 未知の設定フィールド (書き込める設定と混ざっていても拒否する)
    st.fixed_dictionaries(
        {
            "band": st.sampled_from(_BANDS),
            "settings": merged(
                st.fixed_dictionaries({}, optional=_SETTING_FIELDS),
                st.dictionaries(
                    st.text().map(lambda k: f"x_{k}"), json_values, min_size=1
                ),
            ),
        }
    ),
    # 存在しない band
    st.fixed_dictionaries(
        {
            "band": st.text().filter(lambda b: b not in _BANDS),
            "settings": st.just({"enable": True}),
        }
    ),
    # 存在しない network
    st.fixed_dictionaries(
        {
            "band": st.sampled_from(_BANDS),
            "network": st.text().filter(lambda n: n not in _NETWORKS),
            "settings": st.just({"enable": True}),
        }
    ),
)


@given(body=_invalid_config)
@example(body={"band": "band6", "settings": {}})
@example(body={"band": "band6", "settings": {"ssid": None, "password": None}})
@example(body={"band": "band6", "settings": {"enable": True, "foo": "bar"}})
@example(body={"band": "band9", "settings": {"enable": True}})
@example(body={"settings": {"enable": True}})
@example(body={"band": "band6"})
@example(body={"band": "band6", "settings": [{"enable": True}]})
def test_invalid_config_is_rejected_without_writing(
    *, stack: Stack, body: dict[str, Any]
) -> None:
    stack.reset()

    response = stack.api.post("/api/wireless/config", json=body)

    assert response.status_code == 422
    assert stack.router.arrivals == []
    assert stack.app.calls == []


# パスワードを足しても不正なままの入力 (band・network・未知のフィールド)
_INVALID_WITH_PASSWORD: list[dict[str, Any]] = [
    {"settings": {}},
    {"band": "band9", "settings": {}},
    {"band": "band6", "network": "office", "settings": {}},
    {"band": "band6", "settings": {"foo": "bar"}},
]


@pytest.mark.parametrize("body", _INVALID_WITH_PASSWORD)
@given(password=st.text())
def test_invalid_config_response_does_not_depend_on_the_password(
    *, stack: Stack, body: dict[str, Any], password: str
) -> None:
    """422 の応答はパスワードの値を含まないので、パスワードだけを変えても同じになる。"""
    stack.reset()

    def post(value: str) -> Any:
        settings = body["settings"] | {"password": value}
        response = stack.api.post(
            "/api/wireless/config", json=body | {"settings": settings}
        )
        assert response.status_code == 422
        return response.json()

    assert as_json(post(password)) == as_json(post("another-password"))


@pytest.mark.parametrize(
    "settings",
    [{"ssid": "\ud800"}, {"password": "a\udfffb"}, {"mode": "\ud800"}],
    ids=["ssid", "password", "mode"],
)
def test_config_with_text_not_valid_unicode_is_rejected_without_writing(
    *, stack: Stack, settings: dict[str, str]
) -> None:
    stack.reset()

    response = stack.api.post(
        "/api/wireless/config", json={"band": "band6", "settings": settings}
    )

    assert response.status_code == 422
    assert stack.router.arrivals == []
    assert stack.app.calls == []

"""
Wi-Fi 設定の書き込み: POST /api/wireless (ON / OFF) と POST /api/wireless/config。

どちらも Deco アプリと同じ TMP の経路 (wlan の write の opcode) で書き込む。
"""

from base64 import b64encode
from itertools import product
from typing import Any, cast

import pytest
from hypothesis import example, given, strategies as st

from tests.harness import Stack
from tests.strategies import as_json, json_values, merged

# Deco アプリが Wi-Fi の設定の読み取りと書き込みに送る opcode。
_WLAN_READ = 0x4009
_WLAN_WRITE = 0x4008
_BANDS = ["band2_4", "band5_1", "band6"]
_NETWORKS = ["host", "guest"]
# band の radio に書く設定と、TMP での名前。
_RADIO_FIELDS = {"channel": "channel", "channel_width": "htmode", "mode": "hwmode"}


@st.composite
def _with_a_value(draw: st.DrawFn, *, fields: dict[str, Any]) -> dict[str, Any]:
    """値のあるフィールドを 1 つ以上含み、ほかのフィールドは省略・null・値のどれかにする。"""
    required = draw(st.sampled_from(list(fields)))
    return draw(
        st.fixed_dictionaries(
            {required: fields[required]},
            optional={k: st.none() | v for k, v in fields.items() if k != required},
        )
    )


# host と guest のどちらにも書ける設定。
_SETTING_FIELDS: dict[str, Any] = {
    "enable": st.booleans(),
    "ssid": st.text(),
    "password": st.text(),
    "enable_hide_ssid": st.booleans(),
    "channel": st.integers(),
    "channel_width": st.text(),
    "mode": st.text(),
    "encryption_mode": st.text(),
    "enable_wpa3": st.booleans(),
}
_BANDWIDTH_FIELDS: dict[str, Any] = {
    "enable": st.booleans(),
    "downstream_bandwidth": st.integers(min_value=0),
    "upstream_bandwidth": st.integers(min_value=0),
}
# guest にだけ書ける設定。
_GUEST_ONLY_FIELDS: dict[str, Any] = {
    "host_isolation": st.booleans(),
    "access_duration": st.integers(min_value=-1),
    "bandwidth_limit": _with_a_value(fields=_BANDWIDTH_FIELDS),
}


def _b64(text: str) -> str:
    return b64encode(text.encode()).decode()


def _wlan_writes(stack: Stack) -> list[Any]:
    return [c.body.get("params") for c in stack.app.calls if c.opcode == _WLAN_WRITE]


def _without_null(settings: dict[str, Any]) -> dict[str, Any]:
    """null のフィールドを、入れ子の object の中まで除く。"""
    return {
        k: _without_null(cast("dict[str, Any]", v)) if isinstance(v, dict) else v
        for k, v in settings.items()
        if v is not None
    }


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
        {"network": "host", "enable": True},
        {"network": "guest"},
    ],
    ids=[
        "unknown-band",
        "unknown-network",
        "no-enable",
        "enable-not-bool",
        "enable-null",
        "no-band",
        "no-band-for-host",
        "no-band-and-no-enable",
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


# BE85 の wlan の read の形。2.4 / 5 GHz の guest は is_separate を持たず、6 GHz の guest だけが true を持つ。
_WLAN_WITH_SEPARATE_6GHZ_GUEST = {
    "band2_4": {
        "host": {"enable": True},
        "guest": {"enable": True, "encryption_mode": "wpa2"},
        "radio": {"channel": "0"},
    },
    "band5_1": {
        "host": {"enable": True},
        "guest": {"enable": True, "encryption_mode": "wpa2"},
        "radio": {"channel": "0"},
    },
    "band6": {
        "host": {"enable": True},
        "guest": {"enable": False, "encryption_mode": "owe", "is_separate": True},
        "radio": {"channel": "0"},
    },
}


@pytest.mark.parametrize("enable", [True, False])
def test_guest_toggle_without_band_switches_guests_not_marked_separate_in_one_write(
    *, stack: Stack, enable: bool
) -> None:
    stack.reset()
    stack.app.respond_result(_WLAN_READ, result=_WLAN_WITH_SEPARATE_6GHZ_GUEST)
    stack.app.respond_result(_WLAN_WRITE, result=None)

    response = stack.api.post(
        "/api/wireless", json={"network": "guest", "enable": enable}
    )

    assert response.status_code == 200
    written = {
        "band2_4": {"guest": {"enable": enable}},
        "band5_1": {"guest": {"enable": enable}},
    }
    assert as_json(response.json()) == as_json({"updated": written})
    assert as_json(_wlan_writes(stack)) == as_json([written])


def test_guest_toggle_without_band_includes_guest_explicitly_not_separate(
    stack: Stack,
) -> None:
    stack.reset()
    wlan = {"band5_1": {"guest": {"enable": True, "is_separate": False}}}
    stack.app.respond_result(_WLAN_READ, result=wlan)
    stack.app.respond_result(_WLAN_WRITE, result=None)

    response = stack.api.post(
        "/api/wireless", json={"network": "guest", "enable": False}
    )

    assert response.status_code == 200
    assert as_json(_wlan_writes(stack)) == as_json(
        [{"band5_1": {"guest": {"enable": False}}}]
    )


@pytest.mark.parametrize(
    "wlan",
    [
        {"band6": {"guest": {"enable": False, "is_separate": True}}},
        {"band2_4": {"host": {"enable": True}}, "band5_1": {"guest": []}},
        {},
    ],
    ids=["only-separate-guest", "no-guest", "no-band"],
)
def test_guest_toggle_without_band_is_404_without_writing_when_no_guest_to_switch(
    *, stack: Stack, wlan: dict[str, Any]
) -> None:
    stack.reset()
    stack.app.respond_result(_WLAN_READ, result=wlan)

    response = stack.api.post(
        "/api/wireless", json={"network": "guest", "enable": True}
    )

    assert response.status_code == 404
    assert _wlan_writes(stack) == []


_VALID_GUEST_BAND = {"band2_4": {"guest": {"enable": True}}}


@pytest.mark.parametrize(
    "envelope",
    [
        {"error_code": 0, "result": []},
        {"error_code": 0, "result": _VALID_GUEST_BAND | {"band5_1": "on"}},
        {"error_code": 0, "result": _VALID_GUEST_BAND | {"band5_1": {"guest": "on"}}},
        {"error_code": 0, "result": _VALID_GUEST_BAND | {"band5_1": {"guest": None}}},
        {
            "error_code": 0,
            "result": _VALID_GUEST_BAND | {"band6": {"guest": {"is_separate": "true"}}},
        },
        {
            "error_code": 0,
            "result": _VALID_GUEST_BAND | {"band6": {"guest": {"is_separate": 1}}},
        },
        {"error_code": -40101, "msg": "rejected"},
    ],
    ids=[
        "not-an-object",
        "band-not-an-object",
        "guest-not-an-object",
        "guest-null",
        "is-separate-string",
        "is-separate-number",
        "read-rejected",
    ],
)
def test_guest_toggle_without_band_is_502_without_writing_when_wlan_read_is_unusable(
    *, stack: Stack, envelope: dict[str, Any]
) -> None:
    """一部の band のゲストだけを切り替えて成功を返さないよう、どの band にも書き込まない。"""
    stack.reset()
    stack.app.respond(_WLAN_READ, envelope=envelope)

    response = stack.api.post(
        "/api/wireless", json={"network": "guest", "enable": False}
    )

    assert response.status_code == 502
    assert _wlan_writes(stack) == []


def _stored(key: str, *, value: Any) -> Any:
    """network の設定 1 つを、ルーターが保持する形 (TMP に送る形) にする。"""
    if key in {"ssid", "password"}:
        return _b64(value)
    if key == "access_duration":
        return str(value)
    if key == "bandwidth_limit":
        return {k: v if k == "enable" else str(v) for k, v in value.items()}
    return value


@st.composite
def _config_target_and_settings(
    draw: st.DrawFn,
) -> tuple[str | None, dict[str, Any]]:
    """network (省略は host) と、その network に書ける設定。guest だけの設定は guest のときに選ぶ。"""
    network = draw(st.none() | st.sampled_from(_NETWORKS))
    fields = _SETTING_FIELDS | (_GUEST_ONLY_FIELDS if network == "guest" else {})
    return network, draw(_with_a_value(fields=fields))


@given(
    band=st.sampled_from(_BANDS),
    target_and_settings=_config_target_and_settings(),
    result=json_values,
)
def test_config_writes_given_fields_in_the_form_the_router_stores(
    *,
    stack: Stack,
    band: str,
    target_and_settings: tuple[str | None, dict[str, Any]],
    result: Any,
) -> None:
    """
    null のフィールドは書き込まず、network の省略は host を指す。

    ssid / password は base64 にし、TMP が文字列で持つ数値 (access_duration・帯域の上限) は文字列にする。
    channel / channel_width / mode は network によらず band の radio に TMP の名前で書く。
    """
    network, settings = target_and_settings
    stack.reset()
    stack.app.respond_result(_WLAN_WRITE, result=result)
    body = {"band": band, "settings": settings}
    if network is not None:
        body["network"] = network

    response = stack.api.post("/api/wireless/config", json=body)

    assert response.status_code == 200
    target = network or "host"
    given_fields = _without_null(settings)

    network_sent = {
        k: _stored(k, value=v)
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


def test_config_sends_guest_and_radio_settings_in_the_deco_app_form(
    stack: Stack,
) -> None:
    stack.reset()
    stack.app.respond_result(_WLAN_WRITE, result=None)
    settings = {
        "ssid": "Guest",
        "channel": 6,
        "channel_width": "HT40",
        "mode": "11ng",
        "access_duration": -1,
        "bandwidth_limit": {"enable": True, "downstream_bandwidth": 102400},
    }

    response = stack.api.post(
        "/api/wireless/config",
        json={"band": "band2_4", "network": "guest", "settings": settings},
    )

    assert response.status_code == 200
    assert as_json(_wlan_writes(stack)) == as_json(
        [
            {
                "band2_4": {
                    "guest": {
                        "ssid": "R3Vlc3Q=",
                        "access_duration": "-1",
                        "bandwidth_limit": {
                            "enable": True,
                            "downstream_bandwidth": "102400",
                        },
                    },
                    "radio": {"channel": "6", "htmode": "HT40", "hwmode": "11ng"},
                }
            }
        ]
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
    # guest だけの設定を host (network の省略を含む) に指定する
    st.fixed_dictionaries(
        {
            "band": st.sampled_from(_BANDS),
            "settings": _with_a_value(fields=_GUEST_ONLY_FIELDS),
        },
        optional={"network": st.just("host")},
    ),
    # 帯域制限に書き込むフィールドが無い
    st.fixed_dictionaries(
        {
            "band": st.sampled_from(_BANDS),
            "network": st.just("guest"),
            "settings": st.fixed_dictionaries(
                {
                    "bandwidth_limit": st.fixed_dictionaries(
                        {}, optional=dict.fromkeys(_BANDWIDTH_FIELDS, st.none())
                    )
                }
            ),
        }
    ),
    # 範囲外の値 (無期限の -1 より小さい期間、負の帯域)
    st.fixed_dictionaries(
        {
            "band": st.sampled_from(_BANDS),
            "network": st.just("guest"),
            "settings": st.one_of(
                st.fixed_dictionaries({"access_duration": st.integers(max_value=-2)}),
                st.fixed_dictionaries(
                    {
                        "bandwidth_limit": st.sampled_from(
                            ["downstream_bandwidth", "upstream_bandwidth"]
                        ).flatmap(
                            lambda k: st.fixed_dictionaries(
                                {k: st.integers(max_value=-1)}
                            )
                        )
                    }
                ),
            ),
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
@example(body={"band": "band6", "settings": {"host_isolation": True}})
@example(body={"band": "band6", "network": "host", "settings": {"access_duration": -1}})
@example(
    body={"band": "band6", "network": "guest", "settings": {"bandwidth_limit": {}}}
)
@example(
    body={"band": "band6", "network": "guest", "settings": {"access_duration": -2}}
)
@example(
    body={
        "band": "band6",
        "network": "guest",
        "settings": {"bandwidth_limit": {"upstream_bandwidth": -1}},
    }
)
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

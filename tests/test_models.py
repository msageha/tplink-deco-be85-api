from base64 import b64decode

import pytest
from pydantic import ValidationError

from api.schemas import (
    Operation,
    RawRequest,
    WifiBand,
    WifiNetwork,
    WirelessConfigUpdate,
    WlanBandToggle,
)


def test_raw_request_defaults_and_payload():
    r = RawRequest(path="admin/network?form=internet")
    assert r.operation is Operation.read
    assert r.to_payload() == {"operation": "read"}


def test_raw_request_strips_leading_slash_and_keeps_params():
    r = RawRequest(
        path="/admin/client?form=client_list",
        operation="read",
        params={"device_mac": "default"},
    )
    assert r.path == "admin/client?form=client_list"
    assert r.to_payload() == {"operation": "read", "params": {"device_mac": "default"}}


@pytest.mark.parametrize(
    "bad", ["http://evil/x", "admin/../etc", "admin/x?form=y&z=1", "ad min/x"]
)
def test_raw_request_rejects_unsafe_paths(bad):
    with pytest.raises(ValidationError):
        RawRequest(path=bad)


def test_raw_request_rejects_unknown_operation():
    with pytest.raises(ValidationError):
        RawRequest(path="admin/x?form=y", operation="destroy")


def test_wlan_toggle_enums():
    t = WlanBandToggle(band="band5_1", network="guest", enable=False)
    assert t.band is WifiBand.band5_1
    assert t.network is WifiNetwork.guest

    with pytest.raises(ValidationError):
        WlanBandToggle(band="band9", network="host", enable=True)


def test_wireless_config_base64_and_shape():
    u = WirelessConfigUpdate(
        band="band5_1",
        network="guest",
        settings={"enable": True, "ssid": "My Guest", "password": "p@ss"},
    )
    params = u.to_params()
    guest = params["band5_1"]["guest"]
    assert guest["enable"] is True
    assert b64decode(guest["ssid"]).decode() == "My Guest"
    assert b64decode(guest["password"]).decode() == "p@ss"
    # password masked in the echo summary
    assert u.summary()["band5_1"]["guest"]["password"] == "***"


def test_wireless_config_excludes_unset_fields():
    u = WirelessConfigUpdate(band="band2_4", settings={"enable": False})
    assert u.to_params() == {"band2_4": {"host": {"enable": False}}}


def test_wireless_config_requires_a_field():
    with pytest.raises(ValidationError):
        WirelessConfigUpdate(band="band6", settings={})


def test_wireless_config_rejects_unknown_field():
    with pytest.raises(ValidationError):
        WirelessConfigUpdate(band="band6", settings={"foo": "bar"})

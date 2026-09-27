import pytest
from pydantic import ValidationError

from api.models import (
    Operation,
    RawRequest,
    WifiBand,
    WifiNetwork,
    WirelessConfigUpdate,
    WlanBandToggle,
)


def test_raw_request_defaults() -> None:
    r = RawRequest(path="admin/network?form=internet")
    assert r.operation is Operation.read
    assert r.params is None


def test_raw_request_strips_leading_slash() -> None:
    r = RawRequest(path="/admin/client?form=client_list", operation="read")
    assert r.path == "admin/client?form=client_list"


@pytest.mark.parametrize(
    "bad", ["http://evil/x", "admin/../etc", "admin/x?form=y&z=1", "ad min/x"]
)
def test_raw_request_rejects_unsafe_paths(bad: str) -> None:
    with pytest.raises(ValidationError):
        RawRequest(path=bad)


def test_raw_request_rejects_unknown_operation() -> None:
    with pytest.raises(ValidationError):
        RawRequest(path="admin/x?form=y", operation="destroy")


def test_wlan_toggle_enums() -> None:
    t = WlanBandToggle(band="band5_1", network="guest", enable=False)
    assert t.band is WifiBand.band5_1
    assert t.network is WifiNetwork.guest

    with pytest.raises(ValidationError):
        WlanBandToggle(band="band9", network="host", enable=True)


def test_wireless_config_requires_a_field() -> None:
    with pytest.raises(ValidationError):
        WirelessConfigUpdate(band="band6", settings={})


def test_wireless_config_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        WirelessConfigUpdate(band="band6", settings={"foo": "bar"})

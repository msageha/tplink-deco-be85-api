from base64 import b64decode
from typing import Any

import pytest

from deco import DecoClient


def test_set_wlan_encodes_ssid_and_password(monkeypatch: pytest.MonkeyPatch) -> None:
    client = DecoClient("http://deco.test", "pw")
    sent: dict[str, Any] = {}

    def fake_request(
        path: str, operation: str = "read", params: dict[str, Any] | None = None
    ) -> Any:
        sent.update(path=path, operation=operation, params=params)
        return {}

    monkeypatch.setattr(client, "request", fake_request)
    client.set_wlan(
        "band5_1", "guest", {"enable": True, "ssid": "My Guest", "password": "p@ss"}
    )

    assert sent["path"] == "admin/wireless?form=wlan"
    assert sent["operation"] == "write"
    guest = sent["params"]["band5_1"]["guest"]
    assert guest["enable"] is True
    assert b64decode(guest["ssid"]).decode() == "My Guest"
    assert b64decode(guest["password"]).decode() == "p@ss"

import re
from base64 import b64decode
from hashlib import md5
from json import dumps, loads
from typing import Any, cast

import requests

from .crypto import DecoEncryption
from .exceptions import DecoAuthError, DecoConnectionError, DecoError

_SYSAUTH_RE = re.compile(r"sysauth=([^;]+)")


def decode_name(value: Any) -> Any:
    """Client/Deco names come back base64 encoded."""
    if not isinstance(value, str):
        return value
    try:
        return b64decode(value).decode()
    except Exception:
        return value


class DecoClient:
    def __init__(
        self,
        host: str,
        password: str,
        *,
        account: str = "admin",
        verify_ssl: bool = False,
        timeout: int = 30,
    ) -> None:
        self.host = host.rstrip("/")
        # The local API ignores the TP-Link account email; the request signature
        # hash is md5(<local account> + password) and the account is "admin".
        self.account = account
        self.password = password
        self.verify_ssl = verify_ssl
        self.timeout = timeout

        self._enc: DecoEncryption | None = None
        self._stok = ""
        self._sysauth = ""
        self._pwd_nn = ""
        self._pwd_ee = ""
        self._sig_nn = ""
        self._sig_ee = ""
        self._seq = 0
        self._logged = False

    # ------------------------------------------------------------------ #
    # session
    # ------------------------------------------------------------------ #
    @property
    def logged_in(self) -> bool:
        return self._logged

    def _login_url(self, form: str) -> str:
        return f"{self.host}/cgi-bin/luci/;stok=/login?form={form}"

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        try:
            return requests.post(
                url, timeout=self.timeout, verify=self.verify_ssl, **kwargs
            )
        except requests.exceptions.RequestException as err:
            raise DecoConnectionError(
                f"Cannot reach Deco at {self.host}: {err}"
            ) from err

    def _request_keys(self) -> None:
        resp = self._post(self._login_url("keys"), params={"operation": "read"})
        try:
            self._pwd_nn, self._pwd_ee = resp.json()["result"]["password"]
        except Exception as err:
            raise DecoAuthError(
                f"Unexpected response requesting password key: {resp.text}"
            ) from err

    def _request_auth(self) -> None:
        resp = self._post(self._login_url("auth"), params={"operation": "read"})
        try:
            result = resp.json()["result"]
            self._seq = result["seq"]
            self._sig_nn, self._sig_ee = result["key"]
        except Exception as err:
            raise DecoAuthError(
                f"Unexpected response requesting auth key: {resp.text}"
            ) from err

    def _prepare(self, data: str, *, is_login: bool = False) -> dict[str, str]:
        assert self._enc is not None
        encrypted = self._enc.aes_encrypt(data)
        cred_hash = md5((self.account + self.password).encode()).hexdigest()
        sign = self._enc.signature(
            int(self._seq) + len(encrypted),
            self._sig_nn,
            self._sig_ee,
            cred_hash,
            is_login=is_login,
        )
        return {"sign": sign, "data": encrypted}

    def _decrypt(self, envelope: dict[str, Any]) -> dict[str, Any]:
        assert self._enc is not None
        if not envelope.get("data"):
            return envelope
        return loads(self._enc.aes_decrypt(envelope["data"]))

    def login(self) -> None:
        self._logged = False
        self._enc = DecoEncryption()
        self._request_keys()
        self._request_auth()

        crypted_pwd = DecoEncryption.rsa_encrypt(
            self.password, self._pwd_nn, self._pwd_ee
        )
        body = self._prepare(
            dumps({"params": {"password": crypted_pwd}, "operation": "login"}),
            is_login=True,
        )
        resp = self._post(
            self._login_url("login"),
            data=body,
            headers={"Content-Type": "application/json"},
        )

        try:
            payload = self._decrypt(resp.json())
            self._stok = payload["result"]["stok"]
            match = _SYSAUTH_RE.search(resp.headers.get("set-cookie", ""))
            if not match:
                raise DecoAuthError(
                    "Login succeeded but no sysauth cookie was returned"
                )
            self._sysauth = match.group(1)
            self._logged = True
        except DecoAuthError:
            raise
        except Exception as err:
            raise DecoAuthError(
                f"Login failed (check USERNAME/PASSWORD). Response: {resp.text}"
            ) from err

    def logout(self) -> None:
        if self._logged:
            try:
                self.request(
                    "admin/system?form=logout",
                    {"operation": "logout"},
                    ignore_response=True,
                )
            except DecoError:
                pass
        self._stok = ""
        self._sysauth = ""
        self._logged = False

    # ------------------------------------------------------------------ #
    # generic request
    # ------------------------------------------------------------------ #
    @staticmethod
    def _payload_str(payload: dict[str, Any] | str) -> str:
        return payload if isinstance(payload, str) else dumps(payload)

    def _send(self, path: str, data: str, *, _retry: bool = True) -> dict[str, Any]:
        """POST a serialized payload and return the decrypted response envelope.

        Handles both response styles seen on this firmware: the Deco-style
        ``{"error_code": .., "result": ..}`` and the ``{"success": .., "data": ..}``
        style used by a few system endpoints. A stale session (HTML login page or
        HTTP 401/403) triggers one re-login + retry.
        """
        if not self._logged:
            self.login()

        url = f"{self.host}/cgi-bin/luci/;stok={self._stok}/{path}"
        resp = self._post(
            url,
            data=self._prepare(data),
            headers={"Content-Type": "application/json"},
            cookies={"sysauth": self._sysauth},
        )

        try:
            envelope = resp.json()
        except ValueError:
            if _retry:
                self.login()
                return self._send(path, data, _retry=False)
            raise DecoError(f"Non-JSON response for {path}: {resp.text[:200]}")

        if resp.status_code in (401, 403) and _retry:
            self.login()
            return self._send(path, data, _retry=False)

        return self._decrypt(envelope)

    def request(
        self,
        path: str,
        payload: dict[str, Any] | str,
        *,
        ignore_response: bool = False,
    ) -> Any:
        """Send a request and return its useful body (``result`` or ``data``).

        Raises :class:`DecoError` when the router reports a failure.
        """
        data = self._payload_str(payload)

        if ignore_response:
            if not self._logged:
                self.login()
            url = f"{self.host}/cgi-bin/luci/;stok={self._stok}/{path}"
            self._post(
                url,
                data=self._prepare(data),
                headers={"Content-Type": "application/json"},
                cookies={"sysauth": self._sysauth},
            )
            return None

        decrypted = self._send(path, data)
        if decrypted.get("error_code") == 0:
            return decrypted.get("result")
        if decrypted.get("success") is True:
            return decrypted.get("data")
        raise DecoError(
            f"Deco request '{path}' failed: {decrypted}",
            error_code=decrypted.get("error_code"),
        )

    def raw(self, path: str, payload: dict[str, Any] | str) -> dict[str, Any]:
        """Hit an arbitrary endpoint and return the full decrypted envelope
        (does not raise on router-reported errors)."""
        return self._send(path, self._payload_str(payload))

    # ------------------------------------------------------------------ #
    # high level endpoints (mirrors the Deco app)
    # ------------------------------------------------------------------ #
    def get_device_list(self) -> list[dict[str, Any]]:
        result = (
            self.request("admin/device?form=device_list", {"operation": "read"}) or {}
        )
        return result.get("device_list", [])

    def get_client_list(self) -> list[dict[str, Any]]:
        result = (
            self.request(
                "admin/client?form=client_list",
                {"operation": "read", "params": {"device_mac": "default"}},
            )
            or {}
        )
        return result.get("client_list", [])

    def get_wan(self) -> dict[str, Any]:
        return self.request("admin/network?form=wan_ipv4", {"operation": "read"}) or {}

    def get_lan(self) -> dict[str, Any]:
        return (
            self.request(
                "admin/network?form=lan_ip",
                {"operation": "read", "params": {"device_mac": "default"}},
            )
            or {}
        )

    def get_ipv6(self) -> dict[str, Any]:
        return (
            self.request(
                "admin/network?form=ipv6",
                {"operation": "read", "params": {"device_mac": "default"}},
            )
            or {}
        )

    def get_internet(self) -> dict[str, Any]:
        return self.request("admin/network?form=internet", {"operation": "read"}) or {}

    def get_performance(self) -> dict[str, Any]:
        return (
            self.request("admin/network?form=performance", {"operation": "read"}) or {}
        )

    def get_wlan(self) -> dict[str, Any]:
        return self.request("admin/wireless?form=wlan", {"operation": "read"}) or {}

    def set_wlan(self, params: dict[str, Any]) -> Any:
        return self.request(
            "admin/wireless?form=wlan", {"operation": "write", "params": params}
        )

    def get_mode(self) -> dict[str, Any]:
        return self.request("admin/device?form=mode", {"operation": "read"}) or {}

    def get_time_settings(self) -> dict[str, Any]:
        return (
            self.request("admin/device?form=timesetting", {"operation": "read"}) or {}
        )

    def get_mac_clone(self) -> dict[str, Any]:
        return self.request("admin/network?form=mac_clone", {"operation": "read"}) or {}

    def get_wireless_power(self) -> dict[str, Any]:
        return self.request("admin/wireless?form=power", {"operation": "read"}) or {}

    def get_log_types(self) -> Any:
        return self.request("admin/log_export?form=types", {"operation": "read"})

    def get_blocked_clients(self) -> list[dict[str, Any]]:
        result = (
            self.request("admin/client?form=black_list", {"operation": "list"}) or {}
        )
        clients = result.get("client_list", [])
        if isinstance(clients, dict):  # empty list comes back as {}
            clients = list(clients.values())
        return cast("list[dict[str, Any]]", clients)

    def get_cloud_device_info(self) -> dict[str, Any]:
        return (
            self.request(
                "admin/cloud_account?form=get_deviceInfo", {"operation": "read"}
            )
            or {}
        )

    def get_extra_component_info(self) -> dict[str, Any]:
        return (
            self.request("admin/web?form=extra_component_info", {"operation": "get"})
            or {}
        )

    def get_switch_list(self) -> dict[str, Any]:
        return (
            self.request(
                "admin/component_control?form=switch_list", {"operation": "read"}
            )
            or {}
        )

    def reboot(self, macs: list[str]) -> Any:
        return self.request(
            "admin/device?form=system",
            {"operation": "reboot", "params": {"mac_list": [{"mac": m} for m in macs]}},
        )

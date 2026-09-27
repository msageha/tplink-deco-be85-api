"""TP-Link Deco のローカル (luci) API クライアント。"""

import json
import re
from base64 import b64decode, b64encode
from hashlib import md5
from typing import Any

import requests

from .crypto import SessionCipher, rsa_encrypt
from .exceptions import DecoAuthError, DecoConnectionError, DecoError

_SYSAUTH_RE = re.compile(r"sysauth=([^;]+)")


def _decode_names(items: list[Any], *keys: str) -> list[Any]:
    """ルーターが base64 で返す名前フィールドを平文に戻す。base64 でない値はそのまま残す。"""
    for item in items:
        for key in keys:
            value = item.get(key)
            if not isinstance(value, str):
                continue
            try:
                item[key] = b64decode(value, validate=True).decode()
            except ValueError:
                pass
    return items


def _decrypt_envelope(
    cipher: SessionCipher, envelope: dict[str, Any]
) -> dict[str, Any]:
    """`data` に暗号文があれば復号した中身を、無ければ envelope をそのまま返す。"""
    data = envelope.get("data")
    if isinstance(data, str) and data:
        return json.loads(cipher.decrypt(data))
    return envelope


class DecoClient:
    """1 台の Deco に対するログインセッションを保持する同期クライアント。並行利用はできない。"""

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
        self._password = password
        # 署名に使う認証 hash。ローカル API は TP-Link ID ではなくローカル管理アカウント名 (通常 admin) で検証する。
        self._cred_hash = md5(
            (account + password).encode(), usedforsecurity=False
        ).hexdigest()
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._cipher: SessionCipher | None = None
        self._stok = ""
        self._sysauth = ""

    @property
    def logged_in(self) -> bool:
        return self._cipher is not None

    def _post(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> requests.Response:
        try:
            return requests.post(
                f"{self.host}/cgi-bin/luci/;stok={self._stok}/{path}",
                params=params,
                data=data,
                headers={"Content-Type": "application/json"},
                cookies={"sysauth": self._sysauth} if self._sysauth else None,
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as err:
            raise DecoConnectionError(
                f"Cannot reach Deco at {self.host}: {err}"
            ) from err

    def _login_form(self, form: str) -> dict[str, Any]:
        """ログイン前に無認証で読める form (keys / auth) の result を返す。"""
        resp = self._post(f"login?form={form}", params={"operation": "read"})
        try:
            return resp.json()["result"]
        except (ValueError, KeyError, TypeError) as err:
            raise DecoAuthError(
                f"Unexpected response from login form '{form}': {resp.text}"
            ) from err

    def login(self) -> SessionCipher:
        """ログインし、セッション (暗号鍵・stok・sysauth cookie) を作り直す。

        Raises:
            DecoAuthError: パスワード不一致や想定外の応答。
            DecoConnectionError: ルーターへ到達できない。
        """
        self._cipher, self._stok, self._sysauth = None, "", ""
        keys = self._login_form("keys")
        auth = self._login_form("auth")
        try:
            cipher = SessionCipher(auth["key"], int(auth["seq"]), self._cred_hash)
            encrypted_password = rsa_encrypt(self._password, *keys["password"])
        except (KeyError, TypeError, ValueError) as err:
            raise DecoAuthError(f"Unexpected login keys: {keys} / {auth}") from err
        body = {"operation": "login", "params": {"password": encrypted_password}}
        resp = self._post(
            "login?form=login", data=cipher.encrypt(json.dumps(body), login=True)
        )
        try:
            envelope = _decrypt_envelope(cipher, resp.json())
        except (ValueError, AttributeError) as err:
            raise DecoAuthError(
                f"Unexpected login response: {resp.text[:200]}"
            ) from err
        try:
            stok = envelope["result"]["stok"]
        except (KeyError, TypeError) as err:
            # パスワード不一致は error_code -5002 と failureCount / attemptsAllowed で返る。
            raise DecoAuthError(f"Login failed (check PASSWORD): {envelope}") from err
        match = _SYSAUTH_RE.search(resp.headers.get("set-cookie", ""))
        if match is None:
            raise DecoAuthError("Login succeeded but no sysauth cookie was returned")
        self._cipher, self._stok, self._sysauth = cipher, stok, match.group(1)
        return cipher

    def logout(self) -> None:
        """ログアウトする。ルーターに到達できなくてもローカルのセッションは破棄する。"""
        if self._cipher is None:
            return
        try:
            self._post(
                "admin/system?form=logout",
                data=self._cipher.encrypt(json.dumps({"operation": "logout"})),
            )
        except DecoConnectionError:
            pass
        finally:
            self._cipher, self._stok, self._sysauth = None, "", ""

    def _send(
        self, path: str, body: dict[str, Any], *, retry: bool = True
    ) -> dict[str, Any]:
        """本文を暗号化して POST し、復号した envelope を返す。"""
        cipher = self._cipher or self.login()
        resp = self._post(path, data=cipher.encrypt(json.dumps(body)))
        try:
            envelope = resp.json()
        except ValueError:
            envelope = None
        if not isinstance(envelope, dict) or resp.status_code in (401, 403):
            # セッション切れ (ログイン画面の HTML や 401/403)。一度だけ再ログインして再送する。
            if not retry:
                raise DecoError(
                    f"Deco rejected request '{path}' (HTTP {resp.status_code}): "
                    f"{resp.text[:200]}"
                )
            self._cipher = None
            return self._send(path, body, retry=False)
        try:
            return _decrypt_envelope(cipher, envelope)
        except ValueError as err:
            raise DecoError(f"Cannot decrypt response for '{path}'") from err

    def raw(
        self, path: str, operation: str = "read", params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """任意のエンドポイントを呼び、復号した envelope をそのまま返す (ルーター側のエラーでも例外にしない)。"""
        body: dict[str, Any] = {"operation": operation}
        if params is not None:
            body["params"] = params
        return self._send(path, body)

    def request(
        self, path: str, operation: str = "read", params: dict[str, Any] | None = None
    ) -> Any:
        """エンドポイントを呼び、成功応答の本体 (`result` または `data`) を返す。

        Raises:
            DecoError: ルーターがエラーを返したとき (error_code を保持)。
        """
        envelope = self.raw(path, operation, params)
        if envelope.get("error_code") == 0:
            return envelope.get("result")
        if envelope.get("success") is True:
            return envelope.get("data")
        raise DecoError(
            f"Deco request '{path}' failed: {envelope}",
            error_code=envelope.get("error_code"),
        )

    def _read(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request(path, "read", params) or {}

    def get_device_list(self) -> list[dict[str, Any]]:
        devices = self._read("admin/device?form=device_list").get("device_list", [])
        return _decode_names(devices, "nickname", "custom_nickname")

    def get_client_list(self) -> list[dict[str, Any]]:
        result = self._read("admin/client?form=client_list", {"device_mac": "default"})
        return _decode_names(result.get("client_list", []), "name")

    def get_blocked_clients(self) -> list[dict[str, Any]]:
        result = self.request("admin/client?form=black_list", "list") or {}
        clients = result.get("client_list", [])
        if isinstance(clients, dict):  # 空の Lua テーブルは [] ではなく {} で返る
            clients = list(clients.values())
        return _decode_names(clients, "name")

    def get_wan(self) -> dict[str, Any]:
        return self._read("admin/network?form=wan_ipv4")

    def get_internet(self) -> dict[str, Any]:
        return self._read("admin/network?form=internet")

    def get_lan(self) -> dict[str, Any]:
        return self._read("admin/network?form=lan_ip", {"device_mac": "default"})

    def get_ipv6(self) -> dict[str, Any]:
        return self._read("admin/network?form=ipv6", {"device_mac": "default"})

    def get_performance(self) -> dict[str, Any]:
        return self._read("admin/network?form=performance")

    def get_mac_clone(self) -> dict[str, Any]:
        return self._read("admin/network?form=mac_clone")

    def get_wan_mode(self) -> dict[str, Any]:
        return self._read("admin/network?form=wan_mode")

    def get_dhcp_dial(self) -> dict[str, Any]:
        return self._read("admin/network?form=dhcp_dial")

    def get_igmp_setting(self) -> dict[str, Any]:
        return self._read("admin/network?form=igmp_setting")

    def get_fast_xmit_setting(self) -> dict[str, Any]:
        return self._read("admin/network?form=fast_xmit_setting")

    def get_vlan(self) -> dict[str, Any]:
        return self._read("admin/network?form=vlan")

    def get_wlan(self) -> dict[str, Any]:
        return self._read("admin/wireless?form=wlan")

    def set_wlan(self, band: str, network: str, settings: dict[str, Any]) -> Any:
        """band × network (host / guest) の Wi-Fi 設定を書き込む。

        ssid / password はルーターが base64 で保持するので、平文を受け取って変換する。
        """
        encoded = {
            key: b64encode(value.encode()).decode()
            if key in ("ssid", "password")
            else value
            for key, value in settings.items()
        }
        return self.request(
            "admin/wireless?form=wlan", "write", {band: {network: encoded}}
        )

    def get_wireless_power(self) -> dict[str, Any]:
        return self._read("admin/wireless?form=power")

    def get_beamforming(self) -> dict[str, Any]:
        return self._read("admin/wireless?form=beamforming")

    def get_operation_mode(self) -> dict[str, Any]:
        return self._read("admin/wireless?form=operation_mode")

    def get_bridge(self) -> dict[str, Any]:
        return self._read("admin/wireless?form=bridge")

    def get_fast_roaming(self) -> dict[str, Any]:
        return self._read("admin/wireless?form=ieee80211r")

    def get_bandwidth(self) -> dict[str, Any]:
        return self._read("admin/wireless?form=bandwidth_enhance")

    def get_mode(self) -> dict[str, Any]:
        return self._read("admin/device?form=mode")

    def get_time_settings(self) -> dict[str, Any]:
        return self._read("admin/device?form=timesetting")

    def get_speedtest_result(self) -> dict[str, Any]:
        """直近のスピードテスト結果 (計測は行わず、ルーターが保持する値を読むだけ)。"""
        return self._read("admin/device?form=speedtest")

    def get_cloud_device_info(self) -> dict[str, Any]:
        return self._read("admin/cloud_account?form=get_deviceInfo")

    def get_cloud_login_status(self) -> dict[str, Any]:
        return self._read("admin/cloud_account?form=check_login")

    def get_ddns(self) -> dict[str, Any]:
        return self.request("admin/cloud?form=ddns", "get") or {}

    def get_extra_component_info(self) -> dict[str, Any]:
        return self.request("admin/web?form=extra_component_info", "get") or {}

    def get_switch_list(self) -> dict[str, Any]:
        return self._read("admin/component_control?form=switch_list")

    def get_log_types(self) -> Any:
        return self.request("admin/log_export?form=types")

    def get_system_log(self, level: int, index: int, limit: int) -> dict[str, Any]:
        """システムログを 1 ページ分取得する。

        build でルーター側に level (get_log_types の value。8 = ALL) で絞ったスナップショットを
        作らせてから read する。index は 0 始まりのページ番号、limit は 1 ページの件数で、
        応答の totalNum はその limit での総ページ数。
        """
        self.request("admin/log_export?form=feedback_log", "build", {"level": level})
        return self._read(
            "admin/log_export?form=feedback_log", {"index": index, "limit": limit}
        )

    def check_firmware(self) -> list[dict[str, Any]]:
        """TP-Link cloud に各ノードの最新ファームウェアを問い合わせる (数秒かかる)。"""
        result = self.request("admin/cloud?form=firmware_status", "check") or {}
        return _decode_names(result.get("fw_list", []), "new_version")

    def reboot(self, macs: list[str]) -> Any:
        return self.request(
            "admin/device?form=system",
            "reboot",
            {"mac_list": [{"mac": mac} for mac in macs]},
        )

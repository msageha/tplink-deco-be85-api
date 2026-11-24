"""TP-Link Deco のローカル (luci) API クライアント。"""

import json
from base64 import b64decode, b64encode
from contextlib import suppress
from hashlib import md5
from http import HTTPStatus
from typing import Any, cast

import requests

from deco.crypto import SessionCipher, rsa_encrypt
from deco.exceptions import DecoAuthError, DecoConnectionError, DecoError


def _named_items(
    result: dict[str, Any], *, key: str, names: tuple[str, ...]
) -> list[dict[str, Any]]:
    """
    result の一覧 (key) を返し、ルーターが base64 で返す名前フィールドを平文に戻す。

    一覧は配列のほか、キー付きの Lua テーブル (空なら {}) でも返る。base64 でない名前はそのまま残す。

    Raises:
        DecoError: 一覧や項目が JSON の配列・object でない。
    """
    raw: Any = result.get(key)
    if raw is None:
        raw = []
    if isinstance(raw, dict):
        raw = list(cast("dict[str, Any]", raw).values())
    if not isinstance(raw, list):
        raise DecoError(f"Unexpected '{key}' in Deco response: not a list")
    items: list[dict[str, Any]] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            raise DecoError(
                f"Unexpected '{key}' in Deco response: not a list of objects"
            )
        item = cast("dict[str, Any]", entry)
        for name in names:
            value = item.get(name)
            if isinstance(value, str):
                with suppress(ValueError):
                    item[name] = b64decode(value, validate=True).decode()
        items.append(item)
    return items


def _numeric_code(envelope: dict[str, Any]) -> int | None:
    """envelope の error_code。数値でなければ None (bool は int の派生で False == 0 になるので除く)。"""
    error_code = envelope.get("error_code")
    return error_code if type(error_code) is int else None


def _excerpt(resp: requests.Response) -> str:
    """
    エラーメッセージに載せる応答本文の先頭。

    本文は応答が宣言する文字コードで読まれ、孤立した surrogate を含みうるので、bytes の repr
    (ASCII だけ) で書く。
    """
    return repr(resp.content[:200])


def _json_safe[T](value: T) -> T:
    """
    ルーターの JSON から読んだ値を、API の応答に載せられることを確かめて返す。

    JSON の構文は孤立した surrogate (U+D800 など) や NaN・Infinity も通すが、前者は UTF-8 で
    表せず、後者は JSON の値ではないので、API の応答やエラーメッセージに載せられない。

    Raises:
        ValueError: 孤立した surrogate・NaN・Infinity を含む。
    """
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    return value


def _decrypt_envelope(
    envelope: dict[str, Any], *, cipher: SessionCipher
) -> dict[str, Any]:
    """
    `data` に暗号文があれば復号した中身を、無ければ envelope をそのまま返す。

    Raises:
        ValueError: 復号できない、中身が JSON の object でない、または応答に載せられない値を含む。
    """
    data = envelope.get("data")
    decoded: Any = (
        json.loads(cipher.decrypt(data)) if isinstance(data, str) and data else envelope
    )
    if not isinstance(decoded, dict):
        raise ValueError("Deco envelope is not a JSON object")
    return _json_safe(cast("dict[str, Any]", decoded))


class DecoClient:
    """1 台の Deco に対するログインセッションを保持する同期クライアント。並行利用はできない。"""

    def __init__(
        self,
        host: str,
        *,
        password: str,
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
        # 例外の文字列には stok を含む URL が入るので、メッセージには型名だけを載せる。
        except (requests.ConnectionError, requests.Timeout) as err:
            raise DecoConnectionError(
                f"Cannot reach Deco at {self.host}: {type(err).__name__}"
            ) from err
        except requests.RequestException as err:
            raise DecoError(
                f"Invalid HTTP response from Deco at {self.host}: {type(err).__name__}"
            ) from err

    def _login_form(self, form: str) -> dict[str, Any]:
        """ログイン前に無認証で読める form (keys / auth) の result を返す。"""
        resp = self._post(f"login?form={form}", params={"operation": "read"})
        try:
            resp.raise_for_status()
            return _json_safe(resp.json()["result"])
        except (ValueError, KeyError, TypeError, requests.HTTPError) as err:
            raise DecoAuthError(
                f"Unexpected response from login form '{form}': {_excerpt(resp)}"
            ) from err

    def login(self) -> SessionCipher:
        """
        ログインし、セッション (暗号鍵・stok・sysauth cookie) を作り直す。

        Raises:
            DecoAuthError: パスワード不一致や想定外の応答。
            DecoConnectionError: ルーターへ到達できない。
        """
        self._cipher, self._stok, self._sysauth = None, "", ""
        keys = self._login_form("keys")
        auth = self._login_form("auth")
        try:
            cipher = SessionCipher(
                sign_key=auth["key"], seq=int(auth["seq"]), cred_hash=self._cred_hash
            )
            encrypted_password = rsa_encrypt(
                self._password, public_key=keys["password"]
            )
            body = {"operation": "login", "params": {"password": encrypted_password}}
            # 鍵が短く署名のブロックを暗号化できないときも、ここで ValueError になる。
            form = cipher.encrypt(json.dumps(body), login=True)
        except (KeyError, TypeError, ValueError) as err:
            raise DecoAuthError(f"Unexpected login keys: {keys} / {auth}") from err
        resp = self._post("login?form=login", data=form)
        try:
            resp.raise_for_status()
            envelope = _decrypt_envelope(resp.json(), cipher=cipher)
        except (ValueError, AttributeError, requests.HTTPError) as err:
            raise DecoAuthError(f"Unexpected login response: {_excerpt(resp)}") from err
        result: Any = envelope.get("result")
        stok: Any = (
            cast("dict[str, Any]", result).get("stok")
            if isinstance(result, dict)
            else None
        )
        if (
            ("error_code" in envelope and _numeric_code(envelope) != 0)
            or not isinstance(stok, str)
            or not stok
        ):
            # パスワード不一致は error_code -5002 と failureCount / attemptsAllowed で返る。
            raise DecoAuthError(f"Login failed (check PASSWORD): {envelope}")
        sysauth = resp.cookies.get("sysauth")
        if not sysauth:
            raise DecoAuthError("Login succeeded but no sysauth cookie was returned")
        self._cipher, self._stok, self._sysauth = cipher, stok, sysauth
        return cipher

    def logout(self) -> None:
        """ログアウトする。ルーターに到達できなくてもローカルのセッションは破棄する。"""
        if self._cipher is None:
            return
        try:
            # 終了処理からも呼ばれるので、ルーターの不正な応答でも例外にしない。
            with suppress(DecoError):
                self._post(
                    "admin/system?form=logout",
                    data=self._cipher.encrypt(json.dumps({"operation": "logout"})),
                )
        finally:
            self._cipher, self._stok, self._sysauth = None, "", ""

    def _send(
        self, path: str, *, body: dict[str, Any], retry: bool = True
    ) -> dict[str, Any]:
        """本文を暗号化して POST し、復号した envelope を返す。"""
        cipher = self._cipher or self.login()
        resp = self._post(path, data=cipher.encrypt(json.dumps(body)))
        try:
            envelope = resp.json()
        except ValueError:
            envelope = None
        # セッション切れは 401/403 か、JSON でないログイン画面の HTML で返る。ほかのクライアントのログインで
        # セッションが無効になったときは、HTTP 200 で暗号文の空な `{"data": ""}` だけが返る (BE85 の FW 1.2.4 で確認)。
        # ほかの応答 (Content-Type が HTML でも本文が JSON のもの、HTML でない壊れた本文) は操作が適用されたか
        # 分からず、再送すると write を二重に適用しうるので、再送しない。
        expired = resp.status_code in {
            HTTPStatus.UNAUTHORIZED,
            HTTPStatus.FORBIDDEN,
        } or (
            resp.status_code == HTTPStatus.OK
            and (
                (
                    envelope is None
                    and resp.headers.get("content-type", "").startswith("text/html")
                )
                or envelope == {"data": ""}
            )
        )
        if expired:
            # 一度だけ再ログインして再送する。
            if not retry:
                raise DecoError(
                    f"Deco rejected request '{path}' (HTTP {resp.status_code}): "
                    f"{_excerpt(resp)}",
                    error_code=_numeric_code(cast("dict[str, Any]", envelope))
                    if isinstance(envelope, dict)
                    else None,
                )
            self._cipher = None
            return self._send(path, body=body, retry=False)
        if not isinstance(envelope, dict):
            raise DecoError(
                f"Deco returned HTTP {resp.status_code} without an envelope for '{path}'"
            )
        try:
            # isinstance は dict の型引数を絞れない。JSON object のキーは常に str になる。
            decrypted = _decrypt_envelope(
                cast("dict[str, Any]", envelope), cipher=cipher
            )
        except ValueError as err:
            raise DecoError(f"Invalid response for '{path}'") from err
        if not resp.ok:
            raise DecoError(
                f"Deco returned HTTP {resp.status_code} for '{path}': {decrypted}",
                error_code=_numeric_code(decrypted),
            )
        return decrypted

    def raw(
        self,
        path: str,
        *,
        operation: str = "read",
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        任意のエンドポイントを呼び、復号した envelope をそのまま返す。

        envelope のエラー (error_code など) は例外にせず返す。

        Raises:
            DecoError: HTTP エラーや不正な応答。
        """
        body: dict[str, Any] = {"operation": operation}
        if params is not None:
            body["params"] = params
        return self._send(path, body=body)

    def request(
        self,
        path: str,
        *,
        operation: str = "read",
        params: dict[str, Any] | None = None,
    ) -> Any:
        """
        エンドポイントを呼び、成功応答の本体 (`result` または `data`) を返す。

        Raises:
            DecoError: ルーターがエラーを返したとき (error_code を保持)。
        """
        envelope = self.raw(path, operation=operation, params=params)
        numeric_code = _numeric_code(envelope)
        if numeric_code == 0:
            return envelope.get("result")
        if envelope.get("success") is True:
            return envelope.get("data")
        raise DecoError(
            f"Deco request '{path}' failed: {envelope}", error_code=numeric_code
        )

    def _read(
        self,
        path: str,
        *,
        operation: str = "read",
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        設定・状態を 1 つ読み、object の result を返す (空なら {})。

        Raises:
            DecoError: ルーターがエラーを返した、または result が object でない。
        """
        result = self.request(path, operation=operation, params=params)
        if result is None or result in ([], {}):  # 空の Lua テーブルは [] でも返る
            return {}
        if not isinstance(result, dict):
            raise DecoError(f"Unexpected result for '{path}': not a JSON object")
        return cast("dict[str, Any]", result)

    def get_device_list(self) -> list[dict[str, Any]]:
        result = self._read("admin/device?form=device_list")
        return _named_items(
            result, key="device_list", names=("nickname", "custom_nickname")
        )

    def get_client_list(self) -> list[dict[str, Any]]:
        result = self._read(
            "admin/client?form=client_list", params={"device_mac": "default"}
        )
        return _named_items(result, key="client_list", names=("name",))

    def get_blocked_clients(self) -> list[dict[str, Any]]:
        result = self._read("admin/client?form=black_list", operation="list")
        return _named_items(result, key="client_list", names=("name",))

    def get_wan(self) -> dict[str, Any]:
        return self._read("admin/network?form=wan_ipv4")

    def get_internet(self) -> dict[str, Any]:
        return self._read("admin/network?form=internet")

    def get_lan(self) -> dict[str, Any]:
        return self._read("admin/network?form=lan_ip", params={"device_mac": "default"})

    def get_ipv6(self) -> dict[str, Any]:
        return self._read("admin/network?form=ipv6", params={"device_mac": "default"})

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

    def set_wlan(self, *, band: str, network: str, settings: dict[str, Any]) -> Any:
        """
        band と network (host / guest) の組 1 つ分の Wi-Fi 設定を書き込む。

        ssid / password はルーターが base64 で保持するので、平文を受け取って変換する。
        """
        encoded = {
            key: b64encode(value.encode()).decode()
            if key in ("ssid", "password")
            else value
            for key, value in settings.items()
        }
        return self.request(
            "admin/wireless?form=wlan",
            operation="write",
            params={band: {network: encoded}},
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
        return self._read("admin/cloud?form=ddns", operation="get")

    def get_extra_component_info(self) -> dict[str, Any]:
        return self._read("admin/web?form=extra_component_info", operation="get")

    def get_switch_list(self) -> dict[str, Any]:
        return self._read("admin/component_control?form=switch_list")

    def get_log_types(self) -> Any:
        return self.request("admin/log_export?form=types")

    def get_system_log(self, *, level: int, index: int, limit: int) -> dict[str, Any]:
        """
        システムログを 1 ページ分取得する。

        build でルーター側に level (get_log_types の value。8 = ALL) で絞ったスナップショットを
        作らせてから read する。index は 0 始まりのページ番号、limit は 1 ページの件数で、
        応答の totalNum はその limit での総ページ数。
        """
        self.request(
            "admin/log_export?form=feedback_log",
            operation="build",
            params={"level": level},
        )
        return self._read(
            "admin/log_export?form=feedback_log",
            params={"index": index, "limit": limit},
        )

    def check_firmware(self) -> list[dict[str, Any]]:
        """TP-Link cloud に各ノードの最新ファームウェアを問い合わせる (数秒かかる)。"""
        result = self._read("admin/cloud?form=firmware_status", operation="check")
        return _named_items(result, key="fw_list", names=("new_version",))

    def reboot(self, macs: list[str]) -> Any:
        return self.request(
            "admin/device?form=system",
            operation="reboot",
            params={"mac_list": [{"mac": mac} for mac in macs]},
        )

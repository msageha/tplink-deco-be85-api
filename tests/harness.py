"""
e2e テストの実行環境: luci のプロトコルを話す fake の Deco と、それに接続した API サーバー。

fake の Deco は実機の Web UI と同じログインフロー (RSA 公開鍵の配布・署名の検証・AES の本文・
stok と sysauth cookie) を受け付けるので、API サーバーは実機に対するのと同じ経路で動く。
"""

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from base64 import b64decode, b64encode
from binascii import a2b_hex
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import md5
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple, override
from urllib.parse import parse_qs, urlsplit

import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.number import getPrime
from Crypto.Util.Padding import pad, unpad

from tests.fake_tmp import ROUTER_EMAIL, FakeTmp

ROUTER_PASSWORD = "router-admin-password"
_ACCOUNT = "admin"
_LUCI_PREFIX = "/cgi-bin/luci/;stok="
# 512 bit の署名鍵で PKCS#1 v1.5 が 1 ブロックに暗号化できる長さ (64 - 11 byte)。
_SIGN_CHUNK = 53
_LOGIN_SIGN_RE = re.compile(r"k=(?P<k>[0-9a-f]{16})&i=(?P<i>[0-9a-f]{16})&(?P<rest>.*)")
_NO_SUCH_CALLBACK = {"error_code": 1, "msg": "no such callback"}
_LOGIN_FAILURE = {
    "error_code": -5002,
    "result": {"failureCount": 1, "attemptsAllowed": 8},
}
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _sign_key() -> RSA.RsaKey:
    """
    実機と同じ 512 bit の署名用鍵を作る。

    RSA.generate は 1024 bit 未満を受け付けないので、素数から組み立てる。
    """
    e = 65537
    while True:
        p, q = getPrime(256), getPrime(256)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e != 0:
            return RSA.construct((p * q, e, pow(e, -1, phi), p, q))


def _public_hex(key: RSA.RsaKey) -> list[str]:
    return [format(key.n, "x"), format(key.e, "x")]


class HttpReply(NamedTuple):
    """ルーターが返す HTTP 応答そのもの (暗号化しない)。"""

    status: int
    content_type: str
    body: bytes

    @classmethod
    def json(cls, value: Any, *, status: int = 200) -> HttpReply:
        return cls(status, "application/json", json.dumps(value).encode())


LOGIN_PAGE = HttpReply(200, "text/html", b"<html><body>login</body></html>")
UNAUTHORIZED = HttpReply(401, "application/json", b'{"error_code": -40401}')
FORBIDDEN = HttpReply(403, "application/json", b'{"error_code": -40403}')


@dataclass(frozen=True)
class RouterCall:
    """ログイン後のエンドポイントに届いた、復号済みのリクエスト。"""

    path: str
    operation: str
    params: Any


@dataclass
class _Session:
    key: bytes
    iv: bytes
    seq: int
    stok: str
    sysauth: str

    def encrypt(self, envelope: Any) -> str:
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        plain = pad(json.dumps(envelope).encode(), AES.block_size)
        return b64encode(cipher.encrypt(plain)).decode()

    def decrypt(self, data: str) -> Any:
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        return json.loads(unpad(cipher.decrypt(b64decode(data)), AES.block_size))


@dataclass(frozen=True)
class _Deferred:
    """ルーターの lock を持たずに、登録した関数で envelope を作って返す応答。"""

    session: _Session
    build: Callable[[Any], Any]
    params: Any


class FakeDeco:
    """
    1 台の Deco を模した HTTP サーバー。

    ログインは README の手順 (keys → auth → login) だけを受け付け、公開鍵と seq はその都度
    作り直す。署名は README の形式 (平文 53 byte ごとの RSA ブロック) だけを受け付ける。
    ログイン後のエンドポイントは、`respond` で登録した (path, operation) の envelope
    (params から envelope を作る関数でもよい) を暗号化して返し、未登録なら実機と同じ
    `no such callback` を返す。`respond_raw` で登録した応答は暗号化せずにそのまま返す。
    stok・sysauth cookie・署名のどれかが合わないリクエストには `rejection` を返す。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._password_key = RSA.generate(1024)  # noqa: S505 - fake のルーターだけが使う鍵
        self._sign_key = _sign_key()
        self._seq = 0
        self._handshake: list[str] = []
        self._session: _Session | None = None
        self.password = ROUTER_PASSWORD
        self.reachable = True
        self.rejection = LOGIN_PAGE
        self.accepts_sessions = True
        self.sets_cookie = True
        self._responses: dict[tuple[str, str], Any] = {}
        self._calls: list[RouterCall] = []
        self._arrivals: list[str] = []
        self._session_keys: list[tuple[bytes, bytes]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    @classmethod
    @contextmanager
    def serve(cls) -> Generator[FakeDeco]:
        deco = cls()
        thread = threading.Thread(target=deco._server.serve_forever, daemon=True)
        thread.start()
        try:
            yield deco
        finally:
            deco.stop()

    def stop(self) -> None:
        """待ち受けをやめる。以後の接続は拒否される (電源が落ちたルーターと同じ状態)。"""
        self._server.shutdown()
        self._server.server_close()

    def reset(self) -> None:
        """ログインセッション以外の状態 (設定・登録した応答・記録) を初期状態に戻す。"""
        with self._lock:
            self.password = ROUTER_PASSWORD
            self.reachable = True
            self.rejection = LOGIN_PAGE
            self.accepts_sessions = True
            self.sets_cookie = True
            self._responses = {}
            self._calls = []
            self._arrivals = []
            self._session_keys = []

    def respond(
        self,
        path: str,
        *,
        envelope: Any | Callable[[Any], Any],
        operation: str = "read",
    ) -> None:
        with self._lock:
            self._responses[path, operation] = envelope

    def respond_result(
        self, path: str, *, result: Any, operation: str = "read"
    ) -> None:
        self.respond(
            path, envelope={"error_code": 0, "result": result}, operation=operation
        )

    def respond_raw(
        self, path: str, *, reply: HttpReply, operation: str = "read"
    ) -> None:
        """暗号化しない HTTP 応答を登録する (平文の envelope・壊れた暗号文・HTTP エラー)。"""
        with self._lock:
            self._responses[path, operation] = reply

    def expire_session(self) -> None:
        """ルーター側でセッションを失効させる (再起動や timeout と同じ状態)。"""
        with self._lock:
            self._session = None

    @property
    def calls(self) -> list[RouterCall]:
        """ログアウトを除く、ログイン後のエンドポイントが受理した呼び出し (到着順)。"""
        with self._lock:
            return [c for c in self._calls if c.path != "admin/system?form=logout"]

    @property
    def arrivals(self) -> list[str]:
        """
        ログインや拒否したものを含む、届いたすべてのリクエストの path (到着順)。

        luci の path は `<module>?form=<form>` の形、それ以外は URL の path のまま。
        """
        with self._lock:
            return list(self._arrivals)

    @property
    def session_keys(self) -> list[tuple[bytes, bytes]]:
        """成功したログインごとの、クライアントが選んだ AES の鍵と IV。"""
        with self._lock:
            return list(self._session_keys)

    @property
    def logins(self) -> int:
        """成功したログインの回数。"""
        with self._lock:
            return len(self._session_keys)

    @property
    def logged_in(self) -> bool:
        with self._lock:
            return self._session is not None

    def _decrypt_sign(self, sign: str) -> str | None:
        """署名を復号する。平文 53 byte ごとのブロックでなければ None。"""
        block = self._sign_key.size_in_bytes() * 2
        decryptor = PKCS1_v1_5.new(self._sign_key)
        try:
            chunks = [
                decryptor.decrypt(a2b_hex(sign[i : i + block]), b"")
                for i in range(0, len(sign), block)
            ]
        except ValueError:
            return None
        if not chunks or any(len(c) != _SIGN_CHUNK for c in chunks[:-1]):
            return None
        if not 0 < len(chunks[-1]) <= _SIGN_CHUNK:
            return None
        try:
            return b"".join(chunks).decode()
        except UnicodeDecodeError:
            return None

    def _auth_fields(self, *, seq: int, data: str) -> str:
        """署名の `h=<認証 hash>&s=<seq + 暗号文長>` の部分。"""
        cred_hash = md5(
            (_ACCOUNT + self.password).encode(), usedforsecurity=False
        ).hexdigest()
        return f"h={cred_hash}&s={seq + len(data)}"

    def _login_form(self, form: str, *, body: dict[str, str]) -> tuple[Any, str | None]:
        """`login?form=<form>` の応答と、ログインに成功したときの sysauth を返す。"""
        if form == "keys":
            self._password_key = RSA.generate(1024)  # noqa: S505 - fake のルーターだけが使う鍵
            self._handshake = ["keys"]
            return {"result": {"password": _public_hex(self._password_key)}}, None
        if form == "auth":
            # 再ログインで古い鍵や seq を使い回すと署名が合わないよう、auth のたびに変える。
            self._sign_key = _sign_key()
            self._seq = secrets.randbelow(10**9)
            self._handshake.append("auth")
            key = _public_hex(self._sign_key)
            return {"result": {"key": key, "seq": self._seq}}, None
        handshake, self._handshake = self._handshake, []
        if form != "login" or handshake != ["keys", "auth"]:
            return _NO_SUCH_CALLBACK, None
        data = body["data"]
        match = _LOGIN_SIGN_RE.fullmatch(self._decrypt_sign(body["sign"]) or "")
        if match is None or match["rest"] != self._auth_fields(
            seq=self._seq, data=data
        ):
            return _LOGIN_FAILURE, None
        session = _Session(
            key=match["k"].encode(),
            iv=match["i"].encode(),
            seq=self._seq,
            stok=secrets.token_hex(16),
            sysauth=secrets.token_hex(16),
        )
        request = session.decrypt(data)
        password = PKCS1_v1_5.new(self._password_key).decrypt(
            a2b_hex(request["params"]["password"]), b""
        )
        if request["operation"] != "login" or password != self.password.encode():
            return {"data": session.encrypt(_LOGIN_FAILURE)}, None
        self._session = session
        self._session_keys.append((session.key, session.iv))
        ok = {
            "data": session.encrypt({"error_code": 0, "result": {"stok": session.stok}})
        }
        return ok, session.sysauth if self.sets_cookie else None

    def _admin(
        self, *, path: str, stok: str, sysauth: str | None, body: dict[str, str]
    ) -> Any | _Deferred | None:
        """ログイン後のエンドポイントの応答。セッションが無効なら None。"""
        session = self._session
        data = body["data"]
        if (
            not self.accepts_sessions
            or session is None
            or stok != session.stok
            or sysauth != session.sysauth
            or self._decrypt_sign(body["sign"])
            != self._auth_fields(seq=session.seq, data=data)
        ):
            return None
        request = session.decrypt(data)
        operation = request["operation"]
        params = request.get("params")
        self._calls.append(RouterCall(path, operation, params))
        if path == "admin/system?form=logout" and operation == "logout":
            self._session = None
            return {"error_code": 0}
        envelope = self._responses.get((path, operation), _NO_SUCH_CALLBACK)
        if isinstance(envelope, HttpReply):
            return envelope
        if callable(envelope):
            return _Deferred(session, envelope, params)
        return {"data": session.encrypt(envelope)}

    def _handle(
        self, *, url: str, cookie: str, raw_body: str
    ) -> tuple[Any | None, str | None] | None:
        """
        1 リクエストを処理し、(応答本文, 発行した sysauth) を返す。

        luci の URI でなければ None。応答本文が None ならセッションが無効。
        """
        parts = urlsplit(url)
        is_luci = parts.path.startswith(_LUCI_PREFIX)
        stok, _, path = parts.path.removeprefix(_LUCI_PREFIX).partition("/")
        query = parse_qs(parts.query, keep_blank_values=True)
        if "form" in query:
            path = f"{path}?form={query['form'][0]}"
        with self._lock:
            self._arrivals.append(path if is_luci else parts.path)
        if not is_luci:
            return None
        body = parse_qs(raw_body, keep_blank_values=True)
        is_login = not stok and path.startswith("login?form=")
        login_form = path.removeprefix("login?form=") if is_login else None
        reads_key = login_form in {"keys", "auth"}
        # luci が読む項目だけを、重複なしで受け付ける。公開鍵は operation=read で読み、
        # それ以外は本文の sign と data だけを読む。
        well_formed = (
            all(len(v) == 1 for v in [*query.values(), *body.values()])
            and set(body) == (set() if reads_key else {"sign", "data"})
            and (
                set(query) == {"form", "operation"} and query["operation"] == ["read"]
                if reads_key
                else set(query) <= {"form"}
            )
        )
        if not well_formed:
            return _NO_SUCH_CALLBACK, None
        fields = {k: v[0] for k, v in body.items()}
        with self._lock:
            if login_form is not None:
                return self._login_form(login_form, body=fields)
            cookies = SimpleCookie(cookie)
            sysauth = cookies["sysauth"].value if "sysauth" in cookies else None
            response = self._admin(path=path, stok=stok, sysauth=sysauth, body=fields)
        if isinstance(response, _Deferred):
            # 登録した関数は待ち合わせなどをしうるので、lock の外で呼ぶ。
            envelope = response.build(response.params)
            return {"data": response.session.encrypt(envelope)}, None
        return response, None

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        deco = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(length).decode()
                if not deco.reachable:
                    with deco._lock:
                        deco._arrivals.append(urlsplit(self.path).path)
                    # 応答を返さずに切断し、接続できないルーターを模す。
                    self.close_connection = True
                    return
                handled = deco._handle(
                    url=self.path,
                    cookie=self.headers.get("Cookie", ""),
                    raw_body=raw_body,
                )
                if handled is None:
                    self._reply(b"", status=404, content_type="text/html")
                    return
                response, sysauth = handled
                if response is None:
                    response = deco.rejection
                if isinstance(response, HttpReply):
                    self._reply(
                        response.body,
                        status=response.status,
                        content_type=response.content_type,
                    )
                    return
                self._reply(
                    json.dumps(response).encode(),
                    content_type="application/json",
                    sysauth=sysauth,
                )

            def _reply(
                self,
                payload: bytes,
                *,
                status: int = 200,
                content_type: str,
                sysauth: str | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                if sysauth is not None:
                    self.send_header(
                        "Set-Cookie", f"sysauth={sysauth}; path=/cgi-bin/luci"
                    )
                self.end_headers()
                self.wfile.write(payload)

            @override
            def log_message(self, format: str, *args: Any) -> None:
                pass

        return Handler


class Api:
    """起動した API サーバーへの HTTP クライアント。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._session = requests.Session()
        # 実行環境の HTTP_PROXY などを使わず、loopback の API サーバーへ直接つなぐ。
        self._session.trust_env = False

    def get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> requests.Response:
        return self._session.get(f"{self.base_url}{path}", params=params, timeout=30)

    def post(self, path: str, *, json: Any = None) -> requests.Response:
        return self._session.post(f"{self.base_url}{path}", json=json, timeout=30)

    def post_bytes(
        self, path: str, *, body: bytes, content_type: str
    ) -> requests.Response:
        """本文をそのまま送る (JSON でない本文や、JSON の値でない NaN を含む本文)。"""
        return self._session.post(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": content_type},
            timeout=30,
        )


@dataclass(frozen=True)
class Stack:
    """fake の Deco (luci の Web API と、Deco アプリの経路の TMP) と、それに接続した API サーバー。"""

    router: FakeDeco
    app: FakeTmp
    api: Api

    def reset(self) -> None:
        """
        前のテストや入力が残したルーターの状態を捨てる。

        ログインセッション (luci・TMP) は、ログインの往復を入力ごとに繰り返さないよう残す。
        ログイン状態で結果が変わるテストは、`reset_logged_out` から始める。
        """
        self.router.reset()
        self.app.reset()

    def reset_logged_out(self) -> None:
        """`reset` に加えて、API サーバーとルーターの両方を、luci・TMP ともログインしていない状態にする。"""
        self.router.reset()
        self.app.reset()
        self.drop_tmp_sessions()
        self.api.post("/api/logout").raise_for_status()
        self.router.expire_session()
        # ログアウトのリクエストの記録を消す。
        self.router.reset()

    def drop_tmp_sessions(self) -> None:
        """
        TMP のセッションをルーター側から切り、API サーバーが切断を認識するまで待つ。

        切断は API サーバーの SSH のスレッドが非同期に知るので、待たずに次の要求を送ると、
        切れたセッションで送って 504 になりうる。
        """
        self.app.drop_sessions()
        deadline = time.monotonic() + 10
        while self.api.get("/api/health").json()["tmp_connected"]:
            if time.monotonic() > deadline:
                raise RuntimeError("API server did not notice the dropped TMP session")
            time.sleep(0.05)


_STARTED_RE = re.compile(r"Uvicorn running on (http://\S+)")


@contextmanager
def run_api(*, deco_host: str, tmp: FakeTmp, log_path: Path) -> Generator[Api]:
    """
    `uvicorn main:app` を本番と同じ環境変数の設定で subprocess として起動する。

    実行環境の変数は coverage の計測に使うものだけを渡す。settings は名前の大小文字を区別せずに
    読むので、手元の `deco_host` などが残ると実機に接続しうるため。proxy・uvicorn の設定も
    同じ理由で渡さず、ACCOUNT / VERIFY_SSL / TIMEOUT はアプリの既定値で動かす。TMP は
    実機と同じ 20001 番では待ち受けられないので、fake の port を TMP_PORT で、fake の host key を
    TMP_HOST_KEY で渡す。
    """
    env = {k: v for k, v in os.environ.items() if k.startswith("COVERAGE_")}
    env |= {
        "PASSWORD": ROUTER_PASSWORD,
        "TPLINK_EMAIL": ROUTER_EMAIL,
        "DECO_HOST": deco_host,
        "TMP_PORT": str(tmp.port),
        "TMP_HOST_KEY": tmp.trusted_host_key,
    }
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--app-dir",
        "src",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--no-access-log",
    ]
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(  # noqa: S603 - 引数は固定値と sys.executable だけ
            command,
            cwd=_PROJECT_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
    try:
        url = _wait_until_started(process, log_path=log_path)
    except BaseException:
        # 起動の失敗に加え、待機中の中断 (Ctrl-C 等) でもサーバーを残さない。
        process.kill()
        process.wait()
        raise
    try:
        yield Api(url)
    finally:
        _stop(process, log_path=log_path)


def _stop(process: subprocess.Popen[bytes], *, log_path: Path) -> None:
    """SIGTERM で終了させ、lifespan の終了処理と coverage の保存を通す。途中で落ちていたら失敗にする。"""
    if process.poll() is not None:
        raise RuntimeError(
            f"API server exited during the tests:\n{log_path.read_text(encoding='utf-8')}"
        )
    process.terminate()
    try:
        returncode = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        returncode = None
    # uvicorn は終了処理の後に SIGTERM を投げ直して終わる。
    if returncode not in {0, -signal.SIGTERM}:
        raise RuntimeError(
            f"API server did not stop cleanly ({returncode}):\n"
            f"{log_path.read_text(encoding='utf-8')}"
        )


def _wait_until_started(process: subprocess.Popen[bytes], *, log_path: Path) -> str:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        output = log_path.read_text(encoding="utf-8")
        if match := _STARTED_RE.search(output):
            return match.group(1)
        if process.poll() is not None:
            raise RuntimeError(f"API server exited during startup:\n{output}")
        time.sleep(0.05)
    raise RuntimeError(
        f"API server did not start in time:\n{log_path.read_text(encoding='utf-8')}"
    )

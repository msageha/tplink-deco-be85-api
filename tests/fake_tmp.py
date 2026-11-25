"""
Deco アプリの経路 (TMP / TPAP) を話す fake の Deco。

実機の tpapsvr と同じく、TLS (port 30001 相当) を終端し、TP-Link ID で SPAKE2+ 認証したあと、
"TMP" チャネルの data packet を AES-128-CCM で復号して中身の TMP packet を読む。TMP packet と
business の header は実機の形式で読み書きし、CRC が合わない要求は受け付けない。最初の要求は
セッションの割り当て (opcode 0x0001) でなければならない。応答の前には keep-alive を送って
クライアントの応答を待ち、同じ opcode の前の取引への応答と、要求と同じ txid のほかの opcode の通知
(実機がクライアントの接続イベントを送るのと同じ形) を挟む。長い応答は分割して、クライアントの PULL ごとに続きを返す。
"""

import datetime as dt
import json
import socket
import ssl
import struct
import tempfile
import threading
import zlib
from base64 import b64decode, b64encode
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import Any

from Crypto.Cipher import AES
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests.spake2p_verifier import Spake2pVerifier

ROUTER_EMAIL = "owner@example.com"
_TMP_HEADER = struct.Struct(">BBBBHBBII")
_BUSINESS_HEADER = struct.Struct(">BBHBBHIII")
_CRC_PLACEHOLDER = 0x5A6B7C8D
_KEEPALIVE = bytes([1, 1, 4, 0])
_PTYPE_REQUEST, _PTYPE_PULL, _PTYPE_RESPONSE = 2, 4, 5
_OPCODE_ALLOC = 0x0001
_OPCODE_CLIENT_NOTIFY = 0x4015
# 1 つの応答 packet に載せる本文の最大長。これより長い応答は分割して PULL を待つ。
FRAGMENT_SIZE = 256

# TSCP の header。length / checksum は big-endian、seq は little-endian。
_TSCP_HEADER_SIZE = 24
_TSCP_TYPE_AUTH = 1
_TSCP_TYPE_DATA = 2
_TSCP_CHANNEL_TMP = b"TMP\x00\x00\x00\x00\x00"
_TSCP_CRC_MAGIC = 0x92BF30ED
# tpapsvr が受け付ける payload の上限 (本番と同じ)。
_MAX_TSCP_PAYLOAD = 0x10010


@cache
def _tls_cert() -> tuple[str, str]:
    """自己署名の TLS 証明書と秘密鍵を一度だけ作り、ファイルの PATH を返す。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-deco")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    directory = Path(tempfile.mkdtemp(prefix="fake-deco-tls-"))
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


@dataclass(frozen=True)
class TmpCall:
    """TMP の要求として届いた、opcode と JSON の本文。"""

    opcode: int
    body: Any


@dataclass(frozen=True)
class BusinessError:
    """business の header の err に、この値を入れて応答する。"""

    err: int


DISCONNECT = object()
"""要求を受け取ったあと、応答せずにセッションを切る。"""


def _tmp_packet(payload: bytes, *, seq: int) -> bytes:
    header = _TMP_HEADER.pack(1, 1, 5, 0, len(payload), 0, 0, seq, _CRC_PLACEHOLDER)
    crc = zlib.crc32(header + payload)
    return header[:-4] + struct.pack(">I", crc) + payload


def _business_packet(
    *, opcode: int, txid: int, body: bytes = b"", offset: int = 0, err: int = 0
) -> bytes:
    """
    応答の本文 body のうち、offset から最大 FRAGMENT_SIZE byte を載せた応答。

    実機と同じく、CRC32 は分割しても断片ではなく本文全体のものを入れる。
    """
    chunk = body[offset : offset + FRAGMENT_SIZE]
    crc = zlib.crc32(body) if body else 0
    header = _BUSINESS_HEADER.pack(
        1, 2, opcode, _PTYPE_RESPONSE, err, txid, crc, len(body), offset
    )
    return header + chunk


class _ProtocolError(Exception):
    """要求が TMP / TPAP の形式に合わない。"""


class _Transport:
    """TLS の上の TSCP frame を読み書きする。data は session 鍵で暗号化する。"""

    def __init__(self, sock: ssl.SSLSocket) -> None:
        self._sock = sock
        self._key = b""
        self._nonce_base = b""
        self._seq = 0

    def set_session(self, key: bytes, *, nonce_base: bytes) -> None:
        self._key, self._nonce_base = key, nonce_base

    def _receive_exact(self, size: int) -> bytes:
        buffer = b""
        while len(buffer) < size:
            chunk = self._sock.recv(size - len(buffer))
            if not chunk:
                raise EOFError
            buffer += chunk
        return buffer

    def _encrypt(self, plaintext: bytes, *, seq: int) -> bytes:
        nonce = self._nonce_base + struct.pack("<I", seq)
        cipher = AES.new(self._key, AES.MODE_CCM, nonce=nonce, mac_len=16)
        return cipher.encrypt(plaintext) + cipher.digest()

    def _decrypt(self, data: bytes, *, seq: int) -> bytes:
        nonce = self._nonce_base + struct.pack("<I", seq)
        cipher = AES.new(self._key, AES.MODE_CCM, nonce=nonce, mac_len=16)
        return cipher.decrypt_and_verify(data[:-16], data[-16:])

    def send(
        self, payload: bytes, *, packet_type: int, corrupt_crc: bool = False
    ) -> None:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        channel = _TSCP_CHANNEL_TMP if packet_type == _TSCP_TYPE_DATA else bytes(8)
        if packet_type == _TSCP_TYPE_DATA:
            payload = self._encrypt(payload, seq=seq)
        header = (
            bytes([1, 1, packet_type, 0])
            + struct.pack(">I", len(payload))
            + channel
            + struct.pack("<I", seq)
            + struct.pack(">I", _TSCP_CRC_MAGIC)
        )
        crc = zlib.crc32(header + payload)
        if corrupt_crc:
            crc ^= 1
        self._sock.sendall(header[:20] + struct.pack(">I", crc) + payload)

    def receive(self, *, packet_type: int) -> bytes:
        header = self._receive_exact(_TSCP_HEADER_SIZE)
        length = struct.unpack_from(">I", header, 4)[0]
        seq = struct.unpack_from("<I", header, 16)[0]
        checksum = struct.unpack_from(">I", header, 20)[0]
        if header[0] != 1 or header[2] != packet_type:
            raise _ProtocolError(
                f"bad TSCP header (version {header[0]}, type {header[2]})"
            )
        if length > _MAX_TSCP_PAYLOAD:
            raise _ProtocolError(f"TSCP payload too long ({length} bytes)")
        payload = self._receive_exact(length)
        recomputed = zlib.crc32(
            header[:20] + struct.pack(">I", _TSCP_CRC_MAGIC) + payload
        )
        if recomputed != checksum:
            raise _ProtocolError("bad TSCP checksum")
        if packet_type != _TSCP_TYPE_DATA:
            return payload
        return self._decrypt(payload, seq=seq)


class _Session:
    """1 つの TLS セッションの上の TMP の要求を処理する。"""

    def __init__(self, fake: FakeTmp, *, transport: _Transport) -> None:
        self._fake = fake
        self._transport = transport
        self._seq = 0
        self._client_seq = 0
        self._allocated = False
        # 次の要求に使われるべき txid。セッションの割り当てが 0 で、要求ごとに 1 ずつ増える。
        self._next_txid = 0
        self._pending: dict[int, tuple[int, bytes]] = {}

    def _send(self, payload: bytes, *, corrupt_crc: bool = False) -> None:
        self._seq += 1
        packet = _tmp_packet(payload, seq=self._seq)
        if corrupt_crc:
            packet = packet[:15] + bytes([packet[15] ^ 1]) + packet[16:]
        self._transport.send(packet, packet_type=_TSCP_TYPE_DATA)

    def _send_keepalive(self) -> None:
        self._transport.send(_KEEPALIVE, packet_type=_TSCP_TYPE_DATA)

    def _send_fragment(self, *, opcode: int, txid: int, offset: int) -> None:
        _, body = self._pending[txid]
        packet = _business_packet(opcode=opcode, txid=txid, body=body, offset=offset)
        # fragment_skew は先頭以外の断片の offset を実際の位置からずらして送る (壊れた応答を模す)。
        header = list(_BUSINESS_HEADER.unpack_from(packet))
        if offset and self._fake.fragment_skew:
            header[-1] += self._fake.fragment_skew
        # corrupt は応答の CRC を壊す: "body" は business の本文全体の CRC、"packet" は TMP の packet の CRC。
        if self._fake.corrupt == "body":
            header[6] ^= 1
        packet = _BUSINESS_HEADER.pack(*header) + packet[_BUSINESS_HEADER.size :]
        self._send(packet, corrupt_crc=self._fake.corrupt == "packet")
        if offset + FRAGMENT_SIZE >= len(body):
            del self._pending[txid]

    def _read_request(self) -> tuple[int, int, int, int, bytes]:
        """次の要求の (opcode, ptype, txid, offset, 本文)。クライアントの keep-alive の応答は読み捨てる。"""
        while True:
            packet = self._transport.receive(packet_type=_TSCP_TYPE_DATA)
            if packet == _KEEPALIVE:
                continue
            if len(packet) < _TMP_HEADER.size:
                raise _ProtocolError("short TMP packet")
            *fields, crc = _TMP_HEADER.unpack_from(packet)
            if fields[:4] != [1, 1, 5, 0] or fields[5:7] != [0, 0]:
                raise _ProtocolError(f"bad TMP header {fields}")
            payload = packet[_TMP_HEADER.size :]
            if len(payload) != fields[4]:
                raise _ProtocolError("TMP payload length mismatch")
            placeholder = packet[: _TMP_HEADER.size - 4] + struct.pack(
                ">I", _CRC_PLACEHOLDER
            )
            if zlib.crc32(placeholder + payload) != crc:
                raise _ProtocolError("bad TMP CRC")
            # クライアントの seq は 1 から 1 ずつ増える。
            self._client_seq += 1
            if fields[7] != self._client_seq:
                raise _ProtocolError("TMP seq out of order")
            (
                version,
                business_version,
                opcode,
                ptype,
                err,
                txid,
                body_crc,
                total,
                offset,
            ) = _BUSINESS_HEADER.unpack_from(payload)
            body = payload[_BUSINESS_HEADER.size :]
            if (version, business_version, err) != (1, 2, 0) or total != len(body):
                raise _ProtocolError("bad business header")
            if body_crc != (zlib.crc32(body) if body else 0):
                raise _ProtocolError("bad business CRC")
            return opcode, ptype, txid, offset, body

    def serve(self) -> None:
        while True:
            opcode, ptype, txid, offset, body = self._read_request()
            if ptype == _PTYPE_PULL:
                # PULL は分割中の要求と同じ txid で来る。
                if txid not in self._pending:
                    raise _ProtocolError("PULL for unknown txid")
                self._send_fragment(opcode=opcode, txid=txid, offset=offset)
                continue
            if ptype != _PTYPE_REQUEST:
                raise _ProtocolError(f"bad ptype {ptype}")
            if txid != self._next_txid:
                raise _ProtocolError(f"txid {txid} out of order")
            self._next_txid = (txid + 1) & 0xFFFF
            if opcode == _OPCODE_ALLOC:
                self._allocated = True
                token = self._fake.allocate()
                self._send(_business_packet(opcode=opcode, txid=txid, body=token))
                continue
            if not self._allocated:
                raise _ProtocolError("request before session allocation")
            request = json.loads(body)
            reply = self._fake.handle(TmpCall(opcode, request))
            if reply is DISCONNECT:
                return
            self._respond(opcode=opcode, txid=txid, reply=reply)

    def _respond(self, *, opcode: int, txid: int, reply: bytes | BusinessError) -> None:
        # 実機と同じく、応答の前に keep-alive と、ほかの取引の通知が届きうる。
        # keep-alive に応答しないクライアントには応答を返さない。
        self._send_keepalive()
        if self._transport.receive(packet_type=_TSCP_TYPE_DATA) != _KEEPALIVE:
            raise _ProtocolError("keep-alive was not answered")
        # 同じ opcode の、前の取引への応答 (遅れて届いたもの) も挟む。
        stale = json.dumps({"error_code": -1}).encode()
        self._send(
            _business_packet(opcode=opcode, txid=(txid - 1) & 0xFFFF, body=stale)
        )
        notice = json.dumps({"mac": "AA-BB-CC-DD-EE-FF", "event": "online"}).encode()
        self._send(
            _business_packet(
                opcode=_OPCODE_CLIENT_NOTIFY,
                # 通知の txid はルーター側の番号なので、要求の txid と重なりうる。
                txid=txid,
                body=notice,
            )
        )
        if isinstance(reply, BusinessError):
            self._send(_business_packet(opcode=opcode, txid=txid, err=reply.err))
            return
        self._pending[txid] = (opcode, reply)
        self._send_fragment(opcode=opcode, txid=txid, offset=0)


class FakeTmp:
    """
    1 台の Deco の TMP (TPAP の TLS の port) を模したサーバー。

    要求の応答は `respond` で opcode ごとに登録した envelope (本文から envelope を作る関数でもよい)
    を JSON で返す。`BusinessError` なら business の header の err で、`DISCONNECT` なら応答せずに
    切断して返す。未登録の opcode には `{"error_code": -1}` を返す。
    """

    def __init__(self, *, password: str) -> None:
        self._lock = threading.Lock()
        self._initial_password = password
        self.email = ROUTER_EMAIL
        self.password = password
        self.reachable = True
        self.drops_during_auth = False
        self.fragment_skew = 0
        self.corrupt = ""
        self._responses: dict[int, Any] = {}
        self._calls: list[TmpCall] = []
        self._logins = 0
        self._auth_attempts = 0
        self._sockets: list[ssl.SSLSocket] = []
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.load_cert_chain(*_tls_cert())
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self._stopped = threading.Event()

    @classmethod
    @contextmanager
    def serve(cls, *, password: str) -> Generator[FakeTmp]:
        """password は luci と同じ管理パスワード (TP-Link ID のパスワード)。"""
        fake = cls(password=password)
        thread = threading.Thread(target=fake._accept_loop, daemon=True)
        thread.start()
        try:
            yield fake
        finally:
            fake._stopped.set()
            fake._listener.close()
            fake.drop_sessions()

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve_connection, args=(conn,), daemon=True
            ).start()

    def _serve_connection(self, conn: socket.socket) -> None:
        if not self.reachable:
            conn.close()
            return
        try:
            tls = self._tls.wrap_socket(conn, server_side=True)
        except OSError, ssl.SSLError:
            conn.close()
            return
        with self._lock:
            self._sockets.append(tls)
        transport = _Transport(tls)
        try:
            if not self._authenticate(transport):
                return
            _Session(self, transport=transport).serve()
        except EOFError, OSError, ssl.SSLError, ValueError, _ProtocolError:
            pass
        finally:
            with suppress(OSError):
                tls.close()

    def _authenticate(self, transport: _Transport) -> bool:
        """SPAKE2+ のログインを 1 回受ける。成功したら session 鍵を transport に設定して True。"""
        register = json.loads(transport.receive(packet_type=_TSCP_TYPE_AUTH))
        if self.drops_during_auth:
            # 認証の結果を返さずに切断する (認証中に電源が落ちたルーターと同じ状態)。
            raise EOFError
        params = register["params"]
        verifier = Spake2pVerifier(f"{self.email}/{self.password}")
        transport.send(
            json.dumps(
                {"error_code": 0, "result": verifier.register_result()}
            ).encode(),
            packet_type=_TSCP_TYPE_AUTH,
        )
        share = json.loads(transport.receive(packet_type=_TSCP_TYPE_AUTH))["params"]
        with self._lock:
            self._auth_attempts += 1
        username_ok = params.get("username") == sha256(self.email.encode()).hexdigest()
        session = verifier.share_response(
            user_random=b64decode(params["user_random"]),
            user_share=b64decode(share["user_share"]),
            user_confirm=b64decode(share["user_confirm"]),
        )
        if session is None or not username_ok:
            transport.send(
                json.dumps({"error_code": -1}).encode(), packet_type=_TSCP_TYPE_AUTH
            )
            return False
        dev_confirm, key, nonce_base = session
        transport.set_session(key, nonce_base=nonce_base)
        with self._lock:
            self._logins += 1
        transport.send(
            json.dumps(
                {
                    "error_code": 0,
                    "result": {
                        "sub_method": "pake_confirm",
                        "dev_confirm": b64encode(dev_confirm).decode(),
                        "stok": "0123456789abcdef0123456789abcdef",
                        "start_seq": 0,
                        "expired": 3600,
                    },
                }
            ).encode(),
            packet_type=_TSCP_TYPE_AUTH,
        )
        return True

    def reset(self) -> None:
        """セッション以外の状態 (資格情報・登録した応答・記録) を初期状態に戻す。"""
        with self._lock:
            self.email = ROUTER_EMAIL
            self.password = self._initial_password
            self.reachable = True
            self.drops_during_auth = False
            self.fragment_skew = 0
            self.corrupt = ""
            self._responses = {}
            self._calls = []
            self._logins = 0
            self._auth_attempts = 0

    def drop_sessions(self) -> None:
        """張られている TLS セッションをルーター側から切る (再起動や idle timeout と同じ状態)。"""
        with self._lock:
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            # shutdown で FIN をすぐ送り、recv で待っている serve スレッドを解いてから close する。
            # close だけだと、serve スレッドが recv を抜けるまで FIN が遅れ、クライアントが切断を
            # 次の要求の前に気づけない。
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                sock.close()

    def respond(self, opcode: int, *, envelope: Any | Callable[[Any], Any]) -> None:
        with self._lock:
            self._responses[opcode] = envelope

    def respond_result(self, opcode: int, *, result: Any) -> None:
        self.respond(opcode, envelope={"error_code": 0, "result": result})

    @property
    def calls(self) -> list[TmpCall]:
        """セッションの割り当てを除く、受け付けた要求 (到着順)。"""
        with self._lock:
            return list(self._calls)

    @property
    def auth_attempts(self) -> int:
        """SPAKE2+ のログインを試みられた回数 (成功・失敗とも)。"""
        with self._lock:
            return self._auth_attempts

    @property
    def logins(self) -> int:
        """認証に成功した回数。"""
        with self._lock:
            return self._logins

    def allocate(self) -> bytes:
        return b"0123456789abcdef0123456789abcdef"

    def handle(self, call: TmpCall) -> Any:
        """要求を記録し、応答の本文 (bytes)・`BusinessError`・`DISCONNECT` のどれかを返す。"""
        with self._lock:
            self._calls.append(call)
            reply = self._responses.get(call.opcode, {"error_code": -1})
        if callable(reply):
            reply = reply(call.body)
        if reply is DISCONNECT or isinstance(reply, BusinessError):
            return reply
        if isinstance(reply, bytes):
            return reply
        return json.dumps(reply).encode()

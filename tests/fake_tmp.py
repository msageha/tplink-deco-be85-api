"""
Deco アプリの経路 (TMP) を話す fake の Deco。

実機の dropbear と同じく、SSH の password 認証 (username は TP-Link ID の email の SHA-1 の hex)
と、127.0.0.1:20002 への direct-tcpip のチャネルだけを受け付ける。チャネルの上では TMP の
packet と business の header を実機の形式で読み書きし、CRC が合わない要求は受け付けない。
最初の要求はセッションの割り当て (opcode 0x0001) でなければならない。応答の前には keep-alive を送って
クライアントの応答を待ち、同じ opcode の前の取引への応答と、要求と同じ txid のほかの opcode の通知
(実機がクライアントの接続イベントを送るのと同じ形) を挟む。長い応答は分割して、クライアントの PULL ごとに続きを返す。
"""

import json
import socket
import struct
import threading
import zlib
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from hashlib import sha1
from typing import Any, override

import paramiko
from paramiko.common import (
    AUTH_FAILED,
    AUTH_SUCCESSFUL,
    OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED,
    OPEN_FAILED_CONNECT_FAILED,
    OPEN_SUCCEEDED,
)

ROUTER_EMAIL = "owner@example.com"
_DAEMON_ADDRESS = ("127.0.0.1", 20002)
_TMP_HEADER = struct.Struct(">BBBBHBBII")
_BUSINESS_HEADER = struct.Struct(">BBHBBHIII")
_CRC_PLACEHOLDER = 0x5A6B7C8D
_KEEPALIVE = bytes([1, 1, 4, 0])
_PTYPE_REQUEST, _PTYPE_PULL, _PTYPE_RESPONSE = 2, 4, 5
_OPCODE_ALLOC = 0x0001
_OPCODE_CLIENT_NOTIFY = 0x4015
# 1 つの応答 packet に載せる本文の最大長。これより長い応答は分割して PULL を待つ。
FRAGMENT_SIZE = 256


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
    """応答の本文 body のうち、offset から最大 FRAGMENT_SIZE byte を載せた応答。"""
    chunk = body[offset : offset + FRAGMENT_SIZE]
    crc = zlib.crc32(chunk) if chunk else 0
    header = _BUSINESS_HEADER.pack(
        1, 2, opcode, _PTYPE_RESPONSE, err, txid, crc, len(body), offset
    )
    return header + chunk


class _ProtocolError(Exception):
    """要求が TMP の形式に合わない。"""


class _Session:
    """1 つの SSH セッションの上の TMP の要求を処理する。"""

    def __init__(self, fake: FakeTmp, *, channel: paramiko.Channel) -> None:
        self._fake = fake
        self._channel = channel
        self._seq = 0
        self._client_seq = 0
        self._allocated = False
        # 次の要求に使われるべき txid。セッションの割り当てが 0 で、要求ごとに 1 ずつ増える。
        self._next_txid = 0
        self._pending: dict[int, tuple[int, bytes]] = {}

    def _receive_exact(self, size: int) -> bytes:
        buffer = b""
        while len(buffer) < size:
            chunk = self._channel.recv(size - len(buffer))
            if not chunk:
                raise EOFError
            buffer += chunk
        return buffer

    def _send(self, payload: bytes) -> None:
        self._seq += 1
        self._channel.sendall(_tmp_packet(payload, seq=self._seq))

    def _send_fragment(self, *, opcode: int, txid: int, offset: int) -> None:
        _, body = self._pending[txid]
        packet = _business_packet(opcode=opcode, txid=txid, body=body, offset=offset)
        # fragment_skew は先頭以外の断片の offset を実際の位置からずらして送る (壊れた応答を模す)。
        if offset and self._fake.fragment_skew:
            header = list(_BUSINESS_HEADER.unpack_from(packet))
            header[-1] += self._fake.fragment_skew
            packet = _BUSINESS_HEADER.pack(*header) + packet[_BUSINESS_HEADER.size :]
        self._send(packet)
        if offset + FRAGMENT_SIZE >= len(body):
            del self._pending[txid]

    def _read_request(self) -> tuple[int, int, int, int, bytes]:
        """次の要求の (opcode, ptype, txid, offset, 本文)。クライアントの keep-alive の応答は読み捨てる。"""
        while True:
            head = self._receive_exact(4)
            if head == _KEEPALIVE:
                continue
            rest = self._receive_exact(_TMP_HEADER.size - 4)
            *fields, crc = _TMP_HEADER.unpack(head + rest)
            if fields[:4] != [1, 1, 5, 0] or fields[5:7] != [0, 0]:
                raise _ProtocolError(f"bad TMP header {fields}")
            payload = self._receive_exact(fields[4])
            placeholder = (head + rest)[:-4] + struct.pack(">I", _CRC_PLACEHOLDER)
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
        self._channel.sendall(_KEEPALIVE)
        if self._receive_exact(4) != _KEEPALIVE:
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


class _SshServer(paramiko.ServerInterface):
    def __init__(self, fake: FakeTmp, *, conn: socket.socket) -> None:
        self._fake = fake
        self._conn = conn

    @override
    def get_allowed_auths(self, username: str) -> str:
        return "password"

    @override
    def check_auth_password(self, username: str, password: str) -> int:
        if self._fake.drops_during_auth:
            # 認証の結果を返さずに切断する (認証中に電源が落ちたルーターと同じ状態)。
            self._conn.shutdown(socket.SHUT_RDWR)
            return AUTH_FAILED
        if self._fake.accepts(username=username, password=password):
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    @override
    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "direct-tcpip":
            return OPEN_SUCCEEDED
        return OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    @override
    def check_channel_direct_tcpip_request(
        self, chanid: int, origin: tuple[str, int], destination: tuple[str, int]
    ) -> int:
        if tuple(destination) == _DAEMON_ADDRESS:
            return OPEN_SUCCEEDED
        return OPEN_FAILED_CONNECT_FAILED


class FakeTmp:
    """
    1 台の Deco の TMP (SSH の port) を模したサーバー。

    要求の応答は `respond` で opcode ごとに登録した envelope (本文から envelope を作る関数でもよい)
    を JSON で返す。`BusinessError` なら business の header の err で、`DISCONNECT` なら応答せずに
    切断して返す。未登録の opcode には `{"error_code": -1}` を返す。
    """

    def __init__(self, *, password: str) -> None:
        self._lock = threading.Lock()
        self._host_key = paramiko.ECDSAKey.generate()
        self._initial_password = password
        self.email = ROUTER_EMAIL
        self.password = password
        self.reachable = True
        self.drops_during_auth = False
        self.fragment_skew = 0
        self._responses: dict[int, Any] = {}
        self._calls: list[TmpCall] = []
        self._logins = 0
        self._auth_attempts = 0
        self._transports: list[paramiko.Transport] = []
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
        transport = paramiko.Transport(conn)
        transport.add_server_key(self._host_key)
        with self._lock:
            self._transports.append(transport)
        try:
            transport.start_server(server=_SshServer(self, conn=conn))
            channel = transport.accept(timeout=10)
            if channel is None:
                return
            session = _Session(self, channel=channel)
            with suppress(EOFError, OSError, paramiko.SSHException, _ProtocolError):
                session.serve()
        except paramiko.SSHException, EOFError, OSError:
            pass
        finally:
            transport.close()

    def reset(self) -> None:
        """セッション以外の状態 (資格情報・登録した応答・記録) を初期状態に戻す。"""
        with self._lock:
            self.email = ROUTER_EMAIL
            self.password = self._initial_password
            self.reachable = True
            self.drops_during_auth = False
            self.fragment_skew = 0
            self._responses = {}
            self._calls = []
            self._logins = 0
            self._auth_attempts = 0

    def drop_sessions(self) -> None:
        """張られている SSH セッションをルーター側から切る (再起動や idle timeout と同じ状態)。"""
        with self._lock:
            transports, self._transports = self._transports, []
        for transport in transports:
            transport.close()

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
        """password 認証を試みられた回数 (成功・失敗とも)。"""
        with self._lock:
            return self._auth_attempts

    @property
    def logins(self) -> int:
        """認証に成功した回数。"""
        with self._lock:
            return self._logins

    def accepts(self, *, username: str, password: str) -> bool:
        expected = sha1(self.email.encode(), usedforsecurity=False).hexdigest()
        ok = username == expected and password == self.password
        with self._lock:
            self._auth_attempts += 1
            if ok:
                self._logins += 1
        return ok

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

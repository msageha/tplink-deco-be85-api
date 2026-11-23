"""
Deco アプリが使う TMP (Tether Management Protocol) のクライアント。

Deco の dropbear (SSH) に TP-Link ID で認証し、direct-tcpip のチャネルでルーター内の TMP の
デーモン (127.0.0.1:20002) につなぐ。チャネルの上では、TMP の packet (16 byte の header +
payload) に business の header (20 byte) と JSON の本文を載せて、opcode ごとに要求と応答を
やりとりする。数値はすべて big-endian。
"""

import json
import socket
import struct
import threading
import time
import zlib
from hashlib import sha1
from typing import Any, cast
from urllib.parse import urlsplit

import paramiko

from deco.exceptions import (
    DecoAuthError,
    DecoConnectionError,
    DecoError,
    DecoRequestTooLargeError,
)

_DAEMON_ADDRESS = ("127.0.0.1", 20002)
# TMP の packet の header: version 1・1、type、予約、payload 長、予約 2 byte、seq、CRC32。
_TMP_HEADER = struct.Struct(">BBBBHBBII")
# payload 長は 16 bit なので、1 つの packet に載る business header と本文はこの長さまで。
_MAX_PAYLOAD = 0xFFFF
# 応答の全体長として受け付ける上限。全体長はルーターが 32 bit で宣言するので、壊れた値で
# メモリを使い切らないよう、設定やクライアントの一覧に十分な大きさで打ち切る。
_MAX_RESPONSE = 16 * 1024 * 1024
_TMP_TYPE_KEEPALIVE = 4
_TMP_TYPE_ACK = 2
_TMP_TYPE_DATA = 5
# CRC32 は、この値を CRC の欄に入れた packet 全体から計算する。
_CRC_PLACEHOLDER = 0x5A6B7C8D
# business の header: version 1・2、opcode、ptype、err、txid、本文の CRC32、全体長、offset。
_BUSINESS_HEADER = struct.Struct(">BBHBBHIII")
_PTYPE_REQUEST = 2
_PTYPE_PULL = 4
_PTYPE_RESPONSE = 5
# セッションの割り当て。本文なしで送り、応答の token は以後の要求に使わない。
_OPCODE_ALLOC = 0x0001


def _tmp_packet(payload: bytes, *, seq: int) -> bytes:
    header = _TMP_HEADER.pack(1, 1, _TMP_TYPE_DATA, 0, len(payload), 0, 0, seq, 0)
    placeholder = header[:-4] + struct.pack(">I", _CRC_PLACEHOLDER)
    crc = zlib.crc32(placeholder + payload)
    return header[:-4] + struct.pack(">I", crc) + payload


def _business_packet(
    *, opcode: int, ptype: int, txid: int, body: bytes = b"", offset: int = 0
) -> bytes:
    crc = zlib.crc32(body) if body else 0
    header = _BUSINESS_HEADER.pack(1, 2, opcode, ptype, 0, txid, crc, len(body), offset)
    return header + body


class TmpClient:
    """1 台の Deco に対する TMP のセッションを保持する同期クライアント。並行利用はできない。"""

    def __init__(
        self,
        url: str,
        *,
        email: str,
        password: str,
        port: int = 20001,
        timeout: int = 30,
    ) -> None:
        """
        url は luci の Web API と同じ Deco の URL で、そのホストの port に SSH で接続する。

        SSH の username は TP-Link ID の email の SHA-1 (hex) で、dropbear がルーターに保存された
        オーナーのアカウントと照合する。
        """
        hostname = urlsplit(url).hostname
        if not hostname:
            raise ValueError(f"Deco URL has no host: {url!r}")
        self.host = hostname
        self._port = port
        self._username = sha1(email.encode(), usedforsecurity=False).hexdigest()
        self._password = password
        self._timeout = timeout
        self._transport: paramiko.Transport | None = None
        self._channel: paramiko.Channel | None = None
        self._seq = 0
        self._txid = 0
        self._deadline = 0.0
        # ルーターが資格情報を拒否したら、設定を直して起動し直すまで認証を送らない。
        # 誤った資格情報での再試行は、dropbear の失敗回数の上限を消費するため。
        self._rejected = False

    @property
    def connected(self) -> bool:
        return (
            self._transport is not None
            and self._transport.is_active()
            and self._channel is not None
            and not self._channel.closed
        )

    def _connect(self) -> None:
        if self._rejected:
            raise DecoAuthError(
                "TMP login was rejected; fix TPLINK_EMAIL / PASSWORD and restart the server"
            )
        self.close()
        try:
            sock = self._open_socket()
        except OSError as err:
            raise DecoConnectionError(
                f"Cannot reach Deco TMP at {self.host}:{self._port}: {err}"
            ) from err
        transport = paramiko.Transport(sock)
        self._transport = transport
        try:
            remaining = self._remaining()
            transport.banner_timeout = remaining
            transport.handshake_timeout = remaining
            transport.auth_timeout = remaining
            # ルーターの host key は検証しない (luci の HTTPS の証明書を既定で検証しないのと同じ扱い)。
            transport.connect(username=self._username, password=self._password)
            channel = transport.open_channel(
                "direct-tcpip",
                dest_addr=_DAEMON_ADDRESS,
                src_addr=sock.getsockname()[:2],
                timeout=self._remaining(),
            )
        except paramiko.AuthenticationException as err:
            # paramiko は認証中のタイムアウトや切断も AuthenticationException で投げるので、
            # 接続が続いたままルーターが認証の結果を返したときだけ資格情報の不一致として扱う
            # (切断でも auth_event は set される)。
            handler = transport.auth_handler
            answered = (
                transport.is_active()
                and handler is not None
                and handler.auth_event is not None
                and handler.auth_event.is_set()
            )
            self.close()
            if answered:
                self._rejected = True
                raise DecoAuthError(
                    "TMP login failed (check TPLINK_EMAIL and PASSWORD)"
                ) from err
            raise DecoConnectionError(
                f"TMP login to {self.host}:{self._port} did not complete: {err}"
            ) from err
        except (paramiko.SSHException, OSError, EOFError) as err:
            self.close()
            raise DecoConnectionError(
                f"Cannot open TMP session at {self.host}:{self._port}: "
                f"{type(err).__name__}: {err}"
            ) from err
        self._channel = channel
        self._seq = 0
        self._txid = 0
        self._exchange(_OPCODE_ALLOC, body=b"")

    def _open_socket(self) -> socket.socket:
        """
        Deco の TMP の port に TCP で接続する。

        socket.create_connection はアドレスの候補ごとに同じ timeout で待つので、候補ごとに要求の
        期限までの残り時間で試す。名前解決 (getaddrinfo) の待ちは打ち切れない。
        """
        last_error: OSError | None = None
        for family, kind, proto, _, address in socket.getaddrinfo(
            self.host, self._port, type=socket.SOCK_STREAM
        ):
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(self._remaining())
                sock.connect(address)
            except OSError as err:
                sock.close()
                last_error = err
                continue
            return sock
        raise last_error or OSError(f"No address for {self.host}")

    def close(self) -> None:
        """セッションを閉じる。閉じた後の呼び出しは接続し直す。"""
        transport = self._transport
        self._channel, self._transport = None, None
        # Channel.close は切断済みの相手への通知で例外になりうるので呼ばない。Transport.close は
        # 通知を送らずにソケットを閉じ、チャネルもまとめて閉じる。
        if transport is not None:
            transport.close()

    def _remaining(self) -> float:
        """要求ごとの期限までの残り時間 (秒)。期限を過ぎていれば TimeoutError。"""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("No TMP response before the timeout")
        return remaining

    def _abort(self) -> None:
        """期限に、別の thread から transport を閉じてブロック中の I/O を解く。"""
        transport = self._transport
        if transport is not None:
            transport.close()

    def _channel_until_deadline(self) -> paramiko.Channel:
        """
        要求ごとの期限までの残り時間を timeout に設定したチャネル。

        keep-alive や通知が届き続けても、送受信の待ちを要求の期限で打ち切るため、読み書きのたびに呼ぶ。
        """
        channel = cast("paramiko.Channel", self._channel)
        channel.settimeout(self._remaining())
        return channel

    def _send_all(self, data: bytes) -> None:
        # Channel.sendall は部分送信ごとに同じ timeout で待つので、送信全体が期限を超えうる。
        # 部分送信のたびに残り時間を設定し直す。
        while data:
            sent = self._channel_until_deadline().send(data)
            if sent == 0:
                raise EOFError("TMP channel closed by Deco")
            data = data[sent:]

    def _send(self, payload: bytes) -> None:
        self._seq += 1
        self._send_all(_tmp_packet(payload, seq=self._seq))

    def _receive_exact(self, size: int) -> bytes:
        buffer = b""
        while len(buffer) < size:
            chunk = self._channel_until_deadline().recv(size - len(buffer))
            if not chunk:
                raise EOFError("TMP channel closed by Deco")
            buffer += chunk
        return buffer

    def _receive_payload(self) -> bytes:
        """次の data packet の payload。keep-alive には応答し、接続の ACK は読み捨てる。"""
        while True:
            head = self._receive_exact(4)
            packet_type = head[2]
            if packet_type == _TMP_TYPE_KEEPALIVE:
                self._send_all(bytes([1, 1, 4, 0]))
                continue
            if packet_type == _TMP_TYPE_ACK:
                continue
            if packet_type != _TMP_TYPE_DATA:
                raise DecoError(f"Unexpected TMP packet type {packet_type}")
            rest = self._receive_exact(_TMP_HEADER.size - 4)
            (length,) = struct.unpack_from(">H", rest)
            return self._receive_exact(length)

    def _receive_response(self, *, opcode: int, txid: int) -> bytes:
        """要求 (opcode・txid) への応答を、分割されていれば PULL で続きを取ってつないで返す。"""
        data = bytearray()
        expected_total: int | None = None
        while True:
            payload = self._receive_payload()
            if len(payload) < _BUSINESS_HEADER.size:
                raise DecoError(f"Truncated TMP response for opcode {opcode:#06x}")
            _, _, reply_opcode, ptype, err, reply_txid, _, total, offset = (
                _BUSINESS_HEADER.unpack_from(payload)
            )
            # 別の取引の応答やルーターからの通知 (クライアントの接続イベント等) は読み捨てる。
            # 通知の txid はルーター側の番号なので、要求の txid と重なりうる。
            if (reply_opcode, reply_txid) != (opcode, txid):
                continue
            if err != 0:
                raise DecoError(
                    f"Deco rejected TMP opcode {opcode:#06x} (err {err})",
                    error_code=err,
                )
            if ptype != _PTYPE_RESPONSE:
                raise DecoError(
                    f"Unexpected TMP response type {ptype} for opcode {opcode:#06x}"
                )
            fragment = payload[_BUSINESS_HEADER.size :]
            if expected_total is None:
                if total > _MAX_RESPONSE:
                    raise DecoError(
                        f"TMP response for opcode {opcode:#06x} is too large ({total} bytes)"
                    )
                expected_total = total
            consistent = (
                total == expected_total
                and offset == len(data)
                and (fragment or total == 0)
                and len(data) + len(fragment) <= total
            )
            if not consistent:
                raise DecoError(
                    f"Inconsistent TMP response fragments for opcode {opcode:#06x}"
                )
            data += fragment
            if len(data) == total:
                return bytes(data)
            self._send(
                _business_packet(
                    opcode=opcode, ptype=_PTYPE_PULL, txid=txid, offset=len(data)
                )
            )

    def _exchange(self, opcode: int, *, body: bytes) -> bytes:
        """要求を 1 つ送り、応答の本文を返す。失敗したらセッションを捨てる。"""
        txid = self._txid
        self._txid = (self._txid + 1) & 0xFFFF
        try:
            self._send(
                _business_packet(
                    opcode=opcode, ptype=_PTYPE_REQUEST, txid=txid, body=body
                )
            )
            return self._receive_response(opcode=opcode, txid=txid)
        except (OSError, EOFError, paramiko.SSHException) as err:
            # 要求が届いたか分からないので、セッションを捨てて呼び出し元に失敗を返す (再送はしない)。
            self.close()
            raise DecoConnectionError(
                f"TMP session to {self.host} failed: {type(err).__name__}: {err}"
            ) from err
        except DecoError:
            self.close()
            raise

    def call(self, opcode: int, *, params: dict[str, Any] | None = None) -> Any:
        """
        opcode を呼び、成功応答の `result` を返す。

        Raises:
            DecoAuthError: TP-Link ID の email・パスワードが合わない (一度拒否されたら、起動し直すまで
                ルーターに接続せずにこの例外を返す)。
            DecoConnectionError: 接続できない、要求の途中でセッションが切れた、または期限内に応答が無い。
            DecoRequestTooLargeError: 要求の本文が 1 つの packet に収まらない。
            DecoError: ルーターがエラー (error_code) や不正な応答を返した。
        """
        # アプリと同じく、設定の版 (ミリ秒の時刻) を毎回送る。ルーターは write の後にこの値で
        # ほかのノードへの設定の同期を判断する。
        request: dict[str, Any] = {"config_version": time.time_ns() // 1_000_000}
        if params is not None:
            request["params"] = params
        body = json.dumps(request).encode()
        if _BUSINESS_HEADER.size + len(body) > _MAX_PAYLOAD:
            raise DecoRequestTooLargeError(
                f"TMP request for opcode {opcode:#06x} is {len(body)} bytes; "
                f"at most {_MAX_PAYLOAD - _BUSINESS_HEADER.size} bytes fit in one packet"
            )
        # 接続 (TCP・SSH の鍵交換と認証・チャネル・セッションの割り当て) から応答までを 1 つの期限で
        # 打ち切る。チャネルの timeout は SSH の鍵交換の待ちなど transport の中の待機には効かないので、
        # 期限に transport を閉じてブロック中の I/O も解く。
        self._deadline = time.monotonic() + self._timeout
        watchdog = threading.Timer(self._timeout, self._abort)
        watchdog.start()
        try:
            if not self.connected:
                self._connect()
            body = self._exchange(opcode, body=body)
        finally:
            watchdog.cancel()
        try:
            # JSON の構文は孤立した surrogate や NaN も通すが、API の応答に載せられないので拒否する。
            response: Any = json.loads(body)
            json.dumps(response, ensure_ascii=False, allow_nan=False).encode()
        except ValueError as err:
            raise DecoError(f"Invalid TMP response for opcode {opcode:#06x}") from err
        if not isinstance(response, dict):
            raise DecoError(f"Invalid TMP response for opcode {opcode:#06x}")
        envelope = cast("dict[str, Any]", response)
        error_code = envelope.get("error_code")
        if type(error_code) is not int or error_code != 0:
            raise DecoError(
                f"Deco TMP opcode {opcode:#06x} failed: {envelope}",
                error_code=error_code if type(error_code) is int else None,
            )
        return envelope.get("result")

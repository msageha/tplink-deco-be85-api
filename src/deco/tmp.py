"""
Deco アプリが使う TMP (Tether Management Protocol) のクライアント。

FW 1.3.0 の Deco は SSH (dropbear) を閉じ、Deco アプリは tpapsvr (TPAP) 経由で TMP を話す。
クライアントは Deco の TLS (port 30001) につなぎ、TP-Link ID で SPAKE2+ 認証したあと、同じ TLS
接続上の "TMP" チャネルに、ルーター内の TMP のデーモン (127.0.0.1:20002) 宛ての packet を載せる。

3 つの層からなる。
- TSCP: 24 byte の header を持つ外側の packet。auth は平文 JSON、data はセッション鍵で AES-128-CCM
  暗号化した本文を運ぶ。数値は length / checksum が big-endian、seq が little-endian。
- TMP: data の本文に入る 16 byte の header + payload。tpapsvr が tmpsvr へそのまま中継する。
- business: TMP の payload に入る 20 byte の header + JSON。opcode ごとに要求と応答をやりとりする。
TMP と business の framing は SSH の経路と同じで、数値はすべて big-endian。
"""

import contextlib
import json
import socket
import ssl
import struct
import threading
import time
import zlib
from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from secrets import randbits, token_bytes
from typing import Any, NamedTuple, cast
from urllib.parse import urlsplit

from deco.crypto import TmpSessionCipher
from deco.exceptions import (
    DecoAuthError,
    DecoConnectionError,
    DecoError,
    DecoRequestTooLargeError,
)
from deco.spake2p import Spake2pProver, build_credential, sha256_hex

# TSCP の header: version 1・flags 0x01、type、予約、payload 長 (BE)、チャネル名 8 byte、
# seq (LE)、checksum (BE CRC32)。length と checksum は big-endian、seq は little-endian。
_TSCP_HEADER_SIZE = 24
_TSCP_TYPE_AUTH = 1
_TSCP_TYPE_DATA = 2
_TSCP_CHANNEL_TMP = b"TMP\x00\x00\x00\x00\x00"
# checksum の欄にこの値を big-endian で入れた packet 全体から CRC32 を計算する。
_TSCP_CRC_MAGIC = 0x92BF30ED
# tpapsvr が受け付ける payload の上限 (session_read_packet)。壊れた長さでメモリを使い切らない。
_MAX_TSCP_PAYLOAD = 0x10010

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
_TMP_KEEPALIVE = bytes([1, 1, _TMP_TYPE_KEEPALIVE, 0])
# TMP packet の種類を読むのに要る最小の長さ (version・flags・type・予約)。
_TMP_PEEK = 4
# business の header: version 1・2、opcode、ptype、err、txid、本文全体の CRC32、全体長、offset。
_BUSINESS_HEADER = struct.Struct(">BBHBBHIII")
# AES-128-CCM の tag 長 (TSCP の data packet の暗号文に続く)。
_TMP_TAG_SIZE = 16
# 1 つの要求に載せられる本文の上限。business header と本文は TMP packet (16 byte header) の 16 bit の
# payload 長に収まり、さらにそれを暗号化して tag を付けた TSCP の payload が _MAX_TSCP_PAYLOAD に
# 収まる必要がある。両方の制約の厳しい方で頭打ちにする。
_MAX_BODY = min(
    _MAX_PAYLOAD - _BUSINESS_HEADER.size,
    _MAX_TSCP_PAYLOAD - _TMP_TAG_SIZE - _TMP_HEADER.size - _BUSINESS_HEADER.size,
)
_PTYPE_REQUEST = 2
_PTYPE_PULL = 4
_PTYPE_RESPONSE = 5
# セッションの割り当て。本文なしで送り、応答の token は以後の要求に使わない。
_OPCODE_ALLOC = 0x0001
_OPCODE_WLAN_READ = 0x4009
_OPCODE_WLAN_WRITE = 0x4008
# 無線の設定のうち、band の radio (network によらず band で 1 つ) に属するものと、その TMP での名前。
_RADIO_FIELDS = {"channel": "channel", "channel_width": "htmode", "mode": "hwmode"}


def _tmp_crc(header: bytes, *, payload: bytes) -> int:
    """TMP の packet の CRC32。header の CRC の欄を placeholder に置き換えて計算する。"""
    return zlib.crc32(header[:-4] + struct.pack(">I", _CRC_PLACEHOLDER) + payload)


def _tmp_packet(payload: bytes, *, seq: int) -> bytes:
    header = _TMP_HEADER.pack(1, 1, _TMP_TYPE_DATA, 0, len(payload), 0, 0, seq, 0)
    return header[:-4] + struct.pack(">I", _tmp_crc(header, payload=payload)) + payload


def _business_packet(
    *, opcode: int, ptype: int, txid: int, body: bytes = b"", offset: int = 0
) -> bytes:
    crc = zlib.crc32(body) if body else 0
    header = _BUSINESS_HEADER.pack(1, 2, opcode, ptype, 0, txid, crc, len(body), offset)
    return header + body


def _tscp_packet(
    payload: bytes, *, packet_type: int, channel: bytes, seq: int
) -> bytes:
    """TSCP の packet を組む。checksum は magic を仮置きした packet 全体の CRC32。"""
    header = (
        bytes([1, 1, packet_type, 0])
        + struct.pack(">I", len(payload))
        + channel
        + struct.pack("<I", seq)
        + struct.pack(">I", _TSCP_CRC_MAGIC)
    )
    crc = zlib.crc32(header + payload)
    return header[:20] + struct.pack(">I", crc) + payload


class TmpCredentials(NamedTuple):
    """TMP の SPAKE2+ 認証に使う、Deco のオーナーの TP-Link ID。"""

    email: str
    password: str


class TmpClient:
    """1 台の Deco に対する TMP のセッションを保持する同期クライアント。並行利用はできない。"""

    def __init__(
        self,
        url: str,
        *,
        credentials: TmpCredentials,
        port: int = 30001,
        timeout: int = 30,
    ) -> None:
        """
        url は luci の Web API と同じ Deco の URL で、そのホストの port に TLS で接続する。

        SPAKE2+ の username は TP-Link ID の email の SHA-256 (hex)。パスワードは TP-Link ID の
        パスワードで、SPAKE2+ (PAKE) がルーターに保存されたオーナーの verifier と照合する。PAKE
        なので、正しい verifier を持たない接続先はセッションを確立できず、パスワードは渡らない。

        Raises:
            ValueError: url にホストが無い。
        """
        hostname = urlsplit(url).hostname
        if not hostname:
            raise ValueError(f"Deco URL has no host: {url!r}")
        self.host = hostname
        self._port = port
        self._email = credentials.email
        self._username = sha256_hex(credentials.email)
        self._password = credentials.password
        self._timeout = timeout
        # ルーターが TLS 証明書を検証しないのと同じく、検証しない。SPAKE2+ が接続先を認証する。
        self._tls = ssl.create_default_context()
        self._tls.check_hostname = False
        self._tls.verify_mode = ssl.CERT_NONE
        self._socket: ssl.SSLSocket | None = None
        self._cipher: TmpSessionCipher | None = None
        self._tscp_seq = 0
        self._seq = 0
        self._txid = 0
        self._deadline = 0.0
        # ルーターが資格情報を拒否したら、設定を直して起動し直すまで認証を送らない。
        # 誤った資格情報での再試行は、ルーターの失敗回数の上限を消費するため。
        self._rejected = False

    @property
    def connected(self) -> bool:
        """セッションがあるか (ソケットに触れない field だけの判定)。"""
        return self._socket is not None and self._cipher is not None

    def check_connected(self) -> bool:
        """
        生きたセッションがあるかを確かめ、相手に閉じられていれば捨てる。

        ソケットを読むので、要求を直列化する lock を持つ worker thread からだけ呼ぶ (health も
        DecoService 経由でこの thread で呼ぶ)。ルーターへは何も送らない。
        """
        if self.connected and self._session_lost():
            self.close()
        return self.connected

    def _session_lost(self) -> bool:
        """
        張ってあるセッションが相手に閉じられていないかを、ソケットに書かずに確かめる。

        要求の合間は data が来ないので、non-blocking な recv が EOF (閉じられた) か想定外の data を
        返したら作り直す。
        """
        sock = self._socket
        if sock is None:
            return True
        sock.setblocking(False)  # noqa: FBT003 - socket API の引数
        try:
            sock.recv(1)
        except ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError:
            return False
        except OSError, ssl.SSLError:
            return True
        else:
            # 要求の合間に読めるものがあるのは、EOF (閉じられた) か想定外の data。どちらも作り直す。
            return True
        finally:
            with contextlib.suppress(OSError):
                sock.setblocking(True)  # noqa: FBT003 - socket API の引数

    def _connect(self) -> None:
        if self._rejected:
            raise DecoAuthError(
                "TMP login was rejected; fix TPLINK_EMAIL / PASSWORD and restart the server"
            )
        self.close()
        try:
            raw = self._open_socket()
        except OSError as err:
            raise DecoConnectionError(
                f"Cannot reach Deco TMP at {self.host}:{self._port}: {err}"
            ) from err
        try:
            # TCP の接続に時間がかかった後でも、TLS handshake を期限の残り時間で打ち切る。
            # handshake 完了まで self._socket が未設定で watchdog が効かないため。
            raw.settimeout(self._remaining())
            sock = self._tls.wrap_socket(raw, server_hostname=self.host)
        except (OSError, ssl.SSLError) as err:
            raw.close()
            raise DecoConnectionError(
                f"Cannot establish TLS to Deco TMP at {self.host}:{self._port}: {err}"
            ) from err
        self._socket = sock
        self._tscp_seq = randbits(32)
        self._seq = 0
        self._txid = 0
        self._cipher = self._authenticate()
        self._exchange(_OPCODE_ALLOC, body=b"")

    def _authenticate(self) -> TmpSessionCipher:
        """
        SPAKE2+ でセッション鍵を確立する。

        pake_register の username は email の SHA-256 (hex) で、device はこれで tpap_authmgmt の
        オーナーのアカウントを引く。SPAKE2+ の credential は `<email>/<password>` で、device は
        登録時に同じ文字列から verifier を作って保存する (BE85 FW 1.3.0 の accountmgnt.lua)。

        Raises:
            DecoAuthError: 認証できない (起動し直すまで接続しない)。
            DecoConnectionError: 接続が途中で切れた、または応答が不正。
        """
        try:
            return self._spake2p_login()
        except DecoAuthError:
            self._rejected = True
            raise

    def _spake2p_login(self) -> TmpSessionCipher:
        """
        pake_register → pake_share でセッション鍵を導く。

        Raises:
            DecoAuthError: credential が合わない (応答の dev_confirm が一致しない等)。
            DecoConnectionError: 接続が途中で切れた、または応答が不正。
        """
        user_random = b64encode(token_bytes(32)).decode()
        register = self._auth_round(
            {
                "sub_method": "pake_register",
                "username": self._username,
                "user_random": user_random,
                "cipher_suites": [1],
                "encryption": ["aes_128_ccm"],
                "passcode_type": "userpw",
            }
        )
        result = self._auth_result(register)
        credential = build_credential(email=self._email, password=self._password)
        try:
            prover = Spake2pProver(
                result,
                user_random=user_random,
                credential=credential,
                random_scalar=randbits(256),
            )
        except (
            KeyError,
            ValueError,
            TypeError,
            IndexError,
            OverflowError,
            Base64Error,
        ) as err:
            raise DecoConnectionError(
                f"Deco sent an unusable pake_register response: {err}"
            ) from err
        share = self._auth_round(
            {
                "sub_method": "pake_share",
                "user_share": b64encode(prover.user_share).decode(),
                "user_confirm": b64encode(prover.user_confirm).decode(),
            }
        )
        shared = self._auth_result(share)
        try:
            dev_confirm = b64decode(cast("str", shared["dev_confirm"]), validate=True)
        except (KeyError, TypeError, Base64Error) as err:
            raise DecoConnectionError(
                f"Deco sent an unusable pake_share response: {err}"
            ) from err
        if not prover.device_confirm_ok(dev_confirm):
            # verifier が一致しない。偽の接続先か、passcode が合わない。
            raise DecoAuthError("TMP login failed: device confirmation did not match")
        key, nonce_base = prover.session_key_and_nonce()
        return TmpSessionCipher(key, nonce_base=nonce_base)

    def _auth_round(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        auth packet (平文 JSON) を 1 つ送り、応答の JSON を返す。

        Raises:
            DecoConnectionError: 認証の途中で接続が切れた (資格情報の不一致ではないので、拒否として
                覚えない)。
        """
        request = {"method": "login", "params": params}
        try:
            self._send_tscp(
                json.dumps(request).encode(),
                packet_type=_TSCP_TYPE_AUTH,
                channel=bytes(8),
            )
            payload = self._receive_tscp(_TSCP_TYPE_AUTH)
        except (OSError, EOFError, ssl.SSLError) as err:
            raise DecoConnectionError(
                f"TMP login to {self.host}:{self._port} did not complete: "
                f"{type(err).__name__}: {err}"
            ) from err
        try:
            response: Any = json.loads(payload)
        except ValueError as err:
            raise DecoConnectionError("Deco sent an invalid TMP auth response") from err
        if not isinstance(response, dict):
            raise DecoConnectionError("Deco sent an invalid TMP auth response")
        return cast("dict[str, Any]", response)

    @staticmethod
    def _auth_result(response: dict[str, Any]) -> dict[str, Any]:
        """
        auth の応答から `result` を取り出す。

        資格情報の拒否 (error_code が 0 でない) と、応答の形式の不正 (result が無い等) を分ける。
        後者を拒否として扱うと、1 度の不正な応答で起動し直すまで全 TMP 操作が 401 になるため。

        Raises:
            DecoAuthError: ルーターが認証を拒否した (error_code が 0 でない)。
            DecoConnectionError: 応答の形式が不正 (成功コードだが result が無い等)。
        """
        error_code = response.get("error_code")
        if error_code not in (0, None):
            raise DecoAuthError(f"TMP login was refused by Deco: {response}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise DecoConnectionError(
                f"Deco sent an invalid TMP auth response: {response}"
            )
        return cast("dict[str, Any]", result)

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
        sock = self._socket
        self._socket, self._cipher = None, None
        if sock is not None:
            # 切断済みの相手への TLS の close_notify で例外になりうるので、ソケットだけ閉じる。
            with contextlib.suppress(OSError):
                sock.close()

    def _remaining(self) -> float:
        """要求ごとの期限までの残り時間 (秒)。期限を過ぎていれば TimeoutError。"""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("No TMP response before the timeout")
        return remaining

    def _abort(self) -> None:
        """期限に、別の thread からソケットを閉じてブロック中の I/O を解く。"""
        sock = self._socket
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    def _socket_until_deadline(self) -> ssl.SSLSocket:
        """読み書きの待ちを要求の期限で打ち切るため、読み書きのたびに timeout を設定し直す。"""
        sock = cast("ssl.SSLSocket", self._socket)
        sock.settimeout(self._remaining())
        return sock

    def _send_all(self, data: bytes) -> None:
        while data:
            sent = self._socket_until_deadline().send(data)
            if sent == 0:
                raise EOFError("TMP connection closed by Deco")
            data = data[sent:]

    def _receive_exact(self, size: int) -> bytes:
        buffer = b""
        while len(buffer) < size:
            chunk = self._socket_until_deadline().recv(size - len(buffer))
            if not chunk:
                raise EOFError("TMP connection closed by Deco")
            buffer += chunk
        return buffer

    def _send_tscp(self, payload: bytes, *, packet_type: int, channel: bytes) -> None:
        """TSCP packet を 1 つ送る。data は本文をセッション鍵で暗号化する。"""
        seq = self._tscp_seq
        self._tscp_seq = (self._tscp_seq + 1) & 0xFFFFFFFF
        if packet_type == _TSCP_TYPE_DATA:
            payload = cast("TmpSessionCipher", self._cipher).encrypt(payload, seq=seq)
        self._send_all(
            _tscp_packet(payload, packet_type=packet_type, channel=channel, seq=seq)
        )

    def _receive_tscp(self, expected_type: int) -> bytes:
        """
        次の TSCP packet を読み、本文を返す。data は復号する。

        Raises:
            DecoError: packet の種類・version・checksum が合わない、または復号に失敗した。
        """
        header = self._receive_exact(_TSCP_HEADER_SIZE)
        length = struct.unpack_from(">I", header, 4)[0]
        seq = struct.unpack_from("<I", header, 16)[0]
        checksum = struct.unpack_from(">I", header, 20)[0]
        if header[0] != 1 or header[2] != expected_type:
            raise DecoError(
                f"Unexpected TMP packet (version {header[0]}, type {header[2]})"
            )
        if length > _MAX_TSCP_PAYLOAD:
            raise DecoError(f"TMP packet from Deco is too long ({length} bytes)")
        payload = self._receive_exact(length)
        recomputed = zlib.crc32(
            header[:20] + struct.pack(">I", _TSCP_CRC_MAGIC) + payload
        )
        if recomputed != checksum:
            raise DecoError("Corrupt TMP packet from Deco")
        if expected_type != _TSCP_TYPE_DATA:
            return payload
        try:
            return cast("TmpSessionCipher", self._cipher).decrypt(payload, seq=seq)
        except ValueError as err:
            raise DecoError(f"Could not decrypt TMP packet from Deco: {err}") from err

    def _send(self, payload: bytes) -> None:
        """business packet を TMP packet に包み、data の TSCP packet で送る。"""
        self._seq += 1
        self._send_tscp(
            _tmp_packet(payload, seq=self._seq),
            packet_type=_TSCP_TYPE_DATA,
            channel=_TSCP_CHANNEL_TMP,
        )

    def _receive_payload(self) -> bytes:
        """次の business の payload。keep-alive には応答し、接続の ACK は読み捨てる。"""
        while True:
            packet = self._receive_tscp(_TSCP_TYPE_DATA)
            if len(packet) < _TMP_PEEK:
                raise DecoError("Truncated TMP packet from Deco")
            packet_type = packet[2]
            if packet_type == _TMP_TYPE_KEEPALIVE:
                self._send_tscp(
                    _TMP_KEEPALIVE,
                    packet_type=_TSCP_TYPE_DATA,
                    channel=_TSCP_CHANNEL_TMP,
                )
                continue
            if packet_type == _TMP_TYPE_ACK:
                continue
            if packet_type != _TMP_TYPE_DATA:
                raise DecoError(f"Unexpected TMP packet type {packet_type}")
            if len(packet) < _TMP_HEADER.size:
                raise DecoError("Truncated TMP packet from Deco")
            *fields, crc = _TMP_HEADER.unpack_from(packet)
            header = packet[: _TMP_HEADER.size]
            payload = packet[_TMP_HEADER.size :]
            if (
                fields[:4] != [1, 1, _TMP_TYPE_DATA, 0]
                or len(payload) != fields[4]
                or _tmp_crc(header, payload=payload) != crc
            ):
                raise DecoError("Corrupt TMP packet from Deco")
            return payload

    @staticmethod
    def _parse_response(
        payload: bytes, *, opcode: int, txid: int
    ) -> tuple[bytes, int, int, int] | None:
        """
        business packet を (本文の断片, 本文全体の CRC32, 全体長, offset) に分ける。

        要求 (opcode・txid) と別の取引の packet なら None。

        Raises:
            DecoError: ルーターが要求を拒否した、または応答の形式が不正。
        """
        if len(payload) < _BUSINESS_HEADER.size:
            raise DecoError(f"Truncated TMP response for opcode {opcode:#06x}")
        (
            version,
            business_version,
            reply_opcode,
            ptype,
            err,
            reply_txid,
            body_crc,
            total,
            offset,
        ) = _BUSINESS_HEADER.unpack_from(payload)
        # 別の取引の応答やルーターからの通知 (クライアントの接続イベント等) は読み捨てる。
        # 通知の txid はルーター側の番号なので、要求の txid と重なりうる。
        if (reply_opcode, reply_txid) != (opcode, txid):
            return None
        if err != 0:
            raise DecoError(
                f"Deco rejected TMP opcode {opcode:#06x} (err {err})", error_code=err
            )
        if (version, business_version) != (1, 2) or ptype != _PTYPE_RESPONSE:
            raise DecoError(
                f"Unexpected TMP response type {ptype} for opcode {opcode:#06x}"
            )
        return payload[_BUSINESS_HEADER.size :], body_crc, total, offset

    def _receive_response(self, *, opcode: int, txid: int) -> bytes:
        """要求 (opcode・txid) への応答を、分割されていれば PULL で続きを取ってつないで返す。"""
        data = bytearray()
        # 先頭の断片が宣言した (全体長, 本文全体の CRC32)。後の断片も同じ値を持つ。
        declared: tuple[int, int] | None = None
        while True:
            parsed = self._parse_response(
                self._receive_payload(), opcode=opcode, txid=txid
            )
            if parsed is None:
                continue
            fragment, body_crc, total, offset = parsed
            if declared is None:
                if total > _MAX_RESPONSE:
                    raise DecoError(
                        f"TMP response for opcode {opcode:#06x} is too large ({total} bytes)"
                    )
                declared = (total, body_crc)
            consistent = (
                (total, body_crc) == declared
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
                # 分割された応答でも、business の CRC32 は断片ではなく本文全体のもの。
                if total and zlib.crc32(data) != body_crc:
                    raise DecoError(f"Corrupt TMP response for opcode {opcode:#06x}")
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
        except (OSError, EOFError, ssl.SSLError) as err:
            # 要求が届いたか分からないので、セッションを捨てて呼び出し元に失敗を返す (再送はしない)。
            self.close()
            raise DecoConnectionError(
                f"TMP session to {self.host} failed: {type(err).__name__}: {err}"
            ) from err
        except DecoError:
            self.close()
            raise

    def set_wlan(self, *, band: str, network: str, settings: dict[str, Any]) -> Any:
        """
        band の 1 つの network (host / guest) の Wi-Fi 設定と、その band の radio の設定を書き込む。

        luci の `admin/wireless?form=wlan` の write は guest の書き込みを HTTP 500 で落とすので、
        Deco アプリと同じ TMP の経路で書く。settings の名前は luci の read の形 (`GET /api/wireless`)
        に合わせ、TMP の形に置き換えて送る: ssid / password は平文を受け取って base64 にし、
        channel / channel_width / mode は band の radio (channel / htmode / hwmode) に書く。
        TMP が文字列で持つ数値 (channel・access_duration・bandwidth_limit の上限) は文字列にする。
        """
        params: dict[str, Any] = {}
        network_settings: dict[str, Any] = {}
        radio: dict[str, Any] = {}
        for key, value in settings.items():
            if key in _RADIO_FIELDS:
                radio[_RADIO_FIELDS[key]] = str(value) if key == "channel" else value
            elif key in ("ssid", "password"):
                network_settings[key] = b64encode(value.encode()).decode()
            elif key == "access_duration":
                network_settings[key] = str(value)
            elif key == "bandwidth_limit":
                network_settings[key] = {
                    k: v if k == "enable" else str(v) for k, v in value.items()
                }
            else:
                network_settings[key] = value
        if network_settings:
            params[network] = network_settings
        if radio:
            params["radio"] = radio
        return self.call(_OPCODE_WLAN_WRITE, params={band: params})

    def set_guest_enable(self, *, enable: bool) -> list[str]:
        """
        guest が別扱い (`is_separate`) でない band のゲストを、1 回の write でまとめて切り替える。

        `is_separate` が true の guest (BE85 の 6 GHz) は Deco アプリでも別に設定するゲストで、
        未設定のままでもパスワードの無い OWE の設定を持ちうるので、ここでは切り替えない。
        `is_separate` を持たない guest は別扱いでないとみなす (BE85 の 2.4 / 5 GHz は持たない)。
        切り替えた band を返し、対象が無ければ書き込まずに空のリストを返す。

        Raises:
            DecoError: wlan の read の応答が不正。一部の band だけを切り替えて成功を返さないよう、
                書き込む前に止める。
        """
        wlan = self.call(_OPCODE_WLAN_READ)
        if not isinstance(wlan, dict):
            raise DecoError("Unexpected TMP wlan response: not a JSON object")
        bands: list[str] = []
        for band, config in cast("dict[str, Any]", wlan).items():
            if not band.startswith("band"):
                continue
            if not isinstance(config, dict):
                raise DecoError(
                    f"Unexpected TMP wlan response: '{band}' is not an object"
                )
            guest = cast("dict[str, Any]", config).get("guest", [])
            if guest == []:  # guest の無い band。空の Lua テーブルは [] で返る
                continue
            if not isinstance(guest, dict):
                raise DecoError(
                    f"Unexpected TMP wlan response: '{band}.guest' is not an object"
                )
            is_separate = cast("dict[str, Any]", guest).get("is_separate", False)
            if not isinstance(is_separate, bool):
                raise DecoError(
                    f"Unexpected TMP wlan response: '{band}.guest.is_separate' is not a boolean"
                )
            if not is_separate:
                bands.append(band)
        if bands:
            self.call(
                _OPCODE_WLAN_WRITE,
                params={band: {"guest": {"enable": enable}} for band in bands},
            )
        return bands

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
        if len(body) > _MAX_BODY:
            raise DecoRequestTooLargeError(
                f"TMP request for opcode {opcode:#06x} is {len(body)} bytes; "
                f"at most {_MAX_BODY} bytes fit in one packet"
            )
        # 接続 (TCP・TLS・SPAKE2+ 認証・セッションの割り当て) から応答までを 1 つの期限で打ち切る。
        # ソケットの timeout は TLS handshake の中の待機には効かないので、期限にソケットを閉じて
        # ブロック中の I/O も解く。
        self._deadline = time.monotonic() + self._timeout
        watchdog = threading.Timer(self._timeout, self._abort)
        watchdog.start()
        try:
            if not self.check_connected():
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

"""Deco ローカル API の暗号化。本文は AES-128-CBC、署名とパスワードは RSA (PKCS#1 v1.5)。"""

import struct
from base64 import b64decode, b64encode

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad

# 署名用 RSA 鍵は 512 bit なので、PKCS#1 v1.5 で一度に暗号化できるのは 64 - 11 = 53 byte。
_SIGN_CHUNK = 53


def _public_key(key: tuple[str, str]) -> RSA.RsaKey:
    modulus_hex, exponent_hex = key
    return RSA.construct((int(modulus_hex, 16), int(exponent_hex, 16)))


def rsa_encrypt(text: str, *, public_key: tuple[str, str]) -> str:
    cipher = PKCS1_v1_5.new(_public_key(public_key))
    return cipher.encrypt(text.encode()).hex()


class SessionCipher:
    """ログインセッション 1 つ分の AES 鍵 / IV と署名材料を持ち、本文の暗号化・署名・復号を行う。"""

    def __init__(self, *, sign_key: tuple[str, str], seq: int, cred_hash: str) -> None:
        # Web UI と同じく、8 byte の乱数を hex 化した 16 文字をそのまま AES-128 の鍵 / IV にする。
        self._key = get_random_bytes(8).hex().encode()
        self._iv = get_random_bytes(8).hex().encode()
        self._signer = PKCS1_v1_5.new(_public_key(sign_key))
        self._seq = seq
        self._cred_hash = cred_hash

    def encrypt(self, plaintext: str, *, login: bool = False) -> dict[str, str]:
        """
        本文を暗号化し、フォームフィールド `sign` と `data` を返す。

        署名は `h=<認証 hash>&s=<seq + 暗号文長>` を RSA 暗号化したもの。login 時だけ
        AES 鍵 / IV (`k=`, `i=`) を先頭に含めてルーターへ渡す。
        """
        cipher = AES.new(self._key, AES.MODE_CBC, self._iv)
        data = b64encode(
            cipher.encrypt(pad(plaintext.encode(), AES.block_size))
        ).decode()
        fields = f"h={self._cred_hash}&s={self._seq + len(data)}"
        if login:
            fields = f"k={self._key.decode()}&i={self._iv.decode()}&{fields}"
        sign = "".join(
            self._signer.encrypt(fields[i : i + _SIGN_CHUNK].encode()).hex()
            for i in range(0, len(fields), _SIGN_CHUNK)
        )
        return {"sign": sign, "data": data}

    def decrypt(self, data: str) -> str:
        cipher = AES.new(self._key, AES.MODE_CBC, self._iv)
        return unpad(cipher.decrypt(b64decode(data)), AES.block_size).decode()


# TMP (TPAP) の data packet を守る tag の長さ。
_TMP_TAG_SIZE = 16


class TmpSessionCipher:
    """
    TPAP の SPAKE2+ で得た鍵で、TMP の data packet を AES-128-CCM で暗号化・復号する。

    packet ごとの nonce は、鍵導出で得た 8 byte に packet の seq (little-endian uint32) を
    つないだ 12 byte。送信・応答とも、その packet の seq から nonce を作る。
    """

    def __init__(self, key: bytes, *, nonce_base: bytes) -> None:
        self._key = key
        self._nonce_base = nonce_base

    def _nonce(self, seq: int) -> bytes:
        return self._nonce_base + struct.pack("<I", seq & 0xFFFFFFFF)

    def encrypt(self, plaintext: bytes, *, seq: int) -> bytes:
        """暗号文に 16 byte の tag をつないで返す。"""
        cipher = AES.new(
            self._key, AES.MODE_CCM, nonce=self._nonce(seq), mac_len=_TMP_TAG_SIZE
        )
        return cipher.encrypt(plaintext) + cipher.digest()

    def decrypt(self, data: bytes, *, seq: int) -> bytes:
        """
        暗号文 + 16 byte の tag を復号する。

        Raises:
            ValueError: tag が合わない、またはデータが tag より短い。
        """
        if len(data) < _TMP_TAG_SIZE:
            raise ValueError("TMP data packet is shorter than its authentication tag")
        cipher = AES.new(
            self._key, AES.MODE_CCM, nonce=self._nonce(seq), mac_len=_TMP_TAG_SIZE
        )
        return cipher.decrypt_and_verify(data[:-_TMP_TAG_SIZE], data[-_TMP_TAG_SIZE:])

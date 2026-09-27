"""Deco ローカル API の暗号化。本文は AES-128-CBC、署名とパスワードは RSA (PKCS#1 v1.5)。"""

from base64 import b64decode, b64encode

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad

# 署名用 RSA 鍵は 512 bit なので、PKCS#1 v1.5 で一度に暗号化できるのは 64 - 11 = 53 byte。
_SIGN_CHUNK = 53


def _public_key(modulus_hex: str, exponent_hex: str) -> RSA.RsaKey:
    return RSA.construct((int(modulus_hex, 16), int(exponent_hex, 16)))


def rsa_encrypt(text: str, modulus_hex: str, exponent_hex: str) -> str:
    key = PKCS1_v1_5.new(_public_key(modulus_hex, exponent_hex))
    return key.encrypt(text.encode()).hex()


class SessionCipher:
    """ログインセッション 1 つ分の AES 鍵 / IV と署名材料を持ち、本文の暗号化・署名・復号を行う。"""

    def __init__(self, sign_key: tuple[str, str], seq: int, cred_hash: str) -> None:
        # Web UI と同じく、8 byte の乱数を hex 化した 16 文字をそのまま AES-128 の鍵 / IV にする。
        self._key = get_random_bytes(8).hex().encode()
        self._iv = get_random_bytes(8).hex().encode()
        self._signer = PKCS1_v1_5.new(_public_key(*sign_key))
        self._seq = seq
        self._cred_hash = cred_hash

    def encrypt(self, plaintext: str, *, login: bool = False) -> dict[str, str]:
        """本文を暗号化し、フォームフィールド `sign` と `data` を返す。

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

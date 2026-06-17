"""Encryption used by the TP-Link Deco local web API.

Login uses two RSA public keys served by the router:

* ``form=keys`` -> RSA key used to encrypt the account password.
* ``form=auth`` -> RSA key (plus a ``seq`` number) used to sign every request.

Each session generates a random AES-128-CBC key/iv. The request body is AES
encrypted, and a signature carrying the AES key/iv, an md5 credential hash and
``seq + len(body)`` is RSA encrypted in 53-byte chunks (PKCS#1 v1.5).
"""

from base64 import b64decode, b64encode
from binascii import b2a_hex

from Crypto import Random
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey.RSA import construct

_RSA_CHUNK = 53


class DecoEncryption:
    def __init__(self) -> None:
        # 8 random bytes -> 16 hex chars; the hex string bytes are the AES-128 key/iv.
        self._iv = b2a_hex(Random.get_random_bytes(8))
        self._key = b2a_hex(Random.get_random_bytes(8))

    @property
    def aes_key(self) -> str:
        return self._key.decode()

    @property
    def aes_iv(self) -> str:
        return self._iv.decode()

    def aes_encrypt(self, raw: str) -> str:
        padded = self._pad(raw)
        cipher = AES.new(self._key, AES.MODE_CBC, self._iv)
        return b64encode(cipher.encrypt(padded.encode())).decode()

    def aes_decrypt(self, enc: str) -> str:
        cipher = AES.new(self._key, AES.MODE_CBC, self._iv)
        decrypted = cipher.decrypt(b64decode(enc))
        return self._unpad(decrypted).decode()

    @staticmethod
    def rsa_encrypt(data: str, nn: str, ee: str) -> str:
        key = construct((int(nn, 16), int(ee, 16)))
        cipher = PKCS1_v1_5.new(key)
        return b2a_hex(cipher.encrypt(data.encode())).decode()

    def signature(
        self, seq: int, sig_nn: str, sig_ee: str, cred_hash: str, *, is_login: bool
    ) -> str:
        # Login also publishes the session AES key/iv; later requests only carry h/s.
        if is_login:
            payload = f"k={self.aes_key}&i={self.aes_iv}&h={cred_hash}&s={seq}"
        else:
            payload = f"h={cred_hash}&s={seq}"
        sign = ""
        for pos in range(0, len(payload), _RSA_CHUNK):
            sign += self.rsa_encrypt(payload[pos : pos + _RSA_CHUNK], sig_nn, sig_ee)
        return sign

    @staticmethod
    def _pad(s: str) -> str:
        pad = AES.block_size - len(s) % AES.block_size
        return s + pad * chr(pad)

    @staticmethod
    def _unpad(s: bytes) -> bytes:
        if not s:
            return s
        return s[: -s[-1]]

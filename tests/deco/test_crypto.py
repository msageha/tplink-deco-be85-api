from binascii import a2b_hex

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from deco.crypto import SessionCipher, rsa_encrypt


def _hex_key(bits: int) -> tuple[RSA.RsaKey, tuple[str, str]]:
    key = RSA.generate(bits)
    return key, (format(key.n, "x"), format(key.e, "x"))


def test_rsa_encrypt_is_decryptable() -> None:
    key, (modulus, exponent) = _hex_key(1024)
    cipher_hex = rsa_encrypt("s3cr3t-pass", modulus, exponent)
    recovered = PKCS1_v1_5.new(key).decrypt(a2b_hex(cipher_hex), b"FAIL")
    assert recovered == b"s3cr3t-pass"


def test_aes_round_trip() -> None:
    _, sign_key = _hex_key(1024)
    cipher = SessionCipher(sign_key, seq=1, cred_hash="0" * 32)
    plain = '{"operation":"read","params":{"device_mac":"default"}}'
    assert cipher.decrypt(cipher.encrypt(plain)["data"]) == plain


def test_signature_fields_and_chunking() -> None:
    key, sign_key = _hex_key(1024)  # 128 byte modulus -> 256 hex chars per RSA block
    cipher = SessionCipher(sign_key, seq=100, cred_hash="a" * 32)
    decryptor = PKCS1_v1_5.new(key)

    def decrypt_sign(sign: str) -> dict[str, str]:
        assert len(sign) % 256 == 0
        fields = "".join(
            decryptor.decrypt(a2b_hex(sign[i : i + 256]), b"FAIL").decode()
            for i in range(0, len(sign), 256)
        )
        return dict(part.split("=") for part in fields.split("&"))

    login = cipher.encrypt("{}", login=True)
    login_fields = decrypt_sign(login["sign"])
    assert len(login_fields["k"]) == 16
    assert len(login_fields["i"]) == 16
    assert login_fields["h"] == "a" * 32
    assert int(login_fields["s"]) == 100 + len(login["data"])

    request = cipher.encrypt("{}")
    assert set(decrypt_sign(request["sign"])) == {"h", "s"}
    assert len(login["sign"]) > len(request["sign"])

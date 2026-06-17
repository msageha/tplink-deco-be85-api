from binascii import a2b_hex

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from decoapi.deco import DecoEncryption


def test_aes_round_trip():
    enc = DecoEncryption()
    plain = '{"operation":"read","params":{"device_mac":"default"}}'
    assert enc.aes_decrypt(enc.aes_encrypt(plain)) == plain


def test_aes_key_iv_are_16_chars():
    enc = DecoEncryption()
    assert len(enc.aes_key) == 16
    assert len(enc.aes_iv) == 16


def test_rsa_encrypt_is_decryptable():
    key = RSA.generate(1024)
    nn = format(key.n, "x")
    ee = format(key.e, "x")

    cipher_hex = DecoEncryption.rsa_encrypt("s3cr3t-pass", nn, ee)
    decryptor = PKCS1_v1_5.new(key)
    recovered = decryptor.decrypt(a2b_hex(cipher_hex), b"FAIL")
    assert recovered == b"s3cr3t-pass"


def test_signature_is_hex_and_chunked():
    key = RSA.generate(1024)  # 128-byte modulus -> 256 hex chars per chunk
    nn = format(key.n, "x")
    ee = format(key.e, "x")

    enc = DecoEncryption()

    login_sign = enc.signature(
        seq=12345, sig_nn=nn, sig_ee=ee, cred_hash="0" * 32, is_login=True
    )
    req_sign = enc.signature(
        seq=12345, sig_nn=nn, sig_ee=ee, cred_hash="0" * 32, is_login=False
    )

    for sign in (login_sign, req_sign):
        int(sign, 16)  # raises if not valid hex
        assert len(sign) % 256 == 0
        assert len(sign) > 0

    # login sign carries the AES key/iv, so it is longer (2 RSA blocks vs 1)
    assert len(login_sign) > len(req_sign)

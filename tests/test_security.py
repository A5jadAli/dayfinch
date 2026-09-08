from api.security import hash_password, verify_password, verify_totp


def test_passwords_use_salted_scrypt_hashes():
    first = hash_password("a sufficiently long password")
    second = hash_password("a sufficiently long password")
    assert first.startswith("scrypt$")
    assert first != second
    assert verify_password("a sufficiently long password", first)
    assert not verify_password("wrong password", first)


def test_totp_accepts_current_window_and_rejects_wrong_code():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert verify_totp(secret, "287082", at_time=59)
    assert not verify_totp(secret, "000000", at_time=59)

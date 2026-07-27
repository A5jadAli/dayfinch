from tracker_server.security import hash_password, verify_password


def test_passwords_use_salted_scrypt_hashes():
    first = hash_password("a sufficiently long password")
    second = hash_password("a sufficiently long password")
    assert first.startswith("scrypt$")
    assert first != second
    assert verify_password("a sufficiently long password", first)
    assert not verify_password("wrong password", first)

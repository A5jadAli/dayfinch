from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.services.integration_credentials import (
    ClaimedCredential,
    CredentialKeyring,
    IntegrationCredentialError,
    IntegrationCredentialVault,
    OpenedCredential,
)


def _key(key_id: str, value: bytes) -> str:
    encoded = base64.urlsafe_b64encode(value).decode().rstrip("=")
    return f"{key_id}:{encoded}"


def _integration(database, provider: str = "jira") -> str:
    owner = database.bootstrap_admin("owner@example.test", "hash")
    return database.create_integration(
        provider, f"{provider.title()} test", "", owner["id"]
    )


def test_credentials_are_authenticated_private_and_bound_to_integration(database):
    integration_id = _integration(database)
    keyring = CredentialKeyring.parse(_key("primary", b"p" * 32))
    vault = IntegrationCredentialVault(database, keyring)
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    secret = "jira-refresh-token-that-must-never-leak"

    assert (
        vault.store(
            integration_id,
            "jira",
            {"refresh_token": secret, "scope": "offline_access read:jira-work"},
            access_expires_at=expires_at,
        )
        == 1
    )
    row = database.get_integration_credentials(integration_id, "jira")
    assert row is not None
    assert secret.encode() not in bytes(row["ciphertext"])
    assert row["key_id"] == "primary"
    opened = vault.open(integration_id, "jira")
    assert opened.revision == 1
    assert opened.values["refresh_token"] == secret

    other_id = database.create_integration(
        "jira",
        "Other Jira",
        "",
        database.bootstrap_admin("owner@example.test", "hash")["id"],
    )
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO integration_credentials(
                   integration_id,key_id,ciphertext,revision,created_at,updated_at
               ) VALUES (%s,%s,%s,1,%s,%s)""",
            (
                other_id,
                row["key_id"],
                row["ciphertext"],
                datetime.now(UTC),
                datetime.now(UTC),
            ),
        )
    with pytest.raises(IntegrationCredentialError, match="authentication failed"):
        vault.open(other_id, "jira")


def test_old_keys_rewrap_online_and_tampering_or_missing_keys_fail_closed(database):
    integration_id = _integration(database)
    old = IntegrationCredentialVault(
        database, CredentialKeyring.parse(_key("old", b"o" * 32))
    )
    old.store(integration_id, "jira", {"refresh_token": "rotating-secret"})

    rotated = IntegrationCredentialVault(
        database,
        CredentialKeyring.parse(f"{_key('new', b'n' * 32)},{_key('old', b'o' * 32)}"),
    )
    assert rotated.open(integration_id, "jira").revision == 2
    row = database.get_integration_credentials(integration_id, "jira")
    assert row is not None and row["key_id"] == "new"

    with database.connect() as connection:
        ciphertext = bytearray(row["ciphertext"])
        ciphertext[-1] ^= 1
        connection.execute(
            "UPDATE integration_credentials SET ciphertext=%s WHERE integration_id=%s",
            (bytes(ciphertext), integration_id),
        )
    with pytest.raises(IntegrationCredentialError, match="authentication failed"):
        rotated.open(integration_id, "jira")

    with database.connect() as connection:
        connection.execute(
            "UPDATE integration_credentials SET key_id='retired' WHERE integration_id=%s",
            (integration_id,),
        )
    with pytest.raises(IntegrationCredentialError, match="key is unavailable"):
        rotated.open(integration_id, "jira")


def test_bounded_background_rewrap_isolated_per_credential(database):
    owner = database.bootstrap_admin("owner@example.test", "hash")
    first_id = database.create_integration("jira", "First Jira", "", owner["id"])
    second_id = database.create_integration("asana", "Asana", "", owner["id"])
    old = IntegrationCredentialVault(
        database, CredentialKeyring.parse(_key("old", b"o" * 32))
    )
    old.store(first_id, "jira", {"refresh_token": "jira-secret"})
    old.store(second_id, "asana", {"refresh_token": "asana-secret"})
    with database.connect() as connection:
        row = database.get_integration_credentials(second_id, "asana")
        damaged = bytearray(row["ciphertext"])
        damaged[-1] ^= 1
        connection.execute(
            "UPDATE integration_credentials SET ciphertext=%s WHERE integration_id=%s",
            (bytes(damaged), second_id),
        )

    rotating = IntegrationCredentialVault(
        database,
        CredentialKeyring.parse(f"{_key('new', b'n' * 32)},{_key('old', b'o' * 32)}"),
    )
    assert rotating.rewrap_all(limit=2) == {
        "examined": 2,
        "rewrapped": 1,
        "failed": 1,
    }
    assert database.get_integration_credentials(first_id, "jira")["key_id"] == "new"
    assert database.get_integration_credentials(second_id, "asana")["key_id"] == "old"


def test_refresh_claim_serializes_rotation_and_rejects_stale_owners(database):
    integration_id = _integration(database)
    vault = IntegrationCredentialVault(
        database, CredentialKeyring.parse(_key("primary", b"p" * 32))
    )
    vault.store(integration_id, "jira", {"refresh_token": "first"})
    observed_at = datetime.now(UTC)
    first_claim = str(uuid.uuid4())
    second_claim = str(uuid.uuid4())
    claimed = database.claim_integration_credentials(
        integration_id, "jira", first_claim, observed_at
    )
    assert claimed is not None
    assert (
        database.claim_integration_credentials(
            integration_id, "jira", second_claim, observed_at
        )
        is None
    )

    assert (
        vault.replace_after_refresh(
            integration_id,
            "jira",
            first_claim,
            int(claimed["revision"]),
            {"refresh_token": "second"},
            access_expires_at=observed_at + timedelta(minutes=15),
        )
        == 2
    )
    assert vault.open(integration_id, "jira").values["refresh_token"] == "second"
    with pytest.raises(IntegrationCredentialError, match="ownership was lost"):
        vault.replace_after_refresh(
            integration_id,
            "jira",
            first_claim,
            int(claimed["revision"]),
            {"refresh_token": "stale"},
        )

    reclaimed = database.claim_integration_credentials(
        integration_id, "jira", second_claim, observed_at
    )
    assert reclaimed is not None
    assert database.release_integration_credential_claim(
        integration_id, "jira", second_claim
    )


def test_vault_refresh_claim_releases_on_failure_and_protects_active_refresh(database):
    integration_id = _integration(database)
    vault = IntegrationCredentialVault(
        database, CredentialKeyring.parse(_key("primary", b"p" * 32))
    )
    vault.store(integration_id, "jira", {"refresh_token": "first"})
    claimed = vault.claim_for_refresh(integration_id, "jira", datetime.now(UTC))
    assert claimed is not None
    assert claimed.credential.values["refresh_token"] == "first"
    assert vault.claim_for_refresh(integration_id, "jira", datetime.now(UTC)) is None
    with pytest.raises(IntegrationCredentialError, match="could not be stored"):
        vault.store(integration_id, "jira", {"refresh_token": "unsafe overwrite"})
    assert vault.release_refresh(claimed)

    with database.connect() as connection:
        row = database.get_integration_credentials(integration_id, "jira")
        ciphertext = bytearray(row["ciphertext"])
        ciphertext[-1] ^= 1
        connection.execute(
            "UPDATE integration_credentials SET ciphertext=%s WHERE integration_id=%s",
            (bytes(ciphertext), integration_id),
        )
    with pytest.raises(IntegrationCredentialError, match="authentication failed"):
        vault.claim_for_refresh(integration_id, "jira", datetime.now(UTC))
    row = database.get_integration_credentials(integration_id, "jira")
    assert row is not None and row["refresh_claim_token"] is None


def test_background_rewrap_never_races_an_active_refresh_claim(database):
    integration_id = _integration(database)
    old = IntegrationCredentialVault(
        database, CredentialKeyring.parse(_key("old", b"o" * 32))
    )
    old.store(integration_id, "jira", {"refresh_token": "first"})
    rotating = IntegrationCredentialVault(
        database,
        CredentialKeyring.parse(f"{_key('new', b'n' * 32)},{_key('old', b'o' * 32)}"),
    )
    claimed = rotating.claim_for_refresh(integration_id, "jira", datetime.now(UTC))
    assert claimed is not None
    assert rotating.rewrap_all() == {"examined": 1, "rewrapped": 0, "failed": 1}
    row = database.get_integration_credentials(integration_id, "jira")
    assert row is not None and row["key_id"] == "old" and row["revision"] == 1
    assert rotating.release_refresh(claimed)
    assert rotating.rewrap_all() == {"examined": 1, "rewrapped": 1, "failed": 0}


@pytest.mark.parametrize(
    "specification",
    [
        "",
        "missing-separator",
        "bad id:AAAA",
        _key("short", b"x" * 31),
        f"{_key('first', b'x' * 32)},{_key('second', b'x' * 32)}",
    ],
)
def test_keyring_rejects_invalid_configuration(specification):
    with pytest.raises(ValueError):
        CredentialKeyring.parse(specification)


def test_vault_rejects_invalid_identity_and_oversized_or_non_json_payload(database):
    vault = IntegrationCredentialVault(
        database, CredentialKeyring.parse(_key("primary", b"p" * 32))
    )
    with pytest.raises(IntegrationCredentialError, match="identity"):
        vault.store("not-a-uuid", "jira", {"refresh_token": "secret"})
    integration_id = _integration(database)
    with pytest.raises(IntegrationCredentialError, match="too large"):
        vault.store(integration_id, "jira", {"refresh_token": "x" * (65 * 1024)})
    with pytest.raises(IntegrationCredentialError, match="invalid"):
        vault.store(integration_id, "jira", {"refresh_token": object()})


def test_pending_oauth_credentials_are_private_one_time_rows(database):
    owner = database.bootstrap_admin("pending-owner@example.test", "hash")
    pending_id = str(uuid.uuid4())
    vault = IntegrationCredentialVault(
        database, CredentialKeyring.parse(_key("primary", b"p" * 32))
    )
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    secret = "one-time-oauth-access-token"
    vault.store_pending(
        pending_id,
        owner["id"],
        "asana",
        {"access_token": secret},
        expires_at=expires_at,
    )
    row = database.get_pending_oauth_credentials(pending_id, owner["id"], "asana")
    assert row is not None and secret.encode() not in bytes(row["ciphertext"])
    with pytest.raises(IntegrationCredentialError, match="could not be stored"):
        vault.store_pending(
            pending_id,
            owner["id"],
            "asana",
            {"access_token": "collision-must-not-replace"},
            expires_at=expires_at,
        )
    assert (
        vault.open_pending(pending_id, owner["id"], "asana").values["access_token"]
        == secret
    )


def test_vault_converts_database_failures_to_privacy_safe_errors():
    integration_id = str(uuid.uuid4())

    class BrokenDatabase:
        def __getattr__(self, _name):
            def fail(*_args, **_kwargs):
                raise RuntimeError("database connection details must stay private")

            return fail

    vault = IntegrationCredentialVault(
        BrokenDatabase(), CredentialKeyring.parse(_key("primary", b"p" * 32))
    )
    with pytest.raises(
        IntegrationCredentialError, match="could not be stored"
    ) as stored:
        vault.store(integration_id, "jira", {"refresh_token": "secret"})
    with pytest.raises(IntegrationCredentialError, match="could not be read") as opened:
        vault.open(integration_id, "jira")
    with pytest.raises(
        IntegrationCredentialError, match="could not be claimed"
    ) as claimed:
        vault.claim_for_refresh(integration_id, "jira", datetime.now(UTC))
    claim = ClaimedCredential(
        credential=OpenedCredential(
            integration_id=integration_id,
            provider="jira",
            revision=1,
            access_expires_at=None,
            values={"refresh_token": "secret"},
        ),
        claim_token=str(uuid.uuid4()),
    )
    with pytest.raises(
        IntegrationCredentialError, match="could not be released"
    ) as released:
        vault.release_refresh(claim)
    with pytest.raises(
        IntegrationCredentialError, match="listed for rotation"
    ) as listed:
        vault.rewrap_all()
    for error in (
        stored.value,
        opened.value,
        claimed.value,
        released.value,
        listed.value,
    ):
        assert "database connection" not in str(error)


def test_user_credentials_are_subject_bound_refreshable_and_rewrapped(database):
    integration_id = _integration(database)
    owner = database.get_user_by_email("owner@example.test")
    _, first_token = database.create_invitation(
        "first-member@example.test", owner["id"], 24
    )
    first = database.accept_invitation(first_token, "hash")
    _, second_token = database.create_invitation(
        "second-member@example.test", owner["id"], 24
    )
    second = database.accept_invitation(second_token, "hash")
    old = IntegrationCredentialVault(
        database, CredentialKeyring.parse(_key("old", b"o" * 32))
    )
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    assert (
        old.store(
            integration_id,
            "jira",
            {"access_token": "member-access", "refresh_token": "member-refresh"},
            access_expires_at=expires_at,
            subject_user_id=first["id"],
        )
        == 1
    )
    opened = old.open(integration_id, "jira", subject_user_id=first["id"])
    assert opened.subject_user_id == first["id"]
    assert opened.values["refresh_token"] == "member-refresh"

    row = database.get_user_integration_credentials(integration_id, first["id"], "jira")
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO integration_user_credentials(
                   integration_id,user_id,key_id,ciphertext,revision,
                   access_expires_at,created_at,updated_at
               ) VALUES (%s,%s,%s,%s,1,%s,%s,%s)""",
            (
                integration_id,
                second["id"],
                row["key_id"],
                row["ciphertext"],
                expires_at,
                datetime.now(UTC),
                datetime.now(UTC),
            ),
        )
    with pytest.raises(IntegrationCredentialError, match="authentication failed"):
        old.open(integration_id, "jira", subject_user_id=second["id"])

    with database.connect() as connection:
        connection.execute(
            "DELETE FROM integration_user_credentials WHERE integration_id=%s AND user_id=%s",
            (integration_id, second["id"]),
        )
    rotating = IntegrationCredentialVault(
        database,
        CredentialKeyring.parse(f"{_key('new', b'n' * 32)},{_key('old', b'o' * 32)}"),
    )
    assert rotating.rewrap_all() == {"examined": 1, "rewrapped": 1, "failed": 0}
    claim = rotating.claim_for_refresh(
        integration_id,
        "jira",
        datetime.now(UTC),
        subject_user_id=first["id"],
    )
    assert claim is not None and claim.credential.subject_user_id == first["id"]
    assert (
        rotating.replace_after_refresh(
            integration_id,
            "jira",
            claim.claim_token,
            claim.credential.revision,
            {"access_token": "new-access", "refresh_token": "new-refresh"},
            access_expires_at=expires_at + timedelta(hours=1),
            subject_user_id=first["id"],
        )
        == claim.credential.revision + 1
    )
    assert (
        rotating.open(integration_id, "jira", subject_user_id=first["id"]).values[
            "refresh_token"
        ]
        == "new-refresh"
    )

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from lxml import etree
from signxml import (
    CanonicalizationMethod,
    DigestAlgorithm,
    SignatureMethod,
    XMLSigner,
    XMLVerifier,
)
from starlette.requests import Request

from api.config import Settings
from api.main import create_app
from api.services.saml import SAMLAuthenticationError, SAMLService


def _key_and_certificate(
    common_name: str = "Dayfinch SAML",
) -> tuple[bytes, bytes, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    certificate_der = base64.b64encode(
        certificate.public_bytes(serialization.Encoding.DER)
    ).decode()
    return key_pem, certificate_pem, certificate_der


def _saml_settings(
    tmp_path: Path, *, database_url: str = "postgresql://unused"
) -> Settings:
    key, certificate, idp_certificate = _key_and_certificate()
    metadata = f"""<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata" entityID="https://idp.example.test/entity">
  <IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <KeyDescriptor use="signing"><KeyInfo xmlns="http://www.w3.org/2000/09/xmldsig#"><X509Data><X509Certificate>{idp_certificate}</X509Certificate></X509Data></KeyInfo></KeyDescriptor>
    <NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</NameIDFormat>
    <SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" Location="https://idp.example.test/sso"/>
  </IDPSSODescriptor>
</EntityDescriptor>""".encode()
    return Settings(
        data_dir=tmp_path,
        admin_password="correct horse battery staple",
        session_secret="s" * 40,
        cookie_secure=False,
        max_upload_bytes=1024 * 1024,
        retention_days=30,
        admin_email="admin@example.test",
        database_url=database_url,
        saml_idp_entity_id="https://idp.example.test/entity",
        saml_idp_metadata_b64=base64.b64encode(metadata).decode(),
        saml_sp_private_key_b64=base64.b64encode(key).decode(),
        saml_sp_certificate_b64=base64.b64encode(certificate).decode(),
    )


def _request(path: str = "/auth/saml", session: dict | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 50000),
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "session": session if session is not None else {},
        }
    )


def _signed_response(
    settings: Settings,
    request_id: str,
    *,
    lifetime_minutes: int = 5,
    assertion_transform: Callable[[str], str] | None = None,
    response_transform: Callable[[str], str] | None = None,
    sign_assertion: bool = True,
    sign_response: bool = True,
) -> str:
    key = base64.b64decode(settings.saml_sp_private_key_b64).decode()
    certificate = base64.b64decode(settings.saml_sp_certificate_b64).decode()
    now = datetime.now(UTC)
    issued = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    not_before = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    not_after = (now + timedelta(minutes=lifetime_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assertion = f"""<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" xmlns:ds="http://www.w3.org/2000/09/xmldsig#" ID="assertion-valid" Version="2.0" IssueInstant="{issued}">
  <saml:Issuer>{settings.saml_idp_entity_id}</saml:Issuer>
  <ds:Signature Id="placeholder"/>
  <saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">admin-name-id</saml:NameID><saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData InResponseTo="{request_id}" NotOnOrAfter="{not_after}" Recipient="{settings.public_url}/auth/saml/acs"/></saml:SubjectConfirmation></saml:Subject>
  <saml:Conditions NotBefore="{not_before}" NotOnOrAfter="{not_after}"><saml:AudienceRestriction><saml:Audience>{settings.saml_sp_entity_id}</saml:Audience></saml:AudienceRestriction></saml:Conditions>
  <saml:AuthnStatement AuthnInstant="{issued}" SessionIndex="session-valid" SessionNotOnOrAfter="{not_after}"><saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>
  <saml:AttributeStatement><saml:Attribute Name="email"><saml:AttributeValue xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="xs:string" xmlns:xs="http://www.w3.org/2001/XMLSchema">admin@example.test</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>
</saml:Assertion>"""
    if assertion_transform:
        assertion = assertion_transform(assertion)
    signer = XMLSigner(
        signature_algorithm=SignatureMethod.RSA_SHA256,
        digest_algorithm=DigestAlgorithm.SHA256,
        c14n_algorithm=CanonicalizationMethod.EXCLUSIVE_XML_CANONICALIZATION_1_0,
    )
    assertion_element = etree.fromstring(assertion.encode())
    if sign_assertion:
        assertion_element = signer.sign(
            assertion_element,
            key=key,
            cert=certificate,
            reference_uri="assertion-valid",
            id_attribute="ID",
        )
    else:
        placeholder = assertion_element.find(
            "{http://www.w3.org/2000/09/xmldsig#}Signature"
        )
        assertion_element.remove(placeholder)
    response = f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" xmlns:ds="http://www.w3.org/2000/09/xmldsig#" ID="response-valid" Version="2.0" IssueInstant="{issued}" Destination="{settings.public_url}/auth/saml/acs" InResponseTo="{request_id}">
  <saml:Issuer>{settings.saml_idp_entity_id}</saml:Issuer>
  <ds:Signature Id="placeholder"/>
  <samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
  {etree.tostring(assertion_element).decode()}
</samlp:Response>"""
    if response_transform:
        response = response_transform(response)
    response_element = etree.fromstring(response.encode())
    if sign_response:
        response_element = signer.sign(
            response_element,
            key=key,
            cert=certificate,
            reference_uri="response-valid",
            id_attribute="ID",
        )
    else:
        placeholder = response_element.find(
            "{http://www.w3.org/2000/09/xmldsig#}Signature"
        )
        response_element.remove(placeholder)
    return base64.b64encode(etree.tostring(response_element)).decode()


def test_saml_builds_signed_sp_initiated_request_and_metadata(tmp_path):
    settings = _saml_settings(tmp_path)
    settings.prepare()
    service = SAMLService(settings)
    request = _request()

    location = service.begin(request)
    parameters = parse_qs(urlparse(location).query)
    metadata = service.metadata()

    assert location.startswith("https://idp.example.test/sso?")
    assert {"SAMLRequest", "RelayState", "SigAlg", "Signature"} <= parameters.keys()
    assert parameters["SigAlg"] == ["http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"]
    assert request.session["saml_request_id"]
    assert parameters["RelayState"] == [request.session["saml_relay_state"]]
    signed_query = "&".join(
        f"{name}={urlparse(location).query.split(f'{name}=')[1].split('&')[0]}"
        for name in ("SAMLRequest", "RelayState", "SigAlg")
    )
    sp_certificate = x509.load_pem_x509_certificate(
        base64.b64decode(settings.saml_sp_certificate_b64)
    )
    sp_certificate.public_key().verify(
        base64.b64decode(parameters["Signature"][0]),
        signed_query.encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert b"/auth/saml/acs" in metadata
    assert b"Signature" in metadata
    verified_metadata = (
        XMLVerifier()
        .verify(metadata, x509_cert=sp_certificate, id_attribute="ID")
        .signed_xml
    )
    assert verified_metadata.get("entityID") == settings.saml_sp_entity_id


def test_saml_relay_state_is_one_time_and_not_an_open_redirect(tmp_path):
    settings = _saml_settings(tmp_path)
    settings.prepare()
    service = SAMLService(settings)
    request = _request("/auth/saml/acs")
    request.session.update(
        saml_request_id="request-id",
        saml_relay_state="expected-relay",
        saml_request_issued_at=datetime.now(UTC).timestamp(),
    )

    with pytest.raises(SAMLAuthenticationError, match="state is invalid"):
        service.authenticate(request, "response", "https://attacker.test")

    assert "saml_request_id" not in request.session
    assert "saml_relay_state" not in request.session


def test_saml_authenticates_fully_signed_short_lived_correlated_assertion(tmp_path):
    settings = _saml_settings(tmp_path)
    settings.prepare()
    service = SAMLService(settings)
    start = _request()
    service.begin(start)
    request_id = start.session["saml_request_id"]
    relay_state = start.session["saml_relay_state"]
    callback = _request("/auth/saml/acs", start.session)

    identity = service.authenticate(
        callback, _signed_response(settings, request_id), relay_state
    )

    assert identity["issuer"] == settings.saml_idp_entity_id
    assert identity["subject"] == "admin-name-id"
    assert identity["email"] == "admin@example.test"
    assert identity["response_id"] == "response-valid"
    assert identity["assertion_id"] == "assertion-valid"


def test_saml_rejects_signature_tampering_and_long_lived_assertions(tmp_path):
    settings = _saml_settings(tmp_path)
    settings.prepare()
    service = SAMLService(settings)

    first = _request()
    service.begin(first)
    signed = base64.b64decode(
        _signed_response(settings, first.session["saml_request_id"])
    )
    tampered = base64.b64encode(
        signed.replace(b"admin@example.test", b"other@example.test")
    ).decode()
    with pytest.raises(SAMLAuthenticationError, match="could not be verified"):
        service.authenticate(
            _request("/auth/saml/acs", first.session),
            tampered,
            first.session["saml_relay_state"],
        )

    second = _request()
    service.begin(second)
    with pytest.raises(SAMLAuthenticationError, match="is invalid"):
        service.authenticate(
            _request("/auth/saml/acs", second.session),
            _signed_response(
                settings, second.session["saml_request_id"], lifetime_minutes=180
            ),
            second.session["saml_relay_state"],
        )


def test_saml_requires_both_signatures_and_rejects_duplicate_ids(tmp_path):
    settings = _saml_settings(tmp_path)
    settings.prepare()
    service = SAMLService(settings)

    for options in ({"sign_response": False}, {"sign_assertion": False}):
        start = _request()
        service.begin(start)
        with pytest.raises(SAMLAuthenticationError, match="could not be verified"):
            service.authenticate(
                _request("/auth/saml/acs", start.session),
                _signed_response(settings, start.session["saml_request_id"], **options),
                start.session["saml_relay_state"],
            )

    start = _request()
    service.begin(start)
    signed = base64.b64decode(
        _signed_response(settings, start.session["saml_request_id"])
    )
    duplicate = signed.replace(
        b'SessionIndex="session-valid"',
        b'SessionIndex="session-valid" ID="response-valid"',
        1,
    )
    with pytest.raises(SAMLAuthenticationError, match="duplicate identifiers"):
        service.authenticate(
            _request("/auth/saml/acs", start.session),
            base64.b64encode(duplicate).decode(),
            start.session["saml_relay_state"],
        )


def test_saml_rejects_signed_wrong_destination_audience_and_ambiguous_email(
    tmp_path,
):
    settings = _saml_settings(tmp_path)
    settings.prepare()
    service = SAMLService(settings)

    start = _request()
    service.begin(start)
    with pytest.raises(SAMLAuthenticationError, match="not intended"):
        service.authenticate(
            _request("/auth/saml/acs", start.session),
            _signed_response(
                settings,
                start.session["saml_request_id"],
                response_transform=lambda value: value.replace(
                    f'Destination="{settings.public_url}/auth/saml/acs"',
                    'Destination="https://attacker.example/acs"',
                    1,
                ),
            ),
            start.session["saml_relay_state"],
        )

    start = _request()
    service.begin(start)
    with pytest.raises(SAMLAuthenticationError, match="audience is invalid"):
        service.authenticate(
            _request("/auth/saml/acs", start.session),
            _signed_response(
                settings,
                start.session["saml_request_id"],
                assertion_transform=lambda value: value.replace(
                    f"<saml:Audience>{settings.saml_sp_entity_id}</saml:Audience>",
                    "<saml:Audience>https://attacker.example/metadata</saml:Audience>",
                ),
            ),
            start.session["saml_relay_state"],
        )

    start = _request()
    service.begin(start)
    duplicate_email = (
        '<saml:Attribute Name="email"><saml:AttributeValue>'
        "admin@example.test</saml:AttributeValue></saml:Attribute>"
    )
    with pytest.raises(SAMLAuthenticationError, match="incomplete assertion"):
        service.authenticate(
            _request("/auth/saml/acs", start.session),
            _signed_response(
                settings,
                start.session["saml_request_id"],
                assertion_transform=lambda value: value.replace(
                    "</saml:AttributeStatement>",
                    f"{duplicate_email}</saml:AttributeStatement>",
                ),
            ),
            start.session["saml_relay_state"],
        )


def test_saml_rejects_expired_request_state_and_xml_declarations(tmp_path):
    settings = _saml_settings(tmp_path)
    settings.prepare()
    service = SAMLService(settings)

    start = _request()
    service.begin(start)
    start.session["saml_request_issued_at"] = (
        datetime.now(UTC) - timedelta(minutes=16)
    ).timestamp()
    with pytest.raises(SAMLAuthenticationError, match="state is invalid or expired"):
        service.authenticate(
            _request("/auth/saml/acs", start.session),
            _signed_response(settings, start.session["saml_request_id"]),
            start.session["saml_relay_state"],
        )

    start = _request()
    service.begin(start)
    malicious = base64.b64encode(
        b'<!DOCTYPE x [<!ENTITY y SYSTEM "file:///etc/passwd">]><x>&y;</x>'
    ).decode()
    with pytest.raises(SAMLAuthenticationError, match="invalid size"):
        service.authenticate(
            _request("/auth/saml/acs", start.session),
            malicious,
            start.session["saml_relay_state"],
        )


def test_saml_metadata_rejects_expired_and_reserved_endpoint_configuration(
    tmp_path,
):
    settings = _saml_settings(tmp_path)
    expired_metadata = base64.b64decode(settings.saml_idp_metadata_b64).replace(
        b" entityID=",
        b' validUntil="2000-01-01T00:00:00Z" entityID=',
        1,
    )
    expired = replace(
        settings,
        saml_idp_metadata_b64=base64.b64encode(expired_metadata).decode(),
    )
    expired.prepare()
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        SAMLService(expired)

    reserved_metadata = base64.b64decode(settings.saml_idp_metadata_b64).replace(
        b"https://idp.example.test/sso",
        b"https://idp.example.test/sso?SAMLRequest=attacker",
    )
    reserved = replace(
        settings,
        saml_idp_metadata_b64=base64.b64encode(reserved_metadata).decode(),
    )
    reserved.prepare()
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        SAMLService(reserved)


def test_saml_configuration_rejects_partial_mismatched_and_xml_entity_input(tmp_path):
    settings = _saml_settings(tmp_path)
    with pytest.raises(RuntimeError, match="configured together"):
        replace(settings, saml_sp_private_key_b64="").prepare()

    _, unrelated_certificate, _ = _key_and_certificate("Unrelated")
    with pytest.raises(RuntimeError, match="secure pair"):
        replace(
            settings,
            saml_sp_certificate_b64=base64.b64encode(unrelated_certificate).decode(),
        ).prepare()

    malicious = base64.b64encode(
        b'<!DOCTYPE x [<!ENTITY y SYSTEM "file:///etc/passwd">]><x>&y;</x>'
    ).decode()
    with pytest.raises(RuntimeError, match="DTD or entities"):
        replace(settings, saml_idp_metadata_b64=malicious).prepare()


class FakeSAML:
    enabled = True

    def __init__(self, identity: dict):
        self.identity = identity

    @staticmethod
    def begin(request):
        request.session["saml_test"] = "started"
        return "https://idp.example.test/sso?SAMLRequest=signed"

    def authenticate(self, _request, _response, _relay):
        return self.identity

    @staticmethod
    def metadata():
        return b"<EntityDescriptor/>"


def _enable_saml(database) -> None:
    values = database.organization_settings()
    values.update(sso_provider="SAML 2.0", sso_domain="example.test")
    database.update_organization_settings(values)


def test_saml_route_links_invited_user_and_rejects_replay(tmp_path, postgres_url):
    settings = replace(
        _saml_settings(tmp_path, database_url=postgres_url),
        saml_idp_entity_id="",
        saml_idp_metadata_b64="",
        saml_sp_private_key_b64="",
        saml_sp_certificate_b64="",
    )
    app = create_app(settings)
    expires = datetime.now(UTC) + timedelta(minutes=5)
    identity = {
        "issuer": "https://idp.example.test/entity",
        "subject": "admin-name-id",
        "email": settings.admin_email,
        "response_id": "response-1",
        "assertion_id": "assertion-1",
        "expires_at": expires,
    }
    with TestClient(app) as client:
        database = app.state.database
        _enable_saml(database)
        app.state.saml = FakeSAML(identity)

        login = client.get("/login")
        start = client.get("/auth/saml", follow_redirects=False)
        wrong_media_type = client.post(
            "/auth/saml/acs",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        oversized = client.post(
            "/auth/saml/acs",
            content=b"x" * 2_000_001,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        callback = client.post(
            "/auth/saml/acs",
            data={"SAMLResponse": "signed", "RelayState": "opaque"},
            follow_redirects=False,
        )
        client.cookies.clear()
        replay = client.post(
            "/auth/saml/acs",
            data={"SAMLResponse": "signed", "RelayState": "opaque"},
        )

        assert 'href="/auth/saml"' in login.text
        assert start.status_code == 302
        assert wrong_media_type.status_code == 415
        assert oversized.status_code == 413
        assert callback.status_code == 303
        assert callback.headers["location"] == "/"
        linked = database.get_user_by_email(settings.admin_email)
        assert linked["sso_subject"] == "admin-name-id"
        assert replay.status_code == 401
        assert "already used" in replay.text


def test_saml_replay_guard_is_shared_across_replicas(database):
    expires = datetime.now(UTC) + timedelta(minutes=5)

    assert database.consume_saml_assertion("response", "assertion", expires) is True
    assert database.consume_saml_assertion("response", "assertion", expires) is False
    assert database.consume_saml_assertion("response-2", "assertion", expires) is False

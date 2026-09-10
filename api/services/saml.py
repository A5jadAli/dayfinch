from __future__ import annotations

import base64
import binascii
import hmac
import re
import secrets
import zlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
from uuid import uuid4

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    RSAPublicKey,
)
from cryptography.x509.oid import ExtensionOID
from lxml import etree
from signxml import (
    CanonicalizationMethod,
    DigestAlgorithm,
    SignatureConfiguration,
    SignatureMethod,
    XMLSigner,
    XMLVerifier,
)
from signxml.exceptions import InvalidInput, InvalidSignature
from starlette.requests import Request

from ..config import Settings

SAML_ASSERTION = "urn:oasis:names:tc:SAML:2.0:assertion"
SAML_PROTOCOL = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML_METADATA = "urn:oasis:names:tc:SAML:2.0:metadata"
XMLDSIG = "http://www.w3.org/2000/09/xmldsig#"
HTTP_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
HTTP_REDIRECT = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
EMAIL_NAME_ID = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
_CLOCK_SKEW = timedelta(minutes=3)
_MAX_ASSERTION_LIFETIME = timedelta(hours=2)
_MAX_XML_BYTES = 1_048_576
_MAX_XML_NODES = 2_000
_MAX_XML_DEPTH = 32
_MAX_ATTRIBUTES = 64
_SAFE_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,511}\Z")


class SAMLAuthenticationError(RuntimeError):
    pass


def _tag(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _text(element: etree._Element) -> str:
    if len(element):
        raise SAMLAuthenticationError("The SAML response has an invalid structure")
    return (element.text or "").strip()


def _direct(parent: etree._Element, namespace: str, name: str) -> list[etree._Element]:
    expected = _tag(namespace, name)
    return [child for child in parent if child.tag == expected]


def _one(parent: etree._Element, namespace: str, name: str) -> etree._Element:
    values = _direct(parent, namespace, name)
    if len(values) != 1:
        raise SAMLAuthenticationError("The SAML response has an invalid structure")
    return values[0]


def _timestamp(value: str) -> datetime:
    if not value or len(value) > 64:
        raise SAMLAuthenticationError("The SAML response has an invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SAMLAuthenticationError(
            "The SAML response has an invalid timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise SAMLAuthenticationError("The SAML response has an invalid timestamp")
    return parsed.astimezone(UTC)


def _validate_xml_bounds(root: etree._Element) -> None:
    node_count = 0
    stack: list[tuple[etree._Element, int]] = [(root, 1)]
    identifiers: set[str] = set()
    while stack:
        node, depth = stack.pop()
        node_count += 1
        if node_count > _MAX_XML_NODES or depth > _MAX_XML_DEPTH:
            raise SAMLAuthenticationError("The SAML XML exceeds safety limits")
        if len(node.attrib) > _MAX_ATTRIBUTES:
            raise SAMLAuthenticationError("The SAML XML exceeds safety limits")
        for attribute in ("ID", "Id", "id"):
            value = node.get(attribute)
            if value:
                if value in identifiers:
                    raise SAMLAuthenticationError(
                        "The SAML response contains duplicate identifiers"
                    )
                identifiers.add(value)
        if node.text and len(node.text) > 16_384:
            raise SAMLAuthenticationError("The SAML XML exceeds safety limits")
        stack.extend((child, depth + 1) for child in node)


def _parse_xml(payload: bytes, *, configuration: bool = False) -> etree._Element:
    if (
        not payload
        or len(payload) > _MAX_XML_BYTES
        or b"<!DOCTYPE" in payload.upper()
        or b"<!ENTITY" in payload.upper()
    ):
        message = (
            "SAML configuration is invalid"
            if configuration
            else "The SAML response has an invalid size"
        )
        if configuration:
            raise ValueError(message)
        raise SAMLAuthenticationError(message)
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
        remove_comments=True,
        remove_pis=True,
        recover=False,
    )
    try:
        root = etree.fromstring(payload, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        if configuration:
            raise ValueError("SAML configuration is invalid") from exc
        raise SAMLAuthenticationError(
            "The identity provider response could not be parsed"
        ) from exc
    try:
        _validate_xml_bounds(root)
    except SAMLAuthenticationError as exc:
        if configuration:
            raise ValueError("SAML configuration is invalid") from exc
        raise
    return root


class SAMLService:
    """Strict SP-initiated SAML using pinned local IdP metadata."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = settings.saml_enabled
        self._sso_url = ""
        self._sso_origin = ""
        self._private_key: RSAPrivateKey | None = None
        self._sp_certificate_pem = b""
        self._sp_certificate_der = ""
        self._idp_signing_certificates: list[
            tuple[x509.Certificate, datetime, datetime]
        ] = []
        if not self.enabled:
            return
        try:
            metadata = settings._saml_b64(
                "TRACKER_SAML_IDP_METADATA_B64",
                settings.saml_idp_metadata_b64,
                _MAX_XML_BYTES,
            )
            self._load_metadata(metadata)
            private_key_data = settings._saml_b64(
                "TRACKER_SAML_SP_PRIVATE_KEY_B64",
                settings.saml_sp_private_key_b64,
                64 * 1024,
            )
            certificate_data = settings._saml_b64(
                "TRACKER_SAML_SP_CERTIFICATE_B64",
                settings.saml_sp_certificate_b64,
                64 * 1024,
            )
            private_key = serialization.load_pem_private_key(
                private_key_data, password=None
            )
            certificate = x509.load_pem_x509_certificate(certificate_data)
            if not isinstance(private_key, RSAPrivateKey):
                raise ValueError("SP key is not RSA")
            self._private_key = private_key
            self._sp_certificate_pem = certificate.public_bytes(
                serialization.Encoding.PEM
            )
            self._sp_certificate_der = base64.b64encode(
                certificate.public_bytes(serialization.Encoding.DER)
            ).decode("ascii")
        except (
            binascii.Error,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            etree.Error,
        ) as exc:
            raise RuntimeError("SAML configuration is invalid") from exc

    def _load_metadata(self, metadata: bytes) -> None:
        root = _parse_xml(metadata, configuration=True)
        self._validate_metadata_expiry(root)
        if root.tag == _tag(SAML_METADATA, "EntityDescriptor"):
            entities = [root]
        elif root.tag == _tag(SAML_METADATA, "EntitiesDescriptor"):
            entities = [
                node
                for node in root.iter(_tag(SAML_METADATA, "EntityDescriptor"))
                if node.get("entityID") == self.settings.saml_idp_entity_id
            ]
        else:
            raise ValueError("metadata root is invalid")
        entities = [
            entity
            for entity in entities
            if entity.get("entityID") == self.settings.saml_idp_entity_id
        ]
        if len(entities) != 1:
            raise ValueError("configured IdP entity was not found exactly once")
        entity = entities[0]
        self._validate_metadata_expiry(entity)
        descriptors = _direct(entity, SAML_METADATA, "IDPSSODescriptor")
        descriptors = [
            descriptor
            for descriptor in descriptors
            if SAML_PROTOCOL in descriptor.get("protocolSupportEnumeration", "").split()
        ]
        if len(descriptors) != 1:
            raise ValueError("metadata has an invalid IdP descriptor")
        descriptor = descriptors[0]
        endpoints = [
            endpoint
            for endpoint in _direct(descriptor, SAML_METADATA, "SingleSignOnService")
            if endpoint.get("Binding") == HTTP_REDIRECT
        ]
        locations = {endpoint.get("Location", "") for endpoint in endpoints}
        if len(locations) != 1:
            raise ValueError("metadata has an ambiguous redirect endpoint")
        self._sso_url = locations.pop()
        parsed_sso = urlparse(self._sso_url)
        if (
            parsed_sso.scheme not in {"http", "https"}
            or not parsed_sso.hostname
            or parsed_sso.username
            or parsed_sso.password
            or parsed_sso.fragment
            or any(
                key in {"SAMLRequest", "RelayState", "SigAlg", "Signature"}
                for key, _value in parse_qsl(parsed_sso.query, keep_blank_values=True)
            )
        ):
            raise ValueError("IdP SSO URL is invalid")
        if self.settings.environment == "production" and parsed_sso.scheme != "https":
            raise ValueError("IdP SSO URL must use HTTPS in production")
        self._sso_origin = f"{parsed_sso.scheme}://{parsed_sso.netloc}"

        encoded_certificates: list[str] = []
        for key_descriptor in _direct(descriptor, SAML_METADATA, "KeyDescriptor"):
            if key_descriptor.get("use", "signing") not in {"", "signing"}:
                continue
            for key_info in _direct(key_descriptor, XMLDSIG, "KeyInfo"):
                for x509_data in _direct(key_info, XMLDSIG, "X509Data"):
                    encoded_certificates.extend(
                        "".join(node.text.split())
                        for node in _direct(x509_data, XMLDSIG, "X509Certificate")
                        if node.text
                    )
        if not encoded_certificates or len(encoded_certificates) > 10:
            raise ValueError("metadata has an invalid signing certificate set")
        certificates: list[tuple[x509.Certificate, datetime, datetime]] = []
        seen: set[bytes] = set()
        for encoded in encoded_certificates:
            try:
                der = base64.b64decode(encoded, validate=True)
                certificate = x509.load_der_x509_certificate(der)
                public_key = certificate.public_key()
                signature_hash = certificate.signature_hash_algorithm
                key_usage = certificate.extensions.get_extension_for_oid(
                    ExtensionOID.KEY_USAGE
                ).value
            except x509.ExtensionNotFound:
                key_usage = None
            except (
                binascii.Error,
                TypeError,
                ValueError,
                UnsupportedAlgorithm,
            ):
                continue
            if der in seen:
                continue
            seen.add(der)
            if (
                not isinstance(public_key, RSAPublicKey)
                or public_key.key_size < 2048
                or signature_hash is None
                or signature_hash.name.lower() in {"md5", "sha1"}
                or key_usage is None
                or not key_usage.digital_signature
            ):
                continue
            certificates.append(
                (
                    certificate,
                    certificate.not_valid_before_utc,
                    certificate.not_valid_after_utc,
                )
            )
        self._idp_signing_certificates = certificates
        if not self._current_idp_certificates():
            raise ValueError("metadata has no current strong signing certificate")

    @staticmethod
    def _validate_metadata_expiry(element: etree._Element) -> None:
        valid_until = element.get("validUntil", "")
        if not valid_until:
            return
        try:
            expires_at = _timestamp(valid_until)
        except SAMLAuthenticationError as exc:
            raise ValueError("metadata validity is invalid") from exc
        now = datetime.now(UTC)
        if expires_at <= now or expires_at > now + timedelta(days=3660):
            raise ValueError("metadata validity is invalid")

    def _current_idp_certificates(self) -> list[x509.Certificate]:
        now = datetime.now(UTC)
        return [
            certificate
            for certificate, not_before, not_after in self._idp_signing_certificates
            if not_before <= now < not_after
        ]

    def _signing_key(self) -> RSAPrivateKey:
        if not self.enabled or self._private_key is None:
            raise SAMLAuthenticationError("Single sign-on is not configured")
        return self._private_key

    def begin(self, request: Request) -> str:
        request_id = f"_{uuid4().hex}"
        relay_state = secrets.token_urlsafe(32)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        root = etree.Element(
            _tag(SAML_PROTOCOL, "AuthnRequest"),
            nsmap={"samlp": SAML_PROTOCOL, "saml": SAML_ASSERTION},
            ID=request_id,
            Version="2.0",
            IssueInstant=now,
            Destination=self._sso_url,
            ProtocolBinding=HTTP_POST,
            AssertionConsumerServiceURL=(f"{self.settings.public_url}/auth/saml/acs"),
        )
        etree.SubElement(
            root, _tag(SAML_ASSERTION, "Issuer")
        ).text = self.settings.saml_sp_entity_id
        etree.SubElement(
            root,
            _tag(SAML_PROTOCOL, "NameIDPolicy"),
            Format=EMAIL_NAME_ID,
            AllowCreate="true",
        )
        payload = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
        compressor = zlib.compressobj(wbits=-15)
        encoded = base64.b64encode(compressor.compress(payload) + compressor.flush())
        signed_parameters = [
            ("SAMLRequest", encoded.decode("ascii")),
            ("RelayState", relay_state),
            ("SigAlg", RSA_SHA256),
        ]
        signed_query = urlencode(signed_parameters, quote_via=quote)
        signature = self._signing_key().sign(
            signed_query.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        encoded_signature = base64.b64encode(signature).decode("ascii")
        query = (
            f"{signed_query}&"
            f"{urlencode([('Signature', encoded_signature)], quote_via=quote)}"
        )
        endpoint = urlparse(self._sso_url)
        if endpoint.query:
            query = f"{endpoint.query}&{query}"
        location = urlunparse(endpoint._replace(query=query))
        parsed = urlparse(location)
        if (
            len(location) > 16_384
            or f"{parsed.scheme}://{parsed.netloc}" != self._sso_origin
        ):
            raise SAMLAuthenticationError("The identity provider redirect is invalid")
        request.session["saml_request_id"] = request_id
        request.session["saml_relay_state"] = relay_state
        request.session["saml_request_issued_at"] = datetime.now(UTC).timestamp()
        return location

    def _verify_signature(
        self, element: etree._Element, *, expected_tag: str
    ) -> etree._Element:
        certificates = self._current_idp_certificates()
        if not certificates:
            raise SAMLAuthenticationError(
                "The SAML signing certificate expired; load rotated IdP metadata"
            )
        configuration = SignatureConfiguration(
            require_x509=True,
            location="./",
            expect_references=1,
            signature_methods=frozenset(
                {
                    SignatureMethod.RSA_SHA256,
                    SignatureMethod.RSA_SHA384,
                    SignatureMethod.RSA_SHA512,
                }
            ),
            digest_algorithms=frozenset(
                {
                    DigestAlgorithm.SHA256,
                    DigestAlgorithm.SHA384,
                    DigestAlgorithm.SHA512,
                }
            ),
            ignore_ambiguous_key_info=False,
        )
        for certificate in certificates:
            try:
                result = XMLVerifier().verify(
                    element,
                    x509_cert=certificate,
                    id_attribute="ID",
                    expect_config=configuration,
                )
            except (InvalidInput, InvalidSignature, ValueError):
                continue
            signed = result.signed_xml
            if signed is not None and signed.tag == expected_tag:
                return signed
        raise SAMLAuthenticationError(
            "The identity provider response could not be verified"
        )

    @staticmethod
    def _validate_id(element: etree._Element) -> str:
        value = element.get("ID", "")
        if not _SAFE_ID.fullmatch(value):
            raise SAMLAuthenticationError("The SAML response has an invalid identifier")
        return value

    def _validate_response(
        self, response: etree._Element, request_id: str, now: datetime
    ) -> tuple[str, etree._Element]:
        allowed = {
            _tag(SAML_ASSERTION, "Issuer"),
            _tag(XMLDSIG, "Signature"),
            _tag(SAML_PROTOCOL, "Status"),
            _tag(SAML_ASSERTION, "Assertion"),
        }
        if any(child.tag not in allowed for child in response):
            raise SAMLAuthenticationError("The SAML response has an invalid structure")
        if (
            response.get("Version") != "2.0"
            or response.get("Destination")
            != f"{self.settings.public_url}/auth/saml/acs"
            or response.get("InResponseTo") != request_id
        ):
            raise SAMLAuthenticationError(
                "The SAML response is not intended for this sign-in"
            )
        response_id = self._validate_id(response)
        issued = _timestamp(response.get("IssueInstant", ""))
        if issued > now + _CLOCK_SKEW or issued < now - _MAX_ASSERTION_LIFETIME:
            raise SAMLAuthenticationError("The SAML response is not current")
        if (
            _text(_one(response, SAML_ASSERTION, "Issuer"))
            != self.settings.saml_idp_entity_id
        ):
            raise SAMLAuthenticationError("The SAML response issuer is invalid")
        status = _one(response, SAML_PROTOCOL, "Status")
        status_code = _one(status, SAML_PROTOCOL, "StatusCode")
        if status_code.get("Value") != STATUS_SUCCESS or len(status_code):
            raise SAMLAuthenticationError("The identity provider rejected the sign-in")
        assertion = _one(response, SAML_ASSERTION, "Assertion")
        return response_id, assertion

    def _validate_assertion(
        self, assertion: etree._Element, request_id: str, now: datetime
    ) -> dict[str, object]:
        allowed = {
            _tag(SAML_ASSERTION, "Issuer"),
            _tag(XMLDSIG, "Signature"),
            _tag(SAML_ASSERTION, "Subject"),
            _tag(SAML_ASSERTION, "Conditions"),
            _tag(SAML_ASSERTION, "AuthnStatement"),
            _tag(SAML_ASSERTION, "AttributeStatement"),
        }
        if any(child.tag not in allowed for child in assertion):
            raise SAMLAuthenticationError("The SAML assertion has an invalid structure")
        if assertion.get("Version") != "2.0":
            raise SAMLAuthenticationError("The SAML assertion version is invalid")
        assertion_id = self._validate_id(assertion)
        issued = _timestamp(assertion.get("IssueInstant", ""))
        if issued > now + _CLOCK_SKEW or issued < now - _MAX_ASSERTION_LIFETIME:
            raise SAMLAuthenticationError("The SAML assertion is not current")
        if (
            _text(_one(assertion, SAML_ASSERTION, "Issuer"))
            != self.settings.saml_idp_entity_id
        ):
            raise SAMLAuthenticationError("The SAML assertion issuer is invalid")

        subject = _one(assertion, SAML_ASSERTION, "Subject")
        subject_allowed = {
            _tag(SAML_ASSERTION, "NameID"),
            _tag(SAML_ASSERTION, "SubjectConfirmation"),
        }
        if any(child.tag not in subject_allowed for child in subject):
            raise SAMLAuthenticationError("The SAML assertion subject is invalid")
        name_id = _text(_one(subject, SAML_ASSERTION, "NameID"))
        if not name_id or len(name_id) > 512:
            raise SAMLAuthenticationError("The SAML assertion subject is invalid")
        confirmations = _direct(subject, SAML_ASSERTION, "SubjectConfirmation")
        confirmation_expiries: list[datetime] = []
        for confirmation in confirmations:
            if confirmation.get("Method") != BEARER:
                continue
            data_values = _direct(
                confirmation, SAML_ASSERTION, "SubjectConfirmationData"
            )
            if len(data_values) != 1:
                continue
            data = data_values[0]
            try:
                expiry = _timestamp(data.get("NotOnOrAfter", ""))
            except SAMLAuthenticationError:
                continue
            if (
                data.get("Recipient") == f"{self.settings.public_url}/auth/saml/acs"
                and data.get("InResponseTo") == request_id
                and now - _CLOCK_SKEW < expiry <= now + _MAX_ASSERTION_LIFETIME
            ):
                confirmation_expiries.append(expiry)
        if not confirmation_expiries:
            raise SAMLAuthenticationError(
                "The SAML assertion subject confirmation is invalid"
            )

        conditions = _one(assertion, SAML_ASSERTION, "Conditions")
        not_before_raw = conditions.get("NotBefore", "")
        not_before = (
            _timestamp(not_before_raw) if not_before_raw else issued - _CLOCK_SKEW
        )
        expires_at = _timestamp(conditions.get("NotOnOrAfter", ""))
        if (
            not_before > now + _CLOCK_SKEW
            or expires_at <= now - _CLOCK_SKEW
            or expires_at > now + _MAX_ASSERTION_LIFETIME
            or not_before >= expires_at
        ):
            raise SAMLAuthenticationError("The SAML assertion conditions are invalid")
        restrictions = _direct(conditions, SAML_ASSERTION, "AudienceRestriction")
        if not restrictions or any(
            self.settings.saml_sp_entity_id
            not in {
                _text(audience)
                for audience in _direct(restriction, SAML_ASSERTION, "Audience")
            }
            for restriction in restrictions
        ):
            raise SAMLAuthenticationError("The SAML assertion audience is invalid")
        if any(
            child.tag != _tag(SAML_ASSERTION, "AudienceRestriction")
            for child in conditions
        ):
            raise SAMLAuthenticationError("The SAML assertion conditions are invalid")

        authn_statements = _direct(assertion, SAML_ASSERTION, "AuthnStatement")
        if len(authn_statements) != 1:
            raise SAMLAuthenticationError(
                "The SAML authentication statement is invalid"
            )
        authn_instant = _timestamp(authn_statements[0].get("AuthnInstant", ""))
        if authn_instant > now + _CLOCK_SKEW or authn_instant < now - timedelta(days=1):
            raise SAMLAuthenticationError(
                "The SAML authentication statement is invalid"
            )
        authn_context = _one(authn_statements[0], SAML_ASSERTION, "AuthnContext")
        context_refs = _direct(authn_context, SAML_ASSERTION, "AuthnContextClassRef")
        if len(context_refs) != 1 or not _text(context_refs[0]):
            raise SAMLAuthenticationError("The SAML authentication context is invalid")

        email_attributes: list[etree._Element] = []
        for statement in _direct(assertion, SAML_ASSERTION, "AttributeStatement"):
            for attribute in _direct(statement, SAML_ASSERTION, "Attribute"):
                if attribute.get("Name") == self.settings.saml_email_attribute:
                    email_attributes.append(attribute)
        if len(email_attributes) != 1:
            raise SAMLAuthenticationError(
                "The identity provider returned an incomplete assertion"
            )
        email_values = _direct(email_attributes[0], SAML_ASSERTION, "AttributeValue")
        if len(email_values) != 1:
            raise SAMLAuthenticationError(
                "The identity provider returned an incomplete assertion"
            )
        email = _text(email_values[0]).lower()
        if (
            len(email) > 320
            or email.count("@") != 1
            or any(character.isspace() for character in email)
        ):
            raise SAMLAuthenticationError(
                "The identity provider returned an invalid email"
            )
        expires_at = min(expires_at, *confirmation_expiries) + _CLOCK_SKEW
        return {
            "subject": name_id,
            "email": email,
            "assertion_id": assertion_id,
            "expires_at": expires_at,
        }

    def authenticate(
        self, request: Request, saml_response: str, relay_state: str
    ) -> dict[str, object]:
        request_id = str(request.session.pop("saml_request_id", ""))
        expected_relay = str(request.session.pop("saml_relay_state", ""))
        issued_raw = request.session.pop("saml_request_issued_at", None)
        try:
            issued_at = datetime.fromtimestamp(float(issued_raw), tz=UTC)
        except (TypeError, ValueError, OverflowError):
            issued_at = datetime.min.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        if (
            not request_id
            or not _SAFE_ID.fullmatch(request_id)
            or not expected_relay
            or not hmac.compare_digest(expected_relay, relay_state)
            or issued_at < now - timedelta(minutes=15)
            or issued_at > now + _CLOCK_SKEW
        ):
            raise SAMLAuthenticationError(
                "The SAML sign-in state is invalid or expired"
            )
        if not saml_response or len(saml_response) > 1_500_000:
            raise SAMLAuthenticationError("The SAML response has an invalid size")
        try:
            decoded = base64.b64decode(saml_response, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SAMLAuthenticationError(
                "The identity provider response could not be parsed"
            ) from exc
        response = _parse_xml(decoded)
        if response.tag != _tag(SAML_PROTOCOL, "Response"):
            raise SAMLAuthenticationError("The SAML response root is invalid")
        signed_response = self._verify_signature(
            response, expected_tag=_tag(SAML_PROTOCOL, "Response")
        )
        response_id, assertion = self._validate_response(
            signed_response, request_id, now
        )
        signed_assertion = self._verify_signature(
            assertion, expected_tag=_tag(SAML_ASSERTION, "Assertion")
        )
        identity = self._validate_assertion(signed_assertion, request_id, now)
        identity.update(
            issuer=self.settings.saml_idp_entity_id,
            response_id=response_id,
        )
        return identity

    def metadata(self) -> bytes:
        if not self.enabled:
            raise SAMLAuthenticationError("SAML metadata is unavailable")
        now = datetime.now(UTC)
        root = etree.Element(
            _tag(SAML_METADATA, "EntityDescriptor"),
            nsmap={"md": SAML_METADATA, "ds": XMLDSIG},
            ID=f"_{uuid4().hex}",
            entityID=self.settings.saml_sp_entity_id,
            validUntil=(now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            cacheDuration="PT24H",
        )
        etree.SubElement(root, _tag(XMLDSIG, "Signature"), Id="placeholder")
        descriptor = etree.SubElement(
            root,
            _tag(SAML_METADATA, "SPSSODescriptor"),
            AuthnRequestsSigned="true",
            WantAssertionsSigned="true",
            protocolSupportEnumeration=SAML_PROTOCOL,
        )
        key_descriptor = etree.SubElement(
            descriptor, _tag(SAML_METADATA, "KeyDescriptor"), use="signing"
        )
        key_info = etree.SubElement(key_descriptor, _tag(XMLDSIG, "KeyInfo"))
        x509_data = etree.SubElement(key_info, _tag(XMLDSIG, "X509Data"))
        etree.SubElement(
            x509_data, _tag(XMLDSIG, "X509Certificate")
        ).text = self._sp_certificate_der
        etree.SubElement(
            descriptor, _tag(SAML_METADATA, "NameIDFormat")
        ).text = EMAIL_NAME_ID
        etree.SubElement(
            descriptor,
            _tag(SAML_METADATA, "AssertionConsumerService"),
            Binding=HTTP_POST,
            Location=f"{self.settings.public_url}/auth/saml/acs",
            index="0",
            isDefault="true",
        )
        try:
            signed = XMLSigner(
                signature_algorithm=SignatureMethod.RSA_SHA256,
                digest_algorithm=DigestAlgorithm.SHA256,
                c14n_algorithm=(
                    CanonicalizationMethod.EXCLUSIVE_XML_CANONICALIZATION_1_0
                ),
            ).sign(
                root,
                key=self._signing_key(),
                cert=self._sp_certificate_pem,
                reference_uri=root.get("ID"),
                id_attribute="ID",
            )
        except (InvalidInput, ValueError) as exc:
            raise SAMLAuthenticationError("SAML metadata is unavailable") from exc
        return etree.tostring(
            signed,
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=False,
        )

import json
import plistlib
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from sideload_pi.anisette import AnisetteClient, parse_anisette
from sideload_pi.apple_api import AppleAPIError, DeveloperClient, Team, choose_team
from sideload_pi.config import Config
from sideload_pi.credentials import AccountSecrets, save_account
from sideload_pi.errors import AnisetteError, LimitError, SideloadError
from sideload_pi.http_client import HttpResponse
from sideload_pi.layout import Layout
from sideload_pi.library import Library
from sideload_pi.provisioner import Provisioner
from sideload_pi.scheduling import AppIdUse

NOW = datetime(2026, 6, 1, tzinfo=UTC)


class _Http:
    def __init__(self, handler):
        self.handler = handler

    def request(self, method, url, body=None, headers=None, timeout=None):
        return self.handler(method, url, body or b"", headers or {})


def _anisette_payload():
    return json.dumps(
        {
            "X-Apple-I-MD": "md",
            "X-Apple-I-MD-M": "mdm",
            "X-Mme-Device-Id": "device-id",
        }
    ).encode()


def test_anisette_aliases_and_a_missing_server():
    data = parse_anisette({"oneTimePassword": "otp", "machineID": "mid", "deviceUniqueIdentifier": "dev"})
    assert data.md == "otp"
    assert data.device_id == "dev"
    with pytest.raises(AnisetteError):
        AnisetteClient("").fetch()


def test_developer_api_surfaces_result_codes():
    def limited(method, url, body, headers):
        return HttpResponse(200, plistlib.dumps({"resultCode": 9120}), {})

    anisette = parse_anisette({"X-Apple-I-MD": "m", "X-Apple-I-MD-M": "mm", "X-Mme-Device-Id": "d"})
    client = DeveloperClient(adsid="ads", gs_token="tok", http=_Http(limited))
    with pytest.raises(AppleAPIError) as exc:
        client.list_teams(anisette)
    assert exc.value.result_code == 9120

    def denied(method, url, body, headers):
        return HttpResponse(401, b"", {})

    client = DeveloperClient(adsid="ads", gs_token="tok", http=_Http(denied))
    with pytest.raises(AppleAPIError) as exc:
        client.list_teams(anisette)
    assert exc.value.result_code == 401
    free = Team("F", "Personal", "Free", "active")
    individual = Team("I", "Me", "Individual", "active")
    assert choose_team([individual, free]).team_id == "F"


def _certificate_der() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


class _Dev:
    def __init__(self, devices, app_ids=None, fail_add=None):
        self.devices = devices
        self.app_ids = list(app_ids or [])
        self.fail_add = fail_add
        self.added = []
        self.csr_calls = 0

    def list_teams(self, anisette):
        return [Team("TEAM", "Personal", "Individual", "active")]

    def list_devices(self, team_id, anisette):
        return list(self.devices)

    def add_device(self, team_id, udid, name, anisette):
        if self.fail_add:
            raise self.fail_add
        self.added.append(udid)

    def list_app_ids(self, team_id, anisette):
        return list(self.app_ids)

    def add_app_id(self, team_id, bundle_id, name, anisette):
        from sideload_pi.apple_api import AppIdRecord

        created = AppIdRecord("APP1", bundle_id, name)
        self.app_ids.append(created)
        return created

    def submit_csr(self, team_id, csr, machine_id, machine_name, anisette):
        self.csr_calls += 1
        return _certificate_der()

    def download_profile(self, team_id, app_id_id, anisette):
        blob = plistlib.dumps(
            {
                "Name": "profile",
                "ExpirationDate": NOW + timedelta(days=7),
                "CreationDate": NOW,
                "TeamIdentifier": ["TEAM"],
                "Entitlements": {"application-identifier": "TEAM.com.example.notes"},
                "ProvisionedDevices": ["NEW"],
                "IsFreeProvisioningProfile": True,
            }
        )
        return b"cms" + blob


def _provisioner(tmp_path, dev):
    config = Config(root=tmp_path, anisette_url="http://anisette.local")
    layout = Layout(tmp_path)
    layout.ensure()
    library = Library(layout)
    library.load()
    save_account(layout, AccountSecrets(apple_id="person@example.com", password="pw", adsid="ads", gs_token="tok"))

    def http(method, url, body, headers):
        return HttpResponse(200, _anisette_payload(), {})

    return Provisioner(config, layout, library, http=_Http(http), client_factory=lambda adsid, token: dev), library


def test_provision_registers_a_device_and_reuses_the_certificate(tmp_path):
    from sideload_pi.apple_api import DeviceRecord
    from sideload_pi.library import AppRecord

    dev = _Dev([DeviceRecord("OLD", "Old")])
    provisioner, library = _provisioner(tmp_path, dev)
    library.apps["com.example.notes"] = AppRecord("com.example.notes", "Notes", "1", "notes.ipa")
    material = provisioner.provision(
        "com.example.notes", "Notes", "NEW", "Phone", lambda kind: "", NOW
    )
    assert material.expiry == NOW + timedelta(days=7)
    assert dev.added == ["NEW"]
    assert library.account_kind == "free"
    assert len(library.app_id_uses) == 1
    again = provisioner.provision("com.example.notes", "Notes", "NEW", "Phone", lambda kind: "", NOW)
    assert again.expiry == material.expiry
    assert dev.csr_calls == 1
    assert len(library.app_id_uses) == 1


def test_free_account_blocks_a_fourth_device_before_calling_apple(tmp_path):
    from sideload_pi.apple_api import DeviceRecord
    from sideload_pi.library import AppRecord

    devices = [DeviceRecord(f"D{i}", f"P{i}") for i in range(3)]
    dev = _Dev(devices)
    provisioner, library = _provisioner(tmp_path, dev)
    library.account_kind = "free"
    library.apps["com.example.notes"] = AppRecord("com.example.notes", "Notes", "1", "notes.ipa")
    with pytest.raises(LimitError):
        provisioner.provision("com.example.notes", "Notes", "NEW", "Phone", lambda kind: "", NOW)
    assert dev.added == []


def test_unknown_account_reports_apples_device_error(tmp_path):
    from sideload_pi.apple_api import DeviceRecord
    from sideload_pi.library import AppRecord

    dev = _Dev(
        [DeviceRecord(f"D{i}", f"P{i}") for i in range(3)],
        fail_add=AppleAPIError("limit", 35),
    )
    provisioner, library = _provisioner(tmp_path, dev)
    library.apps["com.example.notes"] = AppRecord("com.example.notes", "Notes", "1", "notes.ipa")
    with pytest.raises(SideloadError, match="3 devices"):
        provisioner.provision("com.example.notes", "Notes", "NEW", "Phone", lambda kind: "", NOW)


def test_app_id_window_is_enforced_before_add(tmp_path):
    from sideload_pi.library import AppRecord

    dev = _Dev([])
    provisioner, library = _provisioner(tmp_path, dev)
    library.apps["com.example.notes"] = AppRecord("com.example.notes", "Notes", "1", "notes.ipa")
    library.app_id_uses = [AppIdUse(f"com.example.old{i}", NOW - timedelta(days=1)) for i in range(10)]
    with pytest.raises(LimitError):
        provisioner.provision("com.example.notes", "Notes", "NEW", "Phone", lambda kind: "", NOW)

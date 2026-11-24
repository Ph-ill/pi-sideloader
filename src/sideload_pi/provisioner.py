"""Turn a stored Apple ID into a profile and development certificate.

The order matches Xcode's free-team flow: team, device, App ID (reused when
it already exists), development certificate (reused until it is inside the
refresh margin), then a new provisioning profile. Profile expiry is read back
out of the profile instead of being assumed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sideload_pi.anisette import AnisetteClient
from sideload_pi.apple_api import DeveloperClient, IssuedCertificate, choose_team
from sideload_pi.certs import (
    certificate_not_after,
    certificate_pem,
    csr_b64,
    generate_key,
    key_pem,
    load_key,
    make_csr,
)
from sideload_pi.credentials import AccountSecrets, load_account, save_account
from sideload_pi.errors import AppleAPIError, LimitError, SideloadError
from sideload_pi.gsa import GsaClient
from sideload_pi.layout import Layout, write_secret, write_secret_text
from sideload_pi.library import Library
from sideload_pi.profile import parse_mobileprovision
from sideload_pi.scheduling import assert_can_register_app_id, ensure_aware

log = logging.getLogger("sideload_pi.provision")

# Profile download codes that mean the stored App ID is gone. A new one counts
# against the 10-per-7-days window. 9120 stays a limit error on add_app_id.
_STALE_APP_ID = {8201, 9100, 9115}
_CERT_SLOT = {35, 7460}


@dataclass
class Material:
    profile_path: Path
    key_path: Path
    cert_path: Path
    expiry: datetime
    free: bool | None


class Provisioner:
    def __init__(self, config, layout: Layout, library: Library, http=None, client_factory=None):
        self.config = config
        self.layout = layout
        self.library = library
        self.http = http
        self.gsa = GsaClient(
            http,
            xcode_version=config.xcode_version,
            timeout=config.http_timeout_seconds,
        )
        self.anisette = AnisetteClient(
            config.anisette_url, http, timeout=config.http_timeout_seconds
        )
        self.client_factory = client_factory or self._client

    def _client(self, adsid: str, token: str) -> DeveloperClient:
        return DeveloperClient(
            adsid=adsid,
            gs_token=token,
            client_id=self.config.client_id,
            protocol=self.config.protocol_version,
            xcode_version=self.config.xcode_version,
            http=self.http,
            timeout=self.config.http_timeout_seconds,
        )

    def login(self, apple_id: str, password: str, code_provider, now: datetime) -> AccountSecrets:
        apple_id = apple_id.strip().lower()
        session = self.gsa.authenticate(
            apple_id,
            password,
            self.anisette.fetch,
            code_provider,
            anisette_resync=self.anisette.resync,
        )
        account = AccountSecrets(
            apple_id=apple_id.strip(),
            password=password,
            adsid=session.adsid,
            gs_token=session.gs_token,
            token_expiry=session.token_expiry,
            saved_at=ensure_aware(now),
        )
        save_account(self.layout, account)
        log.info("saved Apple ID session for %s", account.apple_id)
        return account

    def provision(self, bundle_id: str, app_name: str, udid: str, device_name: str, code_provider, now: datetime) -> Material:
        account = load_account(self.layout)
        if account is None or not account.password:
            raise SideloadError("No Apple ID is saved. Run: sudo sideload-pi login")

        def call(fn):
            return self._call(account, fn, code_provider, now)

        anisette = self.anisette.fetch()
        teams = call(lambda client, fresh: client.list_teams(fresh))
        team = choose_team(teams)
        self.library.team_id = team.team_id
        devices = call(lambda client, fresh: client.list_devices(team.team_id, fresh))
        if udid not in {item.udid for item in devices}:
            if self.library.account_kind == "free" and len(devices) >= 3:
                raise LimitError(
                    "Free Apple ID limit: 3 registered devices. "
                    f"This iPhone ({udid}) is not one of them. "
                    "Remove a device in Xcode, or use a phone that is already registered."
                )
            try:
                call(
                    lambda client, fresh: client.add_device(
                        team.team_id, udid, device_name, fresh
                    )
                )
            except AppleAPIError as exc:
                raise SideloadError(
                    f"Could not register this iPhone ({exc}). A free account can "
                    "register 3 devices, and each registration expires after 7 days."
                ) from exc
        app_ids = call(lambda client, fresh: client.list_app_ids(team.team_id, fresh))
        existing = next((item for item in app_ids if item.identifier == bundle_id), None)
        record = self.library.apps.get(bundle_id)
        # A stored id is not proof the App ID still exists. Expired ones drop
        # off list_app_ids and must be created again.
        assert_can_register_app_id(
            self.library.app_id_uses,
            bundle_id,
            now,
            window=timedelta(days=self.config.app_id_window_days),
            limit=self.config.max_app_ids_per_window,
            already_registered=existing is not None,
        )
        if existing is not None:
            app_id_id = existing.app_id_id
            created = False
        else:
            if record is not None and record.app_id_id:
                log.info("App ID for %s is no longer on the account; registering it again", bundle_id)
                record.app_id_id = ""
            app_id_id, created = self._create_app_id(call, team.team_id, bundle_id)
        if record is not None:
            self.library.note_app_id(bundle_id, app_id_id, created, now)
        self._ensure_certificate(account, team.team_id, anisette.device_id, call, now)
        try:
            profile_blob = call(
                lambda client, fresh: client.download_profile(team.team_id, app_id_id, fresh)
            )
        except AppleAPIError as exc:
            if exc.result_code not in _STALE_APP_ID or created:
                raise
            log.info(
                "profile for %s returned %s; clearing the App ID and registering it again",
                bundle_id,
                exc.result_code,
            )
            assert_can_register_app_id(
                self.library.app_id_uses,
                bundle_id,
                now,
                window=timedelta(days=self.config.app_id_window_days),
                limit=self.config.max_app_ids_per_window,
                already_registered=False,
            )
            if record is not None:
                record.app_id_id = ""
            app_id_id, created = self._create_app_id(call, team.team_id, bundle_id)
            if record is not None:
                self.library.note_app_id(bundle_id, app_id_id, True, now)
            profile_blob = call(
                lambda client, fresh: client.download_profile(team.team_id, app_id_id, fresh)
            )
        info = parse_mobileprovision(profile_blob)
        if info.free is True:
            self.library.account_kind = "free"
        elif info.free is False:
            self.library.account_kind = "paid"
        elif info.lifetime is not None and info.lifetime <= timedelta(days=8):
            self.library.account_kind = "free"
        profile_path = self.layout.work / f"{bundle_id}.mobileprovision"
        self.layout.work.mkdir(parents=True, exist_ok=True)
        profile_path.write_bytes(profile_blob)
        return Material(
            profile_path=profile_path,
            key_path=self.layout.cert_key,
            cert_path=self.layout.cert_pem,
            expiry=info.expiry,
            free=info.free,
        )

    def _create_app_id(self, call, team_id: str, bundle_id: str) -> tuple[str, bool]:
        label = f"Xcode iOS App ID {bundle_id}"
        try:
            created_id = call(
                lambda client, fresh: client.add_app_id(team_id, bundle_id, label, fresh)
            )
        except AppleAPIError as exc:
            if exc.result_code == 9120:
                raise LimitError(str(exc)) from exc
            raise
        return created_id.app_id_id, True

    def _ensure_certificate(self, account, team_id, machine_id, call, now: datetime) -> None:
        if self._certificate_fresh(now):
            return
        meta = self._load_cert_meta()
        revoked = self._revoke_ours(team_id, machine_id, meta, call)
        key = generate_key()
        csr = make_csr(key, account.apple_id)
        try:
            issued = self._submit(call, team_id, machine_id, csr)
        except AppleAPIError as exc:
            if exc.result_code == 3250:
                raise SideloadError(
                    "Apple rejected the certificate request (result 3250)."
                ) from exc
            if exc.result_code not in _CERT_SLOT or revoked:
                if exc.result_code in _CERT_SLOT:
                    raise SideloadError(
                        "This Apple ID has no free development-certificate slot, and the "
                        "certificate this tool created was already revoked. Revoke a "
                        "different certificate in Xcode, then try again."
                    ) from exc
                raise
            if not self._revoke_ours(team_id, machine_id, meta, call):
                raise SideloadError(
                    "Apple refused a new development certificate because the account is "
                    "at its certificate limit. This tool only revokes the certificate it "
                    "created. Revoke one in Xcode, then try again."
                ) from exc
            issued = self._submit(call, team_id, machine_id, csr)
        pem = certificate_pem(issued.der)
        write_secret(self.layout.cert_key, key_pem(key))
        write_secret(self.layout.cert_pem, pem)
        self._save_cert_meta(issued, machine_id, pem)

    def _submit(self, call, team_id, machine_id, csr) -> IssuedCertificate:
        issued = call(
            lambda client, fresh: client.submit_csr(
                team_id,
                csr_b64(csr),
                machine_id,
                self.config.machine_name,
                fresh,
            )
        )
        if isinstance(issued, bytes):
            return IssuedCertificate(der=issued)
        return issued

    def _revoke_ours(self, team_id, machine_id, meta: dict, call) -> bool:
        """Revoke only the certificate this tool created. Returns whether one was revoked."""
        try:
            listed = call(lambda client, fresh: client.list_development_certs(team_id, fresh))
        except AppleAPIError as exc:
            log.warning("could not list development certificates (%s)", exc)
            return False
        serial = str(meta.get("serial") or "").casefold()
        cert_id = str(meta.get("cert_id") or "")
        ours = [
            item
            for item in listed
            if (cert_id and item.cert_id == cert_id)
            or (serial and item.serial.casefold() == serial)
        ]
        if not ours and not cert_id and not serial:
            named = [
                item
                for item in listed
                if item.machine_name == self.config.machine_name
                and (not item.machine_id or item.machine_id == machine_id)
            ]
            if len(named) == 1:
                ours = named
        if len(ours) != 1:
            return False
        call(lambda client, fresh: client.revoke_certificate(team_id, ours[0].cert_id, fresh))
        log.info("revoked development certificate %s before requesting a new one", ours[0].cert_id)
        return True

    def _load_cert_meta(self) -> dict:
        path = self.layout.cert_meta
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_cert_meta(self, issued: IssuedCertificate, machine_id: str, pem: bytes) -> None:
        from cryptography import x509

        serial = issued.serial
        if not serial:
            try:
                loaded = x509.load_pem_x509_certificate(pem)
                serial = format(loaded.serial_number, "x")
            except Exception:
                serial = ""
        write_secret_text(
            self.layout.cert_meta,
            json.dumps(
                {
                    "serial": serial,
                    "cert_id": issued.cert_id,
                    "machine_id": machine_id,
                    "machine_name": self.config.machine_name,
                },
                indent=2,
            )
            + "\n",
        )

    def _certificate_fresh(self, now: datetime) -> bool:
        if not self.layout.cert_key.exists() or not self.layout.cert_pem.exists():
            return False
        try:
            load_key(self.layout.cert_key.read_bytes())
            expiry = certificate_not_after(self.layout.cert_pem.read_bytes())
        except Exception:
            log.warning("stored development certificate could not be reused")
            return False
        return expiry - self.config.refresh_margin > ensure_aware(now)

    def _call(self, account: AccountSecrets, fn, code_provider, now: datetime):
        try:
            return self._with_session(account, fn, code_provider, now, allow_relogin=True)
        except AppleAPIError as exc:
            if exc.result_code == 401:
                self._relogin(account, code_provider, now)
                return self._with_session(account, fn, code_provider, now, allow_relogin=False)
            raise

    def _with_session(self, account, fn, code_provider, now, allow_relogin: bool):
        if not account.has_session or self._token_stale(account, now):
            if not allow_relogin:
                raise AppleAPIError("saved Apple login was rejected", 401)
            self._relogin(account, code_provider, now)
        client = self.client_factory(account.adsid, account.gs_token)
        fresh = self.anisette.fetch()
        return fn(client, fresh)

    def _token_stale(self, account: AccountSecrets, now: datetime) -> bool:
        if account.token_expiry is None:
            return False
        return ensure_aware(account.token_expiry) <= ensure_aware(now) + timedelta(minutes=5)

    def _relogin(self, account: AccountSecrets, code_provider, now: datetime) -> None:
        log.info("signing in to Apple again for %s", account.apple_id)
        session = self.gsa.authenticate(
            account.apple_id,
            account.password,
            self.anisette.fetch,
            code_provider,
            anisette_resync=self.anisette.resync,
        )
        account.adsid = session.adsid
        account.gs_token = session.gs_token
        account.token_expiry = session.token_expiry
        account.saved_at = ensure_aware(now)
        save_account(self.layout, account)

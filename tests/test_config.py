import json
from pathlib import Path

import pytest

from sideload_pi.config import DEFAULT_ANISETTE_URL, config_from_dict, load_config
from sideload_pi.errors import ConfigError
from sideload_pi.redact import redact

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_loads_and_ignores_notes():
    data = json.loads((ROOT / "config" / "config.example.json").read_text(encoding="utf-8"))
    cfg = config_from_dict(data)
    assert cfg.display.version == "V4"
    assert cfg.display.allow_partial is True
    assert cfg.battery.address == 0x62
    assert cfg.web.bind == "127.0.0.1"
    assert cfg.anisette_url == DEFAULT_ANISETTE_URL
    assert data["anisette_url"] == DEFAULT_ANISETTE_URL
    assert DEFAULT_ANISETTE_URL == "https://mac-ani.jkcoxson.com"


def test_anisette_url_defaults_to_the_public_mac_server_and_can_be_cleared():
    assert config_from_dict({}).anisette_url == DEFAULT_ANISETTE_URL
    assert load_config(Path("/no/such/sideload-pi.json")).anisette_url == DEFAULT_ANISETTE_URL
    overridden = config_from_dict({"anisette_url": "http://127.0.0.1:6969"})
    assert overridden.anisette_url == "http://127.0.0.1:6969"
    assert config_from_dict({"anisette_url": ""}).anisette_url == ""


def test_remote_bind_and_shutdown_need_an_explicit_opt_in():
    with pytest.raises(ConfigError):
        config_from_dict({"web": {"bind": "0.0.0.0"}})
    with pytest.raises(ConfigError):
        config_from_dict({"battery": {"shutdown_on_low_battery": True}})
    cfg = config_from_dict(
        {
            "web": {"bind": "0.0.0.0", "allow_remote": True},
            "battery": {"shutdown_on_low_battery": True, "power_detect_enabled": True},
            "display": {"version": "V1", "allow_partial": False},
        }
    )
    assert cfg.display.version == "V1"
    assert cfg.display.allow_partial is False


def test_unknown_panel_and_bad_throttle_are_rejected():
    with pytest.raises(ConfigError):
        config_from_dict({"display": {"version": "V5"}})
    with pytest.raises(ConfigError):
        config_from_dict({"display": {"full_only_percent": 0}})
    missing = load_config(Path("/no/such/sideload-pi.json"))
    assert missing.display.version == "V4"


def test_logs_drop_passwords_and_tokens():
    text = 'password=hunter2 token=abcdef X-Apple-I-MD: one-time'
    cleaned = redact(text)
    assert "hunter2" not in cleaned
    assert "abcdef" not in cleaned
    assert "one-time" not in cleaned
    assert "[redacted]" in cleaned

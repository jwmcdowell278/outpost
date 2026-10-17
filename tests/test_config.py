"""Tests for config loading, in particular the zero-coordination sender_id."""
from __future__ import annotations
import base64
import pytest
import yaml

from outpost.config import (
    DEFAULT_LISTEN_PORT, EDITABLE_FIELDS, OutpostConfig, editable_config_values,
    load_config, save_config_fields,
)
from outpost.crypto import generate_channel_key

_MESH_KEY = base64.b64encode(generate_channel_key()).decode()
_CHANNEL_KEY = base64.b64encode(generate_channel_key()).decode()


def _write_minimal_config(path, extra: str = "") -> None:
    path.write_text(
        f"""
mesh_key: "{_MESH_KEY}"
channels:
  - id: 1
    name: ops
    key: "{_CHANNEL_KEY}"
{extra}
"""
    )


def test_sender_id_and_listen_port_default_when_omitted(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)

    config = load_config(str(config_path))

    assert config.listen_port == DEFAULT_LISTEN_PORT
    assert isinstance(config.sender_id, int)
    assert 0 <= config.sender_id < 2**32


def test_auto_generated_sender_id_is_stable_across_reloads(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path)

    first = load_config(str(config_path))
    second = load_config(str(config_path))

    assert first.sender_id == second.sender_id
    assert (tmp_path / "outpost.yaml.identity").exists()


def test_explicit_sender_id_is_respected_and_not_overwritten(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    _write_minimal_config(config_path, extra="sender_id: 42\nlisten_port: 6000\n")

    config = load_config(str(config_path))

    assert config.sender_id == 42
    assert config.listen_port == 6000
    assert not (tmp_path / "outpost.yaml.identity").exists()


def test_editable_config_values_excludes_secrets_and_identity():
    cfg = OutpostConfig(sender_id=1, listen_port=5555, mesh_key=generate_channel_key())
    values = editable_config_values(cfg)

    assert "bitrate" in values
    assert "mesh_key" not in values
    assert "sender_id" not in values
    assert "channels" not in values
    assert "web_ui_token" not in values


@pytest.mark.parametrize("field,bad_value", [
    ("bitrate", -1),
    ("listen_port", 70000),
    ("pex_interval_s", 0),
    ("enable_agc", "yes"),
])
def test_editable_field_validators_reject_bad_values(field, bad_value):
    validator, _live = EDITABLE_FIELDS[field]
    with pytest.raises(ValueError):
        validator(bad_value)


def test_save_config_fields_updates_only_given_keys(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    config_path.write_text("mesh_key: abc\nbitrate: 16000\nchannels: []\n")

    save_config_fields(str(config_path), {"bitrate": 8000, "enable_agc": False})

    raw = yaml.safe_load(config_path.read_text())
    assert raw["bitrate"] == 8000
    assert raw["enable_agc"] is False
    assert raw["mesh_key"] == "abc"  # untouched
    assert raw["channels"] == []  # untouched

"""Multicast is on by default and finds the batman-adv interface itself."""
from outpost.client import _auto_multicast_interface
from outpost.config import load_config


def _sysfs(tmp_path, interfaces: dict[str, list[str]], batman=()):
    """interfaces: name -> bridge members ([] = not a bridge)."""
    for name, members in interfaces.items():
        d = tmp_path / name
        d.mkdir()
        if members:
            (d / "brif").mkdir()
            for m in members:
                (d / "brif" / m).mkdir()
        if name in batman:
            (d / "mesh").mkdir()
    return str(tmp_path)


def test_prefers_the_bridge_bat0_is_in(tmp_path):
    root = _sysfs(tmp_path, {"lo": [], "eth0": [], "bat0": [], "br-lan": ["bat0", "wlan0"]})
    assert _auto_multicast_interface(root) == "br-lan"


def test_uses_bat0_when_not_bridged(tmp_path):
    root = _sysfs(tmp_path, {"lo": [], "eth0": [], "wlan0": [], "bat0": []})
    assert _auto_multicast_interface(root) == "bat0"


def test_recognizes_a_renamed_batman_interface(tmp_path):
    root = _sysfs(tmp_path, {"eth0": [], "mesh0": []}, batman=("mesh0",))
    assert _auto_multicast_interface(root) == "mesh0"


def test_no_mesh_means_default_interface(tmp_path):
    root = _sysfs(tmp_path, {"lo": [], "eth0": [], "wg0": [], "br0": ["eth0"]})
    assert _auto_multicast_interface(root) is None


def test_not_linux_means_default_interface(tmp_path):
    assert _auto_multicast_interface(str(tmp_path / "missing")) is None


def test_multicast_is_on_unless_turned_off(tmp_path):
    from outpost.crypto import generate_channel_key
    import base64
    key = base64.b64encode(generate_channel_key()).decode()
    base = f"mesh_key: {key}\nchannels:\n  - id: 1\n    name: main\n    key: {key}\n"
    on = tmp_path / "on.yaml"
    on.write_text(base)
    off = tmp_path / "off.yaml"
    off.write_text(base + "enable_multicast: false\n")
    assert load_config(str(on)).enable_multicast is True
    assert load_config(str(off)).enable_multicast is False

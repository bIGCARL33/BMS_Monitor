"""Guard the slice of bleak's API this package depends on.

bleak has had breaking major releases (0.x -> 1.x -> 3.x). The failure mode is
nasty: the package installs fine, imports fine, and then fails at the bench
with a battery connected. These checks run only when bleak is present and
assert the exact attributes and signatures ble_link.py uses.

Verified against bleak 3.0.2 on aarch64 (Jetson Orin Nano, JetPack 6).
"""

from __future__ import annotations

import inspect

import pytest

bleak = pytest.importorskip("bleak", reason="bleak not installed")


def test_scanner_still_takes_return_adv():
    """We rely on discover() handing back advertisement data, not just devices."""
    sig = inspect.signature(bleak.BleakScanner.discover)
    assert "return_adv" in sig.parameters
    assert "timeout" in sig.parameters


def test_client_accepts_address_and_timeout():
    sig = inspect.signature(bleak.BleakClient.__init__)
    params = list(sig.parameters)
    assert params[1] in ("address_or_ble_device", "address")
    assert "timeout" in sig.parameters


def test_client_has_the_methods_the_transport_calls():
    for name in ("connect", "disconnect", "start_notify", "write_gatt_char"):
        assert hasattr(bleak.BleakClient, name), f"BleakClient.{name} is gone"


def test_write_gatt_char_still_takes_response():
    """Commands are written without a response; losing that would stall us."""
    assert "response" in inspect.signature(bleak.BleakClient.write_gatt_char).parameters


def test_advertisement_exposes_the_fields_scan_reads():
    from bleak.backends.scanner import AdvertisementData
    for field in ("local_name", "rssi", "service_uuids"):
        assert hasattr(AdvertisementData, field), f"AdvertisementData.{field} is gone"


def test_device_exposes_address_and_name():
    from bleak.backends.device import BLEDevice
    for field in ("address", "name"):
        assert hasattr(BLEDevice, field), f"BLEDevice.{field} is gone"


def test_transport_imports_against_the_installed_bleak():
    """The cheapest possible smoke test: does our module load at all?"""
    from jkbms.transports import ble_link
    frame = ble_link.build_command(ble_link.CMD_CELL_INFO)
    assert len(frame) == 20
    assert frame[:4] == b"\xAA\x55\x90\xEB"

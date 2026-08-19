#!/usr/bin/env python3
# Copyright 2026 Red Hat, Inc.
# Licensed under the Apache License, Version 2.0
#
# Filter out nmstate "interfaces" entries that are no longer present in
# sysfs (e.g. a NIC handed over to vfio-pci for DPDK/SR-IOV passthrough,
# either by edpm_network_config_driver_bind in this same run or by a prior
# run), using the persisted nmstate device_map.yaml to identify them.
#
# An interface entry is skipped only when BOTH are true:
#   - "name" is not currently a netdev in sysfs (/sys/class/net/<name>).
#   - device_map.yaml has a recorded entry for "name" (its PCI address).
#
# device_map.yaml only supplies the name -> PCI address identity; the
# currently bound driver is looked up live via the PCI address
# (/sys/bus/pci/devices/<pci_address>/driver), since device_map's own
# "driver" field is only as fresh as the last successful nmstate apply and
# can be stale relative to a driver_bind step earlier in this same run.
#
# If "name" is absent from sysfs AND absent from device_map, the entry is
# left untouched: we have no evidence this is a known device that moved off
# the host network stack, so nmstate's own validation remains the safety
# net for genuinely wrong/typo interface names.
#
# Skipped names are also removed from any "port"/"ports" list elsewhere in
# the document (bond/bridge membership), so a vanished port does not leave
# a dangling reference that fails nmstate apply.

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Set, Tuple

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required (python3-pyyaml on RHEL-family hosts)."
    ) from exc

SYS_CLASS_NET = os.environ.get("EDPM_TEST_SYS_CLASS_NET", "/sys/class/net")
PCI_DEVICES = os.environ.get("EDPM_TEST_PCI_DEVICES", "/sys/bus/pci/devices")
_PORT_KEYS = frozenset({"port", "ports"})


def _is_present(sys_class_net: str, name: str) -> bool:
    return os.path.isdir(os.path.join(sys_class_net, name))


def _driver_for_pci(pci_devices: str, pci_address: str) -> Optional[str]:
    driver_link = os.path.join(pci_devices, pci_address, "driver")
    try:
        return os.path.basename(os.readlink(driver_link))
    except OSError:
        return None


def _skip_reason(
    name: str, sys_class_net: str, pci_devices: str, device_map: dict
) -> Optional[str]:
    if not name or _is_present(sys_class_net, name):
        return None
    devices = (device_map or {}).get("devices") or {}
    info = devices.get(name)
    if not isinstance(info, dict):
        return None
    pci = info.get("pci")
    # The device_map's own "driver" field is only as fresh as the last successful
    # apply; a driver_bind step earlier in this same run can have rebound the PCI
    # address since then. Look up the live driver by PCI address instead.
    driver = _driver_for_pci(pci_devices, pci) if pci else None
    return (
        f"{name}: not present in sysfs; skipping nmstate configuration for it "
        f"(device_map pci={pci}, current driver={driver})"
    )


def _normalize_key(key) -> str:
    if not isinstance(key, str):
        return ""
    return key.lower().replace("_", "-")


def _strip_ports(node, skipped_names: Set[str]):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if _normalize_key(key) in _PORT_KEYS and isinstance(value, list):
                out[key] = [
                    item
                    for item in value
                    if not (isinstance(item, str) and item in skipped_names)
                ]
            else:
                out[key] = _strip_ports(value, skipped_names)
        return out
    if isinstance(node, list):
        return [_strip_ports(item, skipped_names) for item in node]
    return node


def filter_unavailable_interfaces(
    state: dict, sys_class_net: str, device_map: dict, pci_devices: str = PCI_DEVICES
) -> Tuple[dict, List[str]]:
    """Remove top-level interfaces unavailable in sysfs but known via device_map.

    :param state: Parsed nmstate desired-state document.
    :param sys_class_net: Path to sysfs class net (overridable for tests).
    :param device_map: Parsed nmstate device_map.yaml (or {}).
    :param pci_devices: Path to sysfs PCI devices, used to fetch the live driver
        for a device_map-recorded PCI address (overridable for tests).
    :returns: (filtered_state, list of human-readable skip reasons)
    """
    if not isinstance(state, dict):
        return state, []

    interfaces = state.get("interfaces")
    if not isinstance(interfaces, list):
        return state, []

    kept = []
    skipped_reasons: List[str] = []
    skipped_names: Set[str] = set()

    for entry in interfaces:
        name = entry.get("name") if isinstance(entry, dict) else None
        reason = _skip_reason(name, sys_class_net, pci_devices, device_map)
        if reason:
            skipped_reasons.append(reason)
            skipped_names.add(name)
        else:
            kept.append(entry)

    if not skipped_names:
        return state, []

    new_state = dict(state)
    new_state["interfaces"] = kept
    new_state = _strip_ports(new_state, skipped_names)
    return new_state, skipped_reasons


def _load_yaml(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Filter nmstate interfaces unavailable in sysfs (e.g. bound to "
            "vfio-pci) using the persisted device_map for identification."
        )
    )
    parser.add_argument(
        "-f", "--input", required=True, help="YAML file with the rendered nmstate desired state"
    )
    parser.add_argument(
        "-m", "--map", default="", help="nmstate device_map.yaml path (optional)"
    )
    parser.add_argument(
        "-o", "--output", required=True, help="Write the filtered nmstate desired state here"
    )
    args = parser.parse_args()

    state = _load_yaml(args.input)
    device_map = _load_yaml(args.map)

    filtered, skipped = filter_unavailable_interfaces(state, SYS_CLASS_NET, device_map)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        yaml.safe_dump(filtered, fh, default_flow_style=False, allow_unicode=True)

    for reason in skipped:
        print(reason)

    return 0


if __name__ == "__main__":
    sys.exit(main())

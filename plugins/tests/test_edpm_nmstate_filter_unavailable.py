#!/usr/bin/env python3
# Copyright 2026 Red Hat, Inc.
# Licensed under the Apache License, Version 2.0

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

import yaml

SCRIPT_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "roles",
        "edpm_network_config",
        "files",
        "edpm_nmstate_filter_unavailable.py",
    )
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "edpm_nmstate_filter_unavailable", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestEdpmNmstateFilterUnavailable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sys_class_net = os.path.join(self.tmp, "class", "net")
        self.pci_devices = os.path.join(self.tmp, "bus", "pci", "devices")
        os.makedirs(self.sys_class_net)
        os.makedirs(self.pci_devices)
        os.environ["EDPM_TEST_SYS_CLASS_NET"] = self.sys_class_net
        os.environ["EDPM_TEST_PCI_DEVICES"] = self.pci_devices
        self.mod = _load_module()

    def tearDown(self):
        os.environ.pop("EDPM_TEST_SYS_CLASS_NET", None)
        os.environ.pop("EDPM_TEST_PCI_DEVICES", None)

    def _mk_netdev(self, name):
        os.makedirs(os.path.join(self.sys_class_net, name))

    def _set_pci_driver(self, pci_bdf, driver):
        pci_path = os.path.join(self.pci_devices, pci_bdf)
        os.makedirs(pci_path, exist_ok=True)
        driver_link = os.path.join(pci_path, "driver")
        if os.path.islink(driver_link):
            os.remove(driver_link)
        os.symlink(os.path.join("/fake/drivers", driver), driver_link)

    def _write_yaml(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh)
        return path

    # -- filter_unavailable_interfaces -----------------------------------

    def test_keeps_interface_present_in_sysfs(self):
        self._mk_netdev("eno1")
        state = {"interfaces": [{"name": "eno1", "type": "ethernet", "state": "up"}]}
        filtered, skipped = self.mod.filter_unavailable_interfaces(
            state, self.sys_class_net, {}
        )
        self.assertEqual(filtered, state)
        self.assertEqual(skipped, [])

    def test_keeps_interface_absent_everywhere(self):
        # Not in sysfs and not in device_map: nothing to cross-check, leave as-is
        # (e.g. genuine typo -- let nmstate's own validation catch it).
        state = {"interfaces": [{"name": "eno1", "type": "ethernet", "state": "up"}]}
        filtered, skipped = self.mod.filter_unavailable_interfaces(
            state, self.sys_class_net, {}
        )
        self.assertEqual(filtered, state)
        self.assertEqual(skipped, [])

    def test_skips_interface_bound_to_vfio_pci(self):
        # eno1 is absent from sysfs (handed to vfio-pci) but known via device_map.
        # device_map's own "driver" field is stale (still "ice"); the live driver
        # at the recorded PCI address is what determines the reported reason.
        self._set_pci_driver("0000:8a:00.0", "vfio-pci")
        device_map = {"devices": {"eno1": {"pci": "0000:8a:00.0", "driver": "ice"}}}
        state = {
            "interfaces": [
                {"name": "eno1", "type": "ethernet", "state": "up"},
                {"name": "eno2", "type": "ethernet", "state": "up"},
            ]
        }
        self._mk_netdev("eno2")

        filtered, skipped = self.mod.filter_unavailable_interfaces(
            state, self.sys_class_net, device_map
        )

        self.assertEqual([i["name"] for i in filtered["interfaces"]], ["eno2"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("eno1", skipped[0])
        self.assertIn("vfio-pci", skipped[0])

    def test_strips_skipped_name_from_port_lists(self):
        self._set_pci_driver("0000:8a:00.1", "vfio-pci")
        device_map = {"devices": {"eno1v0": {"pci": "0000:8a:00.1", "driver": "iavf"}}}
        state = {
            "interfaces": [
                {"name": "eno1v0", "type": "ethernet", "state": "up"},
                {
                    "name": "bond0",
                    "type": "bond",
                    "state": "up",
                    "link-aggregation": {"port": ["eno1v0", "eno1v1"]},
                },
            ]
        }
        self._mk_netdev("bond0")
        self._mk_netdev("eno1v1")

        filtered, skipped = self.mod.filter_unavailable_interfaces(
            state, self.sys_class_net, device_map
        )

        names = [i["name"] for i in filtered["interfaces"]]
        self.assertEqual(names, ["bond0"])
        bond_entry = filtered["interfaces"][0]
        self.assertEqual(bond_entry["link-aggregation"]["port"], ["eno1v1"])
        self.assertEqual(len(skipped), 1)

    def test_skip_reason_uses_live_driver_not_stale_device_map_value(self):
        # device_map says "ice" (stale, from before driver_bind ran this run);
        # the live PCI driver is now vfio-pci and must be what gets reported.
        self._set_pci_driver("0000:8a:00.0", "vfio-pci")
        device_map = {"devices": {"eno1": {"pci": "0000:8a:00.0", "driver": "ice"}}}
        reason = self.mod._skip_reason(
            "eno1", self.sys_class_net, self.pci_devices, device_map
        )
        self.assertIn("current driver=vfio-pci", reason)
        self.assertNotIn("driver=ice", reason)

    def test_skip_reason_driver_none_when_pci_address_has_no_live_driver(self):
        device_map = {"devices": {"eno1": {"pci": "0000:8a:00.0", "driver": "ice"}}}
        reason = self.mod._skip_reason(
            "eno1", self.sys_class_net, self.pci_devices, device_map
        )
        self.assertIn("current driver=None", reason)

    def test_non_interface_state_returned_unchanged(self):
        state = {"dns-resolver": {"config": {"server": ["1.1.1.1"]}}}
        filtered, skipped = self.mod.filter_unavailable_interfaces(
            state, self.sys_class_net, {}
        )
        self.assertEqual(filtered, state)
        self.assertEqual(skipped, [])

    # -- CLI end-to-end ---------------------------------------------------

    def test_cli_end_to_end(self):
        self._set_pci_driver("0000:8a:00.0", "vfio-pci")
        device_map = {"devices": {"eno1": {"pci": "0000:8a:00.0", "driver": "ice"}}}
        map_path = self._write_yaml("device_map.yaml", device_map)
        input_path = self._write_yaml(
            "in.yaml",
            {
                "interfaces": [
                    {"name": "eno1", "type": "ethernet", "state": "up"},
                    {"name": "eno2", "type": "ethernet", "state": "up"},
                ]
            },
        )
        self._mk_netdev("eno2")
        output_path = os.path.join(self.tmp, "out.yaml")

        result = subprocess.run(
            [
                sys.executable,
                SCRIPT_PATH,
                "-f",
                input_path,
                "-m",
                map_path,
                "-o",
                output_path,
            ],
            env=dict(os.environ),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("eno1", result.stdout)
        self.assertIn("vfio-pci", result.stdout)

        with open(output_path, encoding="utf-8") as fh:
            output_state = yaml.safe_load(fh)
        self.assertEqual([i["name"] for i in output_state["interfaces"]], ["eno2"])

    def test_cli_no_device_map_leaves_state_unchanged(self):
        input_path = self._write_yaml(
            "in.yaml", {"interfaces": [{"name": "eno1", "type": "ethernet", "state": "up"}]}
        )
        output_path = os.path.join(self.tmp, "out.yaml")

        result = subprocess.run(
            [sys.executable, SCRIPT_PATH, "-f", input_path, "-o", output_path],
            env=dict(os.environ),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

        with open(output_path, encoding="utf-8") as fh:
            output_state = yaml.safe_load(fh)
        self.assertEqual([i["name"] for i in output_state["interfaces"]], ["eno1"])


if __name__ == "__main__":
    unittest.main()

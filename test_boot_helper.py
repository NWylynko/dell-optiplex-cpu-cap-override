"""Exercise the installed helper interface without touching CPU hardware."""

import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HELPER = Path(__file__).with_name("dell-cpu-cap.py")
BOOT_ID = "00000000-0000-0000-0000-000000000001"
REGISTERS = {
    0x194: 0x10800,
    0xCE: 0x808083AF1010800,
    0x1A0: 0x4000850089,
    0x610: 0x4281E0001B8118,
    0x1FC: 0x2C005D,
    0x771: 0x1080823,
}


class BootHelperIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        files = {
            "etc/hostname": "office-mini",
            "proc/sys/kernel/random/boot_id": BOOT_ID,
            "proc/sys/kernel/tainted": "0",
            "proc/cpuinfo": "\n\n".join(
                f"processor\t: {cpu}\nmodel name\t: Intel(R) Core(TM) i5-8500T CPU @ 2.10GHz"
                for cpu in range(6)
            ),
            "sys/class/dmi/id/sys_vendor": "Dell Inc.",
            "sys/class/dmi/id/product_name": "OptiPlex 3060",
            "sys/class/dmi/id/bios_version": "1.32.0",
            "sys/devices/system/cpu/online": "0-5",
            "sys/devices/system/cpu/intel_pstate/status": "active",
            "sys/devices/system/cpu/intel_pstate/no_turbo": "1",
        }
        for cpu in range(6):
            policy = f"sys/devices/system/cpu/cpufreq/policy{cpu}"
            for name, value in {
                "scaling_driver": "intel_pstate",
                "scaling_governor": "performance",
                "scaling_min_freq": "800000",
                "scaling_max_freq": "800000",
                "cpuinfo_max_freq": "800000",
                "affected_cpus": str(cpu),
            }.items():
                files[f"{policy}/{name}"] = value
        for name, value in files.items():
            self.write_file(name, value)
        msr = self.root / "dev/cpu/0/msr"
        msr.parent.mkdir(parents=True)
        with msr.open("wb") as stream:
            # These selected register ranges do not overlap in a regular file.
            # HWP 0x770/0x774 are deliberately not modeled this way.
            for register, value in REGISTERS.items():
                stream.seek(register)
                stream.write(struct.pack("<Q", value))

    def write_file(self, relative_path, value):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value) + "\n")
        return path

    def helper_command(self, command):
        return subprocess.run(
            [sys.executable, str(HELPER), "--root", str(self.root), command],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def write_fixture_register(self, register, value):
        with (self.root / "dev/cpu/0/msr").open("r+b") as stream:
            stream.seek(register)
            stream.write(struct.pack("<Q", value))

    def successful_command(self, command):
        result = self.helper_command(command)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()][-1]

    def test_apply_enables_turbo_without_changing_other_cpu_controls(self):
        result = self.successful_command("apply")
        state = self.successful_command("status")["state"]
        self.assertEqual(result["event"], "applied")
        self.assertEqual(state["registers"]["0x1a0"], "0x850089")
        self.assertEqual(state["no_turbo"], 0)
        for register, value in {
            "0x194": "0x10800",
            "0x610": "0x4281e0001b8118",
            "0x1fc": "0x2c005d",
        }.items():
            self.assertEqual(state["registers"][register], value)
        # A regular file does not emulate asynchronous kernel policy refresh.
        self.assertEqual(state["policies"]["policy0"]["scaling_max_freq"], 800000)
        baseline = self.root / "var/lib/dell-cpu-cap" / f"{BOOT_ID}.json"
        saved = json.loads(baseline.read_text())
        self.assertEqual(saved["registers"]["0x1a0"], "0x4000850089")
        self.assertEqual(saved["no_turbo"], 1)
        self.assertEqual(baseline.stat().st_mode & 0o777, 0o600)

    def test_repeated_apply_preserves_the_first_baseline_and_current_settings(self):
        self.successful_command("apply")
        baseline = self.root / "var/lib/dell-cpu-cap" / f"{BOOT_ID}.json"
        original_backup = baseline.read_bytes()
        original_state = self.successful_command("status")["state"]
        self.successful_command("apply")
        self.assertEqual(baseline.read_bytes(), original_backup)
        self.assertEqual(self.successful_command("status")["state"], original_state)

    def test_supported_models_can_apply_repeat_and_restore_for_any_hostname(self):
        for number, (hostname, product) in enumerate(
            (
                ("office-mini", "OptiPlex 3060"),
                ("lab-node", "OptiPlex 5060"),
            ),
            start=1,
        ):
            with self.subTest(hostname=hostname):
                boot_id = f"00000000-0000-0000-0000-{number:012d}"
                self.write_file("etc/hostname", hostname)
                self.write_file("sys/class/dmi/id/product_name", product)
                self.write_file("proc/sys/kernel/random/boot_id", boot_id)
                before = self.successful_command("status")["state"]
                self.assertEqual(self.successful_command("apply")["event"], "applied")
                after = self.successful_command("status")["state"]
                self.assertEqual(after["registers"]["0x1a0"], "0x850089")
                self.assertEqual(after["no_turbo"], 0)
                for register in ("0x194", "0xce", "0x610", "0x1fc", "0x771"):
                    self.assertEqual(
                        after["registers"][register], before["registers"][register]
                    )
                self.assertEqual(after["policies"], before["policies"])
                baseline = self.root / "var/lib/dell-cpu-cap" / f"{boot_id}.json"
                original_backup = baseline.read_bytes()
                self.assertEqual(json.loads(original_backup), before)
                self.successful_command("apply")
                self.assertEqual(baseline.read_bytes(), original_backup)
                self.assertEqual(
                    self.successful_command("restore")["event"], "restored"
                )
                self.assertEqual(self.successful_command("status")["state"], before)

    def test_apply_refuses_unverified_hardware_or_driver_states_before_writing(self):
        for name, invalid_value in {
            "sys/class/dmi/id/sys_vendor": "Other Vendor",
            "sys/class/dmi/id/product_name": "OptiPlex 7060",
            "sys/class/dmi/id/bios_version": "1.33.0",
            "proc/cpuinfo": "processor: 0\nmodel name: Different CPU",
            "sys/devices/system/cpu/online": "0-4",
            "sys/devices/system/cpu/intel_pstate/status": "passive",
            "sys/devices/system/cpu/cpufreq/policy0/scaling_driver": "acpi-cpufreq",
        }.items():
            with self.subTest(name=name):
                path = self.root / name
                original_text = path.read_text()
                self.write_file(name, invalid_value)
                result = self.helper_command("apply")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("error", result.stderr)
                path.write_text(original_text)
                state = self.successful_command("status")["state"]
                self.assertEqual(state["registers"]["0x1a0"], "0x4000850089")
                self.assertEqual(state["no_turbo"], 1)
                self.assertFalse((self.root / "var/lib/dell-cpu-cap").exists())

    def test_unsupported_vendor_or_model_refuse_every_command_without_writes(self):
        for vendor, product in (
            ("Other Vendor", "OptiPlex 3060"),
            ("Dell Inc.", "OptiPlex 7060"),
        ):
            self.write_file("sys/class/dmi/id/sys_vendor", vendor)
            self.write_file("sys/class/dmi/id/product_name", product)
            before = {
                str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            }
            for command in ("status", "apply", "restore"):
                with self.subTest(vendor=vendor, product=product, command=command):
                    result = self.helper_command(command)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("Unverified target", result.stderr)
                    after = {
                        str(path.relative_to(self.root)): path.read_bytes()
                        for path in self.root.rglob("*")
                        if path.is_file()
                    }
                    self.assertEqual(after, before)
                    self.assertFalse((self.root / "var/lib/dell-cpu-cap").exists())

    def test_failed_linux_write_restores_that_attempt_and_keeps_its_backup(self):
        control = self.root / "sys/devices/system/cpu/intel_pstate/no_turbo"
        control.chmod(0o400)
        self.addCleanup(control.chmod, 0o600)
        result = self.helper_command("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback", result.stderr)
        state = self.successful_command("status")["state"]
        self.assertEqual(state["registers"]["0x1a0"], "0x4000850089")
        self.assertEqual(state["no_turbo"], 1)
        self.assertTrue(
            (self.root / "var/lib/dell-cpu-cap" / f"{BOOT_ID}.json").exists()
        )

    def test_restore_uses_the_boot_baseline_and_preserves_unrelated_later_changes(self):
        self.successful_command("apply")
        self.write_fixture_register(0x1A0, 0x1850089)
        self.write_file(
            "sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq", "2100000"
        )
        self.write_file(
            "sys/devices/system/cpu/cpufreq/policy0/scaling_governor", "powersave"
        )
        result = self.successful_command("restore")
        state = self.successful_command("status")["state"]
        self.assertEqual(result["event"], "restored")
        self.assertEqual(state["registers"]["0x1a0"], "0x4001850089")
        self.assertEqual(state["no_turbo"], 1)
        # The regular-file fixture does not emulate Linux's effective-limit
        # clamp. This later independent request must not be written back.
        self.assertEqual(state["policies"]["policy0"]["scaling_max_freq"], 2100000)
        self.assertEqual(state["policies"]["policy0"]["scaling_governor"], "powersave")

    def test_corrupt_backup_blocks_apply_before_any_cpu_change(self):
        self.write_file(f"var/lib/dell-cpu-cap/{BOOT_ID}.json", "not valid JSON")
        result = self.helper_command("apply")
        self.assertNotEqual(result.returncode, 0)
        state = self.successful_command("status")["state"]
        self.assertEqual(state["registers"]["0x1a0"], "0x4000850089")
        self.assertEqual(state["no_turbo"], 1)

    def test_status_does_not_create_or_modify_machine_files(self):
        before = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.successful_command("status")
        after = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_failed_repeat_does_not_undo_a_previously_successful_register_change(self):
        self.successful_command("apply")
        control = self.write_file("sys/devices/system/cpu/intel_pstate/no_turbo", "1")
        control.chmod(0o400)
        self.addCleanup(control.chmod, 0o600)
        result = self.helper_command("apply")
        self.assertNotEqual(result.returncode, 0)
        state = self.successful_command("status")["state"]
        self.assertEqual(state["registers"]["0x1a0"], "0x850089")
        self.assertEqual(state["no_turbo"], 1)
        baseline = self.root / "var/lib/dell-cpu-cap" / f"{BOOT_ID}.json"
        self.assertEqual(
            json.loads(baseline.read_text())["registers"]["0x1a0"], "0x4000850089"
        )

    def test_restore_refuses_a_previous_boots_backup_without_changing_settings(self):
        self.successful_command("apply")
        self.write_file(
            "proc/sys/kernel/random/boot_id", "00000000-0000-0000-0000-000000000002"
        )
        result = self.helper_command("restore")
        self.assertNotEqual(result.returncode, 0)
        state = self.successful_command("status")["state"]
        self.assertEqual(state["registers"]["0x1a0"], "0x850089")
        self.assertEqual(state["no_turbo"], 0)

    def test_restore_refuses_a_backup_with_a_different_machine_identity(self):
        self.successful_command("apply")
        baseline = self.root / "var/lib/dell-cpu-cap" / f"{BOOT_ID}.json"
        saved = json.loads(baseline.read_text())
        saved["hostname"] = "different-mini"
        baseline.write_text(json.dumps(saved))
        result = self.helper_command("restore")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("machine and boot", result.stderr)
        state = self.successful_command("status")["state"]
        self.assertEqual(state["registers"]["0x1a0"], "0x850089")
        self.assertEqual(state["no_turbo"], 0)

    def test_restore_refuses_a_changed_hostname_or_supported_model_without_writes(self):
        self.successful_command("apply")
        for name, changed_value in {
            "etc/hostname": "renamed-mini",
            "sys/class/dmi/id/product_name": "OptiPlex 5060",
        }.items():
            with self.subTest(name=name):
                path = self.root / name
                original_text = path.read_text()
                self.write_file(name, changed_value)
                before = {
                    str(path.relative_to(self.root)): path.read_bytes()
                    for path in self.root.rglob("*")
                    if path.is_file()
                }
                result = self.helper_command("restore")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("machine and boot", result.stderr)
                after = {
                    str(path.relative_to(self.root)): path.read_bytes()
                    for path in self.root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
                path.write_text(original_text)

    def test_initially_unrestricted_machine_is_saved_and_restored_as_unrestricted(self):
        self.write_fixture_register(0x1A0, 0x850089)
        self.write_file("sys/devices/system/cpu/intel_pstate/no_turbo", "0")
        self.successful_command("apply")
        self.successful_command("restore")
        state = self.successful_command("status")["state"]
        self.assertEqual(state["registers"]["0x1a0"], "0x850089")
        self.assertEqual(state["no_turbo"], 0)

    def test_apply_refuses_an_unverified_ratio_even_on_the_expected_host(self):
        self.write_fixture_register(0x194, 0x11500)
        result = self.helper_command("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unverified CPU restriction", result.stderr)
        state = self.successful_command("status")["state"]
        self.assertEqual(state["registers"]["0x1a0"], "0x4000850089")
        self.assertEqual(state["no_turbo"], 1)

    def test_apply_refuses_mixed_turbo_state_that_would_not_refresh_linux_limits(self):
        self.write_file("sys/devices/system/cpu/intel_pstate/no_turbo", "0")
        result = self.helper_command("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mixed turbo state", result.stderr)
        state = self.successful_command("status")["state"]
        self.assertEqual(state["registers"]["0x1a0"], "0x4000850089")
        self.assertEqual(state["no_turbo"], 0)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/python3
"""Reapply verified Dell CPU turbo permission after boot."""

import argparse
import fcntl
import json
import os
import struct
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

TURBO_DISABLE = 1 << 38
VERIFIED_MODELS = ("OptiPlex 3060", "OptiPlex 5060")
REGISTERS = (0x194, 0xCE, 0x1A0, 0x610, 0x1FC, 0x771)
POLICY_FILES = (
    "scaling_driver",
    "scaling_governor",
    "scaling_min_freq",
    "scaling_max_freq",
    "cpuinfo_max_freq",
    "affected_cpus",
)


def read_cpu_register(root, register):
    with (root / "dev/cpu/0/msr").open("rb", buffering=0) as stream:
        data = os.pread(stream.fileno(), 8, register)
    if len(data) != 8:
        raise ValueError(f"Short MSR read at {register:#x}")
    return struct.unpack("<Q", data)[0]


def write_cpu_register(root, register, value):
    with (root / "dev/cpu/0/msr").open("r+b", buffering=0) as stream:
        count = os.pwrite(stream.fileno(), struct.pack("<Q", value), register)
    if count != 8:
        raise ValueError(f"Short MSR write at {register:#x}")


def read_cpu_cap_state(root):
    policies = {}
    for path in sorted((root / "sys/devices/system/cpu/cpufreq").glob("policy*")):
        policies[path.name] = {
            name: (
                int((path / name).read_text().strip())
                if name.endswith("_freq")
                else (path / name).read_text().strip()
            )
            for name in POLICY_FILES
        }
    return {
        "boot_id": (root / "proc/sys/kernel/random/boot_id").read_text().strip(),
        "hostname": (root / "etc/hostname").read_text().strip(),
        "dmi": {
            name: (root / "sys/class/dmi/id" / name).read_text().strip()
            for name in ("sys_vendor", "product_name", "bios_version")
        },
        "registers": {
            hex(register): hex(read_cpu_register(root, register))
            for register in REGISTERS
        },
        "no_turbo": int(
            (root / "sys/devices/system/cpu/intel_pstate/no_turbo").read_text().strip()
        ),
        "policies": policies,
    }


def baseline_path(root, state):
    return root / "var/lib/dell-cpu-cap" / f"{state['boot_id']}.json"


def report_cpu_cap(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


def preserve_boot_baseline(path, state):
    if path.exists():
        load_boot_baseline(path, state)
        return
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


def validate_cpu_cap_target(root):
    product = (root / "sys/class/dmi/id/product_name").read_text().strip()
    if product not in VERIFIED_MODELS:
        raise ValueError(f"Unverified target: product_name={product!r}")
    expected = {
        "sys/class/dmi/id/sys_vendor": "Dell Inc.",
        "sys/class/dmi/id/bios_version": "1.32.0",
        "sys/devices/system/cpu/online": "0-5",
        "sys/devices/system/cpu/intel_pstate/status": "active",
    }
    for name, value in expected.items():
        actual = (root / name).read_text().strip()
        if actual != value:
            raise ValueError(
                f"Unverified target: {name}={actual!r}, expected {value!r}"
            )
    models = [
        line.split(":", 1)[1].strip()
        for line in (root / "proc/cpuinfo").read_text().splitlines()
        if line.startswith("model name")
    ]
    if len(models) != 6 or any("i5-8500T" not in model for model in models):
        raise ValueError("Unverified target: expected six i5-8500T CPUs")
    paths = sorted((root / "sys/devices/system/cpu/cpufreq").glob("policy*"))
    if [path.name for path in paths] != [f"policy{cpu}" for cpu in range(6)]:
        raise ValueError("Expected all six CPU policies to exist")
    for cpu, path in enumerate(paths):
        if (path / "scaling_driver").read_text().strip() != "intel_pstate" or (
            path / "affected_cpus"
        ).read_text().strip() != str(cpu):
            raise ValueError(f"Unverified CPU policy: {path.name}")
    value = (root / "proc/sys/kernel/random/boot_id").read_text().strip()
    if str(uuid.UUID(value)) != value:
        raise ValueError("Invalid boot ID")


def validate_cpu_cap_registers(state):
    registers = {name: int(value, 16) for name, value in state["registers"].items()}
    flex = registers["0x194"]
    platform = registers["0xce"]
    hwp = registers["0x771"]
    if (
        ((flex >> 8) & 255) != 8
        or not flex & (1 << 16)
        or ((platform >> 8) & 255) != 8
        or (hwp & 255) != 35
        or ((hwp >> 8) & 255) != 8
    ):
        raise ValueError(
            "Unverified CPU restriction: expected enabled FLEX ratio 8, non-turbo ratio 8 and HWP highest/guaranteed 35/8"
        )
    if state["no_turbo"] not in (0, 1):
        raise ValueError("Invalid no_turbo value")
    if registers["0x1a0"] & TURBO_DISABLE and state["no_turbo"] == 0:
        raise ValueError(
            "Unverified mixed turbo state: CPU turbo is disabled while Linux "
            "no_turbo is already 0; the tested recovery requires a 1-to-0 transition"
        )


def restore_turbo_permissions(root, state):
    current = read_cpu_register(root, 0x1A0)
    desired = (current & ~TURBO_DISABLE) | (
        int(state["registers"]["0x1a0"], 16) & TURBO_DISABLE
    )
    if current != desired:
        write_cpu_register(root, 0x1A0, desired)
    control = root / "sys/devices/system/cpu/intel_pstate/no_turbo"
    if int(control.read_text().strip()) != state["no_turbo"]:
        control.write_text(f"{state['no_turbo']}\n")
    if (read_cpu_register(root, 0x1A0) & TURBO_DISABLE) != (
        desired & TURBO_DISABLE
    ) or int(control.read_text().strip()) != state["no_turbo"]:
        raise ValueError("Restored turbo permissions failed readback")


def apply_cpu_cap_override(root, state, path):
    report_cpu_cap("apply_started", state=state, baseline=str(path))
    try:
        if int(state["registers"]["0x1a0"], 16) & TURBO_DISABLE:
            write_cpu_register(
                root, 0x1A0, read_cpu_register(root, 0x1A0) & ~TURBO_DISABLE
            )
        if state["no_turbo"] != 0:
            (root / "sys/devices/system/cpu/intel_pstate/no_turbo").write_text("0\n")
        after = read_cpu_cap_state(root)
        if (
            int(after["registers"]["0x1a0"], 16) & TURBO_DISABLE
            or after["no_turbo"] != 0
        ):
            raise ValueError("Turbo permission changes failed readback")
    except (OSError, ValueError) as error:
        try:
            restore_turbo_permissions(root, state)
        except (OSError, ValueError) as recovery:
            raise RuntimeError(
                f"Apply failed: {error}; rollback FAILED: {recovery}; baseline={path}"
            ) from error
        report_cpu_cap("rollback", state=read_cpu_cap_state(root), baseline=str(path))
        raise RuntimeError(
            f"Apply failed: {error}; rollback complete; baseline={path}"
        ) from error
    report_cpu_cap(
        "applied",
        state=after,
        baseline=str(path),
        note="Policy ceilings are observations; verify clocks under load after boot",
    )


def restore_cpu_cap_baseline(root, state, path):
    saved = load_boot_baseline(path, state)
    report_cpu_cap("restore_started", state=state, baseline=str(path))
    restore_turbo_permissions(root, saved)
    report_cpu_cap(
        "restored",
        state=read_cpu_cap_state(root),
        baseline=str(path),
        note="Policy ceilings are observations; verify after policy refresh",
    )


def load_boot_baseline(path, state):
    try:
        saved = json.loads(path.read_text())
        if any(saved[key] != state[key] for key in ("boot_id", "hostname", "dmi")):
            raise ValueError("Baseline does not belong to this machine and boot")
        validate_cpu_cap_registers(saved)
        if saved["policies"].keys() != state["policies"].keys():
            raise ValueError("Baseline CPU policies do not match")
        for settings in saved["policies"].values():
            low = settings["scaling_min_freq"]
            high = settings["scaling_max_freq"]
            if (
                type(low) != int
                or type(high) != int
                or not 800000 <= low <= high <= 3500000
            ):
                raise ValueError("Invalid baseline policy limits")
        return saved
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid baseline {path}: {error}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/"),
        help="Alternate filesystem root for offline tests; never loads the host MSR module",
    )
    parser.add_argument("command", choices=("apply", "status", "restore"))
    args = parser.parse_args()
    root = args.root.resolve()
    validate_cpu_cap_target(root)
    if root == Path("/"):
        if os.geteuid() != 0:
            raise PermissionError("Run as root to access CPU registers")
        if args.command != "status":
            subprocess.run(["/usr/sbin/modprobe", "msr"], check=True)
    state = read_cpu_cap_state(root)
    if args.command == "status":
        report_cpu_cap("status", state=state, baseline=str(baseline_path(root, state)))
        return
    path = baseline_path(root, state)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (path.parent / ".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_cpu_cap_state(root)
        if args.command == "restore":
            restore_cpu_cap_baseline(root, state, path)
            return
        validate_cpu_cap_registers(state)
        preserve_boot_baseline(path, state)
        apply_cpu_cap_override(root, state, path)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(
            json.dumps({"event": "error", "error": str(error)}),
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

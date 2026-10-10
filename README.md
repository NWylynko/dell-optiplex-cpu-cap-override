# Dell OptiPlex CPU-cap override

A small Linux boot helper for a **specific, tested Dell OptiPlex 3060/5060 Micro configuration** that remained at 800 MHz under load after an unrecognized power-adapter warning. It clears one CPU turbo-disable flag, then enables Linux turbo. A systemd unit can repeat the operation each boot.

**This is a hardware-policy override, not a general fix for every 800 MHz CPU.** It has been tested on only three machines. It cannot establish that an adapter is electrically adequate, that cooling is sufficient, or that another machine has the same cause. Do not remove the guards to make an unsupported machine pass. A compatible, correctly recognized power adapter addresses the adapter-identification problem without this override.

## Tested configuration and evidence

The helper accepts arbitrary hostnames, but requires all of these conditions:

| Check | Required value |
| --- | --- |
| Manufacturer/model | `Dell Inc.` / `OptiPlex 3060` or `OptiPlex 5060` |
| CPU | Intel Core i5-8500T, six online logical CPUs (`0-5`) |
| BIOS | `1.32.0` |
| CPU driver | Active `intel_pstate`, six matching per-CPU policies |
| Restriction registers | Enabled FLEX ratio 8, non-turbo ratio 8, HWP highest/guaranteed 35/8 |

These guards identify the tested configuration; they do not certify all machines with matching values. Other CPUs, BIOS versions, models, drivers, or policy layouts are unsupported. Even `status` checks the machine identity before reading its registers.

The tested machines were two 3060 Micros and one 5060 Micro running Debian 13 (Trixie). The directly measured 3060 used kernel `6.12.111+deb13-amd64`:

- Identical single-core work improved from **10.29058 to 2.37270 seconds**, with matching output. Busy frequency changed from **800 to 3475 MHz**.
- Six-core work measured about **3100 MHz**. A brief, approximately 13.25-second all-core trial had matching computation results, peak sampled temperature **62°C**, and maximum sampled CPU-package power **37.25 W**.
- Startup application and loaded performance after reboot were reported successful on all three machines. A shared post-reboot snapshot on one 3060 showed all six cores busy at a displayed **3.2 GHz**.

These are historical measurements from 2026-10-09, not performance guarantees. The raw research archive is not distributed here. The trials do not establish long-term stability, adapter electrical capacity, or whole-machine power consumption. Idle downclocking to 800 MHz remains normal.

## What it changes

A model-specific register (MSR) is a CPU control register. Bit 38 of `IA32_MISC_ENABLE` (`0x1a0`) disables turbo on this CPU. The helper clears only that package-wide bit through CPU 0, then writes `0` to Linux's `intel_pstate/no_turbo`. The order matters: Linux refuses turbo while the hardware disable flag is set. The Linux transition also refreshes performance policies. See the [Linux 6.12 driver implementation](https://github.com/torvalds/linux/blob/v6.12/drivers/cpufreq/intel_pstate.c).

The helper preserves FLEX ratio, PROCHOT control, package power limits, governors, and firmware. It permits higher clocks rather than selecting a fixed frequency. It does not flash firmware, change Boot Guard, or write CPUFreq policy limits. Saved limits are observations, not a backup of hidden policy requests; see [CPUFreq limit handling](https://github.com/torvalds/linux/blob/v6.12/drivers/cpufreq/cpufreq.c).

An MSR write adds the CPU-out-of-spec kernel taint for that boot. Root access does not override kernel lockdown or MSR-write policy. If a write is denied, diagnose the restriction rather than disabling kernel protections to force this helper to run.

| Command | Behavior |
| --- | --- |
| `status` | Read settings and policy ceilings; make no CPU-setting changes. Requires the MSR device to exist. |
| `apply` | Validate the target, load `msr`, save the first baseline of this boot, clear the turbo-disable bit, enable Linux turbo, and verify readback. |
| `restore` | Restore only the saved turbo-disable bit and Linux turbo setting for this machine and boot. Preserve unrelated bits using a fresh register read. |

Baselines live at `/var/lib/dell-cpu-cap/<boot-id>.json`. Repeated applications preserve the first baseline. A failed application attempts to restore that invocation's starting permissions and reports whether recovery succeeded. A successful rollback still means the application failed. Never copy baselines between machines or boots. Changing the hostname or DMI identity invalidates the saved baseline.

The helper refuses the unverified combination of hardware turbo disabled and Linux `no_turbo=0`: a same-value Linux write would not reproduce the tested policy refresh.

## Prerequisites and download

Use the Linux host itself, not a container or virtual machine. You need Python 3, root privileges, systemd for the supplied service, the kernel's `msr` module, and permission to access `/dev/cpu/0/msr`. The helper uses Python's standard library and does not require `rdmsr` or `wrmsr`. Obtain missing tools through your distribution; nothing here installs dependencies automatically.

Download the repository files, or run these commands as your normal user in a Bash shell on the target host, from the directory where you keep source checkouts:

```bash
git clone https://github.com/NWylynko/dell-optiplex-cpu-cap-override.git
cd dell-optiplex-cpu-cap-override
```

`git clone` downloads a checkout into the named directory; `cd` changes the shell's working directory. These commands do not apply CPU settings. All installation commands below start from this repository root. Before overwriting an existing installation, preserve its helper/unit files if you need that version for recovery.

## Install and validate manually

Use Bash on the target Linux host, from the repository root. `sudo` supplies the root privileges required for system-file installation and register access:

```bash
sudo install -o root -g root -m 0755 dell-cpu-cap.py /usr/local/sbin/dell-cpu-cap
sudo install -o root -g root -m 0644 dell-cpu-cap.service /etc/systemd/system/dell-cpu-cap.service
sudo systemctl daemon-reload
```

`install` copies each first path to its second path. `-o root` / `-g root` set ownership; `-m 0755` makes the helper executable and root-writable, while `0644` makes the unit readable and root-writable. `systemctl daemon-reload` rereads unit files. This installs the two files without enabling startup or applying the override. Updating the checkout later does not update installed files; repeat installation deliberately.

From any directory on that same host, read the starting state, run the service once, and inspect the result:

```bash
sudo modprobe msr
sudo /usr/local/sbin/dell-cpu-cap status
sudo systemctl start dell-cpu-cap.service
sudo /usr/local/sbin/dell-cpu-cap status
sudo systemctl status dell-cpu-cap.service --no-pager
sudo journalctl -u dell-cpu-cap.service -b --no-pager
```

`modprobe msr` loads register access without changing turbo settings; success usually prints nothing. The helper's `status` is read-only. `systemctl start` executes `apply` and changes this boot's permissions. `systemctl status` reads the service result. `journalctl` reads its logs: `-u` selects the unit, `-b` selects this boot, and `--no-pager` prints directly.

Expect bit 38 clear, `no_turbo=0`, and service state `active (exited)`. The unit is `Type=oneshot`: one Python process runs and exits; `RemainAfterExit=yes` retains the success state without monitoring the CPU. `StateDirectory=dell-cpu-cap` creates the baseline directory with mode `0700`. There is no automatic retry or restoration when the service stops. See [systemd service behavior](https://manpages.debian.org/trixie/systemd/systemd.service.5.en.html) and [state directories](https://manpages.debian.org/trixie/systemd/systemd.exec.5.en.html).

Output is one JSON record per line. `event` identifies `status`, `apply_started`, `applied`, `restored`, `rollback`, or `error`. `state.no_turbo` is the Linux setting; `state.registers["0x1a0"]` is the register value; `state.policies` contains ceilings in kHz. Errors go to standard error and return a nonzero exit code. The `baseline` field in `status` is the expected path, not proof that the file exists.

A policy ceiling of `3500000` kHz means 3.5 GHz is permitted; it is not a measured clock. Policy updates can be asynchronous, so an immediate old ceiling alone does not establish failure. A helper error or failed service is a failure regardless of the ceiling. Inspect the journal before proceeding.

To check repeat behavior, from any directory on the same host run:

```bash
sudo /usr/local/sbin/dell-cpu-cap apply
```

`apply` executes again and should preserve the first baseline. A second `systemctl start` while the unit is `active (exited)` does not execute it again. For recovery from a successful application, use the explicit restore instructions below.

## Verify real performance

Compare the same workload before manual application, after it, and after reboot, with other test workloads stopped. If already unrestricted, two fast runs establish current operation but cannot demonstrate a new speedup. Higher permitted ceilings alone are insufficient.

For a brief all-core check, optionally install `stress-ng` and a monitoring tool such as btop yourself. In Bash as your normal user on the target host, from any directory:

```bash
stress-ng --cpu 6 --cpu-method int64 --timeout 30s --metrics-brief --verify
```

`stress-ng` applies temporary load. `--cpu 6` starts six workers, `--cpu-method int64` selects integer work, `--timeout 30s` ends the test after 30 seconds, `--metrics-brief` prints compact results, and `--verify` checks computations. It changes no persistent settings and needs no sudo; Ctrl+C interrupts it. Look for all six workers to pass, zero failures, and loaded clocks substantially above 800 MHz while watching temperatures. A short pass does not establish long-term stability.

For hardware-counter frequency averages and fixed-work timing, use your distribution's turbostat and a repeatable workload. `Bzy_MHz` is frequency while busy; CPU package watts exclude the rest of the computer and adapter losses. See the [upstream turbostat manual](https://github.com/torvalds/linux/blob/v6.12/tools/power/x86/turbostat/turbostat.8). Btop's sampled clock is less precise.

## Enable startup after validation

After the manual service run and workload pass, use Bash on the target host from any directory:

```bash
sudo systemctl enable dell-cpu-cap.service
```

`enable` creates startup links for `WantedBy=multi-user.target`; it does not execute the helper immediately. See [systemctl enable](https://manpages.debian.org/trixie/systemd/systemctl.1.en.html). Arrange a reboot with workloads stopped and a working local console/login available.

After reboot, repeat the service status, current-boot journal, helper status, and workload checks **before manually starting or applying the helper**. Manual application would conceal an automatic-start failure. Firmware is expected to reassert the restriction on boot; the service then reapplies the override. Validate the resulting state rather than assuming either step succeeded.

## Disable and restore

Stop CPU test workloads first. In Bash on the host being restored, from any directory:

```bash
sudo systemctl disable --now dell-cpu-cap.service
sudo /usr/local/sbin/dell-cpu-cap restore
sudo /usr/local/sbin/dell-cpu-cap status
```

`disable` removes startup links; `--now` also stops the service. Stopping the service **does not undo CPU settings**. `restore` reverses the saved turbo permissions for this machine and boot; `status` verifies readback. Baselines remain available for inspection. Restoring a baseline first saved while already unrestricted returns to that unrestricted state; it cannot reconstruct earlier firmware settings.

If no matching baseline exists, restore fails without substituting another machine's or boot's values. Disabling startup and rebooting is expected to let firmware reapply its policy, but the helper cannot guarantee the firmware's next-boot state. Check afterward, with local recovery access available.

## Troubleshooting

| Result | Next step |
| --- | --- |
| MSR device missing during `status` | Run the module-load step; if it fails, diagnose that error before proceeding. |
| `Unverified target` / `Unverified CPU restriction` | Read the mismatch. Keep the guards; this configuration needs separate validation. |
| `Unverified mixed turbo state` | Hardware and Linux disagree with the tested recovery state. Collect `status` for diagnosis rather than forcing a write. |
| `Operation not permitted` | Inspect kernel restrictions and the journal. Root does not ensure MSR writes are allowed. |
| Service failed / rollback failed | Read this boot's journal and current settings. Stop workload testing if recovery failed; the CPU may be partly changed. |
| Ceiling increased but work remains slow | Compare busy clocks and fixed-work timing; inspect temperature, governor, and competing workloads. |

## Offline tests

Run as an **unprivileged user**, in Bash from the repository root on a development machine:

```bash
python3 -m unittest discover -s . -p 'test_*.py' -v
```

`python3 -m unittest` invokes Python's standard-library runner. `discover` finds tests; `-s .` starts in the current directory, quoted `-p` selects test filenames without shell expansion, and `-v` prints individual results. Tests execute the CLI against temporary filesystem fixtures; permission-failure cases require an unprivileged process.

The helper's `--root <fixture>` option redirects machine files to an offline fixture and skips host module loading. Replace `<fixture>` with a test directory's absolute path; never use a live mounted system. The service always uses the live root filesystem. These tests do not validate actual MSR writes, kernel policy refresh, boot ordering, firmware behavior, adapter capacity, or real performance.

## License

[MIT](LICENSE).

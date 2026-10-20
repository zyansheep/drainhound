# What drainhound needs from the hardware

drainhound reads what the kernel exposes and uses what it finds: nothing below
is required except a battery. Missing pieces mean less detail, not errors.

## Measurements

| What | Where it comes from | Without it |
| --- | --- | --- |
| Battery draw and charge | every `Battery` in `/sys/class/power_supply` (not a mouse's or a phone's): `power_now` and `energy_now` (µW, µWh) or `current_now`, `voltage_now` and `charge_now` (µA, µV, µAh); several batteries are added up | no time left; the log still runs (a desktop) |
| Chip power | amdgpu's `PPT` sensor on an AMD APU (Ryzen 6000 and later), or the RAPL package energy counter (`/sys/class/powercap/intel-rapl:0`, Intel and AMD) where it is readable; `[chip] sensor` forces one | the one-stage model: app shares from battery power alone |
| The chip's rails | amdgpu `gpu_metrics` format 2 (APUs; checked on Ryzen 7040) | "Idle chip" is one row instead of cores, fabric and memory links |
| Per-app GPU and video time | DRM fdinfo (`drm-engine-*`): amdgpu, i915, xe, msm, panfrost… on recent kernels | GPU work isn't attributed to apps |
| Per-app CPU and disk | `/proc` | — |
| Devices | backlight, keyboard backlight, network, block devices, USB and PCI runtime PM, ALSA streams | that device's share lands in "Rest of system" |
| Screen lock | logind's `LockedHint`, or a known locker process (`[session] lockers`) | experiments treat you as present |

**RAPL** is root-only since Linux 5.10, because fine-grained energy
readings can leak what the CPU is doing (PLATYPUS, CVE-2020-8694). To let
drainhound read it, make `energy_uj` readable: on NixOS with
`services.drainhound.readableRapl = true`, elsewhere with a udev rule:

```
SUBSYSTEM=="powercap", KERNEL=="intel-rapl:*", RUN+="/bin/chmod 0444 /sys%p/energy_uj"
```

Only do this on a machine whose users you trust.

## Settings experiments can vary

Each is offered only if the machine has it. Root settings go through
`drainhound-helper`, the others run as you; without the helper's sudo rule the
root ones are left out (the panel and `drainhound experiment list` say so).

drainhound never leaves a setting other than it found it. "As found" below is the
value a setting had in this boot before drainhound first changed it, on that power
source (saved then); `disable`, `restore --stock` and the watchdog put back only
what drainhound changed in this boot. In parentheses: the guess used where nothing
was ever seen.

| Setting | Needs | Root | Stock (restored after experiments) |
| --- | --- | --- | --- |
| `aspm`: PCIe link power saving | `/sys/module/pcie_aspm` (firmware granting the OS ASPM control) | yes | `pcie_aspm.policy=` on the kernel command line, else as found (TLP may set it; `default`) |
| `apst`: NVMe power states | an NVMe drive | yes | `nvme_core.default_ps_max_latency_us=` on the kernel command line, else as found, else the limit the drives started with (modprobe.d may set it; 100000 µs) |
| `boost`: CPU boost/turbo | `cpufreq/boost` (amd-pstate, acpi-cpufreq) or `intel_pstate/no_turbo` | yes | `[stock] boost`, else following the profile where power-profiles-daemon does that, else as found (on) |
| `cpu-cap`: a frequency cap | cpufreq | yes | as found (no cap); the cap is `[stock] cpu_cap_mhz` |
| `profile`: power profile | power-profiles-daemon or tuned-ppd (`powerprofilesctl`) | no | `[stock] profile`, else as found on that power source (power-saver on battery); on AC whatever your system sets when plugged in |
| `abm`: panel power savings | amdgpu with an internal panel | yes | `[stock] abm`, else as found on that power source |
| `gpu-dpm`: GPU and memory clocks | amdgpu | yes | as found (`auto`) |
| `wifi-ps`: Wi-Fi power saving | a wireless interface and `iw` | yes | `[stock] wifi_ps`, else as found (on) |
| `refresh`: refresh rate | niri or sway, and an internal panel with more than one rate | no | the mode found (the highest) |
| `throttle-apps` | the `drainhound-throttle` unit, and niri or sway (what it follows) | no | as found (off) |
| `freeze`, `freeze-apps` | systemd app scopes (manual runs only) | no | running |

ASPM, APST and GPU clocks are risky by default (`[experiments] risky`): some
hardware crashes or loses devices with them, so they stay out of automatic
runs unless you add them with `--risky`.

**Boot-level settings** (amdgpu panel self-refresh, `dcdebugmask`) need a boot
entry per arm. On NixOS this is a specialisation, and systemd-boot picks the
entry for the next boot. List the arms in `[experiments] boot_arms`.

## The watchdog

Automatic experiments only run while you can read the kernel log
(`journalctl -k`): membership in `systemd-journal` (or `adm`, or `wheel` on
some distributions). It looks for:

- a dead USB controller
- a Bluetooth command timeout
- an uncorrected PCIe error
- corrected PCIe errors after a link setting changed, on a boot without them before
- an amdgpu ring timeout
- an NVMe timeout

On AMD it also reads the reset reason the kernel prints at boot, to tell a
crash from a clean power-off; elsewhere, boots are judged by their suspend
and shutdown messages.

## Desktops

- **The panel**: GTK 4 and libadwaita, with gtk4-layer-shell for a panel
  under your bar (wlroots compositors, niri, KDE). Elsewhere it is a normal
  window.
- **Throttling and freezing apps**: niri or sway, and apps in their own
  systemd scopes (niri does this, and so do launchers like `app2unit` and
  `uwsm app`). With any other compositor they do nothing.
- **Bars**: anything that runs a command and reads JSON or text, from
  `drainhound waybar` or [data.md](data.md)'s files.

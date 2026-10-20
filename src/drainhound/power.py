"""Battery attribution for the power menu and a long-term raw power log.

Every RECORD seconds one record of raw measurements (battery and chip power,
per-app CPU/GPU/IO, devices and power settings) goes into the power database
(store.py: typed columns, what the models and panel read) and, as one JSON line,
into the raw archive `<state>/power/YYYY-MM-DD.jsonl`; earlier days of the
archive are zstd-compressed. The menu splits measured power with two small
online ridge models. The log keeps the model inputs rather than its outputs,
so better models can be fitted later.
App names are coarse (no PIDs, command lines, browser origins or titles).
"""
import collections
import datetime
import json
import os
import shutil
import subprocess
import time
from compression import zstd
from sys import stderr
from pathlib import Path

from . import battery as batteries
from . import compositor
from . import config
from . import qol
from . import store

SCHEMA = 3  # 3: `qol` is the record's 👍/👎 (+1/−1, 0 none) and each vote is a qol event (2: a 0–3 level).
RECORD = 10        # seconds per log record
WINDOW = 30 * 60   # seconds of history behind the menu
TICKS = os.sysconf('SC_CLK_TCK')
MISSES = 5         # failed reads in a row before a device path is looked up again


def uptime():
    """Seconds since boot, suspends included (what records and gaps are timed by)."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def suspended():
    """Seconds this boot has spent suspended: CLOCK_BOOTTIME counts them, CLOCK_MONOTONIC doesn't."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


def clocks(boot=uptime, wall=time.time):
    """(boot, wall) read together. A suspend between the two reads would pair a
    pre-suspend uptime with a post-resume wall time, so they are read again until
    the uptime didn't move across the wall-clock read."""
    while True:
        before = boot()
        now = wall()
        after = boot()
        if after - before < 1:
            return after, now


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return default


def number(path, default=None):
    try:
        return float(read(path))
    except (TypeError, ValueError):
        return default


def fraction(directory):
    value, top = number(directory / 'brightness'), number(directory / 'max_brightness')
    return round(value / top, 4) if value is not None and top else None


# --- Sub-second power readings -------------------------------------------------

def find_battery(root):
    """The first battery (batteries.find has them all; batteries.read combines them)."""
    return next(iter(batteries.find(root)), None)


mains_online = batteries.mains_online


def battery_watts(path):
    """One battery's signed power: positive while discharging, negative while charging."""
    return batteries.supply_watts(path)[:2]


def find_hwmon(root, name):
    for directory in sorted(root.glob('hwmon*')):
        if read(directory / 'name') == name:
            return directory
    return None


def resolved(path):
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


APU_FORMATS = (2, 3)  # gpu_metrics format revisions of APUs' tables (a discrete GPU's is 1).


def integrated(device, known=None):
    """Whether an amdgpu PCI device is an APU's integrated GPU, whose PPT is the
    whole chip's power, rather than a discrete GPU (a dGPU's PPT is its own
    board's): its gpu_metrics table is an APU's (format revision 2 or 3).

    A device asleep (runtime-suspended) is taken for a dGPU, as amdgpu doesn't
    suspend an APU's GPU at runtime (one that did would be asked again awake),
    and is never read: reading any amdgpu attribute wakes a sleeping dGPU (older
    kernels), and on an awake one restarts its autosuspend timer, so polling one
    would keep a hybrid laptop's dGPU powered for good. Each device is read at
    most once: answers go into `known` (by device)."""
    key = str(device)
    if known is not None and key in known:
        return known[key]
    if read(device / 'power/runtime_status') == 'suspended':
        return False  # Asking costs nothing: not kept, so a device is never judged by it alone.
    try:
        with open(device / 'gpu_metrics', 'rb') as source:
            header = source.read(4)
        answer = len(header) == 4 and header[2] in APU_FORMATS
    except OSError:  # No table (an Intel GPU, an old kernel), or it won't say.
        answer = False
    if known is not None:
        known[key] = answer
    return answer


def integrated_cards(sys, known=None):
    """The PCI devices of the DRM cards that are APUs' integrated GPUs, in card order."""
    devices = []
    for card in sorted((sys / 'class/drm').glob('card*')):
        if '-' in card.name:
            continue  # A connector (card1-eDP-1).
        device = resolved(card / 'device')
        if device is not None and device not in devices and integrated(device, known):
            devices.append(device)
    return devices


class Hwmon:
    """amdgpu's PPT (package power tracking) on an APU: the whole chip's power, µW."""
    source, counter = 'amdgpu', False

    def __init__(self, path):
        self.path = path

    def read(self):
        value = number(self.path)
        return value / 1e6 if value is not None else None


RAPL_GAP = 5.0          # s: a longer pause between readings (a suspend) starts the counter over.
RAPL_MAX_WATTS = 1000   # More from a counter delta is a counter reset, not power.


class Rapl:
    """Package power from RAPL's energy counters (powercap: Intel, and AMD since
    Zen): µJ, cumulative, wrapping at max_energy_range_uj. read() gives W since
    the reading before (None for the first, and after a gap or a reset); `span`
    is the seconds that covered, so a record can weigh by time.

    The counters are root-only since Linux 5.10 (and the stable kernels it went
    back to): PLATYPUS (CVE-2020-8694) showed that unprivileged energy readings
    leak secrets through power side channels (which instructions and data run,
    keys, KASLR). drainhound reads them only where an administrator has opted in,
    for instance with a group the user is in:
        /etc/tmpfiles.d/drainhound-rapl.conf:
            z /sys/class/powercap/intel-rapl:*/energy_uj 0440 root wheel -
    or a udev rule, which also covers the driver loading later:
        ACTION=="add", SUBSYSTEM=="powercap", KERNEL=="intel-rapl:*", \\
            RUN+="/bin/chgrp wheel /sys%p/energy_uj", RUN+="/bin/chmod g+r /sys%p/energy_uj"
    That gives every process of that group the side channel back; mode 0444 gives
    it to every local user. On an AMD APU drainhound uses amdgpu's PPT instead."""
    source, counter = 'rapl', True

    def __init__(self, zones, clock=None):
        self.zones, self.clock = zones, clock or uptime
        self.ranges = [number(zone / 'max_energy_range_uj') for zone in zones]
        self.last, self.span = None, 0.0

    def read(self):
        now, values = self.clock(), [number(zone / 'energy_uj') for zone in self.zones]
        last, self.last = self.last, ((now, values) if None not in values else None)
        if last is None or self.last is None or not 0 < now - last[0] <= RAPL_GAP:
            return None
        joules = 0.0
        for value, before, top in zip(values, last[1], self.ranges):
            if value < before:  # Wrapped: it counts on from zero past its range.
                if not top:
                    return None
                value += top
            joules += (value - before) / 1e6
        watts = joules / (now - last[0])
        if watts > RAPL_MAX_WATTS:
            return None
        self.span = now - last[0]
        return watts


def rapl_packages(sys):
    """RAPL's package domains (intel-rapl:N named package-N, not their core or
    uncore subzones, nor the MMIO interface that double-counts them) whose energy
    counter this user may read."""
    zones = []
    for zone in sorted((sys / 'class/powercap').glob('intel-rapl:*')):
        if zone.name.count(':') != 1 or not (read(zone / 'name') or '').startswith('package'):
            continue
        if os.access(zone / 'energy_uj', os.R_OK) and number(zone / 'energy_uj') is not None:
            zones.append(zone)
    return zones


def find_chip(sys, known=None, sensor=None, clock=None):
    """Where chip (package) power comes from (config chip.sensor, 'auto' by
    default): amdgpu's PPT on an AMD APU's integrated GPU, never a discrete GPU's
    (an Intel laptop with an AMD dGPU); else RAPL's package counters if this user
    can read them (Rapl); else None. 'amdgpu' takes the first amdgpu hwmon
    whatever it is (an APU with no gpu_metrics), 'rapl' only RAPL, 'none' none."""
    sensor = sensor or config.get('chip', 'sensor')
    sensor = sensor if sensor in ('amdgpu', 'rapl', 'none') else 'auto'
    if sensor in ('auto', 'amdgpu'):
        for hwmon in sorted((sys / 'class/hwmon').glob('hwmon*')):
            if read(hwmon / 'name') != 'amdgpu':
                continue
            device = resolved(hwmon / 'device')
            if sensor == 'amdgpu' or (device is not None and integrated(device, known)):
                for name in ('power1_input', 'power1_average'):
                    if (hwmon / name).exists():
                        return Hwmon(hwmon / name)
    if sensor in ('auto', 'rapl') and (zones := rapl_packages(sys)):
        return Rapl(zones, clock)
    return None


def find_gpu_busy(sys, known=None):
    """The integrated GPU's busy percent (a dGPU's isn't the chip's, and polling it keeps it awake)."""
    return next((path for device in integrated_cards(sys, known)
                 if (path := device / 'gpu_busy_percent').exists()), None)


def find_metrics(sys, known=None):
    return next((path for device in integrated_cards(sys, known) if (path := device / 'gpu_metrics').exists()), None)


RAIL_OFFSET = 44  # gpu_metrics v2.1–v2.4: average_soc_power, then average_gfx_power (uint16 mW each).
# Content revisions whose layout puts the rails at RAIL_OFFSET. v2.0 (Renoir's)
# has its timestamp earlier: socket and CPU power there. v3 (Strix Point's) is
# another table altogether.
RAIL_CONTENT = (1, 2, 3, 4)


def chip_rails(path):
    """(VDD rail W, SoC rail W) from amdgpu's gpu_metrics (format 2, the APUs'),
    or None. Measured on a Framework 13 (Ryzen 7040, SMU 13.0.4), the table's soc
    and gfx power fields are its two measured rails: VDDCR_VDD, which feeds the
    CPU cores and graphics, and VDDCR_SOC, the data fabric, memory controller,
    display and video engines and I/O hub. Package power (PPT) less both is the
    rest: memory and I/O interfaces, mostly (it rises with memory traffic).

    Only a table laid out that way is read: format 2, content RAIL_CONTENT, as
    long as its header says. Its socket power can't check the rails: it is
    filtered over seconds, they over a fraction of one (on a Framework 13 a
    load step puts them well above it for a while). Other APUs' rails may split
    the chip differently: they are logged as they are."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if len(data) < RAIL_OFFSET + 4 or data[2] != 2 or data[3] not in RAIL_CONTENT:
        return None
    if not RAIL_OFFSET + 4 <= int.from_bytes(data[0:2], 'little') <= len(data):
        return None  # Its header's size: a torn read, or not this table.
    soc = int.from_bytes(data[RAIL_OFFSET:RAIL_OFFSET + 2], 'little')
    vdd = int.from_bytes(data[RAIL_OFFSET + 2:RAIL_OFFSET + 4], 'little')
    if 0xFFFF in (soc, vdd):  # Not reported.
        return None
    return vdd / 1000, soc / 1000


class FastSampler:
    """Battery and chip power several times per record; the EC updates about 1 Hz.

    Device paths are looked up again after MISSES failed reads in a row (or while
    missing): hwmon and power-supply directories can be re-created and renumbered.
    `battery` is every battery (batteries.find), their watts summed, and looked up
    again every MISSES samples whatever their reads (battery_paths). The chip's
    source (find_chip) goes into the record as soc.src; one that counts energy
    (RAPL) gives the record's mean by time.
    """

    def __init__(self, sys=Path('/sys'), sensor=None):
        self.supplies = sys / 'class/power_supply'
        self.known = {}  # Which GPUs are integrated (integrated()): each is asked once.
        self.find = {'battery': lambda: batteries.find(self.supplies) or None,
                     'chip': lambda: find_chip(sys, self.known, sensor),
                     'gpu_busy': lambda: find_gpu_busy(sys, self.known), 'metrics': lambda: find_metrics(sys, self.known)}
        self.misses = collections.Counter()
        self.looks = 0  # Samples taken: the batteries are looked up again every MISSES.
        self.battery, self.chip, self.gpu_busy, self.metrics = (
            self.find[name]() for name in ('battery', 'chip', 'gpu_busy', 'metrics'))
        self.reset()

    def reset(self):
        self.values = collections.defaultdict(list)
        self.status = None
        self.joules, self.source = [0.0, 0.0], None  # A counting chip source's J and s this record.

    def path(self, name):
        """The device path, looked up again after MISSES failed reads (or, while
        there is none, every MISSES samples)."""
        if self.misses[name] >= MISSES:
            setattr(self, name, self.find[name]())
            self.misses[name] = 0
        if (path := getattr(self, name)) is None:
            self.misses[name] += 1
        return path

    def battery_paths(self):
        """The batteries, as path('battery'), and also looked up again every
        MISSES samples: a set of them is more than one path, and each can read
        well while another is put in (a second battery, a removable one back in
        its bay), which failed reads never show."""
        self.looks += 1
        if self.battery is not None and self.looks % MISSES == 0:
            self.battery = self.find['battery']()
        return self.path('battery')

    def got(self, name, value):
        self.misses[name] = 0 if value is not None else self.misses[name] + 1
        return value

    def sample(self):
        if (paths := self.battery_paths()) is not None:
            self.status, watts = batteries.watts(paths)
            if watts is None and not all(path.exists() for path in paths):
                # One has gone (taken out): the rest at once, not MISSES samples of none.
                self.battery = self.find['battery']()
                if self.battery is not None:
                    self.status, watts = batteries.watts(self.battery)
            if self.got('battery', watts) is not None:
                self.values['bat'].append(watts)
        if (chip := self.path('chip')) is not None and self.got('chip', value := chip.read()) is not None:
            self.values['soc'].append(value)
            self.source = chip.source
            if chip.counter:
                self.joules[0] += value * chip.span
                self.joules[1] += chip.span
        if (busy := self.path('gpu_busy')) is not None and self.got('gpu_busy', value := number(busy)) is not None:
            self.values['gpu_busy'].append(value)
        if (metrics := self.path('metrics')) is not None and self.got('metrics', rails := chip_rails(metrics)):
            self.values['vdd'].append(rails[0])
            self.values['fabric'].append(rails[1])

    def take(self):
        result = {name: {'w': round(sum(v) / len(v), 3), 'min': round(min(v), 3),
                         'max': round(max(v), 3), 'n': len(v)}
                  for name, v in self.values.items() if v}
        if 'soc' in result:
            if self.joules[1] > 0:
                result['soc']['w'] = round(self.joules[0] / self.joules[1], 3)
            result['soc']['src'] = self.source
        status = self.status
        self.reset()
        return result, status


# --- Cumulative counters read once per record ----------------------------------

def cpu_times(proc=Path('/proc')):
    result = {}
    try:
        with open(proc / 'stat') as source:
            for line in source:
                parts = line.split()
                if parts[0] == 'cpu':
                    user, nice, system, idle, iowait, irq, softirq, steal = map(int, parts[1:9])
                    result.update(user=(user + nice) / TICKS, sys=system / TICKS,
                                  irq=(irq + softirq) / TICKS, iowait=iowait / TICKS,
                                  idle=idle / TICKS, steal=steal / TICKS)
                elif parts[0] in ('ctxt', 'intr'):
                    result[parts[0]] = int(parts[1])
    except (OSError, ValueError, IndexError):
        pass
    return result


class IdleStates:
    """C-state residency (seconds) per (state name, CPU): idle_deltas() sums the
    per-CPU increases by name, so a CPU going offline (or back) moves nothing."""

    def __init__(self, sys=Path('/sys')):
        self.paths = [(read(state / 'name', state.name), state / 'time')
                      for state in sorted(sys.glob('devices/system/cpu/cpu[0-9]*/cpuidle/state[0-9]*'))]

    def read(self):
        return {(name, str(path)): value / 1e6 for name, path in self.paths if (value := number(path)) is not None}


def idle_deltas(after, before, scale=1.0):
    totals = collections.defaultdict(float)
    for key, value in after.items():
        if key in before:
            totals[key[0]] += max(0.0, value - before[key]) * scale
    return {name: round(value, 4) for name, value in totals.items()}


def own_cpu(proc=Path('/proc')):
    """The CPU this thread last ran on (/proc/thread-self/stat field 39), or None."""
    try:
        stat = (proc / 'thread-self/stat').read_text()
        return int(stat[stat.rindex(')') + 2:].split()[36])  # Fields after comm start at field 3.
    except (OSError, ValueError, IndexError):
        return None


def cpu_frequency(sys=Path('/sys'), own=None):
    """(mean MHz, peak MHz, active mean MHz, active count) over CPUs right now.

    The mean and peak take every CPU's scaling_cur_freq, as always. Under
    amd-pstate an idle CPU reads exactly its scaling_min_freq, a placeholder, so
    the active mean and count take only CPUs reading anything else, leaving out
    `own` (the collector's CPU, always busy while it reads). Active mean is None
    when no CPU is active."""
    values, active = [], []
    for path in sys.glob('devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq'):
        if not (current := number(path)):
            continue
        values.append(current / 1000)
        if path.parent.parent.name != f'cpu{own}' and current != number(path.with_name('scaling_min_freq')):
            active.append(current / 1000)
    if not values:
        return None, None, None, 0
    return sum(values) / len(values), max(values), sum(active) / len(active) if active else None, len(active)


def clock_means(samples):
    """A record's clock fields from (seconds, mean, peak, active mean, active count)
    samples: `mhz` and `mhz_max` as ever (plain mean of the means, the peak);
    `mhz_active` time-weighted over samples with an active CPU (absent if none
    had one); `active_n` the time-weighted mean active count. A sample weighs
    the time since the one before it."""
    if not samples:
        return {}
    weights = [w for w, *_ in samples] if sum(w for w, *_ in samples) > 0 else [1.0] * len(samples)
    result = {'mhz': round(sum(s[1] for s in samples) / len(samples)), 'mhz_max': round(max(s[2] for s in samples)),
              'active_n': round(sum(w * s[4] for w, s in zip(weights, samples)) / sum(weights), 2)}
    active = [(w, s[3]) for w, s in zip(weights, samples) if s[4]]
    if active:
        total = sum(w for w, _ in active)
        result['mhz_active'] = round(sum(w * m for w, m in active) / total if total > 0
                                     else sum(m for _, m in active) / len(active))
    return result


def interrupts(proc=Path('/proc')):
    counts = {}
    try:
        with open(proc / 'interrupts') as source:
            cpus = len(source.readline().split())
            for line in source:
                label, _, rest = line.partition(':')
                parts = rest.split()
                values = []
                for part in parts[:cpus]:
                    if not part.isdigit():
                        break
                    values.append(int(part))
                label = label.strip()
                # Numbered lines: counts, chip, hwirq-type, then device names.
                name = ' '.join(parts[len(values) + 2:]) if label.isdigit() else ''
                counts[f'{label} {name}'.strip()[:48]] = sum(values)
    except OSError:
        pass
    return counts


def network(sys=Path('/sys')):
    result = {}
    for iface in sorted((sys / 'class/net').glob('*')):
        if iface.name == 'lo':
            continue
        stats = iface / 'statistics'
        values = {key: number(stats / name) for key, name in (
            ('rx', 'rx_bytes'), ('tx', 'tx_bytes'), ('rxp', 'rx_packets'), ('txp', 'tx_packets'))}
        if None in values.values():
            continue  # Going away mid-read: no reading, not zeros (a later one would count from zero).
        result[iface.name] = {key: int(value) for key, value in values.items()}
        result[iface.name]['state'] = read(iface / 'operstate', '')
    return result


def disks(sys=Path('/sys')):
    result = {}
    for device in sorted((sys / 'block').glob('*')):
        if not device.name.startswith(('nvme', 'sd', 'mmcblk')):
            continue
        try:
            fields = [int(v) for v in read(device / 'stat', '').split()]
            result[device.name] = {'rd': fields[2] * 512, 'wr': fields[6] * 512, 'busy': fields[9] / 1000}
        except (ValueError, IndexError):
            continue
    return result


def wifi_signal(proc=Path('/proc')):
    try:
        lines = (proc / 'net/wireless').read_text().splitlines()[2:]
        return {line.split(':')[0].strip(): float(line.split()[3].rstrip('.')) for line in lines}
    except (OSError, ValueError, IndexError):
        return {}


def usb_active(sys=Path('/sys')):
    active = []
    for device in sorted((sys / 'bus/usb/devices').glob('*')):
        if ':' in device.name or device.name.startswith('usb'):
            continue  # Interfaces and root hubs.
        if read(device / 'power/runtime_status') == 'active':
            active.append(read(device / 'product') or
                          f"{read(device / 'idVendor', '?')}:{read(device / 'idProduct', '?')}")
    return active


def pci_active(sys=Path('/sys')):
    counts = collections.Counter()
    for device in sorted((sys / 'bus/pci/devices').glob('*')):
        if read(device / 'power/runtime_status') == 'active':
            try:
                counts[(device / 'driver').readlink().name] += 1
            except OSError:
                counts[read(device / 'class', '?')] += 1
    return dict(counts)


def audio_streams(proc=Path('/proc')):
    result = {'play': 0, 'rec': 0}
    for status in proc.glob('asound/card*/pcm*/sub*/status'):
        if (read(status) or '').startswith('state: RUNNING'):
            result['play' if status.parent.parent.name.endswith('p') else 'rec'] += 1
    return result


def display(sys=Path('/sys')):
    result = {}
    backlight = next(iter(sorted((sys / 'class/backlight').glob('*'))), None)
    if backlight is not None:
        result['bl'] = fraction(backlight)
    keyboard = [f for f in (fraction(led) for led in (sys / 'class/leds').glob('*kbd_backlight*'))
                if f is not None]
    if keyboard:
        result['kbd'] = max(keyboard)
    outputs = []
    for connector in sorted((sys / 'class/drm').glob('card*-*')):
        if read(connector / 'status') != 'connected':
            continue
        name = connector.name.partition('-')[2]
        outputs.append({'name': name, 'enabled': read(connector / 'enabled'), 'dpms': read(connector / 'dpms')})
        if name.startswith(('eDP', 'LVDS', 'DSI')):
            abm = number(connector / 'amdgpu/panel_power_savings')
            if abm is not None:
                result['abm'] = int(abm)
    result['outputs'] = outputs
    return result


def throttle_state(path):
    """'on' while drainhound throttle runs (its state file names a live process), else 'off'."""
    try:
        os.kill(int(json.loads(Path(path).read_text())['pid']), 0)
        return 'on'
    except (OSError, ValueError, KeyError, TypeError):
        return 'off'


def settings(sys=Path('/sys')):
    cpu = sys / 'devices/system/cpu'
    values = {
        'platform_profile': read(sys / 'firmware/acpi/platform_profile'),
        'epp': read(cpu / 'cpu0/cpufreq/energy_performance_preference'),
        'governor': read(cpu / 'cpu0/cpufreq/scaling_governor'),
        'boost': read(cpu / 'cpufreq/boost'),
        'amd_pstate': read(cpu / 'amd_pstate/status'),
        'aspm': next((w[1:-1] for w in (read(sys / 'module/pcie_aspm/parameters/policy') or '').split()
                      if w.startswith('[')), None),
        # The drive's runtime APST limit (what drainhound-helper changes), not nvme_core's boot default.
        'nvme_apst_us': next((read(path) for path in sorted(sys.glob('class/nvme/nvme*/power/pm_qos_latency_tolerance_us'))),
                             read(sys / 'module/nvme_core/parameters/default_ps_max_latency_us')),
        # Boot-level: bit 0x10 disables panel self-refresh (see the psr specialisation).
        'dcdebugmask': read(sys / 'module/amdgpu/parameters/dcdebugmask'),
    }
    if (limit := number(cpu / 'cpu0/cpufreq/scaling_max_freq')) is not None:
        values['max_mhz'] = round(limit / 1000)
    # The floor an idle CPU reads as its clock under amd-pstate (it follows the profile).
    if (limit := number(cpu / 'cpu0/cpufreq/scaling_min_freq')) is not None:
        values['min_mhz'] = round(limit / 1000)
    # The CPU's own maximum: boost raises it, a cap (max_mhz below it) doesn't.
    if (limit := number(cpu / 'cpu0/cpufreq/cpuinfo_max_freq')) is not None:
        values['cpu_max_mhz'] = round(limit / 1000)
    # amdgpu DPM: 'low' pins the GPU, memory and fabric clocks at their lowest.
    values['gpu_dpm'] = next((read(path) for path in sorted(sys.glob('class/drm/card*/device/power_dpm_force_performance_level'))),
                             None)
    if (reading := batteries.read(sys / 'class/power_supply')) is not None:
        values['charge_limit'] = reading['limit']  # Several batteries: where the whole stops.
    for switch in sorted((sys / 'class/rfkill').glob('rfkill*')):
        values[f"rfkill_{read(switch / 'type')}"] = 'on' if read(switch / 'soft') == read(switch / 'hard') == '0' else 'off'
    return {key: value for key, value in values.items() if value is not None}


def thermal(sys=Path('/sys')):
    result = {}
    if (k10temp := find_hwmon(sys / 'class/hwmon', 'k10temp')) and (value := number(k10temp / 'temp1_input')) is not None:
        result['temp'] = value / 1000
    for name in ('framework_laptop', 'cros_ec'):
        if (hwmon := find_hwmon(sys / 'class/hwmon', name)) and (value := number(hwmon / 'fan1_input')) is not None:
            result['fan'] = value
            break
    return result


def lid_state(proc=Path('/proc')):
    for path in proc.glob('acpi/button/lid/*/state'):
        words = (read(path) or '').split()
        return words[-1] if words else None
    return None


class Commands:
    """Slow-changing state that needs a helper process; refreshed once a minute."""

    def __init__(self, interval=60):
        self.interval, self.when, self.value = interval, None, {}

    def read(self, now):
        if self.when is not None and now - self.when < self.interval:
            return self.value
        self.when, self.value = now, {}
        if shutil.which('iw'):
            output = self.run(['iw', 'dev'])
            for iface in [line.split()[1] for line in output.splitlines() if line.strip().startswith('Interface ')]:
                state = self.run(['iw', 'dev', iface, 'get', 'power_save'])
                if 'Power save:' in state:
                    self.value.setdefault('wifi_ps', {})[iface] = state.split(':')[1].strip()
        if (outputs := compositor_outputs(self.run)) is not None:
            self.value['compositor'] = outputs
        return self.value

    @staticmethod
    def run(command):
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return ''


def niri_output(output):
    modes, current = output.get('modes') or [], output.get('current_mode')
    mode = modes[current] if isinstance(current, int) and 0 <= current < len(modes) else {}
    logical = output.get('logical') or {}
    return {'name': output.get('name'), 'w': mode.get('width'), 'h': mode.get('height'),
            'hz': mode.get('refresh_rate') and round(mode['refresh_rate'] / 1000, 2),
            'vrr': output.get('vrr_enabled'), 'scale': logical.get('scale'), 'on': bool(mode)}


def sway_output(output):
    """One output of `swaymsg -t get_outputs`, as niri_output has niri's. A
    disabled output has no mode (on: False); one sway has powered down (DPMS:
    `power`, `dpms` before sway 1.8) keeps its mode but isn't on."""
    mode = (output.get('current_mode') if output.get('active', True) else None) or {}
    awake = output.get('power', output.get('dpms', True)) is not False
    return {'name': output.get('name'), 'w': mode.get('width'), 'h': mode.get('height'),
            'hz': mode.get('refresh') and round(mode['refresh'] / 1000, 2),
            'vrr': output.get('adaptive_sync_status') == 'enabled' if 'adaptive_sync_status' in output else None,
            'scale': output.get('scale') if mode else None, 'on': bool(mode) and awake}


# The compositors whose outputs the collector logs, by compositor.detect()'s name:
# the command and how its reply lists outputs.
OUTPUTS = {'niri': (['niri', 'msg', '-j', 'outputs'], lambda reply: reply.values(), niri_output),
           'sway': (['swaymsg', '-r', '-t', 'get_outputs'], lambda reply: reply, sway_output)}


def compositor_outputs(run, environ=None):
    """The record's display.compositor: {'name': 'niri' or 'sway', 'outputs':
    [{'name', 'w', 'h', 'hz', 'vrr', 'scale', 'on'}]} from the compositor this
    session runs, chosen as throttle and freeze choose it (compositor.detect()),
    or None (another, none, or no reply). Records before sway was logged have
    niri's outputs, in the same shape, as display.niri (store.edp reads either)."""
    if (name := compositor.detect(environ).name) not in OUTPUTS:
        return None
    command, listed, shape = OUTPUTS[name]
    if not shutil.which(command[0]):
        return None
    try:
        return {'name': name, 'outputs': [shape(o) for o in listed(json.loads(run(command) or 'null'))
                                          if isinstance(o, dict)]}
    except (ValueError, TypeError, AttributeError):  # No reply, or not the shape asked for.
        return None


# --- Per-process GPU and IO ----------------------------------------------------

VIDEO_ENGINES = ('dec', 'enc', 'jpeg', 'vcn', 'vpe', 'video')


def engine_group(engine):
    if engine in ('gfx', 'compute', 'render'):
        return 'gpu'
    return 'video' if engine.startswith(VIDEO_ENGINES) else engine


def parse_fdinfo(text):
    client, engines = None, {}
    for line in text.splitlines():
        key, _, value = line.partition(':')
        if key == 'drm-client-id':
            client = value.strip()
        elif key.startswith('drm-engine-') and not key.startswith('drm-engine-capacity'):
            try:
                engines[key[len('drm-engine-'):]] = int(value.split()[0])
            except (ValueError, IndexError):
                continue
    return client, engines


class GpuClients:
    """Per-app GPU engine time from DRM fdinfo, deduplicated by client id.

    A process's DRM fds are looked up when it is new, when its number of open fds
    changes, and every `rescan` seconds regardless. So a client is found in the
    interval it opens, and one new since the last sample counts from zero if its
    process was watched then (or started since); one only a periodic rescan
    found has unknown history and waits a sample. Per app and engine group
    (gpu: gfx + compute; video: decode, encode, …) engine-seconds are summed over
    its clients and engines, then capped at the interval: concurrent rings and
    overlapping jobs report more busy time than there was time.
    """

    def __init__(self, proc=Path('/proc'), rescan=60):
        self.proc, self.rescan = proc, rescan
        self.fds, self.scanned, self.counts, self.previous, self.since = {}, {}, {}, None, None

    def fd_count(self, pid):
        """Open fds: /proc/PID/fd's size (Linux 6.2+; 0 before, which leaves the periodic rescan)."""
        try:
            return os.stat(self.proc / str(pid) / 'fd').st_size
        except OSError:
            return None

    def scan(self, pid, last=None):
        """(DRM fds, every fd) of a process. Given the `last` scan, only fd
        numbers new since are resolved (one reused meanwhile waits for the
        periodic full scan)."""
        found, names = [], set()
        try:
            for entry in os.scandir(self.proc / str(pid) / 'fd'):
                names.add(entry.name)
                if last is not None and entry.name in last[1]:
                    if entry.name in last[0]:
                        found.append(entry.name)
                    continue
                try:
                    if os.readlink(entry.path).startswith('/dev/dri/'):
                        found.append(entry.name)
                except OSError:
                    continue
        except OSError:
            pass
        return found, names

    def sample(self, now, processes):
        current, fresh = {}, set()
        for key in list(self.scanned):
            if key not in processes:
                del self.scanned[key], self.fds[key], self.counts[key]
        for key, entry in processes.items():
            known, count = key in self.scanned, self.fd_count(key[0])
            periodic = known and now - self.scanned[key] >= self.rescan
            if not known or periodic:
                self.fds[key], self.scanned[key], self.counts[key] = self.scan(key[0]), now, count
            elif count != self.counts[key]:
                self.fds[key], self.counts[key] = self.scan(key[0], self.fds[key]), count
            # Its fds were watched last sample (or it started since): a client new
            # to us opened since. A periodic rescan may find an older one.
            watched = (known and not periodic) or (self.since is not None and key[1] / TICKS >= self.since)
            for fd in self.fds[key][0]:
                client, engines = parse_fdinfo(read(self.proc / str(key[0]) / 'fdinfo' / fd, ''))
                if client is not None and engines:
                    current.setdefault(client, (entry[3], engines))
                    if watched:
                        fresh.add(client)
        usage = collections.defaultdict(lambda: collections.defaultdict(float))
        if self.previous is not None:
            for client, (app, engines) in current.items():
                before = self.previous.get(client)
                if before is None and client not in fresh:
                    continue  # Its history is unknown.
                for engine, ns in engines.items():
                    # amdgpu lists only engines a client has used: one new to it starts at zero.
                    if (delta := ns - (before[1].get(engine, 0) if before else 0)) > 0:
                        usage[app][engine_group(engine)] += delta / 1e9
            interval = now - self.since
            for groups in usage.values():
                for group, seconds in groups.items():
                    groups[group] = min(seconds, interval) if interval > 0 else seconds
        self.previous, self.since = current, now
        return usage


class Deltas:
    """Per-app increase of cumulative per-process counters, safe against PID reuse."""

    def __init__(self):
        self.previous, self.since = None, None

    def sample(self, boot, values, apps):
        usage = collections.defaultdict(lambda: collections.defaultdict(float))
        for key, counters in values.items():
            if key not in apps:
                continue  # Not in this scan (raced with process start/exit).
            before = self.previous.get(key) if self.previous is not None else None
            if before is None:
                # Count a process from zero only if it started after the last sample.
                if self.since is None or key[1] / TICKS < self.since:
                    continue
                before = {}
            for name, value in counters.items():
                if (delta := value - before.get(name, 0)) > 0:
                    usage[apps[key]][name] += delta
        self.previous, self.since = values, boot
        return usage


def process_io(proc, keys):
    result = {}
    for key in keys:
        try:
            counters = {}
            with open(proc / str(key[0]) / 'io') as source:
                for line in source:
                    name, _, value = line.partition(':')
                    if name in ('read_bytes', 'write_bytes'):
                        counters['io_r' if name == 'read_bytes' else 'io_w'] = int(value)
            result[key] = counters
        except (OSError, ValueError):
            continue
    return result


# --- Online models and attribution ---------------------------------------------

# Model terms and the priors they start from. drainhound eta trains the real
# values (report.fit) into models.json every 15 minutes; the collector only
# reads them. report.py shares this dict.
# Chip W = floor + clock·GHz + load·busy·GHz² + gpu + video: the mean core clock
# costs ~3 W per GHz even with the cores idle (the whole package's voltage
# follows it), which busy·GHz² alone missed. `idle_ghz` is where the clock sits
# with nothing to do (report.fit_rows learns it, and each profile's: see
# clock_split): the clock up to it is baseline, above it apps drove it up.
# 'clock_active' is the W per GHz of the active cores' clock, used instead once
# minutes log it (see clock_split).
PRIOR = {'floor': 3.0, 'load': 0.12, 'clock': 3.0, 'idle_ghz': 1.0, 'gpu': 5.0, 'video': 1.0, 'clock_active': 1.0,
         'base': 0.5, 'chip': 1.3, 'bl': 3.0, 'wifi': 0.3, 'disk': 2.0, 'usb': 0.3, 'audio': 0.5, 'kbd': 0.3}


# The chip's draw with nothing running, at its idle clock: the floor and the
# clock term up to the idle clock (clock_split), at k per chip watt on battery.
# Split by the chip's measured rails once report.fit_rows has learned their
# shares of it ('rail:<rail>', power.chip_rails); the battery's watts beyond the
# chip's (k − 1 per chip watt: voltage conversion, and board parts whose draw
# follows the chip's) are power delivery.
IDLE = 'Idle chip'
IDLE_PARTS = (('links', 'Memory & I/O links'), ('fabric', 'Fabric & display engine'), ('cores', 'Cores & graphics'))
DELIVERY = 'Power delivery & board'
RAILS = tuple(rail for rail, _ in IDLE_PARTS)


def idle_parts(params, chip, scale):
    """The idle chip's `chip` W (or J) at `scale` battery W per chip W as
    {part: amount}: the rails' shares of it and power delivery, or {IDLE: all}
    until the shares are learned. The parts sum to scale × chip."""
    shares = [params.get(f'rail:{rail}') for rail in RAILS]
    if any(share is None for share in shares):
        return {IDLE: scale * chip}
    parts = {label: chip * min(1.0, scale) * share for (_, label), share in zip(IDLE_PARTS, shares)}
    if scale > 1:
        parts[DELIVERY] = chip * (scale - 1)
    return parts


def load_attribution(path):
    """Trained attribution parameters from drainhound eta's models.json, or None:
    PRIOR's keys, the idle clocks report.fit_rows learned (each profile's
    'idle_ghz:<profile>[:ac]', and 'idle_ghz_active'), the idle chip's
    rail shares ('rail:<rail>') and 'stages' (1: the one-stage model of a
    laptop without chip power; absent, as in every file before models.json
    version 6: the chip and battery models)."""
    try:
        data = json.loads(Path(path).read_text())
        return {**PRIOR, **{k: float(v) for k, v in data['attribution'].items()
                            if k in PRIOR or k == 'stages' or k.startswith(('idle_ghz', 'rail:'))}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def clock_split(params, ghz, profile=None, ac=False, active=None):
    """Chip watts of the clock term: (baseline part, part above the idle clock).

    Idle cores report a placeholder clock set by the platform profile (and AC),
    so the mean clock `ghz` is measured from the profile's own idle clock
    ('idle_ghz:<profile>:ac' on AC, then 'idle_ghz:<profile>', then the pooled
    'idle_ghz'; report.fit_rows learns them): baseline up to it, apps above it.
    The model's clock is on the pooled idle clock's scale (report.clock_of).
    Once the model has an active-core clock ('idle_ghz_active') and the minute
    or record has one (`active`, GHz: no placeholders in it), that is used
    instead: the pooled baseline, plus 'clock_active' W per GHz from its idle."""
    pooled = params['idle_ghz']
    if active is not None and 'idle_ghz_active' in params:
        excess = params['clock_active'] * (active - params['idle_ghz_active'])
        return max(0.0, params['clock'] * pooled + min(0.0, excess)), max(0.0, excess)
    if ghz is None:
        return params['clock'] * pooled, 0.0
    idle = params.get(f'idle_ghz:{profile}', pooled)
    idle = params.get(f'idle_ghz:{profile}:ac', idle) if ac else idle
    return params['clock'] * max(0.0, pooled + min(0.0, ghz - idle)), params['clock'] * max(0.0, ghz - idle)


def platform_features(record):
    dt, shown = record['dt'], record.get('display', {})
    wifi = sum(n.get('rx', 0) + n.get('tx', 0) for name, n in record.get('net', {}).items() if name.startswith('wl'))
    return [1.0, record.get('soc', {}).get('w', 0), shown.get('bl') or 0, wifi / dt / 1e6,
            min(1.0, sum(d.get('busy', 0) for d in record.get('disk', {}).values()) / dt),
            len(record.get('usb', [])), min(1, sum(record.get('audio', {}).values())), shown.get('kbd') or 0]


def discharging(record):
    return record.get('status') == 'Discharging' and 'w' in record.get('bat', {})


DEVICE_TERMS = (('Display', 'bl', 2), ('Wi-Fi', 'wifi', 3), ('Storage', 'disk', 4),
                ('USB devices', 'usb', 5), ('Audio', 'audio', 6), ('Keyboard backlight', 'kbd', 7))


def attribute(record, params):
    """Split one record's measured energy (joules) across apps and hardware.

    On battery each chip watt is scaled by the model's per-chip-watt cost (k),
    so apps carry their share of conversion losses; on AC only chip power is
    known. `params` are drainhound eta's trained values (see PRIOR). The clock
    term up to the profile's idle clock (clock_split) is the idle chip's (IDLE,
    as idle_parts once its rails are learned: 'Idle chip/<part>' rows); above
    it, apps share it by CPU time (theirs is what raised the clock).
    report.attribute splits a minute the same way, by load.

    Without a chip power reading, and on battery for a one-stage model
    ('stages' 1: its chip terms are battery W, k is 1), the chip's part is
    the model's estimate (its floor, the clock and every app's terms), and
    the modelled parts are scaled down together if they come to more than the
    battery delivered. On AC without a chip reading nothing is measured: no rows.
    """
    dt, rows = record['dt'], collections.defaultdict(float)
    soc = record.get('soc', {}).get('w')
    on_battery = discharging(record)
    measured = soc is not None and not (on_battery and params.get('stages') == 1)
    if not (measured or on_battery):
        return rows
    mhz, active = record.get('cpu', {}).get('mhz'), record.get('cpu', {}).get('mhz_active')
    ghz = mhz / 1000 if mhz else None
    ghz2 = ghz * ghz if ghz else 0
    idle, raised = clock_split(params, ghz, (record.get('settings') or {}).get('platform_profile'), not on_battery,
                               active / 1000 if active else None)
    apps = record.get('apps', {})
    cpu = {name: app.get('cpu', 0) for name, app in apps.items()}
    cpu['Kernel'] = cpu.get('Kernel', 0) + max(0, record.get('cpu', {}).get('busy_s', 0) - sum(cpu.values()))
    busy = sum(cpu.values())
    if busy <= 0:
        idle += raised  # Nothing ran to raise it.
    weights = {}
    for name, seconds in cpu.items():
        app = apps.get(name, {})
        weights[name] = ((params['load'] * seconds * ghz2 + params['gpu'] * app.get('gpu', 0)
                          + params['video'] * app.get('video', 0)) / dt
                         + (raised * seconds / busy if busy > 0 else 0))
    total = sum(weights.values())
    if not measured:
        soc = params['floor'] + idle + total  # The chip model's estimate (one-stage: in battery W).
    parts = {}
    if on_battery:
        x = platform_features(record)
        parts = {name: params[term] * x[index] for name, term, index in DEVICE_TERMS}
    modelled = sum(parts.values())
    scale = params['chip'] if on_battery else 1.0
    if on_battery and measured and soc > 0:
        scale = max(0.0, min(scale, record['bat']['w'] / soc))  # Never more than the battery delivered.
    elif on_battery and scale * soc + modelled > 0:  # Estimated: every modelled part scaled down to the draw.
        scale *= max(0.0, min(1.0, record['bat']['w'] / (scale * soc + modelled)))
    floor = min(soc, params['floor'] + idle)
    if total > 0:
        for name, weight in weights.items():
            if weight > 0:
                rows[name] += scale * (soc - floor) * dt * weight / total
    else:
        floor = soc
    for name, joules in idle_parts(params, floor * dt, scale).items():
        rows[name if name == IDLE else f'{IDLE}/{name}'] += joules
    if on_battery:
        rest = record['bat']['w'] - scale * soc
        shrink = min(1.0, max(0.0, rest) / modelled) if modelled > 0 else 0
        for name, watts in parts.items():
            if watts > 0:
                rows[name] += watts * shrink * dt
        rows['Rest of system'] += max(0.0, rest - modelled) * dt
    return rows


def percentages(values):
    """Whole percentages of `values` that sum to 100 (largest remainders round up)."""
    total = sum(values)
    if total <= 0:
        return [0] * len(values)
    exact = [100 * v / total for v in values]
    whole = [int(x) for x in exact]
    for i in sorted(range(len(exact)), key=lambda i: whole[i] - exact[i])[:100 - sum(whole)]:
        whole[i] += 1
    return whole


OTHERS = 'Everything else'
TAIL = 3  # Rows of the rest named under it.


def summarize(window, now, log_totals, limit=6):
    """Menu block: average watts per row over the recent window, the biggest
    `limit` − 1 and the rest as one row (its biggest TAIL listed under it), so
    the shares add up to 100%. A row split into parts ('Idle chip/<part>'
    rows) lists them under it."""
    battery = [w for w in window if w[2]]
    use = battery or window
    seconds = sum(w[1] for w in use)
    totals, parts = collections.defaultdict(float), collections.defaultdict(lambda: collections.defaultdict(float))
    for _, _, _, rows in use:
        for name, joules in rows.items():
            parent, _, part = name.partition('/')
            totals[parent] += joules
            if part:
                parts[parent][part] += joules
    energy = sum(totals.values())
    minutes = max(1, round(seconds / 60))
    if not use or seconds <= 0:
        title = 'Battery use: measuring…'
    elif battery:
        title = f'Battery use · last {minutes} min on battery · {energy / seconds:.1f} W'
    elif energy > 0:
        title = f'On AC · chip power by app, last {minutes} min · {energy / seconds:.1f} W'
    else:  # On AC only the chip's draw is measured (attribute): without a reading of it, nothing is.
        title = 'On AC · no chip power reading, so nothing to split by app'
    rows = []
    if seconds > 0 and energy > 0:
        ranked = sorted(totals.items(), key=lambda r: (-r[1], r[0]))
        shown = [r for r in ranked[:limit - 1 if len(ranked) > limit else limit] if r[1] / seconds >= 0.05]
        rest = ranked[len(shown):]
        if len(rest) > 1:
            name = f'{OTHERS} ({len(rest)})'
            shown.append((name, sum(j for _, j in rest)))
            parts[name] = dict(r for r in rest[:TAIL] if r[1] / seconds >= 0.05)
        elif rest:
            shown.append(rest[0])
        for (name, joules), pct in zip(shown, percentages([j for _, j in shown])):
            row = {'name': name, 'value': f'{joules / seconds:4.1f} W {pct:3d}%'}
            if parts.get(name):
                row['parts'] = [{'name': part, 'value': f'{j / seconds:4.1f} W'}
                                for part, j in sorted(parts[name].items(), key=lambda r: -r[1])]
            rows.append(row)
    return {'title': title, 'rows': rows, 'footer': log_footer(*log_totals), 'timestamp': now}


def log_footer(records, seconds, first):
    if not records:
        return 'Power log: no samples yet'
    span = f'{seconds / 3600:.1f} h' if seconds >= 3600 else f'{seconds / 60:.0f} min'
    since = datetime.datetime.fromtimestamp(first).strftime('%b %-d') if first else '?'
    return f'Power log: {records:,} samples · {span} since {since}'


# --- Persistent log --------------------------------------------------------------

class PowerLog:
    """Daily JSON Lines files; finished days are compressed and summarized in index.json."""

    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.index_path = directory / 'index.json'
        try:
            self.index = json.loads(self.index_path.read_text())
            assert isinstance(self.index.get('days'), dict)
        except (OSError, ValueError, AssertionError, AttributeError):
            self.index = {'days': {}}
        self.handle, self.day, self.pending = None, None, 0
        today = self.day_of(time.time())
        for path in sorted(directory.glob('*.jsonl')):
            if path.stem != today:
                try:
                    self.compress(path)
                except Exception as error:  # A full disk, say: the plain file stays, compressed next start.
                    print(f'drainhound: compressing {path}: {type(error).__name__}: {error}', file=stderr)
        for path in sorted(directory.glob('*.jsonl*')):
            if path.suffix not in ('.jsonl', '.zst'):
                continue  # An interrupted compression's .part file.
            day = path.name.split('.')[0]
            if day == today or day not in self.index['days']:
                self.index['days'][day] = self.scan(path)

    @staticmethod
    def day_of(wall):
        return datetime.date.fromtimestamp(wall).isoformat()

    @staticmethod
    def scan(path):
        stats = {'records': 0, 'seconds': 0.0, 'first': None, 'last': None}
        try:
            with PowerLog.open(path) as source:
                for line in source:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue  # A torn final line after a crash.
                    PowerLog.count(stats, record)
        except (EOFError, ValueError):
            pass  # Includes torn compressed files; the counts so far still stand.
        except Exception as error:  # Unreadable or corrupt (ZstdError is a plain Exception): so do they.
            print(f'drainhound: {path}: {type(error).__name__}: {error}', file=stderr)
        return stats

    @staticmethod
    def open(path):
        return store.open_log(path)

    @staticmethod
    def count(stats, record):
        if 'event' in record or not isinstance(record.get('dt'), (int, float)):
            return
        stats['records'] += 1
        stats['seconds'] += record['dt']
        stats['first'] = min(filter(None, (stats['first'], record['t0'])))
        stats['last'] = max(filter(None, (stats['last'], record['t1'])))

    def compress(self, path):
        target = path.with_name(path.name + '.zst')
        partial = target.with_name(target.name + '.part')
        with open(path, 'rb') as source, zstd.open(partial, 'wb', level=10) as sink:
            shutil.copyfileobj(source, sink)
        partial.replace(target)
        path.unlink()

    def write(self, record):
        day = self.day_of(record['t1'])
        if day != self.day:
            if self.handle is not None:
                self.handle.close()
                try:
                    self.compress(self.directory / f'{self.day}.jsonl')
                except Exception as error:  # Left plain; the next start compresses it.
                    print(f'drainhound: compressing {self.day}: {type(error).__name__}: {error}', file=stderr)
            self.handle, self.day = open(self.directory / f'{day}.jsonl', 'a'), day
        self.handle.write(json.dumps(record, separators=(',', ':'), ensure_ascii=False) + '\n')
        self.count(self.index['days'].setdefault(
            day, {'records': 0, 'seconds': 0.0, 'first': None, 'last': None}), record)
        self.pending += 1
        if self.pending >= 6 or 'event' in record:
            self.flush()

    def flush(self):
        if self.handle is not None:
            self.handle.flush()
        temporary = self.index_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.index))
        temporary.replace(self.index_path)
        self.pending = 0

    def close(self):
        self.flush()
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def totals(self):
        days = self.index['days'].values()
        firsts = [d['first'] for d in days if d['first']]
        return sum(d['records'] for d in days), sum(d['seconds'] for d in days), min(firsts, default=None)

    def recent(self, since):
        """Records after `since` from today's file (tail only), for warm restarts."""
        path = self.directory / f'{self.day_of(time.time())}.jsonl'
        try:
            with open(path, 'rb') as source:
                source.seek(0, os.SEEK_END)
                offset = max(0, source.tell() - 4 * 2**20)
                source.seek(offset)
                lines = source.read().decode(errors='replace').splitlines()[1 if offset else 0:]
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if 'event' not in record and record.get('t1', 0) > since:
                records.append(record)
        return records


# --- Orchestration ---------------------------------------------------------------

def experiment_assignment(path, t0, t1):
    """The running experiment arm for a record, from drainhound experiment's state file.

    `washout` marks records that began before the arm settled (battery lag,
    governor and link retraining) or that straddle a block change.
    """
    try:
        state = json.loads(path.read_text()) if path else None
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get('status') != 'running':
        return None
    try:
        clean = state['block_start'] <= t0 and t0 >= state['washout_until'] and t1 <= state['block_end'] + 5
        return {'run': state['run'], 'name': state['name'], 'arm': state['arm'],
                'value': state.get('value'), 'block': state['block'], 'washout': not clean}
    except (KeyError, TypeError):
        return None


def boot_id(proc=Path('/proc')):
    return (read(proc / 'sys/kernel/random/boot_id') or '')[:8]


def rounded(name, value):
    if name in ('io_r', 'io_w', 'n'):
        return int(value)
    return round(value) if name == 'rss' else round(value, 4)


def subtract(after, before, scale=1.0):
    """Increase of cumulative counters, never negative (/proc/stat iowait can step
    back), times `scale`; integer counters stay integers."""
    result = {}
    for key, value in after.items():
        if key in before and isinstance(value, (int, float)) and isinstance(before[key], (int, float)):
            delta = max(0, value - before[key]) * scale
            result[key] = round(delta) if isinstance(value, int) and isinstance(before[key], int) else round(delta, 4)
    return result


def device_deltas(after, before, scale=1.0):
    """subtract() per device (interface, disk). A device new since `before` has no
    delta yet, and one whose counters went down was re-created: its readings now
    are the new baseline, and this interval counts nothing for it."""
    result = {}
    for name, values in after.items():
        old = before.get(name)
        if old is None or any(isinstance(v, (int, float)) and k in old and v < old[k] for k, v in values.items()):
            result[name] = {}
        else:
            result[name] = subtract(values, old, scale)
    return result


class Monitor:
    """Called every collector loop; writes a record every RECORD seconds.

    `clock` is the uptime (BOOTTIME) source: a record that sees it jump by more
    than RECORD while being taken (a suspend) is dropped, and the next update
    reports the gap. `asleep` (suspended()) says how much of a gap was a
    suspend rather than the collector stalled awake: the gap event's
    `suspended_s`, which report.sleep_drain uses.
    """

    def __init__(self, directory, sys=Path('/sys'), proc=Path('/proc'), experiment=None, db=None, throttle=None,
                 clock=uptime, asleep=suspended):
        self.sys, self.proc, self.experiment, self.throttle, self.clock = sys, proc, experiment, throttle, clock
        self.asleep, self.last_asleep = asleep, None
        self.log = PowerLog(directory)
        # The power database next to the archive (store.py). Read in any archive
        # day it has never seen (the first run, or after it was deleted) or holds
        # more records of than the database (a write failed).
        self.db = store.Writer(db or directory.parent / 'power.sqlite3')
        try:
            counts = {day: stats['records'] for day, stats in self.log.index['days'].items()}
            if days := store.import_logs(directory, self.db.db, counts=counts):
                print(f'drainhound: imported {", ".join(days)} into {self.db.db}', file=stderr)
        except Exception as error:  # Never a reason not to sample: the archive is still written.
            print(f'drainhound: {self.db.db}: {type(error).__name__}: {error}', file=stderr)
        self.models_path, self.models_stamp, self.params = directory / 'models.json', None, dict(PRIOR)
        self.refresh_models()
        self.fast, self.idle_states, self.commands = FastSampler(sys), IdleStates(sys), Commands()
        self.gpu, self.io = GpuClients(proc), Deltas()
        self.window = collections.deque()
        now = time.time()
        for record in self.log.recent(now - WINDOW):
            self.window.append((record['t1'], record['dt'], discharging(record),
                                attribute(record, self.params)))
        self.boot = boot_id(proc)
        self.last_battery = None
        self.voted = max((t for t, _ in qol.votes()), default=0.0)  # Votes up to here are already logged.
        self.write({'v': SCHEMA, 'event': 'start', 't1': now, 'boot': self.boot})
        self.reset(None, None)
        self.menu = summarize(self.window, now, self.log.totals())

    def reset(self, boot, wall):
        self.start_boot, self.start_wall, self.last_boot = boot, wall, boot
        self.cpu, self.cpu_seen = collections.defaultdict(float), None
        self.frequencies = []
        self.counters = None
        self.fast.reset()

    def snapshot_counters(self):
        """Cumulative counters, and the uptime they were read at (their rates' clock)."""
        return {'at': self.clock(), 'cpu': cpu_times(self.proc), 'idle': self.idle_states.read(),
                'irq': interrupts(self.proc), 'net': network(self.sys), 'disk': disks(self.sys)}

    def battery_state(self):
        """The record's battery fields: the gauge's percent, the charge in µ of the
        battery's unit ('uAh' or 'uWh'), volts. Several batteries: combined
        (batteries.read). None at all: nothing (a desktop's records have no battery)."""
        if not self.fast.battery or (reading := batteries.read(self.fast.supplies, self.fast.battery)) is None:
            return {}
        result = {'pct': reading['capacity']}
        if reading['now'] is not None:
            result.update(charge=round(reading['now'] * 1e6, 1), unit='u' + reading['unit'])
        if reading['volts'] is not None:
            result['volts'] = round(reading['volts'], 3)
        return result

    def update(self, boot, wall, processes):
        """`processes`: (pid, start ticks) → (label, cpu seconds, rss bytes, app),
        scanned just before `boot` and `wall` were read (together: clocks())."""
        self.fast.sample()
        asleep = self.asleep()
        if self.start_boot is None or boot - self.last_boot > RECORD:
            if self.start_boot is not None:
                gap = boot - self.last_boot
                # Of the gap, the time suspended (the rest the collector was stalled, awake); read
                # just after `boot`, so a suspend in between could only add to it: capped at the gap.
                slept = ({'suspended_s': round(min(gap, max(0.0, asleep - self.last_asleep)), 1)}
                         if self.last_asleep is not None else {})
                self.write({'v': SCHEMA, 'event': 'gap', 't0': self.start_wall, 't1': wall,
                                'gap_s': round(gap, 1), **slept, 'boot': self.boot,
                                'bat_before': self.last_battery, 'bat_after': self.battery_state()})
            self.reset(boot, wall)
            self.counters = self.snapshot_counters()
            self.gpu.sample(boot, processes)
            # Re-prime after a gap; the interval spanning the gap is discarded.
            self.io.sample(boot, process_io(self.proc, processes),
                           {key: entry[3] for key, entry in processes.items()})
        if self.cpu_seen is not None:
            for key, entry in processes.items():
                before = self.cpu_seen.get(key)
                if before is not None:
                    self.cpu[entry[3]] += max(0.0, entry[1] - before)
                elif key[1] / TICKS >= self.last_boot:
                    self.cpu[entry[3]] += entry[1]
        self.cpu_seen = {key: entry[1] for key, entry in processes.items()}
        since, self.last_boot, self.last_asleep = boot - self.last_boot, boot, asleep
        clock = cpu_frequency(self.sys, own_cpu(self.proc))
        if clock[0] is not None:
            self.frequencies.append((since, *clock))
        if boot - self.start_boot >= RECORD:
            record = self.record(boot, wall, processes)
            if record is None:
                return self.menu  # Suspended while recording: the next update reports the gap.
            fresh = [(t, v) for t, v in qol.votes() if t > self.voted]
            for t, choice in fresh:
                self.write({'v': SCHEMA, 'event': 'qol', 't1': t, 'boot': self.boot, 'vote': choice})
            self.voted = max([self.voted] + [t for t, _ in fresh])
            record['qol'] = -1 if any(v == 'down' for _, v in fresh) else 1 if fresh else 0
            self.write(record)
            self.refresh_models()
            self.window.append((record['t1'], record['dt'], discharging(record),
                                attribute(record, self.params)))
            while self.window and self.window[0][0] < wall - WINDOW:
                self.window.popleft()
            self.start_boot, self.start_wall = boot, wall
            self.cpu = collections.defaultdict(float)
            self.frequencies = []
            self.menu = summarize(self.window, wall, self.log.totals())
        return self.menu

    def record(self, boot, wall, processes):
        """The record ending at `boot`, or None if a suspend began since then."""
        dt = boot - self.start_boot
        power, status = self.fast.take()
        counters = self.snapshot_counters()
        before, self.counters = self.counters, counters
        # Counter deltas cover their own snapshots' interval: as rates, over dt.
        span = counters['at'] - before['at']
        scale = dt / span if span > 0 and abs(span - dt) < RECORD else 1.0
        apps_of = {key: entry[3] for key, entry in processes.items()}
        gpu = self.gpu.sample(boot, processes)
        io = self.io.sample(boot, process_io(self.proc, processes), apps_of)
        apps = collections.defaultdict(dict)
        for key, entry in processes.items():
            app = apps[entry[3]]
            app['n'] = app.get('n', 0) + 1
            app['rss'] = app.get('rss', 0) + entry[2] / 2**20
        for name, seconds in self.cpu.items():
            apps[name]['cpu'] = seconds
        for source in (gpu, io):
            for name, values in source.items():
                apps[name].update(values)
        apps = {name: {k: rounded(k, v) for k, v in values.items() if v and not (k == 'rss' and v < 1)}
                for name, values in apps.items()}
        cpu = subtract(counters['cpu'], before['cpu'], scale)
        cpu['busy_s'] = round(sum(cpu.get(k, 0) for k in ('user', 'sys', 'irq', 'steal')), 4)
        cpu.update(clock_means(self.frequencies))
        cpu['idle'] = idle_deltas(counters['idle'], before['idle'], scale)
        irq = subtract(counters['irq'], before['irq'], scale)
        net = {}
        for name, delta in device_deltas(counters['net'], before['net'], scale).items():
            state = counters['net'][name]['state']
            if any(delta.values()) or state == 'up':
                net[name] = dict(delta, state=state)
        disk = device_deltas(counters['disk'], before['disk'], scale)
        slow = self.commands.read(boot)
        shown = display(self.sys)
        if 'compositor' in slow:
            shown['compositor'] = slow['compositor']
        system = settings(self.sys)
        if self.throttle is not None:
            system['throttle'] = throttle_state(self.throttle)
        if 'wifi_ps' in slow:
            system['wifi_ps'] = slow['wifi_ps']
        state = self.battery_state()
        battery = dict(state, **power.get('bat', {}))
        record = {
            'v': SCHEMA, 't0': round(self.start_wall, 3), 't1': round(wall, 3), 'dt': round(dt, 3),
            'boot': self.boot, 'ac': mains_online(self.sys / 'class/power_supply'), 'status': status,
            'bat': battery, 'soc': power.get('soc', {}),
            # The chip's two measured rails (chip_rails): cores and graphics, and the SoC's.
            **{rail: power[rail] for rail in ('vdd', 'fabric') if rail in power},
            # Engine-seconds over apps, each app capped at the interval (GpuClients),
            # and the sum at dt: the share of the record the GPU was busy.
            'gpu': {'busy': power.get('gpu_busy', {}).get('w', 0),
                    **{k: round(min(dt, sum(a.get(k, 0) for a in apps.values())), 4) for k in ('gpu', 'video')}},
            'cpu': cpu,
            'irq': dict(sorted(((k, v) for k, v in irq.items() if v > 0), key=lambda r: -r[1])[:12]),
            'apps': apps, 'net': net, 'wifi_dbm': wifi_signal(self.proc), 'disk': disk,
            'usb': usb_active(self.sys), 'pci': pci_active(self.sys), 'audio': audio_streams(self.proc),
            'display': shown, 'settings': system, 'lid': lid_state(self.proc),
            'locked': any(entry[3] in (config.get('session', 'lockers') or ()) for entry in processes.values()),
            **thermal(self.sys),
        }
        for name, path in (config.get('log', 'extra') or {}).items():
            # Small JSON state files the config names, logged as display.<name>: a
            # display panel's {night, grayscale} as `panel` fills the store's
            # night_light and grayscale columns.
            path = Path(os.path.expandvars(os.path.expanduser(str(path))))
            try:
                if path.stat().st_size < 4096:
                    record['display'][name] = json.loads(path.read_text())
            except (OSError, ValueError):
                pass
        if (assignment := experiment_assignment(self.experiment, record['t0'], record['t1'])) is not None:
            record['exp'] = assignment
        if self.clock() - boot > RECORD:
            return None  # A suspend since `boot`: the readings straddle it; bat_before stays pre-suspend.
        self.last_battery = state
        return record

    def refresh_models(self):
        """Pick up drainhound eta's newly trained parameters when models.json changes."""
        try:
            stamp = self.models_path.stat().st_mtime
        except OSError:
            return
        if stamp != self.models_stamp:
            self.models_stamp = stamp
            self.params = load_attribution(self.models_path) or self.params

    def write(self, record):
        self.log.write(record)
        self.db.write(record)

    def maintain(self):
        """Deferred database work, one piece per collector loop once sampling runs:
        re-read archive days a failed write missed (flushed first), then recompute a
        day of minutes a migration left stale. Returns whether work remains."""
        if self.db.missed and self.db.conn is not None:
            self.log.flush()
        return self.db.catch_up(self.log.directory)

    def close(self):
        self.log.close()
        self.db.close()

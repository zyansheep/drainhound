"""The system's batteries, found and read in one place and combined into one.

Every power supply of type Battery that powers the system counts: present, and
not a device's (scope Device: a mouse's or a headset's battery). A laptop with
two (many ThinkPads: an internal and a removable one) is one battery here:
their charge, capacity and power summed, their status combined. Standard
library only (the collector runs on plain Python).

Batteries report one of two ways (the kernel's power_supply ABI):
charge_now/charge_full in µAh with current_now in µA, as a Framework 13's
does, or energy_now/energy_full in µWh with power_now in µW, as most Intel
laptops (ThinkPads, Dells) do; some gauges have both, or half of each (unit_of
picks). A reading keeps the battery's own unit: `unit`
is 'Ah' or 'Wh' and `now`, `full` and `design` are in it. A charge becomes
energy only where it has to, through the pack's voltage (nominal_volts).
"""
import math
from pathlib import Path

SUPPLIES = Path('/sys/class/power_supply')
# Volts per Ah when nothing says better: a 3-cell Li-ion pack's nominal voltage
# (3 × 3.85 V), the commonest laptop pack. 4-cell packs (a Framework 13's) are
# 15.4 V, 2-cell ones 7.7 V; a battery's voltage_min_design says which.
DEFAULT_VOLTS = 11.55
FILES = {'Wh': ('energy_now', 'energy_full', 'energy_full_design'),
         'Ah': ('charge_now', 'charge_full', 'charge_full_design')}
USB = ('USB', 'USB_C', 'USB_PD', 'USB_PD_DRP')  # Older kernels named each USB charger kind a type.


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def number(path):
    try:
        value = float(read_text(path))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def entries(root):
    try:
        return sorted(Path(root).iterdir())
    except OSError:
        return []


def is_battery(path):
    return (read_text(path / 'type') == 'Battery' and read_text(path / 'scope') != 'Device'
            and number(path / 'present') != 0)


def find(root=SUPPLIES):
    """The system's batteries (power_supply directories) in name order, () with none."""
    return tuple(path for path in entries(root) if is_battery(path))


def mains_online(root=SUPPLIES, found=None):
    """Whether a charger is online: any Mains supply's `online`. With no Mains
    supply (laptops that charge only over USB-C), the system's USB supplies' (a
    UCSI port is online while it feeds the laptop). None with neither."""
    found = entries(root) if found is None else found
    for kinds in (('Mains',), USB):
        online = [number(path / 'online') for path in found if read_text(path / 'type') in kinds
                  and (kinds != USB or read_text(path / 'scope') != 'Device')]
        if online:
            return any(value == 1 for value in online)
    return None


def supply_watts(path):
    """(status, signed W, W) of one battery: power_now, or current_now ×
    voltage_now. Signed is positive while discharging, negative while charging
    and 0 in any other status (the power log's convention); (status, None,
    None) if it can't be read."""
    status = read_text(path / 'status') or 'Unknown'
    power = number(path / 'power_now')
    if power is None:
        current, voltage = number(path / 'current_now'), number(path / 'voltage_now')
        if current is None or voltage is None:
            return status, None, None
        power = current * voltage / 1e6  # µA × µV → µW
    watts = abs(power) / 1e6  # Some drivers sign it, most don't: the status says which way it flows.
    return status, watts if status == 'Discharging' else -watts if status == 'Charging' else 0.0, watts


def combined_status(statuses, watts=None):
    """One status for several batteries: a single one's as it is; else Discharging
    if any discharges, Charging if any charges (both: the way the net `watts` flow),
    Full if all are, Not charging otherwise. Unknown ones (an idle second battery)
    count only when no other says anything."""
    if len(statuses) == 1:
        return statuses[0]
    known = [s for s in statuses if s and s != 'Unknown']
    if not known:
        return 'Unknown'
    if 'Discharging' in known and 'Charging' in known:
        return 'Charging' if (watts or 0.0) < 0 else 'Discharging'
    for status in ('Discharging', 'Charging'):
        if status in known:
            return status
    return 'Full' if all(s == 'Full' for s in known) else 'Not charging'


def watts(paths):
    """(combined status, signed W) of these batteries, read cheaply (the
    collector's sub-second samples): None for the watts when none could be read
    or one has gone (its directory: look them up again)."""
    statuses, total, read = [], 0.0, False
    for path in paths:
        status, signed, _ = supply_watts(path)
        statuses.append(status)
        if signed is None:
            if not path.exists():
                return combined_status(statuses), None
            continue
        total, read = total + signed, True
    return combined_status(statuses, total), total if read else None


def unit_of(path):
    """The unit a battery is read in: the first whose now and full charge both
    read (a full of 0 doesn't), Wh before Ah; else the first with a now (a
    charge with no full still counts down), else the first with a full.

    Some gauges mix them: a bq27xxx (phones, tablets, some ARM laptops) has
    energy_now but no energy_full, beside a complete charge_now/charge_full;
    read in Wh, it had no full charge (no forecast, no cutoff)."""
    readings = {unit: (number(path / now), number(path / full)) for unit, (now, full, _) in FILES.items()}
    complete = [unit for unit, (now, full) in readings.items() if now is not None and full is not None and full > 0]
    counting = [unit for unit, (now, _) in readings.items() if now is not None]
    sized = [unit for unit, (_, full) in readings.items() if full is not None]
    return next(iter(complete + counting + sized), 'Ah')


def supply(path):
    """One battery's readings, in its own unit (unit_of)."""
    status, signed, power = supply_watts(path)
    unit = unit_of(path)
    now, full, design = (number(path / name) for name in FILES[unit])
    volts, nominal = number(path / 'voltage_now'), number(path / 'voltage_min_design')
    return {'name': path.name, 'status': status, 'watts': signed, 'power': power, 'unit': unit,
            'now': now / 1e6 if now is not None else None, 'full': full / 1e6 if full is not None else None,
            'design': design / 1e6 if design is not None else None, 'capacity': number(path / 'capacity'),
            'volts': volts / 1e6 if volts is not None else None,
            'nominal': nominal / 1e6 if nominal else None,
            'limit': number(path / 'charge_control_end_threshold'), 'cycles': number(path / 'cycle_count')}


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def read(root=SUPPLIES, paths=None):
    """The batteries combined, or None without one: {'status', 'watts' (signed
    W, as supply_watts), 'power' (W, unsigned), 'unit' ('Ah' or 'Wh'), 'now',
    'full', 'design' (in it; None if any battery can't say), 'capacity' (the
    gauges' percent), 'volts' (voltage_now), 'nominal' (voltage_min_design),
    'limit' (charge_control_end_threshold), 'cycles', 'count', 'names', 'mains'}.
    Readings a battery doesn't have are None. `paths` are the batteries (found
    under `root` by default); `root` also gives the chargers.

    Several batteries: charges and power add up; mixed units add up in Wh (a
    charge in Ah at its nominal voltage). Capacity and charge limit are means
    weighted by full charge (where each battery stops is where the whole does),
    volts and nominal plain means, cycles the most worn battery's."""
    found = entries(root)
    paths = tuple(p for p in found if is_battery(p)) if paths is None else tuple(paths)
    if not paths:
        return None
    parts = [supply(path) for path in paths]
    signed = [p['watts'] for p in parts if p['watts'] is not None]
    power = [p['power'] for p in parts if p['power'] is not None]
    reading = {'status': combined_status([p['status'] for p in parts], sum(signed)),
               'watts': sum(signed) if signed else None, 'power': sum(power) if power else None,
               'count': len(parts), 'names': [p['name'] for p in parts], 'mains': mains_online(root, found)}
    if len(parts) == 1:
        return {**reading, **{k: parts[0][k] for k in ('unit', 'now', 'full', 'design', 'capacity', 'volts',
                                                       'nominal', 'limit', 'cycles')}}
    unit = parts[0]['unit'] if len({p['unit'] for p in parts}) == 1 else 'Wh'

    def amount(part, key):
        value = part[key]
        if value is None or part['unit'] == unit:
            return value
        return value * nominal_volts(part, part['volts'])  # Ah → Wh

    def total(key):
        values = [amount(p, key) for p in parts]
        return None if None in values else sum(values)
    fulls = [amount(p, 'full') for p in parts]
    weighted = None not in fulls and sum(fulls) > 0
    capacities, limits = [p['capacity'] for p in parts], [p['limit'] for p in parts]
    if weighted and None not in capacities:
        capacity = sum(c * f for c, f in zip(capacities, fulls)) / sum(fulls)
    else:
        capacity = mean(capacities)
    limit = None
    if any(v is not None for v in limits):  # A battery without a limit charges to 100%.
        limits = [100.0 if v is None else v for v in limits]
        limit = sum(v * f for v, f in zip(limits, fulls)) / sum(fulls) if weighted else mean(limits)
    return {**reading, 'unit': unit, 'now': total('now'), 'full': total('full'), 'design': total('design'),
            'capacity': capacity, 'volts': mean(p['volts'] for p in parts),
            'nominal': mean(p['nominal'] for p in parts), 'limit': limit,
            'cycles': max((p['cycles'] for p in parts if p['cycles'] is not None), default=None)}


def nominal_volts(reading=None, logged=None):
    """Volts per Ah where no voltage curve says better: the pack's
    voltage_min_design, else `logged` (the power log's median loaded voltage),
    else DEFAULT_VOLTS."""
    return (reading or {}).get('nominal') or logged or DEFAULT_VOLTS


def energy_wh(reading):
    """Wh in the battery now: energy_now as it is, a charge at the voltage it
    reads now (None when either is unknown)."""
    if reading is None or reading.get('now') is None:
        return None
    if reading['unit'] == 'Wh':
        return reading['now']
    return reading['now'] * reading['volts'] if reading.get('volts') else None

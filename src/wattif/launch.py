"""The light commands: toggling the resident panel and Waybar's modules.

Neither imports numpy or GTK. The panel is told over D-Bus (GTK's
org.gtk.Actions), started first through its systemd unit, or else directly.
"""
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config

TABS = ('timeline', 'experiments')
STALE = 15  # s: eta.json older than this means wattif eta isn't running.
SNAPSHOT_STALE = 30  # s: the collector's snapshot.json (written every 2 s) older than this is left out.


def panel(argv):
    """`wattif panel [TAB]` toggles the panel (or shows TAB); `serve` runs it
    (the unit's ExecStart); `render DIR [6 h|Day|Week]` draws its charts to PNGs."""
    if argv[:1] in (['serve'], ['render']):
        if argv[:1] == ['serve']:
            preload(['panel', *argv])
        from . import panel as resident
        return resident.main(argv)
    if len(argv) > 1 or (argv and argv[0] not in TABS):
        print(f"usage: wattif panel [{'|'.join(TABS)}] | serve | render DIR [6 h|Day|Week]", file=sys.stderr)
        return 2
    action, parameter = ('show', [argv[0]]) if argv else ('toggle', [])
    if not running() and not start():
        # No unit, no running panel: run it here, opening as asked.
        os.execv(config.self_command()[0], [*config.self_command(), 'panel', 'serve', '--open', *argv])
    for _ in range(50):  # A just-started panel takes a moment to export its actions.
        if activate(action, parameter):
            return 0
        time.sleep(0.1)
    print('wattif: the panel did not answer on D-Bus', file=sys.stderr)
    return 1


def layer_shell_library():
    """gtk4-layer-shell's library: WATTIF_LAYER_SHELL_LIB (packages set it), else
    wherever the system keeps it."""
    import ctypes.util
    return os.environ.get('WATTIF_LAYER_SHELL_LIB') or ctypes.util.find_library('gtk4-layer-shell')


def preload(argv):
    """Run again with gtk4-layer-shell preloaded, unless it is: it must load before
    libwayland-client, so before GTK, which a running Python can't arrange (the
    library's own documentation says to LD_PRELOAD it). WATTIF_PRELOADED keeps
    the LD_PRELOAD from before, for the panel to give its children."""
    library = layer_shell_library()
    loaded = os.environ.get('LD_PRELOAD', '')
    if not library or 'WATTIF_PRELOADED' in os.environ or library in loaded.replace(' ', ':').split(':'):
        return
    os.environ['WATTIF_PRELOADED'] = loaded
    os.environ['LD_PRELOAD'] = ':'.join(filter(None, (library, loaded)))
    command = config.self_command()
    os.execv(command[0], [*command, *argv])


def bus():
    from gi.repository import Gio
    return Gio.bus_get_sync(Gio.BusType.SESSION)


def running():
    from gi.repository import GLib
    try:
        reply = bus().call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                                'NameHasOwner', GLib.Variant('(s)', (config.APP_ID,)), None, 0, 1000, None)
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False


def start():
    """Start the panel's unit (Type=dbus: it returns once the panel owns its name)."""
    if shutil.which('systemctl') is None:
        return False
    done = subprocess.run(['systemctl', '--user', 'start', config.UNITS['panel']], capture_output=True)
    return done.returncode == 0


def activate(action, parameter):
    from gi.repository import GLib
    try:
        bus().call_sync(config.APP_ID, config.APP_PATH, 'org.gtk.Actions', 'Activate',
                        GLib.Variant('(sava{sv})', (action, [GLib.Variant('s', p) for p in parameter], {})),
                        None, 0, 2000, None)
        return True
    except GLib.Error:
        return False


def waybar(argv):
    """Waybar custom modules: `battery` follows wattif eta's label (eta.json;
    run `wattif eta` as its own service), `up`/`down` are the vote buttons."""
    if argv in (['up'], ['down']):
        from . import qol
        print(json.dumps(qol.waybar(argv[0])), flush=True)
        return 0
    if argv != ['battery']:
        print('usage: wattif waybar battery|up|down', file=sys.stderr)
        return 2
    last = None
    while True:
        line = json.dumps(battery_label())
        if line != last:
            print(line, flush=True)
            last = line
        time.sleep(2)


def battery_label(path=None, now=None, snapshot=None):
    """The label wattif eta last published, or '--' while it is stale. With
    [waybar] tooltip, a label without a tooltip of its own gets one (tooltip()),
    for bars that show nothing else on hover."""
    now = time.time() if now is None else now
    wanted = config.get('waybar', 'tooltip')
    try:
        detail = json.loads((path or config.RUNTIME / 'eta.json').read_text())
        if now - float(detail['updated']) < STALE and isinstance(detail.get('waybar'), dict):
            label = detail['waybar']
            if wanted and 'tooltip' not in label:
                label = dict(label, tooltip=tooltip(detail, read_snapshot(snapshot, now)))
            return label
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    stale = {'text': '--', 'class': ['unknown']}
    return dict(stale, tooltip="No estimate: wattif eta isn't running") if wanted else stale


def read_snapshot(path=None, now=None):
    """The battery-use block of the collector's snapshot.json ({'title', 'rows',
    'footer'}) while it is fresh, else None."""
    try:
        snapshot = json.loads(Path(path or config.RUNTIME / 'snapshot.json').read_text())
        fresh = (now or time.time()) - float(snapshot['timestamp']) < SNAPSHOT_STALE
        if fresh and isinstance(snapshot['battery'], dict):
            return snapshot['battery']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def finite(*values):
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


def hours_text(hours):
    """'2 h 05 m', '45 min', and whole hours from ten up ('23 h'): the panel's durations."""
    if not finite(hours):
        return '--'
    if hours >= 10:
        return f'{round(hours)} h'
    minutes = round(max(0.0, hours) * 60)
    return f'{minutes // 60} h {minutes % 60:02d} m' if minutes >= 60 else f'{minutes} min'


def span(low, high):
    return f' (80%: {hours_text(low)}–{hours_text(high)})' if finite(low, high) else ''


def escape(text):
    """Waybar reads a tooltip as Pango markup."""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def tooltip(detail, battery=None):
    """Time left (or to full) with its 80% interval, how long a suspend from now
    would last, and what uses the battery (the collector's block), as Pango
    markup. From eta.json's numbers, without numpy: this runs every 2 s."""
    classes = detail.get('classes') if isinstance(detail.get('classes'), list) else []
    lines = []
    pct, watts = detail.get('pct'), detail.get('watts')
    head = ' · '.join(filter(None, [f'{pct:.0f}%' if finite(pct) else '',
                                    f'{abs(watts):.1f} W' if finite(watts) else '']))
    hours, low, high = detail.get('hours'), detail.get('low'), detail.get('high')
    if 'discharging' in classes:
        if finite(detail.get('cutout')):
            end = detail['poweroff'] if finite(detail.get('poweroff')) else detail['cutout']
            pm = f" ±{detail['cutout_pm']:.0f} s" if finite(detail.get('cutout_pm')) else ''
            lines.append(f"{'Powers off' if finite(detail.get('poweroff')) else 'Cuts out'} at "
                         f"{time.strftime('%H:%M:%S', time.localtime(end))}{pm}")
        elif finite(hours):
            lines.append(f'{hours_text(hours)} left{span(low, high)}')
        if detail.get('charger'):
            lines.append(f"Charger {detail['charger']}")
    elif 'charging' in classes:
        target = detail.get('target')
        if finite(hours):
            lines.append(f"{hours_text(hours)} to {f'{target:.0f}%' if finite(target) else 'full'}{span(low, high)}")
        if finite(detail.get('banked')):
            lines.append(f"Unplugged now: ~{hours_text(detail['banked'])} on battery")
    elif 'plugged' in classes:
        lines.append('On AC')
    asleep = detail.get('sleep')
    if isinstance(asleep, dict) and finite(asleep.get('hours')):
        lines.append(f"Asleep from now: ~{hours_text(asleep['hours'])}{span(asleep.get('low'), asleep.get('high'))}")
    text = escape('\n'.join(filter(None, [head, *lines])))
    listed = []  # (name, value) of each row of the block, then its parts under it.
    for row in (battery or {}).get('rows') or []:
        if isinstance(row, dict):
            listed.append((str(row.get('name', '')), str(row.get('value', ''))))
            listed += [('  ' + str(part.get('name', '')), str(part.get('value', '')))
                       for part in row.get('parts') or [] if isinstance(part, dict)]
    if battery and (battery.get('title') or listed):
        text += ('\n\n' if text else '') + escape(battery.get('title') or '')
        if listed:  # Monospaced, so the values line up.
            width = max(len(name) for name, _ in listed)
            text += '\n<tt>' + escape('\n'.join(f'{name:<{width}}  {value}' for name, value in listed)) + '</tt>'
        if battery.get('footer'):
            text += '\n' + escape(battery['footer'])
    return text

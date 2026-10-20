"""The light commands: toggling the resident panel and Waybar's modules.

Neither imports numpy or GTK. The panel is told over D-Bus (GTK's
org.gtk.Actions), started first through its systemd unit, or else directly.
"""
import json
import os
import shutil
import subprocess
import sys
import time

from . import config

TABS = ('timeline', 'experiments')
STALE = 15  # s: eta.json older than this means wattif eta isn't running.


def panel(argv):
    """`wattif panel [TAB]` toggles the panel (or shows TAB); `serve` runs it
    (the unit's ExecStart); `render DIR [6 h|Day|Week]` draws its charts to PNGs."""
    if argv[:1] in (['serve'], ['render']):
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


def battery_label(path=None, now=None):
    """The label wattif eta last published, or '--' while it is stale."""
    now = time.time() if now is None else now
    try:
        detail = json.loads((path or config.RUNTIME / 'eta.json').read_text())
        if now - float(detail['updated']) < STALE and isinstance(detail.get('waybar'), dict):
            return detail['waybar']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {'text': '--', 'class': ['unknown']}

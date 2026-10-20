"""Where wattif keeps its files, and the user's configuration.

Paths follow the XDG base directories; each can be moved with an environment
variable (WATTIF_STATE_DIR, WATTIF_RUNTIME_DIR, WATTIF_CONFIG). Settings come
from DEFAULTS, then /etc/wattif/config.toml, then
$XDG_CONFIG_HOME/wattif/config.toml (tables merge key by key; a later file
wins). WATTIF_CONFIG names the one file to read instead (empty: none).
"""
import copy
import os
import shlex
import shutil
import sys
import tomllib
from pathlib import Path

NAME = 'wattif'
APP_ID = 'io.github.zyansheep.Wattif'  # The panel's application id and D-Bus name.
APP_PATH = '/' + APP_ID.replace('.', '/')


def _base(variable, fallback):
    value = os.environ.get(variable)
    return Path(value) if value else fallback


HOME = Path(os.environ.get('HOME') or '/nonexistent')
STATE = _base('WATTIF_STATE_DIR', _base('XDG_STATE_HOME', HOME / '.local/state') / NAME)
RUNTIME = _base('WATTIF_RUNTIME_DIR', _base('XDG_RUNTIME_DIR', Path(f'/tmp/{NAME}-{os.getuid()}')) / NAME)
LOG = STATE / 'power'  # The raw JSONL archive, models.json and index.json.
DB = STATE / 'power.sqlite3'
SYSTEM_CONFIG = Path('/etc') / NAME / 'config.toml'
USER_CONFIG = _base('XDG_CONFIG_HOME', HOME / '.config') / NAME / 'config.toml'

# systemd user units (see units.py), by role.
UNITS = {
    'collector': f'{NAME}-collector.service',
    'eta': f'{NAME}-eta.service',
    'panel': f'{NAME}-panel.service',
    'experiment': f'{NAME}-experiment.service',
    'experiments': f'{NAME}-experiments.service',
    'reconcile': f'{NAME}-reconcile.service',
    'throttle': f'{NAME}-throttle.service',
    'freeze': f'{NAME}-freeze.service',
}

DEFAULTS = {
    # How power experiments run the root helper (see helper/wattif-helper), as a
    # list of words. Its sudo rule must allow exactly this command without a
    # password; without one, the root-only settings are left out.
    'helper': {'command': ['sudo', '-n', 'wattif-helper']},
    'apps': {
        # Executable (or Electron app directory) → the name logged and shown;
        # added to the built-in table (collector.APP_NAMES).
        'names': {},
        # Timeline groups: name → executable or app-name prefixes (matched
        # ignoring case), in the order the chart stacks and colours them.
        # Replaces the built-in groups (report.GROUPS) when given; apps in none
        # are "Other apps". The group that matches 'kernel' is the desktop's own:
        # never suggested for closing.
        'groups': None,
        # Where contrib/firefox-bridge.js writes which site each Firefox-family
        # content process shows (empty: $XDG_RUNTIME_DIR/firefox-processes).
        'firefox_processes': '',
    },
    # Processes whose presence means the screen is locked (also logind's LockedHint).
    'session': {'lockers': ['swaylock', 'hyprlock', 'gtklock', 'waylock', 'i3lock', 'physlock']},
    'throttle': {
        'quota': 10,  # % of one core per background app.
        'grace': 120,  # s an app stays free after it loses focus.
        # Never throttled: scope-name patterns, added to the terminals and editors (throttle.KEEP).
        'keep': [],
    },
    'freeze': {
        'apps': [],  # Scope-name patterns of apps to freeze when long unused (empty: none).
        'after': 30,  # min without focus.
        'every': 10,  # min between catch-up thaws.
        'thaw': 10,  # s each thaw lasts.
    },
    'battery': {
        # Power off cleanly just before the battery's learned cutoff (once a
        # battery death has taught it). Off by default: most desktops already
        # act on low battery through UPower.
        'poweroff': False,
        'notify': True,  # Desktop notifications as the cutoff nears.
    },
    'chip': {
        # Where the chip's (package) power comes from: 'auto' takes amdgpu's PPT on
        # an AMD APU, else RAPL's package energy counter if you may read it (root
        # only unless you allow it: see power.Rapl), else none; or 'amdgpu' (the
        # first amdgpu sensor, whatever GPU it is), 'rapl', 'none'.
        'sensor': 'auto',
    },
    # What a setting goes back to when experiments end, on each power source.
    # None, or the string "found" (TOML has no null): as wattif found it in this
    # boot before it first changed it, on that power source (where wattif only
    # changed it on battery, on AC it is left to whatever your system sets when
    # plugged in). A value instead is put back whatever was found; the original
    # author's udev rules, say: profile = {battery = "power-saver", ac =
    # "balanced"}, abm = {battery = 3, ac = 0}.
    'stock': {
        # power-profiles-daemon's profile. As found; on battery, until seen there,
        # power-saver (what common udev rules set).
        'profile': {'battery': None, 'ac': None},
        'abm': {'battery': None, 'ac': None},  # amdgpu panel power savings 0-4.
        # CPU boost 0/1. As found: as the profile leaves it where power-profiles-daemon
        # sets it with the profile (on amd-pstate: off under power-saver), else as
        # found (on until seen).
        'boost': {'battery': None, 'ac': None},
        # Wi-Fi power saving 'on'/'off'. As found (drivers and NetworkManager differ;
        # on until seen).
        'wifi_ps': {'battery': None, 'ac': None},
        # The CPU frequency cap's power-saving value in MHz (not a stock value: the
        # cap goes back to what was found, by default none).
        'cpu_cap_mhz': 2000,
    },
    'experiments': {
        # Settings that can upset some hardware (link power management, the GPU's
        # clocks): never in automatic runs unless added with --risky (a list; one
        # name alone may be a string).
        'risky': ['aspm', 'apst', 'gpu-dpm'],
        'freeze_keep': [],  # Scope patterns "Close every app" leaves running.
        # Boot entries for boot-level experiments: arm → systemd-boot entry
        # pattern (NixOS specialisations match "specialisation-<arm>").
        'boot_arms': {},
    },
    'panel': {'margin': 32},  # px between the screen's top edge and the panel (your bar's height).
    'waybar': {
        'signal': 12,  # The vote modules refresh on SIGRTMIN+signal.
        # 'fontawesome' (Font Awesome's icons, in the bar and the panel) or 'text'
        # (plain Unicode: ⚡ 🔌 ⏳ 👍 👎, for bars without Font Awesome).
        'glyphs': 'fontawesome',
        # `wattif waybar battery`: a tooltip with the time left, sleep and battery
        # use (turn it off if your bar shows its own panel on hover).
        'tooltip': True,
    },
    'log': {
        # Small JSON state files logged with every record, as display.<name>
        # (name → path; ~ and $VARS expand). A file with {"night": …,
        # "grayscale": …} logged as `panel` fills the night_light and grayscale columns.
        'extra': {},
    },
}


def _merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def files():
    """The configuration files read, in order."""
    override = os.environ.get('WATTIF_CONFIG')
    if override is not None:
        return [Path(override)] if override else []
    return [SYSTEM_CONFIG, USER_CONFIG]


def load(paths=None):
    settings = copy.deepcopy(DEFAULTS)
    for path in files() if paths is None else paths:
        try:
            with open(path, 'rb') as source:
                _merge(settings, tomllib.load(source))
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as error:
            print(f'{NAME}: {path}: {error}; ignoring it', file=sys.stderr)
    return settings


SETTINGS = load()


def get(section, key=None):
    table = SETTINGS.get(section, {})
    return table if key is None else table.get(key)


def self_command():
    """The command that runs this wattif: its entry point, or `python -m wattif`
    when run that way (children inherit it through WATTIF_SELF)."""
    if found := os.environ.get('WATTIF_SELF'):
        return shlex.split(found)
    if found := shutil.which(NAME):
        return [found]
    return [sys.executable, '-m', NAME]

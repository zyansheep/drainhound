"""Where wattif keeps its files, and the user's configuration.

Paths follow the XDG base directories; each can be moved with an environment
variable (WATTIF_STATE_DIR, WATTIF_RUNTIME_DIR, WATTIF_CONFIG). Settings come
from DEFAULTS, then /etc/wattif/config.toml, then
$XDG_CONFIG_HOME/wattif/config.toml (tables merge key by key; a later file
wins). WATTIF_CONFIG names the one file to read instead (empty: none).
"""
import copy
import os
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
    # How power experiments run the root helper (see helper/wattif-helper). Its
    # sudo rule must allow exactly this command without a password.
    'helper': {'command': ['sudo', '-n', 'wattif-helper']},
    'apps': {
        # Executable (or Electron app directory) → the name logged and shown;
        # added to the built-in table (collector.APP_NAMES).
        'names': {},
        # Timeline groups: name → executable or app-name prefixes (lower case).
        # Replaces the built-in groups (report.GROUPS) when given.
        'groups': None,
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
    # What a setting goes back to when experiments end, where it isn't simply
    # the boot-time value. These copy common udev rules; set them to yours. A
    # None leaves the setting as found on that power source (on AC: to whatever
    # your system sets when plugged in).
    'stock': {
        # power-profiles-daemon's profile, until wattif has seen yours (it keeps
        # the one you use on each power source).
        'profile': {'battery': 'power-saver', 'ac': 'balanced'},
        'abm': {'battery': None, 'ac': None},  # amdgpu panel power savings 0-4 (None: leave as found).
        # CPU boost 0/1. None: as the profile leaves it (power-profiles-daemon on
        # amd-pstate turns it off under power-saver), else on.
        'boost': {'battery': None, 'ac': None},
        'cpu_cap_mhz': 2000,  # The CPU frequency cap's power-saving value.
    },
    'experiments': {
        # Settings that can upset some hardware (link power management, the GPU's
        # clocks): never in automatic runs unless added with --risky.
        'risky': ['aspm', 'apst', 'gpu-dpm'],
        'freeze_keep': [],  # Scope patterns "Close every app" leaves running.
        # Boot entries for boot-level experiments: arm → systemd-boot entry
        # pattern (NixOS specialisations match "specialisation-<arm>").
        'boot_arms': {},
    },
    'panel': {'margin': 32},  # px between the screen's top edge and the panel (your bar's height).
    'waybar': {
        'signal': 12,  # The vote modules refresh on SIGRTMIN+signal.
        'glyphs': 'fontawesome',  # 'fontawesome' or 'text'.
    },
    'log': {
        # Small JSON state files logged with every record, as display.<name>.
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
        return found.split('\0')
    if found := shutil.which(NAME):
        return [found]
    return [sys.executable, '-m', NAME]

"""drainhound's command line: one entry point, one subcommand per part.

Each part is imported only when its command runs, so `drainhound vote up` or the
Waybar reader start in a few milliseconds without loading numpy or GTK.
"""
import importlib
import json
import os
import shlex
import sys

from . import __version__, config

USAGE = f"""usage: drainhound <command> [args]

Services (normally started by systemd; see `drainhound units`):
  collect [import]       sample power every 2 s and log a record every 10 s
  eta [--quiet]          time left, model training, low-battery warnings
  throttle               cap unfocused apps' CPU on battery
  freeze [APP ...]       freeze long-unused apps on battery (Android's cached apps)

Everyday:
  panel [TAB]            toggle the battery panel (TAB: timeline, experiments)
  vote up|down|resume    tell experiments what stands out (👍 especially good, 👎 worse)
  experiment ...         power experiments: enable, disable, status, start, apply, ...
  waybar battery|up|down Waybar custom-module output

Setup and checks:
  units [--install]      print (or install) the systemd user units
  check                  gather every panel period from your database, timed
  config                 the effective configuration
  version

drainhound {__version__}. Files: {config.STATE} (data), {config.RUNTIME} (live state),
{config.USER_CONFIG} (settings)."""


def module(name):
    return importlib.import_module(f'.{name}', __package__)


def remember_self():
    """Children (the panel's buttons, the experiment runner's units) run this same
    drainhound: the entry point that started it, or `python -m drainhound`."""
    if os.environ.get('DRAINHOUND_SELF'):
        return
    first = sys.argv[0]
    if first.endswith('__main__.py'):
        os.environ['DRAINHOUND_SELF'] = shlex.join([sys.executable, '-m', config.NAME])
    elif os.path.isfile(first) and os.access(first, os.X_OK):
        os.environ['DRAINHOUND_SELF'] = os.path.abspath(first)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    remember_self()
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE)
        return 0
    command, rest = argv[0], argv[1:]
    if command == 'collect':
        return module('collector').main(rest)
    if command == 'eta':
        return module('eta').main(rest)
    if command in ('experiment', 'exp'):
        return module('experiment').main(rest)
    if command == 'vote':
        return module('qol').main(rest)
    if command == 'throttle':
        return module('throttle').main(rest)
    if command == 'freeze':
        return module('freeze').main(rest)
    if command == 'panel':
        return module('launch').panel(rest)
    if command == 'waybar':
        return module('launch').waybar(rest)
    if command == 'units':
        return module('units').main(rest)
    if command == 'check':
        return module('panel_data').check()
    if command == 'config':
        print(json.dumps({'files': [str(p) for p in config.files()], **config.SETTINGS}, indent=2))
        return 0
    if command in ('version', '--version'):
        print(f'drainhound {__version__}')
        return 0
    print(f'drainhound: unknown command {command!r}\n\n{USAGE}', file=sys.stderr)
    return 2

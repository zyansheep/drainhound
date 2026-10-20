"""drainhound's systemd user units, written from one table.

    drainhound units                    print them
    drainhound units --install          write them to ~/.config/systemd/user, reload,
                                    and enable the default set (--no-enable: don't)
    drainhound units --dir DIR [--exec PATH]   write them to DIR only (packaging)

Packages install the same files (the Nix package runs `--dir` at build time).
The collector has no ProtectSystem/ProtectHome/PrivateTmp on purpose: in a user
manager they imply a private user namespace, which hides the session's
/proc/PID/{fd,fdinfo,io,exe} (per-app GPU time and disk IO).
"""
import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import config

SESSION = 'graphical-session.target'
DEFAULT_ENABLED = ('collector', 'eta', 'panel', 'experiments', 'reconcile.timer', 'freeze')


def state_dir():
    """config.STATE as the units see it: %S/drainhound (the user manager's state
    directory, XDG_STATE_HOME's) unless DRAINHOUND_STATE_DIR moved it."""
    return str(config.STATE) if os.environ.get('DRAINHOUND_STATE_DIR') else f'%S/{config.NAME}'


def units(drainhound):
    """{file name: contents} for the drainhound executable `drainhound`."""
    u = config.UNITS
    session = {'PartOf': SESSION, 'After': SESSION}
    table = {
        u['collector']: (
            {'Description': 'drainhound: sample and log power use', **session},
            {'ExecStart': f'{drainhound} collect', 'Restart': 'on-failure',
             # Slow enough that a crash loop never trips the start limit and stops logging.
             'RestartSec': '5', 'NoNewPrivileges': 'true'},
            True),
        u['eta']: (
            {'Description': 'drainhound: time left, model training, low-battery warnings',
             **session, 'After': f"{SESSION} {u['collector']}"},
            {'ExecStart': f'{drainhound} eta --quiet', 'Restart': 'on-failure', 'RestartSec': '5'},
            True),
        u['panel']: (
            {'Description': 'drainhound: resident battery panel', **session, 'Requisite': SESSION},
            {'Type': 'dbus', 'BusName': config.APP_ID, 'ExecStart': f'{drainhound} panel serve',
             'Restart': 'on-failure'},
            True),
        # A manual run (`drainhound experiment start`). Root settings go through
        # `sudo -n drainhound-helper`, so no NoNewPrivileges. Stopping restores the
        # stock values; what could not be is retried by ExecStopPost and the
        # reconciler. TimeoutStopSec covers a full restore (~9 helper calls).
        u['experiment']: (
            {'Description': 'drainhound: a power experiment', 'PartOf': SESSION},
            {'ExecStart': f'{drainhound} experiment run',
             'ExecStopPost': [f'{drainhound} experiment restore',
                              # A manual run stops automatic experiments (Conflicts=); bring them back.
                              f'{drainhound} experiment resume'],
             'Restart': 'no', 'TimeoutStopSec': '180'},
            False),
        # Automatic experiments while `drainhound experiment enable` left its flag
        # (an incident the watchdog sees removes it). Ordered after the manual
        # unit: when one starts and the other stops, the stop's restore finishes first.
        u['experiments']: (
            {'Description': 'drainhound: automatic power experiments', **session,
             'After': f"{SESSION} {u['collector']} {u['experiment']}", 'Conflicts': u['experiment'],
             'ConditionPathExists': f'{state_dir()}/experiments-enabled',
             # A runner that keeps crashing stops trying: at most 5 starts an hour.
             'StartLimitIntervalSec': '3600', 'StartLimitBurst': '5'},
            {'ExecStart': f'{drainhound} experiment auto', 'ExecStopPost': f'{drainhound} experiment restore',
             'Restart': 'on-failure', 'RestartSec': '30', 'TimeoutStopSec': '180'},
            True),
        # While no run owns the settings: retries restores a run could not
        # finish, keeps applied settings (`drainhound experiment apply`) to battery
        # power only, and watches the kernel log while any are applied.
        u['reconcile']: (
            {'Description': 'drainhound: keep applied power settings in step with the power source', 'After': SESSION},
            {'Type': 'oneshot', 'ExecStart': f'{drainhound} experiment reconcile', 'TimeoutStartSec': '180'},
            False),
        u['reconcile'].replace('.service', '.timer'): (
            {'Description': 'drainhound: reconcile power settings every minute', 'PartOf': SESSION},
            {'OnActiveSec': '1min', 'OnUnitActiveSec': '1min', 'AccuracySec': '10s'},
            True),
        # Started and stopped by the throttle-apps setting (experiments, or `apply`).
        u['throttle']: (
            {'Description': 'drainhound: throttle unfocused apps on battery', **session},
            {'ExecStart': f'{drainhound} throttle', 'ExecStopPost': f'{drainhound} throttle --release',
             'Restart': 'on-failure', 'RestartSec': '5'},
            False),
        # The apps come from config [freeze] apps; with none it idles.
        u['freeze']: (
            {'Description': 'drainhound: freeze long-unused apps on battery', **session},
            {'ExecStart': f'{drainhound} freeze', 'ExecStopPost': f'{drainhound} freeze --release',
             'Restart': 'on-failure', 'RestartSec': '5'},
            True),
    }
    return {name: render(name, unit, body, wanted) for name, (unit, body, wanted) in table.items()}


def render(name, unit, body, wanted):
    lines = ['# Written by `drainhound units`.', '[Unit]', *entries(unit), '',
             '[Timer]' if name.endswith('.timer') else '[Service]', *entries(body)]
    if wanted:
        lines += ['', '[Install]', f'WantedBy={SESSION}']
    return '\n'.join(lines) + '\n'


def entries(table):
    for key, value in table.items():
        for one in value if isinstance(value, list) else [value]:
            yield f'{key}={one}'


def enabled_names():
    names = []
    for role in DEFAULT_ENABLED:
        base, _, kind = role.partition('.')
        unit = config.UNITS[base]
        names.append(unit.replace('.service', f'.{kind}') if kind else unit)
    return names


def main(argv=()):
    parser = argparse.ArgumentParser(prog='drainhound units', description=__doc__.split('\n\n')[0])
    parser.add_argument('--install', action='store_true', help='write to ~/.config/systemd/user and enable')
    parser.add_argument('--no-enable', action='store_true', help='with --install: only write and reload')
    parser.add_argument('--dir', type=Path, help='write the unit files to DIR (nothing else)')
    parser.add_argument('--exec', dest='drainhound', help='the drainhound executable the units run')
    args = parser.parse_args(list(argv))
    drainhound = args.drainhound or shlex.join(config.self_command())
    files = units(drainhound)
    if args.dir is None and not args.install:
        for name, text in files.items():
            print(f'### {name}\n{text}')
        return 0
    directory = args.dir or config.HOME / '.config/systemd/user'
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text)
    print(f'wrote {len(files)} units to {directory}', file=sys.stderr)
    if args.dir is not None:
        return 0
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    if not args.no_enable:
        subprocess.run(['systemctl', '--user', 'enable', '--now', *enabled_names()], check=True)
        print(f"enabled {', '.join(enabled_names())}", file=sys.stderr)
    return 0

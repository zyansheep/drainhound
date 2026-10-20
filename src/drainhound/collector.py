#!/usr/bin/env python3
"""Bounded, local cache for Waybar's native hover panels; no command lines stored."""
import collections
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sys
import time

from . import config
from . import store
from .power import RECORD, Monitor, clocks

TICKS = os.sysconf('SC_CLK_TCK')
PAGE = os.sysconf('SC_PAGE_SIZE')


# Executable (lower case, '-wrapped' and a leading '.' gone) → the name logged and
# shown; anything else is logged by its executable's name. [apps] names adds to it.
BUILTIN_NAMES = {
    # Firefox and its forks: content processes share the browser's executable.
    'firefox': 'Firefox', 'firefox-esr': 'Firefox', 'firefox-bin': 'Firefox', 'firefox-nightly': 'Firefox',
    'firefox-developer-edition': 'Firefox', 'librewolf': 'LibreWolf', 'floorp': 'Floorp', 'zen': 'Zen Browser',
    'zen-bin': 'Zen Browser', 'waterfox': 'Waterfox', 'mullvadbrowser': 'Mullvad Browser',
    # Chromium and its relatives: helpers say what they are with --type=.
    'chromium': 'Chromium', 'chromium-browser': 'Chromium', 'chrome': 'Chrome', 'google-chrome': 'Chrome',
    'brave': 'Brave', 'vivaldi-bin': 'Vivaldi', 'msedge': 'Microsoft Edge', 'opera': 'Opera',
    'epiphany': 'GNOME Web', 'falkon': 'Falkon', 'qutebrowser': 'qutebrowser',
    # Editors and coding apps.
    'code': 'VS Code', 'code-oss': 'Code - OSS', 'codium': 'VSCodium', 'vscodium': 'VSCodium', 'cursor': 'Cursor',
    'windsurf': 'Windsurf', 'zed': 'Zed', 'zeditor': 'Zed', 'zed-editor': 'Zed', 't3code': 'T3 Code',
    'emacs': 'Emacs', 'nvim': 'Neovim', 'neovide': 'Neovide', 'sublime_text': 'Sublime Text', 'kate': 'Kate',
    'gnome-text-editor': 'Text Editor', 'electron': 'Electron',
    'cc1': 'C compiler', 'cc1plus': 'C++ compiler',
    'gnome-terminal-server': 'GNOME Terminal', 'kgx': 'Console', 'wezterm-gui': 'WezTerm',
    # Chat and media.
    'vesktop': 'Vesktop', 'vesktop.bin': 'Vesktop', 'discord': 'Discord', 'signal-desktop': 'Signal',
    'slack': 'Slack', 'element-desktop': 'Element', 'element': 'Element', 'telegram-desktop': 'Telegram',
    'teams-for-linux': 'Teams', 'mattermost-desktop': 'Mattermost',
    'zoom': 'Zoom', 'thunderbird': 'Thunderbird', 'spotify': 'Spotify', 'vlc': 'VLC', 'obs': 'OBS Studio',
    'pipewire': 'PipeWire', 'pipewire-pulse': 'PipeWire audio', 'wireplumber': 'WirePlumber',
    # Notes and other apps.
    'obsidian': 'Obsidian', 'logseq': 'Logseq', 'joplin': 'Joplin', 'bitwarden': 'Bitwarden',
    # The desktop itself.
    'niri': 'Niri', 'sway': 'Sway', 'hyprland': 'Hyprland', 'gnome-shell': 'GNOME Shell',
    'kwin_wayland': 'KWin', 'kwin_x11': 'KWin', 'plasmashell': 'Plasma', 'waybar': 'Waybar',
    'swaync': 'Notifications', 'mako': 'Notifications', 'dunst': 'Notifications', 'drainhound': 'drainhound',
}


def configured_names(names=None):
    """[apps] names (or `names`): {executable or Electron app directory, lower case: name}."""
    names = config.get('apps', 'names') if names is None else names
    return {str(exe).lower(): str(name) for exe, name in (names if isinstance(names, dict) else {}).items()}


APP_NAMES = {**BUILTIN_NAMES, **configured_names()}
FIREFOXES = {'Firefox', 'LibreWolf', 'Floorp', 'Zen Browser', 'Waterfox', 'Mullvad Browser'}
CHROMIUMS = {'Chromium', 'Chrome', 'Brave', 'Vivaldi', 'Microsoft Edge', 'Opera'}
BROWSERS = FIREFOXES | CHROMIUMS | {'GNOME Web', 'Falkon', 'qutebrowser'}
# Directories that name an app run by a shared electron binary (in its app.asar's
# or main script's path), as distributions install them; the first match wins.
# The [apps] names keys count too.
ELECTRON_APPS = ('vesktop', 'discord', 'obsidian', 't3code', 'codium', 'vscodium', 'code-oss', 'cursor',
                 'signal-desktop', 'element-desktop', 'element', 'slack', 'teams-for-linux', 'logseq', 'joplin',
                 'bitwarden', 'mattermost-desktop') + tuple(configured_names())


def executable_name(value):
    name = Path(value).name.removesuffix(' (deleted)').lstrip('.')
    return name.removesuffix('-wrapped')


def process_identity(directory, comm):
    """Read only identity metadata. Arguments are never retained in the cache."""
    try:
        exe = executable_name(str((directory / 'exe').readlink()))
    except OSError:
        exe = executable_name(comm)
    app = APP_NAMES.get(exe.lower())
    args = []
    if exe.lower().startswith('electron') or app in CHROMIUMS:
        try:
            with (directory / 'cmdline').open('rb') as command:
                args = command.read(16384).decode(errors='replace').split('\0')
        except OSError:
            pass
    # Electron shares a binary across apps. Recognize installed app entry paths,
    # never URLs or arbitrary substring matches in document/file arguments.
    if exe.lower().startswith('electron'):
        app = APP_NAMES.get(comm.lower(), 'Electron')
        for arg in args[:4]:
            if arg.startswith('/') and arg.endswith(('.asar', '.js')):
                parts = {part.lower() for part in Path(arg).parts}
                for key in ELECTRON_APPS:
                    if key in parts:
                        app = APP_NAMES.get(key, key)
                        break
    role = next((a.partition('=')[2] for a in args if a.startswith('--type=')), '')
    return app, role


def process_label(pid, comm, app, role):
    if app in BROWSERS:
        if comm.startswith(('Isolated Web', 'Web Content', 'WebCOOP')) or role == 'renderer':
            # One process may host several tabs or cross-site frames. A PID is
            # honest and can be matched to the browser's about:processes page.
            return f'{app} web · PID {pid}'
        if comm.startswith('Isolated Servic'):
            return f'{app} worker · PID {pid}'
        roles = {'GPU Process': 'graphics', 'WebExtensions': 'extensions', 'RDD Process': 'media decoder',
                 'Socket Process': 'network', 'Utility Process': 'utilities',
                 'Privileged Cont': 'browser pages', 'Privileged Mozi': 'browser services',
                 'Web Content': 'web content'}
        if comm in roles:
            return f'{app} · {roles[comm]}'
    if role:
        readable = {'renderer': 'renderer', 'gpu-process': 'GPU',
                    'utility': 'utilities', 'zygote': 'process launcher'}.get(role, 'helper')
        return f'{app or executable_name(comm)} · {readable}'
    return app or APP_NAMES.get(executable_name(comm).lower(), executable_name(comm))


def processes(root=Path('/proc')):
    result = {}
    metadata = {}
    for directory in root.iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            stat = (directory / 'stat').read_text()
            # comm may contain spaces and parentheses; fields after its final ')'
            # begin with field 3 (state).
            end = stat.rindex(')')
            fields = stat[end + 2:].split()
            name = stat[stat.index('(') + 1:end]
            start = int(fields[19])
            app, role = process_identity(directory, name)
            metadata[int(directory.name)] = (int(fields[1]), app, role, name)
            result[(int(directory.name), start)] = (
                name, (int(fields[11]) + int(fields[12])) / TICKS,
                max(0, int(fields[21])) * PAGE)
        except (OSError, ValueError, IndexError):
            continue  # Exited process or inaccessible task; never fail the sample.
    def application(pid, seen=None):
        seen = set() if seen is None else seen
        if pid in seen or pid not in metadata or len(seen) > 12:
            return None
        seen.add(pid)
        parent, app, role, _ = metadata[pid]
        if app is None or (app == 'Electron' and role):
            ancestor = application(parent, seen)
            # Sandboxed user services may read stat but not /proc/PID/exe.
            # Browser ancestry still identifies content/helper processes.
            if ancestor in BROWSERS or (app == 'Electron' and role):
                return ancestor or app
        return app

    for key, (comm, seconds, rss) in list(result.items()):
        pid = key[0]
        app = application(pid)
        # The coarse app (no PID or browser detail) is what the power log keeps.
        coarse = 'Kernel' if 2 in (pid, metadata[pid][0]) else app or executable_name(comm)
        result[key] = (process_label(pid, comm, app, metadata[pid][2]), seconds, rss, coarse)
    return result


def temperatures(hwmon=Path('/sys/class/hwmon'), thermal=Path('/sys/class/thermal')):
    result = []
    for directory in sorted(hwmon.glob('hwmon*')):
        try:
            chip = (directory / 'name').read_text().strip()
        except OSError:
            chip = directory.name
        for sensor in sorted(directory.glob('temp*_input')):
            try:
                value = float(sensor.read_text()) / 1000
                label = sensor.with_name(sensor.name.replace('_input', '_label'))
                name = label.read_text().strip() if label.exists() else sensor.stem.replace('_input', '')
                if -273.15 < value < 300:
                    result.append({'name': f'{chip} · {name}', 'value': f'{value:.1f} °C'})
            except (OSError, ValueError):
                continue
    for zone in sorted(thermal.glob('thermal_zone*')):
        try:
            value = float((zone / 'temp').read_text()) / 1000
            if -273.15 < value < 300:
                result.append({'name': f"{(zone / 'type').read_text().strip()} · {zone.name}",
                               'value': f'{value:.1f} °C'})
        except (OSError, ValueError):
            continue
    return result


class History:
    def __init__(self, cores=None):
        self.cores = cores or os.cpu_count() or 1
        self.previous = None
        self.last_time = None
        self.intervals = collections.deque()

    def sample(self, now, current):
        if self.previous is not None:
            elapsed = now - self.last_time
            if 0 < elapsed <= 10:
                usage = collections.defaultdict(float)
                for key, (name, seconds, *_) in current.items():
                    if key in self.previous:
                        usage[name] += max(0, seconds - self.previous[key][1])
                    elif key[1] / TICKS >= self.last_time:
                        usage[name] += seconds
                self.intervals.append((self.last_time, now, usage))
            else:
                self.intervals.clear()  # Suspend or clock discontinuity.
        self.previous, self.last_time = current, now
        cutoff = now - 60
        while self.intervals and self.intervals[0][1] <= cutoff:
            self.intervals.popleft()
        usage = collections.defaultdict(float)
        covered = 0.0
        for start, end, values in self.intervals:
            duration = end - max(start, cutoff)
            covered += duration
            for name, seconds in values.items():
                usage[name] += seconds * duration / (end - start)
        memory = collections.defaultdict(int)
        for name, _, rss, *_ in current.values():
            memory[name] += rss
        cpu = [{'name': name, 'value': f'{seconds / covered / self.cores * 100:.1f}%'}
               for name, seconds in sorted(usage.items(), key=lambda row: (-row[1], row[0]))[:100]
               if covered and seconds > 0]
        ram = [{'name': name, 'value': f'{rss / 2**20:,.0f} MiB'}
               for name, rss in sorted(memory.items(), key=lambda row: (-row[1], row[0]))[:100]
               if rss]
        return {'cpu': cpu, 'memory': ram, 'coverage': round(covered), 'timestamp': time.time()}


def browser_directory():
    """Where contrib/firefox-bridge.js writes each Firefox-family browser's
    content processes ([apps] firefox_processes, else $XDG_RUNTIME_DIR/firefox-processes)."""
    if configured := config.get('apps', 'firefox_processes'):
        return Path(os.path.expandvars(str(configured))).expanduser()
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    return Path(runtime) / 'firefox-processes' if runtime else None


BRIDGED = re.compile(r'(.+) (?:web|worker) · PID \d+')  # process_label's rows for a browser's content processes.


def browser_labels(state, current, directory, now=None):
    """Enrich display rows after accounting; changing titles cannot split CPU history.
    Rows of FIREFOXES' content processes get the site they show, from the files
    the bridge writes in `directory` (None: no bridge)."""
    now = time.time() if now is None else now
    labels = {}
    for path in directory.glob('*.json') if directory is not None else ():
        try:
            if path.stat().st_size > 1024 * 1024:
                continue
            data = json.loads(path.read_text())
            if abs(now - float(data['timestamp'])) > 15:
                continue
            for row in data['processes'][:2048]:
                key = (int(row['pid']), int(row['start']))
                process = current.get(key)
                bridged = BRIDGED.fullmatch(process[0]) if process else None
                if not bridged or bridged[1] not in FIREFOXES:
                    continue
                origin = str(row['origin']).split('^')[0]
                if not origin:
                    continue
                title = next((t for t in row.get('titles', []) if isinstance(t, str) and t), '')
                detail = origin + (' · ' + title if title else '')
                detail = ' '.join(detail.split())[:160]
                labels[process[0]] = f'{bridged[1]} · {detail} · PID {key[0]}'
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    for kind in ('cpu', 'memory'):
        for row in state[kind]:
            row['name'] = labels.get(row['name'], row['name'])


def single_instance(state):
    """Hold an exclusive lock on <state>/collector.lock while the process lives, or
    exit: a second collector would log every record twice."""
    state.mkdir(parents=True, exist_ok=True)
    path = state / 'collector.lock'
    handle = open(path, 'a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        holder = handle.read().strip() or '?'
        handle.close()
        sys.exit(f'drainhound: another collector (PID {holder}) is already running; it holds {path}')
    handle.truncate(0)
    handle.write(f'{os.getpid()}\n')
    handle.flush()
    return handle


def collect(power, history, directory, began, scan=processes, sensors=temperatures, browsers=browser_directory):
    """One sample: scan processes, then read the clocks, so a record's interval
    ends where its process and counter readings do. A suspend during the scan
    (uptime jumped) discards the sample (None): the next one reports the gap."""
    current = scan()
    boot, wall = clocks()
    if boot - began > RECORD:
        return None
    state = history.sample(boot, current)
    browser_labels(state, history.previous, browsers())
    state['temperature'] = sensors()
    state['battery'] = power.update(boot, wall, current)
    temporary = directory / 'snapshot.tmp'
    temporary.write_text(json.dumps(state, ensure_ascii=True))
    temporary.chmod(0o600)
    temporary.replace(directory / 'snapshot.json')
    return state


def main(argv=()):
    log = config.LOG
    if list(argv) == ['import']:
        # Re-read every archive day into the power database, keeping what it has.
        # Safe beside the running collector (idempotent transactions), so no lock.
        for day in store.import_logs(log, config.DB, force=True):
            print(day)
        return 0
    if argv:
        sys.exit('usage: drainhound collect [import]')
    lock = single_instance(log.parent)  # noqa: F841 (held until exit)
    directory = config.RUNTIME
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    power = Monitor(log, experiment=directory / 'experiment.json', throttle=directory / 'throttle.json')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    history = History()
    try:
        while True:
            began = time.clock_gettime(time.CLOCK_BOOTTIME)
            if collect(power, history, directory, began) is not None:
                power.maintain()  # Deferred database work (a migration's recompute), a piece per loop.
            # Battery current and chip power change faster than the process scan.
            while (remaining := began + 2 - time.clock_gettime(time.CLOCK_BOOTTIME)) > .1:
                time.sleep(min(.5, remaining))
                power.fast.sample()
    finally:
        power.close()


if __name__ == '__main__':
    main(sys.argv[1:])

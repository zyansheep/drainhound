#!/usr/bin/env python3
"""Background-app throttle: while on battery, every app with a window but the
focused one runs under a CPU quota of QUOTA% of one core (cgroup cpu.max),
lifted the moment it gets focus and kept off for GRACE after it loses it, so
switching back and forth never lands on a cold app. Apps without a window —
helpers, daemons, clipboard owners (wl-copy serves every paste), whatever a
launcher just spawned while a layer surface had focus — are never capped:
capped, the emoji picker took 4–6 s to paste. Apps playing or recording audio or video
(PipeWire streams that are running, not just open) and KEEP are left alone,
and so is everything on AC. KEEP is where real work runs in the background:
terminals and editors/IDEs (builds, agent CLIs and language servers run in
their scopes; capped, a build ran 10× slower whenever another window had
focus), agent apps and the input method that serves the focused app.

An app is every systemd scope in app.slice (or a slice in it: uwsm, app2unit
and Flatpak start apps in app.slice/app-graphical.slice) with the same launch
pid: Electron apps move their main process into a scope of their own
(app-vesktop-2295) and leave the renderers in the one the launcher started
(app-niri-vesktop-2295), so freeing only the window's scope left Discord's
renderers capped — frozen — while it was focused. A Flatpak app's sandboxes
share its app id, and a launcher's scope with a random id joins the app of
the same name (group()). The quota is the app's: its scopes share it equally (a
two-scope app gets QUOTA/2 per scope, not QUOTA each).

A window is an app's by its pid's scope or, where its pid says nothing (an X11
window under niri has xwayland-satellite's), by its app id: the app whose
scope has that name (window_apps). While the focused window is no app's that
this can tell (a game Steam started, under niri), it could be any app's, so
none is capped.

    wattif throttle [--quota N] [--keep PATTERN ...]
    wattif throttle --release      (lift every quota; the unit's ExecStopPost)

wattif-throttle.service runs it; wattif experiment's throttle-apps setting starts
and stops that unit. It follows focus from the compositor's event stream
(compositor.py: niri or sway; elsewhere it knows no windows, so it caps
nothing) and streams from `pw-dump --monitor`, so between changes it does
nothing but a RESCAN for new apps and AC changes. RUNTIME/throttle.json says
what it throttles, for the collector. Stopping it lifts every quota.

The quota, the grace period and more apps to keep come from the configuration
([throttle] quota, grace, keep); --quota and --keep override them.
"""
import argparse
import collections
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import battery as batteries
from . import compositor
from . import config

QUOTA = config.get('throttle', 'quota')  # % of one core per background app, shared by its scopes.
GRACE = config.get('throttle', 'grace')  # s an app stays free after it loses focus.
PERIOD = 100_000  # µs: cgroup cpu.max's period.
MINIMUM = 1000  # µs: the smallest quota cpu.max takes.
UNLIMITED = f'max {PERIOD}'
# Matched (case-insensitively) anywhere in the scope's name, systemd escapes undone.
TERMINALS = ('foot', 'kitty', 'alacritty', 'wezterm', 'ghostty', 'konsole', 'terminal', 'xterm', 'tilix',
             'terminator', 'ptyxis', 'kgx', 'gnome.console', 'blackbox', 'contour', 'urxvt', 'tmux', 'zellij')
# 'code': VS Code, Code - OSS and agent apps named so (T3 Code); 'terminal': GNOME's, Xfce's, MATE's, ...
EDITORS = ('code', 'codium', 'cursor', 'windsurf', 'zed', 'emacs', 'neovide', 'gvim', 'jetbrains', 'intellij',
           'pycharm', 'android-studio', 'sublime', 'kate', 'kdevelop', 'qtcreator', 'builder', 'eclipse', 'lapce')
INPUT_METHODS = ('fcitx', 'ibus')  # They type into the focused app (and have no window to be capped by).
# Builds, agent CLIs and language servers run in terminals and editors; agents keep working.
KEEP = INPUT_METHODS + TERMINALS + EDITORS
# Who started a scope: app-<launcher>-<app id>-<pid or random>.scope (systemd's naming for desktop apps;
# uwsm's and app2unit's launcher is the desktop's name, as XDG_CURRENT_DESKTOP gives it).
LAUNCHERS = ('niri', 'gnome', 'flatpak', 'kde', 'sway', 'Hyprland', 'hyprland')
RESCAN = 5  # s: new apps, AC changes.
RUNTIME = config.RUNTIME
STATE = RUNTIME / 'throttle.json'
APPS = Path(f'/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice')
MEDIA = ('Stream/Output/Audio', 'Stream/Input/Audio', 'Stream/Output/Video', 'Stream/Input/Video')


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def on_battery(power=batteries.SUPPLIES):
    """Whether the system runs on its battery: no charger online, as the
    estimator and the collector read it (battery.mains_online: a Mains supply,
    or with none, as on laptops that charge only over USB-C, the system's USB
    supplies). With no charger to read at all, on battery if it has one: a
    desktop or a VM has none, and is never on battery."""
    online = batteries.mains_online(power)
    return not online if online is not None else bool(batteries.find(power))


def string_list(value, setting):
    """A setting that lists patterns, as a list of non-empty strings: a lone
    string is a one-element list ([freeze] apps = "steam" is ["steam"]; read
    letter by letter, its 'e' matched nearly every scope), None none. An empty
    pattern would match every scope, so it is dropped; anything but a string
    or a list is refused."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise SystemExit(f'wattif: {setting} must be a list of names, like ["steam"], not {value!r}')
    return [str(item) for item in value if str(item).strip()]


class Session:
    """What the desktop is doing, from the compositor's and PipeWire's event
    streams: which app has focus, which have windows, which play or record
    media. wattif throttle and wattif freeze (freeze.py) act on it. `desktop`
    is a compositor backend (main() picks it with compositor.detect(); niri's
    by default)."""

    def __init__(self, apps=APPS, battery=on_battery, proc=Path('/proc'), clock=time.monotonic, desktop=None):
        self.apps, self.battery, self.proc, self.clock = apps, battery, proc, clock
        self.desktop = desktop if desktop is not None else compositor.Niri()
        self.nodes = {}  # PipeWire node id → what we know
        # From the last look(): each app scope's app, and the apps by scope name (for windows matched by app id).
        self.app, self.named = {}, None
        self.written = None  # The state file's contents as last written (run(); a Freezer's before it freezes).

    def window_event(self, event):
        """One event from the compositor's stream (niri's: one line of `niri msg -j event-stream`)."""
        self.desktop.event(event)

    def media_event(self, objects):
        """One batch of `pw-dump --monitor`: nodes added, changed (possibly only
        what changed) or removed (info null)."""
        for node in objects:
            if node.get('type', 'PipeWire:Interface:Node') != 'PipeWire:Interface:Node' or 'id' not in node:
                continue
            info = node.get('info', {})
            if info is None:
                self.nodes.pop(node['id'], None)
                continue
            known = self.nodes.setdefault(node['id'], {})
            if (props := info.get('props')) is not None:
                known.update(media=props.get('media.class') in MEDIA, pid=props.get('application.process.id'))
            if 'state' in info:
                known['state'] = info['state']

    @property
    def media(self):
        """{node: pid} of audio/video streams that are running."""
        return {n: int(k['pid']) for n, k in self.nodes.items()
                if k.get('media') and k.get('pid') and k.get('state') == 'running'}

    def look(self):
        """Every app scope now (app_scopes), each with its app (group()) in
        self.app and, by name, in self.named, for window_apps."""
        scopes = app_scopes(self.apps)
        self.app, self.named = group(scopes), {}
        for scope in scopes:
            self.named.setdefault(label(scope).lower(), set()).add(self.app[scope])
        return scopes

    def app_for(self, scope):
        return self.app.get(scope) or app_of(scope)

    def scope_of(self, pid):
        """The scope `pid` runs in (its path in app.slice, through any slices
        there: 'app-graphical.slice/app-flatpak-…-412.scope'), or None."""
        if not pid:
            return None
        for line in (read(self.proc / str(pid) / 'cgroup') or '').splitlines():
            parts = line.rpartition(':')[2].split('/')
            if 'app.slice' in parts[:-1]:
                path = parts[parts.index('app.slice') + 1:]
                depth = next((i for i, part in enumerate(path) if not part.endswith('.slice')), len(path))
                return '/'.join(path[:depth + 1]) if depth < len(path) and path[depth].endswith('.scope') else None
        return None

    def stands_in(self, pid):
        """Whether `pid` is an X server's rather than the app's: niri gives every
        X11 window xwayland-satellite's (which may run in an app scope of its own)."""
        return 'xwayland' in (read(self.proc / str(pid) / 'comm') or '').lower()

    def window_apps(self, window):
        """The apps a window (an id in desktop.windows) is: its pid's scope's;
        else, where its pid is in none or is an X server's (stands_in), those
        with a scope its app id names ('steam': app-niri-steam-5000.scope;
        'md.Obsidian': app-md.Obsidian-2243.scope); else set(): whose it is is
        unknown."""
        known = self.desktop.windows.get(window) or {}
        pid = known.get('pid')
        if pid and not self.stands_in(pid) and (scope := self.scope_of(pid)):
            return {self.app_for(scope)}
        name = str(known.get('app_id') or '').rpartition('.')[2].lower()
        if self.named is None:
            self.look()
        return set(self.named.get(name, ())) if name else set()

    def focused_apps(self):
        """The focused window's apps: set() with no window focused (a layer
        surface has focus, or an empty workspace), None if they are unknown."""
        if self.desktop.focused is None:
            return set()
        return self.window_apps(self.desktop.focused) or None

    def windowed(self):
        """Apps with a window."""
        return set().union(*map(self.window_apps, self.desktop.windows))

    def playing(self):
        """Apps with an audio or video stream running."""
        return {self.app_for(scope) for pid in self.media.values() if (scope := self.scope_of(pid))}

    def in_view(self):
        """Windows that may be in use: the focused one, and every one on a
        workspace that is active on its output (maybe on screen:
        compositor.Desktop.shown)."""
        return {w for w in self.desktop.windows if self.desktop.shown(w)} | {self.desktop.focused} - {None}

    def shown(self):
        """Apps with a window on a workspace that is active on its output."""
        return set().union(*(self.window_apps(w) for w in self.desktop.windows if self.desktop.shown(w)))

    def unknown_in_view(self):
        """Whether a window in view (in_view) is no app's that window_apps can tell."""
        return not all(map(self.window_apps, self.in_view()))

    def last_focused(self):
        """{app: when one of its windows last had focus} (CLOCK_MONOTONIC)."""
        out = {}
        for window, known in self.desktop.windows.items():
            if (stamp := known.get('focus')) is not None:
                for app in self.window_apps(window):
                    out[app] = max(out.get(app, stamp), stamp)
        return out


class Throttle(Session):
    state_path = STATE

    def __init__(self, apps=APPS, quota=QUOTA, keep=KEEP, battery=on_battery, proc=Path('/proc'), grace=GRACE,
                 clock=time.monotonic, desktop=None):
        super().__init__(apps, battery, proc, clock, desktop)
        self.keep, self.grace = tuple(k.lower() for k in string_list(keep, 'keep')), grace
        self.quota = int(quota * PERIOD // 100)  # µs per period for a whole app.
        self.limit = f'{self.quota} {PERIOD}'
        self.current, self.left = set(), {}  # The focused apps; when each app last lost focus.

    def wanted(self):
        """{scope: cpu.max} for every app scope."""
        scopes = self.look()
        if not self.battery():
            return dict.fromkeys(scopes, UNLIMITED)
        now = self.clock()
        windowed = self.windowed()  # Empty once the compositor's stream has ended (Desktop.clear): nothing capped.
        focused = self.focused_apps()
        if focused is None:  # Focus is on a window that may be any app's: as if every app had it.
            focused = windowed
        for app in self.current - focused:
            self.left[app] = now
        self.current = set(focused)
        free = self.playing() | focused
        self.left = {app: left for app, left in self.left.items() if now - left < self.grace}
        free |= set(self.left)
        free |= {self.app[s] for s in scopes if any(k in unescape(unit(s)).lower() for k in self.keep)}
        capped = {s for s in scopes if self.app[s] in windowed and self.app[s] not in free}
        shares = collections.Counter(self.app[s] for s in capped)  # The app's quota, split between its scopes.
        return {s: self.share(shares[self.app[s]]) if s in capped else UNLIMITED for s in scopes}

    def share(self, scopes):
        return f'{max(MINIMUM, self.quota // scopes)} {PERIOD}'

    def apply(self, values=None):
        """Write what changed; the scopes now throttled."""
        values = self.wanted() if values is None else values
        for scope, value in values.items():
            path = self.apps / scope / 'cpu.max'
            if read(path) != value:
                try:
                    path.write_text(value)
                except OSError:
                    pass  # The app exited meanwhile.
        return sorted(s for s, v in values.items() if v != UNLIMITED)

    def release(self):
        self.apply(dict.fromkeys(app_scopes(self.apps), UNLIMITED))

    def step(self, media_known):
        """Apply what is wanted now; the state file's contents. Without stream
        data (PipeWire down), apps that might be playing stay unthrottled."""
        throttled = self.apply() if media_known else self.apply(dict.fromkeys(self.wanted(), UNLIMITED))
        return {'quota': self.limit, 'on_battery': self.battery(),
                'throttled': sorted({label(s) for s in throttled}, key=str.lower)}


def app_scopes(apps):
    """Every app scope (app-*.scope) in `apps` (app.slice) and in the slices
    there, as its path in `apps` ('app-niri-floorp-2240.scope',
    'app-graphical.slice/app-sway-firefox-4f3a9c21.scope'), sorted."""
    found = [p.name for p in apps.glob('app-*.scope')]
    for nested in apps.glob('*.slice'):
        found += [f'{nested.name}/{scope}' for scope in app_scopes(nested)]
    return sorted(found)


def unit(scope):
    """A scope's unit name, without the slices it is in."""
    return scope.rpartition('/')[2]


def app_of(scope):
    """An app's scopes share its launch pid: 'app-niri-vesktop-2295.scope' and
    'app-vesktop-2295.scope' are both '2295'. Flatpak numbers each sandbox
    (app-flatpak-<app id>-<n>.scope: an Electron app's zygote gets one of its
    own), so its scopes share the app id. A scope with neither is its own app
    (until group() joins it to another)."""
    if flatpak := re.fullmatch(r'app-flatpak-(.+)-\d+\.scope', unit(scope)):
        return f'flatpak:{flatpak[1]}'
    match = re.search(r'-(\d+)\.scope$', unit(scope))
    return match[1] if match else scope


def group(scopes):
    """{scope: its app}: app_of's, except that a scope named with a random id
    (uwsm's, app2unit's, KDE's) joins every app with a scope of its label: an
    Electron app launched so moves its main process into app-<id>-<pid>.scope
    and leaves the rest in the launcher's (T3 Code started by `uwsm app`, as
    recorded on a Framework 13: app-graphical.slice/app-niri-t3code-6a5fdff9.scope
    and app-com.t3tools.T3Code-814872.scope). Joined apps go by a launch pid."""
    parent, named = {}, {}

    def root(app):
        while parent.get(app, app) != app:
            app = parent[app]
        return app

    for scope in scopes:
        named.setdefault(label(scope).lower(), set()).add(app_of(scope))
    for scope in scopes:
        if app_of(scope) == scope:  # Neither a launch pid nor a Flatpak app id.
            for other in named[label(scope).lower()]:
                # The root: a launch pid (the lowest) before a scope's name.
                first, second = sorted((root(scope), root(other)), key=lambda a: (not a.isdigit(), len(a), a))
                if first != second:
                    parent[second] = first
    return {scope: root(app_of(scope)) for scope in scopes}


def unescape(scope):
    """systemd's escapes: 'emoji\\x2dpicker' → 'emoji-picker'."""
    return re.sub(r'\\x([0-9a-f]{2})', lambda m: chr(int(m[1], 16)), scope)


def label(scope):
    """'app-niri-floorp-2240.scope' → 'floorp', 'app-md.Obsidian-2243.scope' →
    'Obsidian', 'app-gnome-org.gnome.Terminal-3061.scope' → 'Terminal': the
    launcher (LAUNCHERS) and the trailing pid or random id go, as
    experiment.unit_label's do; so does a reverse-DNS app id's domain. Done on
    the escaped name, where only systemd's own separators are '-' (an app id's
    are '\\x2d'), so 'app-gnome\\x2dcalculator-9.scope' stays 'gnome-calculator'."""
    name = unit(scope).removesuffix('.scope').removeprefix('app-')
    name = re.sub(rf'^(?:{"|".join(LAUNCHERS)})-', '', name)
    name = re.sub(r'-(?:\d+|[0-9a-f]{16,}|(?=[a-f]*\d)[0-9a-f]{8,})$', '', name)  # uwsm's, app2unit's: 8 hex digits.
    return unescape(name).rpartition('.')[2]


def write_state(state, path=STATE):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


class Stream:
    """A child process's stdout, read as it comes and started again if it exits."""

    def __init__(self, command, selector):
        self.command, self.selector, self.process, self.buffer, self.retry = command, selector, None, '', 0.0
        self.start()

    def start(self):
        self.buffer = ''
        try:
            self.process = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            os.set_blocking(self.process.stdout.fileno(), False)
            self.selector.register(self.process.stdout, selectors.EVENT_READ, self)
        except OSError as error:
            print(f'wattif throttle: {self.command[0]}: {error}', file=sys.stderr)
            self.process, self.retry = None, time.monotonic() + RESCAN

    def read(self):
        chunk = self.process.stdout.read()
        if chunk:
            self.buffer += chunk.decode(errors='replace')
            return True
        self.selector.unregister(self.process.stdout)
        self.process.stdout.close()
        self.process.wait()
        self.process, self.retry = None, time.monotonic() + RESCAN  # It exited (the compositor or PipeWire restarting).
        return False

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()


lines = compositor.json_lines  # niri: one event per line.
batches = compositor.json_values  # pw-dump: one JSON array per batch of changes.


def run(agent, pw_command=('pw-dump', '--monitor')):
    """Feed `agent` (a Session: Throttle, freeze.Freezer) the compositor's
    (agent.desktop's) and PipeWire's events and act (its step()) after each
    batch, or every RESCAN; its state goes to agent.state_path. When a stream
    ends, what it said is forgotten until it is back: the windows (so nothing
    acts on a dead compositor's) and the media streams (apps are left alone
    meanwhile). Stopping releases what it did."""
    selector = selectors.DefaultSelector()
    desktop = agent.desktop
    windows = Stream(list(desktop.command), selector) if desktop.command else None
    if windows is not None and windows.process is not None:
        desktop.started()
    pw = Stream(list(pw_command), selector)
    started = time.time()
    try:
        while True:
            for key, _ in selector.select(timeout=RESCAN):
                stream = key.data
                if not stream.read():
                    if stream is pw:
                        agent.nodes.clear()
                    else:
                        desktop.clear()
                    continue
                if stream is windows:
                    for event in desktop.decode(windows):
                        desktop.event(event)
                else:
                    for batch in batches(pw):
                        agent.media_event(batch)
            for stream in filter(None, (windows, pw)):
                if stream.process is None and time.monotonic() >= stream.retry:
                    stream.start()
                    if stream is windows and stream.process is not None:
                        desktop.started()
            state = {'pid': os.getpid(), 'since': started, **agent.step(pw.process is not None)}
            if state != agent.written:
                write_state(state, agent.state_path)
                agent.written = state
    finally:
        for stream in filter(None, (windows, pw)):
            stream.stop()
        agent.release()
        agent.state_path.unlink(missing_ok=True)


def exit_on_signals():
    """SIGTERM (systemctl stop) and SIGHUP (the session ending, a terminal
    closing) exit through run()'s finally, which undoes what the agent did."""
    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, lambda *_: sys.exit(0))


def keep_patterns(extra=None):
    """Scope patterns never throttled: KEEP and the configuration's [throttle] keep (or `extra`)."""
    extra = config.get('throttle', 'keep') if extra is None else extra
    return KEEP + tuple(string_list(extra, '[throttle] keep'))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='wattif throttle', description=__doc__.split('\n\n')[0])
    parser.add_argument('--quota', type=int, default=QUOTA,
                        help=f'%% of one core per background app, all its scopes (default: [throttle] quota, {QUOTA})')
    parser.add_argument('--keep', action='append',
                        help='a scope pattern never throttled, instead of [throttle] keep; terminals, editors and '
                             f'input methods always are ({", ".join(KEEP)})')
    parser.add_argument('--release', action='store_true', help='lift every quota and exit')
    args = parser.parse_args(argv)
    if args.release:
        Throttle().release()
        STATE.unlink(missing_ok=True)
        return
    exit_on_signals()
    desktop = compositor.detect()
    if desktop.command is None:
        print(f"wattif throttle: {compositor.unsupported()} isn't supported (niri and sway are): no windows are "
              'known, so nothing is capped', file=sys.stderr)
    run(Throttle(quota=max(1, min(int(args.quota), 100)), keep=keep_patterns(args.keep), desktop=desktop))


if __name__ == '__main__':
    main()

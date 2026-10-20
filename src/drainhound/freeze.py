#!/usr/bin/env python3
"""Cached-app freeze, after Android's: on battery, an app you chose that hasn't
had focus for AFTER and has no window on a workspace in view is frozen with the
cgroup freezer, so it uses no CPU at all. It thaws the moment you focus it or
switch to its workspace (the compositor's events), when you start it again (a
new scope with its name: a second instance hands its arguments to the running
one and would wait for it), and on AC. Every EVERY it thaws for THAW, so
timers, sync and connections catch up. An app busier than BUSY of a core over
the last BUSY_WINDOW (a build, an export) is left running, and one playing or
recording audio or video counts as in use. Its last focus is the compositor's
(niri's focus_timestamp, so a restart doesn't reset it).

Nothing is frozen, and what was is thawed, unless both event streams are live:
without PipeWire's nothing says which apps play media, and without the
compositor's (none supported, or it died: compositor.py) which are in use, so
every chosen app counts as in use until it is back, and AFTER starts again then.
So does every chosen app while a window that is focused or in view is no app's
that throttle.Session.window_apps can tell (a game Steam started, under niri:
an X11 window whose app id names no app scope): it may be one of theirs.

Android can freeze every cached app because its apps are written to be frozen;
desktop apps aren't. So only the apps you list are frozen: not chat apps (their
connection drops while frozen), and nothing that serves other programs (a dev
server stops answering).

    drainhound freeze [PATTERN ...] [--after MIN] [--every MIN] [--thaw SEC]
    drainhound freeze --release      (thaw what it froze; the unit's ExecStopPost)

PATTERN is matched (case-insensitively) anywhere in the app's scope names in
app.slice (and the slices there), systemd's escapes undone, as drainhound
throttle's --keep is. Without one it freezes the configuration's [freeze]
apps, and with none of those it idles; AFTER, EVERY and THAW are [freeze]
after, every and thaw, and the flags override them all. It follows focus and media from the same event streams as
drainhound throttle (throttle.Session). RUNTIME/freeze.json says what it watches
and what it has frozen.
"""
import argparse
import collections
import json
import signal
import sys
import time
from pathlib import Path

from . import compositor
from . import config
from . import throttle
from .throttle import APPS, RUNTIME, Session, app_scopes, label, on_battery, read, string_list, unescape, unit

AFTER = config.get('freeze', 'after') * 60  # s without focus before an app is frozen.
EVERY = config.get('freeze', 'every') * 60  # s between thaws while frozen.
THAW = config.get('freeze', 'thaw')  # s each thaw lasts.
BUSY, BUSY_WINDOW = 0.25, 5 * 60  # Cores, over s: busier apps are working, not idling.
STATE = RUNTIME / 'freeze.json'


class Freezer(Session):
    state_path = STATE

    def __init__(self, patterns, apps=APPS, battery=on_battery, proc=Path('/proc'), clock=time.monotonic,
                 after=AFTER, every=EVERY, thaw=THAW, busy=BUSY, desktop=None):
        super().__init__(apps, battery, proc, clock, desktop)
        self.patterns = tuple(p.lower() for p in string_list(patterns, 'patterns'))
        self.after, self.every, self.thaw, self.busy = after, every, thaw, busy
        # By app: last in use; when frozen (this time); when a periodic thaw began; (time, CPU µs) samples.
        self.used, self.frozen, self.thawed, self.usage, self.was_busy = {}, {}, {}, {}, {}
        self.scopes = None  # The scopes seen last time (None before the first look).
        self.mine = set()  # Scopes this froze (a drainhound experiment freeze is left alone).

    def chosen(self, scope):
        name = unescape(unit(scope)).lower()
        return any(pattern in name for pattern in self.patterns)

    def cpu_seconds(self, scopes):
        total = 0.0
        for scope in scopes:
            for line in (read(self.apps / scope / 'cpu.stat') or '').splitlines():
                key, _, value = line.partition(' ')
                if key == 'usage_usec':
                    total += int(value) / 1e6
        return total

    def busy_now(self, app, scopes, now):
        """Whether the app used more than BUSY of a core over the last BUSY_WINDOW."""
        history = self.usage.setdefault(app, collections.deque())
        if history and now - history[-1][0] < 1:  # Event bursts: one sample a second is plenty.
            return self.was_busy.get(app, False)
        history.append((now, self.cpu_seconds(scopes)))
        while len(history) > 2 and now - history[1][0] >= BUSY_WINDOW:
            history.popleft()
        (t0, used0), (t1, used1) = history[0], history[-1]
        self.was_busy[app] = t1 - t0 >= 60 and (used1 - used0) / (t1 - t0) > self.busy
        return self.was_busy[app]

    def wanted(self):
        """{scope: freeze?} for the scopes of the apps chosen."""
        now, battery = self.clock(), self.battery()
        scopes = self.look()
        new = set(scopes) - self.scopes if self.scopes is not None else set()
        self.scopes = set(scopes)
        apps = collections.defaultdict(list)
        for scope in scopes:
            apps[self.app[scope]].append(scope)
        chosen = {app: own for app, own in apps.items() if any(self.chosen(s) for s in own)}
        # Without the compositor's state nothing says what is in use: all of it might be. Nor while a window
        # in view is no app's that can be told: it might be any chosen one's.
        if self.desktop.known and not self.unknown_in_view():
            in_use = self.playing() | self.shown() | self.focused_apps()
        else:
            in_use = set(chosen)
        focused = self.last_focused()
        values = {}
        for app, own in chosen.items():
            names = {label(s).lower() for s in own}
            # A new scope of its name and of another launch (its pid or random id) than the app's own.
            launches = {instance(s) for s in own if s not in new}
            relaunched = any(label(s).lower() in names and instance(s) not in launches for s in new)
            if app in in_use or relaunched:
                self.used[app] = now
            elif app not in self.used:  # First seen: when it last had focus, if niri knows.
                self.used[app] = min(now, focused.get(app, now))
            busy = self.busy_now(app, own, now)
            if not battery or busy or now - self.used[app] < self.after:
                self.frozen.pop(app, None)
                self.thawed.pop(app, None)
                freeze = False
            elif app in self.thawed:  # A periodic thaw: over after THAW.
                freeze = now - self.thawed[app] >= self.thaw
                if freeze:
                    del self.thawed[app]
                    self.frozen[app] = now
            elif app in self.frozen and now - self.frozen[app] >= self.every:
                del self.frozen[app]
                self.thawed[app] = now
                freeze = False
            else:
                self.frozen.setdefault(app, now)
                freeze = True
            values.update(dict.fromkeys(own, freeze))
        for gone in set(self.used) - set(chosen):
            for state in (self.used, self.frozen, self.thawed, self.usage, self.was_busy):
                state.pop(gone, None)
        return values

    def apply(self, values=None):
        """Freeze and thaw what changed; the scopes now frozen by this. The
        state file names a scope before it is frozen, and a scope is in
        self.mine (what release() thaws) before it is, so a run killed or
        stopped in between leaves nothing frozen that --release doesn't know."""
        values = self.wanted() if values is None else values
        current = {scope: read(self.apps / scope / 'cgroup.freeze') for scope in values}
        freezing = {scope for scope, freeze in values.items() if freeze and current[scope] == '0'}
        if not freezing <= set((self.written or {}).get('scopes') or ()):
            self.written = {**(self.written or {}), 'scopes': sorted(self.mine | freezing)}
            throttle.write_state(self.written, self.state_path)
        for scope, freeze in values.items():
            path = self.apps / scope / 'cgroup.freeze'
            try:
                if scope in freezing:
                    self.mine.add(scope)
                    path.write_text('1')
                elif not freeze and scope in self.mine:
                    if current[scope] == '1':
                        path.write_text('0')
                    self.mine.discard(scope)
            except OSError:
                self.mine.discard(scope)  # The app exited meanwhile.
        self.mine &= set(app_scopes(self.apps))
        return sorted(self.mine)

    def release(self):
        self.apply(dict.fromkeys(self.mine, False))

    def step(self, media_known):
        """Apply what is wanted now; the state file's contents. Without stream
        data nothing stays frozen: without PipeWire's an app might be wanted for
        media, without the compositor's it might be the one you are using."""
        live = media_known and self.desktop.known
        frozen = self.apply() if live else self.apply(dict.fromkeys(self.wanted(), False))
        return {'on_battery': self.battery(), 'scopes': frozen,  # An app's scopes can differ in case: one name.
                'frozen': sorted({label(s).lower() for s in frozen}),
                'watching': sorted({label(s).lower() for s in self.scopes or () if self.chosen(s)})}


def instance(scope):
    """Which launch a scope is of: its name's trailing pid or random id."""
    return unit(scope).rpartition('-')[2]


def release_left(state=STATE, apps=APPS):
    """Thaw the scopes a previous run's state file says it froze (after a crash)."""
    try:
        scopes = json.loads(Path(state).read_text()).get('scopes') or []
    except (OSError, ValueError, AttributeError):
        scopes = []
    for scope in scopes:
        if not (isinstance(scope, str) and scope.endswith('.scope') and '..' not in scope.split('/')):
            continue  # Not one this wrote.
        try:
            if read(apps / scope / 'cgroup.freeze') == '1':
                (apps / scope / 'cgroup.freeze').write_text('0')
        except OSError:
            pass
    Path(state).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='drainhound freeze', description=__doc__.split('\n\n')[0])
    parser.add_argument('patterns', nargs='*', metavar='PATTERN',
                        help='apps to freeze, matched in scope names (default: [freeze] apps)')
    parser.add_argument('--after', type=float, default=AFTER / 60, help='minutes without focus before freezing')
    parser.add_argument('--every', type=float, default=EVERY / 60, help='minutes between thaws while frozen')
    parser.add_argument('--thaw', type=float, default=THAW, help='seconds each thaw lasts')
    parser.add_argument('--release', action='store_true', help='thaw what a previous run froze and exit')
    args = parser.parse_args(argv)
    if args.release:
        release_left()
        return
    release_left()  # A crashed run's freezes: start from thawed.
    throttle.exit_on_signals()  # Each thaws what it froze on the way out.
    patterns = string_list(args.patterns, 'PATTERN') or string_list(config.get('freeze', 'apps'), '[freeze] apps')
    if not patterns:
        # A unit that runs it unconfigured stays up, doing nothing, instead of failing and restarting.
        print('drainhound freeze: no apps to freeze (name them, or set [freeze] apps in the configuration); idling',
              file=sys.stderr)
        while True:
            signal.pause()
    desktop = compositor.detect()
    if desktop.command is None:
        print(f"drainhound freeze: {compositor.unsupported()} isn't supported (niri and sway are): nothing says which "
              'apps are in use, so nothing is frozen', file=sys.stderr)
    throttle.run(Freezer(patterns, after=args.after * 60, every=args.every * 60, thaw=args.thaw, desktop=desktop))


if __name__ == '__main__':
    main()

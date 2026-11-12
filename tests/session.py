"""A desktop of its own for the session tests: a headless sway (no GPU or screen)
showing one window, niri nested in it, and waybar and drainhound as niri's
clients, all on a private runtime directory and D-Bus. Nothing reaches your own
session: not its compositor, bus, systemd (`systemctl --user` finds none, so the
panel runs itself) or drainhound's data.

    with Session(drainhound='/nix/store/…-drainhound/bin/drainhound') as s:
        s.spawn('panel', s.drainhound, 'panel', 'serve', '--open')
        s.wait(lambda: 'drainhound-panel' in s.layers())
        s.screenshot('panel')

Needs sway, niri, dbus-run-session and grim on PATH (waybar and foot for the
tests that use them). Screenshots go to DRAINHOUND_SESSION_SHOTS if it is set.
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

TOOLS = ('sway', 'niri', 'dbus-run-session', 'grim')
SIZE = (1600, 1000)  # A laptop's screen at 1.5×, with room for the whole panel.
# sway only holds niri's window, full screen; niri is the desktop under test.
SWAY = """\
output * mode --custom {0}x{1} position 0 0
default_border none
exec niri -c {2}
for_window [all] fullscreen enable
"""
NIRI = """\
hotkey-overlay { skip-at-startup; }
prefer-no-csd
animations { off; }
"""


def missing(*tools):
    return [tool for tool in (*TOOLS, *tools) if shutil.which(tool) is None]


def session_conf():
    """dbus's own session.conf: a system's /etc one may only include others."""
    daemon = shutil.which('dbus-daemon') or shutil.which('dbus-run-session') or 'dbus-run-session'
    return Path(os.path.realpath(daemon)).parents[1] / 'share/dbus-1/session.conf'


class Session:
    def __init__(self, drainhound='drainhound', timeout=20):
        self.drainhound = drainhound
        self.timeout = timeout
        self.processes = []
        self.shots = os.environ.get('DRAINHOUND_SESSION_SHOTS')
        self.last = b''  # The frame settle() last waited for.

    def __enter__(self):
        # Wayland sockets live in XDG_RUNTIME_DIR and their paths have 108 bytes: keep it short.
        self.root = Path(tempfile.mkdtemp(prefix='dh-', dir=os.environ.get('XDG_RUNTIME_DIR')))
        self.runtime = self.root / 'run'
        self.runtime.mkdir(mode=0o700)
        (self.root / 'config/niri').mkdir(parents=True)
        (self.root / 'config/niri/config.kdl').write_text(NIRI)
        (self.root / 'sway').write_text(SWAY.format(*SIZE, self.root / 'config/niri/config.kdl'))
        base = {k: v for k, v in os.environ.items()
                if k not in ('WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'DISPLAY', 'NIRI_SOCKET', 'SWAYSOCK',
                             'DBUS_SESSION_BUS_ADDRESS', 'LD_PRELOAD', 'HYPRLAND_INSTANCE_SIGNATURE')}
        base.update(XDG_RUNTIME_DIR=str(self.runtime), XDG_CONFIG_HOME=str(self.root / 'config'),
                    XDG_CACHE_HOME=str(self.root / 'cache'), XDG_CURRENT_DESKTOP='niri',
                    DRAINHOUND_STATE_DIR=str(self.root / 'state'), DRAINHOUND_RUNTIME_DIR=str(self.root / 'drainhound'),
                    DRAINHOUND_CONFIG='', GDK_DEBUG='no-portals', GTK_A11Y='none', NO_AT_BRIDGE='1')
        address = self.root / 'bus'
        script = f'echo "$DBUS_SESSION_BUS_ADDRESS" > {address}; exec sway -c {self.root}/sway'
        self.compositor = self.spawn('session', 'dbus-run-session', f'--config-file={session_conf()}', '--',
                                     'sh', '-c', script, env=dict(base, WLR_BACKENDS='headless',
                                                                  WLR_LIBINPUT_NO_DEVICES='1'))
        try:
            self.wait(lambda: list(self.runtime.glob('niri.*.sock')) and address.exists(), what='niri')
            socket = next(self.runtime.glob('niri.*.sock'))  # niri.wayland-1.PID.sock
            self.env = dict(base, WAYLAND_DISPLAY=socket.name.split('.')[1], NIRI_SOCKET=str(socket),
                            DBUS_SESSION_BUS_ADDRESS=address.read_text().strip())
            self.wait(lambda: self.niri('outputs'), what="niri's output")
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        for process in reversed(self.processes):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in self.processes:
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        shutil.rmtree(self.root, ignore_errors=True)

    def log(self, name):
        return (self.root / f'{name}.log').read_text(errors='replace')

    def spawn(self, name, *argv, env=None):
        """A process in its own group (stopped with everything it started), its
        output in name.log."""
        with open(self.root / f'{name}.log', 'ab') as out:
            process = subprocess.Popen(argv, env=env or self.env, stdin=subprocess.DEVNULL, stdout=out,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(process)
        return process

    def run(self, *argv, timeout=None):
        return subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=timeout or self.timeout)

    def niri(self, *request):
        """`niri msg -j REQUEST`'s reply, or None."""
        done = self.run('niri', 'msg', '-j', *request)
        return json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else None

    def layers(self):
        return {layer['namespace'] for layer in self.niri('layers') or ()}

    def wait(self, ready, timeout=None, what='it'):
        """Seconds until ready() is true; AssertionError at the timeout, or if the session ended."""
        start = time.monotonic()
        while not ready():
            if time.monotonic() - start > (timeout or self.timeout):
                raise AssertionError(f'{what} not ready after {timeout or self.timeout} s')
            if self.compositor.poll() is not None:
                raise AssertionError(f'the session ended ({self.compositor.returncode}):\n{self.log("session")[-3000:]}')
            time.sleep(0.02)
        return time.monotonic() - start

    def frame(self):
        return subprocess.run(['grim', '-'], env=self.env, capture_output=True).stdout

    def settle(self, unlike=None, timeout=None, still=1.0):
        """Seconds until the screen stops changing (two frames `still` s apart are
        the same: a panel done loading, its spinner gone), as some other frame than
        `unlike` (the screen before: a window mapped but not yet drawn is still too).
        The frame is then self.last."""
        frames: list[bytes | None] = [None]

        def same():
            frames.append(self.frame())
            if frames[-2] == frames[-1] != unlike:
                self.last = frames[-1]
                return True
            del frames[0]
            time.sleep(still)
        return self.wait(same, timeout, 'a still screen')

    def screenshot(self, name):
        """The whole output as a PNG: in DRAINHOUND_SESSION_SHOTS if set, else this session's directory."""
        path = Path(self.shots or self.root) / f'{name}.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        done = self.run('grim', str(path))
        if done.returncode != 0:
            raise AssertionError(f'grim: {done.stderr}')
        return path

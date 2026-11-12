"""drainhound in a real desktop (tests/session.py: niri nested in a headless sway):
waybar running its modules, the panel opening, closing and switching tabs, and
niri's event stream as compositor.Niri reads it. A synthetic month of data
(tools/demo.py) fills the panel; the battery is this machine's, or none.

They start a compositor, so they run only when asked: DRAINHOUND_SESSION names
the drainhound to run (the flake's session check sets it to the package):

    nix build && DRAINHOUND_SESSION=$PWD/result/bin/drainhound \\
        DRAINHOUND_SESSION_SHOTS=/tmp/shots python -m unittest tests.test_session

or in the sandbox, its screenshots the output: nix build .#checks.x86_64-linux.session

DRAINHOUND_SESSION_SHOTS keeps each test's screenshots there to look at.
"""
import contextlib
import io
import json
import os
import queue
import subprocess
import threading
import time
import unittest
from pathlib import Path

from drainhound import compositor
from tests import session
from tools import demo

DRAINHOUND = os.environ.get('DRAINHOUND_SESSION', '')
MISSING = session.missing('waybar', 'foot')
CONTRIB = Path(__file__).resolve().parents[1] / 'contrib/waybar'
PANEL = 'drainhound-panel'  # Its layer surface's namespace.
WARM = 2.0  # Seconds a running panel may take to open; it takes about 0.1 s.


def lines(process):
    """A process's output lines as they come, on a queue (None at its end)."""
    out = queue.Queue()

    def read():
        for line in process.stdout:
            out.put(line)
        out.put(None)
    threading.Thread(target=read, daemon=True).start()
    return out


def stop(process):
    process.kill()
    process.wait()
    process.stdout.close()


@unittest.skipUnless(DRAINHOUND, 'set DRAINHOUND_SESSION to a drainhound to run (see the module docstring)')
class SessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if MISSING:  # Asked for, so not skipped.
            raise AssertionError(f'the session tests need {", ".join(MISSING)} on PATH')
        cls.s = session.Session(DRAINHOUND).__enter__()
        cls.addClassCleanup(cls.s.__exit__)
        # The bar's modules run `drainhound`: this one.
        cls.s.env['PATH'] = f'{os.path.dirname(DRAINHOUND)}:{cls.s.env["PATH"]}'
        with contextlib.redirect_stdout(io.StringIO()):
            demo.main(cls.s.root)
        cls.s.spawn('eta', DRAINHOUND, 'eta', '--quiet')

    def shot(self, name, unlike=None):
        """A screenshot once the screen is still and not `unlike` (DRAINHOUND_SESSION_SHOTS
        keeps it); the seconds that took."""
        took = self.s.settle(unlike, timeout=60)  # Training models at first, eta slows it.
        self.s.screenshot(f'{self.id().rsplit(".", 1)[-1]}-{name}')
        return took

    def test_waybar_runs_the_modules(self):
        bar = self.s.root / 'config/waybar'
        bar.mkdir(parents=True, exist_ok=True)
        (bar / 'config.jsonc').write_text(json.dumps({
            'layer': 'top', 'position': 'top', 'include': [str(CONTRIB / 'config.jsonc')],
            'modules-right': ['custom/drainhound', 'custom/vote-up', 'custom/vote-down']}))
        (bar / 'style.css').write_text('window#waybar { background: #222; color: #eee; }\n'
                                       + (CONTRIB / 'style.css').read_text())
        self.s.spawn('waybar', 'waybar')
        self.s.wait(lambda: 'waybar' in self.s.layers(), what='waybar')
        # eta publishes within a few seconds of starting; until then the label is '--'.
        battery = subprocess.Popen([DRAINHOUND, 'waybar', 'battery'], env=self.s.env, stdout=subprocess.PIPE,
                                   text=True, start_new_session=True)
        self.addCleanup(stop, battery)
        out = lines(battery)
        label = json.loads(out.get(timeout=self.s.timeout))
        while label['text'] == '--':
            label = json.loads(out.get(timeout=self.s.timeout))
        self.assertTrue({'charging', 'discharging', 'plugged', 'weak-charger', 'no-battery'} & set(label['class']),
                        label)
        for vote in ('up', 'down'):
            self.assertIn('text', json.loads(self.s.run(DRAINHOUND, 'waybar', vote).stdout))
        time.sleep(2)  # The bar's own `drainhound waybar battery` sees the label too.
        self.shot('bar')
        errors = [line for line in self.s.log('waybar').splitlines() if '[error]' in line]
        self.assertEqual(errors, [])

    def test_the_panel_opens_closes_and_shows_a_tab(self):
        before = self.s.frame()
        start = time.monotonic()
        self.s.spawn('panel', DRAINHOUND, 'panel', 'serve', '--open')
        self.s.wait(lambda: PANEL in self.s.layers(), what='the panel')
        cold = time.monotonic() - start
        loaded = cold + self.shot('timeline', unlike=before)  # Its charts load in the background.
        timeline = self.s.last
        for shown in (False, True, False, True):
            toggled = time.monotonic()
            self.assertEqual(self.s.run(DRAINHOUND, 'panel').returncode, 0)
            took = self.s.wait(lambda shown=shown: (PANEL in self.s.layers()) == shown, what=f'the panel {shown=}')
            self.assertLess(time.monotonic() - toggled, WARM, f'toggling took {took:.2f} s')
        self.assertEqual(self.s.run(DRAINHOUND, 'panel', 'experiments').returncode, 0)
        self.shot('experiments', unlike=timeline)
        self.assertIn(PANEL, self.s.layers())
        self.s.run(DRAINHOUND, 'panel')  # Closed, out of the other tests' screenshots.
        self.s.wait(lambda: PANEL not in self.s.layers(), what='the panel closed')
        print(f'\n  panel: open {cold:.1f} s after starting, loaded {loaded:.1f} s', end=' ')
        errors = [line for line in self.s.log('panel').splitlines()
                  if 'Traceback' in line or 'CRITICAL' in line or 'Error' in line]
        self.assertEqual(errors, [], self.s.log('panel')[-3000:])

    def test_niris_event_stream_as_compositor_reads_it(self):
        stream = subprocess.Popen(compositor.Niri.command, env=self.s.env, stdout=subprocess.PIPE, text=True,
                                  start_new_session=True)
        self.addCleanup(stop, stream)
        out = lines(stream)
        desktop = compositor.Niri()

        def read(until):
            deadline = time.monotonic() + self.s.timeout
            while not until():
                line = out.get(timeout=max(0.1, deadline - time.monotonic()))
                self.assertIsNotNone(line, 'the event stream ended')
                desktop.event(json.loads(line))

        read(lambda: desktop.known)
        before = set(desktop.windows)
        self.s.run('niri', 'msg', 'action', 'spawn', '--', 'foot')
        read(lambda: any(w['app_id'] == 'foot' for w in desktop.windows.values()))
        window = next(i for i, w in desktop.windows.items() if w['app_id'] == 'foot' and i not in before)
        niri = next(w for w in self.s.niri('windows') or () if w['id'] == window)
        read(lambda: desktop.focused == window)
        self.assertEqual(desktop.pid(window), niri['pid'])
        self.assertTrue(desktop.shown(window))
        self.assertIsNotNone(desktop.windows[window]['focus'])
        self.shot('foot')
        self.s.run('niri', 'msg', 'action', 'close-window', '--id', str(window))
        read(lambda: window not in desktop.windows)


@unittest.skipUnless(DRAINHOUND, 'set DRAINHOUND_SESSION to a drainhound to run')
class NoDisplayTest(unittest.TestCase):
    def test_the_panel_fails_without_a_display_instead_of_waiting(self):
        """So its unit's Restart=on-failure tries again once there is one."""
        env = {k: v for k, v in os.environ.items() if k not in ('WAYLAND_DISPLAY', 'DISPLAY', 'WAYLAND_SOCKET')}
        env.update(WAYLAND_DISPLAY='drainhound-no-such-display', DBUS_SESSION_BUS_ADDRESS='disabled:')
        done = subprocess.run([DRAINHOUND, 'panel', 'serve'], env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('no display', done.stderr)


if __name__ == '__main__':
    unittest.main()

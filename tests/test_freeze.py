import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from drainhound import compositor, config, freeze, throttle
from drainhound.freeze import AFTER, EVERY, THAW, Freezer


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.apps, self.proc, self.on_battery = root / 'app.slice', root / 'proc', True
        # Obsidian's two scopes share its launch pid, as every Electron app's here do.
        for scope, pid in (('app-md.Obsidian-2243.scope', 2243), ('app-niri-obsidian-2243.scope', 2250),
                           ('app-niri-floorp-2240.scope', 2240), ('app-niri-steam-3000.scope', 3000)):
            self.scope(scope, pid)
        self.now = 10_000.0
        self.freezer = Freezer(['obsidian', 'steam'], self.apps, battery=lambda: self.on_battery, proc=self.proc,
                               clock=lambda: self.now)
        self.freezer.window_event({'WorkspacesChanged': {'workspaces': [
            {'id': 1, 'output': 'eDP-1', 'is_active': True}, {'id': 2, 'output': 'eDP-1', 'is_active': False}]}})
        self.freezer.window_event({'WindowsChanged': {'windows': [
            {'id': 1, 'pid': 2240, 'workspace_id': 1, 'is_focused': True},
            {'id': 2, 'pid': 2243, 'workspace_id': 2, 'is_focused': False},
            {'id': 3, 'pid': 3000, 'workspace_id': 2, 'is_focused': False}]}})

    def tearDown(self):
        self.tmp.cleanup()

    def scope(self, scope, pid, cgroup=None):
        (self.apps / scope).mkdir(parents=True)
        (self.apps / scope / 'cgroup.freeze').write_text('0\n')
        (self.apps / scope / 'cpu.stat').write_text('usage_usec 0\n')
        (self.proc / str(pid)).mkdir(parents=True)
        (self.proc / str(pid) / 'cgroup').write_text(
            f'0::{cgroup or "/user.slice/user-1000.slice/user@1000.service/app.slice/" + scope}\n')

    def frozen(self):
        return {p.parent.relative_to(self.apps).as_posix() for p in self.apps.rglob('cgroup.freeze')
                if p.read_text().strip() == '1'}

    def wait(self, seconds, step=5):
        """Advance the clock, stepping the freezer as run() does every RESCAN."""
        end = self.now + seconds
        while self.now < end:
            self.now = min(end, self.now + step)
            self.freezer.step(True)

    OBSIDIAN = {'app-md.Obsidian-2243.scope', 'app-niri-obsidian-2243.scope'}

    def test_a_chosen_app_unused_for_a_while_freezes_and_focus_thaws_it(self):
        self.freezer.step(True)
        self.assertEqual(self.frozen(), set())  # Just seen: not before AFTER.
        self.wait(AFTER)
        self.assertEqual(self.frozen(), self.OBSIDIAN | {'app-niri-steam-3000.scope'})  # Both scopes; not Floorp
        state = self.freezer.step(True)
        self.assertEqual((state['frozen'], state['watching']), (['obsidian', 'steam'], ['obsidian', 'steam']))
        self.freezer.window_event({'WindowFocusChanged': {'id': 2}})
        self.freezer.step(True)
        self.assertEqual(self.frozen(), {'app-niri-steam-3000.scope'})  # At once
        self.freezer.window_event({'WindowFocusChanged': {'id': 1}})
        self.wait(AFTER - 10)
        self.assertNotIn('app-md.Obsidian-2243.scope', self.frozen())  # Its clock started again

    def test_apps_on_a_workspace_in_view_stay_running(self):
        self.freezer.window_event({'WorkspaceActivated': {'id': 2, 'focused': True}})
        self.wait(AFTER + 60)
        self.assertEqual(self.frozen(), set())
        self.freezer.window_event({'WorkspaceActivated': {'id': 1, 'focused': True}})  # Switched away
        self.wait(AFTER - 10)
        self.assertEqual(self.frozen(), set())
        self.wait(20)
        self.assertEqual(self.frozen(), self.OBSIDIAN | {'app-niri-steam-3000.scope'})

    def test_niris_focus_times_carry_over_a_restart(self):
        self.freezer.window_event({'WindowOpenedOrChanged': {'window': {
            'id': 2, 'pid': 2243, 'workspace_id': 2, 'is_focused': False,
            'focus_timestamp': {'secs': int(self.now - AFTER - 1), 'nanos': 0}}}})
        self.freezer.step(True)
        self.assertEqual(self.frozen(), self.OBSIDIAN)  # Unfocused for AFTER already

    def test_frozen_apps_thaw_now_and_then_to_catch_up(self):
        self.freezer.step(True)
        self.wait(AFTER)
        self.wait(EVERY - 5)
        self.assertTrue(self.OBSIDIAN <= self.frozen())
        self.wait(5)
        self.assertFalse(self.OBSIDIAN & self.frozen())  # A thaw
        self.wait(THAW)
        self.assertTrue(self.OBSIDIAN <= self.frozen())  # Over

    def test_busy_apps_media_and_ac_keep_apps_running(self):
        stat = self.apps / 'app-niri-steam-3000.scope' / 'cpu.stat'
        for _ in range((AFTER + 120) // 5):  # Half a core: a download unpacking, say.
            self.now += 5
            stat.write_text(f'usage_usec {int((self.now - 10_000) * 0.5e6)}\n')
            self.freezer.step(True)
        self.assertNotIn('app-niri-steam-3000.scope', self.frozen())
        self.assertTrue(self.OBSIDIAN <= self.frozen())
        self.freezer.media_event([{'id': 7, 'type': 'PipeWire:Interface:Node', 'info': {
            'state': 'running', 'props': {'media.class': 'Stream/Output/Audio', 'application.process.id': 2250}}}])
        self.freezer.step(True)
        self.assertFalse(self.OBSIDIAN & self.frozen())  # Playing: in use
        self.freezer.media_event([{'id': 7, 'info': None}])
        self.wait(AFTER)
        self.assertTrue(self.OBSIDIAN <= self.frozen())
        self.on_battery = False
        self.freezer.step(True)
        self.assertEqual(self.frozen(), set())
        self.on_battery = True
        self.freezer.step(False)  # PipeWire down: nothing stays frozen
        self.assertEqual(self.frozen(), set())

    def test_starting_a_frozen_app_again_thaws_it(self):
        self.freezer.step(True)
        self.wait(AFTER)
        self.assertTrue(self.OBSIDIAN <= self.frozen())
        self.scope('app-niri-obsidian-4000.scope', 4000)  # `obsidian obsidian://open?…` from another app
        self.freezer.step(True)
        self.assertFalse(self.OBSIDIAN & self.frozen())
        self.wait(AFTER)
        self.assertTrue(self.OBSIDIAN <= self.frozen())
        # Started by `uwsm app` or app2unit: a scope with a random id, which joins Obsidian's by its name, is a
        # new launch too.
        self.scope('app-graphical.slice/app-niri-obsidian-1a2b3c4d.scope', 4001)
        self.freezer.step(True)
        self.assertFalse(self.OBSIDIAN & self.frozen())
        self.assertEqual(self.freezer.app['app-graphical.slice/app-niri-obsidian-1a2b3c4d.scope'], '2243')

    def test_only_its_own_freezes_are_undone(self):
        (self.apps / 'app-niri-floorp-2240.scope' / 'cgroup.freeze').write_text('1\n')  # A drainhound experiment block
        self.wait(AFTER)
        self.freezer.release()
        self.assertEqual(self.frozen(), {'app-niri-floorp-2240.scope'})
        self.wait(5)  # Frozen again; then a crash leaves them so, and the next start thaws them.
        state = self.apps.parent / 'freeze.json'
        state.write_text(json.dumps(self.freezer.step(True)))
        freeze.release_left(state, self.apps)
        self.assertEqual(self.frozen(), {'app-niri-floorp-2240.scope'})
        self.assertFalse(state.exists())

    def test_without_the_compositors_state_nothing_is_frozen_and_what_was_thaws(self):
        self.freezer.step(True)
        self.wait(AFTER)
        self.assertEqual(self.frozen(), self.OBSIDIAN | {'app-niri-steam-3000.scope'})
        self.freezer.desktop.clear()  # The stream ended: niri exited, or restarts.
        state = self.freezer.step(True)
        self.assertEqual((self.frozen(), state['frozen']), (set(), []))
        self.wait(AFTER + 60)  # However long it stays away: nothing says the apps aren't in use.
        self.assertEqual(self.frozen(), set())
        for event in ({'WorkspacesChanged': {'workspaces': [{'id': 1, 'output': 'eDP-1', 'is_active': True},
                                                            {'id': 2, 'output': 'eDP-1', 'is_active': False}]}},
                      {'WindowsChanged': {'windows': [{'id': 2, 'pid': 2243, 'workspace_id': 2, 'is_focused': False},
                                                      {'id': 3, 'pid': 3000, 'workspace_id': 2,
                                                       'is_focused': False}]}}):
            self.freezer.window_event(event)
        self.wait(AFTER - 10)
        self.assertEqual(self.frozen(), set())  # Back: AFTER starts again from when it was.
        self.wait(20)
        self.assertEqual(self.frozen(), self.OBSIDIAN | {'app-niri-steam-3000.scope'})

    def test_without_a_compositor_to_follow_nothing_is_ever_frozen(self):
        blind = Freezer(['obsidian', 'steam'], self.apps, battery=lambda: True, proc=self.proc,
                        clock=lambda: self.now, desktop=compositor.Desktop())
        for _ in range(3):
            self.now += AFTER
            self.assertEqual(blind.step(True)['frozen'], [])
        self.assertEqual(self.frozen(), set())

    SATELLITE = '/user.slice/user-1000.slice/user@1000.service/session.slice/wayland-wm@niri.service'

    def x11(self, *windows):
        """X11 windows under niri: xwayland-satellite's pid (2649, outside every app scope), the X11 class as app id."""
        if not (self.proc / '2649').exists():
            (self.proc / '2649').mkdir()
            (self.proc / '2649' / 'cgroup').write_text(f'0::{self.SATELLITE}\n')
        for window in windows:
            self.freezer.window_event({'WindowOpenedOrChanged': {'window': {'pid': 2649, **window}}})

    def test_a_focused_x11_app_under_niri_is_in_use(self):
        # The review's case: Steam focused and on screen, frozen 30 min on as niri said xwayland-satellite's pid.
        self.freezer.window_event({'WindowClosed': {'id': 3}})
        self.x11({'id': 7, 'app_id': 'steam', 'workspace_id': 1, 'is_focused': True,
                  'focus_timestamp': {'secs': int(self.now - 1), 'nanos': 0}})
        self.freezer.step(True)
        self.wait(AFTER + 60)
        self.assertEqual(self.frozen(), self.OBSIDIAN)
        self.freezer.window_event({'WindowFocusChanged': {'id': 1}})
        self.freezer.window_event({'WorkspaceActivated': {'id': 2, 'focused': True}})  # Steam's window out of view.
        self.wait(AFTER - 10)
        self.assertNotIn('app-niri-steam-3000.scope', self.frozen())
        self.wait(20)
        self.assertIn('app-niri-steam-3000.scope', self.frozen())

    def test_steam_started_by_uwsm_is_seen_and_its_x11_window_is_its(self):
        # As on a Framework 13 under niri and uwsm: Steam's scope nests, with a random id; its windows are X11.
        steam = 'app-graphical.slice/app-niri-steam-140a4d35.scope'
        self.scope(steam, 4100)
        self.freezer.window_event({'WindowClosed': {'id': 3}})
        self.x11({'id': 7, 'app_id': 'steam', 'workspace_id': 1, 'is_focused': True})
        self.freezer.step(True)
        self.assertIn('steam', self.freezer.step(True)['watching'])
        self.wait(AFTER + 60)
        self.assertNotIn(steam, self.frozen())  # Focused.
        self.freezer.window_event({'WindowFocusChanged': {'id': 1}})
        self.freezer.window_event({'WorkspaceActivated': {'id': 2, 'focused': True}})
        self.wait(AFTER + 5)
        self.assertIn(steam, self.frozen())

    def test_while_a_window_in_view_is_no_known_apps_nothing_is_frozen(self):
        # A game Steam started (its class names no scope): it may be any chosen app's, so all are in use.
        self.x11({'id': 8, 'app_id': 'steam_app_1145360', 'workspace_id': 1, 'is_focused': True})
        self.wait(AFTER + 60)
        self.assertEqual(self.frozen(), set())
        self.freezer.window_event({'WindowFocusChanged': {'id': 1}})  # Not focused, still shown: the same.
        self.wait(60)
        self.assertEqual(self.frozen(), set())
        self.freezer.window_event({'WindowClosed': {'id': 8}})  # The game ends: AFTER from then.
        self.wait(AFTER - 10)
        self.assertEqual(self.frozen(), set())
        self.wait(20)
        self.assertEqual(self.frozen(), self.OBSIDIAN | {'app-niri-steam-3000.scope'})
        self.x11({'id': 8, 'app_id': 'steam_app_1145360', 'workspace_id': 2, 'is_focused': False})
        self.freezer.step(True)  # Out of view, it says nothing.
        self.assertEqual(self.frozen(), self.OBSIDIAN | {'app-niri-steam-3000.scope'})

    def test_apps_in_nested_slices_are_frozen_with_all_their_scopes(self):
        # A Flatpak Vesktop's two sandboxes (its zygote has one of its own), as uwsm/app2unit/Flatpak nest them.
        nested = {'app-graphical.slice/app-flatpak-dev.vencord.Vesktop-464058271.scope',
                  'app-graphical.slice/app-flatpak-dev.vencord.Vesktop-900100968.scope'}
        for scope, pid in zip(sorted(nested), (667715, 667740)):
            self.scope(scope, pid)
        self.freezer.patterns = ('vesktop',)
        self.freezer.window_event({'WindowOpenedOrChanged': {'window': {
            'id': 30, 'pid': 667715, 'workspace_id': 1, 'is_focused': True}}})
        self.wait(AFTER + 60)
        self.assertEqual(self.frozen(), set())  # Focused: neither sandbox freezes.
        self.freezer.window_event({'WindowFocusChanged': {'id': 1}})
        self.freezer.window_event({'WindowOpenedOrChanged': {'window': {
            'id': 30, 'pid': 667715, 'workspace_id': 2, 'is_focused': False}}})
        self.wait(AFTER + 5)
        self.assertEqual(self.frozen(), nested)
        state = self.freezer.state_path
        self.assertEqual(json.loads(state.read_text())['scopes'], sorted(nested))
        freeze.release_left(state, self.apps)  # After a crash.
        self.assertEqual(self.frozen(), set())

    def test_the_state_file_names_a_scope_before_it_is_frozen(self):
        self.freezer.state_path = self.apps.parent / 'freeze.json'
        listed = []
        write = Path.write_text

        def watched(path, text, *args, **kwargs):
            if path.name == 'cgroup.freeze' and text == '1':
                listed.append(path.parent.name in json.loads(self.freezer.state_path.read_text())['scopes'])
            return write(path, text, *args, **kwargs)
        with mock.patch.object(Path, 'write_text', watched):
            self.wait(AFTER + 5)
        self.assertEqual(listed, [True] * 3)
        self.assertEqual(self.frozen(), self.OBSIDIAN | {'app-niri-steam-3000.scope'})
        freeze.release_left(self.freezer.state_path, self.apps)
        self.assertEqual(self.frozen(), set())

    def test_a_signal_just_after_a_freeze_still_thaws_it(self):
        # sys.exit from the SIGTERM/SIGHUP handler can come between writing cgroup.freeze and noting it.
        write = Path.write_text

        def interrupted(path, text, *args, **kwargs):
            done = write(path, text, *args, **kwargs)
            if path.name == 'cgroup.freeze' and text == '1':
                raise SystemExit(0)
            return done
        self.freezer.step(True)
        self.now += AFTER
        with mock.patch.object(Path, 'write_text', interrupted), self.assertRaises(SystemExit):
            self.freezer.step(True)
        self.assertEqual(len(self.frozen()), 1)
        self.freezer.release()  # What run()'s finally does.
        self.assertEqual(self.frozen(), set())

    def test_a_lone_string_is_one_app_not_its_letters(self):
        self.scope('app-niri-fcitx5-2259.scope', 2259)
        with mock.patch.object(throttle, 'run') as run, mock.patch.object(freeze.signal, 'signal'), \
                mock.patch.object(freeze, 'release_left'), \
                mock.patch.object(freeze.compositor, 'detect', return_value=compositor.Niri()), \
                mock.patch.dict(config.SETTINGS['freeze'], {'apps': 'steam'}):
            freeze.main([])
            self.assertEqual(run.call_args.args[0].patterns, ('steam',))  # Not s, t, e, a, m: every scope.
        self.assertFalse(Freezer('steam', self.apps).chosen('app-niri-fcitx5-2259.scope'))
        with mock.patch.object(freeze.signal, 'signal'), mock.patch.object(freeze, 'release_left'), \
                mock.patch.dict(config.SETTINGS['freeze'], {'apps': {'steam': True}}), \
                self.assertRaises(SystemExit) as refused:
            freeze.main([])
        self.assertIn('[freeze] apps must be a list', str(refused.exception))

    def test_sighup_is_handled_as_sigterm_is(self):
        with mock.patch.object(throttle, 'run'), mock.patch.object(freeze.signal, 'signal') as handle, \
                mock.patch.object(freeze, 'release_left'), \
                mock.patch.object(freeze.compositor, 'detect', return_value=compositor.Niri()):
            freeze.main(['steam'])
        handled = {call.args[0] for call in handle.call_args_list}
        self.assertLessEqual({signal.SIGTERM, signal.SIGHUP}, handled)

    def test_defaults_come_from_the_configuration_and_flags_override_them(self):
        self.assertEqual((AFTER, EVERY, THAW), (config.DEFAULTS['freeze']['after'] * 60,
                                                config.DEFAULTS['freeze']['every'] * 60,
                                                config.DEFAULTS['freeze']['thaw']))
        with mock.patch.object(throttle, 'run') as run, mock.patch.object(freeze.signal, 'signal'), \
                mock.patch.object(freeze, 'release_left'), \
                mock.patch.object(freeze.compositor, 'detect', return_value=compositor.Niri()), \
                mock.patch.dict(config.SETTINGS['freeze'], {'apps': ['obsidian', 'steam']}):
            freeze.main([])
            agent = run.call_args.args[0]
            self.assertEqual((agent.patterns, agent.after, agent.every, agent.thaw),
                             (('obsidian', 'steam'), AFTER, EVERY, THAW))
            freeze.main(['Krita', '--after', '5', '--thaw', '3'])
            agent = run.call_args.args[0]
            self.assertEqual((agent.patterns, agent.after, agent.thaw), (('krita',), 300, 3))

    def test_with_nothing_to_freeze_it_idles(self):
        with mock.patch.object(throttle, 'run') as run, mock.patch.object(freeze.signal, 'signal'), \
                mock.patch.object(freeze, 'release_left'), \
                mock.patch.object(freeze.signal, 'pause', side_effect=SystemExit(0)) as pause, \
                contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            freeze.main([])  # The default configuration names no apps.
        pause.assert_called_once()
        run.assert_not_called()
        self.assertIn('no apps to freeze', err.getvalue())


class Stop(Exception):
    pass


class RunTests(unittest.TestCase):
    def test_apps_frozen_while_niri_ran_thaw_when_its_stream_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps, proc = Path(tmp) / 'app.slice', Path(tmp) / 'proc'
            scope = apps / 'app-niri-steam-3000.scope'
            scope.mkdir(parents=True)
            (scope / 'cgroup.freeze').write_text('0\n')
            (proc / '3000').mkdir(parents=True)
            (proc / '3000' / 'cgroup').write_text(f'0::/user.slice/app.slice/{scope.name}\n')
            events = [{'WorkspacesChanged': {'workspaces': [{'id': 1, 'output': 'eDP-1', 'is_active': True},
                                                            {'id': 2, 'output': 'eDP-1', 'is_active': False}]}},
                      {'WindowsChanged': {'windows': [{'id': 3, 'pid': 3000, 'workspace_id': 2,
                                                       'is_focused': False}]}}]
            # niri for a moment (its state, then it dies), and PipeWire with no streams.
            text = chr(10).join(map(json.dumps, events))
            niri = (sys.executable, '-c', f'import time; print({text!r}, flush=True); time.sleep(0.6)')
            pw = (sys.executable, '-c', 'import time; print("[]", flush=True); time.sleep(30)')
            seen = []

            class Dies(compositor.Niri):
                command = niri

            class Watched(Freezer):
                state_path = Path(tmp) / 'freeze.json'

                def step(self, media_known):
                    state = super().step(media_known)
                    seen.append((self.desktop.known, (scope / 'cgroup.freeze').read_text().strip()))
                    if ('1' in dict(seen).values() or len(seen) > 100) and not self.desktop.known:
                        raise Stop
                    return state

            agent = Watched(['steam'], apps, battery=lambda: True, proc=proc, after=0, desktop=Dies())
            with mock.patch.object(throttle, 'RESCAN', 0.2), self.assertRaises(Stop):
                throttle.run(agent, pw_command=pw)
        self.assertIn((True, '1'), seen)  # Frozen while niri was there,
        self.assertEqual(seen[-1], (False, '0'))  # thawed once its stream ended.

    def test_a_signal_thaws_what_it_froze_and_a_kill_leaves_it_to_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps, proc, state = Path(tmp) / 'app.slice', Path(tmp) / 'proc', Path(tmp) / 'freeze.json'
            scope = apps / 'app-niri-steam-3000.scope'
            scope.mkdir(parents=True)
            (scope / 'cgroup.freeze').write_text('0\n')
            (proc / '3000').mkdir(parents=True)
            (proc / '3000' / 'cgroup').write_text(f'0::/user.slice/app.slice/{scope.name}\n')
            events = [{'WorkspacesChanged': {'workspaces': [{'id': 1, 'output': 'eDP-1', 'is_active': True},
                                                            {'id': 2, 'output': 'eDP-1', 'is_active': False}]}},
                      {'WindowsChanged': {'windows': [{'id': 3, 'pid': 3000, 'workspace_id': 2,
                                                       'is_focused': False}]}}]
            niri = f'import time; print({chr(10).join(map(json.dumps, events))!r}, flush=True); time.sleep(30)'
            program = f"""
import sys
from pathlib import Path
from drainhound import compositor, freeze, throttle
class Niri(compositor.Niri):
    command = (sys.executable, '-c', {niri!r})
agent = freeze.Freezer(['steam'], Path({str(apps)!r}), battery=lambda: True, proc=Path({str(proc)!r}), after=0,
                       desktop=Niri())
agent.state_path = Path({str(state)!r})
throttle.exit_on_signals()
throttle.run(agent, pw_command=(sys.executable, '-c', 'import time; print("[]", flush=True); time.sleep(30)'))
"""
            flag = scope / 'cgroup.freeze'
            for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
                child = subprocess.Popen([sys.executable, '-c', program],
                                         env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)})
                try:
                    deadline = time.monotonic() + 20
                    while flag.read_text().strip() != '1' and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertEqual(flag.read_text().strip(), '1')
                    child.send_signal(number)
                    code = child.wait(timeout=20)
                finally:
                    child.kill()
                if number == signal.SIGKILL:  # Nothing runs: the state file says what --release thaws.
                    self.assertEqual(json.loads(state.read_text())['scopes'], [scope.name])
                    freeze.release_left(state, apps)
                else:
                    self.assertEqual(code, 0)
                self.assertEqual(flag.read_text().strip(), '0', number)
                self.assertFalse(state.exists())


if __name__ == '__main__':
    unittest.main()

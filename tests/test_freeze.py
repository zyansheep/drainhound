import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wattif import compositor, config, freeze, throttle
from wattif.freeze import AFTER, EVERY, THAW, Freezer


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

    def scope(self, scope, pid):
        (self.apps / scope).mkdir(parents=True)
        (self.apps / scope / 'cgroup.freeze').write_text('0\n')
        (self.apps / scope / 'cpu.stat').write_text('usage_usec 0\n')
        (self.proc / str(pid)).mkdir(parents=True)
        (self.proc / str(pid) / 'cgroup').write_text(f'0::/user.slice/user-1000.slice/user@1000.service/app.slice/{scope}\n')

    def frozen(self):
        return {p.parent.name for p in self.apps.glob('*/cgroup.freeze') if p.read_text().strip() == '1'}

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
        self.wait(AFTER)
        self.scope('app-niri-obsidian-4000.scope', 4000)  # `obsidian obsidian://open?…` from another app
        self.freezer.step(True)
        self.assertFalse(self.OBSIDIAN & self.frozen())

    def test_only_its_own_freezes_are_undone(self):
        (self.apps / 'app-niri-floorp-2240.scope' / 'cgroup.freeze').write_text('1\n')  # A wattif experiment block
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


if __name__ == '__main__':
    unittest.main()

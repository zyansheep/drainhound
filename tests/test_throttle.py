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

from drainhound import compositor, config, throttle
from drainhound.throttle import UNLIMITED, Throttle


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.apps, self.proc, self.on_battery = root / 'app.slice', root / 'proc', True
        # Vesktop, like every Electron app here, has two scopes with its launch pid: its main process (and
        # window) in its own, its renderers (pid 2404) in the one niri started.
        for scope, pid in (('app-niri-floorp-2240.scope', 2240), ('app-md.Obsidian-2243.scope', 2243),
                           ('app-com.t3tools.T3Code-272490.scope', 272490), ('app-niri-fcitx5-2259.scope', 2259),
                           ('app-niri-foot-9.scope', 9), ('app-vesktop-2295.scope', 2295),
                           ('app-niri-vesktop-2295.scope', 2404), ('app-niri-emoji\\x2dpicker-51325.scope', 51325)):
            (self.apps / scope).mkdir(parents=True)
            (self.apps / scope / 'cpu.max').write_text(UNLIMITED + '\n')
            (self.proc / str(pid)).mkdir(parents=True)
            (self.proc / str(pid) / 'cgroup').write_text(
                f'0::/user.slice/user-1000.slice/user@1000.service/app.slice/{scope}\n')
        (self.apps / 'waybar.service').mkdir()  # Services are not apps.
        self.now = 1000.0
        self.throttle = Throttle(self.apps, quota=5, battery=lambda: self.on_battery, proc=self.proc,
                                 clock=lambda: self.now)
        self.throttle.window_event({'WindowsChanged': {'windows': [
            {'id': 1, 'pid': 2240, 'is_focused': False}, {'id': 2, 'pid': 2243, 'is_focused': True},
            {'id': 3, 'pid': 9, 'is_focused': False}, {'id': 4, 'pid': 2295, 'is_focused': False}]}})

    def tearDown(self):
        self.tmp.cleanup()

    def limits(self):
        return {p.parent.name: p.read_text().strip() for p in self.apps.glob('*/cpu.max')}

    def test_everything_but_the_focused_app_and_the_kept_ones_is_capped(self):
        self.assertEqual(throttle.label(self.throttle.apply()[0]), 'floorp')
        limits = self.limits()
        self.assertEqual(limits['app-niri-floorp-2240.scope'], '5000 100000')
        for free in ('app-md.Obsidian-2243.scope', 'app-com.t3tools.T3Code-272490.scope', 'app-niri-fcitx5-2259.scope',
                     'app-niri-foot-9.scope'):  # Focused, kept (agents, the input method, a terminal).
            self.assertEqual(limits[free], UNLIMITED)
        # Focus moves to Discord: it is free at once; Obsidian stays free for the grace period.
        self.throttle.window_event({'WindowFocusChanged': {'id': 4}})
        self.throttle.apply()
        limits = self.limits()
        self.assertEqual((limits['app-vesktop-2295.scope'], limits['app-md.Obsidian-2243.scope']), (UNLIMITED, UNLIMITED))
        self.now += throttle.GRACE + 1
        self.throttle.apply()
        self.assertEqual(self.limits()['app-md.Obsidian-2243.scope'], '5000 100000')

    def test_terminals_and_editors_are_never_capped(self):
        # Builds and agent CLIs run in a terminal's or an editor's scope: capped, they ran 10× slower.
        for scope, pid in (('app-niri-codium-301.scope', 301), ('app-org.wezfurlong.wezterm-302.scope', 302),
                           ('app-com.mitchellh.ghostty-303.scope', 303), ('app-niri-kitty-304.scope', 304),
                           ('app-dev.zed.Zed-305.scope', 305), ('app-niri-code-306.scope', 306)):
            (self.apps / scope).mkdir()
            (self.apps / scope / 'cpu.max').write_text(UNLIMITED + '\n')
            (self.proc / str(pid)).mkdir()
            (self.proc / str(pid) / 'cgroup').write_text(f'0::/user.slice/user-1000.slice/user@1000.service/app.slice/{scope}\n')
            self.throttle.window_event({'WindowOpenedOrChanged': {'window': {'id': pid, 'pid': pid, 'is_focused': False}}})
        throttled = self.throttle.apply()
        self.assertEqual([throttle.label(s) for s in throttled if 'vesktop' not in s], ['floorp'])
        for name in ('foot', 'kitty', 'alacritty', 'wezterm', 'ghostty', 'codium', 'code'):
            self.assertIn(name, throttle.KEEP)

    def test_an_app_shares_one_quota_between_its_scopes(self):
        self.throttle.apply()
        limits = self.limits()
        # Discord's two scopes get half each: 5% for the app, not 5% per scope.
        self.assertEqual((limits['app-vesktop-2295.scope'], limits['app-niri-vesktop-2295.scope']),
                         ('2500 100000', '2500 100000'))
        self.assertEqual(limits['app-niri-floorp-2240.scope'], '5000 100000')
        tiny = Throttle(self.apps, quota=1, battery=lambda: True, proc=self.proc, clock=lambda: self.now)
        self.assertEqual(tiny.share(4), f'{throttle.MINIMUM} 100000')  # cpu.max refuses less than 1 ms.

    def test_apps_without_a_window_are_never_capped(self):
        self.throttle.apply()
        limits = self.limits()  # The picker (and its wl-copy) and fcitx5 have no window.
        self.assertEqual((limits['app-niri-emoji\\x2dpicker-51325.scope'], limits['app-niri-fcitx5-2259.scope']),
                         (UNLIMITED, UNLIMITED))
        # KEEP matches the name without systemd's escapes.
        self.throttle.window_event({'WindowOpenedOrChanged': {'window': {'id': 9, 'pid': 51325, 'is_focused': False}}})
        self.throttle.keep = ('emoji-picker',)
        self.throttle.apply()
        self.assertEqual(self.limits()['app-niri-emoji\\x2dpicker-51325.scope'], UNLIMITED)
        self.throttle.keep = ()
        self.throttle.apply()
        self.assertEqual(self.limits()['app-niri-emoji\\x2dpicker-51325.scope'], '5000 100000')

    def test_focusing_an_app_frees_all_its_scopes(self):
        self.throttle.window_event({'WindowFocusChanged': {'id': 4}})  # Discord's window: its main process.
        self.throttle.apply()
        limits = self.limits()
        self.assertEqual((limits['app-vesktop-2295.scope'], limits['app-niri-vesktop-2295.scope']), (UNLIMITED, UNLIMITED))
        self.assertEqual(throttle.app_of('app-niri-vesktop-2295.scope'), throttle.app_of('app-vesktop-2295.scope'))
        self.assertEqual(throttle.app_of('app-odd.scope'), 'app-odd.scope')

    def test_apps_playing_media_are_left_alone_and_so_is_everything_on_ac(self):
        node = {'id': 80, 'type': 'PipeWire:Interface:Node',
                'info': {'state': 'idle', 'props': {'media.class': 'Stream/Output/Audio', 'application.process.id': 2240}}}
        self.throttle.media_event([node])
        self.assertIn('app-niri-floorp-2240.scope', self.throttle.apply())  # An open, idle stream is not playing.
        self.throttle.media_event([{'id': 80, 'type': 'PipeWire:Interface:Node', 'info': {'state': 'running'}}])
        self.assertNotIn('app-niri-floorp-2240.scope', self.throttle.apply())  # A partial update: now playing.
        self.throttle.media_event([{'id': 80, 'type': 'PipeWire:Interface:Node', 'info': None}])
        self.assertIn('app-niri-floorp-2240.scope', self.throttle.apply())  # The stream is gone.
        self.on_battery = False
        self.assertEqual(self.throttle.apply(), [])
        self.assertEqual(set(self.limits().values()), {UNLIMITED})

    def test_release_lifts_every_quota_even_one_left_by_a_crash(self):
        (self.apps / 'app-niri-vesktop-7.scope').mkdir()
        (self.apps / 'app-niri-vesktop-7.scope' / 'cpu.max').write_text('5000 100000\n')
        self.throttle.apply()
        self.throttle.release()
        self.assertEqual(set(self.limits().values()), {UNLIMITED})

    def test_labels(self):
        self.assertEqual([throttle.label(s) for s in ('app-niri-floorp-2240.scope', 'app-md.Obsidian-2243.scope',
                                                       'app-niri-signal\\x2ddesktop-2247.scope')],
                         ['floorp', 'Obsidian', 'signal-desktop'])

    def test_pw_dump_batches_and_niri_lines_parse_across_reads(self):
        stream = type('Stream', (), {})()
        stream.buffer = '[{"id": 1}]\n[{"id": 2},'
        self.assertEqual(throttle.batches(stream), [[{'id': 1}]])
        stream.buffer += ' {"id": 3}]\n'
        self.assertEqual(throttle.batches(stream), [[{'id': 2}, {'id': 3}]])
        stream.buffer = '{"WindowFocusChanged": {"id": 4}}\n{"Window'
        self.assertEqual(throttle.lines(stream), [{'WindowFocusChanged': {'id': 4}}])
        self.assertEqual(stream.buffer, '{"Window')

    def test_labels_drop_every_launchers_prefix_and_random_suffix(self):
        self.assertEqual([throttle.label(s) for s in (
            'app-gnome-org.gnome.Terminal-3061.scope', 'app-flatpak-com.valvesoftware.Steam-4100.scope',
            'app-kde-org.kde.dolphin-0123456789abcdef0123456789abcdef.scope', 'app-gnome\\x2dcalculator-9.scope',
            'app-niri-gnome\\x2dcalculator-9.scope', 'app-firefox.scope')],
            ['Terminal', 'Steam', 'dolphin', 'gnome-calculator', 'gnome-calculator', 'firefox'])
        # uwsm's and app2unit's 8 hex digits (Steam started by `uwsm app` is app-niri-steam-140a4d35.scope),
        # in a nested slice; a name that is all letters a–f stays.
        self.assertEqual([throttle.label(s) for s in (
            'app-graphical.slice/app-niri-t3code-6a5fdff9.scope', 'app-graphical.slice/app-sway-firefox-4f3a9c21.scope',
            'app-graphical.slice/app-niri-steam-140a4d35.scope', 'app-niri-facade.scope')],
            ['t3code', 'firefox', 'steam', 'facade'])

    def test_an_app_is_its_launch_pid_its_flatpak_id_or_its_launchers_name(self):
        scopes = ['app-niri-vesktop-2295.scope', 'app-vesktop-2295.scope', 'app-niri-foot-9.scope',
                  'app-niri-foot-12.scope', 'app-graphical.slice/app-flatpak-dev.vencord.Vesktop-464058271.scope',
                  'app-graphical.slice/app-flatpak-dev.vencord.Vesktop-900100968.scope',
                  'app-graphical.slice/app-niri-t3code-6a5fdff9.scope', 'app-com.t3tools.T3Code-814872.scope',
                  'app-graphical.slice/app-sway-firefox-4f3a9c21.scope']
        apps = throttle.group(scopes)
        self.assertEqual(apps['app-niri-vesktop-2295.scope'], apps['app-vesktop-2295.scope'])
        self.assertNotEqual(apps['app-niri-foot-9.scope'], apps['app-niri-foot-12.scope'])  # Two launches, two apps.
        self.assertEqual(apps['app-graphical.slice/app-flatpak-dev.vencord.Vesktop-464058271.scope'],
                         apps['app-graphical.slice/app-flatpak-dev.vencord.Vesktop-900100968.scope'])
        self.assertNotEqual(apps['app-vesktop-2295.scope'],  # Not the native Vesktop's.
                            apps['app-graphical.slice/app-flatpak-dev.vencord.Vesktop-464058271.scope'])
        self.assertEqual(apps['app-graphical.slice/app-niri-t3code-6a5fdff9.scope'], '814872')  # By its launch pid.
        self.assertEqual(apps['app-graphical.slice/app-sway-firefox-4f3a9c21.scope'],
                         'app-graphical.slice/app-sway-firefox-4f3a9c21.scope')

    def test_input_methods_terminals_and_editors_are_built_in_and_the_configuration_adds_more(self):
        for name in ('fcitx', 'ibus', 'foot', 'terminal', 'code', 'zed'):
            self.assertIn(name, throttle.KEEP)
        self.assertEqual(throttle.keep_patterns(), throttle.KEEP)  # The default configuration adds none.
        with mock.patch.dict(config.SETTINGS['throttle'], {'keep': ['blender']}):
            self.assertEqual(throttle.keep_patterns(), throttle.KEEP + ('blender',))
            self.assertEqual(throttle.keep_patterns(['gimp']), throttle.KEEP + ('gimp',))  # --keep instead.
        self.assertEqual((throttle.QUOTA, throttle.GRACE), (config.DEFAULTS['throttle']['quota'],
                                                            config.DEFAULTS['throttle']['grace']))

    def test_a_lone_string_is_one_pattern_not_its_letters(self):
        with mock.patch.dict(config.SETTINGS['throttle'], {'keep': 'blender'}):
            self.assertEqual(throttle.keep_patterns(), throttle.KEEP + ('blender',))
        self.assertEqual(throttle.string_list(None, 'x'), [])
        self.assertEqual(throttle.string_list(['krita', '', ' ', 3], 'x'), ['krita', '3'])  # '' would match every scope.
        with mock.patch.dict(config.SETTINGS['throttle'], {'keep': 5}), self.assertRaises(SystemExit) as refused:
            throttle.keep_patterns()
        self.assertIn('[throttle] keep must be a list', str(refused.exception))
        self.assertEqual(Throttle(self.apps, keep='krita').keep, ('krita',))

    def test_the_command_line_overrides_the_configuration(self):
        with mock.patch.object(throttle, 'run') as run, mock.patch.object(throttle.signal, 'signal'), \
                mock.patch.object(throttle.compositor, 'detect', return_value=compositor.Niri()), \
                mock.patch.dict(config.SETTINGS['throttle'], {'keep': ['blender']}):
            throttle.main([])
            agent = run.call_args.args[0]
            self.assertEqual((agent.quota, agent.keep[-1]), (throttle.QUOTA * 1000, 'blender'))
            self.assertIsInstance(agent.desktop, compositor.Niri)
            throttle.main(['--quota', '7', '--keep', 'Krita'])
            agent = run.call_args.args[0]
            self.assertEqual((agent.quota, agent.keep), (7000, throttle.KEEP + ('krita',)))

    def test_sway_windows_are_throttled_the_same_way(self):
        tree = {'type': 'root', 'nodes': [{'type': 'output', 'name': 'eDP-1', 'nodes': [
            {'id': 5, 'type': 'workspace', 'nodes': [{'id': 21, 'type': 'con', 'pid': 2240, 'focused': True},
                                                     {'id': 22, 'type': 'con', 'pid': 2243, 'focused': False}]}]}]}
        sway = compositor.Sway(clock=lambda: self.now, query=lambda kind: tree if kind == 'get_tree' else [
            {'id': 5, 'output': 'eDP-1', 'visible': True}])
        capped = Throttle(self.apps, quota=5, battery=lambda: True, proc=self.proc, clock=lambda: self.now,
                          desktop=sway)
        sway.started()
        self.assertEqual(capped.apply(), ['app-md.Obsidian-2243.scope'])  # Floorp has focus.
        sway.event({'change': 'focus', 'container': {'id': 22, 'type': 'con', 'pid': 2243, 'focused': True}})
        self.assertEqual(capped.apply(), [])  # Obsidian now; Floorp in its grace period.

    def test_nothing_is_capped_once_the_compositors_stream_has_ended(self):
        self.assertTrue(self.throttle.apply())
        self.throttle.desktop.clear()  # What run() does when the stream ends.
        self.assertEqual(self.throttle.apply(), [])
        self.assertEqual(set(self.limits().values()), {UNLIMITED})

    def add(self, scope, pid, cgroup=None, comm=None):
        """An app scope (its path in app.slice) with process `pid` in it (or in `cgroup`, a path from the root)."""
        (self.apps / scope).mkdir(parents=True, exist_ok=True)
        (self.apps / scope / 'cpu.max').write_text(UNLIMITED + '\n')
        (self.proc / str(pid)).mkdir(parents=True, exist_ok=True)
        (self.proc / str(pid) / 'cgroup').write_text(
            f'0::{cgroup or "/user.slice/user-1000.slice/user@1000.service/app.slice/" + scope}\n')
        if comm:
            (self.proc / str(pid) / 'comm').write_text(comm + '\n')

    SATELLITE = '/user.slice/user-1000.slice/user@1000.service/session.slice/wayland-wm@niri.service'

    def test_x11_windows_under_niri_are_their_apps_by_app_id(self):
        # niri gives an X11 window xwayland-satellite's pid, outside every app scope; its app id is the X11 class.
        self.add('app-niri-steam-5000.scope', 5000)
        (self.proc / '2649').mkdir()
        (self.proc / '2649' / 'cgroup').write_text(f'0::{self.SATELLITE}\n')
        self.throttle.window_event({'WindowOpenedOrChanged': {'window': {
            'id': 7, 'pid': 2649, 'app_id': 'steam', 'is_focused': True}}})
        self.assertNotIn('app-niri-steam-5000.scope', self.throttle.apply())  # Focused: free.
        self.throttle.window_event({'WindowFocusChanged': {'id': 1}})
        self.throttle.apply()
        self.now += throttle.GRACE + 1
        self.assertIn('app-niri-steam-5000.scope', self.throttle.apply())  # In the background, as any app with a window.
        # With xwayland-satellite in an app scope of its own (started by the compositor's config), the same.
        self.add('app-niri-xwayland\\x2dsatellite-2649.scope', 2649, comm='.xwayland-satel')
        self.throttle.window_event({'WindowFocusChanged': {'id': 7}})
        self.assertNotIn('app-niri-steam-5000.scope', self.throttle.apply())
        self.assertEqual(self.throttle.window_apps(7), {'5000'})

    def test_while_the_focused_window_is_no_known_apps_nothing_is_capped(self):
        # A game Steam started: an X11 window under niri whose class names no scope. It could be any app's.
        self.add('app-niri-steam-5000.scope', 5000)
        (self.proc / '2649').mkdir()
        (self.proc / '2649' / 'cgroup').write_text(f'0::{self.SATELLITE}\n')
        for window in ({'id': 7, 'pid': 2649, 'app_id': 'steam', 'is_focused': False},
                       {'id': 8, 'pid': 2649, 'app_id': 'steam_app_1145360', 'is_focused': True}):
            self.throttle.window_event({'WindowOpenedOrChanged': {'window': window}})
        self.assertIsNone(self.throttle.focused_apps())
        self.assertEqual(self.throttle.apply(), [])
        self.throttle.window_event({'WindowFocusChanged': {'id': 1}})  # Then Floorp: the rest get the grace period.
        self.assertEqual(self.throttle.apply(), [])
        self.now += throttle.GRACE + 1
        self.assertIn('app-niri-steam-5000.scope', self.throttle.apply())
        self.throttle.window_event({'WindowFocusChanged': {'id': None}})  # A layer surface has focus: known, no app.
        self.assertEqual(self.throttle.focused_apps(), set())

    def test_apps_in_nested_slices_are_found(self):
        # uwsm, app2unit and Flatpak start apps in app.slice/app-graphical.slice (as recorded on a Framework 13:
        # a Flatpak Vesktop's two sandboxes, and T3 Code started by `uwsm app`, whose Electron main process moved
        # into a scope of its own and left the rest in the launcher's).
        for scope, pid in (('app-graphical.slice/app-flatpak-dev.vencord.Vesktop-464058271.scope', 667715),
                           ('app-graphical.slice/app-flatpak-dev.vencord.Vesktop-900100968.scope', 667740),
                           ('app-graphical.slice/app-niri-t3code-6a5fdff9.scope', 814903),
                           ('app-graphical.slice/app-aw\\x2dqt@autostart.service', 2995),  # A service: no app.
                           ('app-t3code.slice/app-t3code-agents.slice/t3code-claude-5c108372.scope', 875345)):
            self.add(scope, pid)
        self.assertIn('app-graphical.slice/app-flatpak-dev.vencord.Vesktop-464058271.scope',
                      throttle.app_scopes(self.apps))
        self.assertNotIn('app-t3code.slice/app-t3code-agents.slice/t3code-claude-5c108372.scope',
                         throttle.app_scopes(self.apps))
        self.assertEqual(self.throttle.scope_of(667715),
                         'app-graphical.slice/app-flatpak-dev.vencord.Vesktop-464058271.scope')
        self.assertIsNone(self.throttle.scope_of(2995))
        self.throttle.window_event({'WindowOpenedOrChanged': {'window': {'id': 30, 'pid': 667715, 'is_focused': False}}})
        self.throttle.window_event({'WindowOpenedOrChanged': {'window': {'id': 31, 'pid': 272490, 'is_focused': False}}})
        self.add('app-graphical.slice/app-niri-t3code-6a5fdff9.scope', 814903)
        self.throttle.keep = ()  # Else 'code' keeps T3 Code.
        self.throttle.apply()
        limits = {p.relative_to(self.apps).parent.as_posix(): p.read_text().strip() for p in self.apps.rglob('cpu.max')}
        # Each app's scopes share its quota: the Flatpak's sandboxes, T3 Code's two scopes.
        for scope in ('app-graphical.slice/app-flatpak-dev.vencord.Vesktop-464058271.scope',
                      'app-graphical.slice/app-flatpak-dev.vencord.Vesktop-900100968.scope',
                      'app-graphical.slice/app-niri-t3code-6a5fdff9.scope', 'app-com.t3tools.T3Code-272490.scope'):
            self.assertEqual(limits[scope], '2500 100000', scope)
        self.throttle.window_event({'WindowFocusChanged': {'id': 31}})
        self.throttle.apply()  # Focused: both of T3 Code's free.
        self.assertEqual((self.apps / 'app-graphical.slice/app-niri-t3code-6a5fdff9.scope/cpu.max').read_text().strip(),
                         UNLIMITED)
        self.throttle.release()
        self.assertEqual({p.read_text().strip() for p in self.apps.rglob('cpu.max')}, {UNLIMITED})

    def test_keep_matches_the_scopes_own_name_not_its_slice(self):
        self.add('app-graphical.slice/app-sway-krita-4f3a9c21.scope', 700)
        self.throttle.window_event({'WindowOpenedOrChanged': {'window': {'id': 70, 'pid': 700, 'is_focused': False}}})
        self.throttle.keep = ('graphical',)
        self.assertIn('app-graphical.slice/app-sway-krita-4f3a9c21.scope', self.throttle.apply())
        self.throttle.keep = ('krita',)
        self.assertNotIn('app-graphical.slice/app-sway-krita-4f3a9c21.scope', self.throttle.apply())


class OnBatteryTests(unittest.TestCase):
    """Chargers read as battery.mains_online reads them (the estimator's and the collector's view)."""

    def supplies(self, **supplies):
        root = Path(tempfile.mkdtemp())
        for name, files in supplies.items():
            (root / name).mkdir()
            for key, value in files.items():
                (root / name / key).write_text(value + '\n')
        return root

    BATTERY = {'type': 'Battery', 'status': 'Discharging', 'present': '1'}

    def test_usb_c_chargers_count_and_mains_first(self):
        usb_only = self.supplies(BAT1=self.BATTERY, **{'ucsi-source-psy-USBC000:001': {'type': 'USB', 'online': '1'},
                                                       'ucsi-source-psy-USBC000:002': {'type': 'USB', 'online': '0'}})
        self.assertFalse(throttle.on_battery(usb_only))  # Plugged in over USB-C only: on AC.
        unplugged = self.supplies(BAT1=self.BATTERY, **{'ucsi-source-psy-USBC000:001': {'type': 'USB', 'online': '0'}})
        self.assertTrue(throttle.on_battery(unplugged))
        mains = self.supplies(BAT0=self.BATTERY, AC={'type': 'Mains', 'online': '0'},
                              **{'ucsi-source-psy-USBC000:001': {'type': 'USB', 'online': '1'}})
        self.assertTrue(throttle.on_battery(mains))  # A Mains supply says, as the estimator reads it.
        mouse = self.supplies(BAT0=self.BATTERY, hidpp_battery_0={'type': 'USB', 'scope': 'Device', 'online': '1'})
        self.assertTrue(throttle.on_battery(mouse))  # A device's supply is no charger.

    def test_without_a_charger_to_read_only_a_battery_says_so(self):
        self.assertTrue(throttle.on_battery(self.supplies(BAT0=self.BATTERY)))
        self.assertFalse(throttle.on_battery(self.supplies()))  # A desktop, a VM: no battery to run on.


class Stop(Exception):
    pass


class RunTests(unittest.TestCase):
    """throttle.run with stand-in event streams (small Python programs)."""

    def test_a_compositor_stream_that_ends_is_forgotten_and_started_again(self):
        events = [{'WorkspacesChanged': {'workspaces': [{'id': 1, 'output': 'eDP-1', 'is_active': True}]}},
                  {'WindowsChanged': {'windows': [{'id': 3, 'pid': 42, 'workspace_id': 1, 'is_focused': True}]}}]
        text = chr(10).join(map(json.dumps, events))
        printer = f'import time; print({text!r}, flush=True); time.sleep(0.5)'  # niri for a moment.
        seen, started = [], []

        class Desktop(compositor.Niri):
            command = (sys.executable, '-c', printer)

            def started(self):
                started.append(time.monotonic())

        class Agent(throttle.Session):
            state_path = Path(tempfile.mkdtemp()) / 'state.json'
            released = False

            def step(self, media_known):
                seen.append((self.desktop.known, set(self.desktop.windows), media_known))
                if len(started) >= 2 and self.desktop.known:  # Back after a restart.
                    raise Stop
                return {'known': self.desktop.known}

            def release(self):
                Agent.released = True

        agent = Agent(desktop=Desktop())
        pw = (sys.executable, '-c', 'import time; print("[]", flush=True); time.sleep(30)')
        with mock.patch.object(throttle, 'RESCAN', 0.2), self.assertRaises(Stop):
            throttle.run(agent, pw_command=pw)
        known = [k for k, _, _ in seen]
        ended = known.index(False, known.index(True))  # Known, then forgotten when the stream ended,
        self.assertEqual(seen[ended][1], set())
        self.assertTrue(known[-1])  # then known again once it was started again.
        self.assertEqual(len(started), 2)
        self.assertTrue(Agent.released)
        self.assertFalse(Agent.state_path.exists())

    def test_sighup_and_sigterm_lift_the_quotas_on_the_way_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps, proc, state = Path(tmp) / 'app.slice', Path(tmp) / 'proc', Path(tmp) / 'throttle.json'
            for scope, pid in (('app-niri-floorp-2240.scope', 2240), ('app-niri-foot-9.scope', 9)):
                (apps / scope).mkdir(parents=True)
                (apps / scope / 'cpu.max').write_text(UNLIMITED + '\n')
                (proc / str(pid)).mkdir(parents=True)
                (proc / str(pid) / 'cgroup').write_text(f'0::/user.slice/app.slice/{scope}\n')
            events = [{'WorkspacesChanged': {'workspaces': [{'id': 1, 'output': 'eDP-1', 'is_active': True}]}},
                      {'WindowsChanged': {'windows': [{'id': 1, 'pid': 2240, 'workspace_id': 1, 'is_focused': False},
                                                      {'id': 2, 'pid': 9, 'workspace_id': 1, 'is_focused': True}]}}]
            niri = f'import time; print({chr(10).join(map(json.dumps, events))!r}, flush=True); time.sleep(30)'
            program = f"""
import sys
from pathlib import Path
from drainhound import compositor, throttle
class Niri(compositor.Niri):
    command = (sys.executable, '-c', {niri!r})
agent = throttle.Throttle(Path({str(apps)!r}), quota=5, battery=lambda: True, proc=Path({str(proc)!r}), desktop=Niri())
agent.state_path = Path({str(state)!r})
throttle.exit_on_signals()
throttle.run(agent, pw_command=(sys.executable, '-c', 'import time; print("[]", flush=True); time.sleep(30)'))
"""
            capped = apps / 'app-niri-floorp-2240.scope' / 'cpu.max'
            for number in (signal.SIGHUP, signal.SIGTERM):
                child = subprocess.Popen([sys.executable, '-c', program],
                                         env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)})
                try:
                    deadline = time.monotonic() + 20
                    # Until capped: a read between the child's truncate and write sees ''.
                    while capped.read_text().strip() != '5000 100000' and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertEqual(capped.read_text().strip(), '5000 100000')
                    child.send_signal(number)
                    self.assertEqual(child.wait(timeout=20), 0)
                finally:
                    child.kill()
                self.assertEqual(capped.read_text().strip(), UNLIMITED, number)
                self.assertFalse(state.exists())

    def test_without_a_compositor_only_pipewire_is_followed(self):
        steps = []

        class Agent(throttle.Session):
            state_path = Path(tempfile.mkdtemp()) / 'state.json'

            def step(self, media_known):
                steps.append(media_known)
                if len(steps) > 2:
                    raise Stop
                return {}

            def release(self):
                pass
        pw = (sys.executable, '-c', 'import time; print("[]", flush=True); time.sleep(30)')
        with mock.patch.object(throttle, 'RESCAN', 0.1), self.assertRaises(Stop):
            throttle.run(Agent(desktop=compositor.Desktop()), pw_command=pw)
        self.assertTrue(steps[-1])


if __name__ == '__main__':
    unittest.main()

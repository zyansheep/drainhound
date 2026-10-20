import json
import math
import os
import random
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
import itertools
from unittest import mock

from wattif import config
from wattif import experiment as experiment_module
from wattif.experiment import Setting, design, experiment, unit_label, automatic, Ledger, Watchdog


def tree(root, files):
    """Files under `root`, {relative path: contents}; a path ending in / is a directory."""
    for path, text in files.items():
        target = root / path
        if path.endswith('/'):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f'{text}\n')


CPU = 'devices/system/cpu/'


def policies(count, **files):
    return {f'{CPU}cpufreq/policy{i}/{name}': value for i in range(count) for name, value in files.items()}


ASPM_POLICY = {'module/pcie_aspm/parameters/policy': '[default] performance powersave powersupersave'}
# A Framework 13 (Ryzen 7040) on battery: amd-pstate (EPP) with per-CPU boost,
# amdgpu with ABM on the panel (3: its udev rule's battery value), NVMe, Wi-Fi.
AMD_LAPTOP = {
    'class/power_supply/ACAD/type': 'Mains', 'class/power_supply/ACAD/online': '0',
    'class/power_supply/BAT1/type': 'Battery', 'class/power_supply/BAT1/status': 'Discharging',
    f'{CPU}cpufreq/boost': '1', f'{CPU}amd_pstate/status': 'active',
    **policies(2, boost='1', scaling_max_freq='4972435', cpuinfo_max_freq='4972435', cpuinfo_min_freq='405914',
               amd_pstate_max_freq='4972435', scaling_driver='amd-pstate-epp'),
    **ASPM_POLICY, 'class/nvme/nvme0/power/pm_qos_latency_tolerance_us': '0',
    'class/drm/card1-eDP-1/amdgpu/panel_power_savings': '3',
    'class/drm/card1/device/power_dpm_force_performance_level': 'auto',
    'module/amdgpu/parameters/dcdebugmask': '0x10', 'class/net/wlan0/wireless/': '',
}
# An Intel laptop on battery: intel_pstate (turbo through no_turbo), i915 (no ABM,
# no DPM levels), NVMe, Wi-Fi, a battery reporting energy and power, and a mouse's.
INTEL_LAPTOP = {
    'class/power_supply/AC/type': 'Mains', 'class/power_supply/AC/online': '0',
    'class/power_supply/BAT0/type': 'Battery', 'class/power_supply/BAT0/status': 'Discharging',
    'class/power_supply/BAT0/energy_now': '41000000', 'class/power_supply/BAT0/power_now': '6500000',
    'class/power_supply/hidpp_battery_0/type': 'Battery', 'class/power_supply/hidpp_battery_0/scope': 'Device',
    'class/power_supply/hidpp_battery_0/status': 'Discharging',
    f'{CPU}intel_pstate/status': 'active', f'{CPU}intel_pstate/no_turbo': '0', f'{CPU}intel_pstate/turbo_pct': '33',
    **policies(4, scaling_max_freq='4700000', cpuinfo_max_freq='4700000', cpuinfo_min_freq='400000',
               base_frequency='2100000', scaling_driver='intel_pstate'),
    **ASPM_POLICY, 'class/nvme/nvme0/power/pm_qos_latency_tolerance_us': '100000',
    'class/drm/card0/device/vendor': '0x8086', 'class/drm/card0-eDP-1/status': 'connected',
    'class/net/wlp0s20f3/phy80211/': '',
}
# Nothing to control: no cpufreq (a virtual machine, say), ASPM policy, NVMe, GPU files or Wi-Fi.
MINIMAL = {'class/power_supply/BAT0/type': 'Battery', 'class/power_supply/BAT0/status': 'Discharging'}
EVERYTHING = ('powerprofilesctl', 'iw')  # Commands on PATH.
# `niri msg -j outputs` and `swaymsg -r -t get_outputs` (trimmed).
NIRI_OUTPUTS = {
    'eDP-1': {'name': 'eDP-1', 'make': 'BOE', 'model': '0x0BCA', 'current_mode': 0, 'vrr_supported': False,
              'modes': [{'width': 2256, 'height': 1504, 'refresh_rate': 59999, 'is_preferred': True},
                        {'width': 2256, 'height': 1504, 'refresh_rate': 47998, 'is_preferred': False},
                        {'width': 1920, 'height': 1200, 'refresh_rate': 59999, 'is_preferred': False}]},
    'DP-3': {'name': 'DP-3', 'current_mode': 1,
             'modes': [{'width': 2560, 'height': 1440, 'refresh_rate': 59951, 'is_preferred': False},
                       {'width': 2560, 'height': 1440, 'refresh_rate': 143998, 'is_preferred': True}]}}
SWAY_OUTPUTS = [
    {'id': 4, 'type': 'output', 'name': 'eDP-1', 'make': 'Sharp Corporation', 'model': '0x1546', 'active': True,
     'dpms': True, 'power': True, 'scale': 2.0, 'transform': 'normal',
     'modes': [{'width': 2560, 'height': 1600, 'refresh': 120000, 'picture_aspect_ratio': 'none'},
               {'width': 2560, 'height': 1600, 'refresh': 60000, 'picture_aspect_ratio': 'none'}],
     'current_mode': {'width': 2560, 'height': 1600, 'refresh': 120000, 'picture_aspect_ratio': 'none'}},
    {'id': 5, 'type': 'output', 'name': 'HDMI-A-1', 'active': False, 'modes': [], 'current_mode': None}]


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.hooks = []
        self.suspends = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        for hook in self.hooks:
            hook(self.now)

    def suspend(self, seconds):
        """The laptop sleeps: time passes and BOOTTIME runs ahead of MONOTONIC."""
        self.now += seconds
        self.suspends += 1

    def suspended_since(self, mark):
        return mark is not None and self.suspends > mark[1], (self.now, self.suspends)


class FakeSetting(Setting):
    def __init__(self, value='performance', ac_managed=False, name='aspm', alternative='powersupersave', stock=None,
                 levels=None):
        self.value, self.history = value, []
        stock = value if stock is None else stock

        def set_value(v):
            self.history.append(v)
            self.value = v
        super().__init__(name, name.upper(), '', lambda: self.value, set_value,
                         lambda cur: alternative, ac_managed=ac_managed,
                         baseline=stock if callable(stock) else (lambda on: stock),
                         levels=(lambda: levels) if levels else None)


class FakeJournal:
    """The kernel log: entries per boot ('0' this one, '-1' the one before)."""

    def __init__(self):
        self.boots = {'0': [], '-1': []}
        self.ids = {'0': 'b0', '-1': 'b1'}
        self.blind = False
        self.previous = None
        self.shutdown = True  # The previous boot logged a clean shutdown.
        self.resumed = None  # The last resume of this boot.

    def add(self, t, message, boot='0'):
        self.boots[boot].append({'t': t, 'boot': self.ids[boot], 'message': message})

    def kernel(self, boot='0', since=None):
        if self.blind:
            return None
        return [e for e in self.boots.get(str(boot), []) if since is None or e['t'] >= since]

    def readable(self):
        return not self.blind

    def previous_boot(self):
        return self.previous

    def shutdown_logged(self, boot='-1'):
        return None if self.blind else self.shutdown

    def last_resume(self):
        return self.resumed


class Sandbox:
    """Every path wattif experiment writes, in a temporary directory; no commands
    (sudo, journalctl, notify-send, systemctl), an empty /sys and /proc, no
    compositor or login session, and a fake kernel log. `commands` are the
    programs on PATH, `units` the user units installed; `helper` whether the root
    helper may be run, `followable` whether wattif throttle can follow the windows."""

    def __init__(self, test):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = root = Path(tmp.name)
        saved, runtime = root / 'state', root / 'run'
        (root / 'sys').mkdir()
        (root / 'cmdline').write_text('pcie_aspm.policy=performance nvme_core.default_ps_max_latency_us=0\n')
        self.journal, self.notes, self.specs, self.boot = FakeJournal(), [], {}, 'b0'
        self.battery = True
        self.commands, self.units = set(), set()
        self.helper, self.followable = True, True

        def no_commands(*args, **kwargs):
            raise AssertionError(f'a test ran a command: {args}')
        patches = dict(SAVED=saved, ENABLED=saved / 'experiments-enabled', POOL=saved / 'experiments.json',
                       LOG=saved / 'experiment-log.json', APPLIED=saved / 'applied.json', RUNTIME=runtime,
                       STATE=runtime / 'experiment.json', REQUEST=runtime / 'experiment-request.json',
                       LOCK=runtime / 'experiment.lock', SOURCE=runtime / 'power-source.json',
                       PROBE=runtime / 'helper.json', helper_usable=lambda: self.helper,
                       throttle_followable=lambda: self.followable,
                       RECOMMEND=runtime / 'recommend.json', CMDLINE=root / 'cmdline', SYS=root / 'sys',
                       CPUFREQ=root / 'sys/devices/system/cpu/cpufreq', PROC=root / 'proc', READBACK=(1, 0),
                       run=no_commands, command_exists=lambda name: name in self.commands,
                       unit_loaded=lambda unit: unit in self.units,
                       Journal=lambda: self.journal, notify=lambda summary, body: self.notes.append((summary, body)),
                       boot_id=lambda: self.boot, profile_get=lambda: 'power-saver',
                       catalog=lambda unit=None, units=None: self.specs, on_battery=lambda: self.battery)
        for name, value in patches.items():
            patcher = mock.patch.object(experiment_module, name, value)
            patcher.start()
            test.addCleanup(patcher.stop)
        for target, value in ((experiment_module.Ledger, ('KEEP', math.inf)),
                              (experiment_module.subprocess, ('run', no_commands))):
            patcher = mock.patch.object(target, *value)
            patcher.start()
            test.addCleanup(patcher.stop)
        environment = mock.patch.dict(os.environ)  # The session's compositor and login session are not the test's.
        environment.start()
        test.addCleanup(environment.stop)
        for variable in ('NIRI_SOCKET', 'SWAYSOCK', 'XDG_SESSION_ID'):
            os.environ.pop(variable, None)
        self.saved, self.runtime = saved, runtime

    def state(self):
        return json.loads((self.runtime / 'experiment.json').read_text())

    def prefs(self):
        return experiment_module.preferences()


def levered(name, value, stock, levels, **kwargs):
    return FakeSetting(value=value, name=name, stock=stock, levels=levels, alternative=levels[0], **kwargs)


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)

    def run_experiment(self, spec, minutes, battery=lambda: True, clock=None, **kwargs):
        clock = clock or FakeClock()
        path = self.box.runtime / 'experiment.json'
        experiment(spec, minutes, clock=clock, battery=battery, locked=lambda: True,
                   state_path=path, rng=random.Random(1), **kwargs)
        return json.loads(path.read_text()), clock

    def run_automatic(self, specs, clock, until, names, choose=None, battery=lambda: True, **kwargs):
        choose = choose or (lambda names, present, run: ({n: True for n in names}, {}))
        states = []
        path = self.box.runtime / 'experiment.json'
        clock.hooks.append(lambda now: states.append(json.loads(path.read_text())) if path.exists() else None)
        automatic(specs, clock=clock, battery=battery, locked=kwargs.pop('locked', lambda: True),
                  resting=kwargs.pop('resting', lambda: 0.0), enabled=kwargs.pop('enabled', lambda: clock.now < until),
                  names=lambda _specs: list(names), choose=choose, state_path=path, **kwargs)
        return json.loads(path.read_text()), states

    # --- Designs and manual runs ------------------------------------------------------------

    def test_one_setting_is_shuffled_ab_pairs(self):
        blocks = design(['aspm'], random.Random(3))
        pairs = [sorted([next(blocks)['aspm'], next(blocks)['aspm']]) for _ in range(20)]
        self.assertTrue(all(pair == ['A', 'B'] for pair in pairs))

    def test_up_to_four_settings_run_every_combination_each_cycle(self):
        blocks = design(['a', 'b', 'c'], random.Random(4))
        for _ in range(3):
            cycle = {tuple(block[n] for n in 'abc') for block in (next(blocks) for _ in range(8))}
            self.assertEqual(cycle, set(itertools.product('AB', repeat=3)))

    def test_more_settings_stay_balanced_per_cycle(self):
        names = ['a', 'b', 'c', 'd', 'e', 'f']
        blocks = design(names, random.Random(5))
        cycle = [next(blocks) for _ in range(16)]
        for name in names:
            self.assertEqual(sum(block[name] == 'B' for block in cycle), 8)
        self.assertEqual(len({tuple(block[n] for n in 'abcd') for block in cycle}), 16)

    def test_blocks_alternate_and_the_original_is_restored(self):
        spec = FakeSetting(name='wifi-ps', value='on', alternative='off')
        state, _ = self.run_experiment(spec, 16)  # Four 240 s blocks.
        self.assertEqual(state['status'], 'finished')
        self.assertEqual(state['blocks'], 4)
        self.assertEqual(spec.value, 'on')
        self.assertEqual(spec.history.count('off'), 2)

    def test_on_ac_pauses_and_restores_unless_udev_manages_the_setting(self):
        clock = FakeClock()
        plugged = {'until': clock.now + 600}
        battery = lambda: clock.now >= plugged['until']
        spec = FakeSetting()
        state, _ = self.run_experiment(spec, 18, battery=battery, clock=clock)
        self.assertEqual(state['blocks'], 5)  # 18 min of battery time after 10 on AC: 4 + 4 + 4 + 4 + 2.
        self.assertAlmostEqual(state['collected'], 18 * 60)
        managed = FakeSetting(value='power-saver', ac_managed=True)
        managed.value = 'balanced'  # udev's AC value; the runner must leave it alone.
        clock = FakeClock()
        with mock.patch.object(experiment_module, 'GIVE_UP', 3600):  # Always on AC: give up after an hour, not a week.
            self.run_experiment(managed, 5, battery=lambda: False, clock=clock)
        self.assertEqual(managed.history, [])

    def test_external_restore_ends_the_block_and_the_arm_is_reapplied(self):
        clock, spec = FakeClock(), FakeSetting()
        # The pre-sleep hook restores ASPM while a B block is running.
        clock.hooks.append(lambda now: setattr(spec, 'value', 'performance') if 1100 <= now < 1105 else None)
        state, _ = self.run_experiment(spec, 8, clock=clock)
        self.assertGreaterEqual(state['blocks'], 3)
        self.assertEqual(spec.value, 'performance')

    def test_settings_randomized_together_are_tagged_and_all_restored(self):
        clock = FakeClock()
        wifi, cap = FakeSetting(name='wifi-ps', value='on', alternative='off'), FakeSetting(
            value='none', name='cpu-cap', alternative='2000')
        seen = []
        path = self.box.runtime / 'experiment.json'

        def watch(_now):
            state = json.loads(path.read_text()) if path.exists() else {}
            if state.get('status') == 'running':
                seen.append((state['arm'], state['value'], {'wifi-ps': wifi.value, 'cpu-cap': cap.value}))
        clock.hooks.append(watch)
        experiment([wifi, cap], 32, clock=clock, battery=lambda: True, locked=lambda: True,
                   state_path=path, rng=random.Random(2))
        final = json.loads(path.read_text())
        self.assertEqual((final['name'], final['blocks']), ('wifi-ps+cpu-cap', 8))
        self.assertEqual((wifi.value, cap.value), ('on', 'none'))
        self.assertEqual(sorted({arm for arm, _, _ in seen}), ['AA', 'AB', 'BA', 'BB'])
        self.assertTrue(all(value == actual for _, value, actual in seen))

    def test_a_manual_run_starts_from_stock_not_from_a_leftover(self):
        # A crashed run left the cap at 2000 MHz: arm A is still 'none', and so is the end.
        cap = levered('cpu-cap', '2000', 'none', ('2000', 'none'))
        state, _ = self.run_experiment(cap, 8)
        self.assertEqual(state['arms']['cpu-cap'], {'A': 'none', 'B': '2000'})
        self.assertEqual((state['originals'], cap.value), ({'cpu-cap': 'none'}, 'none'))

    def test_a_manual_run_waits_after_resume(self):
        clock, spec = FakeClock(), FakeSetting(name='wifi-ps', value='on', alternative='off')
        changes = []
        spec.set = lambda v, original=spec.set: (changes.append((clock.now, v)), original(v))
        clock.hooks.append(lambda now: clock.suspend(600) if now == 1100 else None)
        state, _ = self.run_experiment(spec, 12, clock=clock)
        resumed = 1100 + 600
        self.assertFalse([t for t, _ in changes if resumed <= t < resumed + experiment_module.RESUME_SETTLE])
        self.assertTrue([t for t, _ in changes if t >= resumed + experiment_module.RESUME_SETTLE])
        self.assertEqual(state['status'], 'finished')

    # --- Automatic experiments ----------------------------------------------------------------

    def test_automatic_blocks_follow_the_plan_rest_after_a_thumbs_down_and_restore(self):
        clock = FakeClock()
        cap = levered('cpu-cap', 'none', 'none', ('2000', 'none'))
        wifi = levered('wifi-ps', 'on', 'on', ('on', 'off'))
        wifi.value = 'off'  # Left off by someone: the run starts it at stock.
        specs = {'cpu-cap': cap, 'wifi-ps': wifi}
        # A 👎 5 minutes in (mid second block) rests experiments until 12; they are disabled at 30.
        rest = lambda: max(0.0, 1720 - clock.now) if clock.now >= 1300 else 0.0
        plans = []

        def choose(names, present, run):
            plans.append(present)
            return {name: len(plans) % 2 == 1 for name in names}, {}
        final, states = self.run_automatic(specs, clock, 1000 + 30 * 60, ['cpu-cap', 'wifi-ps'], choose,
                                           resting=rest, locked=lambda: False)
        self.assertEqual(final['status'], 'finished')
        self.assertEqual((cap.value, wifi.value), ('none', 'on'))  # Stock, not the 'off' it found.
        self.assertIn('resting after your 👎', {s.get('reason') for s in states})
        running = list({s['block']: s for s in states if s['status'] == 'running'}.values())
        self.assertEqual(running[0]['value'], {'cpu-cap': '2000', 'wifi-ps': 'on'})
        self.assertEqual(running[1]['value'], {'cpu-cap': 'none', 'wifi-ps': 'off'})
        self.assertTrue(all(plans))
        self.assertGreaterEqual(final['blocks'], 5)

    def test_the_time_tested_leaves_out_suspends_and_pauses(self):
        clock = FakeClock()
        cap = levered('cpu-cap', 'none', 'none', ('2000', 'none'))
        clock.hooks.append(lambda now: clock.suspend(3600) if now == 1600 else None)  # 2 min into block 3.
        final, states = self.run_automatic({'cpu-cap': cap}, clock, 1000 + 3600 + 20 * 60, ['cpu-cap'])
        tested = {s['block']: s['tested'] for s in states if s['status'] == 'running'}
        self.assertEqual(list(tested.values()), [0, 240, 480, 595, 835, 1075])  # Block 3 ran 115 s, then slept;
        self.assertEqual(final['tested'], 1075 + 60)  # the last one ran 60 s before experiments were turned off.

    def test_testing_that_isnt_worth_it_pauses_with_the_goals_combination_and_looks_again(self):
        clock, recheck = FakeClock(), experiment_module.RECHECK
        cap = levered('cpu-cap', 'none', 'none', ('2000', 'none'))
        wifi = levered('wifi-ps', 'on', 'on', ('off', 'on'))
        looks, meanwhile = [], []

        def choose(names, present, run):
            looks.append(clock.now)
            return {n: True for n in names}, {'value': {'worth': clock.now >= 1000 + recheck, 'rows': []}}
        clock.hooks.append(lambda now: meanwhile.append((cap.value, wifi.value)) if now == 4000 else None)
        final, states = self.run_automatic({'cpu-cap': cap, 'wifi-ps': wifi}, clock, 1000 + recheck + 20 * 60,
                                           ['cpu-cap', 'wifi-ps'], choose=choose,
                                           best=lambda names: {'cpu-cap': '2000'},
                                           version=lambda: 'Linux 6.18, BIOS 3.20')
        settled = [s for s in states if s.get('settled')]
        self.assertEqual((settled[0]['reason'], settled[0]['goal']), (experiment_module.SETTLED, 'battery'))
        self.assertEqual(settled[0]['value'], {'cpu-cap': '2000', 'wifi-ps': 'on'})  # The goal's; the rest at stock.
        self.assertEqual(meanwhile, [('2000', 'on')])
        self.assertEqual([t for t in looks if t < 1000 + recheck], [1000])  # One look until RECHECK,
        running = [s for s in states if s['status'] == 'running']
        self.assertTrue(running and min(s['block_start'] for s in running) >= 1000 + recheck)  # then blocks again.
        self.assertEqual((final['status'], cap.value, wifi.value), ('finished', 'none', 'on'))
        self.assertTrue(all(s['worth'] == {'worth': False, 'rows': []} for s in settled))  # For the menu,
        self.assertTrue(all(s['worth']['worth'] for s in running))  # in every state, as last priced.
        self.assertNotIn('value', running[0]['plan'])

    def test_a_restart_keeps_the_last_value_of_testing_while_fresh(self):
        path = self.box.runtime / 'experiment.json'
        worth = {'worth': True, 'rows': [{'hours': 30, 'saves': 50.0, 'costs': 0.8}]}
        for priced, kept in ((1000 - 3600, True), (1000 - experiment_module.RECHECK - 1, False)):
            experiment_module.write_state({'run': 'auto-1', 'status': 'finished', 'worth': worth, 'priced': priced},
                                          path)
            clock = FakeClock()
            _, states = self.run_automatic({'cpu-cap': levered('cpu-cap', 'none', 'none', ('2000', 'none'))}, clock,
                                           1000 + 60, ['cpu-cap'], battery=lambda: False)
            self.assertEqual(states[0].get('worth'), worth if kept else None, priced)  # On AC: not priced yet.

    def test_a_new_kernel_or_bios_is_tested_before_testing_may_stop(self):
        clock = FakeClock()
        cap = levered('cpu-cap', 'none', 'none', ('2000', 'none'))
        first = experiment_module.retest_record('Linux 6.17, BIOS 3.20')
        self.assertEqual(first['tested'], experiment_module.RETEST)  # What was logged before counts as its.
        never = lambda names, present, run: ({n: True for n in names}, {'value': {'worth': False, 'rows': []}})
        with mock.patch.object(experiment_module, 'RETEST', 600):
            final, states = self.run_automatic({'cpu-cap': cap}, clock, 1000 + 3600, ['cpu-cap'], choose=never,
                                               best=lambda names: {}, version=lambda: 'Linux 6.18, BIOS 3.20')
        blocks = sorted({s['block'] for s in states if s['status'] == 'running'})
        self.assertEqual(blocks, [1, 2, 3])  # 600 s of blocks on the new kernel (three of 240 s),
        self.assertTrue(states[-1].get('settled'))  # then it may stop.
        self.assertEqual(self.box.prefs()['retest'], {'version': 'Linux 6.18, BIOS 3.20', 'tested': 720.0})

    def test_a_missing_setting_sits_out_and_a_failing_one_backs_off(self):
        clock = FakeClock()
        cap = levered('cpu-cap', 'none', 'none', ('2000', 'none'))
        wifi = levered('wifi-ps', 'on', 'on', ('off', 'on'))
        present = wifi.get

        def gone():  # The Wi-Fi interface disappears for five minutes.
            if 1600 <= clock.now < 1900:
                raise RuntimeError('iw: No such device (-19)')
            return present()
        wifi.get = gone
        failures, works = [RuntimeError('wattif-helper: Input/output error')], cap.set

        def flaky(value):
            if failures and value == '2000':
                raise failures.pop()
            works(value)
        cap.set = flaky
        final, states = self.run_automatic({'cpu-cap': cap, 'wifi-ps': wifi}, clock, 1000 + 30 * 60,
                                           ['cpu-cap', 'wifi-ps'])
        running = [s for s in states if s['status'] == 'running']
        excluded = [s.get('excluded') for s in running]
        self.assertIn({'cpu-cap': 'failing: wattif-helper: Input/output error'}, excluded)
        self.assertIn({'wifi-ps': 'unavailable'}, excluded)
        self.assertEqual(final['status'], 'finished')
        self.assertEqual((cap.value, wifi.value), ('none', 'on'))  # Restored.
        # The others kept running meanwhile, and the failing one came back after its backoff.
        self.assertTrue(any(s['value'].get('cpu-cap') == '2000' for s in running))

    def test_a_setting_that_always_fails_is_tried_a_few_times_not_every_minute(self):
        clock = FakeClock()
        cap = levered('cpu-cap', 'none', 'none', ('2000', 'none'))
        attempts = []

        def broken(value):
            attempts.append(clock.now)
            raise RuntimeError('wattif-helper: Invalid argument')
        cap.set = broken
        aspm = levered('aspm', 'performance', 'performance', ('powersupersave', 'performance'))
        experiment_module.prefer(risky=['aspm'])
        writes = []
        aspm.set = lambda v, original=aspm.set: (writes.append(clock.now), original(v))
        rng = random.Random(7)
        self.run_automatic({'cpu-cap': cap, 'aspm': aspm}, clock, 1000 + 2 * 3600, ['cpu-cap', 'aspm'],
                           choose=lambda names, present, run: ({n: rng.random() < 0.5 for n in names}, {}))
        # Before: every failure restored everything and retried in 60 s (a simulation gave 122 ASPM writes/h).
        self.assertLessEqual(len(attempts), experiment_module.FAILURES_MAX)
        self.assertLessEqual(len(writes), 2 * 2 + 1)  # Two hours: at most a change and a return per RISKY_HOLD.
        self.assertGreater(len(writes), 1)
        for before, after in zip(writes, writes[1:-1]):  # The last one: the run's end puts it back at once.
            self.assertGreaterEqual(after - before, experiment_module.RISKY_HOLD)

    def test_risky_settings_hold_and_stop_for_the_day(self):
        clock = FakeClock()
        aspm = levered('aspm', 'performance', 'performance', ('powersupersave', 'performance'))
        experiment_module.prefer(risky=['aspm'])
        flips = itertools.count()
        final, states = self.run_automatic({'aspm': aspm}, clock, 1000 + 24 * 3600, ['aspm'],
                                           choose=lambda names, present, run: ({'aspm': next(flips) % 2 == 0}, {}))
        changes = Ledger().changes('aspm')
        self.assertLessEqual(len(changes), experiment_module.RISKY_DAILY + 1)  # + the final restore.
        times = [c['t'] for c in changes[:-1]]
        self.assertTrue(all(b - a >= experiment_module.RISKY_HOLD for a, b in zip(times, times[1:])))
        self.assertTrue(any(s.get('held') == ['aspm'] for s in states))
        self.assertEqual(aspm.value, 'performance')

    def test_automatic_waits_after_resume_and_a_missing_interface_is_not_restored(self):
        clock = FakeClock()
        wifi = levered('wifi-ps', 'on', 'on', ('off', 'on'))
        cap = levered('cpu-cap', 'none', 'none', ('2000', 'none'))
        writes = []
        for spec in (wifi, cap):
            spec.set = lambda v, spec=spec, original=spec.set: (writes.append((clock.now, spec.name, v)), original(v))
        present = wifi.get

        def reloading():  # mt7921e is reloaded after resume: no interface for 40 s.
            if clock.suspends and clock.now < 2700 + 40:
                raise RuntimeError('iw: No such device (-19)')
            return present()
        wifi.get = reloading
        clock.hooks.append(lambda now: clock.suspend(1500) if now == 1200 else None)
        final, states = self.run_automatic({'wifi-ps': wifi, 'cpu-cap': cap}, clock, 4000, ['wifi-ps', 'cpu-cap'])
        resumed = 1200 + 1500
        self.assertIn('settling after resume', {s.get('reason') for s in states})
        self.assertFalse([w for w in writes if resumed <= w[0] < resumed + experiment_module.RESUME_SETTLE])
        self.assertTrue([w for w in writes if w[0] >= resumed + experiment_module.RESUME_SETTLE])
        self.assertEqual(final['status'], 'finished')

    def test_values_seen_on_ac_are_never_the_battery_baseline(self):
        clock = FakeClock()
        # Started on AC: udev's AC values are live. Restores on battery go to the battery stock values.
        profile = levered('profile', 'balanced', lambda on: 'power-saver' if on else 'balanced',
                          ('power-saver', 'balanced'), ac_managed=True)
        abm = levered('abm', '0', lambda on: {True: '3', False: '0'}[on], ('3', '0'), ac_managed=True)
        battery = lambda: clock.now >= 1300
        final, states = self.run_automatic({'profile': profile, 'abm': abm}, clock, 1000 + 20 * 60,
                                           ['profile', 'abm'], battery=battery,
                                           choose=lambda names, present, run: ({n: False for n in names}, {}))
        self.assertEqual(final['originals'], {'profile': 'power-saver', 'abm': '3'})
        self.assertEqual((profile.value, abm.value), ('power-saver', '3'))
        self.assertEqual(profile.history[0], 'power-saver')  # Never 'balanced' (the AC value) on battery.

    def test_a_restart_never_takes_leftovers_as_the_baseline(self):
        # Seen on Oct 11: a crashed run left gpu-dpm low and cpu-cap 2000, and the next run enforced them.
        clock = FakeClock()
        dpm = levered('gpu-dpm', 'low', 'auto', ('low', 'auto'))
        cap = levered('cpu-cap', '2000', 'none', ('2000', 'none'))
        experiment_module.prefer(risky=['gpu-dpm'])
        final, _ = self.run_automatic({'gpu-dpm': dpm, 'cpu-cap': cap}, clock, 1000 + 10 * 60, ['gpu-dpm', 'cpu-cap'])
        self.assertEqual(final['originals'], {'gpu-dpm': 'auto', 'cpu-cap': 'none'})
        self.assertEqual((dpm.value, cap.value), ('auto', 'none'))

    def test_a_partial_restore_failure_stays_pending_and_is_retried(self):
        clock = FakeClock()
        cap = levered('cpu-cap', 'none', 'none', ('2000', 'none'))
        wifi = levered('wifi-ps', 'on', 'on', ('off', 'on'))
        stuck = {'on': True}
        works = wifi.set

        def refuses_stock(value):
            if value == 'on' and stuck['on']:
                raise RuntimeError('wattif-helper: busy')
            works(value)
        wifi.set = refuses_stock
        final, _ = self.run_automatic({'cpu-cap': cap, 'wifi-ps': wifi}, clock, 1000 + 6 * 60, ['cpu-cap', 'wifi-ps'])
        self.assertEqual(final['status'], 'restore-pending')
        self.assertEqual(final['pending'], ['wifi-ps'])
        self.assertEqual((cap.value, wifi.value), ('none', 'off'))  # The rest was restored first.
        self.box.specs = {'cpu-cap': cap, 'wifi-ps': wifi}
        self.assertEqual(experiment_module.command_restore(battery=lambda: True), {'wifi-ps': 'wattif-helper: busy'})
        self.assertEqual(self.box.state()['status'], 'restore-pending')  # Still: ExecStopPost changes nothing.
        later = Ledger(clock=lambda: time.time() + 3600)  # Past the backoff after those failures.
        self.assertEqual(experiment_module.command_reconcile(battery=lambda: True, ledger=Ledger()),
                         {'wifi-ps': 'backing off after failed restores'})  # The timer, a minute later: waits.
        stuck['on'] = False
        self.assertEqual(experiment_module.command_reconcile(battery=lambda: True, ledger=later), {})  # Retried.
        self.assertEqual((self.box.state()['status'], wifi.value), ('finished', 'on'))

    def test_a_restore_that_keeps_failing_backs_off(self):
        # On AC with ASPM left on and its restore failing: not a write every 15 s, but 60 s, 120 s, 240 s …
        clock = FakeClock()
        aspm = levered('aspm', 'powersupersave', 'performance', ('powersupersave', 'performance'))
        experiment_module.prefer(risky=['aspm'])
        attempts = []

        def broken(value):
            attempts.append(clock.now)
            raise RuntimeError('wattif-helper: aspm reads powersupersave after writing performance')
        aspm.set = broken
        final, states = self.run_automatic({'aspm': aspm}, clock, 1000 + 3600, ['aspm'], battery=lambda: False)
        self.assertLessEqual(len(attempts), 8)  # 0, 60, 180, 420, 900, 1860 s, and the final one.
        gaps = [b - a for a, b in zip(attempts, attempts[1:-1])]
        self.assertEqual(gaps, sorted(gaps))
        self.assertEqual((final['status'], final['pending']), ('restore-pending', ['aspm']))

    # --- The watchdog -------------------------------------------------------------------------

    def test_the_watchdog_stops_everything_on_a_kernel_incident(self):
        clock = FakeClock()
        cap = levered('cpu-cap', 'none', 'none', ('2000', 'none'))
        wifi = levered('wifi-ps', 'on', 'on', ('off', 'on'))
        boost = levered('boost', '1', '1', ('0', '1'))
        experiment_module.ENABLED.parent.mkdir(parents=True, exist_ok=True)
        experiment_module.ENABLED.touch()
        experiment_module.prefer(settings=['cpu-cap', 'wifi-ps'])
        # 30 minutes in, Bluetooth times out and the xHCI controller dies 5 s later.
        clock.hooks.append(lambda now: (self.box.journal.add(2800, 'Bluetooth: hci0: command 0x0405 tx timeout'),
                                        self.box.journal.add(2805, 'xhci_hcd 0000:c1:00.3: HC died; cleaning up'))
                           if now == 2810 else None)
        final, _ = self.run_automatic({'cpu-cap': cap, 'wifi-ps': wifi, 'boost': boost}, clock, 1000 + 5 * 3600,
                                      ['cpu-cap', 'wifi-ps'], enabled=lambda: experiment_module.ENABLED.exists())
        self.assertLess(clock.now, 2810 + 2 * experiment_module.WATCH_EVERY)  # Stopped at once, not 10 h later.
        self.assertEqual((cap.value, wifi.value, boost.value), ('none', 'on', '1'))
        self.assertEqual(final['watchdog']['reason'], 'Bluetooth command timeout; xHCI controller died')
        self.assertEqual(set(experiment_module.quarantined()), {'cpu-cap', 'wifi-ps'})
        self.assertFalse(experiment_module.ENABLED.exists())
        self.assertEqual(len(self.box.notes), 1)
        self.assertEqual(experiment_module.pool({'cpu-cap': cap, 'wifi-ps': wifi}), [])

    def test_the_watchdog_checks_the_last_boot_before_the_first_block(self):
        cap = levered('cpu-cap', '2000', 'none', ('2000', 'none'))
        # The last boot: a run set the cap and was still running when the machine reset.
        self.box.boot = 'b1'
        ledger = Ledger(clock=lambda: 5000.0)
        ledger.start('auto')
        ledger.change('cpu-cap', '2000', False, 'auto')
        ledger.update(last=8000.0)
        self.box.boot = 'b0'
        self.box.journal.previous = ('b1', 8030.0)
        self.box.journal.add(9000, 'x86/amd: Previous system reset reason [0x08000800]: an uncorrected error caused '
                                   'a data fabric sync flood event')
        blocks = []
        final, _ = self.run_automatic({'cpu-cap': cap}, FakeClock(9100), 99999, ['cpu-cap'],
                                      choose=lambda *a: blocks.append(a) or ({'cpu-cap': True}, {}))
        self.assertEqual(blocks, [])
        self.assertIn('sync flood', final['watchdog']['reason'])
        self.assertEqual(final['watchdog']['quarantined'], ['cpu-cap'])  # Off stock when it crashed.
        self.assertEqual(cap.history, [])  # Nothing changed in this boot (the reboot reset the cap): nothing written.
        # Handled: the next start runs, and an incident outside any run doesn't count.
        experiment_module.release(['all'])
        self.box.journal.add(9500, 'xhci_hcd 0000:c1:00.3: HC died; cleaning up')
        final, _ = self.run_automatic({'cpu-cap': cap}, FakeClock(9600), 9600 + 600, ['cpu-cap'])
        self.assertIsNone(final['watchdog'])
        self.assertGreater(final['blocks'], 0)

    def test_a_blind_watchdog_runs_nothing(self):
        self.box.journal.blind = True
        cap = levered('cpu-cap', 'none', 'none', ('2000', 'none'))
        clock = FakeClock()
        self.run_automatic({'cpu-cap': cap}, clock, 1000 + 600, ['cpu-cap'])
        self.assertEqual(cap.history, [])
        self.assertEqual(self.box.state()['reason'], 'the watchdog cannot read the kernel log')
        with self.assertRaises(RuntimeError):
            self.run_experiment(cap, 8)

    def test_wifi_power_saving_goes_back_to_what_was_found(self):
        stock, live = experiment_module.config.SETTINGS['stock'], {'ps': 'off'}

        def run(command, check=True, **_):  # iw, and wattif-helper wifips.
            if command[:2] == ['iw', 'dev']:
                return f"\tPower save: {live['ps']}"
            live['ps'] = command[-1]
            return ''
        self.assertEqual(experiment_module.wifi_stock(False, 'wlan0'), 'on')  # Never seen anywhere: the default.
        spec = self.machine({**MINIMAL, 'class/net/wlan0/wireless/': ''}, ['iw'])['wifi-ps']
        with mock.patch.object(experiment_module, 'run', run):
            self.assertEqual(spec.stock(), 'off')  # Untouched: as it is (the driver's, NetworkManager's).
            live['ps'] = 'on'
            self.assertEqual(spec.stock(), 'on')  # Changed by you or your system: as it is now.
            experiment_module.change(spec, 'off', Ledger(), 'manual')  # wattif's first change saves it first,
            self.assertEqual((spec.stock(), live['ps']), ('on', 'off'))  # and its own value never counts.
            self.assertEqual(spec.stock(False), 'on')  # Not seen on AC: as found on battery.
            with mock.patch.dict(stock, {'wifi_ps': {'battery': 'off', 'ac': 'found'}}):
                self.assertEqual((spec.stock(), spec.stock(False)), ('off', 'on'))  # Configured wins; "found" is None.
            with mock.patch.dict(stock, {'wifi_ps': 'maybe'}), self.assertRaises(ValueError):
                spec.stock()
            self.box.boot = 'b2'  # After a reboot: as it is again, until wattif changes it in that boot.
            self.assertEqual(spec.stock(), 'off')

    def test_corrected_pcie_errors_count_only_after_a_link_change_on_a_quiet_boot(self):
        message = 'pcieport 0000:00:1c.0: AER: Correctable error message received from 0000:01:00.0'
        legacy = 'pcieport 0000:00:08.1: AER: Corrected error received: 0000:c1:00.0'
        now = [1000.0]
        ledger = Ledger(clock=lambda: now[0])
        ledger.start('auto')
        ledger.change('cpu-cap', '2000', False, 'auto')  # Not a link setting.
        entry = lambda t, m=message: {'t': t, 'boot': 'b0', 'message': m}
        quiet = lambda _boot: [entry(1200)]
        self.assertEqual(experiment_module.corrected([entry(1200)], quiet, ledger), [])
        now[0] = 1100.0
        ledger.change('aspm', 'powersupersave', False, 'auto')
        found = experiment_module.corrected([entry(1200), entry(1201, legacy)], quiet, ledger)
        self.assertEqual([f['what'] for f in found], [experiment_module.CORRECTED_WHAT] * 2)
        # A boot that logged them before any change has them anyway: background, not a sign.
        noisy = lambda _boot: [entry(500), entry(1200)]
        self.assertEqual(experiment_module.corrected([entry(1200)], noisy, ledger), [])
        self.assertIsNone(experiment_module.classify(entry(1200)))  # Never an incident by itself.

    def test_kernel_messages_that_trip_the_watchdog(self):
        trips = ['xhci_hcd 0000:c1:00.3: HC died; cleaning up', 'Bluetooth: hci0: command tx timeout',
                 'Bluetooth: hci0: command 0x0405 tx timeout',
                 'pcieport 0000:00:08.1: PCIe Bus Error: severity=Uncorrected (Non-Fatal)',
                 'pcieport 0000:00:1c.0: AER: Uncorrectable (Fatal) error message received from 0000:01:00.0',
                 'amdgpu 0000:c1:00.0: amdgpu: ring gfx_0.0.0 timeout, signaled seq=2718238, emitted seq=2718240',
                 'nvme nvme0: I/O tag 12 (100c) opcode 0x2 (Read) QID 3 timeout, aborting',
                 'nvme nvme0: controller is down; will reset: CSTS=0xffffffff']
        harmless = ['pcieport 0000:00:03.1: AER: enabled with IRQ 39',
                    'pcieport 0000:00:08.1: AER: Corrected error received: 0000:c1:00.0',  # See corrected().
                    'acpi PNP0A08:00: _OSC: platform does not '
                    'support [AER]', 'nvme nvme0: 16/0/0 default/read/poll queues', 'xhci_hcd 0000:c1:00.3: xHCI '
                    'Host Controller']
        self.assertTrue(all(experiment_module.classify({'message': m}) for m in trips))
        self.assertFalse(any(experiment_module.classify({'message': m}) for m in harmless))
        lines = '\n'.join(json.dumps(e) for e in (
            {'__REALTIME_TIMESTAMP': '1792078367234062', '_BOOT_ID': 'b1', 'MESSAGE': trips[0]},
            {'__REALTIME_TIMESTAMP': '1792078367000000', '_BOOT_ID': 'b1', 'MESSAGE': list(b'HC died \xff')}))
        result = subprocess.CompletedProcess([], 0, stdout=lines, stderr='')
        with mock.patch.object(experiment_module.subprocess, 'run', return_value=result):
            entries = JOURNAL.entries(['-k'])
        self.assertEqual(entries[0], {'t': 1792078367.234062, 'boot': 'b1', 'message': trips[0]})
        self.assertTrue(entries[1]['message'].startswith('HC died'))
        with mock.patch.object(experiment_module.subprocess, 'run',
                               return_value=subprocess.CompletedProcess([], 1, stdout='', stderr='')):
            self.assertEqual(JOURNAL.entries(['-k']), [])  # 1: nothing matched.

    def test_enable_refuses_once_after_an_unhandled_incident(self):
        cap = levered('cpu-cap', '2000', 'none', ('2000', 'none'))
        self.box.specs = {'cpu-cap': cap}
        ledger = Ledger(clock=lambda: 1000.0)
        ledger.start('manual')
        ledger.change('cpu-cap', '2000', False, 'manual')
        self.box.journal.add(1010, 'xhci_hcd 0000:c1:00.3: HC died; cleaning up')
        with mock.patch.object(experiment_module, 'run', lambda *a, **k: ''):
            with self.assertRaises(SystemExit) as refused:
                experiment_module.main(['enable'])
            self.assertIn('xHCI controller died', str(refused.exception))
            self.assertFalse(experiment_module.ENABLED.exists())
            self.assertEqual(cap.value, 'none')
            experiment_module.main(['enable'])  # Seen now: enabling again is explicit.
        self.assertTrue(experiment_module.ENABLED.exists())

    # --- Pools, quarantine, risky settings ------------------------------------------------------

    def specs_for_pool(self):
        return {name: levered(name, 'x', 'x', ('y', 'x')) for name in
                ('aspm', 'apst', 'boost', 'gpu-dpm', 'cpu-cap', 'wifi-ps')}

    def test_risky_settings_stay_out_of_the_pool_unless_asked_for(self):
        self.box.specs = specs = self.specs_for_pool()
        self.assertEqual(experiment_module.pool(specs), ['boost', 'cpu-cap', 'wifi-ps'])
        experiment_module.prefer(settings=['aspm', 'apst', 'boost'])  # An old pool, from before.
        self.assertEqual(experiment_module.pool(specs), ['boost'])
        with mock.patch('sys.stdout'), self.assertRaises(SystemExit) as refused:
            experiment_module.main(['pool', 'gpu-dpm', 'cpu-cap'])  # The panel's checkbox, no --risky.
        self.assertIn('gpu-dpm is risky', str(refused.exception))
        self.assertEqual(experiment_module.pool(specs), ['cpu-cap'])
        with mock.patch('sys.stdout'):
            experiment_module.main(['pool', 'add', 'aspm', '--risky'])
        self.assertEqual(experiment_module.pool(specs), ['cpu-cap', 'aspm'])
        self.assertEqual(experiment_module.opted_in(), ['aspm'])
        with mock.patch('sys.stdout'):
            experiment_module.main(['pool', 'aspm', 'cpu-cap', 'boost'])  # The panel again: the opt-in stands.
            self.assertEqual(experiment_module.pool(specs), ['aspm', 'cpu-cap', 'boost'])
            experiment_module.main(['pool', 'remove', 'aspm'])
        self.assertEqual((experiment_module.pool(specs), experiment_module.opted_in()), (['cpu-cap', 'boost'], []))

    def test_recommendations_leave_out_whatever_the_pool_does(self):
        self.box.specs = specs = self.specs_for_pool()
        experiment_module.prefer(settings=['aspm', 'boost', 'wifi-ps'], quarantine={'wifi-ps': {'reason': 'HC died'}})
        left = experiment_module.unrecommended(specs)
        self.assertTrue({'aspm', 'apst', 'gpu-dpm', 'wifi-ps', 'cpu-cap'} <= set(left))  # Risky, quarantined, not chosen.
        self.assertNotIn('boost', left)
        with mock.patch('sys.stdout'):
            experiment_module.main(['pool', 'add', 'aspm', '--risky'])
        self.assertNotIn('aspm', experiment_module.unrecommended(specs))

    def test_quarantined_settings_stay_out_until_released(self):
        self.box.specs = specs = self.specs_for_pool()
        experiment_module.prefer(quarantine={'wifi-ps': {'reason': 'xHCI controller died'}})
        self.assertNotIn('wifi-ps', experiment_module.pool(specs))
        with mock.patch('sys.stdout'), self.assertRaises(SystemExit):
            experiment_module.main(['pool', 'add', 'wifi-ps'])
        self.assertEqual(experiment_module.command_apply(['wifi-ps=y'], battery=lambda: True),
                         ['wifi-ps (quarantined: `wattif experiment release wifi-ps`)'])
        self.assertEqual(specs['wifi-ps'].value, 'x')
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            experiment_module.main(['start', 'wifi-ps'])
        with mock.patch('sys.stdout'):
            experiment_module.main(['release', 'wifi-ps'])
        self.assertIn('wifi-ps', experiment_module.pool(specs))
        self.assertEqual(experiment_module.quarantined(), {})

    def test_apply_and_goals_leave_risky_settings_at_stock(self):
        self.box.specs = specs = self.specs_for_pool()
        refused = experiment_module.command_apply(['aspm=saving', 'cpu-cap=saving', 'boost=normal'],
                                                  battery=lambda: True)
        self.assertEqual(refused, ['aspm (risky: add --risky)'])
        self.assertEqual((specs['aspm'].value, specs['cpu-cap'].value), ('x', 'y'))
        experiment_module.command_apply(['aspm=saving'], risky=True, battery=lambda: True)
        self.assertEqual(specs['aspm'].value, 'y')
        best = {'battery': {'apply': ['aspm=saving', 'apst=normal']}, 'comfort': {'apply': []}}
        with mock.patch.object(experiment_module, 'command_apply') as apply:
            experiment_module.apply_goal('battery', recommend=lambda now: best)
            apply.assert_called_once_with(['aspm=saving', 'apst=normal'], risky=False, left=[])
            experiment_module.apply_goal('comfort', recommend=lambda now: best)  # Nothing measured.
            experiment_module.write_state({'mode': 'auto', 'status': 'paused'})
            experiment_module.apply_goal('battery', recommend=lambda now: best)
            self.assertEqual(apply.call_count, 1)

    def test_applied_settings_follow_the_power_source(self):
        self.box.specs = specs = self.specs_for_pool()
        ledger = Ledger()
        experiment_module.command_apply(['cpu-cap=saving'], battery=lambda: False, ledger=ledger)
        self.assertEqual(specs['cpu-cap'].value, 'x')  # On AC: not now.
        experiment_module.command_reconcile(battery=lambda: True, ledger=ledger)
        self.assertEqual(specs['cpu-cap'].value, 'y')  # Unplugged: set.
        specs['cpu-cap'].value = 'z'  # You change it on battery: no transition, left alone.
        experiment_module.command_reconcile(battery=lambda: True, ledger=ledger)
        self.assertEqual(specs['cpu-cap'].value, 'z')
        experiment_module.command_reconcile(battery=lambda: False, ledger=ledger)
        self.assertEqual(specs['cpu-cap'].value, 'x')  # Plugged in: stock.
        experiment_module.command_reconcile(battery=lambda: True, ledger=ledger)
        self.assertEqual(specs['cpu-cap'].value, 'y')
        self.box.boot = 'b2'  # A reboot ends it.
        self.assertEqual(experiment_module.load_applied(), {})

    def test_disable_puts_back_every_setting_wattif_changed(self):
        self.box.specs = specs = self.specs_for_pool()
        experiment_module.command_apply(['cpu-cap=saving', 'gpu-dpm=saving'], risky=True, battery=lambda: True)
        specs['wifi-ps'].value = 'y'  # A crashed run's leftover,
        Ledger().change('wifi-ps', 'y', False, 'auto')
        specs['boost'].value = 'y'  # and something else's doing: not wattif's to put back.
        experiment_module.ENABLED.parent.mkdir(parents=True, exist_ok=True)
        experiment_module.ENABLED.touch()
        with mock.patch.object(experiment_module, 'run', lambda *a, **k: ''):
            experiment_module.main(['disable'])
        self.assertEqual({name: spec.value for name, spec in specs.items()}, dict(dict.fromkeys(specs, 'x'), boost='y'))
        self.assertEqual(specs['boost'].history, [])
        self.assertFalse(experiment_module.APPLIED.exists())
        self.assertFalse(experiment_module.ENABLED.exists())

    def test_reconcile_restores_what_a_crashed_run_left_and_waits_for_a_run(self):
        self.box.specs = specs = self.specs_for_pool()
        ledger = Ledger()
        specs['cpu-cap'].value = 'y'
        ledger.change('cpu-cap', 'y', False, 'auto')  # Then the runner was killed.
        with experiment_module.ownership():  # A run is active: reconcile leaves it alone.
            experiment_module.main(['reconcile'])
            self.assertEqual(specs['cpu-cap'].value, 'y')
        experiment_module.main(['reconcile'])
        self.assertEqual(specs['cpu-cap'].value, 'x')
        self.assertEqual(experiment_module.leftovers(ledger), [])

    def test_the_profile_is_yours_only_when_nothing_of_ours_holds_it(self):
        clock, live = [1000.0], {}

        def capture(on, value):
            live['profile'] = value
            with mock.patch.object(experiment_module, 'profile_get', lambda: live['profile']):
                experiment_module.capture_profile(lambda: on, now=clock[0])
        capture(False, 'performance')  # First sight of AC: wait for udev.
        self.assertEqual(experiment_module.own_profile(False), 'balanced')
        clock[0] += 61
        capture(False, 'performance')
        self.assertEqual((experiment_module.own_profile(False), experiment_module.own_profile(True)),
                         ('performance', 'power-saver'))  # Never what AC had, on battery.
        clock[0] += 400
        capture(True, 'balanced')
        clock[0] += 61
        Ledger().change('profile', 'balanced', False, 'auto')  # A run's leftover is not yours.
        capture(True, 'balanced')
        self.assertEqual(experiment_module.own_profile(True), 'power-saver')

    def test_stock_values_come_from_the_command_line(self):
        self.enterContext(self.authors_stock())  # Its udev rule: ABM 3 on battery, 0 on AC.
        catalog = self.real_catalog()
        with mock.patch.object(experiment_module, 'run', return_value='\tPower save: on'):  # iw, for Wi-Fi's stock.
            stock = {name: spec.stock_or_none(True) for name, spec in catalog.items() if name != 'refresh'}
        self.assertEqual(stock, {'aspm': 'performance', 'apst': '0', 'boost': '0', 'abm': '3',
                                 'profile': 'power-saver', 'gpu-dpm': 'auto', 'cpu-cap': 'none',
                                 'throttle-apps': 'off', 'wifi-ps': 'on'})
        self.assertEqual((catalog['abm'].stock(False), catalog['profile'].stock(False)), ('0', 'balanced'))
        (self.box.root / 'cmdline').write_text('quiet pcie_aspm.policy=powersave pcie_aspm.policy=performance\n')
        self.assertEqual((catalog['aspm'].stock(), catalog['apst'].stock()), ('performance', '0'))  # APST: as found.
        (self.box.root / 'cmdline').write_text('quiet\n')
        self.assertEqual(catalog['aspm'].stock(), 'default')  # As found.
        Ledger().change('apst', '0', False, 'auto')  # A change logged before anything was saved (an older wattif):
        self.assertEqual(catalog['apst'].stock(), experiment_module.APST_DEFAULT)  # nothing better than the guess.
        self.assertEqual(experiment_module.HELPER, ['sudo', '-n', 'wattif-helper'])  # config [helper] command's default.
        self.assertEqual(experiment_module.RISKY, ('aspm', 'apst', 'gpu-dpm'))

    def real_catalog(self):
        """The real catalog over the sandbox's /sys (AMD_LAPTOP) and command line."""
        return self.machine(AMD_LAPTOP, EVERYTHING, [experiment_module.THROTTLE_UNIT])

    def machine(self, files, commands=(), units=()):
        """The real catalog on a fake machine: its /sys, the commands on PATH and the user units."""
        tree(self.box.root / 'sys', files)
        self.box.commands, self.box.units = set(commands), set(units)
        return CATALOG()

    @staticmethod
    def authors_stock():
        """config [stock] as the original author's udev rules set them."""
        return mock.patch.dict(config.SETTINGS['stock'], abm={'battery': 3, 'ac': 0})

    # --- Settings on other machines ----------------------------------------------------------------

    def test_an_amd_laptop_has_every_setting(self):
        catalog = self.real_catalog()
        self.assertEqual(list(catalog), ['aspm', 'apst', 'boost', 'abm', 'profile', 'gpu-dpm', 'cpu-cap',
                                         'throttle-apps', 'wifi-ps'])  # No compositor: no refresh rate.
        self.assertEqual((catalog['boost'].read(), catalog['cpu-cap'].read(), catalog['abm'].read()), ('1', 'none', '3'))
        self.assertIn('2000 MHz (this CPU runs at 405–4972 MHz)', catalog['cpu-cap'].description)
        self.assertIn('Risky', catalog['aspm'].description)
        self.assertNotIn('Risky', catalog['boost'].description)
        # power-profiles-daemon turns boost off with power-saver on amd-pstate: its stock follows the profile.
        self.assertEqual(experiment_module.battery_stock(), {'profile': 'power-saver'})  # Nothing seen yet.
        self.assertEqual(catalog['boost'].stock(), '0')
        with mock.patch.object(experiment_module, 'profile_get', lambda: 'balanced'):
            self.assertEqual(catalog['boost'].stock(), '1')
        experiment_module.note_found(catalog['abm'])  # Saved before a first change (or by capture_profile).
        # What report.own_settings takes for battery without a battery minute outside experiments.
        self.assertEqual(experiment_module.battery_stock(), {'profile': 'power-saver', 'abm': '3', 'boost': '0'})
        experiment_module.prefer(risky=['aspm'])
        self.assertEqual(experiment_module.pool(catalog), ['boost', 'abm', 'profile', 'cpu-cap', 'throttle-apps',
                                                           'wifi-ps'])

    def test_an_intel_laptop_has_what_intel_pstate_and_its_drivers_offer(self):
        catalog = self.machine(INTEL_LAPTOP, EVERYTHING)
        self.assertEqual(list(catalog), ['aspm', 'apst', 'boost', 'profile', 'cpu-cap', 'wifi-ps'])
        self.assertIn('this CPU runs at 400–4700 MHz', catalog['cpu-cap'].description)
        no_turbo = self.box.root / 'sys' / CPU / 'intel_pstate/no_turbo'
        calls = []

        def helper(command, check=True, **_):  # wattif-helper boost: intel_pstate's no_turbo, inverted.
            calls.append(command)
            no_turbo.write_text(f'{1 - int(command[-1])}\n')
            return ''
        boost = catalog['boost']
        self.assertEqual((boost.read(), boost.stock(), boost.stock(False)), ('1', '1', '1'))  # Power-saver keeps turbo.
        self.assertEqual(experiment_module.battery_stock(), {'profile': 'power-saver', 'boost': '1'})
        with mock.patch.object(experiment_module, 'run', helper):
            experiment_module.change(boost, '0', Ledger(), 'manual')
        self.assertEqual((no_turbo.read_text(), boost.read(), calls), ('1\n', '0', [experiment_module.HELPER + ['boost', '0']]))
        self.assertTrue(ON_BATTERY())
        tree(self.box.root / 'sys', {'class/power_supply/AC/online': '1', 'class/power_supply/BAT0/status': 'Charging'})
        self.assertFalse(ON_BATTERY())
        (self.box.root / 'sys/class/power_supply/AC').rename(self.box.root / 'sys/class/power_supply/ucsi-source-psy')
        self.assertFalse(ON_BATTERY())  # USB-C power only: the mouse's battery discharging is not the laptop's.
        tree(self.box.root / 'sys', {f'{CPU}intel_pstate/turbo_pct': '0'})  # No turbo frequencies (or none left).
        self.assertNotIn('boost', CATALOG())

    def test_a_machine_with_nothing_to_set_pauses_instead_of_failing(self):
        specs = self.machine(MINIMAL)
        self.assertEqual(specs, {})
        self.assertEqual(experiment_module.pool(specs), [])
        clock, path, states = FakeClock(), self.box.runtime / 'experiment.json', []
        clock.hooks.append(lambda now: states.append(json.loads(path.read_text())))
        automatic(specs, clock=clock, battery=lambda: True, locked=lambda: True, resting=lambda: 0.0,
                  enabled=lambda: clock.now < 1000 + 120, state_path=path, version=lambda: 'Linux 6.18, BIOS 1',
                  choose=lambda *a: self.fail('nothing to plan'))
        self.assertEqual({(s['status'], s['reason']) for s in states}, {('paused', 'no settings to test')})
        self.assertEqual((self.box.state()['status'], self.box.state()['pending']), ('finished', []))
        self.assertEqual(experiment_module.restore_settings(specs, ['boost'], lambda: True), {'boost': 'unavailable'})

    def test_stock_values_come_from_configuration(self):
        catalog = self.real_catalog()
        abm, panel = catalog['abm'], self.box.root / 'sys/class/drm/card1-eDP-1/amdgpu/panel_power_savings'
        # By default ABM is as found: as it is on battery while wattif hasn't changed it, left to the system on AC.
        self.assertEqual((abm.stock(True), abm.stock(False), abm.ac_managed, bool(abm.reset)), ('3', None, True, True))
        panel.write_text('1\n')
        self.assertEqual(abm.stock(True), '1')  # Until wattif first changes it,
        with mock.patch.object(experiment_module, 'run', lambda command, check=True, **_: panel.write_text('0\n')):
            experiment_module.change(abm, '0', Ledger(), 'manual')
        self.assertEqual(self.box.prefs()['found'], {'abm': {'battery': '1'}})
        self.assertEqual((abm.stock(True), panel.read_text()), ('1', '0\n'))  # then kept: never its own value.
        # The profile, by default, too: on AC left to your system (not put back to balanced).
        self.assertTrue(catalog['profile'].ac_managed)
        self.assertEqual((catalog['profile'].stock(True), catalog['profile'].stock(False)), ('power-saver', 'balanced'))
        with mock.patch.dict(config.SETTINGS['stock'], profile={'battery': 'balanced', 'ac': None},
                             abm={'battery': 2, 'ac': 0}, boost={'battery': 0, 'ac': 1}), \
                mock.patch.object(experiment_module, 'CPU_CAP', '1600'):
            catalog = CATALOG()
            self.assertEqual({n: (catalog[n].stock(True), catalog[n].stock(False)) for n in ('profile', 'abm', 'boost')},
                             {'profile': ('balanced', 'balanced'), 'abm': ('2', '0'), 'boost': ('0', '1')})
            self.assertEqual((catalog['abm'].ac_managed, catalog['abm'].reset, catalog['profile'].ac_managed),
                             (False, None, True))
            self.assertEqual(catalog['cpu-cap'].levels(), ('1600', 'none'))
            self.assertIn('1600 MHz', catalog['cpu-cap'].description)
        with mock.patch.dict(config.SETTINGS['stock'], cpu_cap_mhz=300):  # Below what the CPU runs at.
            with mock.patch.object(experiment_module, 'CPU_CAP', '300'):
                self.assertNotIn('cpu-cap', CATALOG())
            from wattif import report
            self.assertEqual(report.cap_phrase(), 'CPU capped at 0.3 GHz')

    def test_abm_goes_back_to_its_configured_value_or_as_found(self):
        catalog, panel, calls = self.real_catalog(), self.box.root / 'sys/class/drm/card1-eDP-1/amdgpu/panel_power_savings', []
        helper = len(experiment_module.HELPER)

        def run(command, check=True, **_):  # No udev rule sets ABM here: the trigger changes nothing.
            calls.append(command[helper:])
            if command[helper] == 'abm':
                panel.write_text(command[-1] + '\n')
            return ''
        restore = lambda specs, on=True: experiment_module.restore_settings(specs, ['abm'], lambda: on, Ledger())
        with mock.patch.object(experiment_module, 'run', run):
            Ledger().change('abm', '0', False, 'auto')  # A block set 0 before ABM was ever seen.
            panel.write_text('0\n')
            self.assertEqual((restore(catalog), calls), ({}, [['trigger', 'power_supply']]))
            self.assertEqual(panel.read_text(), '0\n')  # Nothing to go back to but the system's policy.
            experiment_module.remember_found('abm', True, '3')  # Seen before the next change.
            calls.clear()
            self.assertEqual((restore(catalog), calls), ({}, [['trigger', 'power_supply'], ['abm', '3']]))
            with mock.patch.dict(config.SETTINGS['stock'], abm={'battery': 3, 'ac': None}):  # Configured on battery:
                catalog = CATALOG()
                panel.write_text('1\n')
                calls.clear()
                self.assertEqual((restore(catalog), calls), ({}, [['abm', '3']]))  # written, no trigger;
                self.assertEqual((restore(catalog, on=False), calls), ({}, [['abm', '3']]))  # AC: the system's.
            self.enterContext(self.authors_stock())
            catalog = CATALOG()
            panel.write_text('1\n')
            calls.clear()
            self.assertEqual((restore(catalog), calls), ({}, [['abm', '3']]))  # Configured: written, no trigger.
            self.assertEqual((restore(catalog, on=False), panel.read_text()), ({}, '0\n'))

    def test_the_refresh_rate_through_niri_or_sway(self):
        socket, calls, outputs = self.box.root / 'compositor.sock', [], {'niri': NIRI_OUTPUTS, 'sway': SWAY_OUTPUTS}
        socket.touch()

        def run(command, check=True, **_):
            calls.append(command)
            if command[:3] == ['niri', 'msg', '-j'] or command[:2] == ['swaymsg', '-r']:
                return json.dumps(outputs[command[0].removesuffix('msg')])
            return ''
        with mock.patch.object(experiment_module, 'run', run):
            self.assertNotIn('refresh', self.real_catalog())  # No compositor that sets modes.
            os.environ['NIRI_SOCKET'] = str(socket)
            spec = CATALOG()['refresh']
            self.assertEqual((spec.read(), spec.levels(), spec.alternative(spec.read()), spec.stock()),
                             ('2256x1504@59.999', ('2256x1504@47.998', '2256x1504@59.999'), '2256x1504@47.998',
                              '2256x1504@59.999'))
            spec.set('2256x1504@47.998')
            self.assertEqual(calls[-1], ['niri', 'msg', 'output', 'eDP-1', 'mode', '2256x1504@47.998'])
            del os.environ['NIRI_SOCKET']
            os.environ['SWAYSOCK'] = str(socket)
            spec = CATALOG()['refresh']
            self.assertEqual((spec.read(), spec.levels()), ('2560x1600@120.000', ('2560x1600@60.000', '2560x1600@120.000')))
            spec.set('2560x1600@60.000')
            self.assertEqual(calls[-1], ['swaymsg', 'output', 'eDP-1', 'mode', '2560x1600@60.000Hz'])
            outputs['sway'] = [dict(SWAY_OUTPUTS[0], modes=SWAY_OUTPUTS[0]['modes'][:1])]
            self.assertNotIn('refresh', CATALOG())  # A panel with one refresh rate.
            socket.unlink()
            self.assertIsNone(experiment_module.compositor())  # A stale variable: no compositor.

    def test_the_screen_is_locked_by_a_locker_or_by_logind(self):
        proc, calls, hint = self.box.root / 'proc', [], ['no']
        tree(proc, {'412/comm': 'niri', '977/comm': 'gtklock'})

        def run(command, check=True, **_):
            calls.append(command)
            return '3 7' if command[1] == 'show-user' else f'LockedHint=no\n\nLockedHint={hint[0]}'
        with mock.patch.object(experiment_module, 'run', run):
            self.assertTrue(experiment_module.screen_locked())
            self.assertEqual(calls, [])  # A locker runs: logind isn't asked.
            (proc / '977/comm').write_text('foot\n')
            self.assertFalse(experiment_module.screen_locked())
            hint[0] = 'yes'  # GNOME or KDE, say: they tell logind.
            self.assertTrue(experiment_module.screen_locked())
            self.assertEqual(calls[-2:], [['loginctl', 'show-user', str(os.getuid()), '-p', 'Sessions', '--value'],
                                          ['loginctl', 'show-session', '3', '7', '-p', 'LockedHint']])
            os.environ['XDG_SESSION_ID'] = '7'
            self.assertTrue(experiment_module.screen_locked())
            self.assertEqual(calls[-1], ['loginctl', 'show-session', '7', '-p', 'LockedHint'])
            (proc / '977/comm').write_text('gtklock\n')
            hint[0] = 'no'
            with mock.patch.dict(config.SETTINGS['session'], lockers=['swaylock']):
                self.assertFalse(experiment_module.screen_locked())

    def test_boot_arms_come_from_configuration(self):
        calls = []
        with mock.patch.object(experiment_module, 'run', lambda command, check=True, **_: calls.append(command) or ''), \
                mock.patch('sys.stdout'):
            self.assertIsNone(experiment_module.psr_arm())
            with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
                experiment_module.main(['next-boot', 'psr'])  # Not configured: no such arm.
            experiment_module.main(['next-boot', 'default'])
            arms = {'psr': 'psr', 'quiet': 'no-splash', 'bad': 'Not an entry!', 'list': 'x'}
            with mock.patch.dict(config.SETTINGS['experiments'], boot_arms=arms):
                self.assertEqual(experiment_module.boot_arms(), {'psr': 'psr', 'quiet': 'no-splash'})
                experiment_module.main(['next-boot', 'quiet'])
                tree(self.box.root / 'sys', {'module/amdgpu/parameters/dcdebugmask': '0x10'})
                self.assertEqual(experiment_module.psr_arm(), 'default')
        self.assertEqual([c[len(experiment_module.HELPER):] for c in calls],
                         [['next-boot', 'default'], ['next-boot', 'no-splash']])

    def test_close_every_app_keeps_what_configuration_says(self):
        kept = []
        with mock.patch.object(experiment_module, 'freeze_targets', lambda keep: kept.append(keep) or []), \
                mock.patch.object(experiment_module, 'test_freeze', lambda units, seconds: []), \
                mock.patch.object(experiment_module, 'uncovered', lambda: []), mock.patch('sys.stdout'):
            experiment_module.main(['test-freeze'])
            with mock.patch.dict(config.SETTINGS['experiments'], freeze_keep=['t3code']):
                experiment_module.main(['test-freeze'])
                experiment_module.main(['test-freeze', '--keep', 'floorp'])
        self.assertEqual(kept, [[], ['t3code'], ['floorp']])

    def test_a_boot_without_a_reset_reason_is_judged_by_its_shutdown(self):
        # Intel (and older AMD) kernels print no "Previous system reset reason".
        self.unended_run(value='none')
        for message in ('intel_pstate: Intel P-state driver initializing', 'nvme nvme0: 8/0/0 default/read/poll queues',
                        'i915 0000:00:02.0: [drm] GuC firmware i915/tgl_guc_70.bin version 70.20.0'):
            self.box.journal.add(9000, message)
        self.assertEqual(Watchdog(journal=self.box.journal, ledger=Ledger()).preflight(0), [])  # A clean shutdown.
        self.box.journal.shutdown = False  # None logged: a battery cut or a hard power-off, say.
        found = Watchdog(journal=self.box.journal, ledger=Ledger()).preflight(0)
        self.assertEqual([(f['quarantine'], 'without a shutdown' in f['what']) for f in found], [(False, True)])

    def test_the_reports_tables_name_the_catalogs_settings(self):
        from wattif import report
        catalog = self.real_catalog()
        names = set(catalog) | {'refresh'}  # And the refresh rate, with a compositor.
        self.assertEqual(set(report.SETTING), names)
        self.assertEqual(set(report.PHRASES), names)
        self.assertEqual(report.NOTICEABLE, {n for n, spec in catalog.items() if spec.visible} | {'refresh'})
        self.assertLessEqual(report.CPU_LEVERS | set(report.MEDIATORS) | report.AC_MANAGED, names)

    # --- Review fixes (October 16) ---------------------------------------------------------------

    def test_a_manual_risky_run_keeps_the_hold_and_daily_limit_when_blocks_are_cut_short(self):
        # AC every other 5 minutes cut each 45 min block short: 144 ASPM writes a day before.
        clock = FakeClock()
        aspm = levered('aspm', 'performance', 'performance', ('powersupersave', 'performance'))
        writes = []
        aspm.set = lambda v, original=aspm.set: (writes.append((clock.now, v)), original(v))
        battery = lambda: int((clock.now - 1000) // 300) % 2 == 0
        state, _ = self.run_experiment(aspm, 24 * 60, battery=battery, clock=clock,
                                       block=max(experiment_module.BLOCK, experiment_module.RISKY_HOLD))
        day = [t for t, _ in writes if t < 1000 + 86400]
        self.assertLessEqual(len(day), experiment_module.RISKY_DAILY)
        offs = [t for t, v in writes if v == 'powersupersave']
        self.assertTrue(offs)  # It still runs.
        self.assertTrue(all(b - a >= experiment_module.RISKY_HOLD for a, b in zip(offs, offs[1:])))
        self.assertEqual(aspm.value, 'performance')

    def test_a_manual_run_on_ac_backs_off_a_failing_restore(self):
        # A restore failing every 15 s while paused on AC: 241 writes an hour before.
        clock = FakeClock()
        cap = levered('cpu-cap', '2000', 'none', ('2000', 'none'))
        attempts = []

        def broken(value):
            attempts.append(clock.now)
            raise RuntimeError('wattif-helper: Invalid argument')
        cap.set = broken
        with mock.patch.object(experiment_module, 'GIVE_UP', 3600):
            state, _ = self.run_experiment(cap, 60, battery=lambda: False, clock=clock)
        self.assertLessEqual(len(attempts), 8)  # 0, 60, 180, 420, 900, 1860 s, and the final one.
        self.assertEqual((state['status'], state['pending']), ('restore-pending', ['cpu-cap']))

    def test_disable_waits_for_a_slow_stop_and_still_puts_everything_back(self):
        self.box.specs = specs = self.specs_for_pool()
        experiment_module.command_apply(['cpu-cap=saving'], battery=lambda: True)
        experiment_module.ENABLED.parent.mkdir(parents=True, exist_ok=True)
        experiment_module.ENABLED.touch()
        calls = []

        def run(command, check=True, timeout=15):
            calls.append((command, timeout))
            if command[:3] == ['systemctl', '--user', 'stop'] and timeout < 180:  # The stop restores for up to 180 s.
                raise subprocess.TimeoutExpired(command, timeout)
            return ''
        with mock.patch.object(experiment_module, 'run', run):
            experiment_module.main(['disable'])
        self.assertIn((['systemctl', '--user', 'stop', experiment_module.AUTO_UNIT], experiment_module.STOP_TIMEOUT),
                      calls)
        self.assertEqual(specs['cpu-cap'].value, 'x')
        self.assertFalse(experiment_module.APPLIED.exists() or experiment_module.ENABLED.exists())
        with mock.patch.object(experiment_module, 'run', lambda command, check=True, timeout=15: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(command, timeout))), mock.patch('sys.stderr'):
            experiment_module.main(['disable'])  # Even a stop that never ends: no traceback, stock values.

    def test_power_lab_calls_outlast_its_lock_wait(self):
        calls = []
        with mock.patch.object(experiment_module, 'run', lambda command, check=True, timeout=15: calls.append(
                (command, timeout)) or ''):
            experiment_module.lab('cpucap', 'none')
        self.assertEqual(calls, [(experiment_module.HELPER + ['cpucap', 'none'], experiment_module.LAB_TIMEOUT)])
        # wattif-helper waits 10 s for another change's lock, then exits 75 (Busy): well inside the timeout.
        self.assertGreaterEqual(experiment_module.LAB_TIMEOUT, 2 * 10 + 5)

    def test_how_the_last_boot_ended(self):
        ending = experiment_module.ending
        reset = lambda value, text: {'t': 1.0, 'boot': 'b0', 'message':
                                     f'x86/amd: Previous system reset reason [{value}]: {text}'}
        flood = reset('0x08000800', 'an uncorrected error caused a data fabric sync flood event')
        reboot = reset('0x00080800', 'software wrote 0x6 to reset control register 0xCF9')
        poweroff = reset('0x00200800', 'ACPI power state transition occurred')
        button = reset('0x00000802', 'power button was pressed for 4 seconds')
        asleep = [{'t': 5.0, 'boot': 'b1', 'message': 'PM: suspend entry (s2idle)'},
                  {'t': 9.0, 'boot': 'b1', 'message': 'PM: suspend exit'},
                  {'t': 20.0, 'boot': 'b1', 'message': 'PM: suspend entry (s2idle)'}]
        awake = asleep[:2]
        clean, abrupt, unknown = (lambda: True), (lambda: False), (lambda: None)
        self.assertEqual(ending([flood], awake, clean)[1:], (flood['message'], True))  # Awake: quarantine.
        self.assertIn('sync flood (the system crashed)', ending([flood], awake, clean)[0])
        self.assertEqual(ending([flood], asleep, clean)[2], False)  # The s2idle crash: stop only.
        self.assertIn('while asleep', ending([flood], asleep, clean)[0])
        self.assertEqual(ending([button], awake, abrupt)[2], True)  # A hang held off with the power button.
        self.assertEqual(ending([], asleep, clean)[2], False)  # Died asleep, no reason (a silent one).
        self.assertIsNone(ending([reboot], awake, clean))
        self.assertIsNone(ending([poweroff], [], clean))
        self.assertIsNone(ending([], [], clean))  # A cold boot after a clean power-off.
        self.assertEqual(ending([], [], abrupt)[2], False)  # A battery cut: stop, quarantine nothing.
        self.assertEqual(ending([reboot], awake, abrupt)[2], False)  # A panic or SysRq reboot.
        self.assertEqual(ending([], [], unknown)[2], False)

    def unended_run(self, boot='b1', start=5000.0, last=8000.0, value='2000'):
        self.box.boot = boot
        ledger = Ledger(clock=lambda: start)
        ledger.start('auto')
        ledger.change('cpu-cap', value, False, 'auto')
        ledger.update(last=last)
        self.box.boot = 'b0'
        self.box.journal.previous = (boot, last + 30)

    def test_a_run_that_died_asleep_stops_but_quarantines_and_disables_nothing(self):
        cap = levered('cpu-cap', '2000', 'none', ('2000', 'none'))
        self.unended_run()
        self.box.journal.add(7900, 'PM: suspend entry (s2idle)', boot='-1')
        self.box.journal.add(9000, 'x86/amd: Previous system reset reason [0x08000800]: an uncorrected error '
                                   'caused a data fabric sync flood event')
        experiment_module.ENABLED.parent.mkdir(parents=True, exist_ok=True)
        experiment_module.ENABLED.touch()
        blocks = []
        final, _ = self.run_automatic({'cpu-cap': cap}, FakeClock(9100), 99999, ['cpu-cap'],
                                      choose=lambda *a: blocks.append(a) or ({'cpu-cap': True}, {}))
        self.assertEqual(blocks, [])
        self.assertIn('while asleep', final['watchdog']['reason'])
        self.assertEqual((final['watchdog']['quarantined'], experiment_module.quarantined()), ([], {}))
        self.assertTrue(experiment_module.ENABLED.exists())  # On again at the next login.
        self.assertEqual(cap.history, [])  # Nothing of this boot's to put back.
        self.assertIn('nothing (not a sign of any setting)', self.box.notes[0][1])
        final, _ = self.run_automatic({'cpu-cap': cap}, FakeClock(9200), 9200 + 600, ['cpu-cap'],
                                      choose=lambda *a: blocks.append(a) or ({'cpu-cap': True}, {}))
        self.assertIsNone(final['watchdog'])  # Handled: the next start runs.
        self.assertTrue(blocks)

    def test_a_clean_shutdown_with_an_unended_run_does_not_trip(self):
        cap = levered('cpu-cap', 'none', 'none', ('2000', 'none'))
        self.unended_run(value='none')
        self.box.journal.add(9000, 'x86/amd: Previous system reset reason [0x00200800]: ACPI power state transition')
        final, _ = self.run_automatic({'cpu-cap': cap}, FakeClock(9100), 9100 + 600, ['cpu-cap'])
        self.assertIsNone(final['watchdog'])
        self.box.journal.shutdown = False  # Same, but nothing logged a shutdown: a battery cut.
        self.assertFalse(experiment_module.quarantined())
        found = Watchdog(journal=self.box.journal, ledger=Ledger()).preflight(0)
        self.assertEqual([(f['quarantine'], 'without a shutdown' in f['what']) for f in found], [(False, True)])

    def test_a_blind_watchdog_still_puts_a_crashed_runs_leftovers_back(self):
        cap = levered('cpu-cap', '2000', 'none', ('2000', 'none'))
        Ledger().change('cpu-cap', '2000', False, 'auto')  # The last run was killed with the cap on.
        self.box.journal.blind = True
        self.run_automatic({'cpu-cap': cap}, FakeClock(), 1000 + 600, ['cpu-cap'])
        self.assertEqual(cap.value, 'none')  # Before waiting for the kernel log, holding the settings lock.
        self.assertEqual(self.box.state()['reason'], 'the watchdog cannot read the kernel log')

    def test_after_resume_risky_settings_stay_at_stock_until_the_wait_and_the_hold(self):
        clock = FakeClock()
        aspm = levered('aspm', 'performance', 'performance', ('powersupersave', 'performance'))
        experiment_module.prefer(risky=['aspm'])
        writes = []
        aspm.set = lambda v, original=aspm.set: (writes.append((clock.now, v)), original(v))

        def sleep(now):  # 50 min in, a 10 min suspend: the pre-sleep hook resets ASPM, post-resume leaves it.
            if now == 1000 + 3000:
                aspm.value = 'performance'
                clock.suspend(600)
        clock.hooks.append(sleep)
        self.run_automatic({'aspm': aspm}, clock, 1000 + 2 * 3600, ['aspm'],
                           choose=lambda names, present, run: ({'aspm': True}, {}))
        resumed = 1000 + 3600
        after = [t for t, v in writes if t >= resumed and v == 'powersupersave']
        self.assertTrue(after)
        self.assertGreaterEqual(after[0] - resumed, experiment_module.RESUME_SETTLE)

    def test_the_reconciler_watches_applied_settings_and_waits_after_resume(self):
        self.box.specs = specs = self.specs_for_pool()
        with mock.patch.object(experiment_module.time, 'time', lambda: 10_000.0):
            experiment_module.command_apply(['aspm=y', 'cpu-cap=y'], risky=True, battery=lambda: True,
                                            ledger=Ledger(clock=lambda: 10_000.0))
        self.assertEqual((specs['aspm'].value, specs['cpu-cap'].value), ('y', 'y'))
        specs['aspm'].value = 'x'  # A suspend: the pre-sleep hook put it back, post-resume leaves it.
        ledger = Ledger(clock=lambda: 10_000.0 + 3600)
        with mock.patch.object(experiment_module.time, 'time', lambda: 10_000.0 + 3600):
            self.box.journal.resumed = 10_000.0 + 3600 - 20
            experiment_module.command_reconcile(battery=lambda: True, ledger=ledger)
            self.assertEqual(specs['aspm'].value, 'x')  # 20 s after resume: not yet.
            self.box.journal.resumed = 10_000.0 + 3600 - 90
            experiment_module.command_reconcile(battery=lambda: True, ledger=ledger)
            self.assertEqual(specs['aspm'].value, 'y')  # Past RESUME_SETTLE (and the hold): set again.
            specs['cpu-cap'].value = 'z'  # Not risky: you changed it, it stays.
            experiment_module.command_reconcile(battery=lambda: True, ledger=ledger)
            self.assertEqual(specs['cpu-cap'].value, 'z')
            # An xHCI death while they are applied: the reconciler is their watchdog.
            self.box.journal.add(10_000.0 + 3700, 'xhci_hcd 0000:c1:00.3: HC died; cleaning up')
            experiment_module.command_reconcile(battery=lambda: True, ledger=ledger)
        self.assertEqual((specs['aspm'].value, specs['cpu-cap'].value), ('x', 'x'))
        self.assertFalse(experiment_module.APPLIED.exists())
        self.assertIn('aspm', experiment_module.quarantined())
        self.assertEqual(len(self.box.notes), 1)

    def test_the_not_applied_line_keeps_its_form(self):
        # The battery panel parses it (panel_data.refused): 'wattif experiment: not applied: name (why); …'.
        self.box.specs = self.specs_for_pool()
        experiment_module.prefer(quarantine={'wifi-ps': {'reason': 'HC died'}})
        with mock.patch('sys.stderr') as err:
            experiment_module.command_apply(['aspm=y', 'wifi-ps=y', 'cpu-cap=y'], battery=lambda: True)
        written = ''.join(call.args[0] for call in err.write.call_args_list)
        self.assertIn('wattif experiment: not applied: aspm (risky: add --risky); '
                      'wifi-ps (quarantined: `wattif experiment release wifi-ps`)\n', written)

    def test_goal_apply_says_at_once_when_a_run_owns_the_settings_and_what_it_computed(self):
        experiment_module.write_state({'mode': 'auto', 'status': 'running'})
        with experiment_module.ownership(), mock.patch('sys.stderr') as err, mock.patch('sys.stdout'):
            started = time.monotonic()
            experiment_module.main(['goal', 'battery', '--apply'])
        self.assertLess(time.monotonic() - started, 2)  # Not 30 s waiting for the lock.
        written = ''.join(call.args[0] for call in err.write.call_args_list)
        self.assertIn('wattif experiment: not applied: experiments own the settings\n', written)
        experiment_module.write_state({'mode': 'auto', 'status': 'finished'})
        self.box.specs = specs = self.specs_for_pool()
        battery = self.box.root / 'sys/class/power_supply/BAT1'  # command_apply's own on_battery() reads it.
        battery.mkdir(parents=True)
        (battery / 'type').write_text('Battery\n')
        (battery / 'status').write_text('Discharging\n')
        stale = lambda now: {'battery': {'apply': ['cpu-cap=saving', 'aspm=saving']}, 'computed': True}
        with mock.patch('sys.stderr') as err:
            applied = experiment_module.apply_goal('battery', recommend=stale)
        self.assertEqual((applied, specs['cpu-cap'].value, specs['aspm'].value), (['cpu-cap=saving'], 'y', 'x'))
        written = ''.join(call.args[0] for call in err.write.call_args_list)
        self.assertIn('wattif experiment: recommend.json was stale, so the battery combination was computed now; '
                      'applied: cpu-cap=saving\n', written)

    # --- Review fixes (October 20): settings go back as wattif found them ---------------------------

    def commands(self, live, calls):
        """`run` for the AMD laptop: wattif-helper over the sandbox's /sys (aspm, apst, abm,
        cpucap; trigger changes nothing: no udev rule sets anything), powerprofilesctl over
        live['profile'], and systemctl doing nothing."""
        sys_root, helper = self.box.root / 'sys', len(experiment_module.HELPER)

        def write(pattern, value):
            for path in sys_root.glob(pattern):
                path.write_text(f'{value}\n')

        def run(command, check=True, **_):
            calls.append(command)
            if command[:helper] == experiment_module.HELPER:
                what, value = command[helper], command[-1]
                if what == 'aspm':
                    write('module/pcie_aspm/parameters/policy', ' '.join(
                        f'[{p}]' if p == value else p for p in ('default', 'performance', 'powersave', 'powersupersave')))
                elif what == 'apst':
                    write('class/nvme/nvme*/power/pm_qos_latency_tolerance_us', value)
                elif what == 'abm':
                    write('class/drm/card*-eDP-*/amdgpu/panel_power_savings', value)
                elif what == 'cpucap':
                    write(f'{CPU}cpufreq/policy*/scaling_max_freq', '4972435' if value == 'none' else f'{value}000')
                return ''
            if command[:2] == ['powerprofilesctl', 'set']:
                live['profile'] = command[2]
                return ''
            if command[:2] == ['systemctl', '--user']:
                return ''
            raise AssertionError(f'unexpected command {command}')
        return run

    def test_tlp_aspm_and_a_modprobe_apst_limit_are_left_alone_and_go_back_as_found(self):
        # TLP sets ASPM powersupersave on battery, and modprobe.d's nvme_core
        # default_ps_max_latency_us=0 works around an SSD that crashes in APST: neither is
        # on the kernel command line. Before, the panel's Off switch wrote ASPM default and
        # APST 100000 (risky, never opted in) until reboot.
        (self.box.root / 'cmdline').write_text('quiet\n')
        files = dict(AMD_LAPTOP, **{'module/pcie_aspm/parameters/policy': 'default performance powersave [powersupersave]',
                                    'module/nvme_core/parameters/default_ps_max_latency_us': '0'})
        self.box.specs = specs = self.machine(files, EVERYTHING, [experiment_module.THROTTLE_UNIT])
        calls = []
        with mock.patch.object(experiment_module, 'run', self.commands({}, calls)):
            self.assertEqual((specs['aspm'].stock(), specs['apst'].stock()), ('powersupersave', '0'))
            experiment_module.main(['disable'])  # wattif never changed them:
            self.assertEqual([c for c in calls if c[0] != 'systemctl'], [])  # nothing is written.
            for name, value in (('aspm', 'performance'), ('apst', experiment_module.APST_DEFAULT)):  # A run's blocks.
                experiment_module.change(specs[name], value, Ledger(), 'manual')
            self.assertEqual((specs['aspm'].read(), specs['apst'].read()), ('performance', '100000'))
            experiment_module.main(['restore', '--stock'])
            self.assertEqual((specs['aspm'].read(), specs['apst'].read()), ('powersupersave', '0'))  # As found.
        # Nothing saved (an older wattif's change): the limit the drives started with, not 100000.
        self.box.boot = 'b2'
        experiment_module.update_preferences(lambda prefs: prefs.pop('found', None))
        Ledger().change('apst', experiment_module.APST_DEFAULT, False, 'auto')
        self.assertEqual(specs['apst'].stock(), '0')

    def test_a_performance_profile_on_ac_is_left_alone(self):
        # Before, every automatic start and pause on AC set balanced (config's AC profile by default).
        live, calls = {'profile': 'performance'}, []
        self.enterContext(mock.patch.object(experiment_module, 'profile_get', lambda: live['profile']))
        self.enterContext(mock.patch.object(experiment_module, 'run', self.commands(live, calls)))
        self.box.battery = False
        self.box.specs = specs = self.real_catalog()
        profile = specs['profile']
        self.assertEqual((profile.ac_managed, profile.stock(False)), (True, 'performance'))
        self.run_automatic(specs, FakeClock(), 1000 + 10 * 60, ['profile', 'cpu-cap'], battery=lambda: False)
        experiment_module.main(['disable'])
        self.assertEqual((live['profile'], [c for c in calls if c[0] == 'powerprofilesctl']), ('performance', []))
        experiment_module.capture_profile(lambda: False, now=time.time() + 3600, specs=specs)  # Seen on AC.
        # A block on battery sets power-saver; plugged in with no udev rule, it stays: back to yours.
        self.box.battery, live['profile'] = True, 'performance'
        experiment_module.change(profile, 'power-saver', Ledger(), 'auto')
        self.box.battery = False
        self.assertEqual(experiment_module.restore_settings(specs, ['profile'], lambda: False, Ledger()), {})
        self.assertEqual(live['profile'], 'performance')
        # With a udev rule that sets balanced on AC, the system's is left to it.
        self.box.battery = True
        experiment_module.change(profile, 'power-saver', Ledger(), 'auto')
        self.box.battery, live['profile'] = False, 'balanced'
        experiment_module.restore_settings(specs, ['profile'], lambda: False, Ledger())
        self.assertEqual(live['profile'], 'balanced')
        with mock.patch.dict(config.SETTINGS['stock'], profile={'battery': 'found', 'ac': 'balanced'}):
            catalog = CATALOG()  # A configured AC profile is put back there; "found" is as found.
            self.assertEqual((catalog['profile'].ac_managed, catalog['profile'].stock(False)), (False, 'balanced'))

    def test_a_120_hz_panel_used_at_60_hz_goes_back_to_60(self):
        socket = self.box.root / 'sway.sock'
        socket.touch()
        os.environ['SWAYSOCK'] = str(socket)
        output = json.loads(json.dumps(SWAY_OUTPUTS[0]))
        output['current_mode'] = dict(output['modes'][1])  # 60 Hz, of 120 and 60.

        def run(command, check=True, **_):
            if command[:2] == ['swaymsg', '-r']:
                return json.dumps([output])
            rate = float(command[-1].removesuffix('Hz').rpartition('@')[2])  # swaymsg output eDP-1 mode …
            output['current_mode'] = next(m for m in output['modes'] if abs(m['refresh'] / 1000 - rate) < 0.01)
            return ''
        with mock.patch.object(experiment_module, 'run', run):
            spec = self.real_catalog()['refresh']
            self.assertEqual(spec.stock(), '2560x1600@60.000')  # The mode you use, not the highest.
            experiment_module.change(spec, '2560x1600@120.000', Ledger(), 'manual')
            self.assertEqual((spec.read(), spec.stock()), ('2560x1600@120.000', '2560x1600@60.000'))
            self.assertEqual(experiment_module.stock_names({'refresh': spec}, Ledger()), ['refresh'])  # Held.
            self.assertEqual(experiment_module.restore_settings({'refresh': spec}, ['refresh'], lambda: True, Ledger()),
                             {})
            self.assertEqual(spec.read(), '2560x1600@60.000')
            self.assertEqual(experiment_module.stock_names({'refresh': spec}, Ledger()), [])  # Put back: not held.

    def test_abm_applied_on_ac_then_disabled_goes_back_to_what_it_was(self):
        # No udev rule sets ABM: before, disable left wattif's 3, logged it as stock and
        # saved it as the ABM found on battery for good.
        panel = self.box.root / 'sys/class/drm/card1-eDP-1/amdgpu/panel_power_savings'
        self.enterContext(mock.patch.object(experiment_module, 'run', self.commands({}, [])))
        self.box.specs = specs = self.real_catalog()
        panel.write_text('0\n')
        self.box.battery = False
        with mock.patch('sys.stdout'):
            experiment_module.command_apply(['abm=saving'], battery=lambda: False)
        self.assertEqual(panel.read_text(), '0\n')  # On AC: not yet.
        self.box.battery = True
        experiment_module.command_reconcile(battery=lambda: True)
        self.assertEqual(panel.read_text(), '3\n')  # Unplugged: applied.
        experiment_module.main(['disable'])
        self.assertEqual(panel.read_text(), '0\n')  # As it was before wattif changed it.
        for later in (100, 500):  # The reconciler's captures afterwards (once the power source is steady).
            experiment_module.capture_profile(lambda: True, now=time.time() + later, specs=specs)
        self.assertEqual(self.box.prefs()['found']['abm'], {'battery': '0'})  # Never wattif's own 3.

    def test_apply_leaves_settings_already_in_the_state_asked_for(self):
        # An "already set" combination moved the profile performance → balanced and ABM 1 → 3.
        panel = self.box.root / 'sys/class/drm/card1-eDP-1/amdgpu/panel_power_savings'
        live, calls = {'profile': 'performance'}, []
        self.enterContext(mock.patch.object(experiment_module, 'profile_get', lambda: live['profile']))
        self.enterContext(mock.patch.object(experiment_module, 'run', self.commands(live, calls)))
        self.box.specs = specs = self.real_catalog()
        panel.write_text('1\n')  # Saving, as much as 3 is.
        combination, left = ['profile=normal', 'abm=saving', 'cpu-cap=saving'], []
        with mock.patch('sys.stderr') as err:
            experiment_module.command_apply(combination, battery=lambda: True, left=left)
        written = ''.join(call.args[0] for call in err.write.call_args_list)
        self.assertEqual(left, ['profile', 'abm'])
        self.assertIn('wattif experiment: already so, left as they are: profile; abm\n', written)
        self.assertEqual((live['profile'], panel.read_text(), specs['cpu-cap'].read()), ('performance', '1\n', '2000'))
        self.assertEqual([c[len(experiment_module.HELPER):] for c in calls], [['cpucap', '2000']])
        self.assertEqual(experiment_module.load_applied()['values'], {'cpu-cap': '2000'})
        # The paused runner sets the goal's combination alike.
        self.assertEqual(experiment_module.recommended_values(specs, ['profile', 'abm', 'cpu-cap'], 'battery',
                                                               lambda: {'battery': {'apply': combination}}),
                         {'profile': 'performance', 'abm': '1', 'cpu-cap': '2000'})
        # Applied on AC, it is judged by its value on battery: a udev rule's ABM 1 there is left.
        experiment_module.APPLIED.unlink()
        self.box.battery = False
        panel.write_text('0\n')
        with mock.patch('sys.stdout'):
            experiment_module.command_apply(['abm=saving'], battery=lambda: False)
        self.box.battery = True
        panel.write_text('1\n')
        with mock.patch('sys.stderr'):
            experiment_module.command_reconcile(battery=lambda: True)
        self.assertEqual((panel.read_text(), experiment_module.load_applied()['values']), ('1\n', {}))

    def test_root_settings_are_left_out_without_the_helpers_sudo_rule(self):
        self.box.helper = False
        self.box.specs = specs = self.real_catalog()
        self.assertEqual(list(specs), ['profile', 'throttle-apps'])
        self.assertEqual(list(specs.missing), ['aspm', 'apst', 'boost', 'abm', 'gpu-dpm', 'cpu-cap', 'wifi-ps'])
        with mock.patch.object(experiment_module, 'run', lambda command, check=True, **_: ''), \
                mock.patch('sys.stdout') as out, mock.patch('sys.stderr') as err:
            experiment_module.main(['list'])
        rows = {row['name']: row for row in json.loads(''.join(call.args[0] for call in out.write.call_args_list))}
        self.assertEqual(set(rows), set(specs) | set(specs.missing))
        self.assertEqual((rows['aspm']['error'], rows['profile']['error']), (experiment_module.HELPER_NEEDED, None))
        self.assertIn("wattif experiment: left out: aspm, apst, boost, abm, gpu-dpm, cpu-cap, wifi-ps (root settings "
                      "need wattif-helper's sudo rule; see the README)", ''.join(c.args[0] for c in err.write.call_args_list))
        from wattif import panel_data  # The panel's row for it says why.
        row = panel_data.lever_rows([rows['aspm']], {'effects': []}, None, set())[0]
        self.assertEqual((row['detail'].split(' · ')[0], row['sensitive']),
                         (f'Unavailable: {experiment_module.HELPER_NEEDED}', False))

    def test_the_helper_is_asked_once_while_it_works_and_again_later_while_it_doesnt(self):
        probes, allowed, now = [], [False], [1000.0]

        def run(command, check=True, timeout=15):
            probes.append(command)
            if not allowed[0]:
                raise RuntimeError(f"{' '.join(command)}: sudo: a password is required")
            return ''
        with mock.patch.object(experiment_module, 'run', run), mock.patch.object(experiment_module.time, 'time',
                                                                                lambda: now[0]):
            self.assertEqual((HELPER_USABLE(), HELPER_USABLE()), (False, False))
            self.assertEqual(probes, [experiment_module.HELPER + ['status']])  # Asked once,
            allowed[0] = True  # then its sudo rule is added:
            now[0] += experiment_module.PROBE_AGAIN
            self.assertTrue(HELPER_USABLE())  # noticed within PROBE_AGAIN.
            now[0] += 86400
            self.assertEqual((HELPER_USABLE(), len(probes)), (True, 2))  # While it works: once a boot,
            allowed[0] = False
            with self.assertRaises(RuntimeError):
                experiment_module.lab('cpucap', 'none')
            self.assertFalse(HELPER_USABLE())  # or until a helper call fails.
            with mock.patch.object(experiment_module, 'HELPER', []):
                self.assertFalse(HELPER_USABLE())  # No helper configured: never asked.
            self.assertEqual(len(probes), 4)
        # config [helper] command and [experiments] risky as strings: never their characters.
        with mock.patch.dict(config.SETTINGS['helper'], command='sudo -n wattif-helper'), mock.patch('sys.stderr') as err:
            command, why = experiment_module.helper_command()
        self.assertEqual(command, [])
        self.assertIn('must be a list of words', why)
        self.assertIn(why, ''.join(c.args[0] for c in err.write.call_args_list))
        with mock.patch.object(experiment_module, 'HELPER', []), \
                mock.patch.object(experiment_module, 'HELPER_PROBLEM', why), self.assertRaises(RuntimeError) as refused:
            experiment_module.lab('cpucap', 'none')
        self.assertEqual(str(refused.exception), why)
        with mock.patch.dict(config.SETTINGS['experiments'], risky='aspm', freeze_keep='t3code'):
            self.assertEqual((experiment_module.listed('experiments', 'risky'), experiment_module.freeze_keep()),
                             (['aspm'], ['t3code']))
        with mock.patch.dict(config.SETTINGS['session'], lockers='swaylock'):
            tree(self.box.root / 'proc', {'12/comm': 's'})
            self.assertFalse(experiment_module.locker_running())  # Not "s", "w", "a" …

    def test_throttle_apps_is_offered_only_where_throttle_can_follow_the_windows(self):
        self.box.followable = False  # Hyprland, GNOME, KDE, X11: wattif throttle caps nothing there.
        self.assertNotIn('throttle-apps', self.real_catalog())
        self.assertFalse(THROTTLE_FOLLOWABLE())
        path = self.box.root / 'niri.sock'
        with socket.socket(socket.AF_UNIX) as listening:  # compositor.detect wants a socket there, not a stale name.
            listening.bind(str(path))
        os.environ['NIRI_SOCKET'] = str(path)
        self.assertTrue(THROTTLE_FOLLOWABLE())

    def test_app_scopes_in_nested_slices_are_found_and_frozen_by_their_unit_name(self):
        # uwsm, app2unit and Flatpak start apps in app.slice/app-graphical.slice.
        root = self.box.root / 'cgroup'

        def scope(path, usec, populated=1):
            tree(root / 'app.slice' / path, {'cpu.stat': f'usage_usec {usec}\nuser_usec 0',
                                             'cgroup.events': f'populated {populated}\nfrozen 0', 'cgroup.procs': '412'})
        scope('app-niri-floorp-2240.scope', 9_000_000)
        scope('app-graphical.slice/app-niri-t3code-6a5fdff9.scope', 30_000_000)
        scope('app-graphical.slice/app-flatpak-com.valvesoftware.Steam-3.scope', 2_000_000)
        scope('app-graphical.slice/app-gone-9.scope', 5, populated=0)
        t3code, steam = ('app-graphical.slice/app-niri-t3code-6a5fdff9.scope',
                         'app-graphical.slice/app-flatpak-com.valvesoftware.Steam-3.scope')
        calls = []
        with mock.patch.object(experiment_module, 'user_slice', lambda: root), \
                mock.patch.object(experiment_module, 'run', lambda command, check=True, **_: calls.append(command) or ''):
            self.assertEqual([(u['unit'], u['label']) for u in experiment_module.freezable_units()],
                             [(t3code, 't3code'), ('app-niri-floorp-2240.scope', 'floorp'), (steam, 'Steam')])
            targets = experiment_module.freeze_targets(['floorp'])
            self.assertEqual(targets, [t3code, steam])
            experiment_module.freeze_all_set(targets, 'frozen')
            self.assertEqual(experiment_module.freeze_get(t3code), 'running')  # The kernel's view, through the slice.
        self.assertEqual(calls, [['systemctl', '--user', 'freeze', 'app-niri-t3code-6a5fdff9.scope'],
                                 ['systemctl', '--user', 'freeze', 'app-flatpak-com.valvesoftware.Steam-3.scope']])

    def test_on_battery_means_no_charger_online_whatever_kind(self):
        supplies = self.box.root / 'sys/class/power_supply'
        tree(supplies, {'BAT0/type': 'Battery', 'BAT0/status': 'Charging', 'ucsi-source-psy-USBC000:001/type': 'USB',
                        'ucsi-source-psy-USBC000:001/online': '1'})
        self.assertFalse(ON_BATTERY())  # Charging over USB-C, with no Mains supply: before, "on battery".
        (supplies / 'ucsi-source-psy-USBC000:001/online').write_text('0\n')
        self.assertTrue(ON_BATTERY())
        (supplies / 'ucsi-source-psy-USBC000:001/type').write_text('Unknown\n')
        self.assertTrue(ON_BATTERY())  # No charger to read: on battery, as it has one.
        (supplies / 'BAT0/type').write_text('Unknown\n')
        self.assertFalse(ON_BATTERY())  # A desktop: never.

    # --- Unchanged helpers ----------------------------------------------------------------------

    def test_a_stale_recommendation_is_computed_again(self):
        path = self.box.runtime / 'recommend.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'updated': 1000}))
        with mock.patch.object(experiment_module, 'RECOMMEND', path), \
                mock.patch('wattif.eta.write_recommendations', return_value={'fresh': True}), \
                mock.patch('wattif.report.load_range', return_value=([], [])):
            self.assertEqual(experiment_module.recommendation(now=1100), {'updated': 1000})
            self.assertEqual(experiment_module.recommendation(now=1000 + 3601), {'fresh': True, 'computed': True})

    def test_the_cpu_cap_reads_none_at_the_cpus_own_maximum(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / 'policy0'
            policy.mkdir()
            (policy / 'cpuinfo_max_freq').write_text('3501000\n')
            with mock.patch.object(experiment_module, 'CPUFREQ', Path(tmp)):
                (policy / 'scaling_max_freq').write_text('3501000\n')
                self.assertEqual(experiment_module.cpu_cap_get(), 'none')
                (policy / 'scaling_max_freq').write_text('2000000\n')
                self.assertEqual(experiment_module.cpu_cap_get(), experiment_module.CPU_CAP)
        specs = self.real_catalog()
        self.assertEqual((specs['cpu-cap'].levels(), specs['gpu-dpm'].levels()), (('2000', 'none'), ('low', 'auto')))

    def test_boost_is_on_only_globally_and_per_cpu_and_profile_changes_keep_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            cpufreq, calls = Path(tmp), []
            write = lambda name, value: (cpufreq / name).write_text(f'{value}\n')
            write('boost', 1)
            for i in range(2):
                (cpufreq / f'policy{i}').mkdir()
                write(f'policy{i}/boost', 1)

            def run(command, check=True, **_):
                calls.append(command)
                if command[:2] == ['powerprofilesctl', 'set']:  # The daemon: per-CPU boost off for power-saver.
                    if (cpufreq / 'boost').read_text().strip() == '0':
                        raise RuntimeError('Failed to activate CPU driver: Invalid argument')
                    for i in range(2):
                        write(f'policy{i}/boost', 0 if command[2] == 'power-saver' else 1)
                elif command[-2] == 'boost':  # wattif-helper: global on, per CPU as asked.
                    write('boost', 1)
                    for i in range(2):
                        write(f'policy{i}/boost', command[-1])
                return ''
            lab = experiment_module.HELPER
            with mock.patch.object(experiment_module, 'CPUFREQ', cpufreq), mock.patch.object(experiment_module, 'run', run):
                self.assertEqual(experiment_module.boost_get(), '1')
                experiment_module.profile_set('power-saver')
                self.assertEqual(experiment_module.boost_get(), '1')  # Put back after the daemon turned it off.
                self.assertEqual(calls[-1], lab + ['boost', '1'])
                write('policy1/boost', 0)
                self.assertEqual(experiment_module.boost_get(), '0')
                write('boost', 0)  # Off globally (the old wattif-helper): the daemon refuses to switch.
                experiment_module.profile_set('balanced')
                self.assertEqual(calls[-3:], [lab + ['boost', '0'], ['powerprofilesctl', 'set', 'balanced'],
                                              lab + ['boost', '0']])
                self.assertEqual((experiment_module.boost_get(), (cpufreq / 'boost').read_text().strip()), ('0', '1'))

    def test_abm_goes_back_by_the_udev_policy_and_keeps_the_profile(self):
        calls, live = [], {'profile': 'balanced', 'abm': '0'}

        def run(command, check=True, **_):
            calls.append(command[-2:])
            if command[-2:] == ['trigger', 'power_supply']:  # udev: ABM 3 and power-saver on battery.
                live.update(abm='3', profile='power-saver')
            elif command[:2] == ['powerprofilesctl', 'set']:
                live['profile'] = command[2]
            return ''
        with mock.patch.object(experiment_module, 'run', run), \
                mock.patch.object(experiment_module, 'profile_get', lambda: live['profile']), \
                mock.patch.object(experiment_module, 'boost_get', lambda: None):
            experiment_module.abm_reset()
        self.assertEqual(live, {'profile': 'balanced', 'abm': '3'})  # Your balanced profile survives.
        self.assertEqual(calls[0], ['trigger', 'power_supply'])

    def test_unit_labels(self):
        self.assertEqual(unit_label('app-niri-floorp-2329.scope'), 'floorp')
        self.assertEqual(unit_label('app-niri-signal\\x2ddesktop-2342.scope'), 'signal-desktop')


# The real ones (the sandbox replaces them).
CATALOG, JOURNAL, ON_BATTERY = experiment_module.catalog, experiment_module.Journal, experiment_module.on_battery
HELPER_USABLE, THROTTLE_FOLLOWABLE = experiment_module.helper_usable, experiment_module.throttle_followable


if __name__ == '__main__':
    unittest.main()

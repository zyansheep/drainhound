import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from wattif import config, launch

NOW = 1.8e9
DISCHARGING = {'status': 'Discharging', 'pct': 62.4, 'watts': 7.84, 'updated': NOW - 1, 'energy_pct': 60,
               'hours': 3.2, 'low': 2.65, 'high': 3.9, 'classes': ['fill60', 'discharging'],
               'sleep': {'hours': 52.3, 'low': 40.1, 'high': 70.0, 'watts': 0.6, 'suspends': 12},
               'waybar': {'text': 'label', 'class': ['fill60', 'discharging'], 'percentage': 60}}
BATTERY = {'title': 'Battery use · last 30 min on battery · 7.8 W', 'timestamp': NOW - 5,
           'rows': [{'name': 'Idle chip', 'value': ' 2.1 W  27%',
                     'parts': [{'name': 'cores', 'value': ' 1.0 W'}, {'name': 'fabric', 'value': ' 1.1 W'}]},
                    {'name': 'Chat & media', 'value': ' 1.5 W  19%'}, {'name': 'Display', 'value': ' 4.2 W  54%'}],
           'footer': 'Power log: 1,234 samples · 3.4 h since Oct 20'}


class TooltipTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.eta, self.snapshot = Path(tmp.name) / 'eta.json', Path(tmp.name) / 'snapshot.json'
        self.snapshot.write_text(json.dumps({'cpu': [], 'memory': [], 'timestamp': NOW - 2, 'battery': BATTERY}))

    def label(self, detail):
        self.eta.write_text(json.dumps(detail))
        return launch.battery_label(self.eta, now=NOW, snapshot=self.snapshot)

    def test_a_label_without_one_gets_time_left_sleep_and_battery_use(self):
        label = self.label(DISCHARGING)
        self.assertEqual((label['text'], label['percentage']), ('label', 60))
        lines = label['tooltip'].split('\n')
        self.assertEqual(lines[:3], ['62% · 7.8 W', '3 h 12 m left (80%: 2 h 39 m–3 h 54 m)',
                                     'Asleep from now: ~52 h (80%: 40 h–70 h)'])
        self.assertEqual(lines[4], 'Battery use · last 30 min on battery · 7.8 W')
        # Lined up as shown (by 'Chat & media', the longest name), then escaped.
        self.assertEqual(lines[5:10], ['<tt>Idle chip      2.1 W  27%', '  cores        1.0 W', '  fabric       1.1 W',
                                       'Chat &amp; media   1.5 W  19%', 'Display        4.2 W  54%</tt>'])
        self.assertEqual(lines[10], 'Power log: 1,234 samples · 3.4 h since Oct 20')

    def test_charging_countdown_and_ac(self):
        charging = dict(DISCHARGING, classes=['charging'], hours=1.25, low=1.0, high=1.5, target=80, banked=4.5)
        lines = self.label(charging)['tooltip'].split('\n')
        self.assertEqual(lines[1:3], ['1 h 15 m to 80% (80%: 1 h 00 m–1 h 30 m)',
                                      'Unplugged now: ~4 h 30 m on battery'])
        countdown = dict(DISCHARGING, cutout=NOW + 200, cutout_pm=40, poweroff=NOW + 140, charger='too weak')
        lines = self.label(countdown)['tooltip'].split('\n')
        self.assertEqual(lines[1:3], [f"Powers off at {time.strftime('%H:%M:%S', time.localtime(NOW + 140))} ±40 s",
                                      'Charger too weak'])
        self.assertEqual(self.label(dict(DISCHARGING, classes=['plugged']))['tooltip'].split('\n')[1], 'On AC')

    def test_its_own_tooltip_a_stale_block_and_the_setting_are_respected(self):
        own = dict(DISCHARGING, waybar=dict(DISCHARGING['waybar'], tooltip='mine'))
        self.assertEqual(self.label(own)['tooltip'], 'mine')
        self.snapshot.write_text(json.dumps({'timestamp': NOW - 600, 'battery': BATTERY}))  # The collector stopped.
        self.assertNotIn('Battery use', self.label(DISCHARGING)['tooltip'])
        self.snapshot.unlink()
        self.assertTrue(self.label(DISCHARGING)['tooltip'].startswith('62% · 7.8 W\n3 h 12 m left'))
        with mock.patch.dict(config.SETTINGS['waybar'], {'tooltip': False}):
            self.assertNotIn('tooltip', self.label(DISCHARGING))
            self.assertEqual(self.label(dict(DISCHARGING, updated=NOW - 60)), {'text': '--', 'class': ['unknown']})
        stale = self.label(dict(DISCHARGING, updated=NOW - 60))
        self.assertEqual((stale['text'], stale['tooltip']), ('--', "No estimate: wattif eta isn't running"))

    def test_hours(self):
        self.assertEqual([launch.hours_text(h) for h in (0.75, 1.9925, 9.6, 10.4, float('nan'), None)],
                         ['45 min', '2 h 00 m', '9 h 36 m', '10 h', '--', '--'])


if __name__ == '__main__':
    unittest.main()

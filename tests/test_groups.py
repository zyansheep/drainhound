"""The timeline's app groups (report.GROUPS), their colours and what-if phrases,
built in and configured."""
import unittest
from unittest import mock

import numpy as np

from wattif import collector, config, panel_data, report


def rows(apps, n=240, seed=3):
    """Battery minutes where each app's load varies on its own."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        loads = {name: rng.uniform(0, 4) for name in apps}
        load, ghz = 1 + sum(loads.values()), 1.5
        soc = 3 + 2 * ghz + .2 * load
        bl = rng.uniform(.3, 1)
        out.append({'t': i * 60, 'dt': 60, 'st': 'D', 'load': load, 'ghz': ghz, 'soc': soc, 'bl': bl,
                    'apps': {name: [value, 0, 0] for name, value in loads.items()},
                    'bat': .5 + 1.4 * soc + 3 * bl + rng.normal(0, .2)})
    return out


def configured(groups):
    """report as it is with [apps] groups = `groups`."""
    found = report.configured_groups(groups)
    return mock.patch.multiple(report, GROUPS=found, APP_GROUPS=[name for name, _ in found] + ['Other apps'])


class GroupTests(unittest.TestCase):
    def test_the_built_in_groups_are_generic(self):
        self.assertEqual(report.APP_GROUPS, ['Browsers', 'Coding tools', 'Chat & media', 'Desktop & system',
                                             'Other apps'])
        for app, group in (('Firefox', 'Browsers'), ('firefox', 'Browsers'), ('Chrome', 'Browsers'),
                           ('VS Code', 'Coding tools'), ('rustc', 'Coding tools'), ('C++ compiler', 'Coding tools'),
                           ('claude', 'Coding tools'), ('foot', 'Coding tools'), ('Signal', 'Chat & media'),
                           ('PipeWire audio', 'Chat & media'), ('Kernel', 'Desktop & system'),
                           ('GNOME Shell', 'Desktop & system'), ('fcitx5', 'Desktop & system'),
                           ('openroad', 'Other apps'), ('Obsidian', 'Other apps'), ('zenity', 'Other apps')):
            self.assertEqual(report.group_of(app), group, app)
        for browser in collector.BROWSERS:  # Whatever the collector calls a browser.
            self.assertEqual(report.group_of(browser), 'Browsers', browser)
        for _, prefixes in report.DEFAULT_GROUPS:
            self.assertEqual([p for p in prefixes if p != p.lower()], [])  # Matched against lower case.
        self.assertEqual(report.system_group(), 'Desktop & system')

    def test_configured_groups_keep_their_order_and_replace_the_built_in_ones(self):
        groups = report.configured_groups({'EDA jobs': ['OpenROAD', 'yosys'], 'Web': 'floorp', 'Other apps': ['x']})
        self.assertEqual(groups, [('EDA jobs', ('openroad', 'yosys')), ('Web', ('floorp',))])
        self.assertIs(report.configured_groups({}), report.DEFAULT_GROUPS)
        self.assertIs(report.configured_groups(['not a table']), report.DEFAULT_GROUPS)
        self.assertIs(report.configured_groups(), report.DEFAULT_GROUPS)  # The default configuration: None.
        with mock.patch.dict(config.SETTINGS['apps'], {'groups': {'Games': ['steam']}}):
            self.assertEqual(report.configured_groups(), [('Games', ('steam',))])
        with configured({'EDA jobs': ['openroad'], 'Web': ['floorp']}):
            self.assertEqual((report.group_of('openroad'), report.group_of('Floorp'), report.group_of('Kernel')),
                             ('EDA jobs', 'Web', 'Other apps'))
            self.assertIsNone(report.system_group())  # No group has the kernel.

    def test_what_if_phrases_follow_the_group_names(self):
        self.assertEqual([report.close_phrase(g) for g in ('Browsers', 'Chat & media', 'EDA jobs', 'X')],
                         ['Close browsers', 'Close chat & media', 'Close EDA jobs', 'Close X'])
        minutes = rows(['Floorp', 'rustc', 'openroad', 'niri'])
        model = report.fit_rows(minutes)
        items = report.candidates(minutes, model)
        self.assertTrue({'Close browsers', 'Close coding tools', 'Close other apps', 'Close Floorp',
                         'Close rustc', 'Close openroad'} <= set(items))
        self.assertFalse({'Close desktop & system', 'Close niri'} & set(items))  # The desktop's own: not for closing.
        with configured({'EDA jobs': ['openroad'], 'Desktop': ['kernel', 'niri']}):
            items = report.candidates(minutes, model)
            self.assertTrue({'Close EDA jobs', 'Close other apps', 'Close Floorp'} <= set(items))
            self.assertFalse({'Close browsers', 'Close desktop', 'Close niri'} & set(items))


class ColorTests(unittest.TestCase):
    def test_every_group_the_timeline_shows_has_a_colour_by_position(self):
        colors = panel_data.chart_colors(report.GROUPS)
        self.assertEqual(list(colors), [report.IDLE, 'Rest of system', 'Display', 'Browsers', 'Coding tools',
                                        'Chat & media', 'Desktop & system', 'Other apps'])
        self.assertEqual([colors[g] for g in ('Browsers', 'Coding tools', 'Chat & media', 'Desktop & system')],
                         list(panel_data.GROUP_SLOTS[:4]))
        six = [(f'Group {i}', (f'g{i}',)) for i in range(6)]
        colors = panel_data.chart_colors(six)
        self.assertEqual([colors[name] for name, _ in six], [*panel_data.GROUP_SLOTS, panel_data.GROUP_SLOTS[0]])
        self.assertEqual((colors['Other apps'], colors['Display']), (panel_data.DARK_GREEN, panel_data.AMBER))
        with configured({'EDA jobs': ['openroad'], 'Web': ['floorp']}):
            minute = rows(['Floorp', 'openroad', 'rustc'], n=40)[0]
            parts = report.attribute(minute, report.fit_rows(rows(['Floorp', 'openroad', 'rustc'], n=40)))
            self.assertLessEqual(set(parts['groups']), set(panel_data.chart_colors(report.GROUPS)))


if __name__ == '__main__':
    unittest.main()

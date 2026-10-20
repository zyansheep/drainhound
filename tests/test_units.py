import os
import unittest
from unittest import mock

from drainhound import cli, config, units


class UnitTests(unittest.TestCase):
    def test_every_unit_runs_a_command_that_exists(self):
        files = units.units('/usr/bin/drainhound')
        self.assertEqual(set(config.UNITS.values()) | {config.UNITS['reconcile'].replace('.service', '.timer')},
                         set(files))
        commands = {line.split('=', 1)[1].split()[1] for text in files.values() for line in text.splitlines()
                    if line.startswith(('ExecStart=', 'ExecStopPost=')) and '/usr/bin/drainhound' in line}
        self.assertLessEqual(commands, {'collect', 'eta', 'panel', 'experiment', 'throttle', 'freeze'})
        for command in commands:  # Each is one of cli's.
            self.assertIn(f'  {command}', cli.USAGE)
        self.assertIn(f'BusName={config.APP_ID}', files[config.UNITS['panel']])

    def test_the_default_set_is_enabled_and_wanted(self):
        files = units.units('/usr/bin/drainhound')
        for name in units.enabled_names():
            self.assertIn('WantedBy=graphical-session.target', files[name], name)

    def test_the_experiments_flag_follows_a_moved_state_directory(self):
        with mock.patch.dict(os.environ, {'DRAINHOUND_STATE_DIR': ''}):
            os.environ.pop('DRAINHOUND_STATE_DIR')
            self.assertIn('ConditionPathExists=%S/drainhound/experiments-enabled',
                          units.units('drainhound')[config.UNITS['experiments']])
        with mock.patch.dict(os.environ, {'DRAINHOUND_STATE_DIR': '/data/drainhound'}), \
                mock.patch.object(config, 'STATE', config.Path('/data/drainhound')):
            self.assertIn('ConditionPathExists=/data/drainhound/experiments-enabled',
                          units.units('drainhound')[config.UNITS['experiments']])


if __name__ == '__main__':
    unittest.main()

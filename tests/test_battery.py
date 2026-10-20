import tempfile
import unittest
from pathlib import Path

from wattif import battery as batteries


def supply(root, name, **values):
    """A power_supply directory under `root` (the class/power_supply directory)."""
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    for key, value in values.items():
        (path / key).write_text(f'{value}\n')
    return path


# A ThinkPad-like battery: energy in µWh, power in µW.
THINKPAD = dict(type='Battery', status='Discharging', present=1, energy_now=30_000_000, energy_full=50_000_000,
                energy_full_design=57_000_000, power_now=8_500_000, voltage_now=11_900_000,
                voltage_min_design=11_520_000, capacity=60, cycle_count=212, charge_control_end_threshold=80)
# A Framework-like battery: charge in µAh, current in µA.
FRAMEWORK = dict(type='Battery', status='Discharging', present=1, charge_now=1_500_000, charge_full=3_000_000,
                 charge_full_design=3_572_000, current_now=1_000_000, voltage_now=15_500_000,
                 voltage_min_design=15_400_000, capacity=50, cycle_count=134, charge_control_end_threshold=90)


class BatteryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_an_energy_battery_reads_in_wh(self):
        supply(self.root, 'BAT0', **THINKPAD)
        supply(self.root, 'AC', type='Mains', online=0)
        reading = batteries.read(self.root)
        self.assertEqual((reading['unit'], reading['status'], reading['count'], reading['names']),
                         ('Wh', 'Discharging', 1, ['BAT0']))
        self.assertEqual((reading['now'], reading['full'], reading['design']), (30.0, 50.0, 57.0))
        self.assertEqual((reading['watts'], reading['power']), (8.5, 8.5))
        self.assertEqual((reading['capacity'], reading['limit'], reading['cycles']), (60, 80, 212))
        self.assertAlmostEqual(reading['volts'], 11.9)
        self.assertAlmostEqual(reading['nominal'], 11.52)
        self.assertIs(reading['mains'], False)
        self.assertEqual(batteries.energy_wh(reading), 30.0)             # Measured, no voltage needed.
        self.assertEqual(batteries.nominal_volts(reading), reading['nominal'])

    def test_a_charge_battery_reads_in_ah(self):
        supply(self.root, 'BAT1', **FRAMEWORK)
        reading = batteries.read(self.root)
        self.assertEqual((reading['unit'], reading['now'], reading['full']), ('Ah', 1.5, 3.0))
        self.assertAlmostEqual(reading['watts'], 15.5)                    # current × voltage
        self.assertAlmostEqual(batteries.energy_wh(reading), 1.5 * 15.5)  # At the voltage now.
        self.assertIsNone(reading['mains'])                               # No charger known.

    def test_signs_follow_the_status(self):
        path = supply(self.root, 'BAT0', **dict(THINKPAD, status='Charging'))
        self.assertEqual(batteries.supply_watts(path), ('Charging', -8.5, 8.5))
        (path / 'status').write_text('Not charging')
        self.assertEqual(batteries.supply_watts(path), ('Not charging', 0.0, 8.5))
        (path / 'power_now').write_text('-8500000')                      # Some drivers sign it.
        (path / 'status').write_text('Discharging')
        self.assertEqual(batteries.supply_watts(path), ('Discharging', 8.5, 8.5))
        (path / 'power_now').unlink()
        (path / 'voltage_now').unlink()
        self.assertEqual(batteries.supply_watts(path), ('Discharging', None, None))

    def test_peripherals_absent_batteries_and_chargers_are_not_the_systems_battery(self):
        supply(self.root, 'hidpp_battery_0', type='Battery', scope='Device', status='Discharging', capacity=90)
        supply(self.root, 'BAT1', type='Battery', present=0)              # An empty bay.
        supply(self.root, 'ucsi-source-psy-USBC000:001', type='USB', scope='System', online=1)
        self.assertEqual(batteries.find(self.root), ())
        self.assertIsNone(batteries.read(self.root))
        self.assertIs(batteries.mains_online(self.root), True)            # No Mains supply: USB-C's.
        supply(self.root, 'AC', type='Mains', online=0)
        self.assertIs(batteries.mains_online(self.root), False)           # A Mains supply decides.
        self.assertIsNone(batteries.mains_online(self.root / 'missing'))
        self.assertEqual(batteries.find(self.root / 'missing'), ())

    def test_two_batteries_are_one(self):
        # A ThinkPad's internal battery runs it; the removable one idles at its limit.
        supply(self.root, 'BAT0', **THINKPAD)
        supply(self.root, 'BAT1', **dict(THINKPAD, status='Unknown', energy_now=20_000_000, energy_full=25_000_000,
                                         energy_full_design=26_000_000, power_now=0, capacity=80,
                                         charge_control_end_threshold=100, cycle_count=40, voltage_now=12_300_000))
        reading = batteries.read(self.root)
        self.assertEqual((reading['status'], reading['count'], reading['unit']), ('Discharging', 2, 'Wh'))
        self.assertEqual((reading['now'], reading['full'], reading['design']), (50.0, 75.0, 83.0))
        self.assertEqual((reading['watts'], reading['power']), (8.5, 8.5))
        self.assertAlmostEqual(reading['capacity'], (60 * 50 + 80 * 25) / 75)   # Weighted by full charge,
        self.assertAlmostEqual(reading['limit'], (80 * 50 + 100 * 25) / 75)     # as where each stops.
        self.assertAlmostEqual(reading['volts'], (11.9 + 12.3) / 2)
        self.assertEqual(reading['cycles'], 212)                                # The more worn.
        status, watts = batteries.watts(batteries.find(self.root))
        self.assertEqual((status, watts), ('Discharging', 8.5))
        (self.root / 'BAT1' / 'charge_control_end_threshold').unlink()          # No limit: 100%.
        self.assertAlmostEqual(batteries.read(self.root)['limit'], (80 * 50 + 100 * 25) / 75)
        (self.root / 'BAT1' / 'energy_now').unlink()                            # One can't say: no total.
        self.assertIsNone(batteries.read(self.root)['now'])

    def test_a_vanished_battery_makes_the_fast_path_look_again(self):
        paths = (supply(self.root, 'BAT0', **THINKPAD), supply(self.root, 'BAT1', **THINKPAD))
        self.assertEqual(batteries.watts(paths), ('Discharging', 17.0))
        (paths[1] / 'power_now').unlink()
        (paths[1] / 'voltage_now').unlink()
        self.assertEqual(batteries.watts(paths), ('Discharging', 8.5))           # Still there: the one it has.
        for child in paths[1].iterdir():
            child.unlink()
        paths[1].rmdir()
        self.assertEqual(batteries.watts(paths)[1], None)                       # Gone: look again.

    def test_mixed_units_add_up_in_wh(self):
        supply(self.root, 'BAT0', **THINKPAD)
        supply(self.root, 'BAT1', **FRAMEWORK)
        reading = batteries.read(self.root)
        self.assertEqual(reading['unit'], 'Wh')
        self.assertAlmostEqual(reading['now'], 30.0 + 1.5 * 15.4)              # A charge at its nominal voltage.
        self.assertAlmostEqual(reading['full'], 50.0 + 3.0 * 15.4)
        self.assertAlmostEqual(reading['watts'], 8.5 + 15.5)

    def test_combined_status(self):
        combined = batteries.combined_status
        self.assertEqual(combined(['Unknown']), 'Unknown')                      # One: as it is.
        self.assertEqual(combined(['Unknown', 'Discharging']), 'Discharging')
        self.assertEqual(combined(['Not charging', 'Charging']), 'Charging')
        self.assertEqual(combined(['Full', 'Full']), 'Full')
        self.assertEqual(combined(['Full', 'Not charging']), 'Not charging')
        self.assertEqual(combined(['Unknown', 'Unknown']), 'Unknown')
        self.assertEqual(combined(['Charging', 'Discharging'], -3.0), 'Charging')   # Net flow decides.
        self.assertEqual(combined(['Charging', 'Discharging'], 3.0), 'Discharging')

    def test_a_battery_with_no_now_reading_takes_its_full_charges_unit(self):
        supply(self.root, 'BAT0', type='Battery', energy_full=55_000_000)
        reading = batteries.read(self.root)
        self.assertEqual((reading['unit'], reading['full'], reading['now']), ('Wh', 55.0, None))
        self.assertIsNone(batteries.energy_wh(reading))
        self.assertEqual(batteries.nominal_volts(reading), batteries.DEFAULT_VOLTS)
        self.assertEqual(batteries.nominal_volts(reading, 16.0), 16.0)          # The logged voltage first.


if __name__ == '__main__':
    unittest.main()

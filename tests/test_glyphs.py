"""[waybar] glyphs: Font Awesome's icons, or plain Unicode for bars without it."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wattif import config, eta
from tests import test_eta  # Its helpers (not its tests: importing a TestCase here would run it again).

try:  # The panel needs GTK 4's typelibs (GI_TYPELIB_PATH); the rest of the suite doesn't.
    from wattif import panel
except (ImportError, ValueError):
    panel = None

PRIVATE_USE = re.compile('[\ue000-\uf8ff]')  # Font Awesome's glyphs live here.


def glyphs(mode):
    return mock.patch.dict(config.SETTINGS['waybar'], {'glyphs': mode})


class LabelTests(unittest.TestCase):
    def test_icons_are_font_awesome_by_default_and_plain_unicode_as_text(self):
        self.assertIn("font_family='Font Awesome 7 Free'", eta.icon(eta.BOLT))
        with glyphs('text'):
            self.assertEqual([eta.icon(g) for g in (eta.BOLT, eta.PLUG, eta.HOURGLASS, eta.POWER)],
                             ['⚡', '🔌', '⏳', '⚠'])

    def test_a_text_label_has_no_font_awesome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_eta.battery(root, status='Charging', current_now=2000000, voltage_now=16000000,
                             charge_now=1500000, charge_full=3000000, charge_control_end_threshold=90, capacity=50)
            live = test_eta.make_live(root)
            live.params = dict(test_eta.EtaTests.params)
            with glyphs('text'):
                text = live.estimate()['text']
            self.assertTrue(text.startswith('⚡ '))
            self.assertNotIn('Font Awesome', text)
            self.assertIsNone(PRIVATE_USE.search(text))


@unittest.skipIf(panel is None, "GTK 4's typelibs aren't available")
class PanelStyleTests(unittest.TestCase):
    def test_the_palette_is_built_in_and_the_thumbs_font_follows_the_setting(self):
        css = panel.stylesheet(theme=Path('/nonexistent/menu-theme.css'), glyphs='text')
        self.assertIn(b'@define-color menu_bg #222226;', css)  # Without the theme file: the same colours.
        self.assertNotIn(b'Font Awesome', css)
        self.assertIn(b'Font Awesome 7 Free', panel.stylesheet(glyphs='fontawesome'))
        with tempfile.NamedTemporaryFile(suffix='.css') as theme:
            theme.write(b'@define-color menu_bg #102030;')
            theme.flush()
            css = panel.stylesheet(theme=Path(theme.name), glyphs='text')
        self.assertLess(css.index(b'#222226'), css.index(b'#102030'))  # The theme's, after: it wins.
        self.assertEqual(set(panel.STACK), set(panel.COLORS))


if __name__ == '__main__':
    unittest.main()

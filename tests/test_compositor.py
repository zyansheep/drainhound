import json
import unittest

from wattif import compositor
from wattif.compositor import Desktop, Niri, Sway

# As `niri msg -j event-stream` prints them (the start recorded from niri 26.04,
# titles and layouts cut, a second output's workspace added): the burst it
# starts with, then a window opening, focus moving and a workspace switch.
NIRI_START = [
    {'WorkspacesChanged': {'workspaces': [
        {'id': 5, 'idx': 5, 'name': 'monitors', 'output': 'eDP-1', 'is_urgent': False, 'is_active': False,
         'is_focused': False, 'active_window_id': None},
        {'id': 2, 'idx': 2, 'name': 'notes', 'output': 'eDP-1', 'is_urgent': False, 'is_active': False,
         'is_focused': False, 'active_window_id': 11},
        {'id': 4, 'idx': 4, 'name': None, 'output': 'eDP-1', 'is_urgent': False, 'is_active': True,
         'is_focused': True, 'active_window_id': 30},
        {'id': 7, 'idx': 1, 'name': None, 'output': 'HDMI-A-1', 'is_urgent': False, 'is_active': True,
         'is_focused': False, 'active_window_id': None}]}},
    {'WindowsChanged': {'windows': [
        {'id': 30, 'title': 'T', 'app_id': 'vesktop', 'pid': 667715, 'workspace_id': 4, 'is_focused': True,
         'is_floating': False, 'is_urgent': False, 'focus_timestamp': {'secs': 75707, 'nanos': 671392850}},
        {'id': 11, 'title': 'T', 'app_id': 'md.Obsidian', 'pid': 2586, 'workspace_id': 2, 'is_focused': False,
         'is_floating': False, 'is_urgent': False, 'focus_timestamp': {'secs': 73535, 'nanos': 560686933}}]}},
    {'KeyboardLayoutsChanged': {'keyboard_layouts': {'names': ['English (US)'], 'current_idx': 0}}},
    {'OverviewOpenedOrClosed': {'is_open': False}},
    {'ConfigLoaded': {'failed': False}},
]
NIRI_LATER = [
    {'WindowOpenedOrChanged': {'window': {'id': 41, 'title': 'T', 'app_id': 'foot', 'pid': 9001, 'workspace_id': 4,
                                          'is_focused': True, 'is_floating': False, 'is_urgent': False,
                                          'focus_timestamp': {'secs': 75800, 'nanos': 0}}}},
    {'WindowFocusChanged': {'id': 30}},
    {'WindowOpenedOrChanged': {'window': {'id': 41, 'title': 'T2', 'app_id': 'foot', 'pid': 9001, 'workspace_id': 4,
                                          'is_focused': False, 'is_floating': False, 'is_urgent': False}}},
    {'WorkspaceActivated': {'id': 2, 'focused': True}},
    {'WindowFocusChanged': {'id': 11}},
    {'WindowClosed': {'id': 41}},
]

# Recorded from a headless sway 1.12 (`swaymsg -t subscribe -m '["window","workspace"]'`,
# get_tree and get_workspaces; nodes cut to the keys the backend reads and a few
# more). Three foot windows on workspace 1; then termB closes, termD opens and
# floats, termA goes to the scratchpad and focus goes to the empty workspace 4;
# then the windows' programs exit.
SWAY_TREE_1 = {"id": 1, "type": "root", "name": "root", "focused": False, "focus": [3, 4], "nodes": [
    {"id": 2147483647, "type": "output", "name": "__i3", "focused": False, "focus": [2147483646], "nodes": [
        {"id": 2147483646, "type": "workspace", "name": "__i3_scratch", "focused": False, "focus": []}]},
    {"id": 4, "type": "output", "name": "HEADLESS-2", "focused": False, "focus": [10], "nodes": [
        {"id": 10, "type": "workspace", "name": "3", "focused": False, "output": "HEADLESS-2", "focus": []}]},
    {"id": 3, "type": "output", "name": "HEADLESS-1", "focused": False, "focus": [5], "nodes": [
        {"id": 5, "type": "workspace", "name": "1", "focused": False, "output": "HEADLESS-1", "focus": [9, 8, 7],
         "nodes": [
             {"id": 7, "type": "con", "name": "foot", "pid": 861821, "app_id": "termA", "focused": False, "focus": []},
             {"id": 8, "type": "con", "name": "foot", "pid": 861867, "app_id": "termB", "focused": False, "focus": []},
             {"id": 9, "type": "con", "name": "foot", "pid": 861905, "app_id": "termC", "focused": True,
              "focus": []}]}]}]}
SWAY_WORKSPACES_1 = [
    {"id": 10, "type": "workspace", "name": "3", "output": "HEADLESS-2", "focused": False, "visible": True},
    {"id": 5, "type": "workspace", "name": "1", "output": "HEADLESS-1", "focused": True, "visible": True}]
SWAY_EVENTS_1 = [
    {"change": "close", "container": {"id": 8, "type": "con", "name": "foot", "pid": 861867, "app_id": "termB",
                                      "focused": False}},
]
SWAY_TREE_2 = {"id": 1, "type": "root", "name": "root", "focused": False, "focus": [3, 4], "nodes": [
    {"id": 2147483647, "type": "output", "name": "__i3", "focused": False, "focus": [2147483646], "nodes": [
        {"id": 2147483646, "type": "workspace", "name": "__i3_scratch", "focused": False, "focus": [7],
         "floating_nodes": [{"id": 7, "type": "floating_con", "name": "foot", "pid": 861821, "app_id": "termA",
                             "focused": False, "focus": []}]}]},
    {"id": 4, "type": "output", "name": "HEADLESS-2", "focused": False, "focus": [10], "nodes": [
        {"id": 10, "type": "workspace", "name": "3", "focused": False, "output": "HEADLESS-2", "focus": []}]},
    {"id": 3, "type": "output", "name": "HEADLESS-1", "focused": False, "focus": [13, 5], "nodes": [
        {"id": 5, "type": "workspace", "name": "1", "focused": False, "output": "HEADLESS-1", "focus": [12, 11],
         "nodes": [{"id": 11, "type": "con", "name": None, "focused": False, "focus": [9], "nodes": [
             {"id": 9, "type": "con", "name": "foot", "pid": 861905, "app_id": "termC", "focused": False,
              "focus": []}]}],
         "floating_nodes": [{"id": 12, "type": "floating_con", "name": "foot", "pid": 862233, "app_id": "termD",
                             "focused": False, "focus": []}]},
        {"id": 13, "type": "workspace", "name": "4", "focused": True, "output": "HEADLESS-1", "focus": []}]}]}
SWAY_WORKSPACES_2 = [
    {"id": 10, "type": "workspace", "name": "3", "output": "HEADLESS-2", "focused": False, "visible": True},
    {"id": 5, "type": "workspace", "name": "1", "output": "HEADLESS-1", "focused": False, "visible": False},
    {"id": 13, "type": "workspace", "name": "4", "output": "HEADLESS-1", "focused": True, "visible": True}]
SWAY_EVENTS_2 = [
    {"change": "new", "container": {"id": 12, "type": "con", "name": None, "pid": 862233, "app_id": "termD",
                                    "focused": False}},
    {"change": "title", "container": {"id": 12, "type": "con", "name": "foot", "pid": 862233, "app_id": "termD",
                                      "focused": False}},
    {"change": "focus", "container": {"id": 12, "type": "con", "name": "foot", "pid": 862233, "app_id": "termD",
                                      "focused": True}},
    {"change": "floating", "container": {"id": 12, "type": "floating_con", "name": "foot", "pid": 862233,
                                         "app_id": "termD", "focused": True}},
    {"change": "floating", "container": {"id": 7, "type": "floating_con", "name": "foot", "pid": 861821,
                                         "app_id": "termA", "focused": False}},
    {"change": "move", "container": {"id": 7, "type": "floating_con", "name": "foot", "pid": 861821,
                                     "app_id": "termA", "focused": False}},
    {"change": "init", "current": {"id": 13, "type": "workspace", "name": "4", "focused": False,
                                   "output": "HEADLESS-1"}, "old": None},
    {"change": "focus", "current": {"id": 13, "type": "workspace", "name": "4", "focused": True,
                                    "output": "HEADLESS-1"},
     "old": {"id": 5, "type": "workspace", "name": "1", "focused": False, "output": "HEADLESS-1"}},
]
SWAY_TREE_3 = {"id": 1, "type": "root", "name": "root", "focused": False, "focus": [3, 4], "nodes": [
    {"id": 2147483647, "type": "output", "name": "__i3", "focused": False, "focus": [2147483646], "nodes": [
        {"id": 2147483646, "type": "workspace", "name": "__i3_scratch", "focused": False, "focus": []}]},
    {"id": 4, "type": "output", "name": "HEADLESS-2", "focused": False, "focus": [10], "nodes": [
        {"id": 10, "type": "workspace", "name": "3", "focused": False, "output": "HEADLESS-2", "focus": []}]},
    {"id": 3, "type": "output", "name": "HEADLESS-1", "focused": False, "focus": [13], "nodes": [
        {"id": 13, "type": "workspace", "name": "4", "focused": True, "output": "HEADLESS-1", "focus": []}]}]}
SWAY_WORKSPACES_3 = [
    {"id": 10, "type": "workspace", "name": "3", "output": "HEADLESS-2", "focused": False, "visible": True},
    {"id": 13, "type": "workspace", "name": "4", "output": "HEADLESS-1", "focused": True, "visible": True}]
SWAY_EVENTS_3 = [  # A scratchpad window's program exits: sway says close, then move.
    {"change": "close", "container": {"id": 7, "type": "floating_con", "name": "foot", "pid": 861821,
                                      "app_id": "termA", "focused": False}},
    {"change": "move", "container": {"id": 7, "type": "con", "name": "foot", "pid": 861821, "app_id": "termA",
                                     "focused": False}},
    {"change": "close", "container": {"id": 9, "type": "con", "name": "foot", "pid": 861905, "app_id": "termC",
                                      "focused": False}},
    {"change": "close", "container": {"id": 12, "type": "floating_con", "name": "foot", "pid": 862233,
                                      "app_id": "termD", "focused": False}},
    {"change": "empty", "current": {"id": 5, "type": "workspace", "name": "1", "focused": False,
                                    "output": "HEADLESS-1"}, "old": None},
]


class Buffer:
    def __init__(self, text=''):
        self.buffer = text


class NiriTests(unittest.TestCase):
    def test_the_first_burst_is_everything_and_events_follow(self):
        niri = Niri()
        self.assertFalse(niri.known)
        niri.event(NIRI_START[0])
        self.assertFalse(niri.known)  # Workspaces alone: windows not yet.
        for event in NIRI_START[1:]:
            niri.event(event)
        self.assertTrue(niri.known)
        self.assertEqual(niri.focused, 30)
        self.assertEqual(niri.windows[30], {'pid': 667715, 'workspace': 4, 'focus': 75707.67139285})
        self.assertEqual(niri.workspaces[7], {'output': 'HDMI-A-1', 'active': True})
        self.assertEqual([w for w in niri.windows if niri.shown(w)], [30])
        for event in NIRI_LATER[:3]:
            niri.event(event)
        self.assertEqual(niri.focused, 30)
        self.assertEqual(niri.windows[41]['focus'], 75800.0)  # A change without a timestamp keeps the last one.
        niri.event(NIRI_LATER[3])  # Workspace 2 active on eDP-1, instead of 4; HDMI-A-1's is still active.
        self.assertEqual({w: k['active'] for w, k in niri.workspaces.items()}, {5: False, 2: True, 4: False, 7: True})
        self.assertEqual({w for w in niri.windows if niri.shown(w)}, {11})
        for event in NIRI_LATER[4:]:
            niri.event(event)
        self.assertEqual((niri.focused, niri.pid(11), set(niri.windows)), (11, 2586, {30, 11}))

    def test_clearing_forgets_everything_until_the_next_burst(self):
        niri = Niri()
        for event in NIRI_START:
            niri.event(event)
        niri.clear()
        self.assertEqual((niri.known, niri.windows, niri.workspaces, niri.focused), (False, {}, {}, None))
        niri.event(NIRI_START[1])
        self.assertFalse(niri.known)  # Both snapshots again.
        niri.event(NIRI_START[0])
        self.assertTrue(niri.known)

    def test_lines_split_across_reads(self):
        stream = Buffer(json.dumps(NIRI_START[0]) + '\n' + json.dumps(NIRI_START[1])[:40])
        self.assertEqual(Niri().decode(stream), [NIRI_START[0]])
        stream.buffer += json.dumps(NIRI_START[1])[40:] + '\n'
        self.assertEqual(Niri().decode(stream), [NIRI_START[1]])
        self.assertEqual(stream.buffer, '')


class SwayTests(unittest.TestCase):
    def setUp(self):
        self.now, self.state, self.asked = 100.0, (SWAY_TREE_1, SWAY_WORKSPACES_1), []

        def query(kind):
            self.asked.append(kind)
            return {'get_tree': self.state[0], 'get_workspaces': self.state[1]}[kind] if self.state else None
        self.sway = Sway(clock=lambda: self.now, query=query)

    def test_a_snapshot_on_start_then_events(self):
        sway = self.sway
        self.assertFalse(sway.known)
        sway.started()
        self.assertTrue(sway.known)
        self.assertEqual({w: (k['pid'], k['workspace']) for w, k in sway.windows.items()},
                         {7: (861821, 5), 8: (861867, 5), 9: (861905, 5)})
        self.assertEqual(sway.focused, 9)
        self.assertEqual(sway.windows[9]['focus'], 100.0)  # Focused now; the others: not known.
        self.assertIsNone(sway.windows[7]['focus'])
        self.assertEqual(sway.workspaces, {10: {'output': 'HEADLESS-2', 'active': True},
                                           5: {'output': 'HEADLESS-1', 'active': True}})
        asked = len(self.asked)
        for event in SWAY_EVENTS_1:  # Closing and focusing need no new snapshot.
            sway.event(event)
        self.now = 130.0
        sway.event({"change": "focus", "container": {"id": 7, "type": "con", "pid": 861821, "focused": True}})
        self.assertEqual(len(self.asked), asked)
        self.assertEqual((sway.focused, set(sway.windows)), (7, {7, 9}))
        self.assertEqual((sway.windows[7]['focus'], sway.windows[9]['focus']), (130.0, 130.0))  # 9 had it until now.

    def test_new_and_moved_windows_and_workspace_changes_read_the_tree_again(self):
        sway = self.sway
        sway.started()
        sway.event(SWAY_EVENTS_1[0])
        self.state, self.now = (SWAY_TREE_2, SWAY_WORKSPACES_2), 200.0
        for event in SWAY_EVENTS_2:
            sway.event(event)
        self.assertEqual({w: k['workspace'] for w, k in sway.windows.items()},
                         {7: 2147483646, 9: 5, 12: 5})  # The scratchpad's, a nested tile, a floating one.
        self.assertIsNone(sway.focused)  # An empty workspace has focus.
        self.assertEqual(sway.windows[12]['focus'], 200.0)  # termD had it until then.
        self.assertEqual([w for w in sway.windows if sway.shown(w)], [])  # Workspace 1 is hidden, so is the scratchpad.
        self.assertEqual(sway.workspaces[13], {'output': 'HEADLESS-1', 'active': True})
        self.state = (SWAY_TREE_3, SWAY_WORKSPACES_3)
        for event in SWAY_EVENTS_3:
            sway.event(event)
        self.assertEqual((sway.windows, set(sway.workspaces), sway.known), ({}, {10, 13}, True))

    def test_a_failed_snapshot_or_a_closed_stream_forgets_everything(self):
        sway = self.sway
        sway.started()
        self.state = None  # sway is exiting.
        sway.event(SWAY_EVENTS_2[0])
        self.assertEqual((sway.known, sway.windows, sway.focused), (False, {}, None))
        self.state = (SWAY_TREE_1, SWAY_WORKSPACES_1)
        sway.event({"change": "focus", "container": {"id": 9, "type": "con", "pid": 861905, "focused": True}})
        self.assertTrue(sway.known)  # A window it doesn't know: read again.
        sway.clear()
        self.assertEqual((sway.known, sway.windows, sway.workspaces), (False, {}, {}))

    def test_events_pretty_printed_or_one_per_line(self):
        stream = Buffer(json.dumps(SWAY_EVENTS_1[0], indent=2) + '\n' + json.dumps(SWAY_EVENTS_2[0]) + '\n'
                        + json.dumps(SWAY_EVENTS_2[1], indent=2)[:30])
        self.assertEqual(self.sway.decode(stream), [SWAY_EVENTS_1[0], SWAY_EVENTS_2[0]])
        stream.buffer += json.dumps(SWAY_EVENTS_2[1], indent=2)[30:]
        self.assertEqual(self.sway.decode(stream), [SWAY_EVENTS_2[1]])


class DetectTests(unittest.TestCase):
    def test_the_backend_follows_the_session(self):
        self.assertIsInstance(compositor.detect({'NIRI_SOCKET': '/run/user/1000/niri.sock'}), Niri)
        self.assertIsInstance(compositor.detect({'SWAYSOCK': '/run/user/1000/sway-ipc.sock'}), Sway)
        for environ in ({'HYPRLAND_INSTANCE_SIGNATURE': 'abc'}, {'XDG_CURRENT_DESKTOP': 'GNOME'}, {}):
            desktop = compositor.detect(environ)
            self.assertIs(type(desktop), Desktop)
            self.assertEqual((desktop.command, desktop.known), (None, False))  # Nothing to follow, nothing known.
        self.assertEqual(Niri.command, ('niri', 'msg', '-j', 'event-stream'))
        self.assertEqual([compositor.unsupported(e) for e in ({'HYPRLAND_INSTANCE_SIGNATURE': 'abc'},
                                                              {'XDG_CURRENT_DESKTOP': 'GNOME'}, {})],
                         ['Hyprland', 'GNOME', 'this session'])
        self.assertEqual(Sway.command[:3], ('swaymsg', '-t', 'subscribe'))


if __name__ == '__main__':
    unittest.main()

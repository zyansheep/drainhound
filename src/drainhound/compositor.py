"""The desktop's windows, from the compositor's IPC, for drainhound throttle and
drainhound freeze: each window's pid and app id, its workspace and when it last had
focus (CLOCK_MONOTONIC s), which window has focus, and which workspaces are
shown on their outputs. An X11 window's pid is the client's where the
compositor knows it (sway: _NET_WM_PID); niri gives xwayland-satellite's, so
there the app id (the X11 class) is what says whose it is.

A backend follows its compositor's event stream (a child process whose stdout
throttle.run reads). `known` says whether its state is live: false until the
first full snapshot, and false again once the stream ends, when everything is
forgotten (clear()). Before, a dead niri stream left its last windows behind,
and drainhound freeze went on freezing apps you were using.

    niri    `niri msg -j event-stream`: it begins with every window and workspace
    sway    `swaymsg -t subscribe -m '["window","workspace"]'`; get_tree and
            get_workspaces say where windows are and which workspaces are shown
            (its events don't), and focus times are kept here (sway has none)

detect() picks one from the environment (NIRI_SOCKET, SWAYSOCK) whose socket
is there. Elsewhere (Hyprland, GNOME, KDE, X11) nothing is known: drainhound
throttle caps nothing and drainhound freeze freezes nothing.
"""
import json
import os
import stat
import subprocess
import time


class Desktop:
    """No compositor to follow: nothing is ever known."""
    name = None
    command = None  # The event stream's argv, or None (nothing to follow).

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.clear()

    def clear(self):
        """Forget every window and workspace: the stream ended (the compositor
        exited, or restarts), so none of it can be trusted."""
        # Window id → {'pid', 'app_id' (Wayland's, or the X11 class; None: none), 'workspace' (its id),
        # 'focus' (CLOCK_MONOTONIC s it last had focus, None: unknown)}.
        self.windows = {}
        self.focused = None  # The focused window's id (None: none, or a layer surface, or an empty workspace).
        self.workspaces = {}  # Workspace id → {'output', 'active': shown on its output}.
        self.known = False

    def started(self):
        """The stream has just (re)started: fetch what it doesn't send itself."""

    def decode(self, stream):
        """The complete events in a throttle.Stream's buffer (it keeps the rest)."""
        return json_lines(stream)

    def event(self, event):
        """One event from the stream."""

    def pid(self, window):
        return self.windows.get(window, {}).get('pid')

    def shown(self, window):
        """Whether the window is on a workspace shown on its output: maybe on screen
        (niri doesn't say which of a workspace's columns are in view)."""
        workspace = self.workspaces.get(self.windows.get(window, {}).get('workspace'))
        return bool(workspace and workspace['active'])


class Niri(Desktop):
    name = 'niri'
    command = ('niri', 'msg', '-j', 'event-stream')
    SNAPSHOTS = {'WindowsChanged', 'WorkspacesChanged'}  # The stream's first events: everything.

    def clear(self):
        super().clear()
        self.snapshots = set()

    def window(self, window):
        """niri's focus_timestamp is CLOCK_MONOTONIC; a change without one keeps the last."""
        stamp = window.get('focus_timestamp')
        focus = (stamp.get('secs', 0) + stamp.get('nanos', 0) / 1e9 if stamp
                 else self.windows.get(window['id'], {}).get('focus'))
        self.windows[window['id']] = {'pid': window.get('pid'), 'app_id': window.get('app_id'),
                                      'workspace': window.get('workspace_id'), 'focus': focus}

    def event(self, event):
        """One line of `niri msg -j event-stream`."""
        if 'WindowsChanged' in event:
            windows = event['WindowsChanged']['windows']
            self.windows = {}
            for window in windows:
                self.window(window)
            self.focused = next((w['id'] for w in windows if w.get('is_focused')), None)
        elif 'WindowOpenedOrChanged' in event:
            window = event['WindowOpenedOrChanged']['window']
            self.window(window)
            if window.get('is_focused'):
                self.focused = window['id']
        elif 'WindowClosed' in event:
            self.windows.pop(event['WindowClosed']['id'], None)
        elif 'WindowFocusChanged' in event:
            self.focused = event['WindowFocusChanged']['id']
        elif 'WorkspacesChanged' in event:
            self.workspaces = {w['id']: {'output': w.get('output'), 'active': bool(w.get('is_active'))}
                               for w in event['WorkspacesChanged']['workspaces']}
        elif 'WorkspaceActivated' in event:  # Active on its output, instead of the one there was.
            activated = event['WorkspaceActivated']['id']
            output = self.workspaces.get(activated, {}).get('output')
            for workspace, known in self.workspaces.items():
                if known['output'] == output:
                    known['active'] = workspace == activated
        self.snapshots |= self.SNAPSHOTS & set(event)
        self.known = self.snapshots == self.SNAPSHOTS


def swaymsg(kind, timeout=2):
    """`swaymsg -t <kind>`'s reply, or None."""
    try:
        done = subprocess.run(['swaymsg', '-r', '-t', kind], capture_output=True, timeout=timeout)
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class Sway(Desktop):
    """sway's window events carry the window but not its workspace, and its
    workspace events don't say which workspaces are now shown, so a new or
    moved window, or any workspace change, reads the tree and the workspaces
    again (a few ms; focus changes, the frequent events, don't). The snapshot is
    read just after the subscription starts, so an event in between can be
    missed until the next one."""
    name = 'sway'
    command = ('swaymsg', '-t', 'subscribe', '-m', '["window","workspace"]')
    REREAD = ('new', 'move', 'floating')  # Window changes that can move it to another workspace.

    def __init__(self, clock=time.monotonic, query=swaymsg):
        self.query = query
        super().__init__(clock)

    def decode(self, stream):
        return json_values(stream)  # One object per event: on one line, or pretty-printed by older swaymsg.

    def started(self):
        self.refresh()

    def focus(self, window):
        """`window` has focus now; the one that had it had it until now."""
        now = self.clock()
        for had in {self.focused, window} - {None}:
            if had in self.windows:
                self.windows[had]['focus'] = now
        self.focused = window

    def refresh(self):
        tree, workspaces = self.query('get_tree'), self.query('get_workspaces')
        if not isinstance(tree, dict) or not isinstance(workspaces, list):
            self.clear()  # sway is going away (the stream ends next), or its reply is broken.
            return
        before, windows, focused = self.windows, {}, None
        for workspace, node in views(tree):
            windows[node['id']] = {'pid': node.get('pid'), 'workspace': workspace,
                                   'app_id': node.get('app_id') or (node.get('window_properties') or {}).get('class'),
                                   'focus': before.get(node['id'], {}).get('focus')}
            if node.get('focused'):
                focused = node['id']
        self.windows = windows
        self.workspaces = {w['id']: {'output': w.get('output'), 'active': bool(w.get('visible'))}
                           for w in workspaces if 'id' in w}
        if focused != self.focused:
            self.focus(focused)
        self.known = True

    def event(self, event):
        """One event from `swaymsg -t subscribe -m '["window","workspace"]'`."""
        change = event.get('change')
        if 'container' in event:
            window = (event.get('container') or {}).get('id')
            if change == 'close':
                self.windows.pop(window, None)
                if self.focused == window:
                    self.focused = None  # A focus event follows, if anything gets it.
            elif change == 'focus' and window in self.windows:
                self.focus(window)
            elif change in self.REREAD or change == 'focus':  # A window not seen yet, too.
                self.refresh()
            # title, mark, urgent, fullscreen_mode: nothing here depends on them.
        elif change is not None and change not in ('rename', 'urgent'):  # A workspace event.
            self.refresh()


def views(node, workspace=None):
    """(workspace id, node) for every window in sway's tree, tiled or floating
    (the scratchpad's are on its hidden workspace, never shown). A window has a
    pid, if only 0 (an X11 client that doesn't say its own); a split doesn't."""
    if node.get('type') == 'workspace':
        workspace = node.get('id')
    if 'pid' in node and 'id' in node:
        yield workspace, node
    for child in (node.get('nodes') or []) + (node.get('floating_nodes') or []):
        yield from views(child, workspace)


BACKENDS = (('NIRI_SOCKET', Niri), ('SWAYSOCK', Sway))


def is_socket(path):
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def detect(environ=None, clock=time.monotonic, socket=is_socket):
    """The backend for the compositor this session runs (a Desktop that knows
    nothing if it is none of them). A NIRI_SOCKET or SWAYSOCK whose socket
    isn't there is a compositor that has exited (a user service can keep an
    old session's environment), as drainhound experiment reads it too."""
    environ = os.environ if environ is None else environ
    for variable, backend in BACKENDS:
        if environ.get(variable) and socket(environ[variable]):
            return backend(clock=clock)
    return Desktop(clock=clock)


def unsupported(environ=None, socket=is_socket):
    """What this session runs instead of a supported compositor, for messages."""
    environ = os.environ if environ is None else environ
    if environ.get('HYPRLAND_INSTANCE_SIGNATURE'):
        return 'Hyprland'
    session = environ.get('XDG_CURRENT_DESKTOP') or 'this session'
    for variable, _ in BACKENDS:
        if environ.get(variable) and not socket(environ[variable]):
            return f'{session} with a stale {variable} (no socket at {environ[variable]})'
    return session


def json_lines(stream):
    """One JSON value per line (niri): the complete lines in `stream.buffer`."""
    *complete, stream.buffer = stream.buffer.split('\n')
    return [json.loads(line) for line in complete if line.strip()]


def json_values(stream):
    """JSON values one after another, however they are broken into lines
    (pw-dump prints one array per batch of changes, swaymsg an object per event):
    the complete ones in `stream.buffer`."""
    decoder, out = json.JSONDecoder(), []
    while True:
        text = stream.buffer.lstrip()
        try:
            value, end = decoder.raw_decode(text)
        except ValueError:
            stream.buffer = text
            return out
        out.append(value)
        stream.buffer = text[end:]

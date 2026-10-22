#!/usr/bin/env python3
"""Layer-shell battery panel: history timeline, sleep drain, what-if runtime and
power experiments, drawn from the power log (see report.py)."""
import json
import math
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")  # compute_bounds() returns a Graphene.Rect.
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

try:  # Optional: without it (or a compositor that has it) the panel is a normal window.
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as GtkLayerShell  # noqa: E402
except (ValueError, ImportError):
    GtkLayerShell = None

from . import config
from . import eta  # noqa: E402,F401 - first: it limits BLAS threads before numpy loads
from . import experiment  # noqa: E402
from . import qol  # noqa: E402
from . import report  # noqa: E402
from . import trail  # noqa: E402
from .panel_data import (GONE, NOTE_SECONDS, History, answers, apply_note, banner_text, command_note,  # noqa: E402
                         duration, finite, gather, ghost, health_text, interaction_clear, lever_rows,
                         long_duration, no_levers, picker, processes, pull_labels, read_battery, read_estimate,
                         read_futures, read_snapshot, state_text, typical_text, value_text, ways, week_sleeps,
                         what_if_note)
from .panel_data import chart_colors  # noqa: E402
from .report import PHRASES, gain_parts  # noqa: E402

# The stack's colours, bottom up; the app groups' follow their configured names (panel_data.chart_colors).
COLORS = chart_colors(report.GROUPS)
STACK = list(COLORS)
SURFACE = tuple(int(trail.GROUND[i:i + 2], 16) / 255 for i in (1, 3, 5))
RANGES = {'6 h': 6 * 3600, 'Day': 24 * 3600, 'Week': 7 * 24 * 3600}
BATTERY_HOURS = [1, 2, 4, 8, 24]  # Experiment length choices, in hours of battery time.
DEBOUNCE = 0.15  # s: reloads this close together (holding ← or →) load only the last one.
RUNTIME = config.RUNTIME
DRAINHOUND = config.self_command()  # The panel's buttons run `drainhound experiment …` and `drainhound vote …`.


def rgb(color, alpha=1.0):
    color = color.lstrip('#')
    return tuple(int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)) + (alpha,)


def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def wrapped(**props):
    """A wrapping label. GTK4 reports a wrapping label's whole text as its natural
    width; the cap keeps the layer surface at the card's width."""
    return Gtk.Label(**{'xalign': 0, 'wrap': True, 'max_width_chars': 100, 'width_chars': 20, **props})


def render(directory, range_name='Day', offset=0):
    """Draw the hero and what's pulling to PNGs without opening a window (`render DIR`), with the
    runtime files as they are now (futures.json, snapshot.json)."""
    import cairo
    length = RANGES[range_name]
    today = time.localtime()
    midnight = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
    end = (time.time() if range_name == '6 h' else midnight + 86400) + offset * length
    result = gather(end - length, end)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    now, at_now = time.time(), offset == 0
    estimate = read_estimate(RUNTIME / 'eta.json', now)
    hero, pull = trail.HeroChart(), trail.PullChart()
    hero.set_data(result.get('points') or [], result.get('span') or (end - length, end), result.get('sleeps') or [],
                  range_name, at_now)
    hero.set_live(read_futures(RUNTIME / 'futures.json', now), (estimate or {}).get('pct'), now)
    pull.set_data(result.get('points') or [], result.get('span') or (end - length, end), at_now)
    pull.set_labels(pull_labels(read_snapshot(RUNTIME / 'snapshot.json', now)))
    pull.set_now(now)
    for name, chart, height in (('hero', hero, 304), ('pull', pull, 176)):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 840, height)
        cr = cairo.Context(surface)
        cr.set_source_rgb(*SURFACE)
        cr.paint()
        chart.render(cr, 840, height)
        surface.write_to_png(str(directory / f'{name}.png'))
        print(directory / f'{name}.png')


class BatteryPanel(Adw.Application):
    def __init__(self):
        super().__init__(application_id=config.APP_ID, flags=Gio.ApplicationFlags.IS_SERVICE)
        self.window = None
        self.range_name, self.offset = 'Day', 0
        self.focused_once = False
        self.generation, self.busy, self.shown = 0, False, (0, 1)
        self.loaded = None
        self.history = History()
        # One loader thread takes the latest reload request (see `work`).
        self.wake, self.request, self.requested, self.worker = threading.Condition(), None, 0.0, None
        self.poll = 0
        self.catalog, self.units, self.effects = [], [], []
        self.psr, self.psr_arm = None, None
        self.result, self.best, self.futures, self.full_minutes, self.period_text = None, None, None, None, ''
        self.minutes_hours = 4  # How long freezing experiments run, in hours of battery time.
        self.lever_names, self.lever_widgets, self.hints = None, {}, {}
        self.unit_names, self.freeze_allowed = [], False
        self.syncing = False  # Set while widgets are updated from state, so they don't echo it back.
        self.note, self.note_until = '', None  # The last command's outcome, under the banner (see set_note).
        self.connect('startup', self.startup)
        self.connect('activate', self.toggle)

    # --- window ------------------------------------------------------------------
    def startup(self, *_):
        self.hold()
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        action = Gio.SimpleAction.new('toggle', None)
        action.connect('activate', self.toggle)
        self.add_action(action)
        # `drainhound panel <tab>` opens directly on timeline or experiments.
        show = Gio.SimpleAction.new('show', GLib.VariantType.new('s'))
        show.connect('activate', self.show_page)
        self.add_action(show)
        css = Gtk.CssProvider()
        css.load_from_data(stylesheet())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.build()
        self.window.realize()

    def build(self):
        self.window = Adw.ApplicationWindow(application=self, title='Battery')
        self.window.connect('close-request', self.close)
        self.window.connect('notify::is-active', self.focus_changed)
        # A layer-shell overlay under the bar where the compositor has it
        # (wlroots compositors, niri, KDE); elsewhere (GNOME, X11) a normal window.
        self.layered = GtkLayerShell is not None and GtkLayerShell.is_supported()
        if self.layered:
            self.window.set_decorated(False)
            GtkLayerShell.init_for_window(self.window)
            GtkLayerShell.set_namespace(self.window, 'drainhound-panel')
            GtkLayerShell.set_layer(self.window, GtkLayerShell.Layer.OVERLAY)
            # DRAINHOUND_PANEL_PASSIVE=1 shows the panel without ever taking keyboard focus.
            keyboard = (GtkLayerShell.KeyboardMode.NONE if os.environ.get('DRAINHOUND_PANEL_PASSIVE')
                        else GtkLayerShell.KeyboardMode.ON_DEMAND)
            GtkLayerShell.set_keyboard_mode(self.window, keyboard)
            GtkLayerShell.set_anchor(self.window, GtkLayerShell.Edge.TOP, True)
            GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.TOP, int(config.get('panel', 'margin') or 0))
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.key)
        self.window.add_controller(keys)
        # Close on focus loss once the panel has had focus: niri focuses an
        # on-demand panel when it opens, so a click on another window dismisses
        # it, like Audio and Wi-Fi. (A freshly mapped surface can report
        # inactive before niri focuses it, hence waiting for the first focus.)
        # The pointer entering also counts, for DRAINHOUND_PANEL_PASSIVE.
        engaged = Gtk.EventControllerMotion()
        engaged.connect('enter', lambda *_: setattr(self, 'focused_once', True))
        self.window.add_controller(engaged)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.set_size_request(880, -1)
        # Wrapping labels and the legend report their one-line width as natural
        # width; the clamp keeps the layer surface at the intended size.
        clamp = Adw.Clamp(maximum_size=880, tightening_threshold=880, child=card)
        outer = Gtk.Box()
        outer.set_name('battery-card')
        outer.append(clamp)
        # GTK's tooltips wait half a second (fixed in GTK 4); hints show at once.
        self.hint_anchor, self.hint_label = outer, wrapped(max_width_chars=60)
        self.hint_popover = Gtk.Popover(autohide=False, can_focus=False, position=Gtk.PositionType.TOP,
                                        child=self.hint_label)
        self.hint_popover.add_css_class('hint')
        self.hint_popover.set_parent(outer)
        header = Gtk.Box(spacing=10)
        header.append(trail.Paw())
        title = Gtk.Label(label='Battery', xalign=0)
        title.add_css_class('panel-title')
        header.append(title)
        # Charging · 34% · 29.9 W in; the numbers that answer "how long" lead the Timeline.
        self.state = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.state.add_css_class('state')
        header.append(self.state)
        right_now = Gtk.Label(label='RIGHT NOW')
        right_now.add_css_class('lab')
        header.append(right_now)
        for choice, words in (('up', 'Especially good right now'), ('down', 'Worse than it should be right now')):
            thumb = Gtk.Label(label=qol.GLYPHS[choice])
            thumb.add_css_class('thumb')
            button = Gtk.Button(child=thumb, valign=Gtk.Align.CENTER)
            button.add_css_class('vote')
            button.add_css_class(f'vote-{choice}')
            button.update_property([Gtk.AccessibleProperty.LABEL], [words])
            self.hint(button, words)
            button.connect('clicked', lambda _b, c=choice: self.command([*DRAINHOUND, 'vote', c], done=None))
            header.append(button)
        close = Gtk.Button.new_from_icon_name('window-close-symbolic')
        close.add_css_class('flat')
        close.update_property([Gtk.AccessibleProperty.LABEL], ['Close battery panel'])
        close.connect('clicked', self.close)
        header.append(close)
        card.append(header)

        # One filter row above everything it scopes, and the tabs.
        controls = Gtk.Box(spacing=6)
        controls.add_css_class('controls')
        group = None
        ranges = Gtk.Box()
        ranges.add_css_class('linked')
        for name in RANGES:
            button = Gtk.ToggleButton(label=name)
            button.set_group(group)
            group = group or button
            button.set_active(name == self.range_name)
            button.connect('toggled', lambda b, n=name: b.get_active() and self.set_range(n))
            ranges.append(button)
        controls.append(ranges)
        back = Gtk.Button.new_from_icon_name('go-previous-symbolic')
        back.connect('clicked', lambda _b: self.shift(-1))
        controls.append(back)
        self.period = Gtk.Label(width_chars=24)
        controls.append(self.period)
        self.forward = Gtk.Button.new_from_icon_name('go-next-symbolic')
        self.forward.connect('clicked', lambda _b: self.shift(1))
        controls.append(self.forward)
        now = Gtk.Button(label='Now')
        now.connect('clicked', lambda _b: self.shift(None))
        controls.append(now)
        self.spinner = Gtk.Spinner()
        controls.append(self.spinner)
        card.append(controls)
        # A part of the data that failed: the rest still shows (panel_data.gather).
        self.problems = wrapped(visible=False)
        self.problems.add_css_class('problem')
        card.append(self.problems)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.stack, hexpand=True, halign=Gtk.Align.END)
        switcher.add_css_class('tabs')
        controls.append(switcher)
        card.append(self.stack)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        # The answers first, as one line of numbers (panel_data.answers), from eta.json every 2 s.
        self.answer_slots = []
        strip = Gtk.Box(spacing=20)
        for _ in range(4):
            slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            label, value = Gtk.Label(xalign=0), Gtk.Label(xalign=0)
            label.add_css_class('lab')
            caption = wrapped(max_width_chars=34, width_chars=10)
            caption.add_css_class('cap')
            for widget in (label, value, caption):
                slot.append(widget)
            strip.append(slot)
            self.answer_slots.append((slot, label, value, caption))
        page.append(strip)
        # Where the battery has been, and where it can go.
        self.hero = trail.HeroChart()
        page.append(self.hero)
        pull_head = Gtk.Box(spacing=10)
        lab = Gtk.Label(label="WHAT'S PULLING", valign=Gtk.Align.BASELINE_CENTER)
        lab.add_css_class('lab')
        pull_head.append(lab)
        self.pull_now = Gtk.Label(valign=Gtk.Align.BASELINE_CENTER)
        self.pull_now.add_css_class('num-small')
        pull_head.append(self.pull_now)
        self.pull_caption = Gtk.Label(xalign=0, hexpand=True, valign=Gtk.Align.BASELINE_CENTER, wrap=True,
                                      max_width_chars=100)
        self.pull_caption.add_css_class('cap')
        pull_head.append(self.pull_caption)
        page.append(pull_head)
        self.pull = trail.PullChart()
        page.append(self.pull)
        # Looking at a past period, there is no live split to label the layers: a legend instead.
        self.legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=9, column_spacing=12,
                                  visible=False)
        page.append(self.legend)

        lower = Gtk.Box(spacing=24)
        lower.add_css_class('lower')
        further = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        # Columns line up through a size group per side: what and where from, the cone, the value and Apply.
        self.ways_lead = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.ways_tail = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        head = Gtk.Box(spacing=6)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.ways_lead.add_widget(titles)
        lab = Gtk.Label(label='WAYS TO GO FURTHER', xalign=0)
        lab.add_css_class('lab')
        titles.append(lab)
        caption = wrapped(label='Minutes per full charge, 90% intervals. Hover one to see it on the trail.',
                          max_width_chars=44)
        caption.add_css_class('cap')
        titles.append(caption)
        head.append(titles)
        self.ways_axis = trail.ConeAxis()
        head.append(self.ways_axis)
        tail = Gtk.Box()
        self.ways_tail.add_widget(tail)
        head.append(tail)
        further.append(head)
        self.ways_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        further.append(self.ways_list)
        self.ways_more = Gtk.Label(xalign=0, wrap=True, max_width_chars=70)
        self.ways_more.add_css_class('cap')
        further.append(self.ways_more)
        self.ways_already = wrapped(max_width_chars=70)
        self.ways_already.add_css_class('already')
        further.append(self.ways_already)
        self.timeline_note = wrapped(max_width_chars=70, visible=False)
        self.timeline_note.add_css_class('cap')
        further.append(self.timeline_note)
        lower.append(further)
        aside = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=280)
        sleep = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        sleep.add_css_class('card')
        lab = Gtk.Label(label='SLEEP', xalign=0)
        lab.add_css_class('lab')
        sleep.append(lab)
        line = Gtk.Box(spacing=8)
        self.sleep_big = Gtk.Label(xalign=0, hexpand=True)
        self.sleep_big.add_css_class('strong')
        line.append(self.sleep_big)
        self.sleep_caption = Gtk.Label(xalign=1)
        self.sleep_caption.add_css_class('cap')
        line.append(self.sleep_caption)
        sleep.append(line)
        self.sleep_dots = trail.SleepDots()
        sleep.append(self.sleep_dots)
        self.sleep_note = wrapped(max_width_chars=40)
        self.sleep_note.add_css_class('cap')
        sleep.append(self.sleep_note)
        aside.append(sleep)
        busy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        busy.add_css_class('card')
        line = Gtk.Box()
        lab = Gtk.Label(label='NOW', xalign=0, hexpand=True)
        lab.add_css_class('lab')
        line.append(lab)
        caption = Gtk.Label(label='CPU, past minute')
        caption.add_css_class('cap')
        line.append(caption)
        busy.append(line)
        self.procs = trail.ProcessList()
        busy.append(self.procs)
        aside.append(busy)
        lower.append(aside)
        page.append(lower)
        footer = Gtk.Box(spacing=10)
        about = Gtk.Expander(label='About these numbers', hexpand=True)
        self.about = wrapped(max_width_chars=90)
        self.about.add_css_class('cap')
        about.set_child(self.about)
        footer.append(about)
        self.power_log = Gtk.Label(xalign=1, valign=Gtk.Align.START)
        self.power_log.add_css_class('cap')
        footer.append(self.power_log)
        page.append(footer)
        scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=600, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(page)
        self.stack.add_titled(scroller, 'timeline', 'Timeline')
        self.stack.add_titled(self.build_experiments(), 'experiments', 'Experiments')
        self.window.set_content(outer)

    def build_experiments(self):
        """Automatic experiments: what the runner is doing; what the goal would change, whether more
        testing pays, and the votes; what each setting saves on one axis (a forest plot), grouped by
        how automatic runs treat it; and the lock-screen and boot experiments. Built once;
        fill_experiments updates it in place."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        status = Gtk.Box(spacing=14)
        status.add_css_class('card')
        self.auto_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.auto_switch.connect('state-set', self.switch_experiments)
        self.hint(self.auto_switch, 'Off (drainhound experiment disable) puts every setting back to stock, '
                                    'applied ones too')
        status.append(self.auto_switch)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(label='Automatic experiments', xalign=0)
        title.add_css_class('strong')
        info.append(title)
        self.banner = wrapped(hexpand=True)
        info.append(self.banner)
        status.append(info)
        self.stop_button = Gtk.Button(label='Stop', valign=Gtk.Align.CENTER)
        self.stop_button.add_css_class('destructive-action')
        self.stop_button.connect('clicked', lambda _b: self.command([*DRAINHOUND, 'experiment', 'stop']))
        status.append(self.stop_button)
        self.resume_button = Gtk.Button(label='Resume now', valign=Gtk.Align.CENTER)
        self.hint(self.resume_button, 'End the rest after a 👎 (drainhound vote resume)')
        self.resume_button.connect('clicked', lambda _b: self.command([*DRAINHOUND, 'vote', 'resume']))
        status.append(self.resume_button)
        page.append(status)

        trio = Gtk.Box(spacing=12, homogeneous=True)
        goal = self.card('What the goal would change')
        goals = Gtk.Box(halign=Gtk.Align.START)
        goals.add_css_class('linked')
        self.goal_buttons, group = {}, None
        for name, label in (('battery', 'Battery'), ('comfort', 'Comfort')):
            button = Gtk.ToggleButton(label=label, sensitive=False)  # Until the first load.
            button.set_group(group)
            group = group or button
            button.connect('toggled', lambda b, n=name: b.get_active() and not self.syncing and self.set_goal(n))
            goals.append(button)
            self.goal_buttons[name] = button
        goal.append(goals)
        self.best_label = wrapped(max_width_chars=30)
        self.best_label.add_css_class('cap')
        goal.append(self.best_label)
        self.apply_button = Gtk.Button(label='Apply', halign=Gtk.Align.START, visible=False)
        self.apply_button.add_css_class('go')
        self.hint(self.apply_button, 'Until reboot, on battery: on AC they go back to stock and return on '
                                     'battery. Risky settings and quarantined ones are left out; the note under '
                                     'the banner says which.')
        self.apply_button.connect('clicked', lambda _b: self.apply_best())
        goal.append(self.apply_button)
        trio.append(goal)
        worth = self.card('Is more testing worth it?')
        self.worth_chart = trail.WorthChart()
        worth.append(self.worth_chart)
        self.worth = wrapped(max_width_chars=40)
        self.worth.add_css_class('cap')
        worth.append(self.worth)
        trio.append(worth)
        votes = self.card('Right now the laptop is')
        for choice, label in (('up', 'Especially good'), ('down', 'Worse than it should be')):
            content = Gtk.Box(spacing=8)
            thumb = Gtk.Label(label=qol.GLYPHS[choice])
            thumb.add_css_class('thumb')
            content.append(thumb)
            content.append(Gtk.Label(label=label))
            button = Gtk.Button(child=content)
            button.add_css_class(f'vote-{choice}')
            button.connect('clicked', lambda _b, c=choice: self.command([*DRAINHOUND, 'vote', c], done=None))
            votes.append(button)
        self.vote_note = wrapped(max_width_chars=36)
        self.vote_note.add_css_class('cap')
        votes.append(self.vote_note)
        trio.append(votes)
        page.append(trio)

        # The forest plot: columns line up through size groups.
        self.forest_lead = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.forest_value = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        head = Gtk.Box(spacing=8)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.forest_lead.add_widget(titles)
        lab = Gtk.Label(label='WHAT EACH SETTING SAVES', xalign=0)
        lab.add_css_class('lab')
        titles.append(lab)
        caption = Gtk.Label(label='Minutes per full charge, 90% intervals.', xalign=0)
        caption.add_css_class('cap')
        titles.append(caption)
        head.append(titles)
        head.append(trail.ForestAxis())
        gain = Gtk.Label(label='Gain', xalign=0, valign=Gtk.Align.END)
        gain.add_css_class('cap')
        self.forest_value.add_widget(gain)
        head.append(gain)
        evidence = Gtk.Label(label='Evidence', xalign=0, hexpand=True, valign=Gtk.Align.END)
        evidence.add_css_class('cap')
        head.append(evidence)
        include = Gtk.Label(label='In', valign=Gtk.Align.END, width_chars=8, xalign=0)
        include.add_css_class('cap')
        self.hint(include, 'Include in automatic runs')
        head.append(include)
        page.append(head)
        self.lever_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.append(self.lever_list)

        lab = Gtk.Label(label='ON THE LOCK SCREEN AND AT BOOT', xalign=0, margin_top=6)
        lab.add_css_class('lab')
        page.append(lab)
        length = Gtk.Box(spacing=8)
        length.append(Gtk.Label(label='Freezing experiments run for'))
        hours, group = Gtk.Box(), None
        hours.add_css_class('linked')
        self.minutes_hours = 4
        for h in BATTERY_HOURS:
            button = Gtk.ToggleButton(label=f'{h} h', active=h == self.minutes_hours)
            button.set_group(group)
            group = group or button
            button.connect('toggled', lambda b, h=h: b.get_active() and setattr(self, 'minutes_hours', h))
            hours.append(button)
        length.append(hours)
        length.append(wrapped(label='of battery time (they stop automatic experiments meanwhile).', max_width_chars=40,
                              hexpand=True))
        page.append(length)
        cards = Gtk.Box(spacing=12, homogeneous=True, valign=Gtk.Align.START)
        self.freeze_cards = [self.freeze_row(), self.freeze_all_row()]
        self.psr_card = self.psr_row()
        for card in (*self.freeze_cards, self.psr_card):
            cards.append(card)
        page.append(cards)
        # The long explanations: behind a disclosure.
        about = Gtk.Expander(label='About these numbers')
        notes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.experiments_note, self.health = wrapped(max_width_chars=110), wrapped(max_width_chars=110)
        self.accuracy, self.together = wrapped(max_width_chars=110), wrapped(max_width_chars=110)
        self.worth_note = wrapped(max_width_chars=110)
        for label in (self.experiments_note, self.accuracy, self.worth_note, self.together, self.health):
            label.add_css_class('cap')
            notes.append(label)
        about.set_child(notes)
        page.append(about)
        scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=600, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(page)
        return scroller

    @staticmethod
    def card(title):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class('card')
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class('strong')
        box.append(label)
        return box

    @staticmethod
    def section(title):
        label = Gtk.Label(label=title, xalign=0, margin_top=6)
        label.add_css_class('heading')
        return label

    def key(self, _controller, keyval, *_):
        if keyval == Gdk.KEY_Escape:
            return self.close()
        if keyval == Gdk.KEY_Left:
            self.shift(-1)
        elif keyval == Gdk.KEY_Right:
            self.shift(1)
        return False

    def toggle(self, *_):
        if self.window.get_visible():
            self.close()
            return
        self.focused_once = False
        self.window.present()
        self.poll = self.poll or GLib.timeout_add_seconds(2, self.tick)
        try:
            self.refresh_status()
            self.reload()
        except Exception:  # noqa: BLE001 - the panel stays usable; the tick retries
            traceback.print_exc()

    def show_page(self, _action, page):
        name = {'sleep': 'timeline', 'whatif': 'experiments', 'savings': 'experiments'}.get(page.get_string(),
                                                                                           page.get_string())
        if self.stack.get_child_by_name(name):
            self.stack.set_visible_child_name(name)
        if not self.window.get_visible():
            self.toggle()

    def hint(self, widget, text):
        """Show `text` (or what `text()` returns) at once on hover. The handlers
        reach the widget through their controller: a closure holding the widget
        is a cycle PyGObject can't collect, and leaked every rebuilt row."""
        motion = Gtk.EventControllerMotion()
        motion.connect('enter', lambda ctl, *_: self.show_hint(ctl.get_widget(), text() if callable(text) else text))
        motion.connect('leave', lambda *_: self.hint_popover.popdown())
        widget.add_controller(motion)

    def show_hint(self, widget, text):
        found, bounds = widget.compute_bounds(self.hint_anchor)
        if not found or not text:
            return
        rect = Gdk.Rectangle()
        rect.x, rect.y = int(bounds.get_x()), int(bounds.get_y())
        rect.width, rect.height = int(bounds.get_width()), int(bounds.get_height())
        self.hint_label.set_text(text)
        self.hint_popover.set_pointing_to(rect)
        self.hint_popover.popup()

    def close(self, *_):
        self.hint_popover.popdown()
        self.window.set_visible(False)
        with self.wake:
            self.request = None
        self.generation += 1  # Cancels a load in flight.
        self.busy = False
        self.history = History()  # Free the 30 days of rows; the next open reloads them.
        if self.poll:
            GLib.source_remove(self.poll)
            self.poll = 0
        return True

    def focus_changed(self, window, *_):
        if not window.get_visible():
            return
        if window.is_active():
            self.focused_once = True
        elif self.focused_once:
            # A dropdown's popover briefly takes focus within the panel.
            GLib.timeout_add(150, lambda: (window.get_visible() and not window.is_active() and self.close()) and False)

    def tick(self):
        """Every 2 s while open: the status line; each minute (at Now) the period."""
        if not self.window.get_visible():
            self.poll = 0
            return False
        try:
            self.refresh_status()
            if not self.busy and self.loaded and time.time() - self.loaded > 60 and self.offset == 0:
                self.reload()
        except Exception:  # noqa: BLE001 - raising here would stop the timer for good
            traceback.print_exc()
        return True

    # --- data -------------------------------------------------------------------------
    def span(self):
        length = RANGES[self.range_name]
        if self.range_name == '6 h':
            end = time.time() + self.offset * length
            return end - length, end
        today = time.localtime()
        midnight = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
        if self.range_name == 'Day':
            start = midnight + self.offset * 86400
            return start, start + 86400
        end = midnight + 86400 + self.offset * length
        return end - length, end

    def set_range(self, name):
        self.range_name, self.offset = name, 0
        self.reload()

    def shift(self, delta):
        offset = 0 if delta is None else min(0, self.offset + delta)
        if offset != self.offset:  # → at Now, or Now there, changes nothing.
            self.offset = offset
            self.reload()

    def reload(self):
        self.generation += 1
        start, end = self.shown = self.span()
        fmt = '%a %b %-d, %H:%M' if self.range_name == '6 h' else '%a %b %-d'
        label = time.strftime(fmt, time.localtime(start))
        if self.range_name == 'Week':
            label += ' – ' + time.strftime('%a %b %-d', time.localtime(end - 1))
        self.period.set_text(label)
        self.forward.set_sensitive(self.offset < 0)
        for chart in (self.hero, self.pull):
            chart.loading = True
            chart.queue_draw()
        self.spinner.start()
        self.busy = True
        with self.wake:
            now = time.monotonic()
            # A reload right after another (a held arrow key) waits for the last of them.
            self.request = (self.generation, start, end, now + DEBOUNCE if now - self.requested < DEBOUNCE else now)
            self.requested = now
            self.wake.notify()
        if self.worker is None:
            self.worker = threading.Thread(target=self.work, name='drainhound panel-load', daemon=True)
            self.worker.start()

    def work(self):
        """The one loader thread: the latest request, once its debounce is over.
        A newer request cancels the running load between two of its parts."""
        while True:
            with self.wake:
                while self.request is None or self.request[3] > time.monotonic():
                    self.wake.wait(None if self.request is None else self.request[3] - time.monotonic())
                generation, start, end, _ = self.request
                self.request = None
            self.load(generation, start, end, self.history)

    def load(self, generation, start, end, history):
        """The period's charts as soon as they are ready, then the rest."""
        try:
            result = gather(start, end, history, cancelled=lambda: generation != self.generation,
                            on_period=lambda partial: GLib.idle_add(self.apply, generation, partial))
        except BaseException as error:  # noqa: BLE001 - last resort; a SystemExit must not end the one loader
            traceback.print_exc()
            result = {'error': f'{type(error).__name__}: {error}'}
        if result is not None:  # None: a newer reload took over.
            GLib.idle_add(self.apply, generation, result)

    def apply(self, generation, result):
        """A load's result, or (`pending`) its period part while the rest is computed."""
        if generation != self.generation:
            return False
        try:
            self.show(result)
        except Exception as error:  # noqa: BLE001 - an idle callback; say so instead of a stuck spinner
            traceback.print_exc()
            self.spinner.stop()
            self.busy = False
            self.show_problems({'the panel': f'{type(error).__name__}: {error}'})
        return False

    def show(self, result):
        pending = result.get('pending', False)
        if not pending:
            self.spinner.stop()
            self.loaded, self.busy = time.time(), False
        self.show_problems(result.get('errors') or {})
        at_now = self.offset == 0
        for chart in (self.hero, self.pull):
            chart.loading = False
        if 'error' in result:
            # Nothing of the previous period stays under the new one's label.
            self.period_text = result['error']
            self.hero.set_data([], self.shown, [], self.range_name, at_now)
            self.pull.set_data([], self.shown, at_now)
            self.fill_legend([], at_now)
            self.refresh_live()
            return
        points = result['points']
        self.hero.set_data(points, result['span'], result['sleeps'], self.range_name, at_now)
        self.pull.set_data(points, result['span'], at_now)
        self.fill_legend(points, at_now)
        battery = [p for p in points if p['st'] == 'D' and p['bat']]
        where = {'6 h': 'these 6 h', 'Day': 'this day', 'Week': 'this week'}[self.range_name]
        if battery:
            mean = sum(p['bat'] for p in battery) / len(battery)
            self.period_text = f'{mean:.1f} W average over the {duration(len(battery) / 60)} on battery in {where}'
        else:
            self.period_text = f'No battery time in {where}'
        dots, median = week_sleeps(result['sleeps_all'])
        self.sleep_dots.set_data(dots, median)
        self.sleep_note.set_text(f"This week's suspends, from {len(result['sleeps_all'])} logged in all."
                                 if result['sleeps_all'] else 'No suspends logged yet.')
        self.refresh_live()
        if pending:
            return  # Ways to go further comes with the rest.
        self.effects = result['effects']
        self.catalog, self.units = result['catalog'], result['units']
        self.psr, self.psr_arm = result.get('psr'), result.get('psr_arm')
        self.result = result
        self.fill_ways()
        typical = result.get('typical')
        self.about.set_text('\n\n'.join(filter(None, (
            typical_text(result) + ('The futures ahead are drainhound eta\'s: the models it forecasts with, '
                                    'sampled 40 times, each landing on a quantile of the forecast.' if typical else ''),
            what_if_note(result), health_text(result.get('health'))))))
        self.fill_experiments()
        self.refresh_status()  # The banner shows the quarantine, now known.

    def show_problems(self, errors):
        if errors:
            parts = '; '.join(f'{part} ({error})' for part, error in errors.items())
            self.problems.set_text(f"Couldn't compute {parts}. Everything else is shown; details: "
                                   'journalctl --user -u drainhound panel, or drainhound check.')
        self.problems.set_visible(bool(errors))

    def fill_legend(self, points, at_now):
        """At Now the layers carry their own labels; looking back, a legend with each layer's average
        on battery and its share."""
        while (child := self.legend.get_first_child()) is not None:
            self.legend.remove(child)
        self.legend.set_visible(not at_now)
        if at_now:
            return
        battery = [p for p in points if p['st'] == 'D' and p['groups']]
        means = {name: sum(p['groups'].get(name, 0) for p in battery) / len(battery) for name in STACK} if battery else {}
        total = sum(means.values())
        for name in STACK:
            if means.get(name, 0) < .05:
                continue
            box = Gtk.Box(spacing=5)
            swatch = Gtk.DrawingArea(content_width=10, content_height=10, valign=Gtk.Align.CENTER)
            swatch.set_draw_func(lambda _a, cr, w, h, c=COLORS[name]: (cr.set_source_rgba(*rgb(c)),
                                                                     rounded(cr, 0, 0, w, h, 2), cr.fill()))
            box.append(swatch)
            label = Gtk.Label(label=f'{name} {means[name]:.1f} W · {means[name] / total * 100:.0f}%')
            label.add_css_class('cap')
            box.append(label)
            self.legend.append(box)

    def fill_ways(self):
        """Ways to go further (panel_data.ways): each row hovers its change onto the hero as a ghost of
        the futures; tested settings that would change something carry Apply."""
        result = self.result
        data = ways(result.get('estimates'), result.get('levers'), self.settings(), result.get('quarantine'))
        energy, typical = result.get('energy'), result.get('typical')
        self.full_minutes = 60 * energy / typical if finite(energy, typical) and typical > 0 else None
        rows = data['rows']
        top = max([r['high'] if finite(r.get('high')) else r['minutes'] for r in rows] + [20])
        scale = (0, max(20, -(-top // 20) * 20))
        self.ways_axis.set_scale(scale)
        while (child := self.ways_list.get_first_child()) is not None:
            self.ways_list.remove(child)
        for row in rows:
            self.ways_list.append(self.way_row(row, scale))
        more = data['more']
        self.ways_more.set_text(f"▸ {len(more)} more estimates under +3 min: {', '.join(more)}." if more else '')
        self.ways_more.set_visible(bool(more))
        already = ''
        if data['already']:
            names = ' · '.join(label for label, _ in data['already'])
            together = sum(m for _, m in data['already'])
            already = f'<b>ALREADY SET</b>  {GLib.markup_escape_text(names)}: about +{together:.0f} min together ' \
                      '(the sum of their separate gains).'
        if data['unclear']:
            already += (' ' if already else '') + GLib.markup_escape_text(
                f"{', '.join(data['unclear'])}: set, with no clear effect yet.")
        self.ways_already.set_markup(already)
        self.ways_already.set_visible(bool(already))
        if not rows:
            empty = Gtk.Label(label=what_if_note(result) if not result.get('estimates') else
                              'Nothing measured would give back more than 3 minutes.', xalign=0, wrap=True,
                              max_width_chars=70)
            empty.add_css_class('cap')
            self.ways_list.append(empty)

    def way_row(self, row, scale):
        box = Gtk.Box(spacing=6)
        box.add_css_class('way')
        lead = Gtk.Box(spacing=6)
        self.ways_lead.add_widget(lead)
        label = Gtk.Label(label=row['label'], xalign=0, hexpand=True, width_chars=18, max_width_chars=22,
                          ellipsize=Pango.EllipsizeMode.END)
        lead.append(label)
        tested = row['kind'] == 'tested'
        tag = Gtk.Label(label='tested' if tested else 'estimate', valign=Gtk.Align.CENTER, width_chars=8)
        tag.add_css_class('tag')
        if tested:
            tag.add_css_class('tested')
        self.hint(tag, 'Measured in randomized experiments on this laptop' if tested else
                  'The battery model with that activity removed: an estimate, not a measurement')
        lead.append(tag)
        box.append(lead)
        cone = trail.Cone()
        cone.set_data(row, scale, tested)
        box.append(cone)
        minutes, interval = gain_parts(row)
        tail = Gtk.Box(spacing=6)
        self.ways_tail.add_widget(tail)
        value = Gtk.Label(xalign=0, hexpand=True, width_chars=14)
        value.set_markup(f'<b>{GLib.markup_escape_text(minutes)}</b> '
                         f"<span alpha='70%' size='small'>{GLib.markup_escape_text(interval)}</span>")
        tail.append(value)
        if tested:
            apply = Gtk.Button(label='Apply', valign=Gtk.Align.CENTER)
            apply.add_css_class('go')
            self.hint(apply, 'Until reboot, on battery: on AC it goes back to stock and returns on battery')
            assignments = list(row['apply'])
            apply.connect('clicked', lambda _b: self.command(
                [*DRAINHOUND, 'experiment', 'apply', *assignments],
                done=lambda out, err: apply_note(assignments, out, err)))
            tail.append(apply)
        else:
            tail.append(Gtk.Box(width_request=54))
        box.append(tail)
        gain = f"{minutes} per full charge{f' ({interval})' if interval else ''}"
        motion = Gtk.EventControllerMotion()
        motion.connect('enter', lambda *_: self.preview(row, gain))
        motion.connect('leave', lambda *_: self.hero.set_ghost(None))
        box.add_controller(motion)
        return box

    def preview(self, row, gain):
        """Hovering a way to go further: its change on the trail, as a ghost of the futures to the cutoff."""
        empty = (self.futures or {}).get('empty')
        if self.offset != 0 or not empty or not self.full_minutes:
            return
        scenario = ghost(empty, row['minutes'], row.get('low'), row.get('high'), self.full_minutes,
                         seed=sum(map(ord, row['label'])))
        self.hero.set_ghost({'scenario': scenario, 'label': row['label'], 'gain': gain})

    # --- experiments ------------------------------------------------------------------------
    def experiment_state(self):
        try:
            state = json.loads((RUNTIME / 'experiment.json').read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def settings(self):
        return [c for c in self.catalog if c['name'] in report.SETTING]

    def manual_active(self):
        state = self.experiment_state()
        return state.get('mode') != 'auto' and state.get('status') in ('running', 'paused')

    def fill_experiments(self):
        """The Experiments tab from the latest result, in place: the minute's
        reload must not reset a choice being made (the app picker, the boxes)."""
        result = self.result
        levers, plan = result.get('levers') or no_levers(), result.get('plan')
        names = {c['name']: c['label'] for c in self.catalog}
        risky = [names.get(n, n) for n in result.get('risky') or () if n in names]
        self.experiments_note.set_text(
            typical_text(result) + 'While on, every 4-minute block on battery sets the included settings to the '
            'combination the battery model expects to learn most from — among those unlikely to bother you, or any '
            'while the screen is locked — and learns from your 👎 which settings to avoid and from your 👍 which are '
            'worth keeping (the Comfort goal). Gains are minutes per full charge (90% intervals), each for that '
            'setting alone with the others at your settings.'
            + (f" {', '.join(risky)}: risky for some hardware, never in automatic runs." if risky else ''))
        self.health.set_text(health_text(result.get('health')))
        self.fill_levers(lever_rows(self.settings(), levers, result.get('quality'), set(result.get('pool') or []),
                                    result.get('risky') or (), result.get('quarantine') or {}))
        if plan and plan.get('accuracy') is not None:
            least = ', '.join(PHRASES.get(n, (n, n))[0] for n, on in (plan.get('least_certain') or {}).items()
                              if on) or 'all off'
            self.accuracy.set_text(f"Battery model: for the {plan['allowed']} of {plan['combinations']} combinations "
                                   f"allowed now, predictions are within ±{plan['accuracy']:.1f} W on average (90%); "
                                   f"least certain: {least}. That is where the next blocks look.")
        else:
            self.accuracy.set_text('Include settings to experiment with.')
        value = (plan or {}).get('value')
        text, _rows = value_text(value, result.get('typical'), result.get('retest'))
        verdict, _, why = text.partition('. ')  # The verdict on the card; how it is priced under About.
        self.worth.set_text(f'{verdict}.' if why else text or 'Priced once automatic experiments have run.')
        self.worth_why = why
        self.worth_note.set_text(self.worth_why)
        self.worth_note.set_visible(bool(self.worth_why))
        self.worth_chart.set_data([r for r in (value or {}).get('rows') or []
                                   if finite(r.get('hours'), r.get('saves'), r.get('costs'))])
        interactions = levers.get('interactions') or []
        found = [i for i in interactions if interaction_clear(i)]
        if found:
            self.together.set_text('Together: ' + '; '.join(
                f"{PHRASES.get(a, (a,))[0]} with {PHRASES.get(b, (b,))[0]} saves {i['watts']:+.2f} W "
                f"({i['watts_low']:+.2f} … {i['watts_high']:+.2f}) more than the two apart"
                + (f" ({'p < 0.001' if i['p'] < 0.001 else f'p = {i['p']:.2g}'})" if isinstance(i.get('p'), float)
                   else '')
                for (a, b), i in ((i['pair'], i) for i in found)) + '.')
        elif interactions:
            self.together.set_text(f'{len(interactions)} pair(s) measured together; no interaction is clear yet '
                                   '(each tested against how many would look clear by chance).')
        else:
            self.together.set_text('')
        self.together.set_visible(bool(self.together.get_text()))
        self.fill_best()
        active = self.manual_active()
        self.fill_units()
        for card in self.freeze_cards:
            card.set_visible(bool(self.units))
        self.freeze_allowed = not active and bool(self.units)
        self.freeze_sensitivity()
        freezes = {e['experiment']: e for e in self.effects}
        self.fill_freeze_all(freezes.get('freeze-apps'), active)
        self.psr_card.set_visible('psr' in (result.get('boot_arms') or {}))
        self.fill_psr()

    def fill_levers(self, rows):
        """The forest plot, by how automatic runs treat each setting, best first within each group;
        rebuilt only when the settings or their groups change (a rebuild closes a hint and drops
        focus), else updated in place."""
        groups = (('auto', 'IN AUTOMATIC RUNS', 'The blocks vary these.'),
                  ('risky', 'KEPT OUT: RISKY FOR SOME HARDWARE', 'Measured in runs you start. Add one with --risky.'),
                  ('other', 'NOT IN THE POOL', 'Tick to let automatic runs vary it.'),
                  ('quarantined', 'QUARANTINED', 'After a hardware incident: automatic experiments leave these alone.'),
                  ('unavailable', 'UNAVAILABLE', ''))
        gain = lambda row: -(row['effect'] or {}).get('minutes', -1e9) if finite((row['effect'] or {}).get('minutes')) else 1e9
        ordered = [(group, row) for group, *_ in groups for row in sorted(rows, key=gain) if row['group'] == group]
        names = [(group, row['name']) for group, row in ordered]
        if names != self.lever_names:
            self.hint_popover.popdown()  # Its widget may be about to go.
            while (child := self.lever_list.get_first_child()) is not None:
                self.lever_list.remove(child)
            self.lever_widgets = {}
            shown = {group for group, _ in names}
            for group, title, note in groups:
                if group not in shown:
                    continue
                head = Gtk.Box(spacing=10, margin_top=8)
                head.add_css_class('forest-group')
                lab = Gtk.Label(label=title, xalign=0)
                lab.add_css_class('lab')
                head.append(lab)
                caption = Gtk.Label(label=note, xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
                caption.add_css_class('cap')
                head.append(caption)
                self.lever_list.append(head)
                for g, row in ordered:
                    if g == group:
                        box, self.lever_widgets[row['name']] = self.lever_row(row['name'], row['label'], group)
                        self.lever_list.append(box)
            self.lever_names = names
        self.syncing = True
        try:
            for row in rows:
                widgets = self.lever_widgets[row['name']]
                widgets['sub'].set_text(f"Now {row['now']} · or {row['other']}" if row['now'] else row['detail'])
                widgets['forest'].set_data(row['effect'])
                minutes, interval = gain_parts(row['effect'])
                widgets['value'].set_markup(
                    f'<b>{GLib.markup_escape_text(minutes)}</b> '
                    f"<span alpha='70%' size='small'>({GLib.markup_escape_text(interval)})</span>" if minutes
                    else GLib.markup_escape_text(row['value']))
                widgets['verdict'].set_text(row['verdict'])
                blocks = (row['effect'] or {}).get('blocks') or {}
                widgets['evidence'].set_text(
                    f"{blocks.get('on', 0)} + {blocks.get('off', 0)} blocks · {row['effect'].get('hours', 0):.0f} h"
                    if row['effect'] else row['evidence'])
                widgets['comfort'].set_text(row['comfort'])
                widgets['check'].set_active(row['included'])
                widgets['check'].set_sensitive(row['sensitive'])
                saving, safe = row['saving'], row['group'] not in ('risky', 'quarantined', 'unavailable')
                widgets['apply'].set_label('Set' if saving else 'Apply')
                widgets['apply'].set_sensitive(safe and not saving)
                self.hints[row['name']] = (row['description'], row['include_hint'],
                                           'Already at its saving value' if saving else
                                           'Until reboot, on battery: on AC it goes back to stock and returns on '
                                           'battery' if safe else 'Not from the panel: ' + row['include_hint'])
        finally:
            self.syncing = False

    def lever_row(self, name, label, group):
        """One setting's row on the forest plot, and its widgets for fill_levers."""
        row = Gtk.Box(spacing=8)
        row.add_css_class('forest-row')
        lead = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, valign=Gtk.Align.CENTER)
        self.forest_lead.add_widget(lead)
        title = Gtk.Label(label=label, xalign=0, width_chars=24, max_width_chars=28, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class('strong')
        lead.append(title)
        sub = Gtk.Label(xalign=0, max_width_chars=30, ellipsize=Pango.EllipsizeMode.END)
        sub.add_css_class('cap')
        lead.append(sub)
        self.hint(lead, lambda: self.hints.get(name, ('', '', ''))[0])
        row.append(lead)
        forest = trail.Forest()
        row.append(forest)
        numbers = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, valign=Gtk.Align.CENTER)
        self.forest_value.add_widget(numbers)
        value = Gtk.Label(xalign=0, width_chars=16)
        numbers.append(value)
        verdict = Gtk.Label(xalign=0)
        verdict.add_css_class('cap')
        numbers.append(verdict)
        row.append(numbers)
        proof = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, valign=Gtk.Align.CENTER, hexpand=True)
        evidence = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=34)
        proof.append(evidence)
        comfort = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=40)
        comfort.add_css_class('cap')
        proof.append(comfort)
        row.append(proof)
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        self.hint(check, lambda: self.hints.get(name, ('', '', ''))[1])
        check.connect('toggled', lambda b: self.syncing or self.set_pool(name, b.get_active()))
        row.append(check)
        apply = Gtk.Button(label='Apply', valign=Gtk.Align.CENTER)
        apply.add_css_class('go')
        self.hint(apply, lambda: self.hints.get(name, ('', '', ''))[2])
        assignment = [f'{name}=saving']
        apply.connect('clicked', lambda _b: self.command([*DRAINHOUND, 'experiment', 'apply', *assignment],
                                                         done=lambda out, err: apply_note(assignment, out, err)))
        row.append(apply)
        return row, {'sub': sub, 'forest': forest, 'value': value, 'verdict': verdict, 'evidence': evidence,
                     'comfort': comfort, 'check': check, 'apply': apply}

    def set_pool(self, name, included):
        """One box, one name: `pool add|remove NAME` leaves the rest of the chosen
        pool alone (quarantined settings stay chosen for `release`, risky ones
        added with --risky stay in). The panel never passes --risky: a risky
        setting goes in only from the CLI, and its box comes out greyed.
        Emptying the pool doesn't run `disable` (which puts every setting back
        to stock, applied ones too): the runner stays on, with nothing to test."""
        if self.result is None:
            return
        pool = [n for n in (self.result.get('pool') or []) if n != name] + ([name] if included else [])
        self.result['pool'] = [c['name'] for c in self.settings() if c['name'] in pool]  # Catalog order.
        empty = None if self.result['pool'] else ('No settings included: automatic experiments have nothing to '
                                                  'test until you include one (the switch turns them off).')
        self.command([*DRAINHOUND, 'experiment', 'pool', 'add' if included else 'remove', name], done=empty)

    def switch_experiments(self, _switch, on):
        if not self.syncing:
            self.command([*DRAINHOUND, 'experiment', 'enable' if on else 'disable'])
        return False

    def set_goal(self, goal):
        if self.result is None:
            return
        self.result['goal'] = goal
        self.fill_best()
        # goal --apply applies nothing while a run owns the settings (as Apply is insensitive then);
        # paused because testing isn't worth it, the runner sets the goal's combination itself.
        state = self.experiment_state()
        owned = state.get('status') in ('running', 'paused')
        assignments = self.goal_assignments(goal)
        self.command([*DRAINHOUND, 'experiment', 'goal', goal, '--apply'],
                     done=f'Goal: {goal.capitalize()}. Paused experiments switch to its combination within a minute.'
                     if owned and state.get('settled') else
                     f'Goal: {goal.capitalize()}. Nothing applied: experiments own the settings.' if owned else
                     lambda out, err: apply_note(assignments, out, err))

    def goal_assignments(self, goal):
        """What `goal --apply` sets: recommend.json's combination, the one shown
        while it is fresh; else the CLI computes its own (None: not known here)."""
        if (self.result or {}).get('recs_source') != 'drainhound eta':
            return None
        return ((self.result.get('recs') or {}).get(goal) or {}).get('apply') or []

    def fill_best(self):
        """Each goal's best measured combination, the chosen goal's first, from
        ONE source (panel_data.recommendations): what the battery menu shows,
        what switching goals applies (unless automatic experiments own the
        settings) and what Apply applies."""
        result = self.result
        goal = result.get('goal') if result.get('goal') in report.GOALS else 'battery'
        self.syncing = True
        try:
            for name, button in self.goal_buttons.items():
                button.set_sensitive(True)
                if name == goal:
                    button.set_active(True)
        finally:
            self.syncing = False
        recs = result.get('recs') or {}
        self.best = recs.get(goal) if (recs.get(goal) or {}).get('apply') else None
        order = (goal,) + tuple(g for g in report.GOALS if g != goal)
        lines = [f"{g.capitalize()}: {(recs.get(g) or {}).get('text') or 'nothing measured yet'}" for g in order]
        if result.get('recs_source') == 'panel':
            lines.append("(Computed here: drainhound eta's recommend.json is missing or over an hour old.)")
        self.best_label.set_text('\n'.join(lines))
        self.apply_button.set_visible(self.best is not None)
        self.apply_button.set_sensitive(self.experiment_state().get('status') not in ('running', 'paused'))

    def apply_best(self):
        if self.best:
            assignments = list(self.best['apply'])
            self.command([*DRAINHOUND, 'experiment', 'apply', *assignments],
                         done=lambda out, err: apply_note(assignments, out, err))

    def freeze_row(self):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row.add_css_class('card')
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = wrapped(label='Close an app (freeze it in B blocks)', max_width_chars=30)
        title.add_css_class('strong')
        info.append(title)
        choice = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.unit_model = Gtk.StringList()
        self.unit_picker = Gtk.DropDown(model=self.unit_model)
        self.unit_picker.connect('notify::selected', lambda *_: self.freeze_sensitivity())
        choice.append(self.unit_picker)
        self.only_locked = Gtk.CheckButton(label='Only while the screen is locked', active=True)
        choice.append(self.only_locked)
        info.append(choice)
        note = wrapped(label='Measures everything the app costs, wakeups included. Frozen '
                         'apps stop responding until their B block ends.', max_width_chars=34)
        note.add_css_class('muted')
        info.append(note)
        row.append(info)
        self.freeze_start = Gtk.Button(label='Start', halign=Gtk.Align.START)
        self.freeze_start.add_css_class('go')
        self.freeze_start.connect('clicked', lambda _b: self.start_freeze())
        row.append(self.freeze_start)
        return row

    def fill_units(self):
        """The app picker (panel_data.picker), replaced only when the set of apps
        changes, keeping the chosen app by unit name (the order follows CPU
        time); if it exited, a placeholder is chosen and Start waits for a choice."""
        chosen = self.chosen_unit()
        if chosen is None and self.unit_names[:1] == [None]:
            chosen = GONE  # The placeholder is still chosen.
        update = picker(self.units, self.unit_names, chosen)
        if update is None:
            return
        self.unit_names, labels, index = update
        self.unit_model.splice(0, self.unit_model.get_n_items(), labels)
        self.unit_picker.set_selected(index)
        self.freeze_sensitivity()

    def chosen_unit(self):
        index = self.unit_picker.get_selected()
        return self.unit_names[index] if 0 <= index < len(self.unit_names) else None

    def freeze_sensitivity(self):
        self.freeze_start.set_sensitive(self.freeze_allowed and self.chosen_unit() is not None)

    def start_freeze(self):
        unit = self.chosen_unit()
        if unit is not None:
            self.start('freeze', unit=unit, only_locked=self.only_locked.get_active())

    def freeze_all_row(self):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row.add_css_class('card')
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = wrapped(label='Close every app (freeze all, while locked)', max_width_chars=30)
        title.add_css_class('strong')
        info.append(title)
        info.append(self.muted(wrapped(label='Runs only while the screen is locked. Frozen blocks measure the '
                                       'platform floor; A − B is what the apps cost in the background.',
                                       max_width_chars=34)))
        keeping = experiment.freeze_keep()  # config [experiments] freeze_keep
        self.keep_apps = Gtk.CheckButton(label=f"Keep {', '.join(keeping)} running", active=True,
                                         visible=bool(keeping))
        info.append(self.keep_apps)
        self.freeze_all_result = wrapped(visible=False, max_width_chars=34)
        self.freeze_all_result.add_css_class('result')
        info.append(self.freeze_all_result)
        row.append(info)
        buttons = Gtk.Box(spacing=6)
        keep = lambda: ([a for k in keeping for a in ('--keep', k)] if self.keep_apps.get_active()
                        else ['--keep', '//'])  # '//' matches nothing.
        self.freeze_all_start = Gtk.Button(label='Start')
        self.freeze_all_start.add_css_class('go')
        self.freeze_all_start.connect('clicked', lambda _b: self.start('freeze-apps', extra=keep()))
        buttons.append(self.freeze_all_start)
        self.freeze_test = Gtk.Button(label='Test now (5 s)')
        self.hint(self.freeze_test, 'Freeze the same apps for 5 seconds right now, measure, and thaw')
        self.freeze_test.connect('clicked', lambda _b: self.command(
            [*DRAINHOUND, 'experiment', 'test-freeze', '--seconds', '5'] + keep(),
            done=lambda out, _err: (self.freeze_test_result(out), None), running='Freezing apps for 5 seconds…'))
        buttons.append(self.freeze_test)
        row.append(buttons)
        return row

    def fill_freeze_all(self, effect, active):
        text_ = ''
        if effect and effect.get('b_watts') is not None:
            text_ = f"Floor with apps frozen: {effect['b_watts']:.1f} W · apps cost {effect['watts']:+.2f} W"
            if 'minutes' in effect:
                text_ += f" ({effect['watts_low']:+.2f} to {effect['watts_high']:+.2f}) · {effect['pairs']} pairs"
        self.freeze_all_result.set_text(text_)
        self.freeze_all_result.set_visible(bool(text_))
        self.freeze_all_start.set_sensitive(not active)
        self.freeze_test.set_sensitive(not active)

    def freeze_test_result(self, output):
        try:
            data = json.loads(output)
        except ValueError:
            return f'Freeze test: unreadable output {output[:200]!r}'
        units = data['units']
        frozen = [u for u in units if u['state'] == 'frozen']
        leaky = [f"{u['label']} {u['cpu_ms_while_frozen']} ms" for u in units if u['cpu_ms_while_frozen'] > 20]
        text = (f"Freeze test: {len(frozen)}/{len(units)} app scopes frozen for {data['seconds']:g} s"
                + (f"; still used CPU: {', '.join(leaky)}" if leaky else '; none used CPU while frozen') + '.')
        missed = [f"{', '.join(g['top'])} ({g['unit']}, {g['cpu_s']} CPU-s)" for g in data['uncovered'][:3]]
        if missed:
            text += ' Not freezable (outside app scopes): ' + '; '.join(missed) + '.'
        return text

    def psr_row(self):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row.add_css_class('card')
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = wrapped(label='Panel self-refresh (PSR) — boot-level', max_width_chars=30)
        title.add_css_class('strong')
        info.append(title)
        self.psr_note = self.muted(wrapped(max_width_chars=34))
        info.append(self.psr_note)
        self.psr_result = wrapped(visible=False, max_width_chars=34)
        self.psr_result.add_css_class('result')
        info.append(self.psr_result)
        row.append(info)
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.psr_buttons = {}
        for arm, label in (('psr', 'Next boot: PSR on'), ('default', 'Next boot: default')):
            button = Gtk.Button(label=label)
            button.connect('clicked', lambda _b, a=arm: self.command(
                [*DRAINHOUND, 'experiment', 'next-boot', a],
                done=f"Next boot set to {'PSR on' if a == 'psr' else 'the default entry'} — restart when convenient."))
            buttons.append(button)
            self.psr_buttons[arm] = button
        row.append(buttons)
        return row

    def fill_psr(self):
        psr = self.psr or {}
        boots, hours = psr.get('boots', {'on': 0, 'off': 0}), psr.get('hours', {'on': 0, 'off': 0})
        current = {'psr': 'on', 'default': 'off'}.get(self.psr_arm, 'unknown')
        self.psr_note.set_text(
            f"This boot: PSR {current}. Logged on battery: PSR on {boots['on']} boots / {hours['on']:.1f} h, "
            f"off {boots['off']} boots / {hours['off']:.1f} h. Needs {report.MIN_BOOTS}+ boots each (with "
            f"{report.BOOT_MINUTES}+ minutes on battery). PSR hangs some panels (which is why it is often "
            "disabled); if the desktop stalls, reboot to the default entry.")
        result = ''
        if 'watts' in psr:
            result = (f"Result: PSR on saves {psr['watts']:+.2f} W ({psr['watts_low']:+.2f} to {psr['watts_high']:+.2f})"
                      + (f" → {psr['minutes']:+.0f} min per charge" if 'minutes' in psr else ''))
        self.psr_result.set_text(result)
        self.psr_result.set_visible(bool(result))
        # Suggest the arm with less battery time so far.
        suggested = 'psr' if hours['on'] <= hours['off'] else 'default'
        for arm, button in self.psr_buttons.items():
            (button.add_css_class if arm == suggested else button.remove_css_class)('suggested-action')

    @staticmethod
    def muted(label):
        label.add_css_class('muted')
        return label

    def start(self, names, unit=None, only_locked=False, extra=()):
        minutes = 60 * self.minutes_hours
        names = [names] if isinstance(names, str) else list(names)
        command = [*DRAINHOUND, 'experiment', 'start', *names, '--minutes', str(minutes)]
        if unit:
            command += ['--unit', unit]
        if only_locked:
            command.append('--only-locked')
        self.command(command + list(extra))

    def command(self, argv, done=None, running=''):
        """Run a CLI. Its outcome goes under the banner (panel_data.command_note):
        `done` is a note, or a function of (stdout, stderr) returning (note,
        seconds); a failure, or a refusal on stderr, stays NOTE_SECONDS.
        `running` shows meanwhile."""
        self.set_note(running)  # The last command's outcome no longer applies.
        try:
            process = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as error:  # Not on PATH, say.
            self.set_note(*command_note(False, '', error.message))
            return

        def finished(proc, res):
            try:
                _, out, err = proc.communicate_utf8_finish(res)
                ok = proc.get_successful()
            except GLib.Error as error:
                out, err, ok = '', error.message, False
            try:
                self.set_note(*command_note(ok, out or '', err or '', done))
            except Exception as error:  # noqa: BLE001 - e.g. output in an unexpected shape
                traceback.print_exc()
                self.set_note(f'{argv[0]} finished, but its output could not be read: {error}', NOTE_SECONDS)
            if ok:  # Settings changed: re-read them (and the rows) shortly.
                GLib.timeout_add(800, lambda: (self.refresh_status(), self.window.get_visible() and self.reload())
                                 and False)
        process.communicate_utf8_async(None, None, finished)

    def set_note(self, text, seconds=None):
        """Under the banner until the next command, or for `seconds`."""
        self.note, self.note_until = text or '', time.monotonic() + seconds if seconds else None
        self.timeline_note.set_text(self.note)
        self.timeline_note.set_visible(bool(self.note))
        self.refresh_status()

    def refresh_status(self):
        """The live parts and the experiments banner, from the files the services
        write; neither may raise (the 2 s timer calls this)."""
        try:
            self.refresh_live()
        except Exception:  # noqa: BLE001 - one bad file must not stop the rest
            traceback.print_exc()
        try:
            self.refresh_experiments()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def refresh_live(self):
        """What drainhound eta (eta.json, futures.json) and the collector (snapshot.json) say now: the
        header's state, the answers, the futures ahead, the labels on what's pulling, the busiest apps."""
        now = time.time()
        estimate, battery = read_estimate(RUNTIME / 'eta.json', now), read_battery()
        self.state.set_text(state_text(estimate, battery))
        items = answers(estimate, now)
        for i, (slot, label, value, caption) in enumerate(self.answer_slots):
            slot.set_visible(i < len(items))
            if i < len(items):
                item = items[i]
                label.set_text(item['label'].upper())
                value.set_text(item['value'])
                value.set_css_classes(['big' if item['big'] else 'num'])
                caption.set_markup(item['caption'])
        self.futures = read_futures(RUNTIME / 'futures.json', now)
        level = (estimate or {}).get('pct') if finite((estimate or {}).get('pct')) else (battery or {}).get('pct')
        self.hero.set_live(self.futures if self.offset == 0 else None, level, now)
        snapshot = read_snapshot(RUNTIME / 'snapshot.json', now)
        labels = pull_labels(snapshot)
        self.pull.set_labels(labels if self.offset == 0 else None)
        self.pull.set_now(now)
        live = labels and self.offset == 0 and finite(labels.get('total'))
        self.pull_now.set_text(f"{labels['total']:.1f} W" if live else '')
        self.pull_now.set_visible(bool(live))
        typical = (self.result or {}).get('typical')
        self.pull_caption.set_text(' · '.join(filter(None, (
            f"now, {labels['window']}" if live and labels.get('window') else '', getattr(self, 'period_text', ''),
            f'{typical:.1f} W typical' if finite(typical) else ''))))
        self.procs.set_data(processes(snapshot))
        asleep = (estimate or {}).get('sleep') or {}
        self.sleep_big.set_text(f"~{long_duration(asleep['hours'])} from now" if finite(asleep.get('hours')) else
                                'Asleep from now: waiting for drainhound eta')
        self.sleep_caption.set_text(' · '.join(filter(None, (
            f"80%: {round(asleep['low'])}–{round(asleep['high'])} h" if finite(asleep.get('low'), asleep.get('high'))
            else '', f"{asleep['watts']:.2f} W" if finite(asleep.get('watts')) else ''))))
        footer = ((snapshot or {}).get('battery') or {}).get('footer')
        self.power_log.set_text(footer if isinstance(footer, str) else '')

    def refresh_experiments(self):
        state = self.experiment_state()
        status, auto = state.get('status'), state.get('mode') == 'auto'
        enabled = experiment.ENABLED.exists()
        self.syncing = True  # Reflect the files without the widgets acting on it.
        try:
            self.auto_switch.set_active(enabled)
        finally:
            self.syncing = False
        self.stop_button.set_visible(not auto and status in ('running', 'paused'))
        recent, rest = qol.votes(), qol.resting()
        last = (f"Last vote: {'👍' if recent[-1][1] == 'up' else '👎'} at "
                f"{time.strftime('%H:%M', time.localtime(recent[-1][0]))}. " if recent else '')
        self.vote_note.set_text(last + (f'Experiments rest {rest / 60:.0f} more min.' if rest else
                                        '👍 for anything unexpectedly nice, 👎 when something is off (it restores '
                                        'your settings at once); also next to the battery.'))
        self.resume_button.set_visible(rest > 0)
        frozen = ''
        if state.get('units') and status in ('running', 'paused'):
            states = [experiment.freeze_get(u) for u in experiment.live_units(state['units'])]
            frozen = f" Apps frozen right now: {states.count('frozen')}/{len(states)}."
        if self.note_until is not None and time.monotonic() > self.note_until:
            self.note, self.note_until = '', None
        self.banner.set_text(banner_text(state, enabled, frozen=frozen, quarantine=(self.result or {}).get('quarantine'))
                             + (f'\n{self.note}' if self.note else ''))


# The panel's palette: Trail's night ground (docs/design/1-trail.html; its charts are
# drawn on trail.GROUND and trail.SURFACE). A menu-theme.css beside this file,
# defining the same @menu_* colours, overrides it, to match other panels of a desktop theme.
PALETTE = b"""
@define-color menu_bg #10171a;
@define-color menu_card #16211f;
@define-color menu_hover #1d2b28;
@define-color menu_active #253a2b;
@define-color menu_active_hover #2c4429;
@define-color menu_accent #8cc084;
@define-color menu_fg #ece4d4;
@define-color menu_muted #a3b2a9;
@define-color menu_border rgba(236, 228, 212, 0.07);
@define-color menu_border_strong rgba(236, 228, 212, 0.13);
"""
# Font Awesome's thumbs (qol.GLYPHS) need its font; plain emoji need none.
THUMB_FONT = b"""
#battery-card .thumb { font-family: "Font Awesome 7 Free"; font-weight: 900; }
"""
CSS = b"""
window { background: transparent; }
#battery-card { background: @menu_bg; color: @menu_fg; border: 1px solid @menu_border;
                border-left-color: @menu_border_strong; border-radius: 14px;
                padding: 14px 20px 12px; font-family: sans-serif; font-size: 13px; }
#battery-card .muted, #battery-card .cap { color: @menu_muted; font-size: 12px; }
#battery-card .cap b { color: @menu_fg; }
#battery-card .lab { color: @menu_muted; font-size: 10.5px; font-weight: 700; letter-spacing: 1.6px; }
#battery-card .panel-title { font-size: 17px; font-weight: 700; }
#battery-card .state { color: @menu_muted; }
#battery-card .big { font-size: 46px; font-weight: 300; }
#battery-card .num { font-size: 27px; font-weight: 300; margin: 6px 0 3px; }
#battery-card .num-small { font-size: 22px; font-weight: 300; }
#battery-card .strong { font-weight: 700; }
#battery-card .problem { color: #f0a35e; font-size: 12px; }
#battery-card .health { font-size: 11px; }
#battery-card .card { background: @menu_card; border: 1px solid @menu_border; border-radius: 12px; padding: 10px 12px; }
#battery-card .already { background: @menu_card; border-radius: 10px; padding: 7px 10px; margin-top: 6px;
                         color: @menu_muted; font-size: 12px; }
#battery-card .lower { border-top: 1px solid @menu_border_strong; padding-top: 12px; }
#battery-card .way { min-height: 24px; }
#battery-card .way:hover { background: alpha(@menu_fg, 0.04); border-radius: 6px; }
#battery-card .tag { font-size: 10px; border-radius: 999px; padding: 0 7px; border: 1px solid #44574f;
                     color: @menu_muted; }
#battery-card .tag.tested { background: #2c4429; color: #b9e3ad; border-color: #4d6b48; }
#battery-card button.go { color: @menu_accent; border: 1px solid #4d6b48; padding: 1px 8px; min-height: 0; }
#battery-card .controls { padding-bottom: 8px; border-bottom: 1px solid @menu_border_strong; }
#battery-card stackswitcher.tabs button { background: none; border: none; box-shadow: none; color: @menu_muted;
                                          font-weight: 700; border-radius: 0; padding: 3px 2px; margin-left: 18px;
                                          border-bottom: 2px solid transparent; }
#battery-card stackswitcher.tabs button:checked { color: @menu_fg; border-bottom-color: @menu_fg; }
#battery-card button.vote { padding: 2px 9px; min-height: 0; min-width: 0; }
#battery-card .banner { background: @menu_card; border-radius: 10px; padding: 8px 10px; }
#battery-card .experiment { padding: 8px 10px; }
#battery-card .result { font-weight: bold; font-size: 12px; }
#battery-card list { background: @menu_card; border-radius: 10px; }
#battery-card button:hover { background: @menu_hover; }
#battery-card expander title { color: @menu_muted; font-size: 12px; }
popover.hint > contents { background: @menu_card; color: @menu_fg; padding: 6px 9px; font-size: 12px; }
#battery-card button.vote-up .thumb { color: #8cc084; }
#battery-card button.vote-down .thumb { color: #e57a55; }
"""


def stylesheet(theme=Path(__file__).with_name('menu-theme.css'), glyphs=None):
    """PALETTE, then a theme's colours if there is one (a later @define-color wins), then the rules."""
    glyphs = config.get('waybar', 'glyphs') if glyphs is None else glyphs
    try:
        overrides = Path(theme).read_bytes()
    except OSError:
        overrides = b''
    return PALETTE + overrides + CSS + (THUMB_FONT if glyphs == 'fontawesome' else b'')


def main(argv):
    """`serve [TAB]` runs the resident panel (opening on TAB if given);
    `render DIR [6 h|Day|Week]` writes its charts as PNGs without a window."""
    if argv[:1] == ['render'] and len(argv) in (2, 3):
        render(argv[1], *(argv[2:3] or ['Day']))
        return 0
    if argv[:1] == ['serve'] and (argv[1:2] in ([], ['--open']) and len(argv) <= 3):
        # The layer-shell library is preloaded for this process (launch.preload);
        # the commands the panel's buttons run don't need it.
        os.environ['LD_PRELOAD'] = os.environ.pop('DRAINHOUND_PRELOADED', os.environ.get('LD_PRELOAD', ''))
        if not os.environ['LD_PRELOAD']:
            del os.environ['LD_PRELOAD']
        # Hidden until toggled (the unit's ExecStart); --open shows it at once, on TAB if given.
        app = BatteryPanel()
        if argv[1:2] == ['--open']:
            def opened(*_):
                if argv[2:]:
                    app.activate_action('show', GLib.Variant('s', argv[2]))
                else:
                    app.activate_action('toggle', None)
            app.connect('startup', lambda *_: GLib.idle_add(lambda: opened() and False))
        return app.run([])
    print('usage: drainhound panel serve [--open [TAB]] | render DIR [6 h|Day|Week]', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))

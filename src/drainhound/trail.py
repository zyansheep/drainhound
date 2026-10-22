"""The battery panel's Trail charts (docs/design/1-trail.html), drawn with cairo:
the hero, where the battery has been and the futures drainhound eta samples
ahead (futures.json), each ending in a dot where it lands; what's pulling, the
watts by part and app group with labels on the layers themselves; and the
small charts beside them. panel.py lays them out; panel_data.py shapes their data."""
import itertools
import math
import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Pango, PangoCairo

from . import report
from .panel_data import AHEAD, chart_colors, dot_stack, finite, hhmm, readout, time_axis

# The night-trail palette: three signal colours with fixed jobs (moss: gains and charging, rust: drains
# and the cutoff, sky: sleep and a previewed change), bone for the trail and text, red for "you are here".
GROUND, SURFACE, RAISED, RULE, CONTOUR = '#10171a', '#16211f', '#1d2b28', '#2a3b37', '#1c2a26'
BONE, MUTED, FAINT, DIM = '#ece4d4', '#a3b2a9', '#84968d', '#5f7069'
MOSS, RUST, SKY, HERE = '#8cc084', '#e57a55', '#7cc0e0', '#d63a3a'
COLORS = chart_colors(report.GROUPS)
STACK = list(COLORS)
FONT = 'Sans'


def rgb(color, alpha=1.0):
    color = color.lstrip('#')
    return tuple(int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)) + (alpha,)


def text(cr, string, x, y, size=9.5, color=MUTED, bold=False, align='left', halo=None, italic=False,
         spacing=0.0, width=None, markup=False):
    """Text with its top-left (or top-right, top-centre) at x, y; `halo` paints a ring of that colour
    behind it, so it reads over lines. `spacing` is letter spacing in points. Returns (w, h)."""
    layout = PangoCairo.create_layout(cr)
    font = Pango.FontDescription.from_string(f"{FONT}{' Bold' if bold else ''}{' Italic' if italic else ''} {size}")
    layout.set_font_description(font)
    if spacing:
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_letter_spacing_new(int(spacing * Pango.SCALE)))
        layout.set_attributes(attrs)
    (layout.set_markup if markup else layout.set_text)(string, -1)
    if width:
        layout.set_width(int(width * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
    w, h = layout.get_pixel_size()
    x = x - w if align == 'right' else x - w / 2 if align == 'center' else x
    cr.move_to(x, y)
    if halo:
        PangoCairo.layout_path(cr, layout)
        cr.set_source_rgba(*rgb(halo))
        cr.set_line_width(4)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke()
        cr.move_to(x, y)
    cr.set_source_rgba(*(rgb(color) if isinstance(color, str) else color))
    PangoCairo.show_layout(cr, layout)
    return w, h


def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def smooth(cr, pts, move=True):
    """A path through `pts`, smoothed with quadratic segments through the midpoints: it never overshoots,
    so two stacked boundaries smoothed this way never cross."""
    if not pts:
        return
    (cr.move_to if move else cr.line_to)(*pts[0])
    if len(pts) < 3:
        for p in pts[1:]:
            cr.line_to(*p)
        return
    cr.line_to((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2)
    for (x0, y0), (x1, y1) in itertools.pairwise(pts[1:]):
        cx, cy = cr.get_current_point()
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        # Quadratic (x0, y0) as a cubic.
        cr.curve_to(cx + 2 / 3 * (x0 - cx), cy + 2 / 3 * (y0 - cy), mx + 2 / 3 * (x0 - mx), my + 2 / 3 * (y0 - my),
                    mx, my)
    cr.line_to(*pts[-1])


def polyline(cr, pts):
    cr.move_to(*pts[0])
    for p in pts[1:]:
        cr.line_to(*p)


def here_marker(cr, x, y, label=None, avoid=(), bounds=None):
    """You are here: a red dot with a soft halo, on the line rather than over it, and a tag joined by a
    short leader: up and to the left, or the first of a few other places clear of the points in `avoid`
    (the trail behind) and inside `bounds` (x0, y0, x1, y1)."""
    cr.arc(x, y, 12, 0, 2 * math.pi)
    cr.set_source_rgba(*rgb(HERE, .16))
    cr.fill()
    cr.arc(x, y, 7.5, 0, 2 * math.pi)
    cr.set_source_rgba(*rgb(HERE, .55))
    cr.set_line_width(1)
    cr.stroke()
    cr.arc(x, y, 4, 0, 2 * math.pi)
    cr.set_source_rgba(*rgb(HERE))
    cr.fill_preserve()
    cr.set_source_rgba(*rgb(BONE))
    cr.set_line_width(1.3)
    cr.stroke()
    if not label:
        return
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(Pango.FontDescription.from_string(f'{FONT} Bold 8'))
    layout.set_text(label, -1)
    w, h = layout.get_pixel_size()
    tw = w + 16

    def clear(tx, ty):  # The tag's right end at tx, its middle at ty.
        if bounds and not (bounds[0] <= tx - tw and tx <= bounds[2] and bounds[1] <= ty - 10 and ty + 10 <= bounds[3]):
            return False
        return not any(tx - tw - 6 <= px <= tx + 6 and ty - 16 <= py <= ty + 16 for px, py in avoid)

    tx, ty = next(((x + dx, y + dy) for dx, dy in ((-12, -54), (-12, 54), (-40, -88), (-40, 88), (-90, -30),
                                                   (-90, 34), (-12, -120)) if clear(x + dx, y + dy)), (x - 12, y - 54))
    cr.move_to(x - 2.5, y + (5.5 if ty > y else -5.5))
    cr.line_to(tx - 8, ty + (-9 if ty > y else 9))
    cr.set_source_rgba(*rgb(HERE))
    cr.set_line_width(1.3)
    cr.stroke()
    rounded(cr, tx - tw, ty - 10, tw, 20, 10)
    cr.fill()
    cr.move_to(tx - w - 8, ty - h / 2)
    cr.set_source_rgba(1, 1, 1, 1)
    PangoCairo.show_layout(cr, layout)


def contours(cr, cx, cy, rings=16):
    """Faint topographic rings around a hill: the hero's texture, ahead."""
    cr.set_source_rgba(*rgb(CONTOUR))
    for k in range(1, rings + 1):
        for a in range(65):
            th = a / 64 * 2 * math.pi
            r = k * 19 * (1 + .14 * math.sin(3 * th + k * .55) + .07 * math.sin(5 * th + k * 1.7))
            (cr.move_to if a == 0 else cr.line_to)(cx + 1.8 * r * math.cos(th), cy + r * math.sin(th))
        cr.close_path()
        cr.set_line_width(1.6 if k % 5 == 0 else 1)
        cr.stroke()


def paw(cr, x, y, scale, color):
    """The hound's paw print (the panel's mark), centred at x, y."""
    cr.save()
    cr.translate(x - 10 * scale, y - 11 * scale)
    cr.scale(scale, scale)
    cr.set_source_rgba(*rgb(color))
    cr.save()
    cr.translate(10, 14.2)
    cr.scale(4.7, 3.9)
    cr.arc(0, 0, 1, 0, 2 * math.pi)
    cr.restore()
    cr.fill()
    for px, py, r in ((4.4, 8.7, 2), (7.9, 5.3, 2.1), (12.1, 5.3, 2.1), (15.6, 8.7, 2)):
        cr.arc(px, py, r, 0, 2 * math.pi)
        cr.fill()
    cr.restore()


def ticks(start, end, now):
    """The time axis: hours behind (every 4 h in a day, days in a week), half hours ahead."""
    span = (now or end) - start
    step = 3600 if span <= 8 * 3600 else 4 * 3600 if span <= 30 * 3600 else 86400
    day = time.localtime(start)
    out, k = [], 0
    while True:
        t = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1)) + k * step
        k += 1
        if t >= (now or end):
            break
        if t > start:
            out.append((t, time.strftime('%a %-d' if step == 86400 else '%H:%M', time.localtime(t)), True))
    if now:
        t = math.ceil((now + 60) / 1800) * 1800
        while t <= now + AHEAD:
            out.append((t, hhmm(t), time.localtime(t).tm_min == 0))
            t += 1800
    return out


class Chart(Gtk.DrawingArea):
    """A cairo chart that knows the pointer, and fades while its data loads."""

    def __init__(self, height):
        super().__init__(hexpand=True, content_height=height)
        self.set_draw_func(self.draw)
        self.hover, self.loading = None, False
        motion = Gtk.EventControllerMotion()
        motion.connect('motion', lambda _c, x, y: self.pointer(x, y))
        motion.connect('leave', lambda _c: self.pointer(None, None))
        self.add_controller(motion)

    def pointer(self, x, y):
        self.hover = None if x is None else (x, y)
        self.queue_draw()

    def draw(self, _area, cr, width, height):
        cr.push_group()
        self.render(cr, width, height)
        cr.pop_group_to_source()
        cr.paint_with_alpha(.45 if self.loading else 1)

    def render(self, cr, width, height):
        raise NotImplementedError


def past_levels(points, start, end, step=300):
    """The battery level behind, one point per `step` (its last reading), as [(t, pct)]."""
    out = {}
    for p in points:
        if start <= p['t'] < end and finite(p.get('pct')):
            out[p['t'] // step] = (p['t'] + 60, p['pct'])
    return [out[k] for k in sorted(out)]


def runs_of(points, start, end, test):
    """Contiguous stretches [(t0, t1)] of the minutes where test(point) holds."""
    out = []
    for p in points:
        if not (start <= p['t'] < end) or not test(p):
            continue
        if out and p['t'] - out[-1][1] <= 120:
            out[-1][1] = p['t'] + 60
        else:
            out.append([p['t'], p['t'] + 60])
    return out


class HeroChart(Chart):
    """The trail: the battery level behind, and ahead the futures drainhound eta sampled (futures.json),
    each a thin line ending in a dot where it lands; the dots stack into the distribution itself.
    Charging, the charging futures (moss) and, in rust, what unplugging now would do; on battery, the
    futures are the trail itself. Hovering ahead reads the futures at that minute; hovering a way to go
    further (set_ghost) previews it as a sky ghost of the futures."""
    PL, PR, TOP, BOTTOM = 34, 8, 52, 66

    def __init__(self):
        super().__init__(304)
        self.points, self.span, self.sleeps, self.range_name = [], (0, 1), [], '6 h'
        self.at_now, self.futures, self.ghost, self.level, self.now = False, None, None, None, None

    def set_data(self, points, span, sleeps, range_name, at_now):
        self.points, self.span, self.sleeps, self.range_name, self.at_now = points, span, sleeps, range_name, at_now
        self.queue_draw()

    def set_live(self, futures, level, now):
        """futures.json (None: none yet, or stale), the charge % now (eta.json's, else the battery's) and
        the time now, where the axis turns from behind to ahead."""
        self.futures, self.level, self.now = futures, level, now
        self.queue_draw()

    def set_ghost(self, ghost):
        """{'scenario': panel_data.ghost(...), 'label', 'gain'} or None."""
        self.ghost = ghost
        self.queue_draw()

    def axis(self, width):
        start, end = self.span
        now = (self.now or time.time()) if self.at_now else None
        return time_axis(start, end, self.PL, width - self.PR, now)

    def render(self, cr, width, height):
        X = self.axis(width)
        ph = height - self.TOP - self.BOTTOM
        Y = lambda p: self.TOP + (100 - p) / 100 * ph
        ground = height - 22
        start, now = X.start, X.now
        behind_end = now or X.end
        rounded(cr, 0, 0, width, ground, 12)
        cr.set_source_rgba(*rgb(SURFACE))
        cr.fill()
        cr.save()
        rounded(cr, 0, 0, width, ground, 12)
        cr.clip()
        xn = X(now) if now else None
        if now:
            contours(cr, X(now + 95 * 60), Y(50))
            cr.rectangle(xn, 0, width - xn, ground)
            cr.set_source_rgba(*rgb(BONE, .035))
            cr.fill()
            fade = cairo.LinearGradient(xn - 40, 0, xn, 0)
            fade.add_color_stop_rgba(0, *rgb(BONE, 0)[:3], 0)
            fade.add_color_stop_rgba(1, *rgb(BONE, .05))
            cr.rectangle(xn - 40, 0, 40, ground)
            cr.set_source(fade)
            cr.fill()
        # Plugged in (moss) and asleep (sky), behind.
        plugged = runs_of(self.points, start, behind_end, lambda p: p.get('st') not in ('D', None))
        for a, b in plugged:
            cr.rectangle(X(a), Y(100), X(b) - X(a), ph)
            cr.set_source_rgba(*rgb(MOSS, .07))
            cr.fill()
            if X(b) - X(a) > 60:
                text(cr, '⚡ plugged in', (X(a) + X(b)) / 2, Y(100) + 4, 8, MOSS, align='center')
        shown = [(max(start, s['start']), min(behind_end, s['end'])) for s in self.sleeps]
        shown = [(a, b) for a, b in shown if b > a]
        for a, b in shown:
            cr.rectangle(X(a), Y(100), X(b) - X(a), ph)
            cr.set_source_rgba(*rgb(SKY, .08))
            cr.fill()
            if X(b) - X(a) > 40 and len(shown) <= 3:  # A week of nights: the bands say it.
                text(cr, 'asleep', (X(a) + X(b)) / 2, Y(100) + 4, 8, SKY, align='center')
        cr.restore()
        # Grid: 0 / 50 / 100%, and the time axis.
        cr.set_line_width(1)
        for p in (0, 50, 100):
            cr.move_to(self.PL, round(Y(p)) + .5)
            cr.line_to(width - self.PR, round(Y(p)) + .5)
            cr.set_source_rgba(*rgb(RULE))
            cr.stroke()
            text(cr, f'{p}%', self.PL - 6, Y(p) - 7, 7.5, FAINT, align='right')
        for t, label, major in ticks(start, behind_end, now):
            x = round(X(t)) + .5
            cr.move_to(x, Y(100))
            cr.line_to(x, Y(0))
            cr.set_source_rgba(*rgb(RULE, .55 if major else .3))
            cr.stroke()
            text(cr, label, x, height - 17, 7.5 if major else 7, FAINT if major else DIM, align='center')
        past = [(X(t), Y(p)) for t, p in past_levels(self.points, start, behind_end)]
        futures = self.futures if now else None
        pct = (futures or {}).get('pct') if finite((futures or {}).get('pct')) else self.level
        if now and finite(pct):
            past.append((xn, Y(pct)))
        if now:
            text(cr, f'ahead drawn ×{X.zoom:.1f} wider' if X.zoom > 1.05 else '', width - self.PR - 4, ground - 17, 7,
                 FAINT, italic=True, align='right')
            self.draw_ahead(cr, X, Y, width, ground, futures)
        if past:
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            for w, a in ((6, .09), (2.2, 1)):
                smooth(cr, past)
                cr.set_source_rgba(*rgb(BONE, a))
                cr.set_line_width(w)
                cr.stroke()
        elif not self.points:
            text(cr, 'No power-log data in this period', width / 2, Y(55), 10, MUTED, align='center')
        if now and finite(pct):
            dense = [(a[0] + (b[0] - a[0]) * k / 4, a[1] + (b[1] - a[1]) * k / 4)
                     for a, b in itertools.pairwise(past) for k in range(4)]
            here_marker(cr, xn, Y(pct), f'You are here · {pct:.0f}%', dense, (self.PL, Y(100), xn, Y(0)))
        self.draw_hover(cr, X, Y, width, height, futures)  # Last: over everything.
        # The header row.
        behind = {'6 h': 'BEHIND · LAST 6 H', 'Day': 'BEHIND · TODAY', 'Week': 'BEHIND · THIS WEEK'}.get(
            self.range_name, 'BEHIND')
        text(cr, behind if now else self.range_name.upper(), self.PL + 4, 9, 7.5, MUTED, bold=True, spacing=1.2)
        if now:
            text(cr, f'NOW {hhmm(now)}', xn - 7, 9, 7.5, BONE, bold=True, align='right', spacing=1)
            text(cr, 'AHEAD · 2¾ H', xn + 7, 9, 7.5, MUTED, bold=True, spacing=1.2)
            kx = width - self.PR - 170
            cr.move_to(kx, 14)
            for dx, dy in ((9, -4), (9, 3), (8, -5)):
                cr.rel_line_to(dx, dy)
            cr.set_source_rgba(*rgb(MUTED))
            cr.set_line_width(1)
            cr.stroke()
            text(cr, 'a future', kx + 30, 7.5, 8, MUTED)
            cr.arc(kx + 86, 13, 2.6, 0, 2 * math.pi)
            cr.fill()
            text(cr, 'where it ends', kx + 93, 7.5, 8, MUTED)
            cr.set_source_rgba(*rgb(BONE, .5))
            cr.set_dash([1, 2])
            cr.move_to(round(xn) + .5, Y(100) - 16)
            cr.line_to(round(xn) + .5, Y(0))
            cr.stroke()
            cr.set_dash([])

    def draw_ahead(self, cr, X, Y, width, ground, futures):
        """The futures, their landing dots, brackets around the middle 8 in 10, and what each fan is."""
        xn = X(X.now)
        now = futures.get('now') if finite((futures or {}).get('now')) else X.now
        xm = lambda m: X(now + m * 60)
        if not futures or not (futures.get('empty') or futures.get('full')):
            text(cr, "Waiting for drainhound eta's forecast…" if futures is None else "", xn + 14, Y(55), 9, MUTED)
            return
        full, empty = futures.get('full'), futures.get('empty')
        cutoff, target = futures.get('cutoff_pct') or 0.0, futures.get('target') or 100
        charging = full is not None
        cr.set_line_width(1)
        # Limits: the charge target (charging) and the cutoff.
        # Labelled at the right end, unless the dots land that far (then where ahead begins).
        lines = [(cutoff, RUST, f'cutoff ~{cutoff:.0f}%', empty)]
        lines += [(target, MOSS, f'{target:.0f}% limit', full)] if charging else []
        for p, color, label, scenario in lines:
            cr.set_source_rgba(*rgb(color, .75))
            cr.set_dash([2, 3])
            cr.move_to(xn, round(Y(p)) + .5)
            cr.line_to(width - self.PR, round(Y(p)) + .5)
            cr.stroke()
            cr.set_dash([])
            last = max((xm(run['end']) for run in (scenario or {}).get('runs') or []), default=xn)
            right = last < width - self.PR - 90
            text(cr, label, width - self.PR - 3 if right else xn + 8, Y(p) + (3 if p == target else -15), 7.5, color,
                 align='right' if right else 'left', halo=SURFACE)
        cr.save()
        cr.rectangle(xn, 0, width - self.PR - xn, ground)
        cr.clip()
        pts = lambda run: [(xm(m), Y(p)) for m, p in run['pts']]
        ghost = (self.ghost or {}).get('scenario')
        if ghost:
            cr.set_dash([3, 2])
            for run in ghost['runs']:
                polyline(cr, pts(run))
                cr.set_source_rgba(*rgb(SKY, .22))
                cr.stroke()
            cr.set_dash([])
        if empty:
            color = RUST if charging else BONE
            for run in empty['runs']:
                polyline(cr, pts(run))
                cr.set_source_rgba(*rgb(color, .2 if charging else .17))
                cr.stroke()
        if full:
            for run in full['runs']:
                polyline(cr, pts(run))
                cr.set_source_rgba(*rgb(MOSS, .22))
                cr.stroke()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        if ghost:
            cr.set_dash([5, 3])
            polyline(cr, pts(ghost['median']))
            cr.set_source_rgba(*rgb(SKY))
            cr.set_line_width(2)
            cr.stroke()
            cr.set_dash([])
        if empty:
            if charging:
                cr.set_dash([5, 3])
            polyline(cr, pts(empty['median']))
            cr.set_source_rgba(*rgb(RUST if charging else BONE))
            cr.set_line_width(1.8 if charging else 2.2)
            cr.stroke()
            cr.set_dash([])
        if full:
            # The median carries the trail on, fading from bone into moss.
            line = pts(full['median'])
            fade = cairo.LinearGradient(xn, 0, xm(full['median']['end']), 0)
            fade.add_color_stop_rgba(0, *rgb(BONE))
            fade.add_color_stop_rgba(.55, *rgb(MOSS))
            for w, a in ((6, .09), (2.2, 1)):
                polyline(cr, line)
                cr.set_source(fade)
                cr.set_line_width(w)
                cr.push_group()
                cr.stroke()
                cr.pop_group_to_source()
                cr.paint_with_alpha(a)
        cr.restore()
        cr.set_line_width(1)
        r, step = 2.2, 5.2
        hours = lambda scenario, key: scenario[key] * 60 if finite(scenario.get(key)) else None
        if empty:
            low, high, median = hours(empty, 'low'), hours(empty, 'high'), hours(empty, 'hours')
            for x, j in dot_stack([xm(run['end']) for run in empty['runs']], step):
                cr.arc(x, Y(cutoff) + r + 2 + j * (2 * r + .6), r, 0, 2 * math.pi)
                cr.set_source_rgba(*rgb(RUST))
                cr.fill()
            if ghost:  # The change's futures land above the line, hollow, today's below it.
                for x, j in dot_stack([xm(run['end']) for run in ghost['runs']], step):
                    cr.arc(x, Y(cutoff) - r - 2 - j * (2 * r + .6), r, 0, 2 * math.pi)
                    cr.set_source_rgba(*rgb(SURFACE))
                    cr.fill_preserve()
                    cr.set_source_rgba(*rgb(SKY))
                    cr.set_line_width(1.2)
                    cr.stroke()
            if median is not None:
                cr.set_source_rgba(*rgb(BONE))
                cr.set_line_width(1.3)
                cr.move_to(round(xm(median)) + .5, Y(cutoff) - (0 if ghost else 8))
                cr.line_to(round(xm(median)) + .5, Y(cutoff) + 44)
                cr.stroke()
            first = xm(empty['runs'][0]['end']) - 10
            if low is not None and high is not None and not ghost:
                cr.set_source_rgba(*rgb(RUST))
                cr.set_line_width(1.2)
                y = Y(cutoff) - 3
                cr.move_to(xm(low), y)
                cr.rel_line_to(0, -4)
                cr.line_to(xm(high), y - 4)
                cr.rel_line_to(0, 4)
                cr.stroke()
                text(cr, hhmm(now + low * 60), xm(low) - 3, Y(cutoff) - 22, 7.5, MUTED, align='right', halo=SURFACE)
                text(cr, hhmm(now + high * 60), xm(high) + 3, Y(cutoff) - 22, 7.5, MUTED, halo=SURFACE)
            if median is not None:
                if charging:
                    title, note = (f'If unplugged now: cuts out at {hhmm(now + median * 60)}',
                                   f"each dot is one of {empty.get('n', len(empty['runs']))} futures, where it ends")
                else:
                    title = f'Cuts out at {hhmm(now + median * 60)}'
                    note = (f'8 in 10 futures between {hhmm(now + low * 60)} and {hhmm(now + high * 60)}'
                            if low is not None and high is not None else '')
                text(cr, title, first, Y(cutoff) + 6, 9, RUST if charging else BONE, bold=True, align='right',
                     halo=SURFACE)
                text(cr, note, first, Y(cutoff) + 20, 8, MUTED, align='right', halo=SURFACE)
            if ghost and self.ghost.get('label'):
                g_median = ghost['median']['end']
                gx = xm(max(run['end'] for run in ghost['runs'][:int(len(ghost['runs']) * .9)])) + 12
                gx = min(gx, width - self.PR - 190)
                gy = Y(30)
                later = round(g_median - (median or g_median))
                text(cr, self.ghost['label'], gx, gy, 9, SKY, bold=True, halo=SURFACE)
                text(cr, f"cuts out at {hhmm(now + g_median * 60)}, {later} min later", gx, gy + 14, 8.5, BONE,
                     halo=SURFACE)
                text(cr, self.ghost.get('gain', ''), gx, gy + 27, 8, MUTED, halo=SURFACE)
                text(cr, 'hollow dots: its futures', gx, gy + 40, 8, MUTED, halo=SURFACE)
        if full:
            low, high, median = hours(full, 'low'), hours(full, 'high'), hours(full, 'hours')
            for x, j in dot_stack([xm(run['end']) for run in full['runs']], step):
                cr.arc(x, Y(target) - r - 2 - j * (2 * r + .6), r, 0, 2 * math.pi)
                cr.set_source_rgba(*rgb(MOSS))
                cr.fill()
            if low is not None and high is not None:
                cr.set_source_rgba(*rgb(MOSS))
                cr.set_line_width(1.2)
                y = Y(target) + 3
                cr.move_to(xm(low), y)
                cr.rel_line_to(0, 4)
                cr.line_to(xm(high), y + 4)
                cr.rel_line_to(0, -4)
                cr.stroke()
                text(cr, hhmm(now + low * 60), xm(low) - 3, Y(target) + 6, 7.5, MUTED, align='right', halo=SURFACE)
                text(cr, hhmm(now + high * 60), xm(high) + 3, Y(target) + 6, 7.5, MUTED, halo=SURFACE)
            if median is not None:
                cr.set_source_rgba(*rgb(BONE))
                cr.set_line_width(1.3)
                cr.move_to(round(xm(median)) + .5, Y(target) + 8)
                cr.line_to(round(xm(median)) + .5, Y(target) - 26)
                cr.stroke()
                first = xm(full['runs'][0]['end']) - 10
                text(cr, f'Charging: full at {hhmm(now + median * 60)}', first, Y(target) - 30, 9, BONE, bold=True,
                     align='right', halo=SURFACE)
                text(cr, '8 in 10 futures land in the brackets', first, Y(target) - 16, 8, MUTED, align='right',
                     halo=SURFACE)

    def minute_at(self, X, x):
        """The minute ahead under x (None behind now)."""
        if X.now is None or x < X(X.now):
            return None
        lo, hi = 0.0, AHEAD / 60
        for _ in range(30):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if X(X.now + mid * 60) < x else (lo, mid)
        return lo

    def draw_hover(self, cr, X, Y, width, height, futures):
        if not self.hover:
            return
        hx, _ = self.hover
        minute = self.minute_at(X, hx)
        cr.set_line_width(1)
        if minute is None or not futures:
            # Behind: the level and state at that minute.
            t = X.start + (hx - self.PL) / max(1, X(X.now or X.end) - self.PL) * ((X.now or X.end) - X.start)
            near = min((p for p in self.points if finite(p.get('pct'))), key=lambda p: abs(p['t'] - t), default=None)
            if near is None or abs(near['t'] - t) > 900 or hx < self.PL:
                return
            x = X(near['t'])
            cr.set_source_rgba(*rgb(BONE, .4))
            cr.set_dash([3, 3])
            cr.move_to(round(x) + .5, Y(100))
            cr.line_to(round(x) + .5, Y(0))
            cr.stroke()
            cr.set_dash([])
            state = 'on battery' if near.get('st') == 'D' else 'plugged in'
            lines = [(f"{near['pct']:.0f}%", f"{state} · {time.strftime('%a %H:%M', time.localtime(near['t']))}")]
            self.box(cr, x, Y(near['pct']), width, height, lines)
            return
        base = futures.get('now') if finite(futures.get('now')) else X.now
        x = X(X.now + minute * 60)
        minute += (X.now - base) / 60  # The futures' own minutes: they start where eta sampled them.
        said = readout(futures, minute)
        cr.set_source_rgba(*rgb(BONE, .4))
        cr.set_dash([3, 3])
        cr.move_to(round(x) + .5, Y(100))
        cr.line_to(round(x) + .5, Y(0))
        cr.stroke()
        cr.set_dash([])
        lines = [(f'{hhmm(base + minute * 60)}', f'in {round(minute - (X.now - base) / 60)} min')]
        if 'full' in said:
            low, high = said['full']
            rounded(cr, x - 3, Y(high), 6, max(4, Y(low) - Y(high)), 3)
            cr.set_source_rgba(*rgb(MOSS))
            cr.fill()
            lines.append((f'{low:.0f}–{high:.0f}%', 'charging (8 in 10)'))
        if 'empty' in said:
            e = said['empty']
            unplugged = 'unplugged' if 'full' in said else 'on battery'
            if e['low'] is not None:
                rounded(cr, x - 3, Y(e['high']), 6, max(4, Y(e['low']) - Y(e['high'])), 3)
                cr.set_source_rgba(*rgb(RUST if 'full' in said else BONE))
                cr.fill()
                lines.append((f"{e['out']} of {e['n']}", f"cut out {unplugged}; the rest at {e['low']:.0f}–{e['high']:.0f}%"))
            else:
                lines.append((f"all {e['n']}", f'cut out {unplugged}'))
        self.box(cr, x, Y(60), width, height, lines)

    @staticmethod
    def box(cr, x, y, width, height, lines):
        """A readout box beside x: values lead, labels follow."""
        w, line_h, pad = 232, 15, 9
        h = pad * 2 + line_h * len(lines)
        bx = x + 12 if x + 12 + w < width - 4 else x - 12 - w
        by = min(max(4, y - h / 2), height - h - 26)
        rounded(cr, bx, by, w, h, 8)
        cr.set_source_rgba(*rgb(GROUND, .96))
        cr.fill_preserve()
        cr.set_source_rgba(*rgb(RULE))
        cr.set_line_width(1)
        cr.stroke()
        for i, (value, label) in enumerate(lines):
            vw, _ = text(cr, value, bx + pad, by + pad + i * line_h, 8.5, BONE, bold=True)
            text(cr, label, bx + pad + vw + 6, by + pad + i * line_h, 8.5, MUTED, width=w - vw - 6 - 2 * pad)


class PullChart(Chart):
    """What's pulling: the watts by part and app group, behind, on the hero's time axis; at Now its layers
    are labelled where they end with the collector's live split (snapshot.json), in watts and as a share."""
    PL, PR, TOP = 34, 8, 8

    def __init__(self):
        super().__init__(176)
        self.points, self.span, self.at_now, self.labels, self.now = [], (0, 1), False, None, None

    def set_data(self, points, span, at_now):
        self.points, self.span, self.at_now = points, span, at_now
        self.queue_draw()

    def set_now(self, now):
        self.now = now
        self.queue_draw()

    def set_labels(self, labels):
        if labels != self.labels:
            self.labels = labels
            self.queue_draw()

    def bins(self, start, end, width):
        """Mean watts per layer in bins of at least 5 min (and ~2 px), battery and AC apart."""
        size = max(300, (end - start) / max(1, width / 2))
        out = {}
        for p in self.points:
            if not (start <= p['t'] < end) or not p['groups']:
                continue
            k = int((p['t'] - start) // size)
            b = out.setdefault(k, {'n': 0, 'battery': 0, 'groups': {}, 'idle': {}, 'apps': {}, 't': start + k * size})
            b['n'] += 1
            b['battery'] += p.get('st') == 'D'
            for name, w in p['groups'].items():
                b['groups'][name] = b['groups'].get(name, 0) + w
            for name, w in (p.get('idle') or {}).items():
                b['idle'][name] = b['idle'].get(name, 0) + w
            for name, w in (p.get('apps') or {}).items():
                b['apps'][name] = b['apps'].get(name, 0) + w
        for b in out.values():
            for key in ('groups', 'idle', 'apps'):
                b[key] = {k: v / b['n'] for k, v in b[key].items()}
            b['battery'] = b['battery'] * 2 > b['n']
            b['t1'] = b['t'] + size
        return [out[k] for k in sorted(out)], size

    def render(self, cr, width, height):
        start, end = self.span
        X = time_axis(start, end, self.PL, width - self.PR, (self.now or time.time()) if self.at_now else None)
        behind_end = X.now or X.end
        bins, _ = self.bins(start, behind_end, width * (1 - .44 if self.at_now else 1))
        top = max([sum(b['groups'].values()) for b in bins] + [10]) * 1.08
        step = 10 if top > 25 else 5
        top = math.ceil(top / step) * step
        Y = lambda w: self.TOP + (top - w) / top * (height - self.TOP - 6)
        cr.set_line_width(1)
        w = 0
        while w <= top:
            cr.move_to(self.PL, round(Y(w)) + .5)
            cr.line_to(width - self.PR, round(Y(w)) + .5)
            cr.set_source_rgba(*rgb(RULE))
            cr.stroke()
            if w:
                text(cr, f'{w} W', self.PL - 6, Y(w) - 7, 7.5, FAINT, align='right')
            w += step
        for t, _label, major in ticks(start, behind_end, X.now):
            cr.move_to(round(X(t)) + .5, self.TOP)
            cr.line_to(round(X(t)) + .5, height - 6)
            cr.set_source_rgba(*rgb(RULE, .55 if major else .3))
            cr.stroke()
        if X.now:
            cr.set_source_rgba(*rgb(BONE, .5))
            cr.set_dash([1, 2])
            cr.move_to(round(X(X.now)) + .5, self.TOP)
            cr.line_to(round(X(X.now)) + .5, height - 6)
            cr.stroke()
            cr.set_dash([])
        anchors, anchor_x = None, None
        # Runs of battery bins as smoothed stacked areas; plugged-in runs hatched (chip power only).
        runs = []
        for b in bins:
            if runs and runs[-1][0] == b['battery'] and abs(runs[-1][1][-1]['t1'] - b['t']) < 1:
                runs[-1][1].append(b)
            else:
                runs.append((b['battery'], [b]))
        for battery, run in runs:
            xs = [X(run[0]['t'])] + [X((b['t'] + b['t1']) / 2) for b in run] + [X(min(behind_end, run[-1]['t1']))]
            if not battery:
                totals = [sum(b['groups'].values()) for b in run]
                totals = [totals[0]] + totals + [totals[-1]]
                edge = list(zip(xs, [Y(v) for v in totals]))
                smooth(cr, edge)
                cr.line_to(xs[-1], Y(0))
                cr.line_to(xs[0], Y(0))
                cr.close_path()
                cr.save()
                cr.clip_preserve()
                cr.set_source_rgba(*rgb('#2a3b37'))
                cr.fill()
                cr.set_source_rgba(*rgb('#3a4d47'))
                cr.set_line_width(2)
                for k in range(-int(height), int(xs[-1] - xs[0] + height), 6):
                    cr.move_to(xs[0] + k, Y(0))
                    cr.line_to(xs[0] + k + height, Y(0) - height)
                cr.stroke()
                cr.restore()
                if xs[-1] - xs[0] > 90:
                    text(cr, 'plugged in · chip only', (xs[0] + xs[-1]) / 2, Y(3.6), 7.5, MUTED, align='center')
                continue
            cum = [0.0] * len(xs)
            for name in STACK:
                vals = [b['groups'].get(name, 0.0) for b in run]
                vals = [vals[0]] + vals + [vals[-1]]
                upper = [c + v for c, v in zip(cum, vals)]
                smooth(cr, list(zip(xs, [Y(v) for v in upper])))
                smooth(cr, list(zip(reversed(xs), [Y(v) for v in reversed(cum)])), move=False)
                cr.close_path()
                cr.set_source_rgba(*rgb(COLORS[name]))
                cr.fill_preserve()
                cr.set_source_rgba(*rgb(GROUND))
                cr.set_line_width(.6)
                cr.stroke()
                if anchor_x is None or xs[-1] >= anchor_x:
                    anchors = anchors or {}
                    anchors[name] = (cum[-1] + upper[-1]) / 2
                cum = upper
            anchor_x = xs[-1]
        if not bins:
            text(cr, 'No battery time in this period', width / 2, height / 2 - 8, 10, MUTED, align='center')
        if X.now and self.labels and anchors:
            self.draw_labels(cr, X, Y, width, anchors, anchor_x)
        if self.hover and bins:
            self.draw_hover(cr, X, Y, width, height, bins)

    def draw_labels(self, cr, X, Y, width, anchors, anchor_x):
        """The live split on the layers: name, watts and share; the idle chip's rails under it."""
        lx, rx = X(X.now) + 26, width - self.PR - 4
        wx, total = rx - 40, self.labels['total'] or 0
        y = self.TOP + 2
        order = {name: i for i, name in enumerate(reversed(STACK))}
        rows = sorted(self.labels['rows'], key=lambda r: (order.get(r['layer'], 99), -r['watts']))
        for row in rows:
            color = COLORS.get(row['layer'], COLORS['Other apps'])
            if row['layer'] in anchors:
                ay = Y(anchors[row['layer']])
                cr.move_to(anchor_x + 2, ay)
                cr.line_to(X(X.now) + 6, ay)
                cr.line_to(lx - 6, y + 6)
                cr.set_source_rgba(*rgb(color, .8))
                cr.set_line_width(1)
                cr.stroke()
                cr.arc(anchor_x + 1, ay, 2, 0, 2 * math.pi)
                cr.fill()
            rounded(cr, lx - 2, y + 2.5, 8, 8, 2)
            cr.set_source_rgba(*rgb(color))
            cr.fill()
            name = row['name'] + (' ▾' if row.get('parts') else '')
            text(cr, name, lx + 10, y, 8.5, BONE, width=wx - lx - 60)
            text(cr, f"{row['watts']:.1f} W", wx, y, 8.5, BONE, bold=True, align='right')
            share = row['share'] if finite(row.get('share')) else (row['watts'] / total * 100 if total else None)
            text(cr, f'{share:.0f}%' if finite(share) else '', rx, y, 8.5, MUTED, align='right')
            y += 15
            if row.get('sub'):
                text(cr, row['sub'], lx + 10, y - 2, 7.5, MUTED, width=rx - lx - 10)
                y += 11
            for part, watts, pct in row.get('parts') or []:
                text(cr, part, lx + 10, y - 2, 7.5, MUTED)
                text(cr, f'{watts:.1f} W', wx, y - 2, 7.5, MUTED, align='right')
                text(cr, f'{pct:.0f}%' if finite(pct) else '', rx, y - 2, 7.5, FAINT, align='right')
                y += 11.5
            y += 2

    def draw_hover(self, cr, X, Y, width, height, bins):
        hx, hy = self.hover
        near = min(bins, key=lambda b: abs(X((b['t'] + b['t1']) / 2) - hx))
        x = X((near['t'] + near['t1']) / 2)
        if abs(x - hx) > 30:
            return
        cr.set_source_rgba(*rgb(BONE, .5))
        cr.set_line_width(1)
        cr.move_to(round(x) + .5, self.TOP)
        cr.line_to(round(x) + .5, height - 6)
        cr.stroke()
        total = sum(near['groups'].values())
        span = self.span[1] - self.span[0]
        when = (f"{time.strftime('%a %H:%M' if span > 86400 else '%H:%M', time.localtime(near['t']))}–"
                f"{time.strftime('%H:%M', time.localtime(near['t1']))}")
        lines = [(f'{total:.1f} W', ('on battery · ' if near['battery'] else 'plugged in · chip only · ') + when, None)]
        for name in sorted(near['groups'], key=lambda n: -near['groups'][n]):
            if near['groups'][name] >= .05:
                lines.append((f"{near['groups'][name]:.1f} W", name, COLORS.get(name)))
            if name == report.IDLE and set(near['idle']) != {report.IDLE}:
                lines += [(f'{w:.1f} W', f'   {part}', None) for part, w in
                          sorted(near['idle'].items(), key=lambda kv: -kv[1]) if w >= .05]
        top = sorted(near['apps'].items(), key=lambda kv: -kv[1])[:3]
        if top:
            lines += [('Top apps', '', None)] + [(f'{w:.2f} W', name, None) for name, w in top]
        tooltip(cr, lines, x, hy, width, height)



def tooltip(cr, lines, x, y, width, height):
    """Values lead, labels follow; series keyed by a short stroke of their colour."""
    pad, line_h, w = 8, 15, 236
    h = pad * 2 + line_h * len(lines)
    tx = x + 14 if x + 14 + w < width else x - 14 - w
    ty = min(max(4, y - h / 2), height - h - 4)
    rounded(cr, tx, ty, w, h, 8)
    cr.set_source_rgba(*rgb(GROUND, .97))
    cr.fill_preserve()
    cr.set_source_rgba(*rgb(RULE))
    cr.set_line_width(1)
    cr.stroke()
    for i, (value, label, color) in enumerate(lines):
        yy = ty + pad + i * line_h
        if color:
            cr.set_source_rgba(*rgb(color))
            cr.set_line_width(3)
            cr.move_to(tx + pad, yy + 7)
            cr.line_to(tx + pad + 10, yy + 7)
            cr.stroke()
        offset = tx + pad + (16 if color else 0)
        vw, _ = text(cr, value, offset, yy, 8, BONE, bold=True)
        text(cr, label, offset + vw + 6, yy, 8, MUTED, width=w - (offset - tx) - vw - 6 - pad)


class Cone(Gtk.DrawingArea):
    """One gain (minutes per full charge) and its 90% interval, as a tapered cone with a dot at the
    estimate, on an axis shared down its column. Estimates bone, tested settings moss."""

    def __init__(self, width=120):
        super().__init__(content_width=width, content_height=18, valign=Gtk.Align.CENTER)
        self.gain, self.scale, self.tested = None, (0, 60), False
        # Not a bound method: one would hold the cone from its own draw func, a cycle PyGObject can't collect.
        self.set_draw_func(Cone.paint)

    def set_data(self, gain, scale, tested):
        if (gain, scale, tested) != (self.gain, self.scale, self.tested):
            self.gain, self.scale, self.tested = gain, scale, tested
            self.queue_draw()

    @staticmethod
    def paint(cone, cr, width, height):
        low, high = cone.scale
        sx = lambda m: 10 + (min(max(m, low), high) - low) / (high - low) * (width - 20)
        cr.set_line_width(1)
        step = 20 if high - low > 40 else 5
        v = math.ceil(low / step) * step
        while v <= high:
            cr.move_to(round(sx(v)) + .5, 1)
            cr.line_to(round(sx(v)) + .5, height - 1)
            cr.set_source_rgba(*rgb(RULE if v else FAINT, .5 if v else .8))
            cr.stroke()
            v += step
        gain = cone.gain
        if not gain or not finite(gain.get('minutes')):
            return
        color, cy = MOSS if cone.tested else BONE, height / 2
        if finite(gain.get('low'), gain.get('high')):
            cr.move_to(sx(gain['low']), cy)
            cr.line_to(sx(gain['minutes']), cy - 5.5)
            cr.line_to(sx(gain['high']), cy)
            cr.line_to(sx(gain['minutes']), cy + 5.5)
            cr.close_path()
            cr.set_source_rgba(*rgb(color, .5 if cone.tested else .32))
            cr.fill()
        cr.arc(sx(gain['minutes']), cy, 3, 0, 2 * math.pi)
        cr.set_source_rgba(*rgb(color))
        cr.fill()


class ConeAxis(Gtk.DrawingArea):
    """The minutes over the column of cones."""

    def __init__(self, width=120):
        super().__init__(content_width=width, content_height=16, valign=Gtk.Align.END)
        self.scale = (0, 60)
        self.set_draw_func(ConeAxis.paint)

    def set_scale(self, scale):
        if scale != self.scale:
            self.scale = scale
            self.queue_draw()

    @staticmethod
    def paint(area, cr, width, _height):
        low, high = area.scale
        step = 20 if high - low > 40 else 5
        v = math.ceil(low / step) * step
        while v <= high:
            x = 10 + (v - low) / (high - low) * (width - 20)
            text(cr, f'+{v:g}' if v > 0 else f'{v:g}', x, 2, 7, FAINT, align='center')
            v += step


class SleepDots(Gtk.DrawingArea):
    """This week's suspends as dots (%/h by day), against the all-history median."""

    def __init__(self):
        super().__init__(content_width=240, content_height=62, hexpand=True)
        self.dots, self.median = [], None
        self.set_draw_func(SleepDots.paint)

    def set_data(self, dots, median):
        self.dots, self.median = dots, median
        self.queue_draw()

    @staticmethod
    def paint(area, cr, width, height):
        top = max([v for _, v in area.dots] + [area.median or 0, 3])
        top = math.ceil(top)
        X = lambda ago: 14 + (6 - ago) * (width - 28) / 6
        Y = lambda v: 6 + (top - v) / top * 36
        cr.set_line_width(1)
        for v in range(top + 1):
            cr.move_to(0, round(Y(v)) + .5)
            cr.line_to(width, round(Y(v)) + .5)
            cr.set_source_rgba(*rgb(RULE, 1 if v == 0 else .5))
            cr.stroke()
        text(cr, f'{top} %/h', 0, Y(top) - 2, 7, FAINT)
        if area.median is not None:
            cr.set_dash([3, 3])
            cr.move_to(0, Y(area.median))
            cr.line_to(width, Y(area.median))
            cr.set_source_rgba(*rgb(SKY, .8))
            cr.stroke()
            cr.set_dash([])
            text(cr, f'median {area.median:.1f} %/h', width, Y(area.median) - 13, 7, SKY, align='right')
        now = time.time()
        for ago in range(7):
            day = time.strftime('%a', time.localtime(now - ago * 86400))
            text(cr, day, X(ago), height - 13, 7, FAINT, align='center')
        for ago, v in area.dots:
            cr.arc(X(ago), Y(v), 5, 0, 2 * math.pi)
            cr.set_source_rgba(*rgb(SKY))
            cr.fill()
            text(cr, f'{v:.1f}', X(ago) + 7, Y(v) - 6, 7, BONE)


class ProcessList(Gtk.DrawingArea):
    """The live CPU list: each app's processes as one row, with a bar."""
    ROW = 19

    def __init__(self):
        super().__init__(content_height=5 * self.ROW, hexpand=True)
        self.rows = []
        self.set_draw_func(ProcessList.paint)

    def set_data(self, rows):
        if rows != self.rows:
            self.rows = rows
            self.set_content_height(max(1, len(rows)) * self.ROW)
            self.queue_draw()

    @staticmethod
    def paint(area, cr, width, height):
        if not area.rows:
            text(cr, 'Waiting for the collector…', 0, 2, 8.5, MUTED)
            return
        top = max(p for _, p, _ in area.rows) or 1
        bar_x, bar_w = width - 122, 70
        for i, (name, pct, count) in enumerate(area.rows):
            y = i * ProcessList.ROW + 2
            w, _ = text(cr, name, 0, y, 8.5, BONE, width=bar_x - 12)
            if count > 1:
                text(cr, f'▸ {count} processes', w + 6, y + 1, 7.5, FAINT)
            rounded(cr, bar_x, y + 6, bar_w, 5, 2.5)
            cr.set_source_rgba(*rgb(RULE))
            cr.fill()
            rounded(cr, bar_x, y + 6, max(3, bar_w * pct / top), 5, 2.5)
            cr.set_source_rgba(*rgb(MUTED))
            cr.fill()
            text(cr, f'{pct:.1f}%', width, y, 8.5, MUTED, align='right')


class Paw(Gtk.DrawingArea):
    """The panel's mark, beside its title."""

    def __init__(self):
        super().__init__(content_width=22, content_height=22, valign=Gtk.Align.CENTER)
        self.set_draw_func(lambda _a, cr, w, h: paw(cr, w / 2, h / 2 + 1, 1, BONE))


class Forest(Gtk.DrawingArea):
    """One setting's gain on the forest plot's shared axis (−5 … +25 min per charge): the 90% interval as a
    tapered cone, a paw at the estimate; moss when it surely saves, muted when the interval crosses zero."""
    LOW, HIGH = -5, 25

    def __init__(self):
        super().__init__(content_width=180, content_height=30, valign=Gtk.Align.CENTER)
        self.effect = None
        self.set_draw_func(Forest.paint)

    def set_data(self, effect):
        if effect != self.effect:
            self.effect = effect
            self.queue_draw()

    @staticmethod
    def x(width, m):
        return 8 + (min(max(m, Forest.LOW), Forest.HIGH) - Forest.LOW) / (Forest.HIGH - Forest.LOW) * (width - 16)

    @staticmethod
    def paint(area, cr, width, height):
        cr.set_line_width(1)
        for v in range(Forest.LOW, Forest.HIGH + 1, 5):
            cr.move_to(round(Forest.x(width, v)) + .5, 3 if v else 1)
            cr.line_to(round(Forest.x(width, v)) + .5, height - (3 if v else 1))
            cr.set_source_rgba(*rgb(FAINT if v == 0 else RULE, .7 if v == 0 else .45))
            cr.stroke()
        e = area.effect
        if not e or not finite(e.get('minutes')):
            return
        sure = finite(e.get('chance'), e.get('low')) and e['chance'] >= .9 and e['low'] > 0
        color, cy, gx = MOSS if sure else MUTED, height / 2, Forest.x(width, e['minutes'])
        if finite(e.get('low'), e.get('high')):
            lo, hi = Forest.x(width, e['low']), Forest.x(width, e['high'])
            cr.move_to(lo, cy)
            cr.line_to(gx, cy - 7)
            cr.line_to(hi, cy)
            cr.line_to(gx, cy + 7)
            cr.close_path()
            cr.set_source_rgba(*rgb(color, .45 if sure else .28))
            cr.fill()
            cr.move_to(lo, cy)
            cr.line_to(hi, cy)
            cr.set_source_rgba(*rgb(color))
            cr.stroke()
        cr.arc(gx, cy, 6.5, 0, 2 * math.pi)
        cr.set_source_rgba(*rgb(GROUND))
        cr.fill_preserve()
        cr.set_source_rgba(*rgb(color))
        cr.set_line_width(1.2)
        cr.stroke()
        paw(cr, gx, cy + .6, .42, color)


class ForestAxis(Gtk.DrawingArea):
    """The minutes over the forest plot."""

    def __init__(self):
        super().__init__(content_width=180, content_height=16, valign=Gtk.Align.END)
        self.set_draw_func(lambda _a, cr, w, _h: [text(cr, f'+{v}' if v > 0 else '0' if v == 0 else f'−{-v}',
                                                       Forest.x(w, v), 2, 7, FAINT, align='center')
                                                  for v in range(Forest.LOW, Forest.HIGH + 1, 5)])


class WorthChart(Gtk.DrawingArea):
    """Whether more testing pays: for each amount of testing, what it would save over the next month (moss)
    and what it costs now (rust), in Wh, on one scale."""

    def __init__(self):
        super().__init__(content_height=78, hexpand=True)
        self.rows = []
        self.set_draw_func(WorthChart.paint)

    def set_data(self, rows):
        if rows != self.rows:
            self.rows = rows
            self.set_content_height(max(1, len(rows)) * 25 + 4)
            self.queue_draw()

    @staticmethod
    def paint(area, cr, width, _height):
        if not area.rows:
            return
        top = max([max(r['saves'], r['costs']) for r in area.rows] + [1])
        X = lambda v: 44 + max(0.0, v) / top * (width - 150)
        for i, r in enumerate(area.rows):
            y = 4 + i * 25
            text(cr, f"{r['hours']:.0f} h", 0, y + 3, 8.5, BONE)
            for value, color, dy, label in ((r['saves'], MOSS, 0, 'saves'), (r['costs'], RUST, 10, 'costs')):
                rounded(cr, X(0), y + dy, max(2, X(value) - X(0)), 8, 2)
                cr.set_source_rgba(*rgb(color, .9))
                cr.fill()
                text(cr, f'{label} {value:.1f} Wh' if value < 9.5 else f'{label} {value:.0f} Wh', X(value) + 4,
                     y + dy - 2, 7, color)

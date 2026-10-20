# How wattif works

Linux has no per-process power meter. What a laptop does measure is the
battery's draw, and on many machines the chip's (amdgpu's package power, or
RAPL's package energy where it's readable). wattif records those totals with
everything that could explain them, fits models to them, and runs
experiments where a model alone can't tell cause from coincidence.

## Measuring

`wattif collect` (the `wattif-collector` user service) samples the battery
and the chip's power several times a second. Every 2 s it scans the processes'
CPU time. Every 10 s it writes one **record** of raw inputs, never its own
conclusions, so better models can be fitted to old data later:

- battery: watts (mean/min/max), %, charge or energy, voltage, status
- chip power, and on AMD APUs the two rails it is made of (from `gpu_metrics`)
- CPU: user/system/irq seconds, mean and active-core clock, C-state residency
- per app: CPU seconds, GPU and video-engine seconds (DRM fdinfo, by client),
  disk bytes
- devices: Wi-Fi and other network bytes, disk busy time, runtime-active USB
  and PCI devices, open audio streams, backlight and keyboard backlight,
  refresh rate
- settings: profile, EPP, governor, boost, ASPM, NVMe APST, Wi-Fi power save,
  CPU caps, GPU DPM, charge limit, rfkill
- thermals, lid, screen lock, the experiment arm, 👍/👎 votes

Records go into the SQLite store and, as JSON lines, into a compressed daily
archive (see [data.md](data.md)). Suspends and other gaps become `gap` events
carrying the battery level before and after. A record never spans a suspend.

"Apps" are coarse: the executable's name, or the Electron app's directory,
with browser helpers folded into their browser. The collector stores no
command lines, URLs or titles.

## Attributing the draw

Two least-squares models are fitted on one-minute means of battery time
(the battery's reading lags the chip's by ~10 s, so shorter windows blur):

**Chip power** = floor + clock × mean GHz + load × Σ busy-seconds × GHz² +
gpu × GPU-seconds + video × video-seconds (+ a column per setting under test).
Dynamic power grows with frequency and voltage, hence GHz². The clock term
matters on its own: the whole package's voltage follows the clock, so a high
clock costs power even while the cores are mostly idle. Idle cores report a
placeholder clock set by the platform profile, so each profile gets its own
*idle clock* (where the clock sits when almost nothing runs: a low percentile
of the minutes). The clock up to it belongs to the idle chip; above it, the apps
that ran pay for it, shared by their busy time.

**Battery power** = base + k × chip power + display × backlight + Wi-Fi ×
traffic + storage × busy time + USB + audio + keyboard backlight. k > 1:
voltage conversion, plus rails outside the chip's own sensor whose draw
follows the chip's. Each chip watt therefore costs k battery watts.

Both are non-negative, lightly shrunk toward prior guesses, so a feature that
never varies keeps a plausible value. Whatever the devices don't explain is
"Rest of system". On AC the battery reading is charge, not consumption, so
only chip power is split there.

**Without a chip power sensor** (most Intel laptops, unless RAPL is made
readable) the chip model's columns go straight into the battery model, so
its terms come out in battery watts. Attribution then works the same way,
with less certainty about how the idle chip compares with the rest of the
system.

On machines that report them, the idle chip is split by its measured rails:
cores and graphics (VDD), fabric and display engine (SoC), and memory and I/O
links (package less both). Power delivery, the (k − 1) × chip part, is shown
on its own.

The estimator refits every model every 15 minutes on the last 30 days and
writes them to `models.json`. The collector's live breakdown and the panel use
that file, so the whole program agrees on one fit.

## Time left

`wattif eta` forecasts the draw, not the percentage:

- **Remaining energy** comes from the battery's energy counter, or for
  charge-counting batteries from the charge times the learned loaded-voltage
  curve. That curve matters: voltage sags toward empty, so the bottom
  percents hold less energy.
- **The draw** is an Ornstein–Uhlenbeck process in log watts (one-minute
  means). It reverts to a *local level*: the typical draw of the last ~5 hours
  on battery, not a month-long mean, so a light day and a build day each
  forecast from their own recent use. The recent 10 minutes decay toward that
  level. The decay time and variance are fitted from history.
- **The 80% interval** includes the level's uncertainty, is calibrated
  against backtests of past discharges, and is corrected for the log-normal's
  mean sitting above its median.
- **Time to full** integrates the learned minutes-per-percent charge curve
  (constant current, then a taper), scaled by the last minutes' rate.

**Empty is where the battery cuts out, which is often not 0%.** Many embedded
controllers cut the power with charge still on the gauge; the laptop this grew
on cut at ~1.8%, every time, with the loaded voltage still normal. wattif
learns that cutoff from boots that ended on battery at a low level with no
shutdown. How each boot ended comes from the journal, so a crash or a reboot
can't teach it. Below 5%:

- the time comes from the gauge's own slope, extrapolated to the cutoff;
- a black box fsyncs the last reading every 2 s, since the database can lose
  its last seconds;
- notifications come at 5 minutes and 1 minute;
- with `battery.poweroff = true`, it runs `systemctl poweroff` 45 s before the
  early edge of the cutoff's estimate (only once a cut-out has taught the
  cutoff, and only when two readings agree), and records that the boot was
  powered off, so a power-off never teaches the cutoff.

**Asleep**: how long a suspend from now would last is the remaining energy
over a log-normal fit of past suspends' drain (longer ones weigh more), with an
80% predictive interval.

## Experiments

Observational data can't tell what a setting does: you turn on power saving
when the battery is low, and you run builds on AC. So wattif randomizes.

**Blocks.** While enabled and on battery, every 4-minute block sets each
setting in the pool to one state, with the first minute ignored (washout:
the battery reading lags, links retrain). A manual run (`wattif experiment
start a b c --minutes N`) cycles a shuffled full factorial of up to four
settings. The automatic runner picks each block's combination with the plan
below.

**Analysis** (`report.lever_effects`): one ridge regression over the clean
blocks of every run:

- battery watts on a baseline per run and day, so only comparisons within a
  run and day count;
- workload covariates (CPU load, GPU, display, Wi-Fi, disk…);
- an indicator per setting, from the values the experiment set, not what was
  read back;
- pairwise interactions of settings randomized together, shrunk toward zero.

Settings that change the CPU's load itself (boost, the profile, a frequency
cap, GPU clocks, app throttling) are fitted without the load covariate,
because load is how they act. Intervals come from a moving-block bootstrap;
blocks correlate for an hour or more.

**Planning** (`report.plan`). The ridge model is read as a Bayesian linear
model (the penalties are its Gaussian prior), so its posterior is closed-form.
For each allowed combination, the plan computes two things. One is how much
observing it would shrink the variance of predictions across all allowed
combinations. The other is what it is expected to cost: the watts it would
draw above the best combination, over posterior draws. As in
information-directed sampling, it samples combinations in proportion to
information / (cost + 0.3 W)², so blocks stay with combinations that might be
best while the model still improves. In simulation, a setting that clearly
costs 3 W got ~13% of blocks instead of half.

**Comfort.** While you're at the laptop (unlocked, screen on), only
combinations with at most a 10% chance per block of earning a 👎 are allowed.
Two logistic models over 4-minute windows give those chances: one for 👎, one
for 👍, each with an effect per setting. "Noticeable" settings (refresh rate,
CPU limits, panel power savings…) start out with a prior of higher 👎 odds, so
experiments begin with about one at a time. A 👎 ends the block at once,
restores your settings and rests 10 minutes. While the screen is locked,
anything goes.

**When to stop** (`report.testing_value`). More testing improves the
recommendation, which only changes a setting where the change saves with 90%
certainty (Benjamini–Hochberg across settings). For 10, 30 and 100 more
hours, wattif simulates the posterior after that much testing. Testing h
hours *saves* the expected loss reduction times your battery hours over the
next 30 days. It *costs* h × the watts blocks draw above the recommendation.
When no amount pays, the runner pauses with the recommended combination set,
and looks again every 6 hours or when the pool changes. As old blocks leave
the 30-day window, testing resumes by itself. After a kernel or BIOS update it
tests 10 hours first.

**Goals.** *Battery* recommends the measured combination with the lowest
draw. *Comfort* recommends the one most likely to earn a 👍, then the lowest
draw. Both only consider allowed combinations. `wattif experiment goal
comfort|battery --apply` switches.

**Safety.**
- Risky settings (ASPM, NVMe APST, GPU clocks by default) stay out of
  automatic runs unless added with `--risky`. Once changed, one holds for 45
  minutes, changes at most 8 times a day, and never changes in the minute
  after a resume.
- Settings are always restored in a fixed order, risky ones first.
- A watchdog reads the kernel log every 30 s while experiments run, and the
  previous boot's at start. On a dead USB controller, a Bluetooth timeout, a
  PCIe, GPU or NVMe error, or a crash reset while awake, it restores stock
  settings, quarantines what changed in the 15 minutes before, and turns
  experiments off until you turn them on again. A boot that died asleep only
  pauses the run.
- Applied settings (`apply`, `goal --apply`) last until reboot, on battery
  only: the reconciler puts them back to stock on AC and sets them again on
  battery.

**Boot-level settings** (e.g. panel self-refresh on amdgpu) can't change at
runtime. With `experiments.boot_arms` configured, the panel picks the boot
entry for the next restart only. Boots are compared on their baselines, with a
Welch t interval over boots once each arm has 5.

## What-if

"Close Firefox", "dim to 50%": the period's battery minutes are re-predicted
with that app's activity (or that backlight) removed, from the fitted models.
Experiment minutes count with their settings held at yours. The 90% intervals
refit the models on resamples of whole days, widened as Student's t over the
days. In simulation, the 10-minute blocks used earlier covered dimming's true
effect only ~48% of the time.

## Background apps

**Throttle** (`wattif throttle`, started by the `throttle-apps` setting). On
battery, every app with a window except the focused one gets a CPU quota
(cgroup `cpu.max`, 10% of a core per app by default). The quota lifts the
moment the app is focused and stays off for 2 minutes after it loses focus.
An app is every systemd scope with its launch pid: Electron apps split into
two. Scopes in nested slices (uwsm, app2unit and Flatpak use
`app.slice/app-graphical.slice`) count too; a Flatpak app's sandboxes are one
app, and so is a launcher's randomly named scope with the app of its name. A
window belongs to the app its pid's scope says, or, when the pid says nothing
(niri gives every X11 window xwayland-satellite's), to the app its app id
names. While the focused window belongs to no app wattif can tell, nothing is
capped. Never throttled:

- apps playing or recording audio or video (a *running* PipeWire stream);
- apps without a window (helpers, clipboard servers);
- terminals, editors and IDEs (builds and agents run there);
- input methods;
- anything while on AC.

**Freeze** (`wattif freeze`, Android's cached-app freezer for apps you list).
On battery, a listed app that hasn't had focus for 30 minutes and has no
window on a workspace in view gets `cgroup.freeze`, so it uses no CPU at all.
It thaws at once when you focus it, switch to its workspace, start it again or
plug in, and every 10 minutes it thaws for 10 s so timers and sync catch up.
An app that is busy (over a quarter of a core) or playing media keeps running.
Without live compositor state, nothing is frozen; nor while a focused or
visible window belongs to no app wattif can tell (under niri, a game Steam
started), since it might be one of yours.

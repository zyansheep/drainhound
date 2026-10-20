# wattif's data and files

Everything wattif keeps is local and readable. The paths below are the
defaults; `WATTIF_STATE_DIR` and `WATTIF_RUNTIME_DIR` move them.

## `~/.local/state/wattif` (kept)

| File | What |
| --- | --- |
| `power.sqlite3` | the power database (below): what the models, panel and estimator read |
| `power/YYYY-MM-DD.jsonl[.zst]` | the raw archive: one JSON line per record and event, finished days zstd-compressed |
| `power/models.json` | every fitted model, written by the estimator every 15 minutes |
| `power/index.json` | per-day record counts (the menu's footer) |
| `qol.json` | your last 100 👍/👎 votes and any rest after a 👎 |
| `deaths.json` | battery cut-outs (and clean power-offs) the estimator has timed; `.bak` is the previous version |
| `lastgasp.json` | the black box: the last reading below 5%, fsynced every 2 s |
| `experiments-enabled` | present while automatic experiments are on |
| `experiments.json` | the experiment pool, risky opt-ins, quarantine, goal |
| `experiment-log.json` | every run and every setting change experiments made |
| `applied.json` | settings `apply` set, for this boot |

## `$XDG_RUNTIME_DIR/wattif` (live, gone at logout)

These are the files a bar, a widget or a script can read. Each is replaced
atomically.

**`eta.json`** (the estimator, every 2 s; stale after 15 s): `updated`,
`status`, `pct`, `energy_pct` (remaining *energy*, the fill), `energy_wh`,
`watts`, `draw`, `hours`/`low`/`high` (time left and its 80% interval),
`target` and `banked`* while charging, `cutout`/`poweroff` (epoch seconds,
near the end), `sleep {hours, low, high, watts, suspends}`, `distribution
{kind, t, p}`, and `waybar` (the label: `text`, `class`, `percentage`).

**`snapshot.json`** (the collector, every 2 s): `timestamp`; `battery {title,
rows: [{name, value, parts?}], footer}`, the battery use of the last 30
minutes on battery, the five biggest then "Everything else (N)", so shares
add up to 100%; `cpu` and `memory`, up to 100 `{name, value}` rows each
(past-minute CPU share, resident memory); `temperature` (every hwmon and
thermal zone sensor); `coverage` (seconds of CPU history behind `cpu`).

**`recommend.json`** (the estimator, at each refit; stale after an hour):
`updated`, and per goal (`battery`, `comfort`): `change` (what it would
change from your settings), `gain` (minutes per full charge) with its 90%
`interval`, `delight` (👍 chance per block now → then), `apply` (the
assignments `wattif experiment apply` takes), `text`.

**`experiment.json`** (the runner): `mode` (manual/auto), `status`
(running/paused/done), `reason`, `block`, `block_start`/`block_end`,
`washout_until`, `name`, `arm`, `value`, `tested` (seconds of battery time
tested), `settled`, `worth {rows: [{hours, saves, costs}]}`.

Also `throttle.json` and `freeze.json` (what they hold), `power-source.json`
and the lock files.

## The power database

`power.sqlite3`, written by the collector one transaction per record (WAL).
Readers open it read-only. `PRAGMA user_version` is its layout. Older
layouts are migrated forward, never dropped.

| Table | Contents |
| --- | --- |
| `records` | one row per 10 s record: every scalar as a typed column, NULL where absent (columns below) |
| `record_apps`, `app_names` | per record and app that did something: CPU, GPU and video seconds, disk bytes read and written |
| `minutes` | per-minute aggregates the models train on, recomputed as each record lands: watts by majority status, load, clock, devices, settings, the experiment and per-app load (`apps`, `settings`, `exp` as JSON) |
| `events` | collector starts, gaps (suspends: `gap_s`, `suspended_s`, battery before and after) and votes |
| `imports` | archive days the database has |
| `meta` | migration bookkeeping |

`records` columns, by group:

- **time**: `t0`, `t1`, `dt`, `boot`
- **battery**: `ac`, `status`, `bat_w`/`bat_min`/`bat_max`/`bat_n`, `pct`, `charge`, `unit` (`uAh` or `uWh`), `volts`
- **chip**: `soc_w`/`soc_min`/`soc_max`/`soc_n`, and the rails `vdd_w`, `fabric_w`
- **CPU**: `busy_s`, `user_s`, `sys_s`, `irq_s`, `iowait_s`, `steal_s`, `mhz`, `mhz_max`, `mhz_active`, `active_n`, `ctxt`, `intr`, C-states `c_poll_s`, `c1_s`, `c2_s`, `c3_s`, IPIs `ipi_call`, `ipi_timer`, `ipi_tlb`, `ipi_resched`, `irq_devices`
- **GPU**: `gpu_busy`, `gpu_s`, `video_s`
- **devices**: `wifi_rx`, `wifi_tx`, `net_rx`, `net_tx`, `wifi_dbm`, `disk_read`, `disk_write`, `disk_busy`, `usb`, `pci_active`, `audio_play`, `audio_rec`
- **display**: `bl`, `kbd`, `abm`, `hz`, `screen_on` (the internal panel's, eDP, LVDS or DSI, as niri or sway reports it), `night_light`, `grayscale`
- **settings**: `profile`, `epp`, `governor`, `boost`, `amd_pstate`, `max_mhz`, `min_mhz`, `cpu_max_mhz`, `gpu_dpm`, `throttle`, `aspm`, `nvme_apst_us`, `dcdebugmask`, `wifi_ps`, `rfkill_bluetooth`, `rfkill_wlan`, `charge_limit`
- **session**: `temp`, `fan`, `lid`, `locked`, `qol`
- **experiment**: `exp_run`, `exp_name`, `exp_block`, `exp_arm`, `exp_value`, `exp_washout`

Query it directly:

```sh
sqlite3 ~/.local/state/wattif/power.sqlite3 \
  "select date(t1, 'unixepoch', 'localtime') day, round(avg(bat_w), 1) watts, count(*) / 360.0 hours
   from records where status = 'Discharging' group by day"
duckdb -c "select * from sqlite_scan('$HOME/.local/state/wattif/power.sqlite3', 'records') limit 5"
```

Size: ~1.4 KB per record, ~12 MB per day awake. The archive is ~2.3 MB per
day compressed. It alone keeps idle apps' process counts and memory, the
per-interrupt and per-interface breakdowns and display layouts. On startup
the collector imports any archive day the database lacks, and `wattif
collect import` re-reads every day, keeping what is stored.

## Privacy

App names are coarse: the executable, or an Electron app's directory, with
browser helpers folded into the browser. Nothing stores command lines, URLs,
window titles or file names. The optional browser bridge
([contrib/firefox-bridge.js](../contrib/firefox-bridge.js)) labels a browser's
processes with their sites in `snapshot.json`, in memory only, for the live
CPU list. Private windows are left out.

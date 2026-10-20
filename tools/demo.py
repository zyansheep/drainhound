"""A synthetic month of data in a fresh state directory, to try the panel or
take screenshots without a month of your own:

    python tools/demo.py /tmp/wattif-demo
    WATTIF_STATE_DIR=/tmp/wattif-demo/state wattif panel serve --open

The month is the tests' (tests/test_panel_data.month: battery and AC time,
manual and automatic experiments, votes, suspends) with everyday app names.
"""
import contextlib
import sys
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / 'src')]

from tests.test_panel_data import month  # noqa: E402
from wattif import store  # noqa: E402

NAMES = {'Floorp': 'Firefox', 'yosys': 'cargo'}
EXTRA = {'Code': 0.12, 'Slack': 0.06, 'Spotify': 0.04, 'Niri': 0.03, 'PipeWire': 0.01}


def main(directory):
    state = Path(directory) / 'state'
    state.mkdir(parents=True, exist_ok=True)
    db = state / 'power.sqlite3'
    if db.exists():
        sys.exit(f'{db} exists; give an empty directory')
    # The month's last day began 8 hours ago, so 'Now' shows its battery time.
    minutes, events = month(now=time.time() + 16 * 3600)
    for m in minutes:
        if m.get('apps'):
            apps = {NAMES.get(name, name): values for name, values in m['apps'].items()}
            load = m['load']
            m['apps'] = {**apps, **{name: [share * load, 0.0, 0.0] for name, share in EXTRA.items()}}
    with contextlib.closing(store.connect(db)) as conn, store.transaction(conn):
        conn.executemany(store.INSERT_MINUTE, [store.minute_values(m) for m in minutes])
        conn.executemany(store.INSERT_EVENT, [(e.get('event'), e.get('t1', 0), e.get('t0'), e.get('boot'),
                                               e.get('gap_s'), store.compact(e)) for e in events])
    print(f'{len(minutes)} minutes, {len(events)} events in {db}')
    print(f'WATTIF_STATE_DIR={state} WATTIF_RUNTIME_DIR={Path(directory) / "runtime"} wattif panel serve --open')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__))

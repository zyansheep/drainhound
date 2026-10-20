"""Tests never read your configuration or touch your data: drainhound's directories
point into a temporary one, and no config file is read (only the defaults)."""
import os
import tempfile

_root = tempfile.mkdtemp(prefix='drainhound-test-')
os.environ['DRAINHOUND_STATE_DIR'] = os.path.join(_root, 'state')
os.environ['DRAINHOUND_RUNTIME_DIR'] = os.path.join(_root, 'runtime')
os.environ['DRAINHOUND_CONFIG'] = ''
os.environ['TZ'] = os.environ.get('DRAINHOUND_TEST_TZ', 'UTC')
# One BLAS thread, as the services run (drainhound/__init__ sets it too, but a test
# module may import numpy first): one per core made a busy machine's suite 7× slower.
os.environ['OPENBLAS_NUM_THREADS'] = os.environ['OMP_NUM_THREADS'] = '1'

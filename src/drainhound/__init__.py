"""drainhound: where a Linux laptop's battery goes, how long it will last, and what
would make it last longer (measured on your machine by experiment)."""
import os

__version__ = '0.1.0'

# Before anything imports numpy: the models solve small matrices, and BLAS
# threads only contend for them (a 226×76 least squares took 27 ms with 12
# threads, 4 ms with one; a busy machine slowed the test suite 7×).
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')

"""Paths that differ between source and a frozen .exe.

Frozen: config/state live next to the .exe (writable); templates come from
_MEIPASS (read-only). Source: both are src/.
"""

import os
import sys

_SRC = os.path.dirname(os.path.abspath(__file__))


def is_frozen():
    return getattr(sys, "frozen", False)


def data_dir():
    # writable: config.json + user state
    if is_frozen():
        return os.path.dirname(sys.executable)
    return _SRC


def resource_dir():
    # read-only bundled assets (templates)
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return _SRC


def config_path():
    return os.path.join(data_dir(), "config.json")

"""Pytest path bootstrap: make the repository root importable.

Running ``python -m pytest`` from the repository root already puts the root on
``sys.path``; this file additionally guarantees the same when pytest targets
the tests from another working directory.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

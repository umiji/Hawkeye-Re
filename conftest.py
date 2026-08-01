"""Put the repository root on sys.path for the test run.

`hawkeye` is importable because it is installed (editable); `debug` is
deliberately NOT part of the wheel, so without this its tests could not
import it. Nothing else belongs here — the shared fixtures live in
tests/conftest.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

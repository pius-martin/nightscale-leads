import os
import sys

INTERP = os.path.join(os.path.dirname(__file__), ".venv", "bin", "python3")
if sys.executable != INTERP:
    os.execl(INTERP, INTERP, *sys.argv)

sys.path.insert(0, os.path.dirname(__file__))

from app import app as application  # noqa: E402,F401

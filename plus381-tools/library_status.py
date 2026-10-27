"""Plus381 Tools: library status (KiCad API plugin action)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plus381 import start  # noqa: E402

start.run("status")

"""Plus381 Tools: publish (KiCad API plugin action)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plus381 import ui  # noqa: E402

ui.main("publish")

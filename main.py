"""Launch the Maya STEP importer from Maya's Python Script Editor."""

from pathlib import Path
import sys

ROOT = str(Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


from maya_step.ui import show


if __name__ == "__main__":
    show()

import os
import sys
from pathlib import Path

# Run Qt headless so the suite works in CI and over SSH
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

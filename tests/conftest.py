"""pytest entry point: isolate the app config before any test imports core.*."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic_env  # noqa: E402,F401  (module-level setup() runs on import)

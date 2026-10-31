"""Source-checkout entry point: python run.py replay (no editable install needed)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from ott_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

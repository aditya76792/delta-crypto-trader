"""Entry point: `python -m main <command>` (see layers/automation/orchestrator.py)."""
import sys

from layers.automation.orchestrator import main

if __name__ == "__main__":
    sys.exit(main())

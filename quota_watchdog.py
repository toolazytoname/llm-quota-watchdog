#!/usr/bin/env python3
"""llm-quota-watchdog — legacy entry point (delegates to quota_watchdog package)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quota_watchdog.app import main

if __name__ == "__main__":
    main()

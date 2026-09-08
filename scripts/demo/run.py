#!/usr/bin/env python3
"""Run the offline recommendation demo with Python 3.11+, no install required."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src'))
from hidden_view_finder.server import main

if __name__ == '__main__':
    main()

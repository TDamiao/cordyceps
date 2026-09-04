#!/usr/bin/env python3
"""Simple test to verify Telegram module import."""

import sys
import os

# Add the current directory to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Try importing
try:
    from src.notifications.telegram import TelegramNotifier, TelegramConfig
    print("SUCCESS: Telegram module imports correctly")
    print(f"TelegramNotifier class: {TelegramNotifier}")
    print(f"TelegramConfig class: {TelegramConfig}")
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)
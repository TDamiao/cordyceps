#!/usr/bin/env python3
"""Test script to verify Telegram import and basic functionality."""

import asyncio
import sys
import os

# Add the current directory to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.notifications.telegram import TelegramNotifier, TelegramConfig

async def test_import():
    print("Testing Telegram import...")
    
    # Test 1: Import works
    print("✓ Import successful")
    
    # Test 2: Can create a config with empty values
    config = TelegramConfig.from_env()
    print(f"✓ Config created: enabled={config.enabled}, bot_token={'set' if config.bot_token else 'not set'}")
    
    # Test 3: Can create notifier instance
    notifier = TelegramNotifier(config=config)
    print("✓ Notifier instance created")
    
    # Test 4: Verify methods exist
    assert hasattr(notifier, 'send_message')
    assert hasattr(notifier, 'notify_startup')
    assert hasattr(notifier, 'notify_trade_open')
    assert hasattr(notifier, 'notify_trade_close')
    print("✓ Required methods exist")
    
    # Test 5: Test global functions
    from src.notifications.telegram import get_notifier, init_notifications
    global_notifier = get_notifier()
    print("✓ Global notifier function works")
    
    print("\nAll tests passed!")

if __name__ == "__main__":
    asyncio.run(test_import())
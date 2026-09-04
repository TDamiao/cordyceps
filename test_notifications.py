#!/usr/bin/env python3
"""Test script to verify notification integration."""

import sys
import os
sys.path.insert(0, 'src')

# Test imports
try:
    from src.notifications.telegram import TelegramNotifier, TelegramConfig
    from src.notifications.service import NotificationService, get_notification_service
    print("✓ Imports successful")
except ImportError as e:
    print(f"✗ Import failed: {e}")
    sys.exit(1)

# Test TelegramConfig
try:
    config = TelegramConfig(bot_token="test_token", chat_id="test_chat")
    assert config.enabled == True
    print("✓ TelegramConfig works")
except Exception as e:
    print(f"✗ TelegramConfig failed: {e}")
    sys.exit(1)

# Test NotificationService
async def test_notification_service():
    try:
        service = NotificationService()
        print("✓ NotificationService instantiated")
        
        # Test methods exist
        assert hasattr(service, 'notify_execution_failure')
        assert hasattr(service, 'notify_partial_fill')
        assert hasattr(service, 'notify_unwind_failed')
        assert hasattr(service, 'notify_circuit_breaker_triggered')
        assert hasattr(service, 'notify_daily_loss_limit_exceeded')
        assert hasattr(service, 'notify_kill_switch')
        print("✓ All notification methods exist")
        
    except Exception as e:
        print(f"✗ NotificationService test failed: {e}")
        raise

# Test Executor import
try:
    from src.execution.executor import OrderExecutor
    print("✓ OrderExecutor imported successfully")
except ImportError as e:
    print(f"✗ OrderExecutor import failed: {e}")
    sys.exit(1)

if __name__ == "__main__":
    import asyncio
    print("Testing notification integration...")
    
    # Run async test
    asyncio.run(test_notification_service())
    
    print("\nAll tests passed!")
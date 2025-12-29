"""
WebSocket client for Polymarket CLOB real-time data.

Connects to the market channel for order book updates.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.config import Endpoints
from src.utils.logging import get_logger

logger = get_logger(__name__)


class ConnectionState(Enum):
    """WebSocket connection states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


@dataclass
class WebSocketConfig:
    """Configuration for WebSocket connection."""

    url: str = Endpoints.CLOB_WS
    ping_interval: float = 30.0
    ping_timeout: float = 10.0
    reconnect_delay: float = 1.0
    max_reconnect_delay: float = 60.0
    reconnect_backoff: float = 2.0
    max_reconnect_attempts: int = 10


@dataclass
class Subscription:
    """Represents a channel subscription."""

    channel: str
    asset_ids: list[str] = field(default_factory=list)


class WebSocketClient:
    """
    Async WebSocket client for Polymarket market data.

    Handles connection management, subscriptions, and message processing.
    """

    def __init__(
        self,
        config: Optional[WebSocketConfig] = None,
        on_message: Optional[Callable[[dict], None]] = None,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ):
        """
        Initialize WebSocket client.

        Args:
            config: Connection configuration
            on_message: Callback for received messages
            on_connect: Callback when connected
            on_disconnect: Callback when disconnected
        """
        self.config = config or WebSocketConfig()
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._state = ConnectionState.DISCONNECTED
        self._subscriptions: list[Subscription] = []
        self._reconnect_attempts = 0
        self._running = False
        self._tasks: list[asyncio.Task] = []

    @property
    def state(self) -> ConnectionState:
        """Get current connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._state == ConnectionState.CONNECTED and self._ws is not None

    async def connect(self) -> None:
        """
        Establish WebSocket connection.

        Handles connection setup and starts background tasks.
        """
        if self._state == ConnectionState.CONNECTED:
            logger.debug("Already connected")
            return

        self._state = ConnectionState.CONNECTING
        self._running = True

        try:
            logger.info("Connecting to WebSocket", url=self.config.url)

            self._ws = await websockets.connect(
                self.config.url,
                ping_interval=self.config.ping_interval,
                ping_timeout=self.config.ping_timeout,
            )

            self._state = ConnectionState.CONNECTED
            self._reconnect_attempts = 0

            logger.info("WebSocket connected")

            if self.on_connect:
                self.on_connect()

            # Resubscribe to channels after reconnection
            await self._resubscribe()

        except Exception as e:
            self._state = ConnectionState.DISCONNECTED
            logger.error("Failed to connect", error=str(e))
            raise

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        self._running = False

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        if self._ws:
            await self._ws.close()
            self._ws = None

        self._state = ConnectionState.DISCONNECTED
        logger.info("WebSocket disconnected")

        if self.on_disconnect:
            self.on_disconnect()

    async def subscribe_market(self, asset_ids: list[str]) -> None:
        """
        Subscribe to market channel for given assets.

        Args:
            asset_ids: List of token IDs to subscribe to
        """
        if not asset_ids:
            logger.warning("No asset IDs provided for subscription")
            return

        subscription = Subscription(channel="market", asset_ids=asset_ids)
        self._subscriptions.append(subscription)

        if self.is_connected:
            await self._send_subscription(subscription)

    async def _send_subscription(self, subscription: Subscription) -> None:
        """Send subscription message to server."""
        if not self._ws:
            return

        message = {
            "type": subscription.channel,
            "assets_ids": subscription.asset_ids,  # Note: API uses assets_ids (plural)
        }

        await self._ws.send(json.dumps(message))
        logger.info(
            "Subscribed to channel",
            channel=subscription.channel,
            assets=len(subscription.asset_ids),
        )

    async def _resubscribe(self) -> None:
        """Resubscribe to all channels after reconnection."""
        for subscription in self._subscriptions:
            await self._send_subscription(subscription)

    async def listen(self) -> None:
        """
        Start listening for messages.

        This is the main message loop. Should be run as a task.
        """
        while self._running:
            try:
                if not self.is_connected:
                    await self.connect()

                await self._receive_loop()

            except ConnectionClosed as e:
                logger.warning("Connection closed", code=e.code, reason=e.reason)
                await self._handle_disconnect()

            except WebSocketException as e:
                logger.error("WebSocket error", error=str(e))
                await self._handle_disconnect()

            except Exception as e:
                logger.error("Unexpected error in listen loop", error=str(e))
                await self._handle_disconnect()

    async def _receive_loop(self) -> None:
        """Process incoming messages."""
        if not self._ws:
            return

        async for message in self._ws:
            try:
                data = json.loads(message)
                await self._process_message(data)
            except json.JSONDecodeError:
                logger.warning("Received invalid JSON", message=message[:100])
            except Exception as e:
                logger.error("Error processing message", error=str(e))

    async def _process_message(self, data: dict) -> None:
        """
        Process a received message.

        Args:
            data: Parsed JSON message
        """
        msg_type = data.get("event_type") or data.get("type", "unknown")

        logger.debug("Received message", type=msg_type)

        if self.on_message:
            self.on_message(data)

    async def _handle_disconnect(self) -> None:
        """Handle disconnection and attempt reconnection."""
        self._state = ConnectionState.DISCONNECTED
        self._ws = None

        if self.on_disconnect:
            self.on_disconnect()

        if not self._running:
            return

        await self._reconnect()

    async def _reconnect(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        if self._reconnect_attempts >= self.config.max_reconnect_attempts:
            logger.error(
                "Max reconnection attempts reached",
                attempts=self._reconnect_attempts,
            )
            self._running = False
            return

        self._state = ConnectionState.RECONNECTING
        self._reconnect_attempts += 1

        delay = min(
            self.config.reconnect_delay * (self.config.reconnect_backoff ** (self._reconnect_attempts - 1)),
            self.config.max_reconnect_delay,
        )

        logger.info(
            "Reconnecting",
            attempt=self._reconnect_attempts,
            delay=delay,
        )

        await asyncio.sleep(delay)

        try:
            await self.connect()
        except Exception as e:
            logger.error("Reconnection failed", error=str(e))


class MarketWebSocket:
    """
    High-level market data WebSocket manager.

    Provides a simple interface for subscribing to markets and
    receiving order book updates.
    """

    def __init__(
        self,
        on_book_update: Optional[Callable[[str, dict], None]] = None,
        on_trade: Optional[Callable[[str, dict], None]] = None,
    ):
        """
        Initialize market WebSocket.

        Args:
            on_book_update: Callback for order book updates (token_id, data)
            on_trade: Callback for trade events (token_id, data)
        """
        self.on_book_update = on_book_update
        self.on_trade = on_trade

        self._client = WebSocketClient(
            on_message=self._handle_message,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
        )
        self._subscribed_assets: set[str] = set()

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._client.is_connected

    async def start(self) -> None:
        """Start the WebSocket connection."""
        await self._client.connect()

    async def stop(self) -> None:
        """Stop the WebSocket connection."""
        await self._client.disconnect()

    async def subscribe(self, token_ids: list[str]) -> None:
        """
        Subscribe to order book updates for tokens.

        Args:
            token_ids: List of token IDs to subscribe to
        """
        new_ids = [tid for tid in token_ids if tid not in self._subscribed_assets]

        if not new_ids:
            return

        self._subscribed_assets.update(new_ids)
        await self._client.subscribe_market(new_ids)

    async def listen(self) -> None:
        """Start listening for messages (blocking)."""
        await self._client.listen()

    def _on_connect(self) -> None:
        """Handle connection event."""
        logger.info("MarketWebSocket connected")

    def _on_disconnect(self) -> None:
        """Handle disconnection event."""
        logger.warning("MarketWebSocket disconnected")

    def _handle_message(self, data: dict) -> None:
        """
        Route incoming messages to appropriate handlers.

        Args:
            data: Parsed message data
        """
        event_type = data.get("event_type", "")
        asset_id = data.get("asset_id", "")

        if event_type == "book":
            if self.on_book_update:
                self.on_book_update(asset_id, data)

        elif event_type == "price_change":
            if self.on_book_update:
                self.on_book_update(asset_id, data)

        elif event_type == "trade":
            if self.on_trade:
                self.on_trade(asset_id, data)

        elif event_type == "last_trade_price":
            # Ignore last trade price updates
            pass

        else:
            logger.debug("Unknown event type", event_type=event_type)

"""
WebSocket client for Polymarket CLOB real-time data.

Connects to the market channel for order book updates.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

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
    """Represents a market-channel subscription."""

    channel: str
    asset_ids: list[str] = field(default_factory=list)


class WebSocketClient:
    """Async WebSocket client for Polymarket market data."""

    def __init__(
        self,
        config: WebSocketConfig | None = None,
        on_message: Callable[[dict], None] | None = None,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ):
        self.config = config or WebSocketConfig()
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

        self._ws: Any | None = None
        self._state = ConnectionState.DISCONNECTED
        self._subscriptions: list[Subscription] = []
        self._reconnect_attempts = 0
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._market_subscription_initialized = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED and self._ws is not None

    async def connect(self) -> None:
        if self._state == ConnectionState.CONNECTED:
            logger.debug("Already connected")
            return

        self._state = ConnectionState.CONNECTING
        self._running = True
        self._market_subscription_initialized = False

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

            await self._resubscribe()
        except Exception as e:
            self._state = ConnectionState.DISCONNECTED
            logger.error("Failed to connect", error=str(e))
            raise

    async def disconnect(self) -> None:
        self._running = False

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
        self._market_subscription_initialized = False
        logger.info("WebSocket disconnected")

        if self.on_disconnect:
            self.on_disconnect()

    async def subscribe_market(self, asset_ids: list[str]) -> None:
        if not asset_ids:
            logger.warning("No asset IDs provided for subscription")
            return

        subscription = Subscription(channel="market", asset_ids=asset_ids)
        self._subscriptions.append(subscription)

        if self.is_connected:
            await self._send_subscription(subscription)

    async def _send_subscription(self, subscription: Subscription) -> None:
        """Send the current Polymarket market-channel subscription format.

        Initial market subscription:
            {"assets_ids": [...], "type": "market"}

        Subsequent subscription updates:
            {"operation": "subscribe", "assets_ids": [...]}
        """
        if not self._ws:
            return

        if subscription.channel != "market":
            logger.warning("Unsupported websocket channel", channel=subscription.channel)
            return

        if not self._market_subscription_initialized:
            message = {
                "assets_ids": subscription.asset_ids,
                "type": "market",
            }
            self._market_subscription_initialized = True
            operation = "initial_subscribe"
        else:
            message = {
                "operation": "subscribe",
                "assets_ids": subscription.asset_ids,
            }
            operation = "subscribe"

        await self._ws.send(json.dumps(message))
        logger.info(
            "Subscribed to channel",
            channel=subscription.channel,
            operation=operation,
            assets=len(subscription.asset_ids),
        )

    async def _resubscribe(self) -> None:
        if not self._subscriptions:
            return

        self._market_subscription_initialized = False
        for subscription in self._subscriptions:
            await self._send_subscription(subscription)

    async def listen(self) -> None:
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
        if not self._ws:
            return

        async for message in self._ws:
            # Polymarket may send simple text control/error responses such as
            # INVALID OPERATION. Handle them explicitly instead of calling them
            # malformed JSON.
            if isinstance(message, str):
                stripped = message.strip()
                if stripped and stripped[0] not in '[{"':
                    logger.warning("Received websocket text response", message=stripped[:100])
                    continue

            try:
                data = json.loads(message)
                await self._process_message(data)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON websocket message", message=str(message)[:100])
            except Exception as e:
                logger.error("Error processing message", error=str(e))

    async def _process_message(self, data: Any) -> None:
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and self.on_message:
                    self.on_message(item)
            return

        if isinstance(data, str):
            logger.warning("Received string message", message=data[:100])
            return

        if not isinstance(data, dict):
            logger.warning("Unexpected message type", type=type(data).__name__)
            return

        msg_type = data.get("event_type") or data.get("type", "unknown")
        logger.debug("Received message", type=msg_type)

        if self.on_message:
            self.on_message(data)

    async def _handle_disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._ws = None
        self._market_subscription_initialized = False

        if self.on_disconnect:
            self.on_disconnect()

        if not self._running:
            return

        await self._reconnect()

    async def _reconnect(self) -> None:
        if self._reconnect_attempts >= self.config.max_reconnect_attempts:
            logger.error("Max reconnection attempts reached", attempts=self._reconnect_attempts)
            self._running = False
            return

        self._state = ConnectionState.RECONNECTING
        self._reconnect_attempts += 1
        delay = min(
            self.config.reconnect_delay
            * (self.config.reconnect_backoff ** (self._reconnect_attempts - 1)),
            self.config.max_reconnect_delay,
        )

        logger.info("Reconnecting", attempt=self._reconnect_attempts, delay=delay)
        await asyncio.sleep(delay)

        try:
            await self.connect()
        except Exception as e:
            logger.error("Reconnection failed", error=str(e))


class MarketWebSocket:
    """High-level market data WebSocket manager."""

    def __init__(
        self,
        on_book_update: Callable[[str, dict], None] | None = None,
        on_trade: Callable[[str, dict], None] | None = None,
    ):
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
        return self._client.is_connected

    async def start(self) -> None:
        await self._client.connect()

    async def stop(self) -> None:
        await self._client.disconnect()

    async def subscribe(self, token_ids: list[str]) -> None:
        new_ids = [tid for tid in token_ids if tid not in self._subscribed_assets]
        if not new_ids:
            return

        self._subscribed_assets.update(new_ids)
        await self._client.subscribe_market(new_ids)

    async def listen(self) -> None:
        await self._client.listen()

    def _on_connect(self) -> None:
        logger.info("MarketWebSocket connected")

    def _on_disconnect(self) -> None:
        logger.warning("MarketWebSocket disconnected")

    def _handle_message(self, data: Any) -> None:
        if not isinstance(data, dict):
            return

        event_type = data.get("event_type", "")
        asset_id = data.get("asset_id", "")

        if not event_type and "market" in data and "bids" in data:
            asset_id = data.get("asset_id", "")
            if self.on_book_update and asset_id:
                self.on_book_update(asset_id, data)
            return

        if event_type == "book":
            if self.on_book_update:
                self.on_book_update(asset_id, data)
        elif event_type == "price_change":
            if self.on_book_update:
                for change in data.get("price_changes", []):
                    change_asset_id = change.get("asset_id", "")
                    if change_asset_id:
                        self.on_book_update(
                            change_asset_id,
                            {"changes": [change], "timestamp": data.get("timestamp")},
                        )
        elif event_type == "trade":
            if self.on_trade:
                self.on_trade(asset_id, data)
        elif event_type == "last_trade_price":
            pass
        else:
            logger.debug("Unknown event type", event_type=event_type, keys=list(data.keys())[:5])

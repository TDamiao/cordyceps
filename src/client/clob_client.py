"""
CLOB Client wrapper for Polymarket API.

Provides a high-level interface for trading operations.
"""

from decimal import Decimal
from typing import Any

from py_clob_client_v2 import (
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    OrderArgs,
    OrderPayload,
    Side,
)
from py_clob_client_v2 import (
    OrderType as ClobOrderType,
)

from src.client.auth import AuthenticatedClient, authenticate
from src.client.models import (
    OrderBook,
    OrderBookLevel,
    OrderSide,
    OrderType,
)
from src.config import Endpoints, get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


class PolymarketClient:
    """
    High-level client for Polymarket CLOB operations.

    In paper mode the client can run public-only, without a wallet or API
    credentials. Live mode uses the authenticated py-clob-client flow.
    """

    def __init__(
        self,
        auth_client: AuthenticatedClient | None = None,
        *,
        public_only: bool = False,
    ):
        """
        Initialize the Polymarket client.

        Args:
            auth_client: Pre-authenticated client for live trading.
            public_only: Use unauthenticated public CLOB endpoints only.
        """
        self._settings = get_settings()
        self._auth: AuthenticatedClient | None = None
        self._public_only = public_only

        if public_only:
            self._client = ClobClient(host=Endpoints.CLOB_HOST, chain_id=self._settings.chain_id)
            logger.info("PolymarketClient initialized in public-only mode")
            return

        if auth_client is None:
            auth_client = authenticate()

        self._auth = auth_client
        self._client = auth_client.client

        logger.info(
            "PolymarketClient initialized",
            eoa=self._auth.eoa_address,
            proxy=self._auth.proxy_address,
        )

    @property
    def client(self) -> ClobClient:
        """Access the underlying ClobClient."""
        return self._client

    @property
    def eoa_address(self) -> str | None:
        """Get the EOA (signing) address, if authenticated."""
        return self._auth.eoa_address if self._auth else None

    @property
    def proxy_address(self) -> str | None:
        """Get the proxy (trading) address, if authenticated."""
        return self._auth.proxy_address if self._auth else None

    # =========================================================================
    # Market Data
    # =========================================================================

    def get_order_book(self, token_id: str) -> OrderBook:
        """Fetch the order book for a token."""
        try:
            raw = self._client.get_order_book(token_id)

            bids = [
                OrderBookLevel(
                    price=Decimal(str(b.price)),
                    size=Decimal(str(b.size)),
                )
                for b in (raw.bids or [])
            ]

            asks = [
                OrderBookLevel(
                    price=Decimal(str(a.price)),
                    size=Decimal(str(a.size)),
                )
                for a in (raw.asks or [])
            ]

            return OrderBook(
                token_id=token_id,
                bids=sorted(bids, key=lambda x: x.price, reverse=True),
                asks=sorted(asks, key=lambda x: x.price),
                timestamp=raw.timestamp if hasattr(raw, "timestamp") else None,
            )

        except Exception as e:
            logger.error("Failed to fetch order book", token_id=token_id, error=str(e))
            return OrderBook(token_id=token_id)

    def get_order_books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        """Fetch order books for multiple tokens."""
        result = {}
        for token_id in token_ids:
            result[token_id] = self.get_order_book(token_id)
        return result

    def get_price(self, token_id: str) -> Decimal | None:
        """Get the current midpoint for a token."""
        try:
            raw = self._client.get_midpoint(token_id)
            mid = raw.get("mid") if isinstance(raw, dict) else getattr(raw, "mid", None)
            return Decimal(str(mid)) if mid is not None else None
        except Exception as e:
            logger.warning("Failed to get price", token_id=token_id, error=str(e))
            return None

    # =========================================================================
    # Order Management
    # =========================================================================

    def _require_authenticated(self) -> None:
        if self._public_only or self._auth is None:
            raise RuntimeError("Authenticated CLOB client required for live trading")

    def create_order(
        self,
        token_id: str,
        side: OrderSide,
        price: Decimal,
        size: Decimal,
        order_type: OrderType = OrderType.GTC,
    ) -> dict[str, Any]:
        """Create and submit an order."""
        if self._settings.trading_mode == "paper" or self._settings.dry_run:
            logger.info(
                "PAPER/DRY RUN - Order not submitted",
                token_id=token_id,
                side=side.value,
                price=str(price),
                size=str(size),
            )
            return {
                "success": True,
                "orderID": "paper_order_id",
                "status": "matched",
                "makingAmount": str(int(size * Decimal("1000000"))),
                "takingAmount": str(int(size * price * Decimal("1000000"))),
            }

        self._require_authenticated()

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=Side.BUY if side == OrderSide.BUY else Side.SELL,
            )
            response = self._client.create_and_post_order(
                order_args=order_args,
                order_type=getattr(ClobOrderType, order_type.value),
            )
            response = response or {}

            logger.info(
                "Order submitted",
                order_id=response.get("orderID"),
                token_id=token_id,
                side=side.value,
                price=str(price),
                size=str(size),
            )
            return response

        except Exception as e:
            logger.error(
                "Order submission failed",
                token_id=token_id,
                error=str(e),
            )
            return {"success": False, "status": "failed", "errorMsg": str(e)}

    def create_fok_order(
        self,
        token_id: str,
        side: OrderSide,
        price: Decimal,
        size: Decimal,
    ) -> dict[str, Any]:
        """Create a Fill-or-Kill order."""
        return self.create_order(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type=OrderType.FOK,
        )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        self._require_authenticated()
        try:
            self._client.cancel_order(OrderPayload(orderID=order_id))
            logger.info("Order cancelled", order_id=order_id)
            return True
        except Exception as e:
            logger.error("Failed to cancel order", order_id=order_id, error=str(e))
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        self._require_authenticated()
        try:
            self._client.cancel_all()
            logger.info("All orders cancelled")
            return True
        except Exception as e:
            logger.error("Failed to cancel all orders", error=str(e))
            return False

    # =========================================================================
    # Account / Positions
    # =========================================================================

    def get_open_orders(self) -> list[dict[str, Any]]:
        """Get all open orders."""
        self._require_authenticated()
        try:
            return self._client.get_open_orders() or []
        except Exception as e:
            logger.error("Failed to get open orders", error=str(e))
            return []

    def get_trades(self) -> list[dict[str, Any]]:
        """Get recent trades."""
        self._require_authenticated()
        try:
            return self._client.get_trades() or []
        except Exception as e:
            logger.error("Failed to get trades", error=str(e))
            return []

    # =========================================================================
    # Utility
    # =========================================================================

    def get_server_time(self) -> int:
        """Get current server timestamp."""
        return self._client.get_server_time()

    def get_order(self, order_id: str) -> dict[str, Any]:
        self._require_authenticated()
        return self._client.get_order(order_id) or {}

    def get_balance_allowance(self, token_id: str | None = None) -> dict[str, Any]:
        self._require_authenticated()
        asset_type = AssetType.CONDITIONAL if token_id else AssetType.COLLATERAL
        params = BalanceAllowanceParams(asset_type=asset_type, token_id=token_id)
        try:
            res = self._client.get_balance_allowance(params)
            if res and res.get("balance") is not None:
                try:
                    bal = float(res.get("balance", 0))
                    if bal == 0:
                        updated = self._client.update_balance_allowance(params)
                        if updated and updated.get("balance") is not None:
                            return updated
                except Exception:
                    pass
                return res
        except Exception as e:
            logger.warning("get_balance_allowance failed, trying update_balance_allowance", error=str(e))
            try:
                return self._client.update_balance_allowance(params) or {}
            except Exception:
                pass
        return {}

    def get_clob_market_info(self, condition_id: str) -> Any:
        return self._client.get_clob_market_info(condition_id)

    def is_connected(self) -> bool:
        """Check if client can reach the server."""
        try:
            self.get_server_time()
            return True
        except Exception:
            return False

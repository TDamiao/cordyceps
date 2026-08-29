import asyncio
import os
import sys
from decimal import Decimal

sys.path.append(os.getcwd())

from src.client import PolymarketClient
from src.config import get_settings
from src.markets import MarketFetcher


async def monitor_spreads():
    settings = get_settings()
    print(f"📡 Connecting to Polygon RPC: {settings.polygon_rpc_url}")
    print(f"🔎 Threshold: {settings.min_profit_threshold*100}%")

    # Initialize components
    # Don't pass settings directly, let it auto-authenticate
    client = PolymarketClient()
    market_fetcher = MarketFetcher()

    print("⏳ Fetching active markets...")
    markets = await market_fetcher.fetch_markets(limit=20)
    print(f"✅ Fetched {len(markets)} markets.")

    print("\n📊 Current Spreads (Snapshot):")
    print(
        f"{'Market ID':<15} | {'Name':<40} | {'Ask Sum':<10} | {'Bid Sum':<10} | {'Spread %':<10}"
    )
    print("-" * 100)

    for market in markets:
        try:
            # Get token IDs
            token_ids = [t.token_id for t in market.tokens]

            # Fetch orderbooks using CLIENT
            # client.get_order_books returns {token_id: OrderBook}
            orderbooks = client.get_order_books(token_ids)

            # Calculate Sums
            ask_sum = Decimal("0")
            bid_sum = Decimal("0")
            valid = True

            for token_id in token_ids:
                if token_id not in orderbooks:
                    valid = False
                    break
                book = orderbooks[token_id]
                if not book.best_ask or not book.best_bid:
                    valid = False
                    break
                ask_sum += book.best_ask.price
                bid_sum += book.best_bid.price

            if not valid:
                continue

            # Spread = 1 - AskSum (for BuySet) or BidSum - 1 (for SellSet)
            # Negative spread means no arb. Positive means ARB!
            buy_spread = (Decimal("1") - ask_sum) * 100

            # Highlight if profitable
            prefix = "🟢 " if buy_spread > 0 else "🔴 "

            print(
                f"{prefix}{market.condition_id[:10]}... | {market.question[:38]:<40} | {ask_sum:.4f}     | {bid_sum:.4f}     | {buy_spread:+.2f}%"
            )

        except Exception as e:
            print(f"Error fetching {market.condition_id}: {e}")

    print("\n✅ Scan Complete.")


if __name__ == "__main__":
    asyncio.run(monitor_spreads())

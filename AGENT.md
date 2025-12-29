# **Project Title: Automated Microstructure Arbitrage Engine on Gnosis CTF (Polymarket)**

---

## **1. Executive Summary**

The objective is to deploy a low-latency trading system that exploits structural inefficiencies in Polymarket's Order Book (CLOB). Unlike directional betting, this system utilizes **Atomic Arbitrage** (Risk-Free) and **Statistical Arbitrage** strategies. The system will leverage the Gnosis Conditional Token Framework (CTF) to mint and merge outcome shares, ensuring profitability through the mathematical "Unity Constraint" (Sum of Probabilities = 1.0).

---

## **2. System Architecture**

The system consists of three asynchronous subsystems operating in a continuous loop.

### **High-Level Data Flow:**

```
WebSocket Feed (CLOB) → state_manager (Local Orderbook) → arb_engine (Math) → execution_client (Batch Orders) → settlement_agent (Merge/Redeem)
```

1. **Market Observer (The Eye):**
   * **Input:** Connects to Polymarket CLOB WebSocket (`wss://ws-clob.polymarket.com/`).
   * **Task:** Maintains a local, real-time mirror of the order book for target markets.
   * **Latency Target:** < 50ms processing time.

2. **Arbitrage Engine (The Brain):**
   * **Logic:** Scans for violations of the Unity Constraint.
   * **Equation:** `Σ P(outcome_i) = 1.0`
   * **Output:** Generates a signal containing `market_id`, `outcome_ids`, `size`, and `limit_prices`.

3. **Execution & Settlement (The Hand):**
   * **Trade:** Sends **Fill-or-Kill (FOK)** batch orders to the CLOB REST API.
   * **Recycle:** Monitors wallet balances. When a complete set of outcomes (e.g., Yes + No) is acquired, it triggers the `mergePositions` function via the Relayer to convert shares back into USDC collateral.

---

## **3. Core Strategies & Mathematics**

### **Strategy A: Atomic Unity Arbitrage (The "Discounted Dollar")**

This is the primary profit driver. In a prediction market, the sum of all mutually exclusive outcomes must equal $1.00.

* **Scenario:** A market has 3 outcomes: A, B, C.
* **Opportunity:**
  * Ask Price A: $0.30
  * Ask Price B: $0.30
  * Ask Price C: $0.30
  * **Total Cost:** $0.90

* **Action:** Buy 1 share of A, B, and C.
* **Result:** You hold a "Complete Set". No matter who wins, one share becomes $1.00, and the others become $0. The payout is guaranteed $1.00.
* **Net Profit:** $1.00 - $0.90 = $0.10 (11.1% ROI).

### **Strategy B: Synthetic Minting (Liquidity Provision)**

If the market is over-enthusiastic (`Σ P > 1.0`), the bot acts as a maker.

* **Action:**
  1. Send USDC to the CTF Contract to `splitPosition` (Mint 1 Set of A+B+C for $1.00).
  2. Sell A, B, and C into the bids at $1.05 total.
  3. **Profit:** $0.05 Risk-Free.

---

## **4. Implementation Details**

### **4.1 Prerequisites**

* **Proxy Wallet:** You must interact via a Gnosis Safe Proxy (standard for Polymarket) to utilize the Gasless Relayer.
* **API Keys:** Generated from the Polymarket dashboard (requires L1/L2 headers for signing).
* **Funding:** USDC (Polygon) deposited into the Proxy Address.

### **4.2 Tech Stack**

* **Language:** Python 3.9+ (Recommended for library support) or Rust (for maximum speed).
* **Libraries:** `py-clob-client` (Official CLOB wrapper), `web3.py` (Smart Contract interaction), `aiohttp` (Async requests).

### **4.3 Code Structure (Python Blueprint)**

**Step 1: Setup & Authentication**

Initialize the client with your EOA key (Owner) and the Proxy Address (Funder).

```python
import os
from py_clob_client.client import ClobClient

# Configuration
HOST = "https://clob.polymarket.com"
KEY = os.getenv("PRIVATE_KEY")        # Your EOA Private Key
FUNDER = os.getenv("PROXY_ADDRESS")   # Your Polymarket Proxy Address
CHAIN_ID = 137                        # Polygon Mainnet

# Initialize Client
client = ClobClient(
    HOST, 
    key=KEY, 
    chain_id=CHAIN_ID, 
    signature_type=1, # 1 = Proxy Wallet (Magic/Polymarket standard)
    funder=FUNDER
)
client.set_api_creds(client.create_or_derive_api_creds())
```

**Step 2: The Arbitrage Loop (Pseudo-code)**

This loop checks for the "Discounted Dollar" opportunity.

```python
async def find_and_execute_arb(market_outcomes):
    """
    market_outcomes: List of token_ids for a single event (e.g., [Yes_id, No_id])
    """
    # 1. Fetch Orderbooks (Optimize with WebSocket in production)
    orderbooks = [await client.get_order_book(tid) for tid in market_outcomes]
    
    # 2. Calculate Total Buy Price
    # Get the best 'ask' (lowest sell price) for each outcome
    best_asks = [ob.asks[0] for ob in orderbooks if ob.asks]
    
    if len(best_asks) != len(market_outcomes):
        return # Liquidity missing in one leg
        
    total_price = sum(float(ask.price) for ask in best_asks)
    
    # 3. Check for Arbitrage (accounting for 0.01% potential taker fee)
    # Target: Cost < 0.99 USDC
    if total_price < 0.99: 
        print(f"Opportunity found! Cost: {total_price}")
        
        # 4. Execute Batch Order
        # We must buy the SAME quantity for all legs to be perfectly hedged.
        min_size = min(float(ask.size) for ask in best_asks)
        
        orders = []
        for token_id, ask in zip(market_outcomes, best_asks):
            orders.append({
                "token_id": token_id,
                "price": ask.price,
                "size": min_size,
                "side": "BUY",
                "order_type": "FOK" # Fill-Or-Kill is critical to prevent partial fills
            })
            
        # 5. Fire Request
        resp = await client.post_batch_orders(orders)
        print(f"Executed: {resp}")
```

**Step 3: Settlement (Capital Recycling)**

Once you bought "Yes" and "No", your capital is locked. You must "Merge" them back to USDC to trade again. This is done via the **CTF Contract** or **Relayer**.

* **Contract:** `Gnosis Conditional Token Framework`
* **Function:** `mergePositions`
* **Method:** Use the `Relayer` API to execute this transaction gaslessly.

```python
# Note: Requires 'polymarket-builder-relayer-client' logic
def merge_positions(condition_id, amount):
    # Encoding the mergePositions call for the CTF Contract
    # Partition [1, 2] is standard for Binary (Yes/No) markets
    merge_payload = ctf_contract.encodeABI(
        fn_name="mergePositions",
        args=[
            USDC_ADDRESS,    # Collateral token
            PARENT_COLLECTION_ID,  # Usually bytes32(0)
            condition_id,    # From the market
            [1, 2],          # Partition bitmasks for Yes/No
            amount           # Amount to merge
        ]
    )
    
    # Send to Relayer (Polymarket pays the gas)
    relayer.execute(to=CTF_ADDRESS, data=merge_payload)
```

---

## **5. Operational Risks & Nuances**

1. **Fees:**
   * Historically, Polymarket had no fees. However, documentation indicates the introduction of a **0.01% Taker Fee** (1 basis point) for aggressive orders.
   * **Impact:** Your arbitrage threshold must account for this.
   * *Formula:* `Target_Entry_Price = 1.00 - (2 * Fee_Rate) - Desired_Profit`.

2. **NegRisk (Negative Risk) Markets:**
   * For markets with 3+ outcomes (e.g., Elections), Polymarket often uses a "Negative Risk" structure.
   * **Implication:** Buying "No" on Candidate A is computationally equivalent to buying "Yes" on "Rest of Field".
   * **Advantage:** You can arbitrage between the specific `Yes` prices and the implicit probability derived from `No` prices using the `NegRiskAdapter` contract.

3. **Rate Limits:**
   * API Limits are approx. 100-300 requests/10s.
   * **Solution:** Use WebSockets for reading data and reserve REST API bandwidth strictly for order execution.

4. **Race Conditions:**
   * This is a "Winner Takes All" game. If another bot sees the arb 1ms before you, your FOK order will fail.
   * **Optimization:** Colocate your server in **AWS us-east-1** (N. Virginia), which is closest to the Polygon infrastructure and Polymarket relays.

---

## **6. Deployment Checklist**

- [ ] **Fund Proxy:** Ensure USDC is on Polygon chain in the Proxy wallet.
- [ ] **Approve Tokens:** The Proxy must `approve` the CTF Exchange to spend your USDC.
- [ ] **Market Mapping:** Build a map of `market_slug` → `condition_id` → `token_ids`.
- [ ] **Dry Run:** Run the `Observer` module to log opportunities without executing trades to verify spread calculations.
- [ ] **GCP Deployment:** Deploy on GCP using asia-east2 (Hong Kong) as the region.

---

This blueprint provides the exact logic used by top Quant/HFT firms in the space. The edge lies in the speed of the `Observer` loop and the efficiency of the `Settlement` recycling.

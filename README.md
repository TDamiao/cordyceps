# Cordyceps

Bot de arbitragem binária para o CLOB da Polymarket, com market data real, paper trading, painel operacional e um caminho de live trading que permanece bloqueado até validação e arm manual.

Implementa duas estratégias complementares:

1. **Unity Arbitrage** — Exploit discrepâncias de preço onde Σ(probabilidades) ≠ 1.0
2. **Favorite Compounding** — Compra de favoritos (85-98¢) próximos à resolução com 88-92% de win rate

## Segurança primeiro

O processo sempre inicia **disarmed**. `TRADING_MODE=live_test` e `LIVE_TRADING_ENABLED=true` apenas tornam o processo elegível para o pre-flight; nenhuma ordem é enviada antes de:

1. `GET /api/readiness` retornar `ready=true`;
2. geoblock oficial permitir trading;
3. kill switch estar desligado;
4. o operador autenticar no painel e digitar `CORDYCEPS LIVE` em **Arm live test**.

Todo restart volta a `armed=false`. Nunca habilite `live` sem passar pelo readiness e validar primeiro o perfil `live_test`.

## Modos

| Modo | Comportamento |
|---|---|
| `paper` | Market data real, fills simulados, sem wallet ou private key obrigatória. |
| `live_test` | Capital real com defaults de US$ 1 por trade, uma execução por vez, readiness e arm manual. |
| `live` | Modo futuro com as mesmas proteções; não é ativado automaticamente. |

O collateral do CLOB V2 é **pUSD**. O painel conserva os rótulos "USDC" onde eles são familiares ao operador, mas balance e allowance do CLOB são lidos em pUSD. O projeto usa `py-clob-client-v2>=1.1.0`; o client legado foi removido após a migração oficial de abril de 2026.

## Estratégias & Benchmarks

### 1. Unity Arbitrage

**Conceito**: Exploit quando Σ(preços das outcomes) < $1.00 ou > $1.00, dependendo da oportunidade.

**Benchmarks Históricos** (Datawallet, 2024-2026):
- Win rate: ~100% (quando executado corretamente)
- Edge médio: 0.5-2% (após taxas)
- Tempo de execução: <1 segundo
- Frequência: 0-5 oportunidades/dia
- Sensibilidade a taxas: **Muito Alta** (fees em 50/50 comem ~70% do edge)

**Quando procurar**:
- Mercados líquidos (>$10k volume)
- Não correlacionados (evita movimentos simultâneos)
- Preferir early-stage (antes que arbitrageurs profissionais equilibrem)

**Limitações**:
- Raro em mercados maduros post-Mar-2026
- Competição feroz com outros bots
- Requer latenicy baixa

### 2. Favorite Compounding

**Conceito**: Compra de resultados de alta probabilidade (85-98¢) próximos à resolução (<72h), esperando resolução a $1.00.

**Benchmarks Históricos** (Datawallet, 2024-2026):
- Win rate: 88-92%
- Retorno médio/trade: 4.2-5.8% (após taxas)
- Sensibilidade a taxas: **Muito Baixa** (fee curve mínima em extremos)
- Sharpe ratio: 1.8-2.3 (muito bom para trading discreto)
- Max drawdown (100 trades): 8-12%
- Trades/dia: 3-8 (depende do calendário)
- Projeção mensal (banca $1k, 15% exposição): 12.8-19.2% retorno líquido

**Quando procurar**:
- Eleições, decisões de taxa de juros, resultados esportivos
- Análise fundamental clara (probabilidade real >> preço)
- <72h até resolução (urgência preserva probabilidade)

**Vantagens sobre arbitrage**:
- Menos sensível a taxas (fee curve é favorável a extremos)
- Win rate mais consistente
- Psicologia melhor (poucos stops losses)
- Capital mais eficiente

**Limitações**:
- Requer análise fundamental (não é puro modelo matemático)
- Eventos Black Swan possíveis (mas raros)
- Depende de calendário de eventos

## Arquitetura

```text
Gamma API → Market Fetcher → CLOB WebSocket → State Manager
                                             ↓
PostgreSQL ← Opportunity Log ← Fee-aware VWAP Engine
                                             ↓
Dashboard ← Readiness/Risk ← Execution State Machine
                                             ↓
                          FOK legs → hedge/unwind → circuit breaker
```

As fees são consultadas por `condition_id` em `GET /clob-markets/{condition_id}` e calculadas pela curva oficial:

```text
fee = shares × rate × (price × (1 - price)) ^ exponent
```

Se os parâmetros não puderem ser obtidos, o engine usa uma curva conservadora explicitamente marcada como `fallback`; fee zero nunca é assumida silenciosamente.

## Rodar localmente

```bash
cp .env.example .env
# Defina ADMIN_TOKEN e mantenha TRADING_MODE=paper.
docker compose up -d --build
curl http://localhost:8000/health
```

Abra `http://localhost:8000/`, informe `ADMIN_TOKEN` e use o dashboard. `/health` e `/status` são públicos e sanitizados. `/api/*`, controles e configurações exigem sessão HTTP-only ou `Authorization: Bearer ***`.

## Profile "Live Test - $10 Wallet"

```dotenv
MAX_TRADE_USD=1
MAX_TOTAL_EXPOSURE_USD=2
MAX_DAILY_LOSS_USD=1
MAX_OPEN_TRADES=1
MAX_LEG_IMBALANCE_USD=1
MIN_NET_EDGE=0.01
MIN_NET_PROFIT_USD=0.01
MAX_SLIPPAGE_PCT=0.005
ORDERBOOK_STALE_MS=3000
CIRCUIT_BREAKER_FAILURE_THRESHOLD=3
CIRCUIT_BREAKER_COOLDOWN_MINUTES=15
LEG_TIMEOUT_MS=2000

# --- Favorite Compounding ---
ENABLE_FAVORITE_STRATEGY=true
MIN_FAVORITE_PROBABILITY=0.90
MIN_FAVORITE_PRICE=0.85
MAX_FAVORITE_PRICE=0.98
MIN_FAVORITE_SIZE_USD=5.0
FAVORITE_TAKE_PROFIT=0.97
FAVORITE_STOP_LOSS=0.80
MAX_FAVORITE_EXPOSURE_PCT=0.30
FAVORITE_KELLY_FRACTION=0.25

# --- Telegram Notifications ---
TELEGRAM_BOT_TOKEN=seu_token_aqui
TELEGRAM_CHAT_ID=seu_chat_id_aqui
```

US$ 10 é o saldo da wallet, não o tamanho de uma operação. O profile limita cada oportunidade a US$ 1 e exposição total a US$ 2. Esses parâmetros e os demais itens de Risk & Strategy são validados e persistidos em `runtime_config`; alterações no painel não exigem restart.

## Variáveis de ambiente

| Variável | Default | Uso |
|---|---:|---|
| `TRADING_MODE` | `paper` | `paper`, `live_test` ou `live`. |
| `LIVE_TRADING_ENABLED` | `false` | Gate do servidor para qualquer modo real. |
| `DRY_RUN` | `true` | Deve ser `false` para o readiness live. |
| `ADMIN_TOKEN` | vazio | Obrigatório para painel e APIs administrativas. |
| `PRIVATE_KEY` | vazio | Signer EOA; nunca sai do servidor. |
| `PROXY_ADDRESS` | vazio | Funder/proxy/deposit wallet. |
| `SIGNATURE_TYPE` | `1` | Tipo da wallet: confirme conforme a conta Polymarket. |
| `CLOB_API_KEY` | vazio | Credencial L2 opcional; pode ser derivada. |
| `CLOB_API_SECRET` | vazio | Segredo L2, somente environment. |
| `CLOB_API_PASSPHRASE` | vazio | Passphrase L2, somente environment. |
| `DATABASE_URL` | SQLite local | Use PostgreSQL no Dokploy. |
| `POLYGON_RPC_URL` | RPC público | RPC Polygon usado no readiness. |
| `KILL_SWITCH` | `false` | Gate inicial; o painel mantém estado em memória. |
| `MARKET_LIMIT` | `50` | Mercados monitorados. |
| `SCAN_INTERVAL_SECONDS` | `60` | Intervalo do scanner. |
| `ENABLE_FAVORITE_STRATEGY` | `false` | Ativa Favorite Compounding (disabled por default). |
| `TELEGRAM_BOT_TOKEN` | vazio | Token do bot Telegram para notificações. |
| `TELEGRAM_CHAT_ID` | vazio | Chat ID para notificações (seu ID de usuário Telegram). |

Veja [.env.example](.env.example) para todos os limites. Segredos não são retornados por nenhuma API, armazenados no banco ou insertos no HTML.

## Readiness

`GET /api/readiness?refresh=true` verifica sem negociar: PostgreSQL, Gamma, CLOB, WebSocket, books, wallet, private key, proxy, autenticação L2, Polygon RPC, balance pUSD, allowances collateral/CTF, geoblock, kill switch, risk config, circuit breaker, `LIVE_TRADING_ENABLED` e `DRY_RUN`.

O geoblock usa exclusivamente `GET https://polymarket.com/api/geoblock`, com cache de 5 minutos e comportamento fail-closed. Não existe VPN, proxy de evasão ou bypass geográfico no projeto. Paper continua ativo quando live está bloqueado.

## Execução e leg risk

Cada execução percorre `DETECTED → VALIDATING → SUBMITTING`, podendo seguir para `PARTIAL → HEDGING`, e termina em `COMPLETED`, `ABORTED` ou `FAILED`. Antes de enviar, books, VWAP, liquidez, slippage, fees, edge e risco são recalculados. Um `orderID` não é considerado fill: apenas `status=matched` do CLOB V2 conta como FOK executada.

Se uma perna falhar, existe uma única tentativa conservadora de completar a outra perna, seguida de uma única tentativa de unwind. O incidente aciona cooldown/circuit breaker. Unwind falho ativa kill switch e mostra `EXPOSURE REQUIRES ATTENTION`.

## Banco

A inicialização idempotente cria, sem apagar dados: `runtime_config`, `opportunities`, `executions`, `execution_legs`, `paper_trades`, `risk_events` e `system_events`, preservando também as tabelas legadas `trades` e `positions`. Colunas novas de `opportunities` são adicionadas via migration aditiva.

## Testes

```bash
ruff check src tests
black --check src tests
pytest -q
```

Todos os testes de segurança mockam rede; CI não depende da Polymarket estar disponível.

## Notificações Telegram

Quando configurado com `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`, o bot envia notificações em tempo real:

- ✅ Startup/shutdown do bot
- 📊 Oportunidades de arbitrage detectadas
- 🎯 Posições Favorite Compounding abertas/fechadas
- 💰 P&L em tempo real
- 🛑 Eventos de risco (kill switch, circuit breaker, stop loss)
- 📅 Resumo diário de trades
- 🔴 Erros e exceções

Todas as notificações são HTML-formatadas e incluem timestamps em PT-BR.

## Referências oficiais

- [CLOB V2 migration](https://docs.polymarket.com/v2-migration)
- [Fees](https://docs.polymarket.com/trading/fees)
- [Geographic restrictions](https://docs.polymarket.com/api-reference/geoblock)
- [Place orders](https://docs.polymarket.com/api-reference/trade/post-a-new-order)
- [Contracts](https://docs.polymarket.com/resources/contracts)

## Documentação de Deploy

Veja [docs/DOCKPLOY.md](docs/DOCKPLOY.md) para instruções completas de deploy no Dokploy.

## Aviso

Software experimental. Arbitragem e recuperação de pernas podem gerar perdas. A existência de `ready=true` reduz riscos operacionais conhecidos, mas não garante execução ou lucro. Benchmarks históricos são baseados em dados 2024-2026 da Datawallet e Laika Labs; resultados futuros podem diferir significativamente.

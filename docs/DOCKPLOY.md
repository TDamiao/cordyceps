# Deploy no Dokploy

O deploy continua composto apenas por Cordyceps + PostgreSQL, porta 8000 e healthcheck `/health`. Não há Redis, Kubernetes ou serviço pago obrigatório.

## 1. Aplicação

- Source: este repositório.
- Build: `Dockerfile`, target `production`.
- Container port: `8000`.
- Replicas/workers: **1**. Arm state e execution lock são process-local por segurança.
- Healthcheck: `GET /health`.
- Domain: por exemplo `https://cordyceps.tdamiao.com`.

## 2. PostgreSQL

Crie PostgreSQL 16 com volume persistente e configure:

```dotenv
POSTGRES_USER=cordyceps
POSTGRES_DB=cordyceps
POSTGRES_PASSWORD=<senha-forte>
```

No app:

```dotenv
DATABASE_URL=postgresql+psycopg://cordyceps:<senha-forte>@postgres:5432/cordyceps
```

A inicialização cria tabelas e migrations aditivas automaticamente. Nunca use `drop_existing` em produção.

## 3. Primeiro deploy: paper

Adicione ao app exatamente:

```dotenv
TRADING_MODE=paper
LIVE_TRADING_ENABLED=false
DRY_RUN=true
KILL_SWITCH=false
ADMIN_TOKEN=<token-aleatorio-forte-para-api-opcional>
GITHUB_CLIENT_ID=<client-id-do-oauth-app>
github_key=<client-secret-do-oauth-app>
GITHUB_REDIRECT_URI=https://cordyceps.tdamiao.com/login
GITHUB_ALLOWED_USER=tdamiao
DATABASE_URL=postgresql+psycopg://cordyceps:<senha-forte>@postgres:5432/cordyceps
POLYGON_RPC_URL=<rpc-polygon>
PORT=8000
LOG_FORMAT=json
LOG_LEVEL=INFO
```

`PRIVATE_KEY`, `PROXY_ADDRESS` e credenciais CLOB podem ficar ausentes em paper. Após o deploy:

```bash
curl -fsS https://cordyceps.tdamiao.com/health
```

No GitHub OAuth App, configure **Authorization callback URL** exatamente como
`https://cordyceps.tdamiao.com/login`. Abra `/`, entre com a conta `tdamiao` e confirme
market data, WebSocket, books e paper metrics.

## 4. Preparar live_test — sem armar

Somente depois de validar paper, adicione/ajuste:

```dotenv
TRADING_MODE=live_test
LIVE_TRADING_ENABLED=true
DRY_RUN=false
PRIVATE_KEY=<secret-do-signer-eoa>
PROXY_ADDRESS=<wallet-funder>
SIGNATURE_TYPE=1
CLOB_API_KEY=<opcional>
CLOB_API_SECRET=<opcional>
CLOB_API_PASSPHRASE=<opcional>
MAX_TRADE_USD=1
MAX_TOTAL_EXPOSURE_USD=2
MAX_DAILY_LOSS_USD=1
MAX_OPEN_TRADES=1
MAX_LEG_IMBALANCE_USD=1
LEG_TIMEOUT_MS=2000
MIN_NET_EDGE=0.01
MIN_NET_PROFIT_USD=0.01
MAX_SLIPPAGE_PCT=0.005
ORDERBOOK_STALE_MS=3000
CIRCUIT_BREAKER_FAILURE_THRESHOLD=3
CIRCUIT_BREAKER_COOLDOWN_MINUTES=15
MARKET_LIMIT=50
SCAN_INTERVAL_SECONDS=60
```

As credenciais CLOB podem ser omitidas para derivação pelo SDK, mas readiness exige autenticação válida. Confirme `SIGNATURE_TYPE` conforme sua wallet (`0` EOA; tipos proxy/safe/deposit conforme documentação oficial vigente).

O restart resultante permanece **disarmed**. No dashboard:

1. Refresh wallet.
2. Run checks em Live Readiness.
3. Corrija todo item `blocked`.
4. Confirme geoblock `allowed`.
5. Somente então clique **Arm live test** e digite `CORDYCEPS LIVE`.

Não adicione `TRADING_MODE=live` nesta fase.

## Variáveis que devem ser secrets no Dokploy

- `ADMIN_TOKEN`
- `github_key`
- `POSTGRES_PASSWORD` (ou o `DATABASE_URL` completo)
- `PRIVATE_KEY`
- `CLOB_API_SECRET`
- `CLOB_API_PASSPHRASE`
- `CLOB_API_KEY` (trate como credencial, embora não seja chave de assinatura)

Nenhum desses valores aparece no dashboard. `PRIVATE_KEY` nunca é persistida no PostgreSQL.

## Kill switch

O botão Kill Switch desarma o processo e impede novas ordens sem interromper market data ou dashboard. `Resume` desliga o switch, mas não rearma live. Se aparecer `EXPOSURE REQUIRES ATTENTION`, não rearme até reconciliar a wallet e o histórico de execução.

## Atualizações e rollback

Todo novo container inicia disarmed. Depois de update/rollback, rode readiness novamente. Mantenha o volume PostgreSQL; as migrations são somente aditivas.

## Troubleshooting

| Sintoma | Ação |
|---|---|
| Login informa que OAuth não está configurado | Configure `GITHUB_CLIENT_ID` e `github_key`, confira a callback `/login` e redeploy. |
| Readiness `geographic_eligibility=blocked` | Live é proibido nessa localização. Use apenas paper; não tente bypass. |
| `balance`/`allowance` bloqueado | Confira pUSD no funder, signature type e approvals oficiais. O bot não aprova tokens automaticamente. |
| `clob_authentication=blocked` | Confira signer/funder/signature type e derive novamente as credenciais L2. |
| `dry_run=blocked` | Para live_test real, configure `DRY_RUN=false`; paper deve continuar `true`. |
| `EXPOSURE REQUIRES ATTENTION` | Mantenha kill switch, confira posições/order history e faça reconciliação manual. |
| Dashboard funciona mas books estão vazios | Verifique logs de WebSocket e Gamma, DNS/egress e `MARKET_LIMIT`. |

Nunca habilite live sem readiness completo. Readiness não envia ordens, approvals ou transações.

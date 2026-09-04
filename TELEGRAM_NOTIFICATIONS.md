"""
CORDYCEPS-TELEGRAM-001: Módulo de Notificações Telegram

Relatório de Implementação e Validação
======================================

## Visão Geral

O módulo `src/notifications/telegram.py` fornece um sistema completo de notificações
via Telegram para o bot Cordyceps. Permite alertar o operador em tempo real sobre:

- Eventos de startup/shutdown
- Abertura e fechamento de posições
- P&L de trades individuais
- Atualizações de posições favoritas
- Resumos diários
- Erros e eventos de risco
- Scans de mercado
- Oportunidades de arbitragem

## Arquitetura

### Componentes Principais

1. **TelegramConfig (dataclass)**
   - Carrega credenciais de variáveis de ambiente (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
   - Fallback para settings.py se env vars não forem definidas
   - Flag `enabled` indica se notificações estão ativas

2. **TelegramNotifier (classe)**
   - Cliente HTTP assíncrono via aiohttp
   - Gerenciamento de sessão reutilizável com timeout de 10s
   - Métodos específicos para cada tipo de evento
   - Parse mode HTML para formatação de mensagens
   - Retorna True/False para sucesso/falha

3. **Gerenciamento Global**
   - get_notifier(): obtém ou cria instância singleton
   - init_notifications(): inicializa e envia mensagem de startup
   - shutdown_notifications(): fecha sessão de forma segura

## Variáveis de Ambiente

Adicione ao seu .env:

```
# Token do bot Telegram (criar em @BotFather)
TELEGRAM_BOT_TOKEN=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk

# Seu Chat ID do Telegram (obter com @userinfobot)
TELEGRAM_CHAT_ID=123456789
```

Ou em settings.py:
```python
telegram_bot_token: str = ""
telegram_chat_id: str = ""
```

## Métodos de Notificação

### notify_startup()
Envia mensagem ao inicializar o bot com:
- Modo de trading (paper/live)
- Capital máximo
- Trade máximo
- Status da estratégia Favorite

### notify_shutdown(reason)
Envia mensagem ao parar o bot com o motivo

### notify_trade_open(market_id, market_question, side, price, size, usd_value, strategy, is_favorite)
Notifica abertura de posição com:
- Identificação do mercado
- Lado (BUY/SELL)
- Preço e tamanho
- Valor em USD
- Estratégia (arbitrage ou favorite)

### notify_trade_close(market_id, market_question, side, entry_price, exit_price, size, pnl, pnl_pct, is_favorite, hold_duration_min)
Notifica fechamento com:
- Preços de entrada e saída
- P&L em dólares e percentual
- Duração da posição (em minutos)
- Emojis verdes (lucro) ou vermelhos (prejuízo)

### notify_favorite_position_update(market_id, market_question, current_price, entry_price, unrealized_pnl_pct, action)
Atualiza posição Favorite em monitoramento com:
- Preço atual e P&L não realizado
- Ação (HOLD, TAKE_PROFIT, STOP_LOSS)

### notify_daily_summary(total_trades, winning_trades, losing_trades, total_pnl, total_pnl_pct, favorite_trades, favorite_pnl, arb_trades, arb_pnl)
Resume o dia com:
- Total de trades e taxa de acerto
- P&L total e percentual
- Breakdown por estratégia

### notify_error(error_type, error_message, context, severity)
Notifica erros com:
- Tipo e mensagem
- Contexto adicional (dict → formatado como lista)
- Severity: ERROR, WARNING, CRITICAL

### notify_risk_event(event_type, message, current_value, limit)
Notifica eventos de risco (KILL_SWITCH, CIRCUIT_BREAKER, etc)

### notify_market_scan(markets_scanned, opportunities_found, opportunities_executed, favorite_candidates)
Resume varredura de mercado (evita spam se vazio)

### notify_arbitrage_opportunity(market_id, market_question, signal_type, edge, yes_price, no_price, yes_bid, no_bid)
Notifica oportunidade de arbitragem detectada

## Exemplo de Uso

```python
from src.notifications.telegram import init_notifications, get_notifier
from decimal import Decimal

# Inicializar (envia startup se configurado)
notifier = await init_notifications()

# Notificar trade aberto
await notifier.notify_trade_open(
    market_id="0xabc123...",
    market_question="Will Bitcoin hit $100k?",
    side="BUY",
    price=Decimal("0.65"),
    size=Decimal("10"),
    usd_value=Decimal("6.50"),
    is_favorite=False,
)

# Notificar fechamento
await notifier.notify_trade_close(
    market_id="0xabc123...",
    market_question="Will Bitcoin hit $100k?",
    side="BUY",
    entry_price=Decimal("0.60"),
    exit_price=Decimal("0.70"),
    size=Decimal("10"),
    pnl=Decimal("1.00"),
    pnl_pct=16.67,
)

# Notificar erro
await notifier.notify_error(
    error_type="API_ERROR",
    error_message="Rate limit exceeded",
    context={"endpoint": "/markets", "retry_after": "60"},
    severity="ERROR",
)

# Encerrar gracefully
await shutdown_notifications()
```

## Testes

O módulo possui cobertura completa de testes em `tests/test_telegram.py`:

### TestTelegramConfig
- test_from_env_no_creds_disables_notifier
- test_from_env_with_bot_token
- test_from_env_with_partial_creds_disabled
- test_settings_fallback
- test_env_overrides_settings
- test_whitespace_values_are_not_empty
- test_long_token_handling

### TestTelegramNotifierSend
- test_disabled_skips_send
- test_send_message_success
- test_send_message_failure_returns_false
- test_send_message_exception_returns_false
- test_close_closes_session

### TestTelegramNotifierEvents
- test_notify_startup
- test_notify_shutdown
- test_notify_trade_open
- test_notify_trade_close_profit
- test_notify_trade_close_loss
- test_notify_error
- test_notify_daily_summary

### TestGlobalNotifier
- test_get_notifier_creates_instance
- test_get_notifier_returns_same_instance
- test_init_notifications_sends_startup

### TestTelegramConfigEdgeCases
- test_whitespace_values_are_not_empty
- test_long_token_handling

## Execução de Testes

```bash
# Executar todos os testes
pytest tests/test_telegram.py -v

# Com cobertura
pytest tests/test_telegram.py --cov=src.notifications.telegram

# Apenas um teste
pytest tests/test_telegram.py::TestTelegramConfig::test_from_env_no_creds_disables_notifier -v
```

## Integração com o Bot

A integração é feita em `src/main.py`:

```python
from src.notifications.telegram import init_notifications, shutdown_notifications

class ArbitrageBot:
    async def start(self):
        # Inicializar notificações
        notifier = await init_notifications()
        
        # ... resto do código ...
        
        # Ao parar:
        await shutdown_notifications()
```

## Características de Segurança

- ✓ Credenciais lidas apenas de env vars / settings (nunca hardcoded)
- ✓ Timeout de 10s em requisições HTTP (evita travamentos)
- ✓ Tratamento de exceções em todos os sends
- ✓ Session reutilizável (eficiência)
- ✓ Graceful shutdown com close()
- ✓ Logs estruturados de falhas
- ✓ HTML escaping através de parse_mode

## Limitações e Notas

- Não envia notificações se TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID não forem definidos
- Retorna False se a API retornar erro (não lança exceção)
- Não implementa retry automático (delegar ao caller se necessário)
- Limite de rate limit do Telegram: ~30 msgs/segundo por chat
- Mensagens cortadas em >4096 caracteres pela API do Telegram

## Dependências

- aiohttp>=3.9.0 (já no pyproject.toml)
- structlog>=24.0.0 (para logging)
- pydantic (para Settings)

## Próximos Passos

- Integrar notificações no fluxo principal do bot (src/main.py)
- Adicionar configuração de frequência de notificações (ex: não notificar cada trade em live)
- Implementar batch de notificações em períodos de alto volume
- Adicionar métricas de sucesso de notificações ao health monitor
"""

# Estratégias de Trading

Este diretório contém documentação detalhada sobre as estratégias de trading implementadas no Cordyceps.

## Estratégias Disponíveis

### [Favorite Compounding](./favorite_compounding.md)
Estratégia de compra de favoritos em mercados binários com alta probabilidade (85-98¢) próximos à resolução. Aproveita a estrutura de fees favorável do Polymarket (pós-reformulação de março de 2026) e o alto win rate (~88-92%) para gerar retornos compostos consistentes.

**Quando usar**:
- Mercados com resolução iminente (<72h)
- Resultados de alta probabilidade claramente identificáveis
- Análise fundamental forte (probabilidade real >> preço de mercado)

**Benchmarks** (Datawallet 2024-2026):
- Win Rate: 88-92%
- Retorno médio/trade: 4.2-5.8% (após taxas)
- Sharpe Ratio: 1.8-2.3
- Max Drawdown: 8-12% (em 100 trades)
- Projeção mensal: 12.8-19.2% (banca $1k, 15% exposição)

## Implementação

Ambas as estratégias compartilham o mesmo motor de execução e gestão de risco, mas têm parâmetros de detecção diferentes:

```python
# Em src/engine/
detector.py    # Detector de Unity Arbitrage
favorite.py    # Detector de Favorite Compounding
```

A estratégia ativa é configurada via variáveis de ambiente (ver `src/config.py`).

## Ativação

Para ativar a estratégia Favorite Compounding:

```bash
# Em .env ou variáveis de ambiente
ENABLE_FAVORITE_STRATEGY=true

# Parâmetros opcionais (defaults sensatos já estão configurados)
MIN_FAVORITE_PROBABILITY=0.90
MIN_FAVORITE_PRICE=0.85
MAX_FAVORITE_PRICE=0.98
FAVORITE_TAKE_PROFIT=0.97
FAVORITE_STOP_LOSS=0.80
```

Ver [README.md](../../README.md) para documentação geral e [docs/DOCKPLOY.md](../DOCKPLOY.md) para instruções de deploy.

## Comparação Rápida

| Aspecto | Unity Arbitrage | Favorite Compounding |
|---------|----------------|---------------------|
| Tipo | Risk-free (se executado) | Statistical edge |
| Win Rate | ~100% | 88-92% |
| Edge Médio | 0.5-2% | 4.2-5.8% |
| Frequência | Rara | Moderada |
| Sensibilidade a Taxas | Muito Alta | Muito Baixa |
| Janela de Execução | Segundos | Horas-Dias |
| Capital | Baixa eficiência | Alta eficiência |
| Risco | Baixo (técnico) | Médio (fundamental) |

## Próximas Estratégias

Roadmap futuro (não implementado):

- **Statistical Arbitrage**: Mean reversion em pares correlacionados
- **Liquidity Provision**: Market making com spread protection
- **Event-Driven**: Trading em torno de eventos macro (FOMC, earnings)

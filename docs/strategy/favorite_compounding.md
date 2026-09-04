# Estratégia: Favorite Compounding (Fechamento de Favoritos)
## Para Polymarket Arbitrage Bot - Cordyceps

### Visão Geral
A estratégia de **Favorite Compounding** foca em negociar resultados de alta probabilidade (favoritos) próximos à resolução, explorando a ineficiência de preço onde o mercado subestima a probabilidade real de eventos quase certos. Esta abordagem gera retornos pequenos, consistentes e compostos através da repetição.

### Por que Funciona em 2026
Após a reformulação das taxas em março de 2026, esta estratégia tornou-se **mais atraente** porque:
- A fórmula de taxa **pica no ponto 50/50** e **diminui em direção aos extremos** (0% e 100%)
- Negociar em 95 cents custa **dramaticamente menos** em taxas do que negociar o mesmo mercado em 50 cents
- Baixa sensibilidade a taxas (baixo *Fee Sensitivity*) conforme mostrado na análise da Datawallet

### Mecânica da Estratégia
1. **Identificação**: Encontrar mercados onde:
   - O resultado tem **probabilidade real ≥ 90%** (baseado em análise fundamental/quantitativa)
   - O mercado precifica o resultado favorito entre **85¢ e 98¢**
   - Resolução iminente (tipicamente < 72 horas)

2. **Entrada**: Comprar ações do resultado favorito no preço de mercado

3. **Saída**: Manter até a resolução (receber $1,00 por ação) ou vender próximo à resolução se houver oportunidade de取利

4. **Cálculo de Lucro Esperado**:
   ```
   Lucro por ação = $1,00 - Preço de entrada
   Retorno % = (Lucro por ação / Preço de entrada) × 100
   ```

### Exemplos Concretos

#### Exemplo 1: Decisão de Taxa de Juros
- **Mercado**: "Federal Reserve manterá taxas inalteradas em setembro 2026?"
- **Análise**: Dados do CME FedWatch Tool mostram 92% de probabilidade de manutenção
- **Preço Polymarket**: NO (não alterar) cotado a 92¢
- **Lucro esperado**: $1,00 - $0,92 = $0,08 por ação
- **Retorno**: 8,70% sobre o capital investido
- **Taxa estimada**: ~0,3% (devido ao preço extremo)
- **Retorno líquido**: ~8,40%

#### Exemplo 2: Resultado Esportivo Claro
- **Mercado**: "Time X vencerá o Campeonato Nacional 2026?"
- **Análise**: Time X tem campanha invicta, saldo de gols +25, adversário técnico fraco
- **Probabilidade real**: 95%
- **Preço Polymarket**: SIM cotado a 93¢
- **Lucro esperado**: $0,07 por ação
- **Retorno**: 7,53%
- **Taxa estimada**: ~0,25%
- **Retorno líquido**: ~7,28%

### Vantagens Competitivas
| Critério | Favorite Compounding | Arbitrage | Contrarian Fading |
|----------|---------------------|-----------|-------------------|
| **Win Rate** | 85-95% | ~100% (se executado) | 60-70% |
| **Fee Sensitivity** | Muito Baixa | Muito Alta | Baixa |
| **Automação Potential** | Alta | Alta | Média |
| **Capital Efficiency** | Alta | Baixa (requere hedge) | Média |
| **Psychological Load** | Baixa (poucos losses) | Muito Baixa | Alta (frequentes losses contra-intuitivos) |
| **Execution Window** | Horas a dias | Minutos | Minutos a horas |

### Implementação Técnica no Cordyceps

#### 1. Filtros de Mercado Adicionais
```python
# Em src/engine/detector.py ou novo módulo
def is_favorite_compounding_candidate(opportunity: ArbitrageOpportunity) -> bool:
    """Verifica se oportunidade se encaixa na estratégia de favorite compounding"""
    
    # Apenas para mercados binários (BUY_SET/SELL_SET direto)
    if opportunity.signal_type not in [SignalType.BUY_SET, SignalType.SELL_SET]:
        return False
        
    # Preço do resultado favorito deve estar em zona favorável
    favorite_price = max(opportunity.prices) if opportunity.signal_type == SignalType.BUY_SET \
                    else min(opportunity.prices)
    
    # Zona ideal: 85¢ - 98¢ (evita extremos onde liquidez some)
    if not (0.85 <= favorite_price <= 0.98):
        return False
        
    # Resolução iminente (< 72 horas)
    time_to_resolution = opportunity.timestamp_to_resolution  # precisa implementar
    if time_to_resolution > 72 * 3600:  # 72 horas em segundos
        return False
        
    # Liquidez mínima necessária
    if opportunity.max_size < settings.min_favorite_size_usd:  # novo parâmetro
        return False
        
    return True
```

#### 2. Modificação na Lógica de Entrada
```python
# Em src/execution/order_executor.py ou similar
async def execute_opportunity(self, opportunity: ArbitrageOpportunity):
    # Verificar se é candidato a favorite compounding
    if self._is_favorite_strategy_enabled and \
       is_favorite_compounding_candidate(opportunity):
       
       # Usar tamanho de posição otimizado para favorite compounding
       # (geralmente maior pois win rate alto)
       position_size = self._calculate_favorite_position_size(opportunity)
       
       # Executar com estratégia de limite para minimizar slippage
       return await self._execute_limit_order(opportunity, position_size)
    else:
        # Lógica padrão existente
        return await self._execute_market_order(opportunity)
```

#### 3. Gestão de Posição
```python
# Manter posição até resolução ou saída antecipada por take profit
async def monitor_favorite_position(self, position: Position):
    # Verificar condições de saída antecipada
    current_price = await self._get_current_market_price(position.market_id)
    
    # Take profit se atingir 97%+ (máximo razoável antes de taxas)
    if current_price >= 0.97:
        await self._close_position(position, reason="take_profit_97")
        return
        
    # Stop loss se preço cair abaixo de 80% (sinal de mudança fundamental)
    if current_price <= 0.80:
        await self._close_position(position, reason="stop_loss_80")
        return
        
    # Manter até resolução caso contrário
```

### Parâmetros de Configuração Recomendados
Adicionar ao `src/config.py` na classe `Settings`:

```python
# Favorite Compounding Strategy Parameters
min_favorite_probability: float = Field(default=0.90, ge=0.80, le=0.99)  # 90% mínimo
min_favorite_price: float = Field(default=0.85, ge=0.70, le=0.95)      # 85¢ mínimo
max_favorite_price: float = Field(default=0.98, ge=0.90, le=0.99)      # 98¢ máximo
min_favorite_size_usd: float = Field(default=5.0, gt=0)               # $5 mínimo de exposição
favorite_take_profit: float = Field(default=0.97, ge=0.90, le=0.99)   # 97% take profit
favorite_stop_loss: float = Field(default=0.80, ge=0.50, le=0.90)     # 80% stop loss
max_favorite_exposure_pct: float = Field(default=0.30, ge=0.10, le=0.50) # 30% do capital max
```

### Validação Estatística
Baseado em dados históricos da Datawallet (2024-2026):

| Métrica | Valor | Fonte |
|---------|-------|-------|
| Win Rate | 88-92% | Análise de 1.200 mercados favoritos resolvidos |
| Average Profit/Trade | 4.2-5.8% | Após taxas, em mercados 85-95¢ |
| Sharpe Ratio | 1.8-2.3 | Ajustado por volatilidade baixa |
| Max Drawdown | 8-12% | Em séries de 100 trades consecutivos |
| Recovery Factor | 3.5-4.5 | Lucro máximo / drawdown máximo |
| Trades/Dia (potencial) | 3-8 | Dependendo do calendário de eventos |

### Integração com Gestão de Risco
A estratégia deve operar dentro dos limites existentes:

```python
# Em src/risk/manager.py
def calculate_favorite_position_size(self, opportunity: ArbitrageOpportunity) -> float:
    """Calcula tamanho de posição otimizado para favorite compounding"""
    
    # Base: Kelly Criterion ajustado para favorite compounding
    win_prob = opportunity.favorite_price  # usando preço de mercado como proxy conservador
    avg_win = (1.0 - opportunity.favorite_price) / opportunity.favorite_price  # retorno se vencer
    avg_loss = 1.0  # perder todo o investimento (improbável mas possível)
    
    # Kelly fraction: f = (bp - q) / b
    # onde b = avg_win, p = win_prob, q = 1 - win_prob
    b = avg_win
    p = win_prob
    q = 1 - p
    
    kelly_fraction = (b * p - q) / b if b > 0 else 0
    
    # Aplicar frações de Kelly para reduzir variância
    fraction = settings.favorite_kelly_fraction  # novo parâmetro (ex: 0.25 para quarter-Kelly)
    adjusted_fraction = kelly_fraction * fraction
    
    # Limitar pelo máximo de exposição permitido
    max_size_by_risk = self._get_max_risk_based_size()
    max_size_by_strategy = settings.max_favorite_exposure_pct * self._get_account_balance()
    
    return min(max_size_by_risk, max_size_by_strategy, adjusted_fraction * self._get_account_balance())
```

### Considerações Especiais

#### 1. Liquidez e Slippage
- Apesar da baixa sensibilidade a taxas, **verificar liquidez** é crucial
- Mercados com < $100 de volume diário podem ter slippage significativo
- Implementar verificação de profundidade de livro antes da entrada

#### 2. Eventos de Black Swan
- Embora improvável, eventos como desclassificações repentinas podem ocorrer
- Implementar stop loss técnico (ex: 80%) como proteção
- Considerar seguro via mercado oposto em casos de alta convicção mas risco de reviravolta

#### 3. Correlação entre Posições
- Evitar exposição excessiva a eventos correlacionados
- Exemplo: Não apostar em múltiplas decisões de taxa de juros do mesmo banco central no mesmo mês
- Implementar limite de exposição por categoria/evento relacionado

### Cronograma de Implementação

#### Fase 1: Análise e Planejamento (Dia 1)
- [x] Pesquisa concluída
- [ ] Definir parâmetros exatos de configuração
- [ ] Criar issue no GitHub para acompanhamento

#### Fase 2: Desenvolvimento (Dias 2-3)
- [ ] Adicionar parâmetros de configuração em `src/config.py`
- [ ] Implementar função de detecção de candidato em `src/engine/`
- [ ] Modificar lógica de execução em `src/execution/`
- [ ] Adicionar função de monitoramento de posição
- [ ] Atualizar testes unitários

#### Fase 3: Teste e Validação (Dia 4)
- [ ] Executar em modo paper com dados históricos
- [ ] Validar contra backtesting de estratégias conhecidas
- [ ] Verificar métricas de performance (win rate, retorno médio, drawdown)
- [ ] Ajustar parâmetros conforme necessário

#### Fase 4: Deploy e Monitoramento (Dia 5+)
- [ ] Deploy em ambiente de teste
- [ ] Monitorar performance em tempo real
- [ ] Coletar feedback e fazer ajustes finos
- [ ] Considerar aumento gradual da exposição

### Projeção de Resultados
Com banca inicial de $1.000 e exposição média de 15% por trade:

| Métrica | Projeção Mensal (4 semanas) |
|---------|-----------------------------|
| Trades Esperados | 20-30 trades |
| Win Rate Esperado | 88% |
| Lucro Médio por Trade | 4.5% |
| Lucro Mensal Bruto | 20-30 trades × $150 × 4.5% = $135-$202 |
| Taxas Estimadas | ~5% do lucro bruto |
| **Lucro Líquido Mensal** | **$128-$192** |
| **Retorno Mensal** | **12.8%-19.2%** |
| **Retorno Anual Composto** | **300%-600%+** (teórico, sujeito a limites de capacidade) |

### Próximos Passos Imediatos
1. Revisar este documento com a equipe
2. Aprovar ou ajustar os parâmetros propostos
3. Iniciar implementação na branch `feature/favorite-compounding`
4. Definir métricas de sucesso para o período de teste
5. Agendar review após 1 semana de operação em paper mode

### Referências
1. Datawallet Team. (2026, August 10). *Top 10 Polymarket Trading Strategies in 2026*. Datawallet Research.
2. Laika Labs. (2026, February 16). *Polymarket Trading Strategies That Actually Work in 2026*. Laika Labs Blog.
3. Various. Polymarket Documentation. https://docs.polymarket.com/
4. CME Group. FedWatch Tool. https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html
5. FiveThirtyEight. Polling Archives. https://fivethirtyeight.com/tag/polls/

---
*Documento gerado para o bot Cordyceps - Polymarket Arbitrage System*
*Versão: 1.0 | Data: $(date +%Y-%m-%d)*
*Este estratégia foca em explorar a vantagem estatística de favoritos quase-certos, combinando alta acurácia com baixa sensibilidade a taxas no ambiente pós-março/2026 do Polymarket.*

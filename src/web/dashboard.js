// ==============================================================================
// Cordyceps Dashboard — Lógica de Interface e Sincronização em Tempo Real (PT-BR)
// ==============================================================================

const $ = (selector) => document.querySelector(selector);
let configValues = {};

// Função auxiliar para chamadas na API
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("Autenticação necessária.");
  }
  const data = await res.json();
  if (!res.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  }
  return data;
}

// Formatadores
const fmt = (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("pt-BR", { maximumFractionDigits: d }));
const money = (v) => (v === null || v === undefined ? "—" : `$ ${Number(v).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`);
const pct = (v) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 3 })}%`);
const shortAddr = (addr) => (addr && addr.length > 12 ? `${addr.slice(0, 6)}...${addr.slice(-4)}` : addr || "Não configurado");

function metric(label, value, cls = "", title = "") {
  return `
    <div class="metric-item">
      <span class="metric-label">${label}</span>
      <span class="metric-value ${cls}" title="${title || ""}">${value}</span>
    </div>
  `;
}

function stateClass(v) {
  const str = String(v).toLowerCase();
  if (["healthy", "ok", "conectado", "running", "sim", "ativo", "true", "ready"].includes(str)) return "ok";
  if (["blocked", "parado", "erro", "false", "não", "falha", "stopped"].includes(str)) return "blocked";
  return "warning";
}

function table(rows, cols) {
  if (!rows || !rows.length) return `<div class="table-empty">Nenhum registro encontrado no momento.</div>`;
  return `
    <table>
      <thead>
        <tr>${cols.map((c) => `<th>${c[0]}</th>`).join("")}</tr>
      </thead>
      <tbody>
        ${rows.map((row) => `<tr>${cols.map((c) => `<td>${c[1](row)}</td>`).join("")}</tr>`).join("")}
      </tbody>
    </table>
  `;
}

// Carregar Status Geral
async function loadStatus() {
  const s = await api("/api/status");

  // Modo e Saúde no Header
  const modeMap = { paper: "SIMULAÇÃO (PAPER)", live_test: "TESTE REAL (LIVE TEST)", live: "PRODUÇÃO REAL (LIVE)" };
  $("#mode").textContent = modeMap[s.mode] || s.mode.toUpperCase();

  const healthMap = { healthy: "OPERACIONAL", stopped: "PARADO", blocked: "BLOQUEADO", running: "RODANDO" };
  $("#health").textContent = healthMap[s.status] || s.status.toUpperCase();
  $("#health").className = `badge badge-health ${stateClass(s.status)}`;

  $("#attention").classList.toggle("hidden", !s.exposure_requires_attention);

  // 1. Sistema & Rede
  const obs = s.observer_stats || {};
  $("#systemMetrics").innerHTML =
    metric("Status do Bot", s.running ? "RODANDO" : "PARADO", s.running ? "ok" : "blocked") +
    metric("WebSocket CLOB", s.websocket ? "CONECTADO" : "DESCONECTADO", s.websocket ? "ok" : "blocked") +
    metric("API CLOB", s.clob_status === "healthy" ? "ONLINE" : "OFFLINE", stateClass(s.clob_status)) +
    metric("Scanner de Mercados", s.scanner ? "ATIVO" : "INATIVO", s.scanner ? "ok" : "blocked") +
    metric("Mercados Ativos", fmt(s.markets, 0)) +
    metric("Tokens Monitorados", fmt(s.tokens, 0)) +
    metric("Livros com Liquidez", fmt(s.books_with_liquidity, 0)) +
    metric("Atualizações de Book", fmt(s.book_updates, 0)) +
    metric("Tempo Online (Uptime)", `${fmt(s.uptime, 0)}s`);

  // 2. Estratégia & Margem
  const strat = s.strategy || {};
  $("#strategyMetrics").innerHTML =
    metric("Mercados Analisados", fmt(strat.markets_analyzed, 0)) +
    metric("Oportunidades Encontradas", fmt(strat.opportunities_found, 0)) +
    metric("Melhor Margem Compra", pct(strat.best_buy_edge_seen)) +
    metric("Melhor Margem Venda", pct(strat.best_sell_edge_seen)) +
    metric("Melhor Margem Líquida", pct(strat.best_net_edge_seen), strat.best_net_edge_seen > 0 ? "ok" : "") +
    metric("Maior Lucro Projetado", money(strat.best_net_profit_seen), strat.best_net_profit_seen > 0 ? "ok" : "") +
    metric("Descarte (Livro Antigo)", fmt(strat.rejected_stale, 0)) +
    metric("Descarte (Margem Baixa)", fmt(strat.rejected_edge, 0));

  // 3. Carteira & Saldo
  const w = s.wallet || {};
  $("#walletMetrics").innerHTML =
    metric("Conta Signer (EOA)", shortAddr(w.eoa_address), w.eoa_address ? "" : "blocked", w.eoa_address) +
    metric("Carteira Proxy", shortAddr(w.proxy_address), "", w.proxy_address) +
    metric("Saldo Disponível", money(w.usdc_balance), w.usdc_balance > 0 ? "ok" : "warning") +
    metric("Aprovação (Allowance)", money(w.exchange_allowance), w.exchange_allowance > 0 ? "ok" : "warning") +
    metric("Autenticação CLOB", w.authenticated ? "AUTORIZADO" : "NÃO AUTORIZADO", w.authenticated ? "ok" : "blocked") +
    metric("Última Leitura", w.last_refresh ? new Date(w.last_refresh * 1000).toLocaleTimeString("pt-BR") : "—");

  // 4. Execução & Controles
  const risk = s.risk || {};
  $("#controlState").innerHTML =
    metric("Armado para Live", s.armed ? "SIM (ARMADO)" : "NÃO (DESARMADO)", s.armed ? "ok" : "warning") +
    metric("Chave Geral (Kill Switch)", s.kill_switch ? "ATIVADA (TRAVADO)" : "DESATIVADA", s.kill_switch ? "blocked" : "ok") +
    metric("Exposição Atual", money(s.current_exposure)) +
    metric("Exposição Incompleta", money(s.incomplete_exposure), s.incomplete_exposure > 0 ? "blocked" : "ok") +
    metric("Resultado Diário (P&L)", money(risk.daily_pnl), risk.daily_pnl > 0 ? "ok" : risk.daily_pnl < 0 ? "blocked" : "") +
    metric("Disjuntor (Circuit Breaker)", risk.is_paused ? "PAUSADO" : "PRONTO", risk.is_paused ? "blocked" : "ok");

  // Oportunidades Mais Próximas (Radar)
  $("#closest").innerHTML = table(strat.closest_opportunities || [], [
    ["Mercado", (r) => shortAddr(r.market_id)],
    ["Sinal", (r) => (r.signal === "BUY_SET" ? "🟢 COMPRA" : "🔴 VENDA")],
    ["Margem Líquida", (r) => pct(r.net_edge)],
    ["Lucro Estimado", (r) => money(r.net_profit)],
  ]);
}

// Configurações Operacionais
const TRANSLATIONS = {
  max_trade_usd: "Tamanho Máx. por Trade ($)",
  max_total_exposure_usd: "Exposição Máx. Total ($)",
  max_daily_loss_usd: "Limite Máx. Perda Diária ($)",
  max_open_trades: "Máx. Operações Simultâneas",
  min_profit_threshold: "Margem Mínima Legada",
  min_net_edge: "Margem Líquida Mínima (Edge)",
  min_net_profit_usd: "Lucro Líquido Mínimo ($)",
  max_slippage_pct: "Slippage Máx. Tolerado (%)",
  orderbook_stale_ms: "Idade Máx. do Livro (ms)",
  min_trade_shares: "Qtd. Mínima de Cotas",
  max_leg_imbalance_usd: "Desbalanceamento Máx. ($)",
  leg_timeout_ms: "Timeout de Cada Ordem (ms)",
  circuit_breaker_failure_threshold: "Limite de Falhas Consecutivas",
  circuit_breaker_cooldown_minutes: "Tempo de Espera Disjuntor (min)",
  simulated_latency_ms: "Latência Simulada Paper (ms)",
  market_limit: "Limite de Mercados Monitorados",
  scan_interval_seconds: "Intervalo de Varredura (s)",
};

async function loadConfig() {
  const c = await api("/api/config");
  configValues = c.values;
  $("#configForm").innerHTML = Object.entries(c.values)
    .map(([k, v]) => `
      <div class="config-item">
        <label for="cfg-${k}">${TRANSLATIONS[k] || k.replaceAll("_", " ")}</label>
        <small>${c.descriptions[k] || "Parâmetro operacional."}</small>
        <input id="cfg-${k}" data-key="${k}" type="number" step="any" value="${v}">
      </div>
    `)
    .join("");
}

async function saveConfig() {
  const values = {};
  document.querySelectorAll("[data-key]").forEach((i) => {
    values[i.dataset.key] =
      i.dataset.key.includes("_ms") ||
      ["max_open_trades", "market_limit", "circuit_breaker_failure_threshold", "circuit_breaker_cooldown_minutes"].includes(i.dataset.key)
        ? Number.parseInt(i.value)
        : Number(i.value);
  });
  const res = await api("/api/config", { method: "PUT", body: JSON.stringify(values) });
  $("#configMessage").textContent = res.restart_required ? "✅ Parâmetros salvos (reinício necessário)." : "✅ Parâmetros aplicados instantaneamente!";
  setTimeout(() => ($("#configMessage").textContent = ""), 4000);
  await loadStatus();
}

// Live Readiness
const CHECK_TRANSLATIONS = {
  database: "Banco de Dados PostgreSQL",
  gamma_api: "API de Mercados Gamma",
  clob_api: "API de Trading CLOB",
  websocket: "Conexão WebSocket CLOB",
  market_data: "Recebimento de Market Data",
  order_books: "Livros de Ofertas com Liquidez",
  wallet: "Carteira Proxy Configurada",
  private_key: "Chave Privada (Signer) Configurada",
  proxy_address: "Endereço Proxy Configurado",
  clob_authentication: "Autenticação L2 CLOB",
  polygon_rpc: "Conexão RPC Rede Polygon",
  balance: "Saldo em Carteira (USDC)",
  usdc_allowance: "Aprovação de Saldo (Allowance)",
  ctf_allowance: "Aprovação de Contratos CTF",
  geographic_eligibility: "Permissão Geográfica",
  kill_switch: "Chave de Emergência Desativada",
  risk_configuration: "Configuração de Risco Consistente",
  circuit_breaker: "Disjuntor de Segurança Pronto",
  live_enabled: "Modo Live Habilitado",
  dry_run: "Modo Simulado Desativado",
};

async function runReadiness() {
  $("#readinessSummary").innerHTML = "<div class='readiness-summary'>⏳ Executando testes de verificação...</div>";
  const r = await api("/api/readiness?refresh=true");
  
  $("#readinessSummary").innerHTML = `
    <div class="readiness-summary ${r.ready ? "ready" : "blocked"}">
      ${r.ready ? "✅ SISTEMA 100% PRONTO PARA ARMAR E OPERAR EM LIVE" : "⚠️ PRÉ-REQUISITOS PENDENTES — MODO REAL BLOQUEADO POR SEGURANÇA"}
    </div>
  `;

  $("#readinessChecks").innerHTML = Object.entries(r.checks)
    .map(([k, v]) => `
      <div class="check-box">
        <span class="check-box-name">${CHECK_TRANSLATIONS[k] || k.replaceAll("_", " ")}</span>
        <span class="check-box-status ${v.status === "ok" ? "ok" : v.status === "warning" ? "warning" : "blocked"}">
          ${v.status === "ok" ? "✔ APROVADO" : v.status === "warning" ? "⚠️ ALERTA" : "✖ BLOQUEADO"}
        </span>
      </div>
    `)
    .join("");
}

// Histórico de Execuções e Auditoria
async function loadHistory() {
  const h = await api("/api/history");

  // Oportunidades
  $("#opportunities").innerHTML = table(h.opportunities || [], [
    ["Horário", (r) => new Date(r.timestamp).toLocaleTimeString("pt-BR")],
    ["Mercado", (r) => shortAddr(r.market_id)],
    ["Sinal", (r) => (r.signal_type === "BUY_SET" ? "🟢 COMPRA" : "🔴 VENDA")],
    ["Margem Líquida", (r) => pct(r.net_edge)],
    ["Lucro Estimado", (r) => money(r.net_profit)],
    ["Decisão", (r) => (r.decision === "paper" ? "SIMULAÇÃO" : r.decision === "candidate" ? "EXECUTAR" : "REJEITADO")],
  ]);

  // Trades
  const trades = [...(h.executions || []), ...(h.paper_trades || [])].sort((a, b) => (b.created_at || b.timestamp) - (a.created_at || a.timestamp));
  $("#trades").innerHTML = table(trades, [
    ["Modo", (r) => (r.mode === "paper" ? "SIMULAÇÃO" : "REAL")],
    ["Mercado", (r) => shortAddr(r.market_id)],
    ["Status", (r) => (r.state === "COMPLETED" || r.success ? "🟢 SUCESSO" : "🔴 FALHA")],
    ["Resultado (P&L)", (r) => money(r.realized_pnl)],
    ["Latência", (r) => `${r.latency_ms || 0}ms`],
  ]);

  // Eventos de Risco
  $("#riskEvents").innerHTML = table(h.risk_events || [], [
    ["Horário", (r) => new Date(r.timestamp).toLocaleTimeString("pt-BR")],
    ["Gravidade", (r) => (r.severity === "critical" ? "🚨 CRÍTICO" : "⚠️ ALERTA")],
    ["Tipo", (r) => r.event_type],
    ["Mensagem", (r) => r.message],
  ]);

  // Eventos do Sistema
  $("#systemEvents").innerHTML = table(h.system_events || [], [
    ["Horário", (r) => new Date(r.timestamp).toLocaleString("pt-BR")],
    ["Módulo", (r) => r.component],
    ["Evento", (r) => r.event_type],
    ["Descrição", (r) => r.message],
  ]);
}

// Notificações Toast
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  t.classList.toggle("danger", bad);
  setTimeout(() => t.classList.add("hidden"), 5000);
}

// Event Listeners
$("#saveConfig").onclick = () => saveConfig().catch((e) => toast(e.message, true));
$("#runReadiness").onclick = () => runReadiness().catch((e) => toast(e.message, true));
$("#refreshWallet").onclick = () =>
  api("/api/wallet/refresh", { method: "POST" })
    .then(() => {
      toast("Carteira atualizada com sucesso!");
      loadStatus();
    })
    .catch((e) => toast(e.message, true));

$("#kill").onclick = () =>
  api("/api/control/kill", { method: "POST" })
    .then(() => {
      toast("Chave de Emergência (Kill Switch) ativada!", true);
      loadStatus();
    })
    .catch((e) => toast(e.message, true));

$("#resume").onclick = () =>
  api("/api/control/resume", { method: "POST" })
    .then(() => {
      toast("Operações retomadas.");
      loadStatus();
    })
    .catch((e) => toast(e.message, true));

$("#arm").onclick = () => $("#armDialog").showModal();

$("#confirmArm").onclick = (e) => {
  e.preventDefault();
  api("/api/control/arm", {
    method: "POST",
    body: JSON.stringify({ confirmation: $("#armConfirmation").value }),
  })
    .then(() => {
      $("#armDialog").close();
      toast("⚡ Bot ARMADO com sucesso para operações em modo real!");
      loadStatus();
    })
    .catch((err) => toast(err.message, true));
};

// Inicialização
Promise.all([loadStatus(), loadConfig(), loadHistory()]).catch((e) => toast(e.message, true));
setInterval(() => {
  loadStatus();
  loadHistory();
}, 4000);

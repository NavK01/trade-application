/**
 * DeepSeek Quant Terminal — Production Frontend Controller
 * Single-frame layout, zero chatbot clutter, persistent P&L, and DeepSeek AI Signals.
 */

document.addEventListener('DOMContentLoaded', () => {

  // --- State Variables ---
  let authToken = localStorage.getItem('quant_auth_token') || '';
  let activeChartSymbol = 'SBIN';
  let activeTimeframe = '5m';
  let activePeriod = '5d';
  let chart = null;
  let candleSeries = null;
  let emaSeries = null;
  let ema25Series = null;
  let ema200Series = null;
  let vwapSeries = null;
  let volumeSeries = null;
  let activePriceLines = [];
  let latestAiSignal = null;
  const ACCOUNT_ID = 'deepseek_flash';

  // --- DOM Elements ---
  const tvChartContainer = document.getElementById('tv-chart-container');
  const chartSymbolTitle = document.getElementById('chart-symbol-title');
  const chartLtp = document.getElementById('chart-ltp');
  const chartChange = document.getElementById('chart-change');
  const orderSymbolInput = document.getElementById('order-symbol-input');
  const fastQty = document.getElementById('fast-qty');
  const btnFastBuy = document.getElementById('btn-fast-buy');
  const btnFastSell = document.getElementById('btn-fast-sell');
  const btnCloseAll = document.getElementById('btn-close-all');
  const latencyTag = document.getElementById('latency-tag');

  // Master P&L Elements
  const pnlCapitalVal = document.getElementById('pnl-capital-val');
  const pnlNetRealizedVal = document.getElementById('pnl-net-realized-val');
  const pnlUnrealizedVal = document.getElementById('pnl-unrealized-val');
  const pnlOpenPositionsCount = document.getElementById('pnl-open-positions-count');
  const targetBarFill = document.getElementById('target-bar-fill');
  const targetCurrentPct = document.getElementById('target-current-pct');
  const targetLockBadge = document.getElementById('target-lock-badge');
  const gateLossCount = document.getElementById('gate-loss-count');
  const gateDrawdown = document.getElementById('gate-drawdown');
  const gateWindow = document.getElementById('gate-window');

  // DeepSeek AI Prediction Elements
  const btnRefreshAiSignal = document.getElementById('btn-refresh-ai-signal');
  const aiSigAction = document.getElementById('ai-sig-action');
  const aiSigSetup = document.getElementById('ai-sig-setup');
  const aiSigGexRegime = document.getElementById('ai-sig-gex-regime');
  const aiSigConf = document.getElementById('ai-sig-conf');
  const aiSigEntry = document.getElementById('ai-sig-entry');
  const aiSigSl = document.getElementById('ai-sig-sl');
  const aiSigTp = document.getElementById('ai-sig-tp');
  const aiSigRr = document.getElementById('ai-sig-rr');
  const aiSigFees = document.getElementById('ai-sig-fees');
  const aiSigBe = document.getElementById('ai-sig-be');
  const aiSigNetPnl = document.getElementById('ai-sig-net-pnl');
  const aiReasoningBullets = document.getElementById('ai-reasoning-bullets');
  const btnExecuteAiSignal = document.getElementById('btn-execute-ai-signal');

  // Ledger Elements
  const tabBtnPositions = document.getElementById('tab-btn-positions');
  const tabBtnHistory = document.getElementById('tab-btn-history');
  const tabBtnDaily = document.getElementById('tab-btn-daily');
  const tabBtnTruth = document.getElementById('tab-btn-truth');
  const tabPositions = document.getElementById('tab-positions');
  const tabHistory = document.getElementById('tab-history');
  const tabDaily = document.getElementById('tab-daily');
  const tabTruth = document.getElementById('tab-truth');
  const ledgerPosCount = document.getElementById('ledger-pos-count');
  const ledgerOrdersCount = document.getElementById('ledger-orders-count');
  const ledgerDailyCount = document.getElementById('ledger-daily-count');
  const positionsTbody = document.getElementById('positions-tbody');
  const ordersTbody = document.getElementById('orders-tbody');
  const dailyTbody = document.getElementById('daily-tbody');

  // Paper-Truth (frozen baseline) Elements
  const truthVerdictTag = document.getElementById('truth-verdict-tag');
  const truthVerdict = document.getElementById('truth-verdict');
  const truthReason = document.getElementById('truth-reason');
  const truthTrades = document.getElementById('truth-trades');
  const truthWin = document.getElementById('truth-win');
  const truthPf = document.getElementById('truth-pf');
  const truthNet = document.getElementById('truth-net');
  const truthFees = document.getElementById('truth-fees');
  const truthDd = document.getElementById('truth-dd');
  const truthNote = document.getElementById('truth-note');

  // Top Nav Ticker & Action Elements
  const marketStatusDot = document.getElementById('market-status-dot');
  const marketStatusText = document.getElementById('market-status-text');
  const serverRamTag = document.getElementById('server-ram-tag');
  const serverCpuTag = document.getElementById('server-cpu-tag');
  const niftyVal = document.getElementById('nifty-val');
  const bankniftyVal = document.getElementById('banknifty-val');
  const sensexVal = document.getElementById('sensex-val');
  const vixVal = document.getElementById('vix-val');
  const btnPreMarketScan = document.getElementById('btn-pre-market-scan');
  const btnResetAccount = document.getElementById('btn-reset-account');
  const btnShowInstructions = document.getElementById('btn-show-instructions');
  const instructionsModal = document.getElementById('instructions-modal');
  const btnCloseModal = document.getElementById('btn-close-modal');
  const btnGotIt = document.getElementById('btn-got-it');
  const loginModal = document.getElementById('login-modal');
  const loginForm = document.getElementById('login-form');
  const loginUsername = document.getElementById('login-username');
  const loginPassword = document.getElementById('login-password');
  const loginErrorMsg = document.getElementById('login-error-msg');
  const userAuthPill = document.getElementById('user-auth-pill');
  const currentUsername = document.getElementById('current-username');
  const btnLogout = document.getElementById('btn-logout');

  // --- Authentication Interceptor ---
  const originalFetch = window.fetch;
  window.fetch = async function(input, init = {}) {
    init = init || {};
    init.headers = init.headers || {};
    if (authToken) {
      if (init.headers instanceof Headers) {
        if (!init.headers.has('Authorization')) init.headers.set('Authorization', `Bearer ${authToken}`);
        if (!init.headers.has('X-Auth-Token')) init.headers.set('X-Auth-Token', authToken);
      } else if (Array.isArray(init.headers)) {
        init.headers.push(['Authorization', `Bearer ${authToken}`]);
        init.headers.push(['X-Auth-Token', authToken]);
      } else {
        if (!init.headers['Authorization']) init.headers['Authorization'] = `Bearer ${authToken}`;
        if (!init.headers['X-Auth-Token']) init.headers['X-Auth-Token'] = authToken;
      }
    }
    const response = await originalFetch(input, init);
    const urlStr = typeof input === 'string' ? input : (input.url || '');
    if (response.status === 401 && !urlStr.includes('/api/auth/login') && !urlStr.includes('/api/auth/check')) {
      showLoginModal('Session expired. Please sign in again.');
    }
    return response;
  };

  function showLoginModal(msg = '') {
    if (loginErrorMsg) {
      if (msg) {
        loginErrorMsg.textContent = msg;
        loginErrorMsg.style.display = 'block';
      } else {
        loginErrorMsg.style.display = 'none';
      }
    }
    if (loginModal) loginModal.style.display = 'flex';
  }

  async function checkAuthStatus() {
    try {
      const res = await fetch('/api/auth/check');
      const data = await res.json();
      if (data && data.authenticated) {
        if (userAuthPill) userAuthPill.style.display = 'flex';
        if (currentUsername) currentUsername.textContent = data.username || 'admin';
        if (loginModal) loginModal.style.display = 'none';
        return true;
      }
    } catch (e) {
      console.warn('Auth check failed:', e);
    }
    showLoginModal();
    return false;
  }

  if (loginForm) {
    loginForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const u = loginUsername.value.trim();
      const p = loginPassword.value;
      if (!u || !p) return;
      try {
        const res = await originalFetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: u, password: p }),
        });
        const data = await res.json();
        if (res.ok && data.token) {
          authToken = data.token;
          localStorage.setItem('quant_auth_token', authToken);
          if (loginModal) loginModal.style.display = 'none';
          if (userAuthPill) userAuthPill.style.display = 'flex';
          if (currentUsername) currentUsername.textContent = data.username || u;
          initAll();
        } else {
          showLoginModal(data.detail || 'Invalid username or password.');
        }
      } catch (err) {
        showLoginModal('Connection error. Server may be offline.');
      }
    });
  }

  if (btnLogout) {
    btnLogout.addEventListener('click', async () => {
      try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (e) {}
      authToken = '';
      localStorage.removeItem('quant_auth_token');
      if (userAuthPill) userAuthPill.style.display = 'none';
      showLoginModal('Signed out.');
    });
  }

  // ==========================================================================
  // 1. CHART INITIALIZATION (TradingView Lightweight Charts)
  // ==========================================================================
  function initChart() {
    if (!tvChartContainer || !window.LightweightCharts) return;
    tvChartContainer.innerHTML = '';

    chart = LightweightCharts.createChart(tvChartContainer, {
      width: tvChartContainer.clientWidth,
      height: tvChartContainer.clientHeight || 420,
      layout: {
        background: { color: '#090e1a' },
        textColor: '#94a3b8',
        fontSize: 11,
        fontFamily: 'JetBrains Mono, monospace',
      },
      grid: {
        vertLines: { color: 'rgba(255, 255, 255, 0.04)' },
        horzLines: { color: 'rgba(255, 255, 255, 0.04)' },
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
      },
      rightPriceScale: {
        borderColor: 'rgba(255, 255, 255, 0.08)',
        scaleMargins: { top: 0.1, bottom: 0.2 },
      },
      timeScale: {
        borderColor: 'rgba(255, 255, 255, 0.08)',
        timeVisible: true,
        secondsVisible: false,
      },
    });

    candleSeries = chart.addCandlestickSeries({
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderVisible: false,
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
    });

    // 20 EMA (Blue)
    emaSeries = chart.addLineSeries({
      color: '#38bdf8',
      lineWidth: 2,
      priceLineVisible: false,
      title: 'EMA 20',
    });

    // 25 EMA (Yellow - BNF Mean Reversion)
    ema25Series = chart.addLineSeries({
      color: '#eab308',
      lineWidth: 1.5,
      priceLineVisible: false,
      title: 'EMA 25',
    });

    // 200 EMA (Purple - Institutional Trend)
    ema200Series = chart.addLineSeries({
      color: '#a855f7',
      lineWidth: 2,
      priceLineVisible: false,
      title: 'EMA 200',
    });

    // VWAP Line (Orange)
    vwapSeries = chart.addLineSeries({
      color: '#f59e0b',
      lineWidth: 2,
      priceLineVisible: false,
      title: 'VWAP',
    });

    // Volume Series
    volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: '',
      scaleMargins: { top: 0.84, bottom: 0 },
    });

    window.addEventListener('resize', () => {
      if (chart && tvChartContainer) {
        chart.applyOptions({
          width: tvChartContainer.clientWidth,
          height: tvChartContainer.clientHeight || 420,
        });
      }
    });

    loadCandles(activeChartSymbol, activeTimeframe, activePeriod);
  }

  // ==========================================================================
  // 2. LOAD CANDLES & RENDER INSTITUTIONAL OVERLAYS
  // ==========================================================================
  async function loadCandles(symbol, interval = '5m', period = '5d') {
    activeChartSymbol = symbol;
    activeTimeframe = interval;
    activePeriod = period;

    if (chartSymbolTitle) chartSymbolTitle.textContent = symbol;
    if (orderSymbolInput) orderSymbolInput.value = symbol;

    try {
      const startTime = performance.now();
      const res = await fetch(`/api/trading/candles?symbol=${encodeURIComponent(symbol)}&interval=${interval}&period=${period}`);
      const data = await res.json();
      const elapsed = Math.round(performance.now() - startTime);
      if (latencyTag) latencyTag.textContent = `Latency: ${elapsed}ms`;

      if (data.candles && data.candles.length) {
        candleSeries.setData(data.candles);

        if (data.ema20 && data.ema20.length) emaSeries.setData(data.ema20);
        if (data.ema25 && data.ema25.length && ema25Series) ema25Series.setData(data.ema25);
        if (data.ema200 && data.ema200.length && ema200Series) ema200Series.setData(data.ema200);
        if (data.vwap && data.vwap.length) vwapSeries.setData(data.vwap);

        if (volumeSeries) {
          const volData = data.candles.map(c => ({
            time: c.time,
            value: c.volume,
            color: c.close >= c.open ? 'rgba(34, 197, 94, 0.4)' : 'rgba(239, 68, 68, 0.4)',
          }));
          volumeSeries.setData(volData);
        }

        // Clear previous horizontal price lines
        activePriceLines.forEach(l => {
          try { candleSeries.removePriceLine(l); } catch (e) {}
        });
        activePriceLines = [];

        // Draw Pre-Market 4-Lines (PDH, PDL, PDC)
        if (data.four_lines) {
          const fl = data.four_lines;
          if (fl.PDH) activePriceLines.push(candleSeries.createPriceLine({ price: fl.PDH, color: '#f87171', lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: 'PDH' }));
          if (fl.PDL) activePriceLines.push(candleSeries.createPriceLine({ price: fl.PDL, color: '#4ade80', lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: 'PDL' }));
          if (fl.PDC) activePriceLines.push(candleSeries.createPriceLine({ price: fl.PDC, color: '#fbbf24', lineWidth: 1.5, lineStyle: 1, axisLabelVisible: true, title: 'PDC' }));
        }

        // Draw Volume Profile lines (POC, VAH, VAL)
        if (data.volume_profile) {
          const vp = data.volume_profile;
          if (vp.POC) activePriceLines.push(candleSeries.createPriceLine({ price: vp.POC, color: '#06b6d4', lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: 'POC' }));
          if (vp.VAH) activePriceLines.push(candleSeries.createPriceLine({ price: vp.VAH, color: '#38bdf8', lineWidth: 1.5, lineStyle: 3, axisLabelVisible: true, title: 'VAH' }));
          if (vp.VAL) activePriceLines.push(candleSeries.createPriceLine({ price: vp.VAL, color: '#38bdf8', lineWidth: 1.5, lineStyle: 3, axisLabelVisible: true, title: 'VAL' }));
        }

        // Attach Sweep & Execution Markers
        if (data.markers && data.markers.length) {
          candleSeries.setMarkers(data.markers);
        }

        // Header LTP & Change
        if (chartLtp) chartLtp.textContent = `₹ ${data.latest_price.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
        if (chartChange) {
          const sign = data.change_percent >= 0 ? '+' : '';
          const cls = data.change_percent >= 0 ? 'val-green' : 'val-red';
          chartChange.className = `chart-change-val ${cls}`;
          chartChange.textContent = `${sign}${data.change_percent}%`;
        }

        chart.timeScale().fitContent();

        // Refresh DeepSeek AI prediction for this symbol
        loadDeepSeekPrediction(symbol);
      }
    } catch (e) {
      console.error('Error loading candles:', e);
    }
  }

  // ==========================================================================
  // 3. LOAD DEEPSEEK AI PREDICTION & REASONING
  // ==========================================================================
  async function loadDeepSeekPrediction(symbol) {
    try {
      // 1. Fetch AI Trade Setup Signal
      const sigRes = await fetch('/api/trading/institutional/ai-signal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: symbol })
      });
      const sig = await sigRes.json();
      latestAiSignal = sig;

      // 2. Fetch GEX & Microstructure Overview
      const overviewRes = await fetch(`/api/trading/institutional/overview?symbol=${encodeURIComponent(symbol)}`);
      const overview = await overviewRes.json();

      // Render Hero Action Banner
      if (aiSigAction) {
        const act = sig.action || 'WAIT';
        aiSigAction.textContent = act;
        aiSigAction.className = `sig-action-badge ${act === 'BUY' ? 'sig-buy' : (act === 'SELL' ? 'sig-sell' : 'sig-wait')}`;
      }
      if (aiSigSetup) {
        aiSigSetup.textContent = (sig.setup_name || 'MONITORING MARKET RANGE').replace(/_/g, ' ');
      }
      if (aiSigGexRegime && overview && overview.gex) {
        aiSigGexRegime.textContent = `${overview.gex.regime} (PCR: ${overview.gex.pcr})`;
      }
      if (aiSigConf) {
        aiSigConf.textContent = `${sig.confidence_score || 85}%`;
      }

      // Render Geometry Levels Grid
      if (aiSigEntry) aiSigEntry.textContent = `₹ ${sig.entry_price.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
      if (aiSigSl) aiSigSl.textContent = `₹ ${sig.stop_loss.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
      if (aiSigTp) aiSigTp.textContent = `₹ ${sig.target_price.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
      if (aiSigRr) aiSigRr.textContent = `1:${sig.risk_reward_ratio || '2.0'} Net`;

      // Render Statutory Charges & Expected Net P&L
      if (aiSigFees && sig.statutory_charges) {
        aiSigFees.textContent = `₹ ${sig.statutory_charges.total_charges}`;
      }
      if (aiSigBe) {
        aiSigBe.textContent = `${sig.breakeven_points || 0.35} pts`;
      }
      if (aiSigNetPnl) {
        const netSign = (sig.expected_net_pnl || 0) >= 0 ? '+' : '';
        const netCls = (sig.expected_net_pnl || 0) >= 0 ? 'val-green' : 'val-red';
        aiSigNetPnl.className = `sub-fee-val ${netCls}`;
        aiSigNetPnl.textContent = `${netSign}₹ ${(sig.expected_net_pnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
      }

      // Render DeepSeek Reasoning Bullets
      if (aiReasoningBullets && sig.reasoning) {
        aiReasoningBullets.innerHTML = sig.reasoning.map(b => `<li>${b}</li>`).join('');
      } else if (overview && overview.attribution && overview.attribution.bullets) {
        aiReasoningBullets.innerHTML = overview.attribution.bullets.map(b => `<li>${b}</li>`).join('');
      }

    } catch (e) {
      console.error('Error fetching DeepSeek prediction:', e);
    }
  }

  // ==========================================================================
  // 4. MASTER P&L, PORTFOLIO & LEDGER TRACKER (Single Frame)
  // ==========================================================================
  async function loadPortfolioAndPnL() {
    try {
      const res = await fetch(`/api/trading/portfolio?account_id=${ACCOUNT_ID}`);
      const data = await res.json();
      if (!data) return;

      // The portfolio API returns rupee-suffixed keys (e.g. total_portfolio_value_inr);
      // the non-suffixed fallbacks keep older payloads working.
      const startCapital = Number(data.initial_capital_inr ?? 10000.0);
      const cash = Number(data.available_cash_inr ?? data.available_cash ?? startCapital);
      const totalEquity = Number(data.total_portfolio_value_inr ?? data.total_equity ?? cash);
      const realizedNet = Number(data.realized_pnl_inr ?? data.realized_pnl ?? 0.0);
      const unrealizedPnl = Number(data.unrealized_pnl_inr ?? data.unrealized_pnl ?? 0.0);
      const totalNet = Number(data.net_pnl_after_ai_inr ?? (totalEquity - startCapital));
      const netPct = startCapital > 0 ? (totalNet / startCapital) * 100 : 0;

      // Master Strip Updates
      if (pnlCapitalVal) pnlCapitalVal.textContent = `₹ ${totalEquity.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
      if (pnlNetRealizedVal) {
        const sign = realizedNet >= 0 ? '+' : '';
        const cls = realizedNet >= 0 ? 'val-green' : 'val-red';
        pnlNetRealizedVal.className = `pnl-val ${cls}`;
        pnlNetRealizedVal.textContent = `₹ ${realizedNet.toFixed(2)} (${sign}${netPct.toFixed(2)}%)`;
      }
      if (pnlUnrealizedVal) {
        const uSign = unrealizedPnl >= 0 ? '+' : '';
        const uCls = unrealizedPnl > 0 ? 'val-green' : (unrealizedPnl < 0 ? 'val-red' : 'val-muted');
        pnlUnrealizedVal.className = `pnl-val ${uCls}`;
        pnlUnrealizedVal.textContent = `₹ ${uSign}${unrealizedPnl.toFixed(2)}`;
      }

      const posList = data.positions || [];
      if (pnlOpenPositionsCount) pnlOpenPositionsCount.textContent = `${posList.length} Open Positions`;
      if (ledgerPosCount) ledgerPosCount.textContent = posList.length;

      // The 0.5% daily-goal bar and guardrail badges are driven by TODAY's
      // realized net P&L (from the risk-state endpoint) inside
      // loadDailyPerformance(), so they are not overwritten with all-time
      // equity here.

      // Render Positions Table
      if (positionsTbody) {
        if (!posList.length) {
          const executedCount = data.total_orders_executed || 0;
          if (executedCount > 0) {
            positionsTbody.innerHTML = `<tr><td colspan="6" class="table-empty">No active open positions. <strong>(${executedCount} orders executed today)</strong> — click <span style="color:var(--accent-blue);cursor:pointer;text-decoration:underline;" onclick="document.getElementById('tab-btn-history').click()">📜 Trade Ledger</span> to view.</td></tr>`;
          } else {
            positionsTbody.innerHTML = '<tr><td colspan="6" class="table-empty">No active open positions. Awaiting DeepSeek setup...</td></tr>';
          }
        } else {
          positionsTbody.innerHTML = posList.map(p => {
            const avgPrice = Number(p.avg_price ?? p.average_price ?? 0);
            const curPrice = Number(p.current_price ?? 0);
            const unPnl = Number(p.unrealized_pnl ?? 0);
            const pnlCls = unPnl >= 0 ? 'val-green' : 'val-red';
            const pnlSign = unPnl >= 0 ? '+' : '';
            return `
              <tr>
                <td><strong>${p.symbol}</strong></td>
                <td>${p.quantity}</td>
                <td>₹ ${avgPrice.toFixed(2)}</td>
                <td>₹ ${curPrice.toFixed(2)}</td>
                <td class="${pnlCls}"><strong>${pnlSign}₹ ${unPnl.toFixed(2)}</strong></td>
                <td><button class="nav-btn btn-logout btn-close-single" data-sym="${p.symbol}" style="padding:2px 6px;font-size:9px;">Close</button></td>
              </tr>
            `;
          }).join('');

          // Hook close single position buttons
          document.querySelectorAll('.btn-close-single').forEach(btn => {
            btn.addEventListener('click', async () => {
              const sym = btn.getAttribute('data-sym');
              await closePosition(sym);
            });
          });
        }
      }

      // Load Trade History Ledger
      loadOrdersHistory();

      // Load Per-Day P&L Summary
      loadDailyPerformance();

      // Load Frozen-Baseline Paper-Truth Report
      loadPaperTruth();

    } catch (e) {
      console.error('Error loading portfolio:', e);
    }
  }

  function formatIstTime(ts) {
    if (!ts) return '--:--';
    try {
      return new Date(ts).toLocaleTimeString('en-IN', {
        hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata',
      });
    } catch (e) {
      return String(ts).substring(11, 16);
    }
  }

  function istDateString() {
    try {
      return new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit',
      }).format(new Date());
    } catch (e) {
      return new Date().toISOString().substring(0, 10);
    }
  }

  async function loadOrdersHistory() {
    try {
      const res = await fetch(`/api/trading/orders?account_id=${ACCOUNT_ID}`);
      const data = await res.json();
      const orders = data.orders || [];
      if (ledgerOrdersCount) ledgerOrdersCount.textContent = orders.length;

      if (ordersTbody) {
        if (!orders.length) {
          ordersTbody.innerHTML = '<tr><td colspan="7" class="table-empty">No orders executed yet today.</td></tr>';
        } else {
          ordersTbody.innerHTML = orders.slice(0, 25).map(o => {
            const net = Number(o.pnl_inr ?? o.pnl ?? o.net_pnl ?? 0);
            const fees = Number(o.fee_inr ?? o.fees ?? 0);
            const gross = Number(o.gross_pnl_inr ?? 0);
            const isExit = o.action === 'SELL';
            const pnlCls = net > 0 ? 'val-green' : (net < 0 ? 'val-red' : 'val-muted');
            const pnlSign = net > 0 ? '+' : '';
            const t = formatIstTime(o.timestamp);
            const reason = o.exit_reason
              ? ` title="${o.exit_reason}${isExit ? ` • gross ₹${gross.toFixed(2)}` : ''}"`
              : '';
            return `
              <tr${reason}>
                <td>${t}</td>
                <td><strong>${o.symbol}</strong></td>
                <td class="${o.action === 'BUY' ? 'val-green' : 'val-red'}"><strong>${o.action}</strong></td>
                <td>${o.quantity ?? '-'}</td>
                <td>₹ ${Number(o.fill_price ?? o.price ?? 0).toFixed(2)}</td>
                <td style="color:var(--text-muted);">₹ ${fees.toFixed(2)}</td>
                <td class="${pnlCls}">${isExit ? `${pnlSign}₹ ${net.toFixed(2)}` : '—'}</td>
              </tr>
            `;
          }).join('');
        }
      }
    } catch (e) {
      console.error('Error loading orders history:', e);
    }
  }

  // ==========================================================================
  // 4b. PER-DAY PROFIT SUMMARY (Daily Performance Ledger)
  // ==========================================================================
  async function loadDailyPerformance() {
    try {
      const [dailyRes, riskRes] = await Promise.all([
        fetch(`/api/performance/daily?account_id=${ACCOUNT_ID}&limit=30`),
        fetch(`/api/trading/risk-state?account_id=${ACCOUNT_ID}`),
      ]);
      const dailyData = dailyRes.ok ? await dailyRes.json() : {};
      const risk = riskRes.ok ? await riskRes.json() : null;
      const history = dailyData.daily_history || [];

      // Live "today" figures from the risk manager (realized net after AI cost).
      const startEquity = Number((risk && risk.start_equity) ?? 10000.0);
      const todayRealized = risk ? Number(risk.realized_pnl_today_inr ?? 0) : 0.0;
      const todayNet = risk ? Number(risk.net_pnl_today_inr ?? todayRealized) : 0.0;
      const todayNetPct = startEquity > 0 ? (todayNet / startEquity) * 100 : 0.0;

      // Drive the 0.5% daily-goal bar with TODAY's realized net P&L.
      if (targetBarFill && targetCurrentPct) {
        targetCurrentPct.textContent = `${todayNetPct >= 0 ? '+' : ''}${todayNetPct.toFixed(2)}%`;
        const fillW = Math.min(100, Math.max(0, (todayNetPct / 0.50) * 100));
        targetBarFill.style.width = `${fillW}%`;
      }
      if (targetLockBadge) {
        const locked = risk ? Boolean(risk.locked) : false;
        if (todayNetPct >= 0.50 || locked) {
          targetLockBadge.className = 'badge-lock lock-halted';
          targetLockBadge.textContent = risk && risk.lock_reason ? 'TRADING LOCKED' : 'PROFIT GOAL LOCKED (+0.5%)';
        } else {
          targetLockBadge.className = 'badge-lock lock-active';
          targetLockBadge.textContent = 'TRADING ACTIVE';
        }
      }

      const rows = [];
      const todayStr = istDateString();
      const hasToday = history.some(h => String(h.date) === todayStr);
      if (risk && !hasToday) {
        rows.push({
          date: `${todayStr} (live)`,
          trades_count: risk.trades_today ?? 0,
          win_rate_pct: null,
          gross_pnl: null,
          fees_inr: null,
          net: todayRealized,
          net_return_pct: todayNetPct,
        });
      }
      for (const h of history) {
        rows.push({
          date: String(h.date),
          trades_count: h.trades_count ?? 0,
          win_rate_pct: h.win_rate_pct,
          gross_pnl: h.net_profit_before_ai ?? h.gross_pnl,
          fees_inr: h.fees_inr,
          net: h.net_profit_before_ai ?? h.realized_pnl,
          net_return_pct: h.net_return_pct,
        });
      }

      if (ledgerDailyCount) ledgerDailyCount.textContent = rows.length;
      if (dailyTbody) {
        if (!rows.length) {
          dailyTbody.innerHTML = '<tr><td colspan="7" class="table-empty">No completed trading days yet.</td></tr>';
        } else {
          dailyTbody.innerHTML = rows.map(r => {
            const net = Number(r.net ?? 0);
            const cls = net > 0 ? 'val-green' : (net < 0 ? 'val-red' : 'val-muted');
            const sign = net > 0 ? '+' : '';
            const pct = r.net_return_pct == null ? '—' : `${Number(r.net_return_pct) >= 0 ? '+' : ''}${Number(r.net_return_pct).toFixed(2)}%`;
            const fees = r.fees_inr == null ? '—' : `₹ ${Number(r.fees_inr).toFixed(2)}`;
            const gross = r.gross_pnl == null ? '—' : `₹ ${Number(r.gross_pnl).toFixed(2)}`;
            const win = r.win_rate_pct == null ? '—' : `${Number(r.win_rate_pct).toFixed(1)}`;
            return `
              <tr>
                <td>${r.date}</td>
                <td>${r.trades_count}</td>
                <td>${win}</td>
                <td>${gross}</td>
                <td>${fees}</td>
                <td class="${cls}"><strong>${sign}₹ ${net.toFixed(2)}</strong></td>
                <td class="${cls}">${pct}</td>
              </tr>
            `;
          }).join('');
        }
      }
    } catch (e) {
      console.error('Error loading daily performance:', e);
    }
  }

  // ==========================================================================
  // 4c. FROZEN-BASELINE PAPER-TRUTH REPORT (Live paper vs walk-forward)
  // ==========================================================================
  const TRUTH_VERDICT_META = {
    on_track: { label: 'ON TRACK', cls: 'val-green' },
    underperforming: { label: 'UNDERPERFORMING', cls: 'val-red' },
    insufficient_sample: { label: 'COLLECTING', cls: 'val-muted' },
  };

  async function loadPaperTruth() {
    try {
      const res = await fetch(`/api/performance/paper-truth?account_id=${ACCOUNT_ID}`);
      if (!res.ok) return;
      const data = await res.json();
      if (!data) return;

      const verdict = data.verdict || 'insufficient_sample';
      const meta = TRUTH_VERDICT_META[verdict] || TRUTH_VERDICT_META.insufficient_sample;
      const realized = data.realized || {};
      const expected = data.expected || {};
      const sample = data.sample || {};

      if (truthVerdictTag) {
        truthVerdictTag.textContent = meta.label;
        truthVerdictTag.className = `truth-verdict-tag ${meta.cls}`;
      }
      if (truthVerdict) {
        truthVerdict.textContent = meta.label;
        truthVerdict.className = `truth-verdict ${meta.cls}`;
      }
      if (truthReason) {
        const reasons = data.verdict_reasons || [];
        truthReason.textContent = verdict === 'insufficient_sample'
          ? (reasons[0] || 'Collecting closed trades; the verdict waits for a real sample.')
          : `Checks: ${reasons.join(', ')}`;
      }

      const closed = Number(sample.closed_trades ?? realized.closed_trades ?? 0);
      const minTrades = Number(sample.min_closed_trades_for_verdict ?? 30);
      if (truthTrades) truthTrades.textContent = `${closed} / ${minTrades}`;
      if (truthWin) truthWin.textContent = `${Number(realized.win_rate_pct ?? 0).toFixed(1)}% / ${Number(expected.win_rate_pct ?? 0).toFixed(1)}%`;
      if (truthPf) truthPf.textContent = `${Number(realized.profit_factor ?? 0).toFixed(2)} / ${Number(expected.profit_factor ?? 0).toFixed(2)}`;

      if (truthNet) {
        const net = Number(realized.net_pnl_inr ?? 0);
        truthNet.className = `truth-cell-value ${net > 0 ? 'val-green' : (net < 0 ? 'val-red' : 'val-muted')}`;
        truthNet.textContent = `${net >= 0 ? '+' : ''}₹ ${net.toFixed(2)}`;
      }
      if (truthFees) truthFees.textContent = `₹ ${Number(realized.fees_inr ?? 0).toFixed(2)}`;
      if (truthDd) truthDd.textContent = `${Number(realized.max_drawdown_pct ?? 0).toFixed(2)}%`;

      if (truthNote) {
        const folds = expected.positive_folds || '--';
        truthNote.innerHTML = `Frozen baseline <strong>${data.baseline_id || '--'}</strong> · walk-forward win ${Number(expected.win_rate_pct ?? 0).toFixed(1)}% · PF ${Number(expected.profit_factor ?? 0).toFixed(2)} · expectancy ${Number(expected.expectancy_pct_per_trade ?? 0).toFixed(2)}%/trade · ${folds} folds positive. The verdict is withheld until ${minTrades} closed trades.`;
      }
    } catch (e) {
      console.error('Error loading paper truth:', e);
    }
  }

  // ==========================================================================
  // 5. RAPID ORDER EXECUTION & 1-CLICK DEEPSEEK SETUP
  // ==========================================================================
  async function placeOrder(action, limitPrice = null, stopLoss = null, targetPrice = null) {
    const symbol = (orderSymbolInput.value || activeChartSymbol).trim().toUpperCase();
    const quantity = parseInt(fastQty.value) || 25;

    btnFastBuy.disabled = true;
    btnFastSell.disabled = true;

    try {
      const res = await fetch('/api/trading/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: symbol,
          action: action,
          quantity: quantity,
          order_type: limitPrice ? 'LIMIT' : 'MARKET',
          limit_price: limitPrice,
          stop_loss: stopLoss,
          target_price: targetPrice,
          account_id: ACCOUNT_ID,
        })
      });
      const data = await res.json();
      if (data.status === 'FILLED' || data.success) {
        loadPortfolioAndPnL();
        loadCandles(activeChartSymbol, activeTimeframe, activePeriod);
      } else {
        alert(`Order Not Executed: ${data.message || JSON.stringify(data)}`);
      }
    } catch (e) {
      alert(`Order error: ${e}`);
    } finally {
      btnFastBuy.disabled = false;
      btnFastSell.disabled = false;
    }
  }

  async function closePosition(symbol) {
    try {
      const res = await fetch('/api/trading/close-position', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: symbol, account_id: ACCOUNT_ID })
      });
      const data = await res.json();
      loadPortfolioAndPnL();
      loadCandles(activeChartSymbol, activeTimeframe, activePeriod);
    } catch (e) {
      alert(`Error closing position: ${e}`);
    }
  }

  if (btnFastBuy) btnFastBuy.addEventListener('click', () => placeOrder('BUY'));
  if (btnFastSell) btnFastSell.addEventListener('click', () => placeOrder('SELL'));
  if (btnCloseAll) btnCloseAll.addEventListener('click', () => closePosition(activeChartSymbol));

  // 1-Click Execute DeepSeek Setup
  if (btnExecuteAiSignal) {
    btnExecuteAiSignal.addEventListener('click', async () => {
      if (!latestAiSignal || latestAiSignal.action === 'PASS') {
        alert('DeepSeek AI recommendation is currently WAIT/PASS. Capital preservation rules prohibit low-probability trades in range chop.');
        return;
      }
      await placeOrder(
        latestAiSignal.action,
        null,
        latestAiSignal.stop_loss,
        latestAiSignal.target_price
      );
    });
  }

  if (btnRefreshAiSignal) {
    btnRefreshAiSignal.addEventListener('click', () => {
      loadDeepSeekPrediction(activeChartSymbol);
    });
  }

  // ==========================================================================
  // 6. CONTROLS, TABS & NAVIGATION
  // ==========================================================================
  // Quick Symbol Chips (NIFTY, BANKNIFTY, SENSEX, heavyweights)
  document.querySelectorAll('.chip-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.chip-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const sym = btn.getAttribute('data-sym');
      if (orderSymbolInput) orderSymbolInput.value = sym;
      if (fastQty) {
        if (sym === 'NIFTY') fastQty.value = 25;
        else if (sym === 'BANKNIFTY') fastQty.value = 15;
        else if (sym === 'SENSEX') fastQty.value = 10;
        else if (['TATASTEEL', 'COALINDIA', 'NTPC'].includes(sym)) fastQty.value = 10;
        else if (['ITC'].includes(sym)) fastQty.value = 5;
        else fastQty.value = 2; // SBIN, ICICIBANK, AXISBANK, TATAMOTORS
      }
      loadCandles(sym, activeTimeframe, activePeriod);
    });
  });

  // Timeframe Buttons (1m, 5m, 15m, 1h, 1D)
  document.querySelectorAll('.btn-tf').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.btn-tf').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const tf = btn.getAttribute('data-tf');
      const period = btn.getAttribute('data-period');
      loadCandles(activeChartSymbol, tf, period);
    });
  });

  // Ledger Card Tabs (Positions vs Trade Ledger vs Daily P&L vs Paper Truth)
  const ledgerTabs = [
    { btn: tabBtnPositions, panel: tabPositions },
    { btn: tabBtnHistory, panel: tabHistory },
    { btn: tabBtnDaily, panel: tabDaily },
    { btn: tabBtnTruth, panel: tabTruth },
  ];
  ledgerTabs.forEach(({ btn, panel }) => {
    if (!btn) return;
    btn.addEventListener('click', () => {
      ledgerTabs.forEach(other => {
        if (other.btn) other.btn.classList.remove('active');
        if (other.panel) other.panel.style.display = 'none';
      });
      btn.classList.add('active');
      if (panel) panel.style.display = 'block';
      if (panel === tabHistory) loadOrdersHistory();
      if (panel === tabDaily) loadDailyPerformance();
      if (panel === tabTruth) loadPaperTruth();
    });
  });

  // Pre-Market Scan Button
  if (btnPreMarketScan) {
    btnPreMarketScan.addEventListener('click', async () => {
      btnPreMarketScan.disabled = true;
      btnPreMarketScan.textContent = '🌅 Scanning...';
      try {
        const res = await fetch('/api/trading/pre-market-scan', { method: 'POST' });
        const data = await res.json();
        alert(`Pre-Market Intelligence Scan Complete:\n${data.summary || 'Market breadth analyzed successfully.'}`);
      } catch (e) {
        alert(`Pre-market scan error: ${e}`);
      } finally {
        btnPreMarketScan.disabled = false;
        btnPreMarketScan.textContent = '🌅 Pre-Market';
      }
    });
  }

  // Reset ₹10,000 Account Button
  if (btnResetAccount) {
    btnResetAccount.addEventListener('click', async () => {
      if (!confirm('Reset DeepSeek testing account to initial ₹10,000.00 capital?')) return;
      try {
        await fetch('/api/trading/reset-account', { method: 'POST' });
        loadPortfolioAndPnL();
      } catch (e) {
        alert(`Reset error: ${e}`);
      }
    });
  }

  // Guide Modal
  if (btnShowInstructions && instructionsModal) {
    btnShowInstructions.addEventListener('click', () => instructionsModal.style.display = 'flex');
  }
  if (btnCloseModal && instructionsModal) {
    btnCloseModal.addEventListener('click', () => instructionsModal.style.display = 'none');
  }
  if (btnGotIt && instructionsModal) {
    btnGotIt.addEventListener('click', () => instructionsModal.style.display = 'none');
  }

  // Market WebSocket Feed for Live Index Ticker
  function connectMarketWebSocket() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${window.location.host}/ws/market`;
    try {
      const ws = new WebSocket(wsUrl);
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'market_tick' || msg.type === 'market_snapshot') {
            if (msg.indices) updateIndices(msg.indices);
            if (msg.market_status) updateMarketStatus(msg.market_status);
            else if (msg.status) updateMarketStatus(msg.status);
          }
        } catch (e) {}
      };
      ws.onclose = () => setTimeout(connectMarketWebSocket, 3000);
      ws.onerror = () => ws.close();
    } catch (e) {}
  }

  function updateIndices(idx) {
    if (!idx) return;
    const n50 = idx.NIFTY_50 || idx.NIFTY || idx['^NSEI'];
    const sen = idx.SENSEX || idx['^BSESN'];
    const bnf = idx.BANK_NIFTY || idx.BANKNIFTY || idx['^NSEBANK'];
    const vix = idx.INDIA_VIX || idx.VIX || idx['^INDIAVIX'];

    if (n50 && niftyVal) renderIndexItem(niftyVal, n50);
    if (sen && sensexVal) renderIndexItem(sensexVal, sen);
    if (bnf && bankniftyVal) renderIndexItem(bankniftyVal, bnf);
    if (vix && vixVal) {
      const vixP = vix.price !== undefined ? vix.price : vix;
      vixVal.innerHTML = `<span>${typeof vixP === 'number' ? vixP.toFixed(2) : vixP}</span>`;
    }
  }

  function renderIndexItem(el, data) {
    const price = data.price !== undefined ? data.price : 0;
    const change = data.change !== undefined ? data.change : 0;
    const chgPct = data.change_pct !== undefined ? data.change_pct : 0;
    const cls = change >= 0 ? 'val-green' : 'val-red';
    const sign = change >= 0 ? '+' : '';
    el.innerHTML = `<span class="${cls}">${Number(price).toLocaleString('en-IN', {minimumFractionDigits: 1, maximumFractionDigits: 2})} (${sign}${chgPct}%)</span>`;
  }

  function updateMarketStatus(s) {
    if (!marketStatusDot || !marketStatusText) return;
    if (!s) return;
    const isOpen = (s.is_open === true || s.status === 'OPEN');
    marketStatusDot.className = `status-dot ${isOpen ? 'dot-open' : 'dot-closed'}`;
    marketStatusText.textContent = isOpen ? 'NSE/BSE Market Open' : (s.description || 'Market Closed');
  }

  // REST Fallbacks & System Metrics
  async function loadSystemHealth() {
    try {
      const res = await fetch('/api/health');
      if (!res.ok) return;
      const data = await res.json();
      if (data.system) {
        if (serverRamTag && data.system.ram) {
          serverRamTag.textContent = `RAM: ${data.system.ram.percent.toFixed(0)}%`;
        }
        if (serverCpuTag && data.system.cpu) {
          serverCpuTag.textContent = `CPU: ${data.system.cpu.percent.toFixed(0)}%`;
        }
      }
    } catch (e) {}
  }

  async function loadMarketStatus() {
    try {
      const res = await fetch('/api/trading/market-status');
      if (!res.ok) return;
      const data = await res.json();
      updateMarketStatus(data);
    } catch (e) {}
  }

  async function loadIndices() {
    try {
      const res = await fetch('/api/financial-advisor/indices');
      if (!res.ok) return;
      const data = await res.json();
      if (data.indices) {
        updateIndices(data.indices);
      }
    } catch (e) {}
  }

  // Periodic Refresh Loop (every 10s for real-time P&L, status, indices, metrics, and DeepSeek signal, every 30s for candles)
  setInterval(() => {
    loadPortfolioAndPnL();
    loadMarketStatus();
    loadIndices();
    loadSystemHealth();
    if (activeChartSymbol) {
      loadDeepSeekPrediction(activeChartSymbol);
    }
  }, 10000);

  setInterval(() => {
    if (activeChartSymbol) {
      loadCandles(activeChartSymbol, activeTimeframe, activePeriod);
    }
  }, 30000);

  // Initialize Terminal
  async function initAll() {
    initChart();
    await Promise.allSettled([
      loadPortfolioAndPnL(),
      loadMarketStatus(),
      loadIndices(),
      loadSystemHealth()
    ]);
    connectMarketWebSocket();
  }

  checkAuthStatus().then(authed => {
    if (authed) initAll();
  });

});

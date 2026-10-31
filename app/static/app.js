// Quant Trading Terminal & Dual Agent Arena Controller
// Manages TradingView Lightweight Charts, dynamic scanner, Claude Opus vs DeepSeek arena.

document.addEventListener('DOMContentLoaded', () => {

  // --- Top Navigation & Indices ---
  const niftyVal = document.getElementById('nifty-val');
  const sensexVal = document.getElementById('sensex-val');
  const bankNiftyVal = document.getElementById('banknifty-val');
  const vixVal = document.getElementById('vix-val');
  const marketStatusDot = document.getElementById('market-status-dot');
  const marketStatusText = document.getElementById('market-status-text');
  const leaderText = document.getElementById('leader-text');
  const serverRamTag = document.getElementById('server-ram-tag');
  const serverCpuTag = document.getElementById('server-cpu-tag');
  const btnTriggerArenaCycle = document.getElementById('btn-trigger-arena-cycle');
  const btnPreMarketScan = document.getElementById('btn-pre-market-scan');
  const btnRunBacktest = document.getElementById('btn-run-backtest');
  const btnResetAccount = document.getElementById('btn-reset-account');
  const btnShowInstructions = document.getElementById('btn-show-instructions');
  const instructionsModal = document.getElementById('instructions-modal');
  const btnCloseModal = document.getElementById('btn-close-modal');
  const btnGotIt = document.getElementById('btn-got-it');

  // --- Dual Arena Cards (Claude Opus vs DeepSeek) ---
  const opusCash = document.getElementById('opus-cash');
  const opusPnl = document.getElementById('opus-pnl');
  const opusWinrate = document.getElementById('opus-winrate');
  const opusSignal = document.getElementById('opus-signal');
  const opusSym = document.getElementById('opus-sym');
  const opusConf = document.getElementById('opus-conf');
  const opusEntry = document.getElementById('opus-entry');
  const opusTarget = document.getElementById('opus-target');
  const opusStop = document.getElementById('opus-stop');
  const opusRr = document.getElementById('opus-rr');
  const opusCatalyst = document.getElementById('opus-catalyst');
  const toggleAutoOpus = document.getElementById('toggle-auto-opus');
  const opusPositionsCount = document.getElementById('opus-positions-count');
  const opusModelBadge = document.getElementById('opus-model-badge');

  const deepseekCash = document.getElementById('deepseek-cash');
  const deepseekPnl = document.getElementById('deepseek-pnl');
  const deepseekWinrate = document.getElementById('deepseek-winrate');
  const deepseekSignal = document.getElementById('deepseek-signal');
  const deepseekSym = document.getElementById('deepseek-sym');
  const deepseekConf = document.getElementById('deepseek-conf');
  const deepseekEntry = document.getElementById('deepseek-entry');
  const deepseekTarget = document.getElementById('deepseek-target');
  const deepseekStop = document.getElementById('deepseek-stop');
  const deepseekRr = document.getElementById('deepseek-rr');
  const deepseekCatalyst = document.getElementById('deepseek-catalyst');
  const toggleAutoDeepseek = document.getElementById('toggle-auto-deepseek');
  const deepseekPositionsCount = document.getElementById('deepseek-positions-count');
  const deepseekModelBadge = document.getElementById('deepseek-model-badge');

  const argonCountdown = document.getElementById('argon-countdown');
  const arenaCurrentSymbol = document.getElementById('arena-current-symbol');

  // --- Chart Elements ---
  const chartSymbolTitle = document.getElementById('chart-symbol-title');
  const chartLtp = document.getElementById('chart-ltp');
  const chartChange = document.getElementById('chart-change');
  const tvChartContainer = document.getElementById('tv-chart-container');
  const orderAccountSelect = document.getElementById('order-account-select');
  const orderSymbolInput = document.getElementById('order-symbol-input');
  const fastQty = document.getElementById('fast-qty');
  const btnFastBuy = document.getElementById('btn-fast-buy');
  const btnFastSell = document.getElementById('btn-fast-sell');
  const latencyTag = document.getElementById('latency-tag');

  // --- Tabs, Scanner & Positions ---
  const tabBtnScanner = document.getElementById('tab-btn-scanner');
  const tabBtnPositions = document.getElementById('tab-btn-positions');
  const tabBtnOrders = document.getElementById('tab-btn-orders');
  const tabScanner = document.getElementById('tab-scanner');
  const tabPositions = document.getElementById('tab-positions');
  const tabOrders = document.getElementById('tab-orders');
  const btnRescanMarket = document.getElementById('btn-rescan-market');
  const scannerTbody = document.getElementById('scanner-tbody');
  const scannerCountBadge = document.getElementById('scanner-count-badge');
  const positionsTbody = document.getElementById('positions-tbody');
  const positionsCountBadge = document.getElementById('positions-count-badge');
  const ordersTbody = document.getElementById('orders-tbody');
  const learningContainer = document.getElementById('learning-container');

  // --- Chatbot & Token Metrics ---
  const chatMessages = document.getElementById('chat-messages');
  const chatInput = document.getElementById('chat-input');
  const btnSendChat = document.getElementById('btn-send-chat');
  const chatSearchToggle = document.getElementById('chat-search-toggle');
  const chatModelSelect = document.getElementById('chat-model-select');
  const tokensCount = document.getElementById('tokens-count');
  const tokensCost = document.getElementById('tokens-cost');
  const tokensTradingPnl = document.getElementById('tokens-trading-pnl');

  // --- Auth Elements ---
  const loginModal = document.getElementById('login-modal');
  const loginForm = document.getElementById('login-form');
  const loginUsername = document.getElementById('login-username');
  const loginPassword = document.getElementById('login-password');
  const loginErrorMsg = document.getElementById('login-error-msg');
  const btnLoginSubmit = document.getElementById('btn-login-submit');
  const userAuthPill = document.getElementById('user-auth-pill');
  const currentUsername = document.getElementById('current-username');
  const btnLogout = document.getElementById('btn-logout');

  // --- State Variables ---
  let authToken = localStorage.getItem('quant_auth_token') || '';
  let activeChartSymbol = 'SBIN';
  let activeTimeframe = '5m';
  let activePeriod = '5d';
  let nextCycleTime = null;
  let chart = null;
  let candleSeries = null;
  let emaSeries = null;
  let vwapSeries = null;
  let volumeSeries = null;
  let activePositionsFilter = 'all';

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

  function showLoginModal(error = '') {
    if (loginModal) {
      loginModal.style.display = 'flex';
      if (error && loginErrorMsg) {
        loginErrorMsg.textContent = error;
        loginErrorMsg.style.display = 'block';
      }
    }
  }

  function hideLoginModal() {
    if (loginModal) loginModal.style.display = 'none';
  }

  async function checkAuthStatus() {
    try {
      const res = await originalFetch('/api/auth/check', {
        headers: authToken ? { 'Authorization': `Bearer ${authToken}`, 'X-Auth-Token': authToken } : {}
      });
      const data = await res.json();
      if (!data.auth_enabled) {
        hideLoginModal();
        return true;
      }
      if (data.authenticated) {
        if (userAuthPill) userAuthPill.style.display = 'flex';
        if (currentUsername) currentUsername.textContent = data.username || 'admin';
        hideLoginModal();
        return true;
      } else {
        showLoginModal();
        return false;
      }
    } catch (e) {
      return false;
    }
  }

  if (loginForm) {
    loginForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const username = loginUsername.value.trim();
      const password = loginPassword.value;
      if (!username || !password) return;

      btnLoginSubmit.disabled = true;
      try {
        const res = await originalFetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password })
        });
        const data = await res.json();
        if (data.success && data.token) {
          authToken = data.token;
          localStorage.setItem('quant_auth_token', authToken);
          hideLoginModal();
          initAll();
        } else {
          showLoginModal(data.detail || 'Invalid administrator credentials.');
        }
      } catch (err) {
        showLoginModal('Connection error connecting to server.');
      } finally {
        btnLoginSubmit.disabled = false;
      }
    });
  }

  if (btnLogout) {
    btnLogout.addEventListener('click', async () => {
      try { await fetch('/api/auth/logout', { method: 'POST' }); } catch(e){}
      authToken = '';
      localStorage.removeItem('quant_auth_token');
      showLoginModal('Signed out.');
    });
  }

  // -------------------------------------------------------------
  // 1. TRADINGVIEW LIGHTWEIGHT CHARTS INITIALIZATION
  // -------------------------------------------------------------
  function initChart() {
    if (!tvChartContainer || !window.LightweightCharts) return;

    // Destroy existing chart if any
    tvChartContainer.innerHTML = '';

    chart = LightweightCharts.createChart(tvChartContainer, {
      width: tvChartContainer.clientWidth,
      height: tvChartContainer.clientHeight || 340,
      layout: {
        background: { type: 'solid', color: '#090d16' },
        textColor: '#94a3b8',
        fontSize: 11,
        fontFamily: "'JetBrains Mono', monospace",
      },
      grid: {
        vertLines: { color: 'rgba(255, 255, 255, 0.04)' },
        horzLines: { color: 'rgba(255, 255, 255, 0.04)' },
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
      },
      rightPriceScale: {
        borderColor: '#1e283d',
      },
      timeScale: {
        borderColor: '#1e283d',
        timeVisible: true,
        secondsVisible: false,
      },
    });

    // 1. Candlestick series
    candleSeries = chart.addCandlestickSeries({
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderVisible: false,
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
    });

    // 2. 20-EMA Line (Blue)
    emaSeries = chart.addLineSeries({
      color: '#38bdf8',
      lineWidth: 2,
      priceLineVisible: false,
      title: 'EMA 20',
    });

    // 3. VWAP Line (Orange)
    vwapSeries = chart.addLineSeries({
      color: '#f59e0b',
      lineWidth: 2,
      priceLineVisible: false,
      title: 'VWAP',
    });

    // 4. Volume Histogram
    volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: '',
      scaleMargins: { top: 0.82, bottom: 0 },
    });

    // Resize observer
    window.addEventListener('resize', () => {
      if (chart && tvChartContainer) {
        chart.applyOptions({
          width: tvChartContainer.clientWidth,
          height: tvChartContainer.clientHeight || 340,
        });
      }
    });

    loadCandles(activeChartSymbol, activeTimeframe, activePeriod);
  }

  async function loadCandles(symbol, interval = '5m', period = '5d') {
    activeChartSymbol = symbol;
    activeTimeframe = interval;
    activePeriod = period;

    if (chartSymbolTitle) chartSymbolTitle.textContent = symbol;
    if (orderSymbolInput) orderSymbolInput.value = symbol;

    try {
      const res = await fetch(`/api/trading/candles?symbol=${encodeURIComponent(symbol)}&interval=${interval}&period=${period}`);
      const data = await res.json();

      if (data.candles && data.candles.length) {
        candleSeries.setData(data.candles);

        if (data.ema20 && data.ema20.length) {
          emaSeries.setData(data.ema20);
        }
        if (data.vwap && data.vwap.length) {
          vwapSeries.setData(data.vwap);
        }
        if (volumeSeries) {
          const volData = data.candles.map(c => ({
            time: c.time,
            value: c.volume,
            color: c.close >= c.open ? 'rgba(34, 197, 94, 0.4)' : 'rgba(239, 68, 68, 0.4)',
          }));
          volumeSeries.setData(volData);
        }

        // Execution Markers (Green arrows for BUY, Red for SELL)
        if (data.markers && data.markers.length) {
          candleSeries.setMarkers(data.markers);
        }

        // Header LTP & Change
        if (chartLtp) chartLtp.textContent = `₹ ${data.latest_price.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
        if (chartChange) {
          const sign = data.change_percent >= 0 ? '+' : '';
          const cls = data.change_percent >= 0 ? 'val-green' : 'val-red';
          chartChange.className = `chart-chg-val ${cls}`;
          chartChange.textContent = `${sign}${data.change_percent}%`;
        }

        chart.timeScale().fitContent();
      }
    } catch (e) {
      console.error('Error loading candles:', e);
    }
  }

  // Chart quick symbol button chips
  document.querySelectorAll('.chip-sym').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.chip-sym').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const sym = btn.getAttribute('data-sym');
      loadCandles(sym, activeTimeframe, activePeriod);
    });
  });

  // Timeframe buttons (1m, 5m, 15m, 1h, 1D)
  document.querySelectorAll('.btn-tf').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.btn-tf').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const tf = btn.getAttribute('data-tf');
      const period = btn.getAttribute('data-period');
      loadCandles(activeChartSymbol, tf, period);
    });
  });

  // -------------------------------------------------------------
  // 2. DUAL AGENT ARENA UPDATES & REAL-TIME COMPARISON
  // -------------------------------------------------------------
  async function updateDualArena() {
    try {
      const res = await fetch('/api/trading/arena');
      const data = await res.json();
      renderArena(data);
    } catch (e) {
      console.error('Error fetching arena status:', e);
    }
  }

  function renderArena(data) {
    if (!data || !data.arena) return;
    const a = data.arena;

    // Leader banner
    if (leaderText && a.leader_statement) {
      leaderText.textContent = a.leader_statement;
    }

    // Account A: Claude Opus 4.6 Thinking
    const g = a.opus;
    if (g) {
      if (opusCash) opusCash.textContent = `₹ ${g.available_cash.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
      if (opusPnl) {
        const cls = g.realized_pnl >= 0 ? 'val-green' : 'val-red';
        const sign = g.realized_pnl >= 0 ? '+' : '';
        opusPnl.className = cls;
        opusPnl.textContent = `₹ ${sign}${g.realized_pnl.toFixed(2)} (${sign}${g.roc_percent}%)`;
      }
      if (opusWinrate) opusWinrate.textContent = `${g.win_rate_pct}% (${g.winning_trades}/${g.closed_trades})`;
      if (opusPositionsCount) opusPositionsCount.textContent = `${(g.open_positions || []).length} Open`;
      if (toggleAutoOpus) toggleAutoOpus.checked = g.auto_execute;
    }

    // Account B: DeepSeek Flash MaaS
    const d = a.deepseek;
    if (d) {
      if (deepseekCash) deepseekCash.textContent = `₹ ${d.available_cash.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
      if (deepseekPnl) {
        const cls = d.realized_pnl >= 0 ? 'val-green' : 'val-red';
        const sign = d.realized_pnl >= 0 ? '+' : '';
        deepseekPnl.className = cls;
        deepseekPnl.textContent = `₹ ${sign}${d.realized_pnl.toFixed(2)} (${sign}${d.roc_percent}%)`;
      }
      if (deepseekWinrate) deepseekWinrate.textContent = `${d.win_rate_pct}% (${d.winning_trades}/${d.closed_trades})`;
      if (deepseekPositionsCount) deepseekPositionsCount.textContent = `${(d.open_positions || []).length} Open`;
      if (toggleAutoDeepseek) toggleAutoDeepseek.checked = d.auto_execute;
    }

    // Claude Opus Prediction Card
    if (data.opus_prediction) {
      renderPredictionCard(data.opus_prediction, {
        signal: opusSignal,
        sym: opusSym,
        conf: opusConf,
        entry: opusEntry,
        target: opusTarget,
        stop: opusStop,
        rr: opusRr,
        catalyst: opusCatalyst,
        modelBadge: opusModelBadge,
      });
    }

    // DeepSeek Prediction Card
    if (data.deepseek_prediction) {
      renderPredictionCard(data.deepseek_prediction, {
        signal: deepseekSignal,
        sym: deepseekSym,
        conf: deepseekConf,
        entry: deepseekEntry,
        target: deepseekTarget,
        stop: deepseekStop,
        rr: deepseekRr,
        catalyst: deepseekCatalyst,
        modelBadge: deepseekModelBadge,
      });
    }

    if (data.next_run_timestamp) {
      nextCycleTime = new Date(data.next_run_timestamp);
    }
  }

  function renderPredictionCard(pred, els) {
    if (!pred || !els.signal) return;

    const sig = (pred.signal || 'HOLD').toUpperCase();
    els.signal.textContent = sig;
    els.signal.className = `signal-badge signal-${sig.toLowerCase()}`;

    if (els.sym) els.sym.textContent = pred.symbol || 'SBIN';
    if (els.conf) els.conf.textContent = `Conf: ${pred.confidence_pct || 75}%`;

    const entryPrice = pred.current_price_inr || pred.current_price || 0.0;
    if (els.entry) els.entry.textContent = `₹ ${entryPrice.toLocaleString('en-IN')}`;
    if (els.target) els.target.textContent = `₹ ${(pred.target_2_inr || pred.target_1_inr || entryPrice * 1.02).toLocaleString('en-IN')}`;
    if (els.stop) els.stop.textContent = `₹ ${(pred.stop_loss_inr || entryPrice * 0.99).toLocaleString('en-IN')}`;
    if (els.rr) els.rr.textContent = pred.risk_reward_ratio || '1:2.0';
    if (els.catalyst) els.catalyst.textContent = pred.catalyst || pred.action_summary || 'Standard trading setup.';
    if (els.modelBadge && pred.model) els.modelBadge.textContent = pred.model;

    if (arenaCurrentSymbol) arenaCurrentSymbol.textContent = pred.symbol || 'SBIN';
  }

  // Auto-execute toggles
  if (toggleAutoOpus) {
    toggleAutoOpus.addEventListener('change', async () => {
      await fetch(`/api/trading/auto-toggle?account_id=opus_flash&enabled=${toggleAutoOpus.checked}`, { method: 'POST' });
    });
  }
  if (toggleAutoDeepseek) {
    toggleAutoDeepseek.addEventListener('change', async () => {
      await fetch(`/api/trading/auto-toggle?account_id=deepseek_flash&enabled=${toggleAutoDeepseek.checked}`, { method: 'POST' });
    });
  }

  // Trigger Arena Cycle Button
  if (btnTriggerArenaCycle) {
    btnTriggerArenaCycle.addEventListener('click', async () => {
      btnTriggerArenaCycle.disabled = true;
      btnTriggerArenaCycle.textContent = '⚔️ Evaluating...';
      try {
        const res = await fetch(`/api/trading/arena/trigger?symbol=${encodeURIComponent(activeChartSymbol)}`, { method: 'POST' });
        const data = await res.json();
        renderArena({
          arena: data.arena,
          opus_prediction: data.opus_prediction,
          deepseek_prediction: data.deepseek_prediction,
          next_run_timestamp: data.next_run_timestamp,
        });
        appendChatMessage('System', `⚔️ AI Arena cycle executed on ${activeChartSymbol}: Claude Opus [${data.opus_prediction.signal} ${data.opus_prediction.confidence_pct}%] vs DeepSeek [${data.deepseek_prediction.signal} ${data.deepseek_prediction.confidence_pct}%]`, 'system');
      } catch (e) {
        console.error('Arena trigger error:', e);
      } finally {
        btnTriggerArenaCycle.disabled = false;
        btnTriggerArenaCycle.textContent = '⚔️ Run Arena Cycle';
      }
    });
  }

  // -------------------------------------------------------------
  // 3. DYNAMIC MULTI-STOCK SCANNER
  // -------------------------------------------------------------
  async function loadScannerResults() {
    try {
      const res = await fetch('/api/trading/scanner');
      const data = await res.json();
      renderScannerTable(data.opportunities || []);
    } catch (e) {
      console.error('Scanner load error:', e);
    }
  }

  function renderScannerTable(items) {
    if (!scannerTbody) return;
    if (!items || !items.length) {
      scannerTbody.innerHTML = '<tr><td colspan="7" class="hint-text text-center">No breakout setups currently active. Click Re-Scan to scan 30+ stocks.</td></tr>';
      return;
    }

    if (scannerCountBadge) scannerCountBadge.textContent = items.length;
    scannerTbody.innerHTML = '';

    items.forEach(item => {
      const sym = item.symbol;
      const price = item.current_price;
      const chg = item.change_percent;
      const cls = chg >= 0 ? 'val-green' : 'val-red';
      const sign = chg >= 0 ? '+' : '';

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong class="clickable-sym" onclick="selectStockForChart('${sym}')">${sym}</strong></td>
        <td>₹ ${price.toLocaleString('en-IN', { minimumFractionDigits: 2 })}</td>
        <td class="${cls}">${sign}${chg.toFixed(2)}%</td>
        <td><span class="badge badge-accent">${item.setup_type}</span></td>
        <td><strong>${item.score}/100</strong></td>
        <td>SL: ₹${item.stop_loss} / Tgt: ₹${item.target_1}</td>
        <td>
          <button class="btn btn-small btn-primary" onclick="tradeScannedStock('${sym}')">⚡ Trade</button>
        </td>
      `;
      scannerTbody.appendChild(tr);
    });
  }

  window.selectStockForChart = (sym) => {
    loadCandles(sym, activeTimeframe, activePeriod);
  };

  window.tradeScannedStock = (sym) => {
    loadCandles(sym, activeTimeframe, activePeriod);
    if (orderSymbolInput) orderSymbolInput.value = sym;
  };

  if (btnRescanMarket) {
    btnRescanMarket.addEventListener('click', async () => {
      btnRescanMarket.disabled = true;
      btnRescanMarket.textContent = '🔍 Scanning 30+ Stocks...';
      try {
        const res = await fetch('/api/trading/scanner/run?limit=10', { method: 'POST' });
        const data = await res.json();
        renderScannerTable(data.opportunities || []);
        appendChatMessage('System', `🔍 Scanned 30+ liquid Indian equities: Found ${data.count} high-momentum breakout candidates!`, 'system');
      } catch (e) {
        console.error('Scan error:', e);
      } finally {
        btnRescanMarket.disabled = false;
        btnRescanMarket.textContent = '🔍 Re-Scan';
      }
    });
  }

  // -------------------------------------------------------------
  // 4. MULTI-AGENT POSITIONS & ORDER HISTORY
  // -------------------------------------------------------------
  async function loadPositionsAndOrders() {
    try {
      // 1. Load Claude Opus & DeepSeek positions
      const [gRes, dRes] = await Promise.all([
        fetch('/api/trading/portfolio?account_id=opus_flash'),
        fetch('/api/trading/portfolio?account_id=deepseek_flash')
      ]);
      const gData = await gRes.json();
      const dData = await dRes.json();

      const allPositions = [
        ...(gData.positions || []).map(p => ({ ...p, account_id: 'opus_flash', agent_name: 'Claude Opus 4.6 Thinking' })),
        ...(dData.positions || []).map(p => ({ ...p, account_id: 'deepseek_flash', agent_name: 'DeepSeek Flash MaaS' }))
      ];

      renderPositionsTable(allPositions);

      // 2. Load Trade Orders
      const oRes = await fetch('/api/trading/orders');
      const oData = await oRes.json();
      renderOrdersTable(oData.orders || []);

    } catch (e) {
      console.error('Positions/Orders load error:', e);
    }
  }

  function renderPositionsTable(positions) {
    if (!positionsTbody) return;
    if (positionsCountBadge) positionsCountBadge.textContent = `${positions.length} Open`;

    const filtered = activePositionsFilter === 'all'
      ? positions
      : positions.filter(p => p.account_id === activePositionsFilter);

    if (!filtered.length) {
      positionsTbody.innerHTML = '<tr><td colspan="7" class="hint-text text-center">No open positions. Use the order bar above.</td></tr>';
      return;
    }

    positionsTbody.innerHTML = '';
    filtered.forEach(pos => {
      const isOpus = pos.account_id === 'opus_flash';
      const agentBadgeCls = isOpus ? 'badge-accent' : 'badge-done';
      const pnlCls = pos.unrealized_pnl >= 0 ? 'val-green' : 'val-red';
      const sign = pos.unrealized_pnl >= 0 ? '+' : '';

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><span class="badge ${agentBadgeCls}">${isOpus ? 'Claude Opus' : 'DeepSeek'}</span></td>
        <td><strong>${pos.symbol}</strong></td>
        <td>${pos.quantity}</td>
        <td>₹ ${pos.avg_price}</td>
        <td>₹ ${pos.current_price}</td>
        <td class="${pnlCls}">${sign}₹ ${pos.unrealized_pnl} (${sign}${pos.unrealized_pnl_pct}%)</td>
        <td>
          <button class="btn btn-small btn-danger" onclick="closePositionForAccount('${pos.symbol}', '${pos.account_id}')">Close</button>
        </td>
      `;
      positionsTbody.appendChild(tr);
    });
  }

  window.closePositionForAccount = async (sym, accountId) => {
    try {
      const res = await fetch('/api/trading/close-position', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: sym, account_id: accountId })
      });
      const data = await res.json();
      if (data.status === 'FILLED') {
        appendChatMessage('System', `⚡ [${accountId}] Closed position: SOLD ${data.quantity} ${data.symbol} at ₹${data.fill_price}!`, 'system');
        loadPositionsAndOrders();
        updateDualArena();
        loadCandles(activeChartSymbol, activeTimeframe, activePeriod);
      }
    } catch (e) {
      console.error('Error closing position:', e);
    }
  };

  function renderOrdersTable(orders) {
    if (!ordersTbody) return;
    if (!orders.length) {
      ordersTbody.innerHTML = '<tr><td colspan="8" class="hint-text text-center">No orders executed yet.</td></tr>';
      return;
    }

    ordersTbody.innerHTML = '';
    orders.slice(0, 30).forEach(o => {
      const isOpus = o.account_id === 'opus_flash';
      const isBuy = o.action === 'BUY';
      const timeStr = o.timestamp ? new Date(o.timestamp).toLocaleTimeString() : '--';

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${timeStr}</td>
        <td><span class="badge ${isOpus ? 'badge-accent' : 'badge-done'}">${isOpus ? 'Claude Opus' : 'DeepSeek'}</span></td>
        <td><strong>${o.symbol}</strong></td>
        <td class="${isBuy ? 'val-green' : 'val-red'}"><strong>${o.action}</strong></td>
        <td>${o.quantity}</td>
        <td>₹ ${o.fill_price}</td>
        <td class="${(o.pnl_inr || 0) >= 0 ? 'val-green' : 'val-red'}">${o.pnl_inr ? '₹ ' + o.pnl_inr.toFixed(2) : '--'}</td>
        <td><span class="badge badge-done">${o.status}</span></td>
      `;
      ordersTbody.appendChild(tr);
    });
  }

  // Filter chips for positions (All, Claude Opus, DeepSeek)
  document.querySelectorAll('.chip-filter').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.chip-filter').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activePositionsFilter = btn.getAttribute('data-acc');
      loadPositionsAndOrders();
    });
  });

  // Tab switching (Scanner vs Positions vs Orders)
  const tabs = [
    { btn: tabBtnScanner, content: tabScanner },
    { btn: tabBtnPositions, content: tabPositions },
    { btn: tabBtnOrders, content: tabOrders },
  ];
  tabs.forEach(t => {
    if (t.btn) {
      t.btn.addEventListener('click', () => {
        tabs.forEach(x => {
          if (x.btn) x.btn.classList.remove('active');
          if (x.content) x.content.style.display = 'none';
        });
        t.btn.classList.add('active');
        if (t.content) t.content.style.display = 'flex';
      });
    }
  });

  // -------------------------------------------------------------
  // 5. CHART ORDER PLACEMENT
  // -------------------------------------------------------------
  async function executeOrder(action) {
    const symbol = (orderSymbolInput.value || activeChartSymbol).trim().toUpperCase();
    const quantity = parseInt(fastQty.value) || 1;
    const account_id = orderAccountSelect.value || 'opus_flash';

    if (latencyTag) latencyTag.textContent = 'Executing...';
    btnFastBuy.disabled = true;
    btnFastSell.disabled = true;

    try {
      const res = await fetch('/api/trading/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, action, quantity, account_id, order_type: 'MARKET' })
      });
      const data = await res.json();

      if (data.status === 'FILLED') {
        if (latencyTag) latencyTag.textContent = `Executed: ${data.latency_ms}ms ⚡`;
        appendChatMessage('System', `⚡ ORDER FILLED (${account_id}): ${data.action} ${data.quantity} ${data.symbol} @ ₹${data.fill_price} | ID: ${data.order_id}`, 'system');
        loadPositionsAndOrders();
        updateDualArena();
        loadCandles(symbol, activeTimeframe, activePeriod);
      } else {
        if (latencyTag) latencyTag.textContent = 'Rejected';
        appendChatMessage('System', `❌ Order Rejected (${account_id}): ${data.reason || 'Insufficient funds or quantity'}`, 'system');
      }
    } catch (e) {
      if (latencyTag) latencyTag.textContent = 'Error';
    } finally {
      btnFastBuy.disabled = false;
      btnFastSell.disabled = false;
    }
  }

  if (btnFastBuy) btnFastBuy.addEventListener('click', () => executeOrder('BUY'));
  if (btnFastSell) btnFastSell.addEventListener('click', () => executeOrder('SELL'));

  // Reset Account Button
  if (btnResetAccount) {
    btnResetAccount.addEventListener('click', async () => {
      if (confirm('Reset both Claude Opus & DeepSeek accounts back to ₹10,000 initial capital?')) {
        await fetch('/api/trading/reset-account?capital=10000', { method: 'POST' });
        updateDualArena();
        loadPositionsAndOrders();
        loadCandles(activeChartSymbol, activeTimeframe, activePeriod);
        appendChatMessage('System', '🔄 Dual Arena testing accounts reset fresh to ₹10,000 credits each.', 'system');
      }
    });
  }

  // Pre-Market Scan Button
  if (btnPreMarketScan) {
    btnPreMarketScan.addEventListener('click', async () => {
      btnPreMarketScan.disabled = true;
      btnPreMarketScan.textContent = '🌅 Scanning...';
      try {
        const res = await fetch('/api/trading/pre-market-scan', { method: 'POST' });
        const data = await res.json();
        appendChatMessage('Quant Advisor', `🌅 Pre-Market Analysis Complete: ${data.analysis || 'High momentum sectors identified.'}`, 'bot');
      } catch (e) {
        console.error(e);
      } finally {
        btnPreMarketScan.disabled = false;
        btnPreMarketScan.textContent = '🌅 Pre-Market';
      }
    });
  }

  // Historical Backtest Button (DeepSeek Flash MaaS)
  if (btnRunBacktest) {
    btnRunBacktest.addEventListener('click', async () => {
      btnRunBacktest.disabled = true;
      btnRunBacktest.textContent = '🧪 Backtesting...';
      appendChatMessage('System', '🧪 Running Intraday Backtest across past 5-minute data with DeepSeek Flash MaaS...', 'system');
      try {
        const res = await fetch('/api/trading/backtest', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbols: ['RELIANCE', 'TCS', 'INFY', 'VEDL', 'SBIN', 'ICICIBANK'], period: '5d', use_deepseek: true, time_filter: true })
        });
        const data = await res.json();
        const net = Number(data.net_pnl_after_ai_inr ?? data.net_pnl_inr ?? 0);
        const pnlStr = (net >= 0 ? '+' : '') + '₹' + net.toFixed(2);
        const top = data.top_strategy ? ` | Top Strategy: ${data.top_strategy}` : '';
        appendChatMessage('DeepSeek Quant', `✅ Backtest Complete: ${data.total_trades} Trades | Win Rate: ${data.win_rate_pct}% | Profit Factor: ${data.profit_factor} | Net after AI: ${pnlStr} | Max Drawdown: ${data.max_drawdown_pct}%${top}`, 'bot');
        await loadLearningLogs();
      } catch (e) {
        console.error('Backtest error:', e);
        appendChatMessage('System', `❌ Backtest failed: ${e.message}`, 'error');
      } finally {
        btnRunBacktest.disabled = false;
        btnRunBacktest.textContent = '🧪 Backtest (DeepSeek)';
      }
    });
  }

  // -------------------------------------------------------------
  // 6. CHATBOT & LEARNING LESSONS
  // -------------------------------------------------------------
  async function loadLearningLogs() {
    try {
      const res = await fetch('/api/learning/insights?limit=15');
      const data = await res.json();
      if (!learningContainer) return;
      if (!data.insights || !data.insights.length) {
        learningContainer.innerHTML = '<div class="hint-text text-center" style="padding: 10px;">No trade retrospectives recorded yet.</div>';
        return;
      }
      learningContainer.innerHTML = '';
      data.insights.forEach(l => {
        const div = document.createElement('div');
        div.className = 'learning-item';
        div.innerHTML = `
          <div class="learning-header">
            <span class="learning-cat">[${l.category}] ${l.symbol_ref ? '• ' + l.symbol_ref : ''}</span>
            <span class="learning-time">${new Date(l.timestamp).toLocaleTimeString()}</span>
          </div>
          <div class="learning-text">${l.insight}</div>
        `;
        learningContainer.appendChild(div);
      });
    } catch (e){}
  }

  async function loadTokensSummary() {
    try {
      const res = await fetch('/api/tokens/summary');
      const data = await res.json();
      const maxToks = data.max_daily_tokens || 10500000;
      const maxStr = maxToks >= 1000000 ? `${(maxToks / 1000000).toFixed(1)}M` : `${Math.round(maxToks / 1000)}K`;
      if (tokensCount) {
        tokensCount.textContent = `${(data.today_tokens_used || 0).toLocaleString()} / ${maxStr}`;
        tokensCount.title = `Input: ${(data.today_prompt_tokens || 0).toLocaleString()} / 10M | Output: ${(data.today_completion_tokens || 0).toLocaleString()} / 500K`;
      }
      if (tokensCost) tokensCost.textContent = `₹ ${(data.estimated_ai_cost_inr || 0).toFixed(2)}`;
      if (tokensTradingPnl) tokensTradingPnl.textContent = `₹ ${(data.gross_trading_pnl_inr || 0).toFixed(2)}`;
    } catch(e){}
  }

  function appendChatMessage(sender, text, type = 'user') {
    if (!chatMessages) return;
    const msg = document.createElement('div');
    msg.className = `chat-msg ${type}-msg`;
    msg.innerHTML = `<span class="sender">${sender}:</span> <span class="text">${text}</span>`;
    chatMessages.appendChild(msg);
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  async function sendChatMessage() {
    const text = chatInput.value.trim();
    if (!text) return;

    appendChatMessage('You', text, 'user');
    chatInput.value = '';
    btnSendChat.disabled = true;

    try {
      const model = chatModelSelect.value || 'deepseek-ai/deepseek-v4.1-flash-maas';
      const use_search = chatSearchToggle.checked;
      const res = await fetch('/api/chatbot/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, model, use_search })
      });
      const data = await res.json();
      const botReply = data.response || data.reply || (data.error ? ('Error: ' + data.error) : 'No response.');
      appendChatMessage('Quant Chatbot', botReply, 'bot');

      if (data.order_executed || data.trade_result) {
        loadPositionsAndOrders();
        updateDualArena();
        loadCandles(activeChartSymbol, activeTimeframe, activePeriod);
      }
    } catch (e) {
      appendChatMessage('System', 'Chat error: ' + e.message, 'system');
    } finally {
      btnSendChat.disabled = false;
    }
  }

  if (btnSendChat) btnSendChat.addEventListener('click', sendChatMessage);
  if (chatInput) {
    chatInput.addEventListener('keypress', (e) => {
      if (e.key === 'Enter') sendChatMessage();
    });
  }

  // -------------------------------------------------------------
  // 7. WEBSOCKETS & PERIODIC REFRESH
  // -------------------------------------------------------------
  function connectMarketWebSocket() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/market`);

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'market_tick') {
          if (data.indices) updateIndices(data.indices);
          if (data.market_status) updateMarketStatus(data.market_status);
        } else if (data.type === 'arena_update') {
          renderArena(data);
          loadPositionsAndOrders();
        }
      } catch (e) {}
    };

    ws.onclose = () => {
      setTimeout(connectMarketWebSocket, 3000);
    };
  }

  function updateIndices(idx) {
    if (idx.NIFTY_50 && niftyVal) renderIndexItem(niftyVal, idx.NIFTY_50);
    if (idx.SENSEX && sensexVal) renderIndexItem(sensexVal, idx.SENSEX);
    if (idx.BANK_NIFTY && bankNiftyVal) renderIndexItem(bankNiftyVal, idx.BANK_NIFTY);
    if (idx.INDIA_VIX && vixVal) vixVal.innerHTML = `<span>${idx.INDIA_VIX.price}</span>`;
  }

  function renderIndexItem(el, data) {
    const cls = data.change >= 0 ? 'val-green' : 'val-red';
    const sign = data.change >= 0 ? '+' : '';
    el.innerHTML = `<span class="${cls}">${data.price.toLocaleString('en-IN')} (${sign}${data.change_pct}%)</span>`;
  }

  function updateMarketStatus(s) {
    if (!marketStatusDot || !marketStatusText) return;
    marketStatusDot.className = `status-dot ${s.is_open ? 'dot-open' : 'dot-closed'}`;
    marketStatusText.textContent = s.status_text || (s.is_open ? 'Market Open (NSE/BSE)' : 'Market Closed');
  }

  // 5-minute Countdown Timer
  setInterval(() => {
    if (!nextCycleTime || !argonCountdown) return;
    const diff = Math.max(0, Math.floor((nextCycleTime - new Date()) / 1000));
    const mins = String(Math.floor(diff / 60)).padStart(2, '0');
    const secs = String(diff % 60).padStart(2, '0');
    argonCountdown.textContent = `${mins}:${secs}`;
    if (diff === 0) {
      updateDualArena();
    }
  }, 1000);

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

  // Initialize Terminal
  async function initAll() {
    initChart();
    await updateDualArena();
    await loadScannerResults();
    await loadPositionsAndOrders();
    await loadLearningLogs();
    await loadTokensSummary();
    connectMarketWebSocket();
  }

  checkAuthStatus().then(authed => {
    if (authed) initAll();
  });

});

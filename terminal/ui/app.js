import { sparkline, lineChart, candles, bars } from '/static/charts.js';

// ------------------------------------------------------------------ helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const fmt = (n, d = 0) => (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
const inr = (n, d = 0) => (n == null || isNaN(n)) ? '—' : (n < 0 ? '-' : '') + '₹' + fmt(Math.abs(n), d);
const pct = n => (n == null || isNaN(n)) ? '—' : (n >= 0 ? '+' : '') + Number(n).toFixed(2) + '%';
const sign = n => n > 0 ? 'pos' : (n < 0 ? 'neg' : '');
const ts = t => t ? new Date(t * 1000).toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' }) : '—';
const dts = t => t ? new Date(t * 1000).toLocaleString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' }) : '—';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const token = () => localStorage.getItem('terminal_token') || '';

async function api(path, method = 'GET', body) {
  const headers = { 'Content-Type': 'application/json' };
  if (token()) headers.Authorization = 'Bearer ' + token();
  const r = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  let data = null; try { data = await r.json(); } catch { }
  if (r.status === 401) { showLogin(); throw new Error('AUTH_REQUIRED'); }
  if (!r.ok || (data && data.ok === false)) throw new Error((data && (data.error || data.detail)) || r.statusText);
  return data ? data.data : null;
}

function toast(msg, cls = '') { const el = document.createElement('div'); el.className = 'toast ' + cls; el.textContent = msg; $('#toasts').appendChild(el); setTimeout(() => el.remove(), 4500); }
function confirmModal(html) {
  return new Promise(res => { $('#modal-body').innerHTML = html; $('#modal').classList.remove('hidden'); const done = v => { $('#modal').classList.add('hidden'); res(v); }; $('#modal-ok').onclick = () => done(true); $('#modal-cancel').onclick = () => done(false); });
}
async function act(fn, okMsg) { try { const r = await fn(); if (okMsg) toast(okMsg, 'ok'); return r; } catch (e) { toast(e.message, 'err'); } }

// ------------------------------------------------------------------ state
const S = { snap: null, view: 'overview', user: null, chain: null, chainUnder: 'NIFTY', chainExpiry: null, hist: {}, preview: null, perf: null, backtest: null, health: null, logs: [], audit: [], council: [], strategies: null, settings: null, agents: null, events: [] };

function showLogin() { $('#login').classList.remove('hidden'); }
async function boot() {
  try {
    const me = await api('/api/auth/me');
    if (!me.user) { showLogin(); return; }
    S.user = me.user; $('#sb-user').textContent = `${me.user.username} (${me.user.role})`;
    $('#login').classList.add('hidden');
    mountAll(); connectWS();
  } catch (e) { showLogin(); }
}
$('#login-btn').onclick = async () => {
  const tok = $('#login-token').value.trim();
  if (tok) { localStorage.setItem('terminal_token', tok); return boot(); }
  try { const r = await api('/api/auth/login', 'POST', { username: $('#login-user').value, password: $('#login-pass').value }); localStorage.setItem('terminal_token', r.token); boot(); }
  catch (e) { $('#login-err').textContent = e.message; }
};

let ws, wsRetry = 1000;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws${token() ? '?token=' + encodeURIComponent(token()) : ''}`);
  ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.type === 'snapshot') { onSnapshot(m.data); } else if (m.type === 'event') onEvent(m.topic, m.payload); };
  ws.onopen = () => { wsRetry = 1000; };
  ws.onclose = () => { $('#sb-feed').textContent = 'reconnecting'; setTimeout(connectWS, wsRetry); wsRetry = Math.min(wsRetry * 2, 15000); };
  setInterval(() => { if (ws.readyState === 1) ws.send('ping'); }, 20000);
}
function onEvent(topic, p) {
  if (topic === 'alert' && p && p.level !== 'INFO') toast(`${p.level}: ${p.title}`, p.level === 'CRITICAL' ? 'err' : '');
  if (topic === 'plan.proposed') toast('Council proposed a trade — see Approvals', 'ok');
  if (topic === 'order.filled') toast(`Filled ${p.side} ${p.lots}L ${p.symbol} @ ${p.filled_price}`, 'ok');
  if (topic === 'log' && S.view === 'system') { S.logs.unshift(p); S.logs = S.logs.slice(0, 400); renderLogs(); }
  if (topic === 'council.brief' && S.view === 'council') views.council.render(S.snap);
}

function onSnapshot(snap) {
  S.snap = snap;
  for (const m of snap.market) { (S.hist[m.symbol] ||= []).push(m.ltp); if (S.hist[m.symbol].length > 120) S.hist[m.symbol].shift(); }
  renderChrome(snap);
  const v = views[S.view]; if (v && v.render) v.render(snap);
}

// ------------------------------------------------------------------ chrome
function renderChrome(s) {
  $('#version').textContent = 'v' + s.version;
  $$('#mode-seg button').forEach(b => b.classList.toggle('active', b.dataset.mode === s.mode));
  const env = $('#env-badge'); env.textContent = s.env + (s.env === 'LIVE' ? ' ●' : ''); env.className = 'pill env ' + s.env;
  const r = s.risk.snapshot; const rb = $('#risk-badge'); rb.textContent = r.level; rb.className = 'pill risk ' + r.level;
  const g = $('#gate-badge'); g.textContent = r.safety_gate_open ? 'GATE OPEN' : 'GATE CLOSED'; g.className = 'pill gate ' + (r.safety_gate_open ? 'open' : 'closed');
  $('#btn-gate').textContent = r.safety_gate_open ? 'Close Gate' : 'Open Gate';
  const eb = $('#engine-badge'); eb.textContent = s.paused ? 'PAUSED' : (r.kill_switch ? 'KILLED' : 'ENGINE LIVE'); eb.className = 'pill ' + (s.paused || r.kill_switch ? 'off' : 'on');
  $('#btn-pause').textContent = s.paused ? 'Resume' : 'Pause';
  $('#btn-kill').textContent = r.kill_switch ? 'RESET KILL' : 'KILL SWITCH';
  $('#sb-feed').textContent = s.feed.connected ? `${s.feed.name} · ${s.feed.last_tick_age ?? '?'}s` : 'DOWN'; $('#sb-feed').className = s.feed.connected && (s.feed.last_tick_age ?? 99) < 8 ? 'ok' : 'bad';
  $('#sb-broker').textContent = s.broker.name + (s.broker.connected ? '' : ' ✕'); $('#sb-broker').className = s.broker.connected ? 'ok' : 'bad';
  $('#sb-council').textContent = `cycle ${s.council.cycle}${s.council.busy ? ' ⟳' : ''}`;
  $('#sb-clock').textContent = new Date().toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' });
  const pend = s.pending_plans.length + s.pending_orders.length; const nb = $('#nav-approvals'); nb.textContent = pend; nb.classList.toggle('hidden', !pend);
  $('#ticker').innerHTML = [`<span>VIX<b class="${s.vix && s.vix.change_pct >= 0 ? 'down' : 'up'}">${s.vix ? s.vix.ltp.toFixed(2) : '—'}</b></span>`].concat(s.market.map(m => `<span>${m.symbol}<b class="${m.change_pct >= 0 ? 'up' : 'down'}">${fmt(m.ltp, 1)} ${pct(m.change_pct)}</b></span>`)).join('');
}

$('#mode-seg').onclick = async e => {
  const b = e.target.closest('button'); if (!b) return; const mode = b.dataset.mode; if (S.snap && S.snap.mode === mode) return;
  if (mode === 'AUTO' && !(await confirmModal('<h3>Switch to AUTO mode?</h3><p>The agent council will deploy and manage trades automatically within the risk envelope. Every order still passes the risk manager. You can revert or hit the kill switch at any time.</p>'))) return;
  await act(() => api('/api/mode', 'POST', { mode, reason: 'operator' }), 'Mode → ' + mode);
};
$('#btn-gate').onclick = async () => { const open = !S.snap.risk.snapshot.safety_gate_open; if (open && !(await confirmModal('<h3>Open the safety gate?</h3><p>New entries (manual, approved and AUTO) become possible. Hard risk limits remain enforced.</p>'))) return; await act(() => api('/api/safety/gate', 'POST', { open }), open ? 'Gate opened' : 'Gate closed'); };
$('#btn-pause').onclick = () => act(() => api('/api/engine/' + (S.snap.paused ? 'resume' : 'pause'), 'POST'), S.snap.paused ? 'Engine resumed' : 'Engine paused');
$('#btn-cycle').onclick = () => act(() => api('/api/engine/cycle', 'POST'), 'Council cycle requested');
$('#btn-kill').onclick = async () => {
  if (S.snap.risk.snapshot.kill_switch) { return act(() => api('/api/safety/reset', 'POST'), 'Kill switch reset (gate stays closed)'); }
  if (await confirmModal('<h3 style="color:#f43f5e">ENGAGE KILL SWITCH?</h3><p>Cancels all working orders, flattens every position at market, closes the safety gate and forces MANUAL mode.</p>')) await act(() => api('/api/safety/kill', 'POST'), 'KILL SWITCH ENGAGED');
};
$('#nav').onclick = e => { const a = e.target.closest('a'); if (!a) return; switchView(a.dataset.view); };
function switchView(name) {
  S.view = name; $$('#nav a').forEach(a => a.classList.toggle('active', a.dataset.view === name)); $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  const v = views[name]; if (v.activate) v.activate(); if (S.snap && v.render) v.render(S.snap);
}

// ------------------------------------------------------------------ shared renderers
function positionsTable(ps, withActions = true) {
  if (!ps.length) return '<div class="empty">No open positions</div>';
  return `<div class="scroll"><table><thead><tr><th>Symbol</th><th class="num">Qty</th><th class="num">Avg</th><th class="num">LTP</th><th class="num">P&L</th><th class="num">Δ</th><th class="num">Θ</th><th class="num">V</th>${withActions ? '<th></th>' : ''}</tr></thead><tbody>` +
    ps.map(p => `<tr><td>${p.symbol}<br><small class="muted">${p.exchange} ${p.expiry}</small></td><td class="num ${p.net_qty < 0 ? 'neg' : 'pos'}">${p.net_qty} (${p.lots}L)</td><td class="num">${fmt(p.avg_price, 2)}</td><td class="num">${fmt(p.ltp, 2)}</td><td class="num ${sign(p.unrealized_pnl)}">${inr(p.unrealized_pnl)}</td><td class="num">${fmt(p.delta, 1)}</td><td class="num">${fmt(p.theta, 0)}</td><td class="num">${fmt(p.vega, 0)}</td>${withActions ? `<td><button class="btn sm" data-close="${p.symbol}">Close</button></td>` : ''}</tr>`).join('') + '</tbody></table></div>';
}
function runsTable(runs, withActions = true) {
  if (!runs.length) return '<div class="empty">No active strategy runs</div>';
  return runs.map(r => {
    const credit = Math.abs(r.premium_collected) || 1; const sl = -credit * r.stop_loss_pct / 100, tg = credit * r.target_pct / 100;
    const p = Math.max(-1, Math.min(1, r.mtm / (r.mtm >= 0 ? tg : -sl)));
    return `<div class="plan" style="margin-bottom:8px;border-color:${r.mtm >= 0 ? 'rgba(34,197,94,.4)' : 'rgba(244,63,94,.4)'}"><div class="row"><b>${esc(r.strategy)}</b> <span class="muted">${r.underlying} · ${r.expiry} · ${r.lots}L · ${r.source}</span><span class="sp"></span><b class="${sign(r.mtm)} mono" style="font-size:15px">${inr(r.mtm)}</b>${withActions && r.status === 'ACTIVE' ? `<button class="btn sm danger" data-exit="${r.id}">Exit</button>` : ''}</div>
      <div class="legs">${r.legs.map(l => `<span class="leg ${l.side}">${l.side} ${l.option_type} ${fmt(l.strike)} @ ${fmt(l.entry_price, 2)} → ${fmt(l.ltp, 2)}</span>`).join('')}</div>
      <div class="progress"><i class="${p < 0 ? 'neg' : ''}" style="${p < 0 ? `right:50%;width:${-p * 50}%` : `left:50%;width:${p * 50}%`}"></i></div>
      <div class="row muted" style="font-size:10px"><span>SL ${inr(sl)}</span><span class="sp"></span><span>credit ${inr(r.premium_collected)} · peak ${inr(r.peak_mtm)} · adj ${r.adjustments} · ${ts(r.entered_at)}</span><span class="sp"></span><span>Target ${inr(tg)}</span></div>
      ${r.notes.length ? `<div class="muted" style="font-size:10px">${esc(r.notes.at(-1))}</div>` : ''}</div>`;
  }).join('');
}
function planCard(p, actions = true) {
  return `<div class="plan"><div class="row"><b style="font-size:14px">${esc(p.strategy)}</b><span class="muted">${p.underlying} · ${p.exchange} · exp ${p.expiry}</span><span class="sp"></span><span class="pill">${p.status}</span></div>
    <div class="legs">${p.legs.map(l => `<span class="leg ${l.side}">${l.side} ${l.option_type} ${fmt(l.strike)} @ ${fmt(l.entry_price, 2)}</span>`).join('')}</div>
    <div class="grid g4"><div class="tile"><span class="s">Credit / lot</span><span class="v ${sign(p.premium_collected)}" style="font-size:16px">${inr(p.premium_collected)}</span></div><div class="tile"><span class="s">Margin est.</span><span class="v" style="font-size:16px">${inr(p.margin_estimate)}</span></div><div class="tile"><span class="s">Max loss</span><span class="v neg" style="font-size:16px">${p.max_loss == null ? 'undefined' : inr(p.max_loss)}</span></div><div class="tile"><span class="s">Consensus / conf</span><span class="v" style="font-size:16px">${p.consensus.toFixed(2)} / ${p.confidence.toFixed(2)}</span></div></div>
    <div class="muted" style="font-size:11px">Breakevens ${p.breakevens.map(b => fmt(b)).join(' · ')} · SL ${p.stop_loss_pct}% · Target ${p.target_pct}% · lots ${p.lots} · ${dts(p.created_at)}</div>
    <div class="rat">${p.rationale.map(r => '• ' + esc(r)).join('<br>')}</div>
    ${actions ? `<div class="row"><input type="number" min="1" value="${p.lots}" style="width:80px" data-lots="${p.id}"><button class="btn success" data-approve="${p.id}">Approve &amp; Deploy</button><button class="btn" data-reject="${p.id}">Reject</button></div>` : ''}</div>`;
}
function alertsList(alerts) { return alerts.length ? alerts.map(a => `<div class="alert ${a.level}"><b>${a.level}</b><div><div>${esc(a.title)}</div><small>${ts(a.ts)} · ${esc(a.category)} ${a.market ? '· ' + a.market : ''}</small>${a.body ? `<div class="muted">${esc(a.body)}</div>` : ''}</div></div>`).join('') : '<div class="empty">No alerts</div>'; }
function agentCard(a, withControls = false) {
  const l = a.last; const score = l ? l.score : 0; const cls = l && l.veto ? 'veto' : (score > 0.3 ? 'good' : '');
  return `<div class="agent ${a.enabled ? '' : 'off'}"><div class="head"><div><b>${a.name}</b><div class="role">${esc(a.role)}</div></div><span class="stance ${cls}">${l ? esc(l.stance) : '—'}</span></div>
    <div class="bar"><i class="${score < 0 ? 'neg' : ''}" style="${score < 0 ? `right:50%;width:${-score * 50}%` : `left:50%;width:${score * 50}%`}"></i></div>
    <div class="row muted" style="font-size:10px"><span>score ${score.toFixed(2)}</span><span>conf ${l ? l.confidence.toFixed(2) : '—'}</span><span>w ${a.weight.toFixed(1)}</span><span>${l ? l.latency_ms + 'ms' : ''}</span><span class="sp"></span><span>${a.runs} runs${a.failures ? ` · ${a.failures} err` : ''}</span></div>
    <ul>${(l ? l.findings : []).slice(0, 4).map(f => `<li>${esc(f)}</li>`).join('')}</ul>
    ${withControls ? `<div class="ctl"><label><input type="checkbox" ${a.enabled ? 'checked' : ''} data-agent-enable="${a.name}"> enabled</label><input type="range" min="0" max="3" step="0.1" value="${a.weight}" data-agent-weight="${a.name}"><span>weight</span></div>` : ''}</div>`;
}

// ------------------------------------------------------------------ views
const views = {};

views.overview = {
  mount() { $('#view-overview').innerHTML = `<div class="grid g6" id="ov-tiles"></div><div class="grid g4" id="ov-markets" style="margin-top:14px"></div><div class="grid g-2-1" style="margin-top:14px"><div class="grid" style="gap:14px"><div class="card"><h3>Active strategy runs</h3><div id="ov-runs"></div></div><div class="card"><h3>Open positions</h3><div id="ov-pos"></div></div></div><div class="grid" style="gap:14px"><div class="card"><h3>Council — latest decisions</h3><div id="ov-council"></div></div><div class="card"><h3>Alerts</h3><div id="ov-alerts"></div></div></div></div>`; },
  render(s) {
    const r = s.risk.snapshot, p = s.pnl;
    $('#ov-tiles').innerHTML = [['Daily P&L', inr(p.daily), sign(p.daily), `realized ${inr(p.realized)} · charges ${inr(p.charges)}`], ['Unrealized', inr(p.unrealized), sign(p.unrealized), `${r.open_positions} positions · ${r.open_lots} lots`], ['Loss budget used', r.loss_budget_used_pct + '%', r.loss_budget_used_pct > 70 ? 'neg' : (r.loss_budget_used_pct > 40 ? 'warn' : ''), `max loss ${inr(r.max_daily_loss)}`], ['Margin used', inr(r.margin_used), r.margin_utilisation_pct > 60 ? 'warn' : '', `${r.margin_utilisation_pct}% utilisation`], ['Portfolio Δ / Θ', `${fmt(r.portfolio_delta, 1)} / ${fmt(r.portfolio_theta)}`, '', `vega ${fmt(r.portfolio_vega)} · gamma ${fmt(r.portfolio_gamma, 3)}`], ['Mode', `${s.mode} · ${s.env}`, s.mode === 'AUTO' ? 'info' : '', `${r.trades_today} fills today · risk ${r.level}`]]
      .map(([t, v, c, sub]) => `<div class="card tile"><span class="s">${t}</span><span class="v ${c}">${v}</span><span class="s">${sub}</span></div>`).join('');
    const mk = $('#ov-markets');
    if (mk.children.length !== s.market.length) mk.innerHTML = s.market.map(m => `<div class="mcard ${m.enabled ? '' : 'disabled'}" data-sym="${m.symbol}"><div class="sym"><b>${m.symbol}</b><span class="ex">${m.exchange} · ${m.session}</span></div><div class="row"><span class="px" data-f="ltp"></span><span data-f="chg"></span></div><canvas data-f="spark"></canvas><div class="meta" data-f="meta"></div></div>`).join('');
    s.market.forEach((m, i) => { const el = mk.children[i]; if (!el) return; $('[data-f=ltp]', el).textContent = fmt(m.ltp, 1); const c = $('[data-f=chg]', el); c.textContent = pct(m.change_pct); c.className = sign(m.change_pct); sparkline($('[data-f=spark]', el), S.hist[m.symbol] || [], m.change_pct >= 0 ? '#22c55e' : '#f43f5e'); $('[data-f=meta]', el).innerHTML = `<span>PCR <b>${fmt(m.pcr, 2)}</b></span><span>IV <b>${fmt(m.iv_atm, 1)}</b></span><span>DTE <b>${fmt(m.dte, 1)}</b></span><span>±<b>${fmt(m.expected_move)}</b></span><span>MP <b>${fmt(m.max_pain)}</b></span>`; });
    $('#ov-runs').innerHTML = runsTable(s.runs, true);
    $('#ov-pos').innerHTML = positionsTable(s.positions, false);
    $('#ov-council').innerHTML = s.council.last.length ? s.council.last.map(d => `<div class="decision ${d.decision}"><b>${d.underlying} · ${d.decision}</b> <small>${ts(d.ts)} · c=${d.consensus.toFixed(2)}</small><div class="muted">${esc(d.summary)}</div></div>`).join('') : '<div class="empty">Council warming up…</div>';
    $('#ov-alerts').innerHTML = alertsList(s.alerts.slice(0, 8));
  },
};
$('#view-overview').addEventListener('click', e => { const m = e.target.closest('.mcard'); if (m) { S.chainUnder = m.dataset.sym; S.chainExpiry = null; switchView('chain'); } const ex = e.target.closest('[data-exit]'); if (ex) exitRun(ex.dataset.exit); });
async function exitRun(id) { if (await confirmModal('<h3>Exit strategy run?</h3><p>All legs will be closed at market.</p>')) await act(() => api('/api/strategy/exit/' + id, 'POST'), 'Exit submitted'); }

views.chain = {
  timer: null,
  mount() { $('#view-chain').innerHTML = `<div class="card"><div class="row"><label>Underlying <select id="ch-under"></select></label><label>Expiry <select id="ch-exp"></select></label><span class="sp"></span><div id="ch-ind" class="row muted mono" style="font-size:11px"></div></div></div><div class="grid g-3-2" style="margin-top:14px"><div class="card"><h3>Spot · 1-minute candles <span class="right" id="ch-spot"></span></h3><canvas id="ch-candles" class="chart"></canvas></div><div class="card"><h3>PCR (OI) history</h3><canvas id="ch-pcr" class="chart"></canvas><div class="grid g4" id="ch-stats" style="margin-top:8px"></div></div></div><div class="card" style="margin-top:14px"><h3>Option chain <span class="right muted" id="ch-meta"></span></h3><div class="scroll tall chain" id="ch-table"></div></div>`; $('#ch-under').onchange = e => { S.chainUnder = e.target.value; S.chainExpiry = null; views.chain.load(); }; $('#ch-exp').onchange = e => { S.chainExpiry = e.target.value; views.chain.load(); }; },
  activate() { this.load(); clearInterval(this.timer); this.timer = setInterval(() => { if (S.view === 'chain') this.load(); else clearInterval(this.timer); }, 3000); },
  async load() {
    try { const d = await api(`/api/market/chain?underlying=${S.chainUnder}${S.chainExpiry ? '&expiry=' + S.chainExpiry : ''}`); const c = await api(`/api/market/candles?symbol=${S.chainUnder}&limit=150`); S.chain = d; S.chain.candles = c.candles; this.draw(); } catch (e) { }
  },
  render(s) { const sel = $('#ch-under'); if (sel.options.length !== s.market.length) { sel.innerHTML = s.market.map(m => `<option value="${m.symbol}">${m.symbol} (${m.exchange})</option>`).join(''); } sel.value = S.chainUnder; },
  draw() {
    const d = S.chain; if (!d) return; const ch = d.chain, ind = d.indicators || {};
    const ex = $('#ch-exp'); if (ex.options.length !== d.expiries.length) ex.innerHTML = d.expiries.map(e => `<option value="${e}">${e}</option>`).join(''); ex.value = ch.expiry;
    $('#ch-ind').innerHTML = ['trend', 'supertrend', 'rsi', 'ema9', 'ema21', 'vwap', 'atr', 'realized_vol'].map(k => `<span>${k} <b>${ind[k] ?? '—'}</b></span>`).join('');
    $('#ch-spot').textContent = `${fmt(ch.spot, 1)} · ATM ${fmt(ch.atm_strike)} · DTE ${ch.days_to_expiry}`;
    candles($('#ch-candles'), d.candles, { lines: [{ y: ind.vwap, color: '#a78bfa', label: 'VWAP' }, { y: ind.ema21, color: '#f59e0b', label: 'EMA21' }] });
    const ph = d.pcr_history || [];
    lineChart($('#ch-pcr'), [{ values: ph.map((p, i) => ({ x: i, y: p.pcr })), color: '#22d3ee', area: true }], { height: 150, fmt: v => v.toFixed(2) });
    $('#ch-stats').innerHTML = [['PCR (OI)', fmt(ch.pcr, 3)], ['PCR (vol)', fmt(ch.pcr_volume, 3)], ['ATM IV', fmt(ch.iv_atm, 2) + '%'], ['Max pain', fmt(ch.max_pain)], ['Exp. move', '±' + fmt(ch.expected_move)], ['CE OI', fmt(ch.total_ce_oi / 1e5, 1) + 'L'], ['PE OI', fmt(ch.total_pe_oi / 1e5, 1) + 'L'], ['Lot', ch.lot_size]].map(([k, v]) => `<div class="tile"><span class="s">${k}</span><span class="v" style="font-size:14px">${v}</span></div>`).join('');
    $('#ch-meta').textContent = `${ch.underlying} ${ch.expiry} · ${ts(ch.ts)}`;
    const maxOi = Math.max(...ch.rows.flatMap(r => [r.ce.oi, r.pe.oi])) || 1;
    $('#ch-table').innerHTML = `<table><thead><tr><th class="ce">CE OI</th><th class="ce">OI chg</th><th class="ce">IV</th><th class="ce">Δ</th><th class="ce">Θ</th><th class="ce">LTP</th><th style="text-align:center">Strike</th><th>LTP</th><th>Θ</th><th>Δ</th><th>IV</th><th>OI chg</th><th>PE OI</th></tr></thead><tbody>` +
      ch.rows.map(r => `<tr class="${r.strike === ch.atm_strike ? 'atm' : ''}"><td class="ce oi ce"><i style="width:${r.ce.oi / maxOi * 100}%"></i><span>${fmt(r.ce.oi / 1e5, 1)}L</span></td><td class="ce ${sign(r.ce.oi_change)}">${fmt(r.ce.oi_change / 1e3)}K</td><td class="ce">${fmt(r.ce.iv, 1)}</td><td class="ce">${r.ce.delta.toFixed(2)}</td><td class="ce">${fmt(r.ce.theta, 1)}</td><td class="ce ${r.strike < ch.spot ? 'itm' : ''}" data-sym="${r.ce.symbol}"><b>${fmt(r.ce.ltp, 2)}</b></td><td style="text-align:center;font-weight:700;color:#e2e8f0">${fmt(r.strike)}</td><td class="${r.strike > ch.spot ? 'itm' : ''}" data-sym="${r.pe.symbol}"><b>${fmt(r.pe.ltp, 2)}</b></td><td>${fmt(r.pe.theta, 1)}</td><td>${r.pe.delta.toFixed(2)}</td><td>${fmt(r.pe.iv, 1)}</td><td class="${sign(r.pe.oi_change)}">${fmt(r.pe.oi_change / 1e3)}K</td><td class="oi pe"><i style="width:${r.pe.oi / maxOi * 100}%"></i><span>${fmt(r.pe.oi / 1e5, 1)}L</span></td></tr>`).join('') + '</tbody></table>';
  },
};
$('#view-chain').addEventListener('click', e => { const td = e.target.closest('[data-sym]'); if (td) { $('#ot-symbol').value = td.dataset.sym; switchView('trading'); toast('Symbol loaded into order ticket'); } });

views.council = {
  mount() { $('#view-council').innerHTML = `<div class="grid g-2-1"><div class="card"><h3>Agent council <span class="right"><span class="muted" id="cn-meta"></span><button class="btn sm" id="cn-run">Run cycle now</button></span></h3><div class="chips" id="cn-focus" style="margin-bottom:10px"></div><div class="grid g3" id="cn-agents"></div></div><div class="grid" style="gap:14px"><div class="card"><h3>Consensus</h3><div id="cn-consensus"></div></div><div class="card"><h3>Desk briefing <span class="right muted" id="cn-llm"></span></h3><div class="brief" id="cn-brief"></div></div></div></div><div class="card" style="margin-top:14px"><h3>Decision log</h3><div id="cn-decisions" class="scroll"></div></div>`;
    $('#cn-run').onclick = () => act(() => api('/api/agents/run', 'POST'), 'Cycle executed');
    $('#cn-focus').onclick = e => { const c = e.target.closest('.chip'); if (!c) return; const f = new Set(S.snap.council.focus); f.has(c.dataset.sym) ? f.delete(c.dataset.sym) : f.add(c.dataset.sym); act(() => api('/api/agents/focus', 'POST', { symbols: [...f] }), 'Focus updated'); };
    $('#cn-agents').addEventListener('change', e => { const en = e.target.closest('[data-agent-enable]'); if (en) act(() => api(`/api/agents/${en.dataset.agentEnable}/config`, 'POST', { patch: { enabled: en.checked } })); const w = e.target.closest('[data-agent-weight]'); if (w) act(() => api(`/api/agents/${w.dataset.agentWeight}/config`, 'POST', { patch: { weight: parseFloat(w.value) } })); });
  },
  render(s) {
    const c = s.council;
    $('#cn-meta').textContent = `cycle ${c.cycle} · ${c.busy ? 'running' : 'idle'} · AUTO trades today ${c.auto_trades_today ?? '—'}`;
    $('#cn-focus').innerHTML = s.market.map(m => `<span class="chip ${c.focus.includes(m.symbol) ? 'on' : ''}" data-sym="${m.symbol}">${m.symbol}</span>`).join('');
    const ag = $('#cn-agents'); if (!ag.dataset.built || document.activeElement?.closest('#cn-agents') == null) { ag.innerHTML = c.agents.map(a => agentCard(a, true)).join(''); ag.dataset.built = '1'; }
    const last = c.last[0];
    $('#cn-consensus').innerHTML = last ? `<div class="tile"><span class="v ${last.consensus > 0.3 ? 'pos' : (last.consensus < 0 ? 'neg' : '')}">${last.consensus >= 0 ? '+' : ''}${last.consensus.toFixed(2)}</span><span class="s">${last.underlying} · ${last.decision} · confidence ${last.confidence.toFixed(2)}</span></div><div class="gauge" style="margin-top:8px"><div class="track"><i style="width:${Math.max(0, (last.consensus + 1) / 2 * 100)}%;background:${last.consensus >= 0.62 ? '#22c55e' : (last.consensus > 0 ? '#f59e0b' : '#f43f5e')}"></i></div><div class="lbl"><span>-1 avoid</span><span>threshold 0.62</span><span>+1 enter</span></div></div><div class="muted" style="margin-top:8px;font-size:11px">${esc(last.summary)}</div>` : '<div class="empty">No decision yet</div>';
    $('#cn-llm').textContent = c.llm.enabled ? `${c.llm.provider} · ${c.llm.model}` : 'LLM narration off (LLM_ENABLED=false)';
    const briefs = Object.entries(c.briefs || {}); $('#cn-brief').textContent = briefs.length ? briefs.map(([u, b]) => `${u}\n${b}`).join('\n\n') : (last ? 'Deterministic summary: ' + last.summary : '');
    $('#cn-decisions').innerHTML = c.last.map(d => `<div class="decision ${d.decision}"><b>${d.underlying} · ${d.decision}</b> <small>${dts(d.ts)} · consensus ${d.consensus.toFixed(2)} · conf ${d.confidence.toFixed(2)} · ${d.mode}</small><div class="muted">${esc(d.summary)}</div><div class="chips" style="margin-top:6px">${d.assessments.map(a => `<span class="chip ${a.veto ? 'on' : ''}" style="${a.veto ? 'border-color:#f43f5e' : ''}">${a.agent}: ${a.stance} ${a.score.toFixed(2)}</span>`).join('')}</div></div>`).join('') || '<div class="empty">—</div>';
  },
};

views.strategy = {
  mount() { $('#view-strategy').innerHTML = `<div class="grid g-1-2"><div class="card"><h3>Deploy strategy (manual)</h3><div class="form"><label>Strategy<select id="st-key"></select></label><label>Underlying<select id="st-under"></select></label><label>Lots<input id="st-lots" type="number" min="1" value="1"></label><label>Expiry<select id="st-exp"><option value="">nearest</option></select></label><label>Delta<input id="st-delta" type="number" step="0.01" placeholder="auto"></label><label>SL %<input id="st-sl" type="number" placeholder="auto"></label><label>Target %<input id="st-tg" type="number" placeholder="auto"></label></div><div class="row" style="margin-top:10px"><button class="btn" id="st-preview">Preview</button><button class="btn primary" id="st-deploy">Deploy now</button></div><div id="st-prev" style="margin-top:12px"></div><canvas id="st-payoff" class="chart" style="margin-top:8px"></canvas></div><div class="card"><h3>Strategy library &amp; parameters</h3><div id="st-lib" class="grid g2"></div></div></div><div class="card" style="margin-top:14px"><h3>Runs</h3><div id="st-runs"></div></div>`;
    $('#st-preview').onclick = () => views.strategy.preview(false); $('#st-deploy').onclick = () => views.strategy.preview(true);
    $('#st-under').onchange = async () => { const r = await api('/api/market/expiries?underlying=' + $('#st-under').value); $('#st-exp').innerHTML = '<option value="">nearest</option>' + r.expiries.map(e => `<option>${e}</option>`).join(''); };
    $('#st-lib').addEventListener('click', e => { const b = e.target.closest('[data-save]'); if (!b) return; const card = b.closest('.plan'); const patch = {}; $$('input', card).forEach(i => { patch[i.dataset.k] = i.type === 'checkbox' ? i.checked : parseFloat(i.value); }); act(() => api('/api/strategy/config/' + b.dataset.save, 'POST', { patch }), 'Saved'); });
    $('#st-runs').addEventListener('click', e => { const ex = e.target.closest('[data-exit]'); if (ex) exitRun(ex.dataset.exit); });
  },
  async activate() { S.strategies = await api('/api/strategy'); const keys = S.strategies.strategies; $('#st-key').innerHTML = keys.map(k => `<option value="${k.key}">${k.name}</option>`).join(''); $('#st-under').innerHTML = S.snap.market.map(m => `<option>${m.symbol}</option>`).join(''); $('#st-under').onchange(); $('#st-lib').innerHTML = keys.map(k => `<div class="plan"><div class="row"><b>${k.name}</b><span class="sp"></span><span class="pill">${k.defined_risk ? 'DEFINED RISK' : 'UNDEFINED RISK'}</span></div><div class="muted" style="font-size:11px">${esc(k.description)}</div><div class="muted" style="font-size:10px">fits: ${k.regime_fit.join(', ')}</div><div class="form">${Object.entries(k.params).map(([p, v]) => `<label>${p}<input type="number" step="any" value="${v}" data-k="${p}"></label>`).join('')}<label>enabled<input type="checkbox" ${k.enabled ? 'checked' : ''} data-k="enabled"></label></div><div class="row"><button class="btn sm" data-save="${k.key}">Save</button></div></div>`).join(''); },
  render(s) { $('#st-runs').innerHTML = runsTable(s.runs, true) + (S.strategies ? `<div class="hr"></div><div class="scroll"><table><thead><tr><th>Closed run</th><th>Underlying</th><th class="num">Lots</th><th class="num">Credit</th><th class="num">P&L</th><th>Exit</th><th>Closed</th></tr></thead><tbody>${S.strategies.runs.filter(r => r.status === 'CLOSED').slice(0, 30).map(r => `<tr><td>${r.strategy}</td><td>${r.underlying}</td><td class="num">${r.lots}</td><td class="num">${inr(r.premium_collected)}</td><td class="num ${sign(r.realized_pnl)}">${inr(r.realized_pnl)}</td><td class="sans">${esc(r.exit_reason)}</td><td>${dts(r.closed_at)}</td></tr>`).join('')}</tbody></table></div>` : ''); },
  async preview(deploy) {
    const body = { strategy: $('#st-key').value, underlying: $('#st-under').value, lots: parseInt($('#st-lots').value) || 1, expiry: $('#st-exp').value || null, params: {} };
    if ($('#st-delta').value) body.params.delta = parseFloat($('#st-delta').value); if ($('#st-sl').value) body.params.stop_loss_pct = parseFloat($('#st-sl').value); if ($('#st-tg').value) body.params.target_pct = parseFloat($('#st-tg').value);
    if (deploy) { if (!(await confirmModal(`<h3>Deploy ${body.strategy} on ${body.underlying} × ${body.lots}?</h3><p>Orders are sent immediately through the risk manager (${S.snap.env} environment).</p>`))) return; const r = await act(() => api('/api/strategy/deploy', 'POST', body), 'Deployed'); if (r) S.strategies = await api('/api/strategy'); return; }
    const r = await act(() => api('/api/strategy/preview', 'POST', body)); if (!r) return; S.preview = r;
    $('#st-prev').innerHTML = planCard(r.plan, false) + `<div class="muted" style="margin-top:6px;font-size:11px">Risk pre-check: ${r.risk_check.allowed ? '<span class="pos">ALLOWED</span>' : '<span class="neg">BLOCKED: ' + r.risk_check.reasons.join(', ') + '</span>'}</div>`;
    lineChart($('#st-payoff'), [{ values: r.payoff.x.map((x, i) => ({ x, y: r.payoff.y[i] })), color: '#22d3ee', fillSign: true }], { height: 170, zero: true, vlines: [{ x: r.plan.legs[0] ? S.snap.market.find(m => m.symbol === body.underlying).ltp : 0, label: 'spot', color: '#a78bfa' }, ...r.payoff.breakevens.map(b => ({ x: b, label: 'BE', color: '#f59e0b' }))] });
  },
};

views.approvals = {
  mount() { $('#view-approvals').innerHTML = `<div class="card"><h3>Council proposals awaiting your approval <span class="right muted">MANUAL mode queue · proposals expire after 15 min</span></h3><div id="ap-plans" class="grid g2"></div></div><div class="card" style="margin-top:14px"><h3>Agent orders awaiting confirmation</h3><div id="ap-orders"></div></div>`;
    $('#view-approvals').addEventListener('click', async e => { const a = e.target.closest('[data-approve]'); if (a) { const lots = parseInt($(`[data-lots="${a.dataset.approve}"]`).value) || null; await act(() => api(`/api/approvals/${a.dataset.approve}/approve`, 'POST', { lots }), 'Plan approved & deployed'); } const r = e.target.closest('[data-reject]'); if (r) await act(() => api(`/api/approvals/${r.dataset.reject}/reject`, 'POST', { reason: 'operator' }), 'Rejected'); const oa = e.target.closest('[data-oapprove]'); if (oa) await act(() => api(`/api/orders/${oa.dataset.oapprove}/approve`, 'POST'), 'Order approved'); const orj = e.target.closest('[data-oreject]'); if (orj) await act(() => api(`/api/orders/${orj.dataset.oreject}/reject`, 'POST', { reason: 'operator' }), 'Order rejected'); });
  },
  render(s) { $('#ap-plans').innerHTML = s.pending_plans.length ? s.pending_plans.map(p => planCard(p, true)).join('') : '<div class="empty">No pending proposals. The council proposes when consensus ≥ threshold and the safety gate is open.</div>'; $('#ap-orders').innerHTML = s.pending_orders.length ? `<table><thead><tr><th>Order</th><th>Side</th><th class="num">Lots</th><th>Source</th><th>Reason</th><th></th></tr></thead><tbody>${s.pending_orders.map(o => `<tr><td>${o.symbol}</td><td class="${o.side === 'SELL' ? 'neg' : 'pos'}">${o.side}</td><td class="num">${o.lots}</td><td>${o.source}</td><td class="sans">${esc(o.reason)}</td><td><button class="btn sm success" data-oapprove="${o.id}">Approve</button> <button class="btn sm" data-oreject="${o.id}">Reject</button></td></tr>`).join('')}</tbody></table>` : '<div class="empty">None</div>'; },
};

views.trading = {
  mount() { $('#view-trading').innerHTML = `<div class="grid g-1-2"><div class="card"><h3>Order ticket (manual)</h3><div class="form"><label>Option symbol<input id="ot-symbol" placeholder="NIFTY06OCT2624800CE"></label><label>Side<select id="ot-side"><option>SELL</option><option>BUY</option></select></label><label>Lots<input id="ot-lots" type="number" min="1" value="1"></label><label>Type<select id="ot-type"><option>MARKET</option><option>LIMIT</option></select></label><label>Limit price<input id="ot-price" type="number" step="0.05"></label></div><div class="row" style="margin-top:10px"><button class="btn primary" id="ot-place">Place order</button><button class="btn danger" id="ot-flatten">Flatten all</button></div><div class="muted" style="margin-top:8px;font-size:11px">Pick a symbol by clicking any LTP in the option chain. Orders pass the risk manager; entries need the safety gate open.</div><div class="hr"></div><h3>Greeks</h3><div id="ot-greeks" class="grid g4"></div></div><div class="card"><h3>Open positions</h3><div id="ot-pos"></div></div></div><div class="card" style="margin-top:14px"><h3>Order book</h3><div id="ot-orders" class="scroll"></div></div>`;
    $('#ot-place').onclick = async () => { const body = { symbol: $('#ot-symbol').value.trim(), side: $('#ot-side').value, lots: parseInt($('#ot-lots').value) || 1, order_type: $('#ot-type').value, limit_price: $('#ot-price').value ? parseFloat($('#ot-price').value) : null }; if (!(await confirmModal(`<h3>${body.side} ${body.lots} lot(s) ${esc(body.symbol)}?</h3><p>${body.order_type}${body.limit_price ? ' @ ' + body.limit_price : ''} · ${S.snap.env}</p>`))) return; const r = await act(() => api('/api/orders/place', 'POST', body)); if (r) toast(`${r.status}: ${r.message}`, r.status === 'FILLED' ? 'ok' : 'err'); };
    $('#ot-flatten').onclick = async () => { if (await confirmModal('<h3>Flatten all positions?</h3><p>Every open position is closed at market and active runs are marked closed.</p>')) await act(() => api('/api/positions/flatten', 'POST'), 'Flatten submitted'); };
    $('#ot-pos').addEventListener('click', async e => { const b = e.target.closest('[data-close]'); if (b && await confirmModal(`<h3>Close ${b.dataset.close}?</h3>`)) await act(() => api(`/api/positions/${b.dataset.close}/close`, 'POST'), 'Close submitted'); });
    $('#ot-orders').addEventListener('click', async e => { const b = e.target.closest('[data-cancel]'); if (b) await act(() => api(`/api/orders/${b.dataset.cancel}/cancel`, 'POST'), 'Cancelled'); });
  },
  render(s) { const r = s.risk.snapshot; $('#ot-greeks').innerHTML = [['Delta', r.portfolio_delta, 1], ['Gamma', r.portfolio_gamma, 3], ['Theta/day', r.portfolio_theta, 0], ['Vega', r.portfolio_vega, 0]].map(([k, v, d]) => `<div class="tile"><span class="s">${k}</span><span class="v" style="font-size:16px">${fmt(v, d)}</span></div>`).join(''); $('#ot-pos').innerHTML = positionsTable(s.positions, true); $('#ot-orders').innerHTML = s.orders.length ? `<table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th class="num">Lots</th><th class="num">Fill</th><th class="num">Charges</th><th>Status</th><th>Source</th><th>Note</th><th></th></tr></thead><tbody>${s.orders.map(o => `<tr><td>${ts(o.created_at)}</td><td>${o.symbol}</td><td class="${o.side === 'SELL' ? 'neg' : 'pos'}">${o.side}</td><td class="num">${o.lots}</td><td class="num">${fmt(o.filled_price, 2)}</td><td class="num">${fmt(o.charges, 0)}</td><td class="${o.status === 'FILLED' ? 'pos' : (o.status.includes('REJECT') ? 'neg' : 'warn')}">${o.status}</td><td>${o.source}</td><td class="sans muted">${esc(o.reason || o.message)}</td><td>${['OPEN', 'PENDING', 'PENDING_APPROVAL'].includes(o.status) ? `<button class="btn sm" data-cancel="${o.id}">Cancel</button>` : ''}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No orders yet</div>'; },
};

views.risk = {
  mount() { $('#view-risk').innerHTML = `<div class="grid g3"><div class="card"><h3>Daily loss budget</h3><div class="gauge" id="rk-loss"></div></div><div class="card"><h3>Margin utilisation</h3><div class="gauge" id="rk-margin"></div></div><div class="card"><h3>Status</h3><div id="rk-status"></div></div></div><div class="grid g-1-2" style="margin-top:14px"><div class="card"><h3>Markets</h3><div id="rk-markets" class="chips"></div><div class="hr"></div><h3>Simulation stress test</h3><div class="form"><label>Symbol<select id="rk-shock-sym"></select></label><label>Shock %<input id="rk-shock" type="number" step="0.5" value="-3"></label></div><div class="row" style="margin-top:8px"><button class="btn" id="rk-shock-btn">Inject shock</button><span class="muted" style="font-size:11px">Simulated feed only. Exercises Sentinel / risk halts.</span></div></div><div class="card"><h3>Risk limits <span class="right"><button class="btn sm" id="rk-save">Save limits</button></span></h3><div class="form" id="rk-limits"></div></div></div>`;
    $('#rk-markets').onclick = e => { const c = e.target.closest('.chip'); if (c) act(() => api('/api/safety/market', 'POST', { market: c.dataset.m, enabled: !S.snap.risk.markets[c.dataset.m] }), 'Market toggled'); };
    $('#rk-save').onclick = () => { const limits = {}; $$('#rk-limits input').forEach(i => limits[i.dataset.k] = parseFloat(i.value)); act(() => api('/api/risk/limits', 'POST', { limits }), 'Limits saved'); };
    $('#rk-shock-btn').onclick = () => act(() => api('/api/market/shock', 'POST', { symbol: $('#rk-shock-sym').value, pct: parseFloat($('#rk-shock').value) }), 'Shock injected');
  },
  render(s) {
    const r = s.risk.snapshot, L = s.risk.limits;
    $('#rk-loss').innerHTML = `<div class="tile"><span class="v ${sign(r.daily_pnl)}">${inr(r.daily_pnl)}</span><span class="s">of ${inr(-r.max_daily_loss)} max loss</span></div><div class="track"><i style="width:${Math.min(100, r.loss_budget_used_pct)}%"></i></div><div class="lbl"><span>${r.loss_budget_used_pct}% used</span><span>halt at 100%</span></div>`;
    $('#rk-margin').innerHTML = `<div class="tile"><span class="v">${inr(r.margin_used)}</span><span class="s">available ${inr(r.margin_available)}</span></div><div class="track"><i style="width:${Math.min(100, r.margin_utilisation_pct)}%"></i></div><div class="lbl"><span>${r.margin_utilisation_pct}%</span><span>cap ${L.max_margin_utilisation_pct}%</span></div>`;
    $('#rk-status').innerHTML = `<div class="kv"><span>Level</span><b class="${r.level === 'GREEN' ? 'ok' : 'bad'}">${r.level}</b></div><div class="kv"><span>Kill switch</span><b>${r.kill_switch}</b></div><div class="kv"><span>Safety gate</span><b class="${r.safety_gate_open ? 'ok' : ''}">${r.safety_gate_open ? 'OPEN' : 'CLOSED'}</b></div><div class="kv"><span>Halted</span><b>${s.risk.halted_reason || '—'}</b></div><div class="kv"><span>Open lots</span><b>${r.open_lots} / ${L.max_open_lots}</b></div><div class="kv"><span>Per market</span><b>${Object.entries(r.per_market_lots).map(([k, v]) => k + ':' + v).join(' ') || '—'}</b></div><div class="kv"><span>Δ / vega</span><b>${fmt(r.portfolio_delta, 1)} / ${fmt(r.portfolio_vega)}</b></div><div class="kv"><span>Operating window</span><b class="${s.scheduler.in_operating_window ? 'ok' : 'bad'}">${s.scheduler.operating_window.join('–')} ${s.scheduler.in_operating_window ? 'open' : 'closed'}</b></div><div class="kv"><span>Pending exits</span><b class="${s.exits.pending.length ? 'bad' : ''}">${s.exits.pending.length ? s.exits.pending.map(x => `${x.symbol} try ${x.attempts} (next ${x.next_retry_in}s)`).join(', ') : 'none'}</b></div><div class="hr"></div>${r.breaches.map(b => `<div class="neg">✕ ${b}</div>`).join('')}${r.warnings.map(w => `<div class="warn">⚠ ${w}</div>`).join('')}${!r.breaches.length && !r.warnings.length ? '<div class="pos">✓ All limits respected</div>' : ''}`;
    $('#rk-markets').innerHTML = Object.entries(s.risk.markets).map(([m, on]) => `<span class="chip ${on ? 'on' : ''}" data-m="${m}">${m} ${on ? 'enabled' : 'disabled'}</span>`).join('');
    const ss = $('#rk-shock-sym'); if (!ss.options.length) ss.innerHTML = ['INDIAVIX', ...s.market.map(m => m.symbol)].map(x => `<option>${x}</option>`).join('');
    const lim = $('#rk-limits'); if (!lim.children.length) lim.innerHTML = Object.entries(L).map(([k, v]) => `<label>${k.replaceAll('_', ' ')}<input type="number" step="any" value="${v}" data-k="${k}"></label>`).join('');
  },
};

views.reports = {
  mount() { $('#view-reports').innerHTML = `<div class="grid g6" id="rp-tiles"></div><div class="grid g2" style="margin-top:14px"><div class="card"><h3>Equity curve (closed trades)</h3><canvas id="rp-equity" class="chart"></canvas></div><div class="card"><h3>Daily P&L</h3><canvas id="rp-daily" class="chart"></canvas></div></div><div class="grid g-1-2" style="margin-top:14px"><div class="card"><h3>By strategy / market</h3><div id="rp-by"></div></div><div class="card"><h3>Trade book <span class="right"><a class="btn sm" href="/api/reports/export.csv" target="_blank">Export CSV</a><button class="btn sm" id="rp-refresh">Refresh</button></span></h3><div id="rp-trades" class="scroll"></div></div></div><div class="card" style="margin-top:14px"><h3>Backtest engine</h3><div class="form"><label>Strategy<select id="bt-key"></select></label><label>Underlying<select id="bt-under"></select></label><label>Days<input id="bt-days" type="number" value="90"></label><label>Lots<input id="bt-lots" type="number" value="1"></label><label>Delta<input id="bt-delta" type="number" step="0.01" placeholder="default"></label><label>SL %<input id="bt-sl" type="number" placeholder="default"></label><label>Target %<input id="bt-tg" type="number" placeholder="default"></label><label>Seed<input id="bt-seed" type="number" value="42"></label></div><div class="row" style="margin-top:10px"><button class="btn primary" id="bt-run">Run backtest</button><span class="muted" style="font-size:11px">Synthetic path + Black-Scholes repricing with the live engine's exit rules. Research tool, not a tick replay.</span></div><div id="bt-res" style="margin-top:12px"></div><canvas id="bt-eq" class="chart"></canvas></div>`;
    $('#rp-refresh').onclick = () => views.reports.activate(); $('#bt-run').onclick = () => views.reports.backtest();
  },
  async activate() { S.perf = await api('/api/reports/performance'); this.draw(); if (!$('#bt-key').options.length) { const st = await api('/api/strategy'); $('#bt-key').innerHTML = st.strategies.map(k => `<option value="${k.key}">${k.name}</option>`).join(''); $('#bt-under').innerHTML = S.snap.market.map(m => `<option>${m.symbol}</option>`).join(''); } },
  render() { },
  draw() {
    const p = S.perf; if (!p) return;
    $('#rp-tiles').innerHTML = [['Net P&L', inr(p.net_pnl), sign(p.net_pnl)], ['Trades', p.trades, ''], ['Win rate', p.win_rate == null ? '—' : (p.win_rate * 100).toFixed(0) + '%', ''], ['Profit factor', p.profit_factor ?? '—', ''], ['Avg win / loss', `${inr(p.avg_win)} / ${inr(p.avg_loss)}`, ''], ['Max drawdown', inr(p.max_drawdown), 'neg']].map(([k, v, c]) => `<div class="card tile"><span class="s">${k}</span><span class="v ${c}" style="font-size:18px">${v}</span></div>`).join('');
    lineChart($('#rp-equity'), [{ values: p.equity.map((e, i) => ({ x: i, y: e.equity })), color: '#22d3ee', area: true }], { height: 180, zero: true });
    bars($('#rp-daily'), p.daily.map(d => ({ label: d.key, value: d.pnl })), { height: 180 });
    $('#rp-by').innerHTML = `<table><thead><tr><th>Bucket</th><th class="num">Trades</th><th class="num">Win%</th><th class="num">P&L</th></tr></thead><tbody>${[...p.by_strategy, ...p.by_market].map(b => `<tr><td>${b.key}</td><td class="num">${b.trades}</td><td class="num">${b.win_rate == null ? '—' : (b.win_rate * 100).toFixed(0)}</td><td class="num ${sign(b.pnl)}">${inr(b.pnl)}</td></tr>`).join('')}</tbody></table>`;
    api('/api/reports/trades?limit=200').then(tr => { $('#rp-trades').innerHTML = tr.length ? `<table><thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">P&L</th><th>Strategy</th><th>Reason</th></tr></thead><tbody>${tr.map(t => `<tr><td>${dts(t.ts_close)}</td><td>${t.symbol}</td><td>${t.side}</td><td class="num">${t.qty}</td><td class="num">${fmt(t.entry, 2)}</td><td class="num">${fmt(t.exit, 2)}</td><td class="num ${sign(t.pnl)}">${inr(t.pnl)}</td><td>${t.strategy}</td><td class="sans muted">${esc(t.reason)}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No closed trades yet</div>'; });
  },
  async backtest() {
    const body = { strategy: $('#bt-key').value, underlying: $('#bt-under').value, days: parseInt($('#bt-days').value) || 60, lots: parseInt($('#bt-lots').value) || 1, seed: parseInt($('#bt-seed').value) || 42, params: {} };
    if ($('#bt-delta').value) body.params.delta = parseFloat($('#bt-delta').value); if ($('#bt-sl').value) body.params.stop_loss_pct = parseFloat($('#bt-sl').value); if ($('#bt-tg').value) body.params.target_pct = parseFloat($('#bt-tg').value);
    $('#bt-res').innerHTML = '<div class="muted">Running…</div>'; const r = await act(() => api('/api/reports/backtest', 'POST', body)); if (!r) return;
    $('#bt-res').innerHTML = `<div class="grid g6">${[['Net P&L', inr(r.net_pnl), sign(r.net_pnl)], ['Days', r.days, ''], ['Win rate', r.win_rate == null ? '—' : (r.win_rate * 100).toFixed(0) + '%', ''], ['Profit factor', r.profit_factor ?? '—', ''], ['Avg win / loss', `${inr(r.avg_win)} / ${inr(r.avg_loss)}`, ''], ['Max DD', inr(r.max_drawdown), 'neg']].map(([k, v, c]) => `<div class="tile"><span class="s">${k}</span><span class="v ${c}" style="font-size:16px">${v}</span></div>`).join('')}</div><div class="muted" style="margin-top:6px;font-size:11px">Exits: target ${r.exits.TARGET} · stop ${r.exits.STOP_LOSS} · square-off ${r.exits.SQUARE_OFF} · params ${JSON.stringify(r.params)}</div>`;
    lineChart($('#bt-eq'), [{ values: r.equity.map((e, i) => ({ x: i, y: e })), color: '#a78bfa', area: true }], { height: 180, zero: true });
  },
};

views.system = {
  mount() { $('#view-system').innerHTML = `<div class="grid g4" id="sy-tiles"></div><div class="grid g-1-2" style="margin-top:14px"><div class="card"><h3>Services</h3><div id="sy-services"></div><div class="hr"></div><h3>Feed &amp; broker</h3><div id="sy-fb"></div><div class="hr"></div><h3>Live broker session <span class="right"><button class="btn sm" id="sy-live">Reconnect</button></span></h3><div id="sy-live-st" class="muted" style="font-size:11px"></div><div id="sy-zerodha" class="row hidden" style="margin-top:8px"><button class="btn sm" id="sy-zlogin">Open Kite login</button><input id="sy-ztoken" placeholder="paste request_token from redirect URL" style="flex:1"><button class="btn sm primary" id="sy-zsession">Create session</button></div></div><div class="card"><h3>Logs <span class="right"><select id="sy-level" style="width:auto"><option value="">all levels</option><option>INFO</option><option>WARNING</option><option>CRITICAL</option></select><input id="sy-src" placeholder="source filter" style="width:130px"><button class="btn sm" id="sy-refresh">Refresh</button></span></h3><div class="log scroll tall" id="sy-logs"></div></div></div><div class="card" style="margin-top:14px"><h3>Audit trail (hash-chained) <span class="right muted" id="sy-audit-n"></span></h3><div class="log scroll" id="sy-audit"></div></div>`;
    $('#sy-refresh').onclick = () => views.system.activate(); $('#sy-level').onchange = () => views.system.activate();
    $('#sy-live').onclick = () => act(() => api('/api/broker/live/connect', 'POST'), 'Live session reconnected').then(() => views.system.activate());
    $('#sy-zlogin').onclick = async () => { const r = await act(() => api('/api/broker/zerodha/login-url')); if (r) window.open(r.login_url, '_blank'); };
    $('#sy-zsession').onclick = () => act(() => api('/api/broker/zerodha/session', 'POST', { request_token: $('#sy-ztoken').value.trim() }), 'Zerodha session created').then(() => views.system.activate()); },
  async activate() { S.health = await api('/api/system/health'); S.logs = await api(`/api/system/logs?limit=300${$('#sy-level').value ? '&level=' + $('#sy-level').value : ''}${$('#sy-src').value ? '&source=' + encodeURIComponent($('#sy-src').value) : ''}`); const a = await api('/api/system/audit?limit=150'); S.audit = a.rows; $('#sy-audit-n').textContent = a.count + ' records'; this.draw(); renderLogs(); },
  render(s) { const k = s.live_session; const el = $('#sy-live-st'); if (!el) return; $('#sy-zerodha').classList.toggle('hidden', !(k && k.provider === 'zerodha')); el.innerHTML = k ? `<div class="kv"><span>Provider</span><b>${k.provider}${k.user_id ? ' · ' + k.user_id : ''}${k.needs_daily_login ? ' · daily login' : ''}</b></div><div class="kv"><span>Session</span><b class="${k.authenticated ? 'ok' : 'bad'}">${k.authenticated ? 'authenticated' : 'not authenticated'}${k.last_error ? ' · ' + esc(k.last_error) : ''}</b></div><div class="kv"><span>Instruments</span><b>${Object.entries(k.tokens || {}).map(([a, b]) => a + '→' + b).join(' ') || '—'}</b></div><div class="kv"><span>API calls</span><b>${k.calls} (${k.errors} errors)</b></div><div class="kv"><span>Option chain polls</span><b>${k.chain.polls} · ${k.chain.quotes} live quotes${k.chain.last_error ? ' · ' + esc(k.chain.last_error) : ''}</b></div>${k.missing_credentials.length ? `<div class="warn">Missing: ${k.missing_credentials.join(', ')}</div>` : ''}` : `<div class="kv"><span>Data source</span><b>${s.data_source} (set DATA_SOURCE=kotak, zerodha or angel + credentials in .env)</b></div>`; },
  draw() { const h = S.health; if (!h) return; $('#sy-tiles').innerHTML = [['Uptime', Math.floor(h.uptime_seconds / 60) + ' min'], ['CPU', h.cpu_pct + '%'], ['Memory', (h.memory.used_pct ?? '—') + '%'], ['Loop lag', h.loop_lag_ms + ' ms']].map(([k, v]) => `<div class="card tile"><span class="s">${k}</span><span class="v" style="font-size:18px">${v}</span></div>`).join(''); $('#sy-services').innerHTML = Object.entries(h.services).map(([k, v]) => `<div class="kv"><span>${k}</span><b class="${v.ok ? 'ok' : 'bad'}">${v.ok ? '● ' : '○ '}${esc(v.detail)}</b></div>`).join(''); $('#sy-fb').innerHTML = `<div class="kv"><span>Feed</span><b>${h.feed.name} · ${h.feed.ticks} ticks · ${h.feed.errors} errors</b></div><div class="kv"><span>Broker</span><b>${h.broker.name} · ${h.broker.connected ? 'connected' : 'disconnected'} · ${h.broker.live ? 'LIVE' : 'paper'}</b></div><div class="kv"><span>WS clients</span><b>${h.websocket_clients}</b></div><div class="kv"><span>Python</span><b>${h.python}</b></div>`; $('#sy-audit').innerHTML = S.audit.map(r => `<div class="l"><span>${ts(r.ts)}</span><span>${esc(r.actor)}</span><span>${esc(r.event)}</span><span class="muted">${esc(JSON.stringify(r.detail)).slice(0, 160)}</span></div>`).join(''); },
};
function renderLogs() { const el = $('#sy-logs'); if (el) el.innerHTML = S.logs.map(l => `<div class="l ${l.level}"><span>${ts(l.ts)}</span><span>${l.level}</span><span>${esc(l.source)}</span><span>${esc(l.message)}</span></div>`).join(''); }

views.settings = {
  mount() { $('#view-settings').innerHTML = `<div class="grid g-1-2"><div class="card"><h3>Trading schedule (IST)</h3><div class="form"><label>Terminal start<input id="sc-tstart"></label><label>Terminal end (square-off)<input id="sc-tend"></label><label>Entry from<input id="sc-start"></label><label>Entry until<input id="sc-end"></label><label>Square-off<input id="sc-sq"></label><label>MCX square-off<input id="sc-mcx"></label></div><div class="row" style="margin-top:10px"><button class="btn primary" id="sc-save">Save</button></div><div class="hr"></div><h3>Event calendar (no-trade windows)</h3><textarea id="ev-json" rows="6" style="font-family:var(--mono)"></textarea><div class="row" style="margin-top:8px"><button class="btn" id="ev-save">Save events</button><button class="btn ghost" id="al-test">Send test alert</button></div></div><div class="card"><h3>Configuration (from .env, secrets masked)</h3><div class="log scroll tall" id="se-conf"></div></div></div>`;
    $('#sc-save').onclick = () => act(() => api('/api/schedule', 'POST', { terminal_start_time: $('#sc-tstart').value, terminal_end_time: $('#sc-tend').value, entry_window_start: $('#sc-start').value, entry_window_end: $('#sc-end').value, square_off_time: $('#sc-sq').value, mcx_square_off_time: $('#sc-mcx').value }), 'Schedule saved');
    $('#ev-save').onclick = () => { try { act(() => api('/api/agents/events', 'POST', { events: JSON.parse($('#ev-json').value) }), 'Events saved'); } catch (e) { toast('Invalid JSON', 'err'); } };
    $('#al-test').onclick = () => act(() => api('/api/alerts/test', 'POST'), 'Test alert sent');
  },
  async activate() { S.settings = await api('/api/settings'); const sc = S.snap.scheduler; $('#sc-tstart').value = sc.operating_window[0]; $('#sc-tend').value = sc.operating_window[1]; $('#sc-start').value = sc.entry_window[0]; $('#sc-end').value = sc.entry_window[1]; $('#sc-sq').value = sc.square_off; $('#sc-mcx').value = sc.mcx_square_off; $('#se-conf').innerHTML = Object.entries(S.settings.settings).map(([k, v]) => `<div class="l" style="grid-template-columns:260px 1fr"><span>${k}</span><span class="muted">${esc(String(v))}</span></div>`).join('') + `<div class="l" style="grid-template-columns:260px 1fr"><span>auth</span><span class="muted">${esc(JSON.stringify(S.settings.auth))}</span></div>`; if (!$('#ev-json').value) $('#ev-json').value = JSON.stringify([{ date: '2026-10-01', name: 'RBI MPC decision', impact: 'HIGH' }], null, 1); },
  render() { },
};

function mountAll() { Object.values(views).forEach(v => v.mount && v.mount()); }
setInterval(() => { if (S.snap) $('#sb-clock').textContent = new Date().toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' }); }, 1000);
boot();

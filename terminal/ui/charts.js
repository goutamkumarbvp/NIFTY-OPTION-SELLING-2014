// Minimal dependency-free canvas charts tuned for the terminal's dark theme.
const C = { line: '#1e293b', text: '#64748b', accent: '#22d3ee', green: '#22c55e', red: '#f43f5e', blue: '#38bdf8', purple: '#a78bfa', amber: '#f59e0b' };

function prep(canvas, h) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || canvas.parentElement?.clientWidth || 300;
  const height = h || canvas.clientHeight || 120;
  canvas.width = Math.floor(w * dpr); canvas.height = Math.floor(height * dpr);
  canvas.style.height = height + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, height);
  return { ctx, w, h: height };
}

export function sparkline(canvas, values, color = C.accent, h = 36) {
  const { ctx, w, h: H } = prep(canvas, h);
  if (!values || values.length < 2) return;
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
  ctx.beginPath();
  values.forEach((v, i) => { const x = i / (values.length - 1) * w, y = H - 3 - (v - min) / span * (H - 6); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke();
  const g = ctx.createLinearGradient(0, 0, 0, H); g.addColorStop(0, color + '55'); g.addColorStop(1, color + '00');
  ctx.lineTo(w, H); ctx.lineTo(0, H); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
}

function axes(ctx, w, h, pad, min, max, fmt = v => v.toFixed(0)) {
  ctx.strokeStyle = C.line; ctx.fillStyle = C.text; ctx.font = '10px ui-monospace, monospace'; ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (h - pad.t - pad.b) * i / 4; const v = max - (max - min) * i / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(fmt(v), pad.l - 4, y + 3);
  }
}

export function lineChart(canvas, series, opts = {}) {
  // series: [{values:[{x,y}], color, area}]
  const h = opts.height || 180; const { ctx, w, h: H } = prep(canvas, h);
  const pad = { l: 46, r: 10, t: 10, b: 18 };
  const all = series.flatMap(s => s.values);
  if (!all.length) { ctx.fillStyle = C.text; ctx.fillText('no data', pad.l, H / 2); return; }
  let min = Math.min(...all.map(p => p.y)), max = Math.max(...all.map(p => p.y));
  if (opts.zero) { min = Math.min(min, 0); max = Math.max(max, 0); }
  if (min === max) { min -= 1; max += 1; }
  const xmin = Math.min(...all.map(p => p.x)), xmax = Math.max(...all.map(p => p.x)) || 1;
  const X = x => pad.l + (x - xmin) / ((xmax - xmin) || 1) * (w - pad.l - pad.r);
  const Y = y => pad.t + (max - y) / (max - min) * (H - pad.t - pad.b);
  axes(ctx, w, H, pad, min, max, opts.fmt);
  if (opts.zero) { ctx.strokeStyle = '#334155'; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(pad.l, Y(0)); ctx.lineTo(w - pad.r, Y(0)); ctx.stroke(); ctx.setLineDash([]); }
  (opts.vlines || []).forEach(v => { ctx.strokeStyle = v.color || C.amber; ctx.setLineDash([4, 3]); ctx.beginPath(); ctx.moveTo(X(v.x), pad.t); ctx.lineTo(X(v.x), H - pad.b); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = v.color || C.amber; ctx.textAlign = 'center'; ctx.fillText(v.label || '', X(v.x), H - 6); });
  series.forEach(s => {
    if (!s.values.length) return;
    ctx.beginPath(); s.values.forEach((p, i) => i ? ctx.lineTo(X(p.x), Y(p.y)) : ctx.moveTo(X(p.x), Y(p.y)));
    ctx.strokeStyle = s.color || C.accent; ctx.lineWidth = s.width || 1.6; ctx.stroke();
    if (s.area) {
      const g = ctx.createLinearGradient(0, pad.t, 0, H - pad.b); g.addColorStop(0, (s.color || C.accent) + '44'); g.addColorStop(1, (s.color || C.accent) + '00');
      ctx.lineTo(X(s.values.at(-1).x), Y(opts.zero ? 0 : min)); ctx.lineTo(X(s.values[0].x), Y(opts.zero ? 0 : min)); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
    }
    if (s.fillSign) { // payoff style: green above zero, red below
      ctx.save(); ctx.beginPath(); ctx.rect(pad.l, pad.t, w - pad.l - pad.r, Y(0) - pad.t); ctx.clip();
      ctx.beginPath(); s.values.forEach((p, i) => i ? ctx.lineTo(X(p.x), Y(p.y)) : ctx.moveTo(X(p.x), Y(p.y))); ctx.lineTo(X(s.values.at(-1).x), Y(0)); ctx.lineTo(X(s.values[0].x), Y(0)); ctx.closePath(); ctx.fillStyle = C.green + '33'; ctx.fill(); ctx.restore();
      ctx.save(); ctx.beginPath(); ctx.rect(pad.l, Y(0), w - pad.l - pad.r, H - pad.b - Y(0)); ctx.clip();
      ctx.beginPath(); s.values.forEach((p, i) => i ? ctx.lineTo(X(p.x), Y(p.y)) : ctx.moveTo(X(p.x), Y(p.y))); ctx.lineTo(X(s.values.at(-1).x), Y(0)); ctx.lineTo(X(s.values[0].x), Y(0)); ctx.closePath(); ctx.fillStyle = C.red + '33'; ctx.fill(); ctx.restore();
    }
  });
  if (opts.xlabels) { ctx.fillStyle = C.text; ctx.textAlign = 'center'; opts.xlabels.forEach(l => ctx.fillText(l.label, X(l.x), H - 5)); }
}

export function candles(canvas, cds, opts = {}) {
  const h = opts.height || 220; const { ctx, w, h: H } = prep(canvas, h);
  const pad = { l: 56, r: 10, t: 10, b: 18 };
  if (!cds || cds.length < 2) { ctx.fillStyle = C.text; ctx.fillText('warming up…', pad.l, H / 2); return; }
  const min = Math.min(...cds.map(c => c.low)), max = Math.max(...cds.map(c => c.high));
  const span = (max - min) || 1;
  const Y = y => pad.t + (max - y) / span * (H - pad.t - pad.b);
  axes(ctx, w, H, pad, min, max, v => v.toFixed(1));
  const bw = (w - pad.l - pad.r) / cds.length;
  cds.forEach((c, i) => {
    const x = pad.l + i * bw + bw / 2; const up = c.close >= c.open;
    ctx.strokeStyle = ctx.fillStyle = up ? C.green : C.red;
    ctx.beginPath(); ctx.moveTo(x, Y(c.high)); ctx.lineTo(x, Y(c.low)); ctx.stroke();
    const top = Y(Math.max(c.open, c.close)), bot = Y(Math.min(c.open, c.close));
    ctx.fillRect(x - Math.max(1, bw * 0.3), top, Math.max(2, bw * 0.6), Math.max(1, bot - top));
  });
  (opts.lines || []).forEach(l => { if (l.y == null) return; ctx.strokeStyle = l.color; ctx.setLineDash([4, 3]); ctx.beginPath(); ctx.moveTo(pad.l, Y(l.y)); ctx.lineTo(w - pad.r, Y(l.y)); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = l.color; ctx.textAlign = 'left'; ctx.fillText(l.label, pad.l + 4, Y(l.y) - 3); });
  ctx.fillStyle = C.text; ctx.textAlign = 'center';
  [0, Math.floor(cds.length / 2), cds.length - 1].forEach(i => { const d = new Date(cds[i].ts * 1000); ctx.fillText(d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' }), pad.l + i * bw + bw / 2, H - 5); });
}

export function bars(canvas, items, opts = {}) {
  // items: [{label, value}]
  const h = opts.height || 160; const { ctx, w, h: H } = prep(canvas, h);
  const pad = { l: 50, r: 10, t: 10, b: 22 };
  if (!items.length) { ctx.fillStyle = C.text; ctx.fillText('no data', pad.l, H / 2); return; }
  let min = Math.min(0, ...items.map(i => i.value)), max = Math.max(0, ...items.map(i => i.value));
  if (min === max) max = min + 1;
  const Y = y => pad.t + (max - y) / (max - min) * (H - pad.t - pad.b);
  axes(ctx, w, H, pad, min, max, opts.fmt);
  const bw = (w - pad.l - pad.r) / items.length;
  items.forEach((it, i) => {
    const x = pad.l + i * bw + bw * 0.15; const y0 = Y(0), y1 = Y(it.value);
    ctx.fillStyle = it.color || (it.value >= 0 ? C.green : C.red);
    ctx.fillRect(x, Math.min(y0, y1), bw * 0.7, Math.abs(y1 - y0) || 1);
  });
  ctx.fillStyle = C.text; ctx.textAlign = 'center';
  const step = Math.ceil(items.length / 8);
  items.forEach((it, i) => { if (i % step === 0) ctx.fillText(String(it.label).slice(-5), pad.l + i * bw + bw / 2, H - 6); });
}

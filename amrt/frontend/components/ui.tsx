"use client";
import React from "react";

// ---- truthful status labels -------------------------------------------------------------
const LABEL_CLASS: Record<string, string> = {
  "LIVE DATA VERIFIED": "b-green",
  "LIVE (UNVERIFIED)": "b-amber",
  "HISTORICAL REPLAY": "b-blue",
  SIMULATED: "b-purple",
  "DATA UNAVAILABLE": "b-red",
  "OFFICIAL END-OF-DAY (imported)": "b-blue",
};

export function DataLabelBadge({ label }: { label?: string | null }) {
  const l = label || "DATA UNAVAILABLE";
  return <span className={`badge ${LABEL_CLASS[l] ?? "b-grey"}`} title="Data provenance label">{l}</span>;
}

const STATE_CLASS: Record<string, string> = {
  FILLED: "b-green", ACKNOWLEDGED: "b-blue", PARTIALLY_FILLED: "b-blue", SUBMITTING: "b-amber", INTENT_PERSISTED: "b-grey",
  CANCELLED: "b-grey", REJECTED: "b-red", REJECTED_PRE_TRADE: "b-red", "ORDER STATE UNKNOWN": "b-red blink", NOT_FOUND_AT_BROKER: "b-grey",
  HEALTHY: "b-green", DEGRADED: "b-amber", STALE: "b-amber", UNAVAILABLE: "b-red", FAILED: "b-red", RECOVERING: "b-blue", QUARANTINED: "b-purple", UNKNOWN: "b-grey",
  ADVISORY: "b-blue", "REQUIRES APPROVAL": "b-amber", AUTHORIZED: "b-green", "NO ACTION": "b-grey", "INSUFFICIENT DATA": "b-red",
  SUPPORTIVE: "b-green", NEUTRAL: "b-grey", CAUTION: "b-amber", BLOCK: "b-red", INSUFFICIENT_DATA: "b-red",
  READY: "b-green", "NOT READY": "b-red", BLOCKED: "b-red", OK: "b-green", ERROR: "b-red", TIMEOUT: "b-red",
  PENDING: "b-amber", APPROVED: "b-green", EXPIRED: "b-grey", OPEN: "b-red", ESCALATED: "b-red", RESOLVED: "b-green",
  VERIFIED: "b-green", PARTIAL: "b-amber", BLOCKED_STATUS: "b-red",
  SEV1: "b-red", SEV2: "b-red", SEV3: "b-amber", SEV4: "b-grey", CRITICAL: "b-red", WARNING: "b-amber", INFO: "b-blue",
};

export function Badge({ v, title }: { v?: string | null; title?: string }) {
  const s = v ?? "—";
  return <span className={`badge ${STATE_CLASS[s] ?? "b-grey"}`} title={title}>{s}</span>;
}

export function Card({ title, right, children, className }: { title?: React.ReactNode; right?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={`card ${className ?? ""}`}>
      {(title || right) && (
        <header className="card-h">
          <h2>{title}</h2>
          <div>{right}</div>
        </header>
      )}
      {children}
    </section>
  );
}

export function ErrorNote({ e }: { e: { code?: string; message?: string } | null }) {
  if (!e) return null;
  return <div className="note err">{e.code}: {e.message}</div>;
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Json({ v }: { v: unknown }) {
  return <pre className="json">{JSON.stringify(v, null, 2)}</pre>;
}

export function KV({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl className="kv">
      {rows.map(([k, v]) => (
        <React.Fragment key={k}>
          <dt>{k}</dt>
          <dd>{v ?? "—"}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

// ---- tiny SVG line chart (no external chart library) ------------------------------------
export function LineChart({ series, height = 160, yLabel, refLine }: {
  series: { name: string; color: string; points: { x: number; y: number | null }[] }[];
  height?: number; yLabel?: string; refLine?: number;
}) {
  const all = series.flatMap((s) => s.points.filter((p) => p.y !== null && Number.isFinite(p.y as number)));
  if (all.length < 2) return <Empty>Not enough history to draw a chart yet.</Empty>;
  const xs = all.map((p) => p.x), ys = all.map((p) => p.y as number);
  if (refLine !== undefined) ys.push(refLine);
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)];
  let [y0, y1] = [Math.min(...ys), Math.max(...ys)];
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const W = 600, H = height, pad = 32;
  const sx = (x: number) => pad + ((x - x0) / Math.max(1e-9, x1 - x0)) * (W - pad - 8);
  const sy = (y: number) => H - 18 - ((y - y0) / (y1 - y0)) * (H - 30);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart" role="img" aria-label={yLabel}>
      <text x={4} y={12} className="axis">{yLabel}</text>
      <text x={4} y={sy(y1) + 4} className="axis">{y1.toFixed(2)}</text>
      <text x={4} y={sy(y0)} className="axis">{y0.toFixed(2)}</text>
      {refLine !== undefined && <line x1={pad} x2={W - 8} y1={sy(refLine)} y2={sy(refLine)} className="ref" />}
      {series.map((s) => {
        const pts = s.points.filter((p) => p.y !== null && Number.isFinite(p.y as number));
        const d = pts.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y as number).toFixed(1)}`).join(" ");
        return <path key={s.name} d={d} fill="none" stroke={s.color} strokeWidth={2} />;
      })}
      <g className="legend">
        {series.map((s, i) => (
          <text key={s.name} x={pad + i * 170} y={H - 2} fill={s.color}>■ {s.name}</text>
        ))}
      </g>
    </svg>
  );
}

export function Bars({ rows, height = 220 }: { rows: { label: string; a: number; b: number }[]; height?: number }) {
  if (!rows.length) return <Empty>No data.</Empty>;
  const max = Math.max(1, ...rows.flatMap((r) => [Math.abs(r.a), Math.abs(r.b)]));
  const W = 600, bw = (W - 40) / rows.length;
  return (
    <svg viewBox={`0 0 ${W} ${height}`} className="chart">
      {rows.map((r, i) => {
        const ha = (Math.abs(r.a) / max) * (height / 2 - 20), hb = (Math.abs(r.b) / max) * (height / 2 - 20);
        const x = 30 + i * bw;
        return (
          <g key={r.label}>
            <rect x={x} y={height / 2 - ha} width={bw * 0.4} height={ha} className="bar-ce" />
            <rect x={x + bw * 0.42} y={height / 2 - hb} width={bw * 0.4} height={hb} className="bar-pe" />
            {i % Math.ceil(rows.length / 10) === 0 && <text x={x} y={height - 4} className="axis">{r.label}</text>}
          </g>
        );
      })}
      <text x={30} y={12} className="axis">■ CE OI  ■ PE OI</text>
    </svg>
  );
}

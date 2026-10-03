"use client";
import { useState } from "react";
import { useShell } from "@/components/Shell";
import { Card, Empty, Json, KV, LineChart } from "@/components/ui";
import { fmt } from "@/lib/api";

type Res = { status?: string; reason?: string; backtest: { label: string; days_traded: number; days_in_data: number; metrics: Record<string, number | null>; costs_total: number; leakage_checks: { total: number; failed: unknown[] }; disclaimer: string };
  trades: { day: string; net_pnl: number; exit_reason: string; flags: string[] }[]; walk_forward: Record<string, unknown>; sensitivity: Record<string, Record<string, number | null>>; stress: Record<string, unknown>; validated: unknown };

export default function Backtest() {
  const { act } = useShell();
  const [b, setB] = useState({ underlying: "NIFTY", strategy_id: "ROLLING_ATM_IRON_FLY", version: "1.0", start: "", end: "", walk_forward_folds: 4 });
  const [r, setR] = useState<Res | null>(null);
  const run = async () => setR(await act<Res>("/api/backtest", { ...b, start: b.start || null, end: b.end || null }));
  let cum = 0;
  return (
    <>
      <div className="note">Backtests run on chain snapshots this system recorded. Fills at the next snapshot at the touch plus slippage, with dated charges. Results on SIMULATED data are labelled as such and never count as validation. Past results do not predict future results.</div>
      <Card title="Run">
        <div className="row">
          <input value={b.underlying} onChange={(e) => setB({ ...b, underlying: e.target.value.toUpperCase() })} style={{ width: 110 }} />
          <select value={b.strategy_id} onChange={(e) => setB({ ...b, strategy_id: e.target.value })}><option>ROLLING_ATM_IRON_FLY</option><option>ROLLING_ATM_STRADDLE</option><option>MCX_ATM_IRON_FLY</option></select>
          <input type="date" value={b.start} onChange={(e) => setB({ ...b, start: e.target.value })} /><input type="date" value={b.end} onChange={(e) => setB({ ...b, end: e.target.value })} />
          <button className="primary" onClick={run}>Run backtest + walk-forward</button>
        </div>
      </Card>
      {r?.status && <Empty>{r.status}: {r.reason}</Empty>}
      {r?.backtest && (
        <>
          <Card title={`${r.backtest.label}`} right={<span className="muted">{r.backtest.disclaimer}</span>}>
            <KV rows={[["Days in data / traded", `${r.backtest.days_in_data} / ${r.backtest.days_traded}`], ...Object.entries(r.backtest.metrics).map(([k, v]) => [k, fmt(v as number)] as [string, string]),
              ["Costs total", `₹${fmt(r.backtest.costs_total)}`], ["Leakage checks", `${r.backtest.leakage_checks.total} run, ${r.backtest.leakage_checks.failed.length} failed`],
              ["Validated evidence", r.validated ? "yes" : "no (simulated, too short, or no walk-forward)"]]} />
            <LineChart yLabel="Cumulative net P&L (₹)" refLine={0} series={[{ name: "equity", color: "#79c0ff", points: r.trades.map((t, i) => ({ x: i, y: (cum += t.net_pnl) })) }]} />
          </Card>
          <div className="grid"><Card title="Walk-forward"><Json v={r.walk_forward} /></Card><Card title="Sensitivity (net P&L)"><Json v={Object.fromEntries(Object.entries(r.sensitivity).map(([k, v]) => [k, v.net_pnl]))} /></Card><Card title="Stress"><Json v={r.stress} /></Card></div>
        </>)}
    </>
  );
}

"use client";
import { useState } from "react";
import { Bars, Card, DataLabelBadge, Empty, ErrorNote, KV, LineChart } from "@/components/ui";
import { fmt, ts } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

type Leg = { instrument_key: string; ltp: number | null; bid: number | null; ask: number | null; oi: number | null; oi_change: number | null; iv: number | null; volume: number | null };
type SV = { strike: number; side: string; value: number } | null;
type Ratio = { value: number | null; numerator: number; denominator: number; interpretation: string; note: string } | null;
type Chain = {
  label: string; age_ms: number | null; underlying?: string; expiry?: string; spot?: number; ts?: number; source?: string; feed_error?: string | null;
  rows?: { strike: number; ce: Leg | null; pe: Leg | null }[];
  analytics?: { status: string; atm_strike: number | null; window: number[]; oi_coverage_pct: number; ce_max_oi: SV; ce_second_oi: SV; pe_max_oi: SV; pe_second_oi: SV;
    max_buildup: SV; max_unwinding: SV; pcr_total_oi: Ratio; pcr_change_oi: Ratio; pcr_total_oi_full: Ratio; pcr_change_oi_full: Ratio;
    support_zones: SV[]; resistance_zones: SV[]; notes: string[]; concentration: Record<string, number | null> } | null;
  windows?: Record<string, { status: string; max_buildup?: { strike: number; side: string; oi_change: number } | null; max_unwinding?: { strike: number; side: string; oi_change: number } | null;
    ce_oi_change?: number; pe_oi_change?: number; pcr_total_oi_change?: number | null }>;
};
type Pcr = { label: string; series: { ts: number; spot: number | null; pcr_total_oi: number | null; pcr_change_oi: number | null; status: string }[]; definition: string };

export default function Market() {
  const und = usePoll<{ underlyings: string[] }>("/api/market/underlyings", 0);
  const [sel, setSel] = useState<string | null>(null);
  const u = sel ?? und.data?.underlyings[0] ?? null;
  const chain = usePoll<Chain>(u ? `/api/market/chain/${u}` : null, 3000);
  const pcr = usePoll<Pcr>(u ? `/api/market/pcr/${u}` : null, 10000);
  const c = chain.data, a = c?.analytics;
  const win = a?.window ?? [];
  const rows = (c?.rows ?? []).filter((r) => !win.length || (r.strike >= win[0] && r.strike <= win[win.length - 1]));
  const hl = (strike: number, side: "CE" | "PE") => {
    const m = side === "CE" ? a?.ce_max_oi : a?.pe_max_oi, s2 = side === "CE" ? a?.ce_second_oi : a?.pe_second_oi;
    return m?.strike === strike ? "hi" : s2?.strike === strike ? "hi2" : "";
  };
  return (
    <>
      <div className="tabs">{(und.data?.underlyings ?? []).map((x) => <button key={x} className={x === u ? "on" : ""} onClick={() => setSel(x)}>{x}</button>)}</div>
      <ErrorNote e={chain.error} />
      <Card title={<>{u} option chain — ATM ± 10 <DataLabelBadge label={c?.label} /></>} right={<span className="muted">{c?.source} · {ts(c?.ts)} · age {fmt((c?.age_ms ?? 0) / 1000, 1)}s</span>}>
        {!c?.rows ? <Empty>{c?.feed_error ?? "DATA UNAVAILABLE — no chain received."}</Empty> : (
          <>
            <KV rows={[["Spot", fmt(c.spot)], ["Expiry", c.expiry], ["ATM strike", fmt(a?.atm_strike)], ["Window status", `${a?.status} (OI coverage ${fmt(a?.oi_coverage_pct, 1)}%)`]]} />
            <div className="scroll">
              <table><thead><tr><th>CE OI</th><th>CE ΔOI</th><th>CE IV</th><th>CE bid</th><th>CE ask</th><th>CE LTP</th><th className="l">Strike</th><th>PE LTP</th><th>PE bid</th><th>PE ask</th><th>PE IV</th><th>PE ΔOI</th><th>PE OI</th></tr></thead>
                <tbody>{rows.map((r) => (
                  <tr key={r.strike} className={r.strike === a?.atm_strike ? "atm" : ""}>
                    <td className={hl(r.strike, "CE")}>{fmt(r.ce?.oi, 0)}</td><td className={(r.ce?.oi_change ?? 0) < 0 ? "neg" : "pos"}>{fmt(r.ce?.oi_change, 0)}</td><td>{fmt(r.ce?.iv)}</td>
                    <td>{fmt(r.ce?.bid)}</td><td>{fmt(r.ce?.ask)}</td><td>{fmt(r.ce?.ltp)}</td><td className="l"><strong>{r.strike}</strong></td>
                    <td>{fmt(r.pe?.ltp)}</td><td>{fmt(r.pe?.bid)}</td><td>{fmt(r.pe?.ask)}</td><td>{fmt(r.pe?.iv)}</td>
                    <td className={(r.pe?.oi_change ?? 0) < 0 ? "neg" : "pos"}>{fmt(r.pe?.oi_change, 0)}</td><td className={hl(r.strike, "PE")}>{fmt(r.pe?.oi, 0)}</td>
                  </tr>))}
                </tbody></table>
            </div>
            <Bars rows={rows.map((r) => ({ label: String(r.strike), a: r.ce?.oi ?? 0, b: r.pe?.oi ?? 0 }))} />
          </>)}
      </Card>
      {a && (
        <div className="grid">
          <Card title="Observed (facts)">
            <KV rows={[
              ["Max CE OI", a.ce_max_oi ? `${a.ce_max_oi.strike} (${fmt(a.ce_max_oi.value, 0)})` : "—"], ["2nd CE OI", a.ce_second_oi ? `${a.ce_second_oi.strike} (${fmt(a.ce_second_oi.value, 0)})` : "—"],
              ["Max PE OI", a.pe_max_oi ? `${a.pe_max_oi.strike} (${fmt(a.pe_max_oi.value, 0)})` : "—"], ["2nd PE OI", a.pe_second_oi ? `${a.pe_second_oi.strike} (${fmt(a.pe_second_oi.value, 0)})` : "—"],
              ["Largest buildup (ΔOI)", a.max_buildup ? `${a.max_buildup.strike} ${a.max_buildup.side} +${fmt(a.max_buildup.value, 0)}` : "—"],
              ["Largest unwinding (ΔOI)", a.max_unwinding ? `${a.max_unwinding.strike} ${a.max_unwinding.side} ${fmt(a.max_unwinding.value, 0)}` : "—"],
              ["Total OI PCR (window)", a.pcr_total_oi?.value === null || !a.pcr_total_oi ? "undefined" : fmt(a.pcr_total_oi.value, 3)],
              ["Change-in-OI PCR (window)", !a.pcr_change_oi || a.pcr_change_oi.value === null ? "undefined" : `${fmt(a.pcr_change_oi.value, 3)} ${a.pcr_change_oi.interpretation}`],
              ["Total OI PCR (full chain)", fmt(a.pcr_total_oi_full?.value, 3)], ["Top-3 OI share CE / PE", `${fmt(a.concentration?.ce_top3_share, 3)} / ${fmt(a.concentration?.pe_top3_share, 3)}`],
            ]} />
          </Card>
          <Card title="Inferred (labelled, not predictions)">
            <KV rows={[
              ["Support zones (PE OI ≤ spot)", a.support_zones.map((z) => z?.strike).join(", ") || "—"],
              ["Resistance zones (CE OI ≥ spot)", a.resistance_zones.map((z) => z?.strike).join(", ") || "—"],
            ]} />
            {a.notes.map((n) => <p key={n} className="muted">{n}</p>)}
          </Card>
          <Card title="Look-back windows (ΔOI vs earlier snapshot)">
            <table><thead><tr><th className="l">Window</th><th className="l">Status</th><th>CE ΔOI</th><th>PE ΔOI</th><th className="l">Max buildup</th><th className="l">Max unwinding</th><th>ΔPCR</th></tr></thead>
              <tbody>{Object.entries(c?.windows ?? {}).map(([k, w]) => (
                <tr key={k}><td className="l">{k}</td><td className="l">{w.status}</td><td>{fmt(w.ce_oi_change, 0)}</td><td>{fmt(w.pe_oi_change, 0)}</td>
                  <td className="l">{w.max_buildup ? `${w.max_buildup.strike} ${w.max_buildup.side} +${fmt(w.max_buildup.oi_change, 0)}` : "—"}</td>
                  <td className="l">{w.max_unwinding ? `${w.max_unwinding.strike} ${w.max_unwinding.side} ${fmt(w.max_unwinding.oi_change, 0)}` : "—"}</td><td>{fmt(w.pcr_total_oi_change, 3)}</td></tr>))}
              </tbody></table>
          </Card>
        </div>)}
      <Card title={<>PCR history <DataLabelBadge label={pcr.data?.label} /></>} right={<span className="muted">{pcr.data?.definition}</span>}>
        <LineChart yLabel="Total OI PCR" refLine={1} series={[{ name: "Total OI PCR", color: "#79c0ff", points: (pcr.data?.series ?? []).map((p) => ({ x: p.ts, y: p.pcr_total_oi })) }]} />
        <LineChart yLabel="Change-in-OI PCR" refLine={1} series={[{ name: "Change-in-OI PCR", color: "#d2a8ff", points: (pcr.data?.series ?? []).map((p) => ({ x: p.ts, y: p.pcr_change_oi })) }]} />
      </Card>
    </>
  );
}

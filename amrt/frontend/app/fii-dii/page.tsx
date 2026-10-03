"use client";
import { useState } from "react";
import { useShell } from "@/components/Shell";
import { Card, DataLabelBadge, Empty, LineChart } from "@/components/ui";
import { fmt } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

type Series = { date: string; net: Record<string, number | null>; change_vs_previous: Record<string, number | null> | null }[];
type Agg = { period: string; net_value: number | null; days: number; expected_days: number; completeness: number; status: string }[];
type F = { methodology: Record<string, unknown>; participant: Record<string, Series>; cash: Record<string, Agg>; label: string };

export default function FiiDii() {
  const { act } = useShell();
  const [period, setPeriod] = useState("D");
  const q = usePoll<F>(`/api/market/fii-dii?period=${period}`, 30000);
  const [kind, setKind] = useState("participant_oi");
  const [content, setContent] = useState("");
  const [source, setSource] = useState("https://archives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv");
  const fii = q.data?.participant?.FII ?? [];
  return (
    <>
      <div className="note">Official NSE files only (participant-wise OI, FII/DII cash). No participant P&L is computed or estimated. <DataLabelBadge label={q.data?.label} /></div>
      <Card title="FII index positioning (contracts, net long − short)">
        {!fii.length ? <Empty>No participant files imported.</Empty> : (
          <>
            <LineChart yLabel="FII net index futures" series={[{ name: "Index futures net", color: "#79c0ff", points: fii.map((r, i) => ({ x: i, y: r.net.index_futures_net })) }]} />
            <LineChart yLabel="FII net index options" series={[{ name: "Calls net", color: "#f0883e", points: fii.map((r, i) => ({ x: i, y: r.net.index_call_net })) },
              { name: "Puts net", color: "#3fb950", points: fii.map((r, i) => ({ x: i, y: r.net.index_put_net })) }]} />
            <table><thead><tr><th className="l">Date</th><th>Idx fut net</th><th>Δ</th><th>Calls net</th><th>Puts net</th><th>Long %</th></tr></thead>
              <tbody>{fii.slice(-15).reverse().map((r) => <tr key={r.date}><td className="l">{r.date}</td><td>{fmt(r.net.index_futures_net, 0)}</td><td>{fmt(r.change_vs_previous?.index_futures_net, 0)}</td>
                <td>{fmt(r.net.index_call_net, 0)}</td><td>{fmt(r.net.index_put_net, 0)}</td><td>{fmt(r.net.index_futures_long_pct, 1)}</td></tr>)}</tbody></table>
          </>)}
      </Card>
      <Card title="FII / DII cash market (₹ crore)" right={<div className="tabs">{["D", "W", "M", "Y", "5Y"].map((p) => <button key={p} className={p === period ? "on" : ""} onClick={() => setPeriod(p)}>{p}</button>)}</div>}>
        {!Object.keys(q.data?.cash ?? {}).length ? <Empty>No cash files imported.</Empty> : (
          <table><thead><tr><th className="l">Period</th><th>FII net</th><th>DII net</th><th className="l">Completeness</th></tr></thead>
            <tbody>{(q.data?.cash.FII ?? []).map((r) => <tr key={r.period}><td className="l">{r.period}</td><td>{fmt(r.net_value)}</td><td>{fmt(q.data?.cash.DII?.find((d) => d.period === r.period)?.net_value)}</td>
              <td className="l">{r.status} ({r.days}/{r.expected_days})</td></tr>)}</tbody></table>)}
      </Card>
      <Card title="Import an official file (owner / operator)">
        <div className="row"><select value={kind} onChange={(e) => setKind(e.target.value)}><option value="participant_oi">Participant-wise OI CSV</option><option value="fii_dii_cash">FII/DII cash (CSV or JSON)</option></select>
          <input style={{ width: 480 }} value={source} onChange={(e) => setSource(e.target.value)} placeholder="source URL / file name" /></div>
        <textarea value={content} onChange={(e) => setContent(e.target.value)} placeholder="paste the file contents" />
        <button className="primary" onClick={() => act("/api/market/fii-dii/import", { kind, content, source }).then(() => q.reload())}>Validate & import</button>
      </Card>
    </>
  );
}

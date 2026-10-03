"use client";
import { useState } from "react";
import { Badge, Card, Json } from "@/components/ui";
import { api, ts } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

type Ev = { seq: number; ts: number; type: string; actor_id: string; actor_kind: string; correlation_id: string | null; payload: Record<string, unknown>; hash: string };

export default function Audit() {
  const [prefix, setPrefix] = useState("");
  const q = usePoll<Ev[]>(`/api/audit?limit=300${prefix ? `&type_prefix=${encodeURIComponent(prefix)}` : ""}`, 5000);
  const [ver, setVer] = useState<Record<string, unknown> | null>(null);
  const [cid, setCid] = useState("");
  const [rep, setRep] = useState<unknown>(null);
  return (
    <>
      <Card title="Append-only, hash-chained event log" right={<button onClick={async () => setVer(await api.get("/api/audit/verify"))}>Verify chain</button>}>
        {ver && <div><Badge v={ver.ok ? "OK" : "FAILED"} /> checked {String(ver.checked)} events · head {String(ver.head ?? "").slice(0, 16)}…</div>}
        <div className="row"><input placeholder="type prefix e.g. ORDER_ or RISK_DECISION" value={prefix} onChange={(e) => setPrefix(e.target.value)} /></div>
        <div className="scroll"><table><thead><tr><th>Seq</th><th className="l">Time</th><th className="l">Type</th><th className="l">Actor</th><th className="l">Correlation</th><th className="l">Payload</th></tr></thead>
          <tbody>{(q.data ?? []).map((e) => <tr key={e.seq}><td>{e.seq}</td><td className="l">{ts(e.ts)}</td><td className="l">{e.type}</td><td className="l">{e.actor_kind}:{e.actor_id}</td>
            <td className="l">{e.correlation_id ? <a onClick={() => setCid(e.correlation_id as string)} style={{ cursor: "pointer" }}>{e.correlation_id}</a> : "—"}</td>
            <td className="l" style={{ whiteSpace: "normal", maxWidth: 520, fontSize: 11 }}>{JSON.stringify(e.payload).slice(0, 300)}</td></tr>)}</tbody></table></div>
      </Card>
      <Card title="Replay a decision / order from the log">
        <div className="row"><input style={{ width: 360 }} placeholder="correlation id (decision id DP-…)" value={cid} onChange={(e) => setCid(e.target.value)} />
          <button onClick={async () => setRep(await api.get(`/api/audit/replay/${encodeURIComponent(cid)}`))}>Replay</button></div>
        {rep !== null && <Json v={rep} />}
      </Card>
    </>
  );
}

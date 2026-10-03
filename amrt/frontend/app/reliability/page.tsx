"use client";
import { useShell } from "@/components/Shell";
import { Badge, Card, Json } from "@/components/ui";
import { ts } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

type H = { name: string; kind: string; critical: boolean; state: string; liveness: boolean; readiness: boolean; safety_readiness: boolean; reason: string; last_error: string; quarantined: boolean; failures: number; last_beat_age_s: number | null };
type Inc = { incident_id: string; opened_at: number; severity: string; component: string; title: string; status: string; body: { escalation: string; timeline: { ts: number; event: string; by: string }[] } };
type R = { health: H[]; supervisor: { allowlist: Record<string, string[]>; forbidden: string[]; active: Record<string, unknown>; recent: { ts: number; action: string; component: string; record: Record<string, unknown> }[] };
  incidents: Inc[]; backups: { last: Record<string, unknown>; files: { file: string; bytes: number }[] } };
type A = { alerts: { alert_id: string; ts: number; severity: string; category: string; title: string; body: string; acked_by: string | null; delivery: Record<string, string> }[]; delivery: Record<string, unknown> };

export default function Reliability() {
  const { act } = useShell();
  const q = usePoll<R>("/api/reliability", 3000);
  const al = usePoll<A>("/api/alerts", 5000);
  return (
    <>
      <Card title="Components (liveness ≠ readiness ≠ safety readiness)">
        <table><thead><tr><th className="l">Component</th><th className="l">Kind</th><th className="l">State</th><th className="l">Live</th><th className="l">Ready</th><th className="l">Safety-ready</th><th>Beat age</th><th>Failures</th><th className="l">Reason / error</th><th></th></tr></thead>
          <tbody>{(q.data?.health ?? []).map((h) => (
            <tr key={h.name}><td className="l">{h.name} {h.critical && <span className="badge b-red">critical</span>}</td><td className="l">{h.kind}</td><td className="l"><Badge v={h.state} /></td>
              <td className="l">{h.liveness ? "yes" : "no"}</td><td className="l">{h.readiness ? "yes" : <Badge v="NOT READY" />}</td><td className="l">{h.safety_readiness ? "yes" : "no"}</td>
              <td>{h.last_beat_age_s ?? "—"}</td><td>{h.failures}</td><td className="l" style={{ whiteSpace: "normal", maxWidth: 360 }}>{h.last_error || h.reason}</td>
              <td>{h.quarantined && <button onClick={() => act(`/api/components/${encodeURIComponent(h.name)}/unquarantine`, {}, { confirm: `Unquarantine ${h.name}?` })}>Unquarantine</button>}</td></tr>))}
          </tbody></table>
      </Card>
      <div className="grid">
        <Card title="Incidents (never deleted)">
          {(q.data?.incidents ?? []).slice(0, 30).map((i) => (
            <div key={i.incident_id} style={{ borderBottom: "1px solid var(--line)", padding: "6px 0" }}>
              <Badge v={i.severity} /> <Badge v={i.status} /> <strong>{i.title}</strong> <span className="muted">{i.component} · {ts(i.opened_at)} · escalation {i.body.escalation}</span>
              <div className="row">
                <button onClick={() => act(`/api/incidents/${i.incident_id}/ack`)}>Acknowledge</button>
                {i.status !== "RESOLVED" && <button onClick={() => { const r = window.prompt("Resolution note (SEV1/SEV2 need a passing recovery verification)"); if (r) act(`/api/incidents/${i.incident_id}/resolve`, { resolution: r }); }}>Resolve</button>}
              </div>
            </div>))}
        </Card>
        <Card title="Self-healing supervisor (allowlist only)">
          <h3>Recent recovery actions</h3>
          {(q.data?.supervisor.recent ?? []).slice(-15).reverse().map((r, i) => <div key={i}><span className="muted">{ts(r.ts)}</span> {r.action} → {r.component} <span className="muted">{JSON.stringify(r.record)}</span></div>)}
          <h3>Never automated</h3><div className="muted">{(q.data?.supervisor.forbidden ?? []).join(", ")}</div>
          <h3>Allowlist</h3><Json v={q.data?.supervisor.allowlist ?? {}} />
        </Card>
      </div>
      <div className="grid">
        <Card title="Alerts" right={<span className="muted">delivery: {JSON.stringify(al.data?.delivery ?? {}).slice(0, 120)}</span>}>
          {(al.data?.alerts ?? []).slice(0, 40).map((a) => <div key={a.alert_id}><span className="muted">{ts(a.ts)}</span> <Badge v={a.severity} /> {a.title} <span className="muted">{a.body}</span> {a.acked_by ? <span className="muted">(acked by {a.acked_by})</span> : <button onClick={() => act(`/api/alerts/${a.alert_id}/ack`).then(() => al.reload())}>ack</button>}</div>)}
        </Card>
        <Card title="Backups" right={<button onClick={() => act("/api/backups").then(() => q.reload())}>Back up now</button>}>
          <Json v={q.data?.backups.last ?? {}} />
          {(q.data?.backups.files ?? []).map((f) => <div key={f.file}>{f.file} <span className="muted">{(f.bytes / 1024).toFixed(1)} KiB</span></div>)}
        </Card>
      </div>
    </>
  );
}

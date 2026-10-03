"use client";
import { useShell } from "@/components/Shell";
import { Badge, Card, Json, KV } from "@/components/ui";
import { usePoll } from "@/lib/hooks";

type C = { settings: Record<string, unknown>; integrity: { ok: boolean | null; hash: string; known_good: string | null; differences?: string[]; note?: string }; hard_limits: Record<string, unknown> };

export default function Settings() {
  const { act } = useShell();
  const q = usePoll<C>("/api/config", 15000);
  const i = q.data?.integrity;
  return (
    <>
      <Card title="Configuration integrity" right={<button onClick={() => act("/api/config/known-good", {}, { confirm: "Mark the current configuration as known-good?" }).then(() => q.reload())}>Mark current as known-good</button>}>
        <KV rows={[["Status", <Badge key="s" v={i?.ok === true ? "OK" : i?.ok === false ? "FAILED" : "UNKNOWN"} />], ["Current hash", i?.hash], ["Known-good hash", i?.known_good ?? i?.note],
          ["Differences", (i?.differences ?? []).join(", ") || "none"]]} />
      </Card>
      <Card title="Effective configuration (secrets redacted)"><Json v={q.data?.settings ?? {}} /></Card>
      <Card title="Deployment hard limits"><Json v={q.data?.hard_limits ?? {}} /></Card>
    </>
  );
}

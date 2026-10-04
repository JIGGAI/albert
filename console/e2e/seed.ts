import type { APIRequestContext } from "@playwright/test";

export const api = process.env.ALBERT_API_URL ?? "http://127.0.0.1:18080";
export const headers = { Authorization: `Bearer ${process.env.ALBERT_E2E_KEY}` };

export const VOICE =
  "Brand voice posts sound sharp confident masculine friendly persuasive energetic bold";
export const PAYOUT =
  "Daily payout export reconciles stylist commission deposits against bank transfers";

interface MapBody {
  nodes: { id: string; title: string; cluster: number | null }[];
  links: { kind: string }[];
  clusters: { id: number; label: string; size: number }[];
}

let seeded: Promise<void> | null = null;

/** Two topical groups of memories, stored once per run, indexed before returning. */
export function seedTopics(request: APIRequestContext): Promise<void> {
  seeded ??= (async () => {
    for (const [subject, base] of [
      ["Brand voice", VOICE],
      ["Payout export", PAYOUT],
    ]) {
      for (let index = 0; index < 4; index += 1) {
        const response = await request.post(`${api}/v1/memories`, {
          headers,
          data: { subject: `# ${subject} ${index}`, content: `${base} variant${index}` },
        });
        if (!response.ok()) throw new Error(`seed failed: ${response.status()}`);
      }
    }
    const organizations = await (await request.get("/api/albert/v1/console/organizations")).json();
    const organization = organizations.items.find((o: { name: string }) => o.name === "E2E");
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const map = (await (
        await request.get(`/api/albert/v1/console/map?organization_id=${organization.id}`)
      ).json()) as MapBody;
      const labels = map.clusters.map((cluster) => cluster.label);
      if (labels.includes("Brand Voice") && labels.includes("Payout Export")) return;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw new Error("seeded memories were not indexed into clusters in time");
  })();
  return seeded;
}

import { redirect } from "next/navigation";
import { VIEWS } from "@/lib/ops-views";

// Operations used to be one page with tabs. Its views are top-level pages now;
// old links and bookmarks land on the right one with their other filters kept.
export default async function OpsRedirect({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const requested = typeof params.view === "string" ? params.view : "";
  const view = VIEWS.find((v) => v.id === requested)?.id ?? "agents";
  const rest = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    if (name === "view") continue;
    if (typeof value === "string") rest.set(name, value);
  }
  const query = rest.toString();
  redirect(query ? `/${view}?${query}` : `/${view}`);
}

// The five operations views. Each is its own top-level page (/agents, /memory,
// ...) that the side menu switches between; `sub` heads the page. Kept out of
// the client components so server pages can read the table too.
export const VIEWS = [
  { id: "agents", label: "Agents", sub: "Who is using memory, and how much." },
  { id: "memory", label: "Memory", sub: "What the store holds, by type, workspace and recall." },
  { id: "retrieval", label: "Retrieval", sub: "How well recall is working, and the path a search takes." },
  { id: "graph", label: "Graph", sub: "Where the knowledge graph is written and how it is queried." },
  { id: "service", label: "Service", sub: "Request health, latency and the providers in use." },
] as const;
export type ViewId = (typeof VIEWS)[number]["id"];

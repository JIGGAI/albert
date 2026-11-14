# `@jiggai/albert`

Official typed client for Albert REST v1 and the equivalent declared MCP operations.

```ts
import { ServerAlbertClient } from "@jiggai/albert/server";

const albert = new ServerAlbertClient({
  baseUrl: "https://brain.example.com",
  apiKey: process.env.ALBERT_API_KEY!,
});
await albert.negotiate();
const memory = await albert.memories.create({ content: "PostgreSQL is canonical." });
const context = await albert.context.assemble({ query: "Where is canonical data?" });
```

Browser applications must import `@jiggai/albert/browser` and provide a short-lived access-token callback. The browser entry point does not accept an Albert server API key. Keep long-lived Brain credentials in server-only code.

Writes are retried only when the Albert contract makes them idempotent (capture idempotency keys and episode/source deduplication). Reads use bounded exponential retry. Every client owns immutable headers and credentials; create one client per Brain/tenant boundary.

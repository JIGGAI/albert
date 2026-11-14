# TypeScript SDK

The official SDK lives in `sdk/typescript` and publishes as `@jiggai/albert`.
It supports Node 18 and newer and builds ESM, CommonJS, and TypeScript declarations.

## Server applications

```ts
import { ServerAlbertClient } from "@jiggai/albert/server";

const albert = new ServerAlbertClient({
  baseUrl: "https://brain.example.com",
  apiKey: process.env.ALBERT_API_KEY!,
});

await albert.negotiate();
await albert.sources.ingest({
  content: "The deploy completed at revision abc123.",
  source_type: "deployment",
  source_uri: "deploy://abc123",
});
```

Use a separate client for each tenant or Brain. Headers, credential callbacks,
compatibility state, retries, and telemetry are instance-local.

## Browser applications

```ts
import { BrowserAlbertClient } from "@jiggai/albert/browser";

const albert = new BrowserAlbertClient({
  baseUrl: "https://brain.example.com",
  getAccessToken: () => session.getShortLivedAlbertToken(),
});
```

The browser entry point does not accept `apiKey`. Never place a long-lived Brain
key in browser source, storage, bundles, or environment variables exposed by a
frontend framework. The application backend must issue a short-lived, scoped
token through its authenticated session.

## Reliability and compatibility

`negotiate()` reads `/openapi.json`, validates the required REST v1 paths, and
returns a compatibility object. `requireFeature(path)` raises a typed
`AlbertCompatibilityError` before a gated call when the server lacks that path.

Reads retry transient network and 408/425/429/5xx responses with bounded
exponential delay. Writes retry only where Albert's contract is idempotent:
capture operations carry idempotency keys, and episode/source ingestion uses
Albert's documented deduplication identity. Explicit memory writes and deletes
are not retried automatically.

Errors are `AlbertError` subclasses with stable codes. HTTP errors also include
status, request ID when supplied, and parsed response detail. Telemetry receives
only operation paths, methods, status, attempts, duration, and error codes; it
never receives credentials, request bodies, query text, or sensitive URLs.

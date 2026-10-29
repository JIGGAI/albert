import { NextRequest } from "next/server";

// The operator key lives only here, on the server. The browser talks to this
// route; this route talks to Albert, and only to the console API (reads, plus the
// one POST that is itself a read: the operator search) and the Settings page's
// credential endpoints, each listed exactly.
const API = (process.env.ALBERT_API_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
const KEY = process.env.ALBERT_CONSOLE_API_KEY ?? "";
const ALLOWED_PREFIX = ["v1", "console"];
const FORWARDED_REQUEST_HEADERS = ["accept", "last-event-id"];
const FORWARDED_RESPONSE_HEADERS = ["content-type", "cache-control", "x-accel-buffering"];

const POST_PATHS = new Set([
  "v1/console/search",
  "v1/settings/api-keys",
  "v1/settings/connection-links",
]);
const SETTINGS_READ = "v1/settings/credentials";
const DELETE_PATH = /^v1\/settings\/(api-keys|connection-links)\/[0-9a-f-]{36}$/;
const POST_BODY_LIMIT = 4096;

// The console has no login, so the network it is published on is its access
// control. A page on another site must not borrow that: a DNS-rebinding page
// arrives with its own name in Host, and a cross-site form arrives with its
// own Origin. Hosts are loopback unless ALBERT_CONSOLE_ALLOWED_HOSTS names
// more (comma-separated; "host" allows any port, "host:port" only that one).
const ALLOWED_HOSTS = (process.env.ALBERT_CONSOLE_ALLOWED_HOSTS ?? "localhost,127.0.0.1,[::1]")
  .split(",")
  .map((entry) => entry.trim())
  .filter(Boolean)
  .map(parseHost)
  .filter((entry): entry is { hostname: string; port: string } => entry !== null);

function parseHost(value: string): { hostname: string; port: string } | null {
  if (/[/?#@\\s]/.test(value)) return null;
  try {
    const url = new URL(`http://${value}`);
    return { hostname: url.hostname, port: url.port };
  } catch {
    return null;
  }
}

function hostAllowed(host: string | null): boolean {
  const parsed = host ? parseHost(host) : null;
  if (!parsed) return false;
  return ALLOWED_HOSTS.some(
    (entry) => entry.hostname === parsed.hostname && (!entry.port || entry.port === parsed.port),
  );
}

// Writes must come from a console page: the browser's Origin names this same host.
function sameOrigin(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  if (!origin || !host) return false;
  try {
    return new URL(origin).host === new URL(`http://${host}`).host;
  } catch {
    return false;
  }
}

function refused(request: NextRequest, write: boolean): Response | null {
  if (!hostAllowed(request.headers.get("host"))) {
    return Response.json({ detail: "Host not allowed" }, { status: 421 });
  }
  if (write && !sameOrigin(request)) {
    return Response.json({ detail: "Cross-origin request refused" }, { status: 403 });
  }
  return null;
}

function isAllowed(path: string[]): boolean {
  if (path.join("/") === SETTINGS_READ) return true;
  return (
    path.length > ALLOWED_PREFIX.length &&
    ALLOWED_PREFIX.every((segment, index) => path[index] === segment) &&
    path.every((segment) => segment !== ".." && segment !== "." && segment !== "")
  );
}

function relay(upstream: Response): Response {
  const out = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await params;
  const target = path.join("/");
  if (!POST_PATHS.has(target)) {
    return Response.json({ detail: "Method not allowed" }, { status: 405 });
  }
  const refusal = refused(request, true);
  if (refusal) return refusal;
  const body = await request.text();
  if (body.length > POST_BODY_LIMIT) {
    return Response.json({ detail: "Request too large" }, { status: 413 });
  }
  const upstream = await fetch(`${API}/${target}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${KEY}`, "content-type": "application/json" },
    body,
    cache: "no-store",
    signal: request.signal,
  });
  return relay(upstream);
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await params;
  if (!isAllowed(path)) {
    return Response.json({ detail: "Not found" }, { status: 404 });
  }
  const refusal = refused(request, false);
  if (refusal) return refusal;
  const url = `${API}/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  const headers = new Headers({ Authorization: `Bearer ${KEY}` });
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const upstream = await fetch(url, {
    method: "GET",
    headers,
    cache: "no-store",
    signal: request.signal,
  });
  return relay(upstream);
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await params;
  const target = path.join("/");
  if (!DELETE_PATH.test(target)) {
    return Response.json({ detail: "Method not allowed" }, { status: 405 });
  }
  const refusal = refused(request, true);
  if (refusal) return refusal;
  const upstream = await fetch(`${API}/${target}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${KEY}` },
    cache: "no-store",
    signal: request.signal,
  });
  return relay(upstream);
}

export const dynamic = "force-dynamic";

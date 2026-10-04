import { NextRequest } from "next/server";

// The operator key lives only here, on the server. The browser talks to this
// route; this route talks to Albert, and only to the console API: reads, plus the
// one POST that is itself a read (the operator search).
const API = (process.env.ALBERT_API_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
const KEY = process.env.ALBERT_CONSOLE_API_KEY ?? "";
const ALLOWED_PREFIX = ["v1", "console"];
const FORWARDED_REQUEST_HEADERS = ["accept", "last-event-id"];
const FORWARDED_RESPONSE_HEADERS = ["content-type", "cache-control", "x-accel-buffering"];

const POST_PATH = "v1/console/search";
const POST_BODY_LIMIT = 4096;

function isAllowed(path: string[]): boolean {
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
  if (path.join("/") !== POST_PATH) {
    return Response.json({ detail: "Method not allowed" }, { status: 405 });
  }
  const body = await request.text();
  if (body.length > POST_BODY_LIMIT) {
    return Response.json({ detail: "Request too large" }, { status: 413 });
  }
  const upstream = await fetch(`${API}/${POST_PATH}`, {
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

export const dynamic = "force-dynamic";

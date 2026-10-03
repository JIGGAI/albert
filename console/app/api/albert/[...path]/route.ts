import { NextRequest } from "next/server";

// The operator key lives only here, on the server. The browser talks to this
// route; this route talks to Albert, and only to the read-only console API.
const API = (process.env.ALBERT_API_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
const KEY = process.env.ALBERT_CONSOLE_API_KEY ?? "";
const ALLOWED_PREFIX = ["v1", "console"];
const FORWARDED_REQUEST_HEADERS = ["accept", "last-event-id"];
const FORWARDED_RESPONSE_HEADERS = ["content-type", "cache-control", "x-accel-buffering"];

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await params;
  const allowed =
    path.length > ALLOWED_PREFIX.length &&
    ALLOWED_PREFIX.every((segment, index) => path[index] === segment) &&
    path.every((segment) => segment !== ".." && segment !== "." && segment !== "");
  if (!allowed) {
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
  const out = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export const dynamic = "force-dynamic";

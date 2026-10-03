import { NextRequest } from "next/server";

// The operator key lives only here, on the server. The browser talks to this
// route; this route talks to Albert.
const API = (process.env.ALBERT_API_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
const KEY = process.env.ALBERT_CONSOLE_API_KEY ?? "";
const FORWARDED_REQUEST_HEADERS = ["accept", "content-type", "last-event-id"];
const FORWARDED_RESPONSE_HEADERS = ["content-type", "cache-control", "x-accel-buffering"];

async function proxy(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await params;
  const url = `${API}/${path.join("/")}${request.nextUrl.search}`;
  const headers = new Headers({ Authorization: `Bearer ${KEY}` });
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const upstream = await fetch(url, {
    method: request.method,
    headers,
    body: hasBody ? await request.arrayBuffer() : undefined,
    cache: "no-store",
  });
  const out = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as DELETE };
export const dynamic = "force-dynamic";

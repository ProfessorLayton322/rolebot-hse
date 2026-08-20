export interface Env {
  TG_BOT_TOKEN: string;
  YANDEX_TO_CF_EGRESS_HMAC_SECRET: string;
  MAX_BODY_BYTES?: string;
  TIMESTAMP_WINDOW_SECONDS?: string;
}

interface SendRequest {
  request_id: string;
  method: "sendMessage";
  payload: Record<string, unknown>;
}

const ALLOWED_METHODS = new Set(["sendMessage"]);
const encoder = new TextEncoder();

function constantTimeEqual(left: string, right: string): boolean {
  const a = encoder.encode(left);
  const b = encoder.encode(right);
  const length = Math.max(a.length, b.length);
  let difference = a.length ^ b.length;
  for (let index = 0; index < length; index += 1) {
    difference |= (a[index % Math.max(a.length, 1)] ?? 0) ^ (b[index % Math.max(b.length, 1)] ?? 0);
  }
  return difference === 0;
}

async function digest(body: Uint8Array): Promise<string> {
  const result = await crypto.subtle.digest("SHA-256", body as BufferSource);
  return [...new Uint8Array(result)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function sign(secret: string, canonical: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const result = await crypto.subtle.sign("HMAC", key, encoder.encode(canonical));
  return [...new Uint8Array(result)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function validRequestShape(value: unknown): value is SendRequest {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const request = value as Record<string, unknown>;
  return (
    typeof request.request_id === "string" &&
    typeof request.method === "string" &&
    ALLOWED_METHODS.has(request.method) &&
    typeof request.payload === "object" &&
    request.payload !== null &&
    !Array.isArray(request.payload)
  );
}

async function handle(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (request.method !== "POST" || url.pathname !== "/telegram/send") {
    return new Response("Not Found", { status: 404 });
  }
  const maxBody = Number(env.MAX_BODY_BYTES ?? "32768");
  const declaredLength = Number(request.headers.get("Content-Length") ?? "0");
  if (declaredLength > maxBody) return new Response("Payload Too Large", { status: 413 });
  const body = new Uint8Array(await request.arrayBuffer());
  if (body.byteLength > maxBody) return new Response("Payload Too Large", { status: 413 });

  const timestamp = request.headers.get("X-Timestamp") ?? "";
  const requestId = request.headers.get("X-Request-Id") ?? "";
  const supplied = request.headers.get("X-Signature") ?? "";
  const numericTimestamp = Number(timestamp);
  const windowSeconds = Number(env.TIMESTAMP_WINDOW_SECONDS ?? "60");
  if (!Number.isInteger(numericTimestamp) || Math.abs(Math.floor(Date.now() / 1000) - numericTimestamp) > windowSeconds) {
    return new Response("Forbidden", { status: 403 });
  }
  const bodyHash = await digest(body);
  const canonical = [timestamp, requestId, "POST", "/telegram/send", bodyHash].join("\n");
  const expected = await sign(env.YANDEX_TO_CF_EGRESS_HMAC_SECRET, canonical);
  if (!requestId || !constantTimeEqual(supplied, expected)) {
    return new Response("Forbidden", { status: 403 });
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder().decode(body));
  } catch {
    return new Response("Bad Request", { status: 400 });
  }
  if (!validRequestShape(parsed) || parsed.request_id !== requestId) {
    return new Response("Bad Request", { status: 400 });
  }
  const telegramUrl = `https://api.telegram.org/bot${env.TG_BOT_TOKEN}/${parsed.method}`;
  const response = await fetch(telegramUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(parsed.payload),
  });
  const responseBody = await response.text();
  return new Response(responseBody, {
    status: response.status,
    headers: { "Content-Type": response.headers.get("Content-Type") ?? "application/json" },
  });
}

export default { fetch: handle };
export { constantTimeEqual, handle, sign };

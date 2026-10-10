export interface Env {
  ACTIVE_BACKEND: string;
  TG_WEBHOOK_SECRET: string;
  VK_CALLBACK_SECRET: string;
  VK_CONFIRMATION_STRING: string;
  VK_GROUP_ID: string;
  CF_TO_YANDEX_HMAC_SECRET: string;
  CF_TO_AWS_HMAC_SECRET: string;
  YANDEX_BACKEND_URL: string;
  AWS_BACKEND_URL: string;
  BACKEND_INLINE_CUTOFF_MS?: string;
  INGRESS_HARD_DEADLINE_MS?: string;
  MAX_BODY_BYTES?: string;
  TG_USER_RATE_LIMITER?: RateLimit;
}

type ActiveBackend = "yandex" | "aws" | "maintenance";
type Transport = "telegram" | "vk";
type TelegramContract =
  | { delivery: "inline"; telegram: Record<string, unknown> }
  | { delivery: "deferred"; request_id: string };

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

async function hexDigest(algorithm: AlgorithmIdentifier, data: BufferSource): Promise<string> {
  const result = await crypto.subtle.digest(algorithm, data);
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
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(canonical));
  return [...new Uint8Array(signature)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function emptyOk(): Response {
  return new Response(null, { status: 200 });
}

function backend(env: Env): ActiveBackend | undefined {
  if (env.ACTIVE_BACKEND === "yandex" || env.ACTIVE_BACKEND === "aws" || env.ACTIVE_BACKEND === "maintenance") {
    return env.ACTIVE_BACKEND;
  }
  return undefined;
}

function destinationFor(env: Env, selected: Exclude<ActiveBackend, "maintenance">, transport: Transport): URL {
  const destination = new URL(selected === "yandex" ? env.YANDEX_BACKEND_URL : env.AWS_BACKEND_URL);
  if (destination.protocol !== "https:" || destination.username || destination.password) {
    throw new Error("invalid backend URL");
  }
  destination.pathname = transport === "telegram" ? "/webhooks/telegram" : "/webhooks/vk-cloudflare";
  destination.search = "";
  destination.hash = "";
  return destination;
}

function transportFor(url: URL): Transport | undefined {
  if (url.pathname === "/" || url.pathname === "/telegram") return "telegram";
  if (url.pathname === "/vk") return "vk";
  return undefined;
}

// Every update type this bot subscribes to (message, callback_query) carries its
// sender in the `from` field of its single top-level payload object.
function senderId(update: Record<string, unknown>): number | undefined {
  for (const value of Object.values(update)) {
    if (typeof value !== "object" || value === null) continue;
    const from = (value as { from?: unknown }).from;
    if (typeof from !== "object" || from === null) continue;
    const id = (from as { id?: unknown }).id;
    if (typeof id === "number" && Number.isSafeInteger(id)) return id;
  }
  return undefined;
}

async function senderIsRateLimited(update: Record<string, unknown>, env: Env): Promise<boolean> {
  const sender = senderId(update);
  if (!env.TG_USER_RATE_LIMITER || sender === undefined) return false;
  try {
    const { success } = await env.TG_USER_RATE_LIMITER.limit({ key: `telegram:${sender}` });
    return !success;
  } catch {
    // A limiter outage must never drop real users' updates.
    return false;
  }
}

function validVkCallback(event: Record<string, unknown>, env: Env): boolean {
  const groupId = event.group_id;
  const suppliedSecret = event.secret;
  return (
    typeof groupId === "number" &&
    Number.isSafeInteger(groupId) &&
    String(groupId) === env.VK_GROUP_ID &&
    typeof suppliedSecret === "string" &&
    constantTimeEqual(suppliedSecret, env.VK_CALLBACK_SECRET)
  );
}

async function forward(
  body: Uint8Array,
  env: Env,
  selected: Exclude<ActiveBackend, "maintenance">,
  transport: Transport,
  receivedAt: number,
): Promise<Response> {
  const destination = destinationFor(env, selected, transport);
  const requestId = crypto.randomUUID();
  const timestamp = String(Math.floor(receivedAt / 1000));
  const bodyHash = await hexDigest("SHA-256", body as BufferSource);
  const canonical = [timestamp, requestId, "POST", destination.pathname, bodyHash].join("\n");
  const secret = selected === "yandex" ? env.CF_TO_YANDEX_HMAC_SECRET : env.CF_TO_AWS_HMAC_SECRET;
  const signature = await sign(secret, canonical);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Gateway-Request-Id": requestId,
    "X-Gateway-Timestamp": timestamp,
    "X-Gateway-Signature": signature,
  };
  if (transport === "telegram") {
    const inlineCutoff = Number(env.BACKEND_INLINE_CUTOFF_MS ?? "1500");
    headers["X-Telegram-Inline-Deadline-Ms"] = String(receivedAt + inlineCutoff);
  }
  return fetch(destination, { method: "POST", body: body as BodyInit, headers });
}

async function handle(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const transport = transportFor(new URL(request.url));
  if (!transport) return new Response("Not Found", { status: 404 });
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405, headers: { Allow: "POST" } });
  }
  if (transport === "telegram") {
    const webhookSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "";
    if (!constantTimeEqual(webhookSecret, env.TG_WEBHOOK_SECRET)) {
      return new Response("Forbidden", { status: 403 });
    }
  }

  const maxBody = Number(env.MAX_BODY_BYTES ?? "262144");
  const declaredLength = Number(request.headers.get("Content-Length") ?? "0");
  if (declaredLength > maxBody) return new Response("Payload Too Large", { status: 413 });
  const body = new Uint8Array(await request.arrayBuffer());
  if (body.byteLength > maxBody) return new Response("Payload Too Large", { status: 413 });

  let payload: Record<string, unknown>;
  try {
    const decoded = JSON.parse(new TextDecoder().decode(body)) as unknown;
    if (typeof decoded !== "object" || decoded === null || Array.isArray(decoded)) throw new Error("not an object");
    payload = decoded as Record<string, unknown>;
  } catch {
    return new Response("Bad Request", { status: 400 });
  }

  if (transport === "telegram") {
    if (await senderIsRateLimited(payload, env)) {
      // Telegram redelivers any non-2xx response, so an over-limit update is
      // acknowledged and dropped before it reaches either paid backend.
      console.warn("telegram_update_rate_limited");
      return emptyOk();
    }
  } else {
    if (!validVkCallback(payload, env)) return new Response("Forbidden", { status: 403 });
    // Keep Callback API confirmation independent of backend health and routing
    // mode, including during a production maintenance window.
    if (payload.type === "confirmation") {
      return new Response(env.VK_CONFIRMATION_STRING, {
        status: 200,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }
  }

  const selected = backend(env);
  if (!selected) return new Response("Routing configuration unavailable", { status: 503 });
  if (selected === "maintenance") return transport === "telegram" ? emptyOk() : new Response("ok");

  const receivedAt = Date.now();
  const hardDeadline = Number(env.INGRESS_HARD_DEADLINE_MS ?? "2500");
  let upstream: Promise<Response>;
  try {
    // Validate synchronously so a bad operator binding is distinguished from
    // a reachable backend that failed the request.
    destinationFor(env, selected, transport);
    upstream = forward(body, env, selected, transport, receivedAt);
  } catch {
    return new Response("Routing configuration unavailable", { status: 503 });
  }
  const timeout = new Promise<"timeout">((resolve) => {
    setTimeout(() => resolve("timeout"), hardDeadline);
  });

  let raced: Response | "timeout";
  try {
    raced = await Promise.race([upstream, timeout]);
  } catch {
    return new Response("Bad Gateway", { status: 502 });
  }
  if (raced === "timeout") {
    // The request has reached the selected backend. Keep observing it after the
    // platform callback is acknowledged so its durable operation can finish.
    ctx.waitUntil(upstream.then(() => undefined, () => undefined));
    return transport === "telegram" ? emptyOk() : new Response("ok");
  }
  if (!raced.ok) return new Response("Bad Gateway", { status: 502 });
  if (transport === "vk") {
    return new Response(await raced.text(), {
      status: raced.status,
      headers: { "Content-Type": raced.headers.get("Content-Type") ?? "text/plain; charset=utf-8" },
    });
  }

  let contract: TelegramContract;
  try {
    contract = (await raced.json()) as TelegramContract;
  } catch {
    return new Response("Bad Gateway", { status: 502 });
  }
  if (contract.delivery === "deferred") return emptyOk();
  if (contract.delivery === "inline" && contract.telegram && Date.now() < receivedAt + hardDeadline) {
    return Response.json(contract.telegram, { status: 200 });
  }
  return emptyOk();
}

export default { fetch: handle };
export { constantTimeEqual, destinationFor, handle, sign };

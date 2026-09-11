export interface Env {
  TG_WEBHOOK_SECRET: string;
  CF_TO_YANDEX_HMAC_SECRET: string;
  YANDEX_GATEWAY_URL: string;
  BACKEND_INLINE_CUTOFF_MS?: string;
  INGRESS_HARD_DEADLINE_MS?: string;
  MAX_BODY_BYTES?: string;
  TG_USER_RATE_LIMITER?: RateLimit;
}

type YandexContract =
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

async function handle(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405, headers: { Allow: "POST" } });
  }
  const webhookSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token") ?? "";
  if (!constantTimeEqual(webhookSecret, env.TG_WEBHOOK_SECRET)) {
    return new Response("Forbidden", { status: 403 });
  }
  const maxBody = Number(env.MAX_BODY_BYTES ?? "262144");
  const declaredLength = Number(request.headers.get("Content-Length") ?? "0");
  if (declaredLength > maxBody) {
    return new Response("Payload Too Large", { status: 413 });
  }
  const body = new Uint8Array(await request.arrayBuffer());
  if (body.byteLength > maxBody) {
    return new Response("Payload Too Large", { status: 413 });
  }
  let update: Record<string, unknown>;
  try {
    const decoded = JSON.parse(new TextDecoder().decode(body)) as unknown;
    if (typeof decoded !== "object" || decoded === null || Array.isArray(decoded)) {
      throw new Error("not an object");
    }
    update = decoded as Record<string, unknown>;
  } catch {
    return new Response("Bad Request", { status: 400 });
  }
  if (await senderIsRateLimited(update, env)) {
    // Telegram redelivers any non-2xx response, so an over-limit update is acknowledged
    // and dropped here before it costs anything in Yandex. No reply is sent: replies
    // would spend the bot's shared Bot API send budget on the spammer.
    console.warn("telegram_update_rate_limited");
    return emptyOk();
  }

  const receivedAt = Date.now();
  const inlineCutoff = Number(env.BACKEND_INLINE_CUTOFF_MS ?? "1500");
  const hardDeadline = Number(env.INGRESS_HARD_DEADLINE_MS ?? "2500");
  const requestId = crypto.randomUUID();
  const timestamp = String(Math.floor(receivedAt / 1000));
  const destination = new URL(env.YANDEX_GATEWAY_URL);
  const bodyHash = await hexDigest("SHA-256", body);
  const canonical = [timestamp, requestId, "POST", destination.pathname, bodyHash].join("\n");
  const signature = await sign(env.CF_TO_YANDEX_HMAC_SECRET, canonical);

  const upstream = fetch(destination, {
    method: "POST",
    body,
    headers: {
      "Content-Type": "application/json",
      "X-Gateway-Request-Id": requestId,
      "X-Gateway-Timestamp": timestamp,
      "X-Gateway-Signature": signature,
      "X-Telegram-Inline-Deadline-Ms": String(receivedAt + inlineCutoff),
    },
  });
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
    // The fetch has already reached Yandex. waitUntil only observes its completion; the
    // durable command is created in Yandex and does not live in this Worker lifecycle.
    ctx.waitUntil(upstream.then(() => undefined, () => undefined));
    return emptyOk();
  }
  if (!raced.ok) {
    return new Response("Bad Gateway", { status: 502 });
  }
  let contract: YandexContract;
  try {
    contract = (await raced.json()) as YandexContract;
  } catch {
    return new Response("Bad Gateway", { status: 502 });
  }
  if (contract.delivery === "deferred") {
    return emptyOk();
  }
  if (contract.delivery === "inline" && contract.telegram && Date.now() < receivedAt + hardDeadline) {
    return Response.json(contract.telegram, { status: 200 });
  }
  return emptyOk();
}

export default { fetch: handle };
export { constantTimeEqual, handle, sign };

export interface Env {
  TG_WEBHOOK_SECRET: string;
  CF_TO_YANDEX_HMAC_SECRET: string;
  YANDEX_GATEWAY_URL: string;
  BACKEND_INLINE_CUTOFF_MS?: string;
  INGRESS_HARD_DEADLINE_MS?: string;
  MAX_BODY_BYTES?: string;
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
  try {
    const decoded = JSON.parse(new TextDecoder().decode(body)) as unknown;
    if (typeof decoded !== "object" || decoded === null || Array.isArray(decoded)) {
      throw new Error("not an object");
    }
  } catch {
    return new Response("Bad Request", { status: 400 });
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

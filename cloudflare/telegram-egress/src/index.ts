export interface Env {
  TG_BOT_TOKEN: string;
  YANDEX_TO_CF_EGRESS_HMAC_SECRET: string;
  YANDEX_DISK_TOKEN: string;
  VK_ACCESS_TOKEN: string;
  VK_CALLBACK_SECRET: string;
  VK_CONFIRMATION_STRING: string;
  VK_GROUP_ID: string;
  TG_ADMIN_IDS: string;
  VK_ADMIN_IDS: string;
  PARTICIPANT_KEY_HMAC_SECRET: string;
  CF_TO_YANDEX_HMAC_SECRET: string;
  YMQ_ACCESS_KEY_ID: string;
  YMQ_SECRET_ACCESS_KEY: string;
  YANDEX_OIDC_AUDIENCE: string;
  YANDEX_SERVICE_ACCOUNT_IDS: string;
  MAX_BODY_BYTES?: string;
  TIMESTAMP_WINDOW_SECONDS?: string;
}

interface SendRequest {
  request_id: string;
  method: "sendMessage";
  payload: Record<string, unknown>;
}

const ALLOWED_METHODS = new Set(["sendMessage"]);
const OIDC_ISSUER = "https://auth.yandex.cloud";
const JWKS_URL = "https://auth.yandex.cloud/oauth/jwks/keys";
const RUNTIME_CONFIG_PATH = "/runtime/config";
const RUNTIME_CONFIG_KEYS = [
  "YANDEX_DISK_TOKEN",
  "VK_ACCESS_TOKEN",
  "VK_CALLBACK_SECRET",
  "VK_CONFIRMATION_STRING",
  "VK_GROUP_ID",
  "TG_ADMIN_IDS",
  "VK_ADMIN_IDS",
  "PARTICIPANT_KEY_HMAC_SECRET",
  "CF_TO_YANDEX_HMAC_SECRET",
  "YANDEX_TO_CF_EGRESS_HMAC_SECRET",
  "YMQ_ACCESS_KEY_ID",
  "YMQ_SECRET_ACCESS_KEY",
] as const;
const encoder = new TextEncoder();

interface IdentityHeader {
  alg?: unknown;
  kid?: unknown;
  typ?: unknown;
}

interface IdentityClaims {
  iss?: unknown;
  sub?: unknown;
  aud?: unknown;
  exp?: unknown;
  iat?: unknown;
}

interface JwksResponse {
  keys?: SigningJwk[];
}

type SigningJwk = JsonWebKey & { kid?: string };

let cachedKeys: { expiresAt: number; keys: SigningJwk[] } | undefined;

function decodeBase64Url(value: string): Uint8Array {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  const decoded = atob(padded);
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

function decodeJwtJson<T>(value: string): T {
  return JSON.parse(new TextDecoder().decode(decodeBase64Url(value))) as T;
}

async function publicKey(kid: string): Promise<CryptoKey | undefined> {
  const now = Date.now();
  if (!cachedKeys || cachedKeys.expiresAt <= now) {
    const response = await fetch(JWKS_URL, { headers: { Accept: "application/json" } });
    if (!response.ok) return undefined;
    const payload = (await response.json()) as JwksResponse;
    if (!Array.isArray(payload.keys)) return undefined;
    cachedKeys = { expiresAt: now + 5 * 60 * 1000, keys: payload.keys };
  }
  const jwk = cachedKeys.keys.find((key) => key.kid === kid && key.kty === "RSA" && key.use === "sig");
  if (!jwk) return undefined;
  return crypto.subtle.importKey(
    "jwk",
    jwk,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["verify"],
  );
}

async function verifyYandexIdentityToken(token: string, env: Env, now = Math.floor(Date.now() / 1000)): Promise<boolean> {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return false;
    const [encodedHeader, encodedClaims, encodedSignature] = parts as [string, string, string];
    const header = decodeJwtJson<IdentityHeader>(encodedHeader);
    const claims = decodeJwtJson<IdentityClaims>(encodedClaims);
    if (header.alg !== "RS256" || typeof header.kid !== "string") return false;
    if (
      claims.iss !== OIDC_ISSUER ||
      typeof claims.sub !== "string" ||
      typeof claims.exp !== "number" ||
      typeof claims.iat !== "number" ||
      claims.exp <= now ||
      claims.iat > now + 60 ||
      claims.iat < now - 3700 ||
      claims.exp - claims.iat > 3700
    ) {
      return false;
    }
    const audiences = typeof claims.aud === "string" ? [claims.aud] : claims.aud;
    if (!Array.isArray(audiences) || !audiences.includes(env.YANDEX_OIDC_AUDIENCE)) return false;
    const allowedSubjects = JSON.parse(env.YANDEX_SERVICE_ACCOUNT_IDS) as unknown;
    if (!Array.isArray(allowedSubjects) || !allowedSubjects.includes(claims.sub)) return false;
    const key = await publicKey(header.kid);
    if (!key) return false;
    return crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      decodeBase64Url(encodedSignature) as BufferSource,
      encoder.encode(`${encodedHeader}.${encodedClaims}`),
    );
  } catch {
    return false;
  }
}

function runtimeValues(env: Env): Record<string, string> | undefined {
  const values: Record<string, string> = {};
  for (const key of RUNTIME_CONFIG_KEYS) {
    const value = env[key];
    if (typeof value !== "string" || value.length === 0) return undefined;
    values[key] = value;
  }
  return values;
}

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
  if (url.pathname === RUNTIME_CONFIG_PATH) {
    if (request.method !== "GET") return new Response("Method Not Allowed", { status: 405, headers: { Allow: "GET" } });
    const authorization = request.headers.get("Authorization") ?? "";
    const token = authorization.startsWith("Bearer ") ? authorization.slice("Bearer ".length) : "";
    if (!token || !(await verifyYandexIdentityToken(token, env))) return new Response("Forbidden", { status: 403 });
    const values = runtimeValues(env);
    if (!values) return new Response("Runtime configuration unavailable", { status: 503 });
    return Response.json(
      { values },
      { status: 200, headers: { "Cache-Control": "no-store", Pragma: "no-cache" } },
    );
  }
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
export { constantTimeEqual, handle, sign, verifyYandexIdentityToken };

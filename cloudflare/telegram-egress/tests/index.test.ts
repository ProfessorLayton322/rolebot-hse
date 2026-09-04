import { afterEach, describe, expect, it, vi } from "vitest";
import { handle, sign, type Env } from "../src/index";

const env: Env = {
  TG_BOT_TOKEN: "123:token",
  YANDEX_TO_CF_EGRESS_HMAC_SECRET: "egress-secret",
  YANDEX_DISK_TOKEN: "disk-token",
  VK_ACCESS_TOKEN: "vk-token",
  VK_CALLBACK_SECRET: "vk-secret",
  VK_CONFIRMATION_STRING: "confirmation",
  VK_GROUP_ID: "42",
  TG_ADMIN_IDS: "[1]",
  VK_ADMIN_IDS: "[2]",
  TG_GAMEMASTER_IDS: "[3]",
  VK_GAMEMASTER_IDS: "[4]",
  PARTICIPANT_KEY_HMAC_SECRET: "participant-secret",
  CF_TO_YANDEX_HMAC_SECRET: "ingress-secret",
  YMQ_ACCESS_KEY_ID: "ymq-key-id",
  YMQ_SECRET_ACCESS_KEY: "ymq-secret",
  YANDEX_OIDC_AUDIENCE: "https://egress.example/runtime/config",
  YANDEX_SERVICE_ACCOUNT_IDS: '["gateway-service-account","worker-service-account"]',
};

function base64Url(value: Uint8Array): string {
  const binary = String.fromCharCode(...value);
  return btoa(binary).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}

async function identityRequest(subject = "gateway-service-account"): Promise<Request> {
  const pair = (await crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["sign", "verify"],
  )) as CryptoKeyPair;
  const kid = crypto.randomUUID();
  const publicJwk = (await crypto.subtle.exportKey("jwk", pair.publicKey)) as JsonWebKey & { kid?: string };
  publicJwk.kid = kid;
  publicJwk.use = "sig";
  const now = Math.floor(Date.now() / 1000);
  const header = base64Url(new TextEncoder().encode(JSON.stringify({ alg: "RS256", kid, typ: "JWT" })));
  const claims = base64Url(
    new TextEncoder().encode(
      JSON.stringify({
        iss: "https://auth.yandex.cloud",
        sub: subject,
        aud: env.YANDEX_OIDC_AUDIENCE,
        iat: now,
        exp: now + 3600,
      }),
    ),
  );
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    pair.privateKey,
    new TextEncoder().encode(`${header}.${claims}`),
  );
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ keys: [publicJwk] })));
  return new Request("https://egress.example/runtime/config", {
    headers: { Authorization: `Bearer ${header}.${claims}.${base64Url(new Uint8Array(signature))}` },
  });
}

async function signedRequest(method = "sendMessage", offsetSeconds = 0): Promise<Request> {
  const requestId = "operation-id";
  const body = JSON.stringify({ request_id: requestId, method, payload: { chat_id: 1, text: "hello" } });
  const bytes = new TextEncoder().encode(body);
  const hashBuffer = await crypto.subtle.digest("SHA-256", bytes);
  const hash = [...new Uint8Array(hashBuffer)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  const timestamp = String(Math.floor(Date.now() / 1000) + offsetSeconds);
  const signature = await sign(env.YANDEX_TO_CF_EGRESS_HMAC_SECRET, [timestamp, requestId, "POST", "/telegram/send", hash].join("\n"));
  return new Request("https://egress.example/telegram/send", {
    method: "POST",
    body,
    headers: { "X-Timestamp": timestamp, "X-Request-Id": requestId, "X-Signature": signature },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("telegram egress", () => {
  it("forwards an allowlisted method to Telegram", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ok: true })));
    const response = await handle(await signedRequest(), env);
    expect(response.status).toBe(200);
    expect(String(vi.mocked(fetch).mock.calls[0]?.[0])).toContain("api.telegram.org/bot123:token/sendMessage");
  });

  it("rejects unsupported methods", async () => {
    vi.stubGlobal("fetch", vi.fn());
    expect((await handle(await signedRequest("getUpdates"), env)).status).toBe(400);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("rejects stale signatures", async () => {
    vi.stubGlobal("fetch", vi.fn());
    expect((await handle(await signedRequest("sendMessage", -120), env)).status).toBe(403);
  });

  it("rejects a bad signature", async () => {
    const request = await signedRequest();
    const headers = new Headers(request.headers);
    headers.set("X-Signature", "bad");
    expect((await handle(new Request(request, { headers }), env)).status).toBe(403);
  });

  it("returns runtime values to an allowed Yandex service account", async () => {
    const response = await handle(await identityRequest(), env);
    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    const payload = (await response.json()) as { values: Record<string, string> };
    expect(payload.values.YANDEX_DISK_TOKEN).toBe("disk-token");
    expect(payload.values.YMQ_SECRET_ACCESS_KEY).toBe("ymq-secret");
    expect(payload.values.TG_GAMEMASTER_IDS).toBe("[3]");
    expect(payload.values.VK_GAMEMASTER_IDS).toBe("[4]");
    expect(payload.values.TG_BOT_TOKEN).toBeUndefined();
  });

  it("rejects a valid identity for an unapproved service account", async () => {
    expect((await handle(await identityRequest("other-service-account"), env)).status).toBe(403);
  });

  it("does not disclose runtime values without an identity token", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const response = await handle(new Request("https://egress.example/runtime/config"), env);
    expect(response.status).toBe(403);
    expect(fetch).not.toHaveBeenCalled();
  });
});

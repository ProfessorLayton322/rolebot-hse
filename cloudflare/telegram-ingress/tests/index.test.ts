import { afterEach, describe, expect, it, vi } from "vitest";
import { handle, sign, type Env } from "../src/index";

// The production defaults. Only the hard-timeout test shortens the deadline.
const env: Env = {
  ACTIVE_BACKEND: "yandex",
  TG_WEBHOOK_SECRET: "webhook-secret",
  VK_CALLBACK_SECRET: "vk-secret",
  VK_CONFIRMATION_STRING: "confirmation-value",
  VK_GROUP_ID: "42",
  CF_TO_YANDEX_HMAC_SECRET: "yandex-transport-secret",
  CF_TO_AWS_HMAC_SECRET: "aws-transport-secret",
  YANDEX_BACKEND_URL: "https://yandex.example/ignored/legacy/path",
  AWS_BACKEND_URL: "https://aws.example/",
  BACKEND_INLINE_CUTOFF_MS: "1500",
  INGRESS_HARD_DEADLINE_MS: "2500",
};

function telegramRequest(
  secret = "webhook-secret",
  update: Record<string, unknown> = { update_id: 1, message: { text: "/start" } },
): Request {
  return new Request("https://ingress.example/", {
    method: "POST",
    body: JSON.stringify(update),
    headers: { "X-Telegram-Bot-Api-Secret-Token": secret },
  });
}

function vkRequest(event: Record<string, unknown>): Request {
  return new Request("https://ingress.example/vk", {
    method: "POST",
    body: JSON.stringify(event),
    headers: { "Content-Type": "application/json" },
  });
}

function context(): ExecutionContext {
  return { waitUntil: vi.fn(), passThroughOnException: vi.fn(), props: {} } as unknown as ExecutionContext;
}

function limitedEnv(outcome: (options: RateLimitOptions) => Promise<RateLimitOutcome>) {
  const limit = vi.fn(outcome);
  return { limit, env: { ...env, TG_USER_RATE_LIMITER: { limit } } };
}

async function forwardedSignatureIs(secret: string, body: string, path: string): Promise<boolean> {
  const call = vi.mocked(fetch).mock.calls[0];
  const destination = new URL(String(call?.[0]));
  const init = call?.[1] as RequestInit;
  const headers = new Headers(init.headers);
  const hashBuffer = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body));
  const hash = [...new Uint8Array(hashBuffer)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  const expected = await sign(
    secret,
    [headers.get("X-Gateway-Timestamp"), headers.get("X-Gateway-Request-Id"), "POST", path, hash].join("\n"),
  );
  return destination.pathname === path && headers.get("X-Gateway-Signature") === expected;
}

const deferred = () => Response.json({ delivery: "deferred", request_id: "r" });
const message = { update_id: 2, message: { from: { id: 42 }, chat: { id: 42 }, text: "spam" } };
const vkMessage = {
  type: "message_new",
  event_id: "vk-event",
  group_id: 42,
  secret: "vk-secret",
  object: { message: { from_id: 7, peer_id: 7, text: "/start" } },
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("reversible backend routing", () => {
  it("routes Telegram to Yandex with the Yandex ingress secret", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(deferred()));
    const request = telegramRequest();
    const body = await request.clone().text();
    expect((await handle(request, env, context())).status).toBe(200);
    expect(String(vi.mocked(fetch).mock.calls[0]?.[0])).toBe("https://yandex.example/webhooks/telegram");
    expect(await forwardedSignatureIs(env.CF_TO_YANDEX_HMAC_SECRET, body, "/webhooks/telegram")).toBe(true);
  });

  it("routes Telegram to AWS with the distinct AWS ingress secret", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(deferred()));
    const request = telegramRequest();
    const body = await request.clone().text();
    expect((await handle(request, { ...env, ACTIVE_BACKEND: "aws" }, context())).status).toBe(200);
    expect(String(vi.mocked(fetch).mock.calls[0]?.[0])).toBe("https://aws.example/webhooks/telegram");
    expect(await forwardedSignatureIs(env.CF_TO_AWS_HMAC_SECRET, body, "/webhooks/telegram")).toBe(true);
    expect(await forwardedSignatureIs(env.CF_TO_YANDEX_HMAC_SECRET, body, "/webhooks/telegram")).toBe(false);
  });

  it("acknowledges authenticated traffic without a backend in maintenance", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const maintenance = { ...env, ACTIVE_BACKEND: "maintenance" };
    const telegram = await handle(telegramRequest(), maintenance, context());
    const vk = await handle(vkRequest(vkMessage), maintenance, context());
    expect(telegram.status).toBe(200);
    expect(await telegram.text()).toBe("");
    expect(vk.status).toBe(200);
    expect(await vk.text()).toBe("ok");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("fails closed for an invalid selector or backend URL", async () => {
    vi.stubGlobal("fetch", vi.fn());
    expect((await handle(telegramRequest(), { ...env, ACTIVE_BACKEND: "invalid" }, context())).status).toBe(503);
    expect(
      (
        await handle(
          telegramRequest(),
          { ...env, ACTIVE_BACKEND: "aws", AWS_BACKEND_URL: "http://insecure.example" },
          context(),
        )
      ).status,
    ).toBe(503);
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("Telegram ingress", () => {
  it("returns one inline Bot API response", async () => {
    const upstream = new Response(JSON.stringify({ delivery: "inline", telegram: { method: "sendMessage", text: "ok" } }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(upstream));
    const response = await handle(telegramRequest(), env, context());
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ method: "sendMessage", text: "ok" });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("rejects a bad Telegram webhook secret before routing", async () => {
    vi.stubGlobal("fetch", vi.fn());
    expect((await handle(telegramRequest("bad"), env, context())).status).toBe(403);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("returns an empty 200 at the hard timeout", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    const response = await handle(telegramRequest(), { ...env, INGRESS_HARD_DEADLINE_MS: "25" }, context());
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("");
  });
});

describe("VK stable ingress", () => {
  it("answers an authenticated confirmation locally in every routing mode", async () => {
    vi.stubGlobal("fetch", vi.fn());
    for (const active of ["yandex", "aws", "maintenance"]) {
      const response = await handle(
        vkRequest({ type: "confirmation", group_id: 42, secret: "vk-secret" }),
        { ...env, ACTIVE_BACKEND: active },
        context(),
      );
      expect(response.status).toBe(200);
      expect(await response.text()).toBe("confirmation-value");
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it("rejects a forged callback before routing", async () => {
    vi.stubGlobal("fetch", vi.fn());
    expect((await handle(vkRequest({ ...vkMessage, secret: "wrong" }), env, context())).status).toBe(403);
    expect((await handle(vkRequest({ ...vkMessage, group_id: 43 }), env, context())).status).toBe(403);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("routes a callback through the signed Cloudflare-only backend path", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("ok")));
    const request = vkRequest(vkMessage);
    const body = await request.clone().text();
    const response = await handle(request, { ...env, ACTIVE_BACKEND: "aws" }, context());
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("ok");
    expect(String(vi.mocked(fetch).mock.calls[0]?.[0])).toBe("https://aws.example/webhooks/vk-cloudflare");
    expect(await forwardedSignatureIs(env.CF_TO_AWS_HMAC_SECRET, body, "/webhooks/vk-cloudflare")).toBe(true);
  });
});

describe("Telegram per-user rate limit", () => {
  it("acknowledges and drops an over-limit sender without reaching a backend", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { limit, env: limited } = limitedEnv(async () => ({ success: false }));
    const response = await handle(telegramRequest("webhook-secret", message), limited, context());
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("");
    expect(limit).toHaveBeenCalledWith({ key: "telegram:42" });
    expect(fetch).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledWith("telegram_update_rate_limited");
  });

  it("forwards updates when the limiter is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(deferred()));
    const { env: limited } = limitedEnv(async () => {
      throw new Error("limiter unavailable");
    });
    expect((await handle(telegramRequest("webhook-secret", message), limited, context())).status).toBe(200);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("does not count unauthenticated requests, senderless updates, or VK callbacks", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(deferred()).mockResolvedValueOnce(new Response("ok")));
    const { limit, env: limited } = limitedEnv(async () => ({ success: false }));
    expect((await handle(telegramRequest("bad", message), limited, context())).status).toBe(403);
    expect((await handle(telegramRequest(), limited, context())).status).toBe(200);
    expect((await handle(vkRequest(vkMessage), limited, context())).status).toBe(200);
    expect(limit).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});

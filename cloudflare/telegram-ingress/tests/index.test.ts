import { afterEach, describe, expect, it, vi } from "vitest";
import { handle, type Env } from "../src/index";

const env: Env = {
  TG_WEBHOOK_SECRET: "webhook-secret",
  CF_TO_YANDEX_HMAC_SECRET: "transport-secret",
  YANDEX_GATEWAY_URL: "https://gateway.example/webhooks/telegram",
  BACKEND_INLINE_CUTOFF_MS: "1500",
  INGRESS_HARD_DEADLINE_MS: "25",
};

function request(
  secret = "webhook-secret",
  update: Record<string, unknown> = { update_id: 1, message: { text: "/start" } },
): Request {
  return new Request("https://ingress.example/", {
    method: "POST",
    body: JSON.stringify(update),
    headers: { "X-Telegram-Bot-Api-Secret-Token": secret },
  });
}

function context(): ExecutionContext {
  return { waitUntil: vi.fn(), passThroughOnException: vi.fn(), props: {} } as unknown as ExecutionContext;
}

function limitedEnv(outcome: (options: RateLimitOptions) => Promise<RateLimitOutcome>) {
  const limit = vi.fn(outcome);
  return { limit, env: { ...env, TG_USER_RATE_LIMITER: { limit } } };
}

const deferred = () => Response.json({ delivery: "deferred", request_id: "r" });
const message = { update_id: 2, message: { from: { id: 42 }, chat: { id: 42 }, text: "spam" } };

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("telegram ingress", () => {
  it("returns one inline Bot API response", async () => {
    const upstream = new Response(JSON.stringify({ delivery: "inline", telegram: { method: "sendMessage", text: "ok" } }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(upstream));
    const response = await handle(request(), env, context());
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ method: "sendMessage", text: "ok" });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("acknowledges explicit deferred delivery with an empty body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ delivery: "deferred", request_id: "r" })));
    const response = await handle(request(), env, context());
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("");
  });

  it("rejects a bad Telegram webhook secret", async () => {
    vi.stubGlobal("fetch", vi.fn());
    expect((await handle(request("bad"), env, context())).status).toBe(403);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("returns an empty 200 at the hard timeout", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    const response = await handle(request(), env, context());
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("");
  });
});

describe("telegram per-user rate limit", () => {
  it("acknowledges and drops an over-limit sender without reaching Yandex", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { limit, env: limited } = limitedEnv(async () => ({ success: false }));
    const response = await handle(request("webhook-secret", message), limited, context());
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("");
    expect(limit).toHaveBeenCalledWith({ key: "telegram:42" });
    expect(fetch).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledWith("telegram_update_rate_limited");
  });

  it("forwards a sender within the limit", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(deferred()));
    const { env: limited } = limitedEnv(async () => ({ success: true }));
    expect((await handle(request("webhook-secret", message), limited, context())).status).toBe(200);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("keys button presses by the user who pressed them", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(deferred()));
    const { limit, env: limited } = limitedEnv(async () => ({ success: true }));
    const press = { update_id: 3, callback_query: { id: "q", from: { id: 7 }, data: "menu" } };
    await handle(request("webhook-secret", press), limited, context());
    expect(limit).toHaveBeenCalledWith({ key: "telegram:7" });
  });

  it("forwards updates when the limiter is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(deferred()));
    const { env: limited } = limitedEnv(async () => {
      throw new Error("limiter unavailable");
    });
    expect((await handle(request("webhook-secret", message), limited, context())).status).toBe(200);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("does not count unauthenticated requests or updates without a sender", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(deferred()));
    const { limit, env: limited } = limitedEnv(async () => ({ success: false }));
    expect((await handle(request("bad", message), limited, context())).status).toBe(403);
    expect((await handle(request(), limited, context())).status).toBe(200);
    expect(limit).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

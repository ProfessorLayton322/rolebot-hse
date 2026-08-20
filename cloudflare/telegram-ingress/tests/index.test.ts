import { afterEach, describe, expect, it, vi } from "vitest";
import { handle, type Env } from "../src/index";

const env: Env = {
  TG_WEBHOOK_SECRET: "webhook-secret",
  CF_TO_YANDEX_HMAC_SECRET: "transport-secret",
  YANDEX_GATEWAY_URL: "https://gateway.example/webhooks/telegram",
  BACKEND_INLINE_CUTOFF_MS: "1500",
  INGRESS_HARD_DEADLINE_MS: "25",
};

function request(secret = "webhook-secret"): Request {
  return new Request("https://ingress.example/", {
    method: "POST",
    body: JSON.stringify({ update_id: 1, message: { text: "/start" } }),
    headers: { "X-Telegram-Bot-Api-Secret-Token": secret },
  });
}

function context(): ExecutionContext {
  return { waitUntil: vi.fn(), passThroughOnException: vi.fn(), props: {} } as unknown as ExecutionContext;
}

afterEach(() => vi.unstubAllGlobals());

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

import { afterEach, describe, expect, it, vi } from "vitest";
import { handle, sign, type Env } from "../src/index";

const env: Env = {
  TG_BOT_TOKEN: "123:token",
  YANDEX_TO_CF_EGRESS_HMAC_SECRET: "egress-secret",
};

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
});

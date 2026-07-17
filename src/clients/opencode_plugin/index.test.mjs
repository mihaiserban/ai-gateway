import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { AgentAiGatewayPlugin } from "./index.js";

async function withIsolatedPluginState(run) {
  const originalFetch = globalThis.fetch;
  const originalDataHome = process.env.XDG_DATA_HOME;
  const originalVirtualKey = process.env.VIRTUAL_KEY;
  const dataHome = fs.mkdtempSync(path.join(os.tmpdir(), "agent-ai-gateway-plugin-"));
  process.env.XDG_DATA_HOME = dataHome;
  delete process.env.VIRTUAL_KEY;
  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
    if (originalDataHome === undefined) delete process.env.XDG_DATA_HOME;
    else process.env.XDG_DATA_HOME = originalDataHome;
    if (originalVirtualKey === undefined) delete process.env.VIRTUAL_KEY;
    else process.env.VIRTUAL_KEY = originalVirtualKey;
    fs.rmSync(dataHome, { recursive: true, force: true });
  }
}

function catalogResponse(entries) {
  return new Response(JSON.stringify({ data: entries }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

test("rejects malformed options before fetching or writing state", async () => {
  await assert.rejects(
    AgentAiGatewayPlugin({}, { baseURL: "javascript:alert(1)" }),
    /baseURL must be a URL/
  );
  await assert.rejects(
    AgentAiGatewayPlugin({}, { providerId: "../../../outside" }),
    /providerId must be a lowercase slug/
  );
});

test("coalesces concurrent catalog fetches and reuses the TTL cache", async () => {
  await withIsolatedPluginState(async () => {
    let calls = 0;
    let releaseFetch;
    const blocked = new Promise((resolve) => {
      releaseFetch = resolve;
    });
    globalThis.fetch = async () => {
      calls += 1;
      await blocked;
      return catalogResponse([
        {
          id: "model-a",
          gateway: { kind: "registry-model", context_length: 32000, capabilities: ["chat"] },
        },
      ]);
    };
    const hooks = await AgentAiGatewayPlugin(
      {},
      { baseURL: "http://gateway:4100/v1", apiKey: "secret", providerId: "cache-test" }
    );

    const first = hooks.provider.models({}, {});
    const second = hooks.provider.models({}, {});
    assert.equal(calls, 1);
    releaseFetch();
    const [firstModels, secondModels] = await Promise.all([first, second]);
    const cachedModels = await hooks.provider.models({}, {});

    assert.deepEqual(firstModels, secondModels);
    assert.deepEqual(cachedModels, firstModels);
    assert.equal(firstModels["model-a"].limit.context, 32000);
    assert.equal(calls, 1);
  });
});

test("normalizes malformed catalog metadata to safe model defaults", async () => {
  await withIsolatedPluginState(async () => {
    globalThis.fetch = async () =>
      catalogResponse([
        {
          id: "member-a",
          gateway: {
            context_length: -1,
            capabilities: "chat",
            pricing: { input_cost_per_token: "free", output_cost_per_token: -1 },
          },
        },
        { id: "member-b", gateway: { context_length: 0, capabilities: {} } },
        { id: "combo", gateway: { kind: "combo", candidates: ["member-a", "member-b"] } },
        null,
        {},
      ]);
    const hooks = await AgentAiGatewayPlugin({}, { providerId: "malformed-test", apiKey: "secret" });

    const models = await hooks.provider.models({}, {});

    assert.deepEqual(Object.keys(models), ["member-a", "member-b", "combo"]);
    assert.equal(models["member-a"].limit.context, 128000);
    assert.equal(models["member-a"].cost.input, 0);
    assert.equal(models["member-a"].cost.output, 0);
    assert.equal(models.combo.limit.context, 128000);
    assert.equal(models.combo.capabilities.toolcall, false);
  });
});

test("recovers the last catalog snapshot after a fetch failure", async () => {
  await withIsolatedPluginState(async () => {
    const options = { providerId: "snapshot-test", apiKey: "secret" };
    globalThis.fetch = async () => catalogResponse([{ id: "snapshot-model", gateway: {} }]);
    const online = await AgentAiGatewayPlugin({}, options);
    await online.provider.models({}, {});

    globalThis.fetch = async () => {
      throw new Error("gateway unavailable");
    };
    const offline = await AgentAiGatewayPlugin({}, options);
    const models = await offline.provider.models({}, {});

    assert.deepEqual(Object.keys(models), ["snapshot-model"]);
  });
});

test("auth loader replaces stale authorization without dropping other headers", async () => {
  await withIsolatedPluginState(async () => {
    let seenHeaders;
    globalThis.fetch = async (_input, init) => {
      seenHeaders = init.headers;
      return new Response("ok");
    };
    const hooks = await AgentAiGatewayPlugin({}, { apiKey: "" });
    const auth = await hooks.auth.loader(async () => ({ type: "api", key: "fresh-key" }), {});

    await auth.fetch("http://gateway:4100/v1/models", {
      headers: { Authorization: "Bearer stale-key", "X-Test": "preserved" },
    });

    assert.equal(seenHeaders.get("Authorization"), "Bearer fresh-key");
    assert.equal(seenHeaders.get("X-Test"), "preserved");
  });
});

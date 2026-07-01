// Agent AI Gateway plugin for OpenCode.
//
// Implements the @opencode-ai/plugin contract using three hooks:
//   - config:  materialises provider.gateway into Config at startup (static fallback)
//   - provider: returns live models fetched from GET /v1/models?view=<catalog>
//   - auth:    injects the virtual key + a fetch interceptor
//
// This lets OpenCode discover gateway models at runtime with no hand-written
// provider.* block in opencode.json — only a plugin tuple is required.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const AgentAiGatewayPlugin = async (_input, rawOptions) => {
  const opts = resolveOptions(rawOptions);
  const providerId = opts.providerId;
  const baseURL = normalizeBaseURL(opts.baseURL);
  const apiKey = resolveApiKey(opts.apiKey);
  const catalog = opts.catalog;
  const cacheTtl = opts.modelCacheTtl;

  // Shared between config and provider hooks so /v1/models is fetched once per TTL.
  const cache = {
    fetchedAt: 0,
    promise: null,
    data: null,
  };

  const fetchModels = async () => {
    const now = Date.now();
    if (cache.data && now - cache.fetchedAt < cacheTtl) {
      return cache.data;
    }
    if (cache.promise) return cache.promise;
    cache.promise = doFetchModels(baseURL, apiKey, catalog)
      .then((entries) => {
        cache.data = entries;
        cache.fetchedAt = Date.now();
        writeDiskSnapshot(providerId, entries);
        return entries;
      })
      .catch((err) => {
        const fallback = readDiskSnapshot(providerId);
        if (fallback && fallback.length) {
          return fallback;
        }
        throw err;
      })
      .finally(() => {
        cache.promise = null;
      });
    return cache.promise;
  };

  return {
    // 1) Static fallback: write provider.gateway into Config at startup.
    //    Older OpenCode versions / serve mode use this block directly.
    //    If the operator already declared provider[providerId] in opencode.json, we respect it.
    config: async (config) => {
      if (config.provider && config.provider[providerId]) return;

      let entries;
      try {
        entries = await fetchModels();
      } catch (err) {
        entries = readDiskSnapshot(providerId) || [];
      }

      config.provider = config.provider || {};
      config.provider[providerId] = {
        npm: "@ai-sdk/openai-compatible",
        name: opts.displayName,
        options: {
          baseURL,
          apiKey,
        },
        models: buildStaticModels(entries),
      };
    },

    // 2) Dynamic path: live model list for OpenCode >= 1.14.49.
    provider: {
      id: providerId,
      async models(provider, _ctx) {
        const entries = await fetchModels();
        const key = provider?.options?.apiKey || apiKey;
        return buildV2Models(entries, providerId, baseURL, key);
      },
    },

    // 3) Auth injection.
    auth: {
      provider: providerId,
      methods: [
        {
          type: "api",
          label: `${opts.displayName} API Key`,
          prompts: [
            {
              type: "text",
              key: "apiKey",
              message: `Enter your ${opts.displayName} virtual key`,
            },
          ],
        },
      ],
      async loader(getAuth, _provider) {
        let key = apiKey;
        if (!key) {
          try {
            const auth = await getAuth();
            if (auth && auth.type === "api" && auth.key) {
              key = auth.key;
            }
          } catch {
            // fall through to env/option key
          }
        }
        if (!key) return {};
        return {
          apiKey: key,
          baseURL,
          fetch: createAuthFetch(key),
        };
      },
    },
  };
};

export default AgentAiGatewayPlugin;

// ---------------------------------------------------------------------------
// Options / env resolution
// ---------------------------------------------------------------------------

function resolveOptions(raw) {
  return {
    providerId: "gateway",
    displayName: "Agent AI Gateway",
    baseURL: "http://localhost:4100/v1",
    apiKey: "{env:VIRTUAL_KEY}",
    catalog: "all",
    modelCacheTtl: 300000,
    ...(raw || {}),
  };
}

function resolveApiKey(spec) {
  if (!spec || typeof spec !== "string") return process.env.VIRTUAL_KEY || "";
  const m = spec.match(/^\{env:(\w+)\}$/);
  if (m) return process.env[m[1]] || "";
  return spec;
}

function normalizeBaseURL(url) {
  if (!url || typeof url !== "string") {
    throw new Error("Agent AI Gateway plugin: baseURL is required");
  }
  let base = url.trim();
  if (!/^https?:\/\//i.test(base)) {
    throw new Error(`Agent AI Gateway plugin: baseURL must be a URL, got ${JSON.stringify(base)}`);
  }
  base = base.replace(/\/+$/, "");
  if (base.endsWith("/v1")) base = base.slice(0, -3);
  return base + "/v1";
}

// ---------------------------------------------------------------------------
// Network + caching
// ---------------------------------------------------------------------------

async function doFetchModels(baseURL, apiKey, catalog) {
  const url = `${baseURL.replace(/\/$/, "")}/models?view=${encodeURIComponent(
    catalog || "all"
  )}`;
  const headers = apiKey ? { Authorization: `Bearer ${apiKey}` } : {};
  const res = await fetch(url, { headers });
  if (!res.ok) {
    throw new Error(`gateway /models returned ${res.status}`);
  }
  const body = await res.json();
  return Array.isArray(body.data) ? body.data : Array.isArray(body) ? body : [];
}

function createAuthFetch(apiKey) {
  return async (input, init) => {
    const next = { ...(init || {}) };
    const headers = new Headers(next.headers || {});
    headers.set("Authorization", `Bearer ${apiKey}`);
    next.headers = headers;
    return fetch(input, next);
  };
}

function pluginDataDir() {
  const home = os.homedir ? os.homedir() : process.env.HOME;
  const base =
    process.env.XDG_DATA_HOME || (home ? path.join(home, ".local/share") : "/tmp");
  return path.join(base, "opencode/plugins");
}

function snapshotPath(providerId) {
  const dir = pluginDataDir();
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch {
    return null;
  }
  return path.join(dir, `agent-ai-gateway-${providerId}.json`);
}

function readDiskSnapshot(providerId) {
  const file = snapshotPath(providerId);
  if (!file) return null;
  try {
    const raw = fs.readFileSync(file, "utf-8");
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function writeDiskSnapshot(providerId, entries) {
  const file = snapshotPath(providerId);
  if (!file || !entries || !entries.length) return;
  try {
    fs.writeFileSync(file, JSON.stringify(entries, null, 2));
  } catch {
    // best-effort
  }
}

// ---------------------------------------------------------------------------
// Model mapping
// ---------------------------------------------------------------------------

function buildStaticModels(entries) {
  const models = {};
  for (const entry of entries) {
    if (!entry || !entry.id) continue;
    models[entry.id] = { name: displayName(entry) };
  }
  return models;
}

function buildV2Models(entries, providerId, baseURL, modelApiKey) {
  // Build a lookup of non-combo models first so combos can aggregate from it.
  const registry = {};
  for (const entry of entries) {
    if (!entry || !entry.id) continue;
    const kind = entry.gateway?.kind;
    if (kind !== "combo") {
      registry[entry.id] = entry;
    }
  }

  const models = {};
  for (const entry of entries) {
    if (!entry || !entry.id) continue;
    const meta = entry.gateway || {};
    const isCombo = meta.kind === "combo";
    const candidates =
      isCombo && Array.isArray(meta.candidates) ? meta.candidates : [];

    // Aggregate limits/caps across combo candidates.
    let context = meta.context_length;
    let capabilities = meta.capabilities || [];
    if (isCombo && candidates.length) {
      const memberCtxs = [];
      const memberCaps = [];
      for (const cid of candidates) {
        const m = registry[cid];
        if (!m) continue;
        if (m.gateway?.context_length) memberCtxs.push(m.gateway.context_length);
        if (m.gateway?.capabilities) memberCaps.push(m.gateway.capabilities);
      }
      if (!context && memberCtxs.length) {
        context = Math.min(...memberCtxs);
      }
      if (!capabilities.length && memberCaps.length) {
        capabilities = memberCaps.reduce((acc, cur) =>
          acc.filter((c) => cur.includes(c))
        );
      }
    }

    const caps = normalizeCapabilities(capabilities);
    const limits = {
      context: context || 128000,
      output: 8192,
    };

    const pricing = meta.pricing || {};
    const cost = {
      input: pricing.input_cost_per_token || 0,
      output: pricing.output_cost_per_token || 0,
      cache: { read: 0, write: 0 },
    };

    models[entry.id] = {
      id: entry.id,
      providerID: providerId,
      name: displayName(entry),
      api: {
        id: "openai-compatible",
        url: baseURL,
        npm: "@ai-sdk/openai-compatible",
      },
      capabilities: {
        temperature: caps.temperature,
        reasoning: caps.reasoning,
        attachment: caps.attachment,
        toolcall: caps.toolcall,
        input: {
          text: true,
          audio: false,
          image: caps.attachment,
          video: false,
          pdf: false,
        },
        output: { text: true, audio: false, image: false, video: false, pdf: false },
        interleaved: false,
      },
      cost,
      limit: limits,
      status: "active",
      release_date: "",
      headers: {},
      options: modelApiKey ? { apiKey: modelApiKey } : {},
    };
  }

  return models;
}

function displayName(entry) {
  if (!entry || !entry.id) return "";
  const meta = entry.gateway || {};
  if (meta.task) {
    return meta.task.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }
  if (entry.name) return entry.name;
  return entry.id;
}

function normalizeCapabilities(capabilities) {
  const list = Array.isArray(capabilities) ? capabilities : [];
  return {
    temperature: true,
    reasoning: list.includes("reasoning"),
    attachment: list.includes("vision") || list.includes("image") || list.includes("multimodal"),
    toolcall: list.includes("coding") || list.includes("chat") || list.includes("tools"),
  };
}

// Agent AI Gateway plugin for OpenCode.
//
// Fetches the live model catalog from the gateway at startup and exposes it as
// an OpenAI-compatible provider. The plugin entry in opencode.json is:
//
//   ["./plugins/agent-ai-gateway/index.js", {options}]
//
// Supported options (all optional):
//   providerId    default "gateway"
//   displayName   default "Agent AI Gateway"
//   baseURL       default "http://localhost:4100/v1"
//   apiKey        default "{env:VIRTUAL_KEY}"
//   catalog       default "all"
//   modelCacheTtl default 300000 (ms)
//
// The plugin fetches GET <baseURL>/models?view=<catalog>, maps every returned
// model id into an @ai-sdk/openai-compatible provider model entry, and caches
// the successful fetch in memory for modelCacheTtl. On fetch failure it keeps
// the last successful in-memory catalog for this OpenCode process.

const plugin = {
  id: "agent-ai-gateway",
  name: "Agent AI Gateway",

  async setup(_app, options) {
    const opts = {
      providerId: "gateway",
      displayName: "Agent AI Gateway",
      baseURL: "http://localhost:4100/v1",
      apiKey: "{env:VIRTUAL_KEY}",
      catalog: "all",
      modelCacheTtl: 300000,
      ...options,
    };

    const providerId = opts.providerId;
    const baseURL = opts.baseURL;
    const apiKey = _resolveApiKey(opts.apiKey);
    const cacheTtl = opts.modelCacheTtl;

    let cached = null;
    let cacheAt = 0;

    async function fetchCatalog() {
      const now = Date.now();
      if (cached && now - cacheAt < cacheTtl) {
        return cached;
      }
      const url = `${baseURL.replace(/\/$/, "")}/models?view=${encodeURIComponent(opts.catalog)}`;
      const res = await fetch(url, {
        headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
      });
      if (!res.ok) {
        if (cached) return cached;
        throw new Error(`gateway /models returned ${res.status}`);
      }
      const body = await res.json();
      cached = Array.isArray(body.data) ? body.data : [];
      cacheAt = now;
      return cached;
    }

    const provider = {
      id: providerId,
      name: opts.displayName,
      npm: "@ai-sdk/openai-compatible",
      options: { baseURL, apiKey },
      async models() {
        const entries = await fetchCatalog();
        const models = {};
        for (const entry of entries) {
          // The gateway /v1/models response is expected to carry display
          // metadata (e.g. entry.name or entry.gateway.display_name) in a
          // future revision; fall back to the entry id for now.
          const meta = entry.gateway || {};
          const name = entry.name || meta.display_name || entry.id;
          models[entry.id] = { name };
        }
        return models;
      },
    };

    return { providers: { [providerId]: provider } };
  },
};

function _resolveApiKey(spec) {
  if (!spec || typeof spec !== "string") return process.env.VIRTUAL_KEY || "";
  const m = spec.match(/^\{env:(\w+)\}$/);
  if (m) return process.env[m[1]] || "";
  return spec;
}

module.exports = plugin;

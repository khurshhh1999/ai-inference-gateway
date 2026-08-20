/** Gateway OpenAPI document (kept aligned with packages/shared/openapi.yaml). */
export const openApiDocument = {
  openapi: "3.1.0",
  info: {
    title: "AI Inference Gateway",
    version: "0.14.0",
    description:
      "OpenAI-shaped edge for multi-cloud LLM routing: chat completions (including tool / function calling), model listing, embeddings, semantic cache, streaming, per-tenant budgets, rate limiting, tracing, and indexed vector lookup.",
  },
  servers: [{ url: "http://localhost:18080" }],
  paths: {
    "/health": {
      get: {
        summary: "Gateway health",
        security: [],
        responses: { "200": { description: "OK" } },
      },
    },
    "/metrics": {
      get: {
        summary: "Prometheus metrics",
        security: [],
        responses: {
          "200": {
            description: "Prometheus text exposition format",
            content: { "text/plain": { schema: { type: "string" } } },
          },
        },
      },
    },
    "/openapi.json": {
      get: {
        summary: "OpenAPI document",
        security: [],
        responses: { "200": { description: "OpenAPI 3.1 JSON" } },
      },
    },
    "/docs": {
      get: {
        summary: "Swagger UI",
        security: [],
        responses: { "200": { description: "HTML docs" } },
      },
    },
    "/v1/models": {
      get: {
        summary: "List models",
        security: [{ ApiKeyAuth: [] }, { BearerAuth: [] }],
        parameters: [
          {
            name: "X-Request-Id",
            in: "header",
            required: false,
            schema: { type: "string", maxLength: 128 },
          },
        ],
        responses: {
          "200": { description: "OpenAI-shaped model list" },
          "401": { description: "Missing or invalid API key" },
          "429": { description: "Gateway rate limit exceeded" },
          "502": { description: "Upstream router error" },
        },
      },
    },
    "/v1/models/{model}": {
      get: {
        summary: "Retrieve a model",
        security: [{ ApiKeyAuth: [] }, { BearerAuth: [] }],
        parameters: [
          {
            name: "model",
            in: "path",
            required: true,
            schema: { type: "string" },
          },
          {
            name: "X-Request-Id",
            in: "header",
            required: false,
            schema: { type: "string", maxLength: 128 },
          },
        ],
        responses: {
          "200": { description: "Model object" },
          "401": { description: "Missing or invalid API key" },
          "404": { description: "Unknown model" },
          "429": { description: "Gateway rate limit exceeded" },
        },
      },
    },
    "/v1/embeddings": {
      post: {
        summary: "Create embeddings",
        security: [{ ApiKeyAuth: [] }, { BearerAuth: [] }],
        parameters: [
          {
            name: "X-Request-Id",
            in: "header",
            required: false,
            schema: { type: "string", maxLength: 128 },
          },
        ],
        requestBody: {
          required: true,
          content: {
            "application/json": {
              schema: {
                type: "object",
                required: ["model", "input"],
                properties: {
                  model: { type: "string", minLength: 1 },
                  input: {
                    oneOf: [
                      { type: "string" },
                      { type: "array", items: { type: "string" }, minItems: 1 },
                    ],
                  },
                  encoding_format: { type: "string", enum: ["float"], default: "float" },
                  dimensions: { type: "integer", minimum: 32, maximum: 4096 },
                  user: { type: "string" },
                },
              },
            },
          },
        },
        responses: {
          "200": { description: "OpenAI-shaped embedding list" },
          "400": { description: "Invalid request body or non-embedding model" },
          "401": { description: "Missing or invalid API key" },
          "402": { description: "Tenant hard budget exhausted" },
          "413": { description: "Request body too large" },
          "429": { description: "Gateway rate limit exceeded" },
          "502": { description: "Upstream router error" },
        },
      },
    },
    "/v1/chat/completions": {
      post: {
        summary: "Create a chat completion",
        security: [{ ApiKeyAuth: [] }, { BearerAuth: [] }],
        parameters: [
          {
            name: "X-Request-Id",
            in: "header",
            required: false,
            description:
              "Optional correlation id (echoed on the response and forwarded upstream). Generated when omitted or invalid.",
            schema: { type: "string", maxLength: 128 },
          },
          {
            name: "X-Cache-Bypass",
            in: "header",
            required: false,
            description: 'Set to "1" to skip semantic cache read/write',
            schema: { type: "string", enum: ["1", "true"] },
          },
        ],
        requestBody: {
          required: true,
          content: {
            "application/json": {
              schema: {
                type: "object",
                additionalProperties: false,
                required: ["model", "messages"],
                properties: {
                  model: { type: "string", minLength: 1 },
                  messages: {
                    type: "array",
                    minItems: 1,
                    items: {
                      type: "object",
                      required: ["role"],
                      properties: {
                        role: {
                          type: "string",
                          enum: ["system", "user", "assistant", "tool"],
                        },
                        content: { type: ["string", "null"] },
                        name: { type: "string" },
                        tool_call_id: { type: "string" },
                        tool_calls: { type: "array" },
                      },
                    },
                  },
                  stream: { type: "boolean", default: false },
                  max_tokens: { type: "integer", minimum: 1, maximum: 8192 },
                  temperature: { type: "number", minimum: 0, maximum: 2 },
                  tools: { type: "array" },
                  tool_choice: {},
                },
              },
            },
          },
        },
        responses: {
          "200": {
            description:
              "JSON completion (includes provider, route_reason, cached), or SSE (`text/event-stream`) when stream=true. Tool calls use OpenAI tools/tool_choice. May include X-RateLimit-* headers.",
          },
          "400": { description: "Invalid request body" },
          "401": { description: "Missing or invalid API key" },
          "402": { description: "Tenant hard budget exhausted" },
          "413": { description: "Request body too large" },
          "429": {
            description:
              "Gateway rate limit exceeded (per key/tenant), or budget exhausted when BUDGET_HARD_STATUS=429",
          },
          "502": { description: "Upstream router / provider error" },
        },
      },
    },
  },
  components: {
    securitySchemes: {
      ApiKeyAuth: {
        type: "apiKey",
        in: "header",
        name: "X-API-Key",
        description:
          "X-API-Key, or OpenAI-style Authorization: Bearer <key>. Maps to a tenant via TENANT_API_KEYS (or DEMO_API_KEY → tenant default).",
      },
      BearerAuth: {
        type: "http",
        scheme: "bearer",
        description: "Same secret as X-API-Key (OpenAI client default).",
      },
    },
  },
} as const;

export const swaggerUiHtml = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>AI Inference Gateway — OpenAPI</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui.css" />
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui-bundle.js"></script>
  <script>
    window.ui = SwaggerUIBundle({
      url: "/openapi.json",
      dom_id: "#swagger-ui",
      presets: [SwaggerUIBundle.presets.apis],
    });
  </script>
</body>
</html>`;

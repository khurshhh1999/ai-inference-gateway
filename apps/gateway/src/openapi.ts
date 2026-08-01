/** Gateway OpenAPI document (kept aligned with packages/shared/openapi.yaml). */
export const openApiDocument = {
  openapi: "3.1.0",
  info: {
    title: "AI Inference Gateway",
    version: "0.6.0",
    description:
      "OpenAI-shaped chat completions edge for multi-cloud LLM routing with semantic cache, streaming, and per-tenant budgets.",
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
    "/v1/chat/completions": {
      post: {
        summary: "Create a chat completion",
        security: [{ ApiKeyAuth: [] }],
        parameters: [
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
                      required: ["role", "content"],
                      properties: {
                        role: {
                          type: "string",
                          enum: ["system", "user", "assistant"],
                        },
                        content: { type: "string" },
                      },
                    },
                  },
                  stream: { type: "boolean", default: false },
                  max_tokens: { type: "integer", minimum: 1, maximum: 8192 },
                  temperature: { type: "number", minimum: 0, maximum: 2 },
                },
              },
            },
          },
        },
        responses: {
          "200": {
            description:
              "JSON completion, or SSE (`text/event-stream`) when stream=true",
          },
          "400": { description: "Invalid request body" },
          "401": { description: "Missing or invalid API key" },
          "402": { description: "Tenant hard budget exhausted" },
          "413": { description: "Request body too large" },
          "429": { description: "Budget exhausted when BUDGET_HARD_STATUS=429" },
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
          "Maps to a tenant via TENANT_API_KEYS (or DEMO_API_KEY → tenant default).",
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

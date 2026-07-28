export type GatewayConfig = {
  port: number;
  routerUrl: string;
  demoApiKey: string;
  logLevel: string;
};

export function loadConfig(env: NodeJS.ProcessEnv = process.env): GatewayConfig {
  return {
    port: Number(env.PORT ?? "8080"),
    routerUrl: (env.ROUTER_URL ?? "http://127.0.0.1:8081").replace(/\/$/, ""),
    demoApiKey: env.DEMO_API_KEY ?? "demo-key-change-me",
    logLevel: env.LOG_LEVEL ?? "info",
  };
}

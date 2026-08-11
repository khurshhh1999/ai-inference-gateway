import { buildServer } from "./app.js";
import { loadConfig } from "./config.js";
import { shutdownTracing } from "./tracing.js";

async function main(): Promise<void> {
  const config = loadConfig();
  // Tracing is initialized inside buildServer (safe to call once).
  const app = await buildServer(config);
  await app.listen({ port: config.port, host: "0.0.0.0" });

  const shutdown = async () => {
    await app.close();
    await shutdownTracing();
    process.exit(0);
  };
  process.on("SIGINT", () => void shutdown());
  process.on("SIGTERM", () => void shutdown());
}

main().catch(async (err) => {
  console.error(err);
  await shutdownTracing();
  process.exit(1);
});

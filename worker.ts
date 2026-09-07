import { Container, getContainer } from "@cloudflare/containers";
import { env } from "cloudflare:workers";

export class UrbanGreen extends Container {
  defaultPort = 8000;
  sleepAfter = "15m";
  envVars = {
    PORT: "8000",
    CANOPY_ROOT: "/app",
    ...(typeof env.OPENAI_API_KEY === "string" && env.OPENAI_API_KEY
      ? { OPENAI_API_KEY: env.OPENAI_API_KEY }
      : {}),
  };
}

export default {
  async fetch(request: Request, workerEnv: { URBAN_GREEN: DurableObjectNamespace }): Promise<Response> {
    return getContainer(workerEnv.URBAN_GREEN).fetch(request);
  },
};

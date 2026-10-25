import type { CreateClientConfig } from './generated/client.gen';

// Picked up by the generated client (`runtimeConfigPath` in openapi-ts.config.ts). The SDK's URLs are already
// absolute paths (`/api/v1/...`), so an empty base URL means "same origin": the FastAPI server in production and
// Vite's dev proxy in `npm run dev`. `client.ts` owns everything else (token, errors, revisions, SSE).
export const createClientConfig: CreateClientConfig = (config) => ({
  ...config,
  baseUrl: '',
});

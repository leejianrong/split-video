import { defineConfig } from '@hey-api/openapi-ts';

// Input is the committed, drift-checked schema at the repo root, so no Python is needed to regenerate.
// Output is committed and drift-checked in CI (`npm run api:generate && git status --porcelain`).
export default defineConfig({
  input: '../openapi.json',
  output: {
    path: 'src/lib/api/generated',
    // Generated files are not touched by prettier or eslint (see .prettierignore and eslint.config.js).
    clean: true,
  },
  plugins: [
    '@hey-api/typescript',
    {
      name: '@hey-api/client-fetch',
      runtimeConfigPath: './src/lib/api/client-config',
      throwOnError: true,
    },
    '@hey-api/sdk',
  ],
});

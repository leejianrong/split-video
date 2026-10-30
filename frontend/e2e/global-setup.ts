import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { EXTRA_NAME, GIG_NAME, generateExtra, generateGig } from './media';

export const MEDIA_DIR = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  'e2e-results',
  'media',
);
export const GIG_PATH = path.join(MEDIA_DIR, GIG_NAME);
export const EXTRA_PATH = path.join(MEDIA_DIR, EXTRA_NAME);

/** Generate the fixture recording once per run, into the gitignored `e2e-results/`. */
export default function globalSetup(): void {
  const have = (p: string) => fs.existsSync(p) && fs.statSync(p).size > 0;
  if (!have(GIG_PATH)) generateGig(GIG_PATH);
  if (!have(EXTRA_PATH)) generateExtra(EXTRA_PATH);
}

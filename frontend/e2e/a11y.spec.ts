import AxeBuilder from '@axe-core/playwright';
import type { Page } from '@playwright/test';
import { test, expect, openApp, openEditor } from './fixtures';

// Serious and critical axe violations fail (docs/adr/0004-frontend-stack-and-structure.md#accessibility). Anything
// listed here is a known, explained exception; the list must stay empty or carry an issue link (docs/e2e.md).
const KNOWN: Record<string, string> = {};

// Controls that are disabled are exempt from the contrast rule (WCAG 1.4.3, "inactive user interface component"), but
// axe cannot tell that a Slider's value readout (an <output>, which is not a form control) belongs to a disabled
// slider, and the Slider dims it on purpose. Excluded by selector rather than by rule so everything else is checked.
const INACTIVE = '.disabled output';

async function check(page: Page, where: string, include?: string) {
  let builder = new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .exclude(INACTIVE);
  if (include) builder = builder.include(include);
  const { violations } = await builder.analyze();
  const bad = violations.filter(
    (v) => (v.impact === 'serious' || v.impact === 'critical') && !(v.id in KNOWN),
  );
  expect(
    bad.map((v) => ({
      id: v.id,
      impact: v.impact,
      help: v.help,
      nodes: v.nodes.map((n) => n.target.join(' ')).slice(0, 5),
    })),
    `axe violations on ${where}`,
  ).toEqual([]);
}

test.describe('accessibility (axe)', () => {
  test.describe('first run', () => {
    test.use({ seedMedia: false });
    test('the first-run page has no serious violations', async ({ page, app }) => {
      await openApp(page, app);
      await expect(page.getByTestId('drop-zone')).toBeVisible();
      await check(page, 'first run');
    });
  });

  test('the library has no serious violations', async ({ page, app }) => {
    await openApp(page, app, '#/library');
    await expect(
      page.getByTestId('library-list').or(page.getByTestId('library-empty')),
    ).toBeVisible();
    await check(page, 'library');
  });

  test('the editor shell has no serious violations on any tab', async ({ page, app }) => {
    await openEditor(page, app);
    const tabs = page.locator('[data-region=left] [role=tab]');
    const count = await tabs.count();
    expect(count).toBeGreaterThanOrEqual(7);
    for (let i = 0; i < count; i++) {
      const tab = tabs.nth(i);
      const name = (await tab.innerText()).trim();
      await tab.click();
      await expect(tab).toHaveAttribute('aria-selected', 'true');
      await check(page, `editor, ${name} tab`);
    }
  });

  test('the inspector and the export dialog have no serious violations', async ({ page, app }) => {
    await openEditor(page, app);
    await page.getByTestId('clip').first().click();
    await expect(page.getByTestId('clip-inspector')).toBeVisible();
    await check(page, 'editor with a clip selected');

    await page.getByTestId('export-button').click();
    await expect(page.locator('dialog[open]')).toBeVisible();
    await check(page, 'export dialog');
  });
});

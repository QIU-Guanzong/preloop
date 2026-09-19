import { expect, type Page } from '@playwright/test';

/**
 * After /login lands on /console, a cloud overlay may still be asking the
 * first-login plan question. Click the free-arm escape hatch so specs that
 * are not about that screen can reach the console. No-op on OSS (the
 * element is never mounted) and for accounts that already answered.
 */
export async function dismissPlanChoiceIfShown(page: Page): Promise<void> {
  const screen = page.locator('plan-choice-screen');
  const startFree = screen.locator('sl-button', { hasText: /Start on Free/i });
  const shown = await startFree
    .waitFor({ state: 'visible', timeout: 3_000 })
    .then(() => true)
    .catch(() => false);
  if (!shown) {
    return;
  }
  await startFree.click();
  await expect(screen).toHaveCount(0, { timeout: 30_000 });
}

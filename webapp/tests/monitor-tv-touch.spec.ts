import { expect, test, type Locator, type Page } from "playwright/test";

test.use({
  hasTouch: true,
  userAgent: "Mozilla/5.0 (Web0S; Linux/SmartTV) AppleWebKit/537.36 Chrome/132.0.0.0 Safari/537.36",
});

async function tapCenter(page: Page, locator: Locator) {
  const bounds = await locator.boundingBox();
  expect(bounds).not.toBeNull();
  await page.touchscreen.tap(
    (bounds?.x ?? 0) + (bounds?.width ?? 0) / 2,
    (bounds?.y ?? 0) + (bounds?.height ?? 0) / 2,
  );
}

test("webOS touch opens settings, switches views, and stays scroll-safe", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "The TV touch contract needs one browser viewport.");
  await page.goto("/monitor/index.html?mode=dummy&profile=tv");
  await expect(page.locator(".connection")).toHaveAttribute("data-state", "dummy");

  const settingsTrigger = page.getByRole("button", { name: "교수 프로필에서 연결 설정 열기" }).first();
  await tapCenter(page, settingsTrigger);
  await expect(page.getByRole("dialog", { name: "CONNECTION SETTINGS" })).toBeVisible();
  await tapCenter(page, page.getByRole("button", { name: "설정 닫기" }));
  await expect(page.getByRole("dialog", { name: "CONNECTION SETTINGS" })).toBeHidden();

  const fieldFocus = page.getByRole("tab", { name: "FIELD FOCUS" });
  await tapCenter(page, fieldFocus);
  await expect(fieldFocus).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("main.app-shell")).toHaveClass(/field-focus/);

  await page.setViewportSize({ width: 1080, height: 1920 });
  await page.waitForTimeout(100);
  const geometry = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    clientHeight: document.documentElement.clientHeight,
    scrollWidth: document.documentElement.scrollWidth,
    scrollHeight: document.documentElement.scrollHeight,
  }));
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
  expect(geometry.scrollHeight).toBeLessThanOrEqual(geometry.clientHeight + 1);
});

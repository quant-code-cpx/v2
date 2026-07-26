import { expect, test } from "@playwright/test";

test("opens market overview and instrument analysis", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "市场概览" })).toBeVisible();
  await page.getByRole("link", { name: "贵州茅台" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await expect(page.getByText("K 线与技术指标")).toBeVisible();
  await expect(page.getByText("相对表现")).toBeVisible();
});

import { expect, test, type Page, type WebSocketRoute } from "playwright/test";
import AxeBuilder from "@axe-core/playwright";

type LauncherMode = "live" | "llm-surgeon" | "replay" | "debug";

const dummyRuntimeConfig = `
  window.SURGIMATE_CONFIG = Object.freeze({
    mode: "dummy",
    dummyDataFile: "/monitor/dummy-data.json",
    rosbridge: Object.freeze({
      url: "ws://127.0.0.1:9092",
      gatewayStaleAfterMs: 3000,
      topicSilenceTimeoutMs: 3000,
      connectTimeoutMs: 8000,
      reconnect: Object.freeze({ initialDelayMs: 1000, maxDelayMs: 15000, multiplier: 1.8, jitterRatio: 0.2 }),
      cameraStreams: Object.freeze({ enabled: true, throttleRateMs: 100, fit: "contain", playoutMode: "latest" }),
    }),
  });
`;

const liveRuntimeConfig = dummyRuntimeConfig.replace('mode: "dummy"', 'mode: "ros"');
const monitorTopicAllowlist = [
  "/surgery/gateway_info",
  "/surgery/catalog",
  "/surgery/context",
  "/surgery/instruments",
  "/surgery/robots",
  "/surgery/robot_end_effectors",
  "/surgery/tool_predictions",
  "/surgery/speech",
  "/surgery/health",
  "/surgery/images/flir/compressed",
].sort();

async function installShellStubs(page: Page) {
  const transitions: LauncherMode[] = [];
  const runtime = {
    phase: "idle",
    active_mode: "llm-surgeon",
    requested_mode: "llm-surgeon",
    message: "Selected runtime is ready.",
    retryable: false,
  };

  await page.route("**/api/runtime/status", (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(runtime) }));
  await page.route("**/api/runtime/transition", async (route) => {
    transitions.push((route.request().postDataJSON() as { mode: LauncherMode }).mode);
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify(runtime),
    });
  });
  await page.route("**/monitor/runtime-config.js", (route) =>
    route.fulfill({ contentType: "application/javascript", body: dummyRuntimeConfig }));

  const handleMissionSocket = (socket: WebSocketRoute) => {
    socket.onMessage((raw) => {
      const message = JSON.parse(typeof raw === "string" ? raw : raw.toString()) as {
        op?: string;
        id?: string;
        service?: string;
        topic?: string;
      };
      if (message.op === "subscribe" && message.topic === "/simulation/state") {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            procedure_id: "test-procedure",
            active_bundle: "thyroidectomy_v1",
            filtered_phase: "P03",
            running: false,
            execution_state: "idle",
            instrument_states: [],
          },
        }));
        return;
      }
      if (message.op !== "call_service" || !message.id || !message.service) return;
      socket.send(JSON.stringify({
        op: "service_response",
        id: message.id,
        service: message.service,
        result: true,
        values: { success: true, accepted: true, message: "ok", model_ids: [] },
      }));
    });
  };

  await Promise.all([
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9090\/?$/, handleMissionSocket),
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9091\/?$/, handleMissionSocket),
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9099\/?$/, handleMissionSocket),
  ]);

  return transitions;
}

test("opens the pixel-isolated monitor and preserves browser history without a runtime transition", async ({ page }) => {
  const transitions = await installShellStubs(page);
  await page.goto("/");
  await expect(page.locator('[data-slot="mission-workspace"]')).toBeVisible();

  await page.getByRole("button", { name: "수술 관제" }).click();
  await expect(page).toHaveURL(/\?workspace=monitor$/);
  await expect(page.locator('[data-slot="surgimate-monitor-workspace"]')).toHaveAttribute("data-state", "ready");

  const monitorFrame = page.locator('iframe[title="SurgiMate 수술 관제"]');
  await expect(monitorFrame).toBeVisible();
  await expect(monitorFrame).toHaveAttribute(
    "sandbox",
    "allow-scripts allow-same-origin allow-downloads",
  );
  const frame = page.frameLocator('iframe[title="SurgiMate 수술 관제"]');
  await expect(frame.locator(".connection")).toHaveAttribute("data-state", "dummy");
  await expect(frame.locator(".connection")).toBeVisible();

  const viewport = page.viewportSize();
  const hostBounds = await monitorFrame.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height };
  });
  expect(hostBounds).toEqual({
    x: 0,
    y: 0,
    width: viewport?.width,
    height: viewport?.height,
  });

  const canvasMetrics = await frame.locator("main.app-shell").evaluate((element) => {
    const htmlElement = element as HTMLElement;
    const bounds = htmlElement.getBoundingClientRect();
    return {
      offsetWidth: htmlElement.offsetWidth,
      offsetHeight: htmlElement.offsetHeight,
      renderedWidth: bounds.width,
      renderedHeight: bounds.height,
      position: getComputedStyle(htmlElement).position,
      bodyOverflow: getComputedStyle(document.body).overflow,
    };
  });
  expect(canvasMetrics.offsetWidth).toBe(1920);
  expect(canvasMetrics.offsetHeight).toBe(1080);
  expect(canvasMetrics.renderedWidth).toBeCloseTo(viewport?.width ?? 0, 1);
  expect(canvasMetrics.renderedHeight).toBeCloseTo(viewport?.height ?? 0, 1);
  expect(canvasMetrics.position).toBe("absolute");
  expect(canvasMetrics.bodyOverflow).toBe("hidden");
  const frameOverflow = await frame.locator("html").evaluate((element) => ({
    clientWidth: element.clientWidth,
    clientHeight: element.clientHeight,
    scrollWidth: element.scrollWidth,
    scrollHeight: element.scrollHeight,
  }));
  expect(frameOverflow.scrollWidth).toBe(frameOverflow.clientWidth);
  expect(frameOverflow.scrollHeight).toBe(frameOverflow.clientHeight);
  expect(transitions).toEqual([]);

  await page.goBack();
  await expect(page.locator('[data-slot="mission-workspace"]')).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("mission-main");
  await expect(page).not.toHaveURL(/workspace=monitor/);
  await expect(page.locator('iframe[title="SurgiMate 수술 관제"]')).toHaveCount(0);

  await page.goForward();
  await expect(page.locator('[data-slot="surgimate-monitor-workspace"]')).toHaveAttribute("data-state", "ready");
  expect(transitions).toEqual([]);
});

test("standalone SurgiMate monitor passes WCAG AA", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One browser run is sufficient for the monitor accessibility audit.");
  await installShellStubs(page);
  await page.goto("/monitor/index.html?mode=dummy");
  await expect(page.locator(".connection")).toHaveAttribute("data-state", "dummy");
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const summary = result.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    targets: violation.nodes.map((node) => node.target.join(" ")),
  }));
  expect(result.violations, JSON.stringify(summary, null, 2)).toEqual([]);
});

test("standalone SurgiMate monitor remains WCAG AA and scroll-safe on compact screens", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "The compact monitor accessibility audit runs once from the FHD project.");
  await page.setViewportSize({ width: 320, height: 800 });
  await installShellStubs(page);
  await page.goto("/monitor/index.html?mode=dummy");
  await expect(page.locator(".connection")).toHaveAttribute("data-state", "dummy");
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const summary = result.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    targets: violation.nodes.map((node) => node.target.join(" ")),
  }));
  expect(result.violations, JSON.stringify(summary, null, 2)).toEqual([]);
  const geometry = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    scrollHeight: document.documentElement.scrollHeight,
    clientHeight: document.documentElement.clientHeight,
  }));
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
  expect(geometry.scrollHeight).toBeLessThanOrEqual(geometry.clientHeight + 1);
});

test("keeps connection settings usable on compact viewports", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "The compact viewport matrix runs once from the FHD project.");
  await installShellStubs(page);
  await page.goto("/?workspace=monitor");
  await expect(page.locator('[data-slot="surgimate-monitor-workspace"]')).toHaveAttribute("data-state", "ready");

  const frame = page.frameLocator('iframe[title="SurgiMate 수술 관제"]');
  for (const viewport of [
    { width: 320, height: 800 },
    { width: 390, height: 844 },
    { width: 600, height: 800 },
    { width: 1024, height: 768 },
  ]) {
    await page.setViewportSize(viewport);
    await frame.getByRole("button", { name: "교수 프로필에서 연결 설정 열기" }).first().click();
    const panel = frame.locator(".settings-panel");
    const geometry = await panel.evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      return {
        left: bounds.left,
        right: bounds.right,
        bottom: bounds.bottom,
        width: bounds.width,
        height: bounds.height,
      };
    });
    expect(geometry.left, `${viewport.width}x${viewport.height}: panel left`).toBeGreaterThanOrEqual(-1);
    expect(geometry.right, `${viewport.width}x${viewport.height}: panel right`).toBeLessThanOrEqual(viewport.width + 1);
    expect(geometry.bottom, `${viewport.width}x${viewport.height}: panel bottom`).toBeLessThanOrEqual(viewport.height + 1);
    expect(geometry.width, `${viewport.width}x${viewport.height}: panel width`).toBeGreaterThanOrEqual(
      Math.min(250, viewport.width * 0.8),
    );
    await frame.getByRole("button", { name: "설정 닫기" }).click();
  }

  await page.setViewportSize({ width: 1024, height: 768 });
  await frame.getByRole("button", { name: "교수 프로필에서 연결 설정 열기" }).first().click();
  await frame.getByRole("radio", { name: /DUMMY DATA/ }).check();
  const dummyFileGeometry = await frame.locator(".dummy-file-control").evaluate((element) => {
    const input = element.querySelector("input");
    const browse = element.querySelector("label");
    const path = element.querySelector("code");
    const bounds = (candidate: Element | null) => candidate?.getBoundingClientRect();
    return {
      input: bounds(input),
      browse: bounds(browse),
      path: bounds(path),
    };
  });
  expect(dummyFileGeometry.input?.width).toBeLessThanOrEqual(1);
  expect(dummyFileGeometry.browse?.height).toBeGreaterThan(0);
  expect(dummyFileGeometry.path?.height).toBeGreaterThan(0);
  expect(dummyFileGeometry.browse?.right).toBeLessThanOrEqual((dummyFileGeometry.path?.left ?? 0) + 1);
  await frame.getByRole("button", { name: "설정 닫기" }).click();
});

test("returns safely from a direct monitor deep link and releases the iframe", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One viewport is enough for the direct-link lifecycle check.");
  const transitions = await installShellStubs(page);
  await page.goto("/?workspace=monitor");
  await expect(page.locator('[data-slot="surgimate-monitor-workspace"]')).toHaveAttribute("data-state", "ready");
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("surgimate-monitor-main");

  const returnButton = page.getByRole("button", { name: "Taskplanner 미션 화면으로 돌아가기" });
  await returnButton.focus();
  await expect(returnButton).toBeVisible();
  await returnButton.click();

  await expect(page.locator('[data-slot="mission-workspace"]')).toBeVisible();
  await expect(page).not.toHaveURL(/workspace=monitor/);
  await expect(page.locator('iframe[title="SurgiMate 수술 관제"]')).toHaveCount(0);
  expect(transitions).toEqual([]);
});

test("the live monitor emits only allowlisted subscribe and unsubscribe operations", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One viewport is enough for the public bridge wire contract.");
  await installShellStubs(page);
  await page.unroute("**/monitor/runtime-config.js");
  await page.route("**/monitor/runtime-config.js", (route) =>
    route.fulfill({ contentType: "application/javascript", body: liveRuntimeConfig }));

  const frames: Array<{
    op?: string;
    topic?: string;
    throttle_rate?: number;
    queue_length?: number;
    compression?: string;
  }> = [];
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:9092\/?$/, (socket) => {
    socket.onMessage((raw) => {
      frames.push(JSON.parse(typeof raw === "string" ? raw : raw.toString()));
    });
  });

  await page.goto("/?workspace=monitor");
  await expect(page.locator('[data-slot="surgimate-monitor-workspace"]')).toHaveAttribute("data-state", "ready");
  await expect.poll(() => frames.filter(({ op }) => op === "subscribe").length).toBe(10);
  await expect(page.frameLocator('iframe[title="SurgiMate 수술 관제"]').locator(".connection")).toBeVisible();

  const subscriptions = frames.filter(({ op }) => op === "subscribe");
  expect(subscriptions.map(({ topic }) => topic).sort()).toEqual(monitorTopicAllowlist);
  const cameraSubscription = subscriptions.find(
    ({ topic }) => topic === "/surgery/images/flir/compressed",
  );
  expect(cameraSubscription).toMatchObject({
    compression: "cbor",
    queue_length: 1,
    throttle_rate: 100,
  });

  const returnButton = page.getByRole("button", { name: "Taskplanner 미션 화면으로 돌아가기" });
  await returnButton.focus();
  await returnButton.click();
  await expect(page.locator('[data-slot="mission-workspace"]')).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("mission-main");
  await expect.poll(() => frames.filter(({ op }) => op === "unsubscribe").length).toBe(10);
  expect([...new Set(frames.map(({ op }) => op))].sort()).toEqual(["subscribe", "unsubscribe"]);
  expect(
    frames.filter(({ op }) => op === "unsubscribe").map(({ topic }) => topic).sort(),
  ).toEqual(monitorTopicAllowlist);
});

test("offers Mission recovery when the lazy monitor chunk fails", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One viewport is enough for the lazy-load recovery check.");
  await installShellStubs(page);
  await page.route("**/src/components/monitor/SurgiMateMonitorWorkspace.tsx*", (route) =>
    route.abort("failed"));

  await page.goto("/");
  await page.getByRole("button", { name: "수술 관제" }).click();
  await expect(page.getByRole("alert")).toContainText("수술 관제 화면을 불러오지 못했습니다.");
  await page.getByRole("button", { name: "미션 화면" }).click();
  await expect(page.locator('[data-slot="mission-workspace"]')).toBeVisible();
  await expect(page).not.toHaveURL(/workspace=monitor/);
});

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type WebSocketRoute } from "playwright/test";

type RuntimeMode = "live" | "llm-surgeon" | "replay" | "debug";

function debugStatus(): Record<string, unknown> {
  return {
    schema: "taskplanner.integration_debug.status.v1",
    stamp_sec: Date.now() / 1_000,
    session: {
      session_id: "a11y-session",
      state: "MONITOR_ONLY",
      armed: false,
      fault_locked: false,
      last_error: "",
      event_log_path: "/tmp/a11y-events.jsonl",
    },
    runtime: {
      ros_domain_id: "0",
      rmw_implementation: "rmw_fastrtps_cpp",
      discovery_range: "LOCALHOST",
      blocked_nodes: [],
      operational_runtime_stopped: true,
      manual_control_available: true,
      planner_coexistence_allowed: false,
      network: {
        primary_interface: "eth0",
        primary_ipv4: "127.0.0.1",
        prefix_length: 8,
        gateway_ipv4: "",
        multicast_capable: true,
        interface_present: true,
        link_up: true,
        addresses: [],
        settings_path: "/tmp/a11y-network.json",
        restart_supported: true,
        restart_scheduled: false,
      },
    },
    inputs: [],
    endpoints: [],
    action: {
      route: "",
      command_id: "",
      state: "idle",
      progress: 0,
      success: false,
      terminal: true,
      reason_code: "",
      recovery_required: false,
    },
    outputs: [],
    voice: { auto_execute: false, last_sentence: "", last_parse: {} },
    asr: { state: "STOPPED" },
    surgery_record: { state: "IDLE", history: [] },
    recent_events: [],
  };
}

async function installIsolatedStubs(page: Page, activeMode: RuntimeMode = "llm-surgeon") {
  await page.route("**/api/runtime/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      phase: "idle",
      active_mode: activeMode,
      requested_mode: activeMode,
      message: "Selected runtime is ready.",
      retryable: false,
    }),
  }));
  await page.route("**/api/runtime/transition", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      phase: "idle",
      active_mode: activeMode,
      requested_mode: activeMode,
      message: "Selected runtime is ready.",
      retryable: false,
    }),
  }));

  const handleSocket = (socket: WebSocketRoute) => {
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
            procedure_id: "a11y-procedure",
            active_bundle: "thyroidectomy_v1",
            filtered_phase: "P03",
            running: false,
            execution_state: "idle",
            instrument_states: [{ instrument_id: "grasper" }],
          },
        }));
        return;
      }
      if (message.op === "subscribe" && message.topic === "/integration/debug/status") {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: { data: JSON.stringify(debugStatus()) },
        }));
        return;
      }
      if (message.op === "subscribe" && message.topic === "/multicam_node/capture_status") {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            online_cameras: ["cam_1", "cam_2", "cam_3", "cam_4", "flir"],
            offline_cameras: [],
            all_cameras_online: true,
            uptime_sec: 20,
            cameras: [],
          },
        }));
        return;
      }
      if (message.op === "subscribe" && message.topic === "/tf_static") {
        socket.send(JSON.stringify({
          op: "publish",
          topic: message.topic,
          msg: {
            transforms: [{
              header: { frame_id: "humanoid" },
              child_frame_id: "tag1",
              transform: {
                translation: { x: 0, y: 0, z: 0 },
                rotation: { x: 0, y: 0, z: 0, w: 1 },
              },
            }],
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
        values: message.service === "/multicam_observer/rosapi/topics"
          ? {
              topics: ["/multicam_node/capture_status"],
              types: ["arpa_multicam_msgs/msg/CaptureStatus"],
            }
          : { success: true, accepted: true, message: "ok", model_ids: [] },
      }));
    });
  };

  await Promise.all([
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:(?:9090|19990)\/?$/, handleSocket),
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:(?:9091|19991)(?:\/multicam)?\/?$/, handleSocket),
    page.routeWebSocket(/ws:\/\/127\.0\.0\.1:(?:9099|19999)\/?$/, handleSocket),
  ]);
}

async function expectWcagAA(page: Page) {
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const summary = result.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    targets: violation.nodes.map((node) => node.target.join(" ")),
  }));
  expect(result.violations, JSON.stringify(summary, null, 2)).toEqual([]);
}

test("Mission and its safety dialog pass WCAG AA with composed motion", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One browser run is sufficient for the semantic audit.");
  await installIsolatedStubs(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "수술실 디지털 트윈" })).toBeVisible();
  await expectWcagAA(page);

  const reset = page.getByRole("button", { name: "초기화" });
  await reset.click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  const transformDuringEntrance = await dialog.evaluate((element) => getComputedStyle(element).transform);
  expect(transformDuringEntrance).not.toBe("none");
  await expect.poll(() => dialog.evaluate((element) => getComputedStyle(element).opacity)).toBe("1");
  await expectWcagAA(page);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(reset).toBeFocused();
});

test("Multicam and Debug workspaces pass WCAG AA", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One browser run is sufficient for the semantic audit.");
  await installIsolatedStubs(page);
  await page.goto("/?workspace=multicam");
  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible();
  const modelViewport = page.locator(".ops-tf-canvas");
  await expect(modelViewport).toHaveAttribute("data-model-state", "ready", { timeout: 20_000 });
  await expect.poll(async () => Number(await modelViewport.getAttribute("data-model-mesh-count"))).toBeGreaterThan(0);
  const modelBounds = (await modelViewport.getAttribute("data-model-bounds"))
    ?.split(",")
    .map(Number) ?? [];
  expect(modelBounds).toHaveLength(6);
  // Guard the app-space bounds captured from the source GLB. A 5 mm tolerance
  // allows quantization without accepting a shifted or expanded CAD model.
  expect(modelBounds[0]).toBeCloseTo(-0.694, 2);
  expect(modelBounds[1]).toBeCloseTo(-1.023, 2);
  expect(modelBounds[2]).toBeCloseTo(-1.257, 2);
  expect(modelBounds[3]).toBeCloseTo(0.423, 2);
  expect(modelBounds[4]).toBeCloseTo(0.752, 2);
  expect(modelBounds[5]).toBeCloseTo(0.088, 2);
  await expectWcagAA(page);

  await page.addInitScript(() => {
    window.localStorage.setItem("taskplanner.runtimeMode.llm", "debug");
  });
  await page.unroute("**/api/runtime/status");
  await page.route("**/api/runtime/status", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      phase: "idle",
      active_mode: "debug",
      requested_mode: "debug",
      message: "Selected runtime is ready.",
      retryable: false,
    }),
  }));
  await page.goto("/");
  await expect(page.locator("[data-slot='debug-workspace']")).toBeVisible();
  await expectWcagAA(page);
});

test("reduced motion and compact targets remain accessible", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "This test sets its own compact viewport.");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await installIsolatedStubs(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "수술실 디지털 트윈" })).toBeVisible();

  await page.getByRole("button", { name: "초기화" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  const reducedMotionState = await page.evaluate(() => ({
    dialogTransform: getComputedStyle(document.querySelector(".safety-dialog")!).transform,
    longAnimations: document.getAnimations({ subtree: true }).flatMap((animation) => {
      const timing = animation.effect?.getComputedTiming();
      const keyframes = animation.effect instanceof KeyframeEffect
        ? animation.effect.getKeyframes()
        : [];
      const properties = Array.from(new Set(keyframes.flatMap((frame) =>
        Object.keys(frame).filter((key) => !["offset", "computedOffset", "easing", "composite"].includes(key)))));
      const hasSpatialMotion = properties.some((property) =>
        ["transform", "translate", "scale", "rotate"].includes(property));
      if (
        Number(timing?.duration ?? 0) <= 250 &&
        Number(timing?.iterations ?? 1) <= 1 &&
        !hasSpatialMotion
      ) return [];
      const target = animation.effect instanceof KeyframeEffect
        ? animation.effect.target as HTMLElement | null
        : null;
      return [{
        duration: Number(timing?.duration ?? 0),
        iterations: Number(timing?.iterations ?? 1),
        properties,
        target: target?.className || target?.tagName || "unknown",
      }];
    }),
  }));
  expect(["none", "matrix(1, 0, 0, 1, 0, 0)"]).toContain(reducedMotionState.dialogTransform);
  expect(reducedMotionState.longAnimations, JSON.stringify(reducedMotionState.longAnimations, null, 2)).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  const compactAudit = await page.evaluate(() => {
    const targets = Array.from(document.querySelectorAll<HTMLElement>(
      "button, a[href], input, select, textarea, [role='button'], [role='tab']",
    ));
    const failures = targets.flatMap((target) => {
      const style = getComputedStyle(target);
      const rect = target.getBoundingClientRect();
      if (
        style.display === "none" ||
        style.visibility === "hidden" ||
        Number(style.opacity) === 0 ||
        rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth
      ) return [];
      const hitTarget = target.matches("input[type='checkbox'], input[type='radio']")
        ? target.closest("label")?.getBoundingClientRect() ?? rect
        : rect;
      if (hitTarget.width >= 44 && hitTarget.height >= 44) return [];
      return [{
        label: target.getAttribute("aria-label") || target.textContent?.trim().slice(0, 60) || target.tagName,
        width: Math.round(hitTarget.width),
        height: Math.round(hitTarget.height),
      }];
    });
    return {
      failures,
      documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  });
  expect(compactAudit.failures, JSON.stringify(compactAudit.failures, null, 2)).toEqual([]);
  expect(compactAudit.documentOverflow).toBeLessThanOrEqual(1);
  await expectWcagAA(page);
});

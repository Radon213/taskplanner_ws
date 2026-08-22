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
    asr: {
      available: false,
      dependency_error: "",
      state: "STOPPED",
      endpoint_id: "cloud",
      server_url: "",
      topic: "/input/asr/final",
      device_id: null,
      device_name: "",
      devices: [],
      device_status: "NO_INPUT",
      device_message: "입력 장치 없음",
      connected: false,
      audio_level_dbfs: -60,
      peak_level_dbfs: -60,
      elapsed_sec: 0,
      blocks_captured: 0,
      input_dropped: 0,
      partial_text: "",
      finals: [],
      last_error: "",
      recording_path: "",
      transcript_path: "",
      sample_rate: 16_000,
      channels: 1,
      sample_width_bits: 16,
      block_frames: 320,
      wire_chunk_bytes: 0,
      input_sample_rate: 0,
      input_channels: 0,
      input_block_frames: 0,
      resampling: false,
      sent_chunks: 0,
      responses: 0,
      dropped_chunks: 0,
      sessions: 0,
      padded_final_bytes: 0,
      pending_chunks: 0,
    },
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
            instrument_states: [{
              instrument_id: "grasper",
              home_location_type: "rack",
              home_location_id: "grasper",
              location_type: "rack",
              location_id: "grasper",
              owner: "none",
              status: "available",
              confidence: 0.9,
              cleanliness_state: "sterile",
              contaminated: false,
              lifecycle_stage: "home_rack",
              reserved_for: "",
              last_holder: "none",
              next_required_transition: "",
              visual_anchor_id: "grasper",
            }],
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
            capture_dir: "/tmp/a11y-capture",
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
  await expect(page.locator('[data-slot="control-authority-strip"]')).toHaveCount(0);
  await expect(page.getByText("제어 권한 경계", { exact: true })).toHaveCount(0);
  await expect(page.locator(".system-pill")).toHaveAttribute("aria-live", "polite");
  await expect(page.locator(".system-pill")).toHaveAttribute("aria-atomic", "true");
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

test("Mission remains WCAG AA and scroll-safe at a compact phone width", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "The compact accessibility audit runs once from the FHD project.");
  await page.setViewportSize({ width: 320, height: 800 });
  await installIsolatedStubs(page, "live");
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "수술실 디지털 트윈" })).toBeVisible();
  await expect(page.getByRole("button", { name: "통합 Debug 관측 열기" })).toBeVisible();
  await expect(page.locator(".button-primary").first()).toHaveCSS("opacity", "1");
  await expectWcagAA(page);
  const geometry = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    overflowing: [...document.querySelectorAll("body *")]
      .filter((element) => {
        const style = getComputedStyle(element);
        const bounds = element.getBoundingClientRect();
        return (
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          style.display !== "inline" &&
          bounds.width > 0 &&
          (bounds.left < -1 || bounds.right > document.documentElement.clientWidth + 1)
        );
      })
      .map((element) => element.className || element.tagName)
      .slice(0, 20),
  }));
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
  expect(geometry.overflowing).toEqual([]);
});

test("landscape monitoring keeps the operating-room stage proportionate", async ({ page }) => {
  await installIsolatedStubs(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "수술실 디지털 트윈" })).toBeVisible();
  await expect(page.getByText("휴머노이드 도구 전달과 집도의 의도 흐름을 실시간으로 조율합니다.")).toHaveCount(0);
  await expect(page.getByRole("switch", { name: /객체 인식/ })).toHaveCount(0);
  await expect(page.locator(".timeline-area")).toBeHidden();

  const board = page.locator(".stage-area .foxglove-board");
  await expect(board).toBeVisible();
  const bounds = await board.evaluate((element) => {
    const { width, height } = element.getBoundingClientRect();
    return { width, height, ratio: width / height };
  });
  expect(bounds.height).toBeGreaterThan(600);
  expect(bounds.ratio).toBeCloseTo(1.55, 1);
});

test("caps the fixed Mission canvas instead of stretching it on UHD displays", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One UHD layout run is sufficient for the canvas-height regression.");
  await page.setViewportSize({ width: 3840, height: 2160 });
  await installIsolatedStubs(page);
  await page.goto("/");

  const bounds = await page.evaluate(() => {
    const shell = document.querySelector<HTMLElement>(".mission-app-shell");
    const layout = document.querySelector<HTMLElement>(".mission-layout");
    const stage = document.querySelector<HTMLElement>(".stage-card");
    if (!shell || !layout || !stage) return null;
    return {
      shellHeight: shell.getBoundingClientRect().height,
      layoutHeight: layout.getBoundingClientRect().height,
      stageHeight: stage.getBoundingClientRect().height,
    };
  });
  expect(bounds).not.toBeNull();
  expect(bounds?.shellHeight).toBeLessThanOrEqual(1080);
  expect(bounds?.layoutHeight).toBeLessThanOrEqual(946);
  expect(bounds?.stageHeight).toBeLessThanOrEqual(946);
});

test("keeps the command ribbon readable at an intermediate laptop width", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "This regression owns the 1024px intermediate layout.");
  await page.setViewportSize({ width: 1024, height: 768 });
  await installIsolatedStubs(page, "live");
  await page.goto("/");

  const ribbon = page.locator('[data-slot="mission-command-bar"]');
  const debugEntry = ribbon.locator(".debug-mode-entry");
  const integratedObserve = ribbon.getByRole("button", { name: "통합 Debug 관측 열기" });
  await expect(debugEntry).toBeVisible();
  await expect(integratedObserve).toBeVisible();
  const audit = await page.evaluate(() => {
    const viewport = document.documentElement.clientWidth;
    const entry = document.querySelector<HTMLElement>('[data-slot="mission-command-bar"] .debug-mode-entry');
    const navigation = document.querySelector<HTMLElement>('[data-slot="mission-command-bar"] .workspace-navigation');
    const model = document.querySelector<HTMLElement>('[data-slot="mission-command-bar"] .ribbon-model-control');
    const status = document.querySelector<HTMLElement>('[data-slot="mission-command-bar"] .ribbon-status-actions');
    if (!entry || !navigation || !model || !status) return null;
    const rect = (element: HTMLElement) => {
      const bounds = element.getBoundingClientRect();
      return { right: bounds.right, width: bounds.width, height: bounds.height };
    };
    return {
      viewport,
      entry: rect(entry),
      integratedObserve: rect(document.querySelector<HTMLElement>('[data-slot="mission-command-bar"] [aria-label="통합 Debug 관측 열기"]')!),
      navigation: rect(navigation),
      model: rect(model),
      status: rect(status),
      whiteSpace: getComputedStyle(entry).whiteSpace,
    };
  });
  expect(audit).not.toBeNull();
  expect(audit?.whiteSpace).toBe("nowrap");
  expect(audit?.entry.width).toBeGreaterThanOrEqual(100);
  expect(audit?.entry.height).toBeLessThanOrEqual(46);
  expect(audit?.integratedObserve.height).toBeGreaterThanOrEqual(44);
  expect(audit?.integratedObserve.width).toBeGreaterThanOrEqual(100);
  expect(audit?.navigation.right).toBeLessThanOrEqual((audit?.viewport ?? 0) + 1);
  expect(audit?.model.right).toBeLessThanOrEqual((audit?.viewport ?? 0) + 1);
  expect(audit?.status.right).toBeLessThanOrEqual((audit?.viewport ?? 0) + 1);

  // The same label must remain intact at the desktop breakpoint just before
  // the command-center ribbon can safely return to one row.
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.reload();
  const desktopEntry = page.locator('[data-slot="mission-command-bar"] .debug-mode-entry');
  await expect(desktopEntry).toBeVisible();
  await expect.poll(async () => (await desktopEntry.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(100);
});

test("keeps embedded camera controls distinct and LLM status readable across board scales", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "The responsive control audit runs once.");
  await installIsolatedStubs(page);
  await page.goto("/");

  for (const viewport of [
    { width: 320, height: 800 },
    { width: 1024, height: 768 },
    { width: 1366, height: 768 },
    { width: 1920, height: 1080 },
    { width: 2560, height: 1080 },
  ]) {
    await page.setViewportSize(viewport);
    const audit = await page.evaluate(() => {
      const visible = (element: HTMLElement) => {
        const style = getComputedStyle(element);
        const bounds = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && bounds.width > 0 && bounds.height > 0;
      };
      const cameras = [...document.querySelectorAll<HTMLElement>(".switchable-stage-camera")].map((camera) => {
        const cameraBounds = camera.getBoundingClientRect();
        const buttons = [...camera.querySelectorAll<HTMLElement>("button")]
          .filter(visible)
          .map((button) => {
            const bounds = button.getBoundingClientRect();
            return {
              label: button.getAttribute("aria-label") || button.textContent?.trim() || "",
              left: bounds.left,
              right: bounds.right,
              top: bounds.top,
              bottom: bounds.bottom,
              width: bounds.width,
              height: bounds.height,
            };
          });
        const overlaps = buttons.flatMap((left, index) => buttons.slice(index + 1).flatMap((right) => {
          const width = Math.max(0, Math.min(left.right, right.right) - Math.max(left.left, right.left));
          const height = Math.max(0, Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top));
          return width > 1 && height > 1 ? [`${left.label}/${right.label}`] : [];
        }));
        return {
          buttons,
          overlaps,
          contained: buttons.every((button) =>
            button.left >= cameraBounds.left - 1 && button.right <= cameraBounds.right + 1
            && button.top >= cameraBounds.top - 1 && button.bottom <= cameraBounds.bottom + 1),
        };
      });
      const surgeon = document.querySelector<HTMLElement>(".llm-surgeon-dock");
      const loadState = surgeon?.querySelector<HTMLElement>(".model-load-state");
      const gestureLabel = surgeon?.querySelector<HTMLElement>(".public-gesture-status > span");
      return {
        cameras,
        loadStateClipped: Boolean(loadState && loadState.scrollWidth > loadState.clientWidth + 1),
        gestureLabelClipped: Boolean(gestureLabel && gestureLabel.scrollWidth > gestureLabel.clientWidth + 1),
      };
    });

    for (const camera of audit.cameras) {
      expect(camera.buttons.length, `${viewport.width}px camera switch is missing`).toBeGreaterThan(0);
      expect(camera.overlaps, `${viewport.width}px camera controls overlap`).toEqual([]);
      expect(camera.contained, `${viewport.width}px camera control is clipped`).toBe(true);
      for (const button of camera.buttons) {
        expect(button.width, `${viewport.width}px ${button.label} width`).toBeGreaterThanOrEqual(44);
        expect(button.height, `${viewport.width}px ${button.label} height`).toBeGreaterThanOrEqual(44);
      }
    }
    if (viewport.width === 1366) {
      expect(audit.loadStateClipped).toBe(false);
      expect(audit.gestureLabelClipped).toBe(false);
    }
  }
});

test("Multicam and Debug workspaces pass WCAG AA", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One browser run is sufficient for the semantic audit.");
  await installIsolatedStubs(page);
  await page.goto("/?workspace=multicam");
  await expect(page.getByRole("heading", { name: "멀티캠 관제 콘솔" })).toBeVisible();
  await expect(page.locator('.ops-preview-card [role="tab"][aria-selected="true"]'))
    .toHaveAttribute("aria-controls", "multicam-camera-panel");
  await expect(page.locator("#multicam-camera-panel")).toHaveAttribute("role", "tabpanel");
  const colorTab = page.getByRole("tab", { name: "Color" });
  const depthTab = page.getByRole("tab", { name: "Depth" });
  await expect(colorTab).toHaveAttribute("tabindex", "0");
  await expect(depthTab).toHaveAttribute("tabindex", "-1");
  await colorTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(depthTab).toBeFocused();
  await expect(depthTab).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowLeft");
  await expect(colorTab).toBeFocused();
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

  await page.setViewportSize({ width: 768, height: 1024 });
  const inventoryLayout = await page.locator(".ops-table").evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return {
      display: getComputedStyle(element).display,
      right: bounds.right,
      viewport: document.documentElement.clientWidth,
      labelledCells: element.querySelectorAll("[data-label]").length,
    };
  });
  expect(inventoryLayout.display).toBe("block");
  expect(inventoryLayout.right).toBeLessThanOrEqual(inventoryLayout.viewport + 1);
  expect(inventoryLayout.labelledCells).toBeGreaterThanOrEqual(25);
  await expectWcagAA(page);

  for (const viewport of [
    { width: 320, height: 800 },
    { width: 390, height: 844 },
    { width: 600, height: 900 },
    { width: 768, height: 1024 },
    { width: 1024, height: 768 },
    { width: 1280, height: 800 },
  ]) {
    await page.setViewportSize(viewport);
    const layoutAudit = await page.evaluate(() => {
      const viewportWidth = document.documentElement.clientWidth;
      const selectors = [
        ".ops-header",
        ".ops-layout",
        ".ops-preview-card",
        ".ops-tf-card",
        ".ops-world-card",
        ".ops-capture-card",
        ".ops-topic-card",
        ".ops-camera-grid",
        ".ops-tf-canvas",
        ".ops-topic-workspace",
        ".ops-inventory-table-wrap",
      ];
      const failures = selectors.flatMap((selector) => Array.from(document.querySelectorAll<HTMLElement>(selector)).flatMap((element) => {
        const bounds = element.getBoundingClientRect();
        return bounds.left < -1 || bounds.right > viewportWidth + 1
          ? [{ selector, left: Math.round(bounds.left), right: Math.round(bounds.right), viewportWidth }]
          : [];
      }));
      return {
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: viewportWidth,
        failures,
      };
    });
    expect(layoutAudit.failures, `${viewport.width}x${viewport.height}: ${JSON.stringify(layoutAudit.failures)}`).toEqual([]);
    expect(layoutAudit.scrollWidth).toBeLessThanOrEqual(layoutAudit.clientWidth + 1);
  }

  await page.addInitScript(() => {
    window.localStorage.setItem("taskplanner.runtimeMode.live", "debug");
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
  const debugConnectionTab = page.getByRole("tab", { name: "ROS 연결" });
  const debugSttTab = page.getByRole("tab", { name: "STT 입력·USB 캡처" });
  await expect(debugConnectionTab).toBeVisible();
  await debugConnectionTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(debugSttTab).toBeFocused();
  await expect(debugSttTab).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Home");
  await expect(debugConnectionTab).toBeFocused();
  for (const viewport of [
    { width: 320, height: 480 },
    { width: 390, height: 844 },
    { width: 600, height: 800 },
  ]) {
    await page.setViewportSize(viewport);
    const debugOverflow = await page.evaluate(() => {
      const limit = document.documentElement.clientWidth;
      return [...document.querySelectorAll("body *")].filter((element) => {
        const style = getComputedStyle(element);
        const bounds = element.getBoundingClientRect();
        return (
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          style.display !== "inline" &&
          bounds.width > 0 &&
          (bounds.left < -1 || bounds.right > limit + 1) &&
          !element.classList.contains("skip-link")
        );
      }).length;
    });
    expect(debugOverflow, `${viewport.width}x${viewport.height} has ${debugOverflow} overflowing elements`).toBe(0);
  }
});

test("Multicam and Debug remain WCAG AA and scroll-safe on compact screens", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "The compact engineering workspace audit runs once from the FHD project.");
  await page.setViewportSize({ width: 390, height: 844 });
  await installIsolatedStubs(page);

  await page.goto("/?workspace=multicam");
  await expect(page.locator("[data-slot='multicam-ops-workspace']")).toBeVisible();
  await expectWcagAA(page);
  const multicamGeometry = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(multicamGeometry.scrollWidth).toBeLessThanOrEqual(multicamGeometry.clientWidth + 1);

  await page.goto("/");
  await page.addInitScript(() => {
    window.localStorage.setItem("taskplanner.runtimeMode.live", "debug");
  });
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
  const debugGeometry = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(debugGeometry.scrollWidth).toBeLessThanOrEqual(debugGeometry.clientWidth + 1);
});

test("CAM4 perception Debug panel remains WCAG AA and scroll-safe at phone widths", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "The compact perception audit runs once from the FHD project.");
  await page.addInitScript(() => {
    window.localStorage.setItem("taskplanner.runtimeMode.live", "debug");
  });
  await installIsolatedStubs(page, "debug");
  await page.goto("/");
  await expect(page.locator("[data-slot='debug-workspace']")).toBeVisible();
  await page.getByRole("tab", { name: /CAM4 인식 오버레이/ }).click();
  const panel = page.locator('[data-slot="debug-perception-panel"]');
  await expect(panel).toBeVisible();

  for (const viewport of [
    { width: 320, height: 800 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expectWcagAA(page);
    const geometry = await page.evaluate(() => {
      const clientWidth = document.documentElement.clientWidth;
      const overflowing = [...document.querySelectorAll<HTMLElement>(
        '[data-slot="debug-perception-panel"] *',
      )].flatMap((element) => {
        const style = getComputedStyle(element);
        const bounds = element.getBoundingClientRect();
        return style.display !== "none"
          && style.visibility !== "hidden"
          && style.display !== "inline"
          && bounds.width > 0
          && (bounds.left < -1 || bounds.right > clientWidth + 1)
          ? [{ tag: element.tagName, className: element.className, left: bounds.left, right: bounds.right }]
          : [];
      });
      return {
        clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        overflowing,
      };
    });
    expect(
      geometry.overflowing,
      `${viewport.width}x${viewport.height}: ${JSON.stringify(geometry.overflowing)}`,
    ).toEqual([]);
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
  }
});

test("defers the heavyweight TF model until the observer viewport is viewed", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "fhd", "One browser run is sufficient for the performance contract.");
  await installIsolatedStubs(page);
  await page.setViewportSize({ width: 390, height: 844 });
  const modelRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/models/humanoid-tray-tag1.glb")) modelRequests.push(request.url());
  });

  await page.goto("/?workspace=multicam");
  const modelViewport = page.locator(".ops-tf-canvas");
  await expect(modelViewport).toBeVisible();
  await page.waitForTimeout(700);
  expect(modelRequests).toEqual([]);

  await modelViewport.scrollIntoViewIfNeeded();
  await expect.poll(() => modelRequests.length).toBe(1);
  await expect(modelViewport).toHaveAttribute("data-model-state", "ready", { timeout: 20_000 });
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
  const dialogClose = dialog.getByRole("button", { name: "닫기" });
  const dialogConfirm = dialog.getByRole("button", { name: "실행 상태 초기화" });
  await expect(dialogClose).toBeFocused();
  await dialogClose.press("Tab");
  await expect(dialogConfirm).toBeFocused();
  await dialogConfirm.press("Tab");
  await expect(dialogClose).toBeFocused();
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
  await dialogClose.click();
  const resetButton = page.getByRole("button", { name: "초기화", exact: true });
  await expect(resetButton).toBeFocused();
  await resetButton.click();
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
    const stageArea = document.querySelector<HTMLElement>(".stage-area")?.getBoundingClientRect();
    const stageCard = document.querySelector<HTMLElement>(".stage-area > .stage-card")?.getBoundingClientRect();
    const board = document.querySelector<HTMLElement>(".stage-area .foxglove-board")?.getBoundingClientRect();
    const tabButtons = Array.from(document.querySelectorAll<HTMLElement>(
      ".observability-panel-decision .tab-switch button",
    ));
    return {
      failures,
      documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      stageWithinColumn: Boolean(
        stageArea && stageCard && board
        && stageCard.left >= stageArea.left - 1
        && stageCard.right <= stageArea.right + 1
        && board.left >= stageArea.left - 1
        && board.right <= stageArea.right + 1,
      ),
      tabsWithinViewport: tabButtons.every((button) => {
        const rect = button.getBoundingClientRect();
        return rect.left >= -1 && rect.right <= document.documentElement.clientWidth + 1;
      }),
    };
  });
  expect(compactAudit.failures, JSON.stringify(compactAudit.failures, null, 2)).toEqual([]);
  expect(compactAudit.documentOverflow).toBeLessThanOrEqual(1);
  expect(compactAudit.stageWithinColumn).toBe(true);
  expect(compactAudit.tabsWithinViewport).toBe(true);
  await expectWcagAA(page);
});

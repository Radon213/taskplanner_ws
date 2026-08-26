import { expect, test } from "playwright/test";

import {
  integrationReadinessBlockReason,
  normalizeExecutionRouteCommandResult,
  normalizeExecutionRouteState,
  normalizeIntegrationReadiness,
} from "../src/ros/runtimeAdmissionMessages";

const NOW_MS = 1_800_000_000_000;

function rosString(payload: Record<string, unknown>) {
  return { data: JSON.stringify(payload) };
}

function readinessPayload(overrides: Record<string, unknown> = {}) {
  return {
    schema: "taskplanner.integration_readiness.v1",
    stamp_sec: NOW_MS / 1_000,
    ready: true,
    checks: {
      contract_configuration: true,
      perception_input: true,
    },
    checklist: [
      {
        id: "contract_configuration",
        required: true,
        status: "pass",
        reason: "configured",
        detail: "",
      },
      {
        id: "perception_input",
        required: true,
        status: "pass",
        reason: "fresh",
        detail: "typed observations",
      },
    ],
    missing: [],
    details: {
      active_bundle: "thyroidectomy_v1",
      procedure_type: "thyroidectomy",
      robot_endpoint_source: "virtual",
      retraction_endpoint_source: "virtual",
      retraction_state_machine_suppressed: true,
    },
    ...overrides,
  };
}

function routePayload(overrides: Record<string, unknown> = {}) {
  return {
    schema: "taskplanner.execution_route_state.v1",
    stamp_sec: NOW_MS / 1_000,
    revision: 7,
    initialization_revision: 7,
    selected_source: "virtual",
    run_endpoint_source: "",
    retraction_source: "virtual",
    run_retraction_source: "",
    initialization_state: "initialized",
    action_server_ready: true,
    retraction_service_ready: true,
    source_readiness: {
      virtual: {
        action_server_ready: true,
        retraction_service_ready: true,
      },
    },
    route_control_enabled: true,
    active_request_count: 0,
    require_bed_robot_status: false,
    require_physical_stop_confirmation: false,
    retraction_state_machine_suppressed: true,
    ...overrides,
  };
}

test("normalizes a self-consistent fresh readiness projection", () => {
  const readiness = normalizeIntegrationReadiness(
    rosString(readinessPayload()),
  );

  expect(readiness).toEqual(expect.objectContaining({
    ready: true,
    activeBundle: "thyroidectomy_v1",
    robotEndpointSource: "virtual",
    retractionEndpointSource: "virtual",
    retractionStateMachineSuppressed: true,
  }));
  expect(readiness?.checklist).toHaveLength(2);
  expect(
    integrationReadinessBlockReason(
      readiness,
      NOW_MS,
      "thyroidectomy_v1",
      NOW_MS,
    ),
  ).toBeNull();
});

test("rejects readiness when presentation rows or missing checks contradict the gate", () => {
  const failedChecks = {
    contract_configuration: true,
    perception_input: false,
  };
  const contradictoryChecklist = readinessPayload({
    ready: false,
    checks: failedChecks,
    missing: ["perception_input"],
  });
  expect(normalizeIntegrationReadiness(rosString(contradictoryChecklist))).toBeNull();

  const contradictoryMissing = readinessPayload({
    ready: false,
    checks: failedChecks,
    checklist: undefined,
    missing: [],
  });
  expect(normalizeIntegrationReadiness(rosString(contradictoryMissing))).toBeNull();
});

test("accepts authored external suppression without bundle allowlists", () => {
  const customExternalDetails = {
    active_bundle: "custom_external_retraction",
    procedure_type: "custom_procedure",
    robot_endpoint_source: "external",
    retraction_endpoint_source: "external",
    retraction_state_machine_suppressed: true,
  };
  const suppressed = normalizeIntegrationReadiness(rosString(readinessPayload({
    details: customExternalDetails,
  })));
  expect(suppressed).toEqual(expect.objectContaining({
    activeBundle: "custom_external_retraction",
    retractionEndpointSource: "external",
    retractionStateMachineSuppressed: true,
  }));

  const enforced = normalizeIntegrationReadiness(rosString(readinessPayload({
    details: {
      ...customExternalDetails,
      retraction_state_machine_suppressed: false,
    },
  })));
  expect(enforced?.retractionStateMachineSuppressed).toBe(false);
});

test("rejects missing suppression and an unsuppressed virtual route", () => {
  const missingSuppression = readinessPayload({
    details: {
      active_bundle: "custom_external_retraction",
      procedure_type: "custom_procedure",
      robot_endpoint_source: "external",
      retraction_endpoint_source: "external",
    },
  });
  expect(normalizeIntegrationReadiness(rosString(missingSuppression))).toBeNull();

  const unsuppressedVirtual = readinessPayload({
    details: {
      active_bundle: "custom_virtual_retraction",
      procedure_type: "custom_procedure",
      robot_endpoint_source: "virtual",
      retraction_endpoint_source: "virtual",
      retraction_state_machine_suppressed: false,
    },
  });
  expect(normalizeIntegrationReadiness(rosString(unsuppressedVirtual))).toBeNull();
});

test("keeps missing, stale, bundle mismatch, and not-ready blockers distinct", () => {
  const ready = normalizeIntegrationReadiness(rosString(readinessPayload()));
  expect(integrationReadinessBlockReason(null, null, "thyroidectomy_v1", NOW_MS)).toBe("missing");
  expect(
    integrationReadinessBlockReason(ready, NOW_MS - 4_001, "thyroidectomy_v1", NOW_MS),
  ).toBe("stale");
  expect(
    integrationReadinessBlockReason(ready, NOW_MS, "other_bundle", NOW_MS),
  ).toBe("bundle_mismatch");

  const blocked = normalizeIntegrationReadiness(rosString(readinessPayload({
    ready: false,
    checks: { perception_input: false },
    checklist: undefined,
    missing: ["perception_input"],
  })));
  expect(
    integrationReadinessBlockReason(blocked, NOW_MS, "thyroidectomy_v1", NOW_MS),
  ).toBe("not_ready");
});

test("normalizes a route projection without turning discovery into admission", () => {
  const route = normalizeExecutionRouteState(rosString(routePayload()));

  expect(route).toEqual(expect.objectContaining({
    selectedSource: "virtual",
    retractionSource: "virtual",
    initializationState: "initialized",
    routeControlEnabled: true,
    activeRequestCount: 0,
  }));
  expect(route?.sourceEndpointReadiness.virtual).toEqual({
    actionServerReady: true,
    retractionServiceReady: true,
  });
});

test("rejects mismatched latched sources and physical claims on a virtual route", () => {
  expect(normalizeExecutionRouteState(rosString(routePayload({
    run_endpoint_source: "external",
  })))).toBeNull();
  expect(normalizeExecutionRouteState(rosString(routePayload({
    require_physical_stop_confirmation: true,
  })))).toBeNull();
  expect(normalizeExecutionRouteState(rosString(routePayload({
    source_readiness: { virtual: { action_server_ready: "yes" } },
  })))).toBeNull();
});

test("keeps route-command reset attestation explicit and fail closed", () => {
  const accepted = normalizeExecutionRouteCommandResult(JSON.stringify({
    ...routePayload(),
    digital_twin_reset: true,
  }));
  expect(accepted?.digitalTwinReset).toBe(true);
  expect(accepted?.state.revision).toBe(7);

  const notReset = normalizeExecutionRouteCommandResult(JSON.stringify({
    ...routePayload(),
    digital_twin_reset: false,
  }));
  expect(notReset?.digitalTwinReset).toBe(false);
  expect(notReset?.digitalTwinReset === true).toBe(false);

  expect(normalizeExecutionRouteCommandResult(JSON.stringify(routePayload()))).toBeNull();
  expect(normalizeExecutionRouteCommandResult(JSON.stringify({
    ...routePayload({ selected_source: "unknown" }),
    digital_twin_reset: true,
  }))).toBeNull();
});

test("rejects oversized and over-deep rosbridge payloads before normalization", () => {
  expect(normalizeIntegrationReadiness({ data: "x".repeat(256 * 1024 + 1) })).toBeNull();

  let nested: Record<string, unknown> = { leaf: true };
  for (let depth = 0; depth < 10; depth += 1) nested = { nested };
  expect(normalizeExecutionRouteState(rosString(routePayload({ extra: nested })))).toBeNull();
});

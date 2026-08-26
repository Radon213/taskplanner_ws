import { expect, test } from "playwright/test";

import {
  assertSetParametersAccepted,
  boolParameter,
  normalizeModelCatalogEntry,
  normalizeModelProviderStatus,
  parseLegacyModelCatalogResponse,
  parseModelCatalogResponse,
  setParametersRequest,
  stringParameter,
} from "../src/ros/modelCatalogMessages";

test("normalizes a bounded modern model catalog and ignores unknown future fields", () => {
  const projection = parseModelCatalogResponse({
    success: true,
    active_provider_id: "provider-a",
    active_model_id: "model-a",
    providers: [{
      provider_id: "provider-a",
      provider_name: "Provider A",
      endpoint: "http://model.invalid/v1",
      reachable: true,
      status: "online",
      detail: "ready",
      latency_sec: 0.125,
      model_count: 1,
      future_health_field: { state: "green" },
    }],
    models: [{
      provider_id: "provider-a",
      provider_name: "Provider A",
      model_id: "model-a",
      display_name: "Model A",
      capability: "vision",
      load_state: "loaded",
      selectable: true,
      detail: "",
      runtime_managed: true,
      available_actions: ["sleep", "wake", "future_action"],
      future_runtime_field: true,
    }],
    message: "connected",
    future_catalog_version: 2,
  });

  expect(projection.selection).toEqual({
    provider_id: "provider-a",
    model_id: "model-a",
  });
  expect(projection.status).toBe("connected");
  expect(projection.providers).toEqual([
    expect.objectContaining({
      provider_id: "provider-a",
      reachable: true,
      latency_sec: 0.125,
      model_count: 1,
    }),
  ]);
  expect(projection.models).toEqual([
    expect.objectContaining({
      model_id: "model-a",
      runtime_managed: true,
      available_actions: ["sleep", "wake"],
    }),
  ]);
});

test("projects a legacy ListModels response through the same catalog shape", () => {
  const projection = parseLegacyModelCatalogResponse({
    success: true,
    model_ids: ["legacy-a", "legacy-b"],
    message: "legacy connected",
  });

  expect(projection.status).toBe("legacy connected");
  expect(projection.selection).toEqual({
    provider_id: "legacy",
    model_id: "legacy-a",
  });
  expect(projection.providers).toEqual([
    expect.objectContaining({
      provider_id: "legacy",
      provider_name: "OpenAI compatible",
      model_count: 2,
    }),
  ]);
  expect(projection.models.map((entry) => entry.model_id)).toEqual([
    "legacy-a",
    "legacy-b",
  ]);
});

test("rejects malformed or structurally oversized catalog responses", () => {
  expect(() => parseModelCatalogResponse({
    success: "true",
    active_provider_id: "",
    active_model_id: "",
    providers: [],
    models: [],
    message: "",
  })).toThrow(/success field was invalid/);

  expect(() => parseLegacyModelCatalogResponse({
    success: true,
    model_ids: Array.from({ length: 257 }, (_, index) => `model-${index}`),
    message: "",
  })).toThrow(/payload bound or was malformed/);

  expect(normalizeModelProviderStatus({
    provider_id: "provider-a",
    reachable: true,
    latency_sec: Number.POSITIVE_INFINITY,
    model_count: 1,
  })).toBeNull();
  expect(normalizeModelCatalogEntry({
    provider_id: "provider-a",
    model_id: "model-a",
    selectable: "true",
  })).toBeNull();
});

test("builds typed ROS parameters and validates every SetParameters result", () => {
  const enabled = boolParameter("enabled", true);
  const model = stringParameter("model_id", "model-a");

  expect(setParametersRequest([enabled, model])).toEqual({
    parameters: [
      { name: "enabled", value: { type: 1, bool_value: true } },
      { name: "model_id", value: { type: 4, string_value: "model-a" } },
    ],
  });
  expect(() => assertSetParametersAccepted({
    results: [
      { successful: true, reason: "" },
      { successful: true, reason: "" },
    ],
  }, 2)).not.toThrow();

  expect(() => assertSetParametersAccepted({
    results: [{ successful: false, reason: "parameter update rejected by node" }],
  }, 1)).toThrow("parameter update rejected by node");
  expect(() => assertSetParametersAccepted({
    results: [{ successful: "false", reason: "invalid bool" }],
  }, 1)).toThrow(/invalid result/);
  expect(() => assertSetParametersAccepted({ results: [] }, 1)).toThrow(/result count/);
  expect(() => assertSetParametersAccepted({
    results: [{ successful: true, reason: "x".repeat(64 * 1024 + 1) }],
  }, 1)).toThrow(/malformed/);
});

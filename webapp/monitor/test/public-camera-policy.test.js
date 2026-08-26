import test from "node:test";
import assert from "node:assert/strict";

import {
  publicCameraMayRender,
  publicCameraReceptionStatus,
} from "../ros/public-camera-policy.js";

test("public FLIR reception remains fail-closed until the gateway contract is valid", () => {
  assert.deepEqual(
    publicCameraReceptionStatus({ gatewayReady: false, contractCompatible: false }),
    { ready: false, reason: "gateway_not_ready" },
  );
  assert.deepEqual(
    publicCameraReceptionStatus({ gatewayReady: true, contractCompatible: false }),
    { ready: false, reason: "contract_not_ready" },
  );
  assert.deepEqual(
    publicCameraReceptionStatus({ gatewayReady: true, contractCompatible: true }),
    { ready: true, reason: "ready" },
  );
});

test("validated public FLIR frames may render while the procedure is idle", () => {
  assert.equal(publicCameraMayRender({ contractCompatible: true, hasFrame: true }), true);
  assert.equal(publicCameraMayRender({ contractCompatible: false, hasFrame: true }), false);
  assert.equal(publicCameraMayRender({ contractCompatible: true, hasFrame: false }), false);
});

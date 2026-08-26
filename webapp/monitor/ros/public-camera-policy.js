export function publicCameraReceptionStatus({ gatewayReady, contractCompatible } = {}) {
  if (!gatewayReady) return Object.freeze({ ready: false, reason: "gateway_not_ready" });
  if (!contractCompatible) return Object.freeze({ ready: false, reason: "contract_not_ready" });
  return Object.freeze({ ready: true, reason: "ready" });
}

export function publicCameraMayRender({ contractCompatible, hasFrame } = {}) {
  return contractCompatible === true && hasFrame === true;
}

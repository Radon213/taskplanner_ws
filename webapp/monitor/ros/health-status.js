/**
 * Return whether the monitor should present the gateway as degraded.
 *
 * ``unavailableSources`` and ``staleSources`` are diagnostic inventories. A
 * gateway can remain healthy when only optional sources are absent, so the
 * monitor follows the gateway's reviewed boolean rather than reclassifying
 * every diagnostic row as an operational warning.
 */
export function gatewayHealthIsDegraded(health) {
  return !health || health.healthy !== true;
}

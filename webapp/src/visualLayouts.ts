import type { LayoutAnchor, LayoutBundle, LayoutEntity } from "./types";

type EntityVisual = Pick<LayoutEntity, "x" | "y" | "width" | "height">;
type AnchorVisual = Pick<LayoutAnchor, "x" | "y">;

type VisualLayoutOverride = {
  entities: Record<string, EntityVisual>;
  anchors: Record<string, AnchorVisual>;
};

const thyroidectomy: VisualLayoutOverride = {
  entities: {
    cleaner_station: { x: 13, y: 14, width: 11.5, height: 11.5 },
    instrument_rack: { x: 9.5, y: 43.5, width: 23, height: 28 },
    humanoid_body: { x: 36.5, y: 34, width: 17, height: 32 },
    thyroid_bed: { x: 58, y: 32.5, width: 24, height: 28 },
    mayo_stand: { x: 55.5, y: 62.5, width: 29, height: 8.5 },
    surgeon_actor: { x: 84.5, y: 29, width: 12, height: 27 },
    unknown_zone: { x: 88, y: 62, width: 10, height: 8 },
  },
  anchors: {
    cleaner_slot: { x: 18.8, y: 19.8 },
    robot_left_hand: { x: 37.5, y: 46 },
    robot_right_hand: { x: 53.5, y: 41.5 },
    surgeon_receive_zone: { x: 79.8, y: 43.5 },
    surgeon_return_zone: { x: 78.5, y: 53 },
    surgeon_hand: { x: 88, y: 42.5 },
    field_region_thyroid: { x: 66.2, y: 44.8 },
    mayo_recovery_zone: { x: 61.5, y: 66.5 },
    mayo_reuse_zone: { x: 76.5, y: 66.5 },
    unknown_zone_anchor: { x: 93, y: 66 },
    main_tray_slot_1: { x: 14.5, y: 51.5 },
    main_tray_slot_2: { x: 23.5, y: 51.5 },
    main_tray_slot_3: { x: 14.5, y: 59.5 },
    main_tray_slot_4: { x: 23.5, y: 59.5 },
    main_tray_slot_5: { x: 14.5, y: 67.2 },
    main_tray_slot_6: { x: 23.5, y: 67.2 },
  },
};

const nephrectomy: VisualLayoutOverride = {
  entities: {
    cleaner_station: { x: 13.5, y: 14, width: 11.5, height: 11.5 },
    instrument_rack: { x: 9, y: 42.5, width: 25, height: 30 },
    humanoid_body: { x: 36, y: 34.5, width: 17, height: 32 },
    nephrectomy_bed: { x: 58, y: 32, width: 25, height: 29 },
    mayo_stand: { x: 56, y: 63, width: 29, height: 8.5 },
    surgeon_actor: { x: 84.5, y: 29.5, width: 12, height: 27 },
    unknown_zone: { x: 88, y: 62, width: 10, height: 8 },
  },
  anchors: {
    cleaner_slot: { x: 19.2, y: 19.8 },
    robot_left_hand: { x: 37, y: 47 },
    robot_right_hand: { x: 53, y: 42 },
    surgeon_receive_zone: { x: 79.8, y: 44 },
    surgeon_return_zone: { x: 78.2, y: 54 },
    surgeon_hand: { x: 88, y: 43 },
    field_region_kidney_hilum: { x: 66.2, y: 45 },
    mayo_recovery_zone: { x: 62, y: 67 },
    mayo_reuse_zone: { x: 77, y: 67 },
    unknown_zone_anchor: { x: 93, y: 66 },
    main_tray_slot_1: { x: 14.5, y: 50 },
    main_tray_slot_2: { x: 24.5, y: 50 },
    main_tray_slot_3: { x: 14.5, y: 56.5 },
    main_tray_slot_4: { x: 24.5, y: 56.5 },
    main_tray_slot_5: { x: 14.5, y: 63 },
    main_tray_slot_6: { x: 24.5, y: 63 },
    main_tray_slot_7: { x: 14.5, y: 69.5 },
    main_tray_slot_8: { x: 24.5, y: 69.5 },
  },
};

export const visualLayoutOverrides: Record<string, VisualLayoutOverride> = {
  thyroidectomy,
  nephrectomy,
};

export function applyVisualLayout(bundleName: string, source: LayoutBundle): LayoutBundle {
  const override = visualLayoutOverrides[bundleName];
  if (!override) return source;

  return {
    ...source,
    entities: source.entities.map((entity) => ({
      ...entity,
      ...(override.entities[entity.id] ?? {}),
    })),
    anchors: source.anchors.map((anchor) => ({
      ...anchor,
      ...(override.anchors[anchor.id] ?? {}),
    })),
  };
}

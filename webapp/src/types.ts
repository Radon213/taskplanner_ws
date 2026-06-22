export type InstrumentState = {
  instrument_id: string;
  home_location_type: string;
  home_location_id: string;
  location_type: string;
  location_id: string;
  owner: string;
  status: string;
  confidence: number;
  cleanliness_state: string;
  contaminated: boolean;
  reserved_for: string;
  last_holder: string;
  lifecycle_stage: string;
  next_required_transition: string;
  visual_anchor_id: string;
};

export type SimulationState = {
  procedure_id: string;
  active_bundle: string;
  running: boolean;
  execution_state: string;
  filtered_phase: string;
  robot_state: string;
  surgeon_intent: string;
  surgeon_request_tool: string;
  surgeon_ready_for_handover: boolean;
  surgeon_ready_for_retrieval: boolean;
  cleaner_busy: boolean;
  cleaner_remaining_sec: number;
  pending_transition_tools: string[];
  active_recovery_tools: string[];
  right_hand_tool: string;
  left_hand_tool: string;
  prepositioned_tool: string;
  active_robot_task_id: string;
  active_robot_task_type: string;
  active_robot_task_tool_id: string;
  active_robot_task_arm: string;
  active_robot_task_source_anchor: string;
  active_robot_task_target_anchor: string;
  active_robot_task_progress: number;
  active_robot_task_remaining_sec: number;
  instrument_states: InstrumentState[];
  recent_events: string[];
  layout_json?: string;
};

export type SimulationEvent = {
  ui_id?: string;
  event_type: string;
  instrument_id: string;
  from_anchor: string;
  to_anchor: string;
  arm: string;
  status: string;
  detail: string;
};

export type SurgeonState = {
  procedure_id: string;
  phase_id: string;
  intent: string;
  requested_tool: string;
  ready_for_handover: boolean;
  ready_for_retrieval: boolean;
  scripted: boolean;
  voice_text: string;
  scene_note: string;
};

export type BTDecision = {
  decision: string;
  selected_tool: string;
  selected_tool_lifecycle: string;
  next_required_transition: string;
  action: string;
  handover_allowed: boolean;
  rationale: string;
  decision_reason: string;
  blocking_guard: string;
};

export type SkillStatus = {
  command_id: string;
  action: string;
  instrument_id: string;
  state: string;
  success: boolean;
  message: string;
  arm: string;
  source_location_id: string;
  source_location_type: string;
  target_location_id: string;
  target_location_type: string;
  target_owner: string;
  cleaning_required: boolean;
  mode: string;
  progress: number;
  elapsed_sec: number;
  remaining_sec: number;
};

export type VLMHealth = {
  connected: boolean;
  healthy: boolean;
  model_id: string;
  image_source: string;
  latency_sec: number;
  prompt_chars: number;
  output_chars: number;
  parse_retry_count: number;
  last_error: string;
  last_mode: string;
};

export type VLMResult = {
  source: string;
  schema_version: string;
  raw_json: string;
  summary: string;
  phase_ids: string[];
  phase_confidences: number[];
  observed_tool_ids: string[];
  observed_location_ids: string[];
  observed_location_types: string[];
  observed_confidences: number[];
  gesture_event_type: string;
  gesture_requested_tool: string;
  gesture_hand_pose: string;
  gesture_confidence: number;
  uncertainty: number;
};

export type VLMReducerDecision = {
  source: string;
  proposal_id: string;
  instrument_id: string;
  proposed_transition: string;
  reducer_result: string;
  reducer_reason: string;
  accepted: boolean;
  confidence: number;
  detail_json: string;
};

export type CompressedImageFrame = {
  src: string;
  format: string;
  topic: string;
  frameId: string;
  sizeBytes: number;
  receivedAt: number;
};

export type LayoutEntity = {
  id: string;
  type: string;
  x: number;
  y: number;
  width: number;
  height: number;
  label: string;
  display_name?: string;
  display_name_ko?: string;
};

export type LayoutAnchor = {
  id: string;
  attached_to: string;
  x: number;
  y: number;
  label?: string;
  display_name?: string;
  display_name_ko?: string;
};

export type DisplayCatalogEntry = {
  display_name?: string;
  display_name_ko?: string;
  severity?: "normal" | "warning" | "error";
  tone?: "neutral" | "robot" | "surgeon" | "cleaning" | "warning";
  category?: string;
  tool_display_state?: "waiting" | "handover" | "using" | "recovery" | "cleaning";
  tool_tone?: "ready" | "active" | "surgeon" | "cleaning" | "recovery" | "danger";
  badge_tone?: "neutral" | "active" | "warning" | "danger";
};

export type DisplayCatalog = {
  lifecycle?: Record<string, DisplayCatalogEntry>;
  actions?: Record<string, DisplayCatalogEntry>;
  skill_states?: Record<string, DisplayCatalogEntry>;
  transitions?: Record<string, DisplayCatalogEntry>;
  intents?: Record<string, DisplayCatalogEntry>;
  events?: Record<string, DisplayCatalogEntry>;
};

export type LayoutDisplayMetadata = {
  procedure?: {
    id: string;
    display_name?: string;
    display_name_ko?: string;
  };
  phases?: Array<{
    id: string;
    display_name?: string;
    display_name_ko?: string;
  }>;
  instruments?: Array<{
    id: string;
    display_name?: string;
    display_name_ko?: string;
    aliases?: string[];
    category?: string;
    role?: string;
    handover_profile?: string;
    requestable?: boolean;
  }>;
  requestable_instruments?: string[];
  display_catalog?: DisplayCatalog;
  bundles?: Array<{
    id: string;
    display_name?: string;
    display_name_ko?: string;
    requestable_instruments?: string[];
    phases?: Array<{
      id: string;
      display_name?: string;
      display_name_ko?: string;
    }>;
    instruments?: Array<{
      id: string;
      display_name?: string;
      display_name_ko?: string;
      aliases?: string[];
      category?: string;
      role?: string;
      handover_profile?: string;
      requestable?: boolean;
    }>;
  }>;
};

export type LayoutBundle = {
  entities: LayoutEntity[];
  anchors: LayoutAnchor[];
  metadata?: LayoutDisplayMetadata;
};

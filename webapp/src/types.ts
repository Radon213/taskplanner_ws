export type InstrumentState = {
  instrument_id: string;
  instance_id?: string;
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
  preposition_origin_location_type?: string;
  preposition_origin_location_id?: string;
  preposition_origin_lifecycle_stage?: string;
};

export type RosTime = {
  sec: number;
  nanosec: number;
};

export type BedRobotArmState = {
  arm_id: string;
  role: "retraction" | string;
  role_instance_id: string;
  state:
    | "standby"
    | "direct_teach"
    | "retracting"
    | "changing_tool"
    | "moving_to_standby"
    | "fault"
    | "protective_stop"
    | "unknown"
    | string;
  direct_teach_active: boolean;
  reason_code: string;
};

export type BedRobotArmStateArray = {
  stamp: RosTime;
  revision: number;
  procedure_type: string;
  arms: BedRobotArmState[];
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
  active_recovery_tool_instances?: string[];
  right_hand_tool: string;
  right_hand_tool_instance_id?: string;
  left_hand_tool: string;
  left_hand_tool_instance_id?: string;
  prepositioned_tool: string;
  prepositioned_tool_instance_id?: string;
  active_robot_task_id: string;
  active_robot_task_type: string;
  active_robot_task_tool_id: string;
  active_robot_task_tool_instance_id?: string;
  active_robot_task_arm: string;
  active_robot_task_source_anchor: string;
  active_robot_task_target_anchor: string;
  active_robot_task_progress: number;
  active_robot_task_remaining_sec: number;
  bed_robot_arms: BedRobotArmState[];
  instrument_states: InstrumentState[];
  recent_events: string[];
  layout_json?: string;
};

export type WorldState = {
  procedure_id: string;
  running: boolean;
  execution_state: string;
  filtered_phase: string;
  phase_confidence: number;
  phase_uncertain: boolean;
  phase_stability: number;
  expected_instruments: string[];
  available_instruments: string[];
  right_hand_tool: string;
  right_hand_tool_instance_id?: string;
  left_hand_tool: string;
  left_hand_tool_instance_id?: string;
  prepositioned_tool: string;
  prepositioned_tool_instance_id?: string;
  predicted_tool: string;
  predicted_tool_confidence: number;
  predicted_tool_stability_sec: number;
  surgeon_request_tool: string;
  surgeon_request_instance_id?: string;
  surgeon_request_generation?: number;
  surgeon_request_additional_instance_assumed?: boolean;
  explicit_request_voice_backed: boolean;
  bed_robot_arms: BedRobotArmState[];
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

export type SurgeonLLMDecision = {
  model_id: string;
  raw_json: string;
  accepted: boolean;
  reject_reason: string;
  action: string;
  tool: string;
  request_mode: string;
  speech: string;
  hidden_phase: string;
  latency_sec: number;
  seed: number;
  overlay_json: string;
};

export type SpeechUtterance = {
  stamp: RosTime;
  start_stamp: RosTime;
  end_stamp: RosTime;
  utterance_id: string;
  text: string;
  is_final: boolean;
  has_confidence: boolean;
  confidence: number;
  speaker_role: string;
  language: string;
  source: string;
};

export type LiveAsrDevice = {
  id: number;
  name: string;
  input_channels: number;
  default_samplerate: number;
  default: boolean;
};

export type LiveAsrFinal = {
  stamp: string;
  text: string;
  response_latency_ms: number | null;
  latency_basis: string;
  latency_correlated: boolean;
};

export type LiveAsrRoutePolicy = "cloud" | "lan" | "auto";

export type LiveAsrLanHealth = {
  enabled: boolean;
  state: "UNKNOWN" | "CHECKING" | "READY" | "UNAVAILABLE" | "STALE" | string;
  method: string;
  age_ms: number | null;
  latency_ms: number | null;
  consecutive_failures: number;
  last_error: string;
};

export type LiveAsrStatus = {
  schema: "taskplanner.asr.status.v1";
  stamp_sec: number;
  available: boolean;
  dependency_error: string;
  state: "UNAVAILABLE" | "STOPPED" | "STARTING" | "LISTENING" | "STOPPING" | "ERROR" | string;
  server_url: string;
  topic: string;
  device_id: number | null;
  device_name: string;
  devices: LiveAsrDevice[];
  device_status: string;
  device_message: string;
  connected: boolean;
  audio_level_dbfs: number;
  peak_level_dbfs: number;
  elapsed_sec: number;
  partial_text: string;
  finals: LiveAsrFinal[];
  last_error: string;
  sample_rate: number;
  channels: number;
  sample_width_bits: number;
  endpoint_id: "cloud" | "lan" | string;
  route_policy: LiveAsrRoutePolicy;
  selection_reason: string;
  lan_health: LiveAsrLanHealth;
};

export type LiveAsrControlResult = {
  accepted: boolean;
  message: string;
};

export type ShadowReplayState = {
  stamp: RosTime;
  run_id: string;
  case_id: string;
  procedure_id: string;
  state: string;
  mode: "realtime_1x" | "elastic_demo" | string;
  loaded: boolean;
  running: boolean;
  paused: boolean;
  completed: boolean;
  source_time_sec: number;
  duration_sec: number;
  image_duration_sec: number;
  wall_elapsed_sec: number;
  playback_rate: number;
  elastic_hold_sec: number;
  hold_reason: string;
  last_error: string;
  published_image_count: number;
  published_transcript_count: number;
  completed_vlm_count: number;
  pending_vlm_count: number;
  active_skill_count: number;
};

export type ModelProviderStatus = {
  provider_id: string;
  provider_name: string;
  endpoint: string;
  reachable: boolean;
  status: string;
  detail: string;
  latency_sec: number;
  model_count: number;
};

export type ModelCatalogEntry = {
  provider_id: string;
  provider_name: string;
  model_id: string;
  display_name: string;
  capability: string;
  load_state: string;
  selectable: boolean;
  detail: string;
  runtime_managed: boolean;
  available_actions: ModelRuntimeCommand[];
};

export type ModelSelection = {
  provider_id: string;
  model_id: string;
};

export type ModelRuntimeCommand = "load" | "unload" | "sleep" | "wake";

export type BTDecision = {
  decision: string;
  selected_tool: string;
  selected_tool_instance_id?: string;
  request_generation?: number;
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
  instrument_instance_id?: string;
  request_generation?: number;
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

export type InputSourceStatus = {
  stamp?: RosTime;
  source_id: string;
  modality: string;
  state: "READY" | "STALE" | "MISSING" | "RECOVERING" | "ERROR" | "DISABLED" | string;
  healthy: boolean;
  last_observation_stamp?: RosTime;
  age_sec: number;
  received_count: number;
  accepted_count: number;
  rejected_count: number;
  epoch: number;
  dropped_count: number;
  error_code: string;
  detail: string;
};

export type VLMResult = {
  stamp?: RosTime;
  source: string;
  source_epoch?: number;
  source_sequence?: number;
  correlation_id?: string;
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

export type Cam4ToolRequestObservation = {
  available: boolean;
  state: "request" | "not_request" | "hand_with_tool" | "uncertain";
  requested: boolean | null;
  confidence: number;
  sourceStampSec: number;
  receivedAt: number;
  onsetSourceStampSec: number;
  onsetReceivedAt: number;
};

export type ShadowGroundTruthState = {
  available: boolean;
  runId: string;
  caseId: string;
  sourceTimeSec: number;
  phase: {
    phaseId: string;
    startSec: number;
    endSec: number;
    active: boolean;
  };
  eventId: string;
  active: boolean;
  startSec: number;
  endSec: number;
  receivedAt: number;
  eventStartReceivedAt: number;
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
  default_phase_id?: string;
  phases?: Array<{
    id: string;
    display_name?: string;
    display_name_ko?: string;
  }>;
  normal_phase_ids?: string[];
  interrupt_phase_ids?: string[];
  instruments?: Array<{
    id: string;
    display_name?: string;
    display_name_ko?: string;
    aliases?: string[];
    category?: string;
    inventory_count?: number;
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
    default_phase_id?: string;
    requestable_instruments?: string[];
    phases?: Array<{
      id: string;
      display_name?: string;
      display_name_ko?: string;
    }>;
    normal_phase_ids?: string[];
    interrupt_phase_ids?: string[];
    instruments?: Array<{
      id: string;
      display_name?: string;
      display_name_ko?: string;
      aliases?: string[];
      category?: string;
      inventory_count?: number;
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

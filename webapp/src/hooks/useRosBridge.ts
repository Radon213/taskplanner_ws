import { useEffect, useRef, useState, startTransition } from "react";
import ROSLIB from "roslib";

import type {
  BTDecision,
  SimulationEvent,
  SimulationState,
  SkillStatus,
  SurgeonState,
  VLMHealth,
  VLMReducerDecision,
  VLMResult,
} from "../types";

const DEFAULT_STATE: SimulationState = {
  procedure_id: "",
  active_bundle: "",
  running: false,
  execution_state: "idle",
  filtered_phase: "",
  robot_state: "idle",
  surgeon_intent: "",
  surgeon_request_tool: "",
  surgeon_ready_for_handover: false,
  surgeon_ready_for_retrieval: false,
  cleaner_busy: false,
  cleaner_remaining_sec: 0,
  pending_transition_tools: [],
  active_recovery_tools: [],
  right_hand_tool: "",
  left_hand_tool: "",
  prepositioned_tool: "",
  active_robot_task_id: "",
  active_robot_task_type: "",
  active_robot_task_tool_id: "",
  active_robot_task_arm: "",
  active_robot_task_source_anchor: "",
  active_robot_task_target_anchor: "",
  active_robot_task_progress: 0,
  active_robot_task_remaining_sec: 0,
  instrument_states: [],
  recent_events: [],
  layout_json: "",
};

const DEFAULT_SURGEON: SurgeonState = {
  procedure_id: "",
  phase_id: "",
  intent: "",
  requested_tool: "",
  ready_for_handover: false,
  ready_for_retrieval: false,
  scripted: true,
  voice_text: "",
  scene_note: "",
};

const DEFAULT_BT_DECISION: BTDecision = {
  decision: "idle",
  selected_tool: "",
  selected_tool_lifecycle: "",
  next_required_transition: "",
  action: "",
  handover_allowed: false,
  rationale: "",
  decision_reason: "",
  blocking_guard: "",
};

const DEFAULT_SKILL_STATUS: SkillStatus = {
  command_id: "",
  action: "",
  instrument_id: "",
  state: "",
  success: false,
  message: "",
  arm: "",
  source_location_id: "",
  source_location_type: "",
  target_location_id: "",
  target_location_type: "",
  target_owner: "",
  cleaning_required: false,
  mode: "",
  progress: 0,
  elapsed_sec: 0,
  remaining_sec: 0,
};

const DEFAULT_VLM_HEALTH: VLMHealth = {
  connected: false,
  healthy: false,
  model_id: "",
  image_source: "",
  latency_sec: 0,
  prompt_chars: 0,
  output_chars: 0,
  parse_retry_count: 0,
  last_error: "",
  last_mode: "",
};

const DEFAULT_VLM_RESULT: VLMResult = {
  source: "",
  schema_version: "",
  raw_json: "",
  summary: "",
  phase_ids: [],
  phase_confidences: [],
  observed_tool_ids: [],
  observed_location_ids: [],
  observed_location_types: [],
  observed_confidences: [],
  gesture_event_type: "",
  gesture_requested_tool: "",
  gesture_hand_pose: "",
  gesture_confidence: 0,
  uncertainty: 0,
};

export type OverrideAck = {
  eventType: string;
  toolId: string;
  message: string;
  voiceText?: string;
};

export type OverridePayload = {
  eventType: "request_tool" | "voice_request" | "return_tool";
  requestedTool: string;
  voiceText: string;
  toolLabel: string;
};

export type ControlCommand = "start" | "pause" | "resume" | "stop" | "reset";

function runtimeStatusMessage(state: SimulationState): string {
  if (state.execution_state === "running" && state.running) {
    return `simulation running on ${state.active_bundle}`;
  }
  if (state.execution_state === "paused" && state.running) {
    return `simulation paused on ${state.active_bundle}`;
  }
  if (state.execution_state === "idle" && !state.running) {
    return "simulation runtime reset to idle";
  }
  if (state.execution_state === "halted" && !state.running) {
    return "simulation stopped";
  }
  return "";
}

export function useRosBridge() {
  const [url, setUrl] = useState("ws://127.0.0.1:9090");
  const [connected, setConnected] = useState(false);
  const [bundle, setBundle] = useState("");
  const [simulationState, setSimulationState] = useState<SimulationState>(DEFAULT_STATE);
  const [surgeonState, setSurgeonState] = useState<SurgeonState>(DEFAULT_SURGEON);
  const [btDecision, setBtDecision] = useState<BTDecision>(DEFAULT_BT_DECISION);
  const [skillStatus, setSkillStatus] = useState<SkillStatus>(DEFAULT_SKILL_STATUS);
  const [vlmHealth, setVlmHealth] = useState<VLMHealth>(DEFAULT_VLM_HEALTH);
  const [vlmResult, setVlmResult] = useState<VLMResult>(DEFAULT_VLM_RESULT);
  const [vlmReducerDecisions, setVlmReducerDecisions] = useState<VLMReducerDecision[]>([]);
  const [vlmHealthReceivedAt, setVlmHealthReceivedAt] = useState<number | null>(null);
  const [vlmResultReceivedAt, setVlmResultReceivedAt] = useState<number | null>(null);
  const [events, setEvents] = useState<SimulationEvent[]>([]);
  const [actionPending, setActionPending] = useState("");
  const [actionMessage, setActionMessage] = useState("Ready.");
  const [overrideAck, setOverrideAck] = useState<OverrideAck | null>(null);

  const rosRef = useRef<unknown>(null);
  const simulationStateRef = useRef<SimulationState>(DEFAULT_STATE);
  const reconnectTimerRef = useRef<number | null>(null);
  const bundleDirtyRef = useRef(false);
  const eventSequenceRef = useRef(0);
  const actionRunIdRef = useRef(0);

  const activeBundle = simulationState.active_bundle || bundle;

  useEffect(() => {
    let disposed = false;
    const ros = new ROSLIB.Ros({ url });
    const scheduleReconnect = () => {
      if (disposed || reconnectTimerRef.current !== null) return;
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null;
        if (!disposed) {
          ros.connect(url);
        }
      }, 1500);
    };

    ros.on("connection", () => {
      setConnected(true);
      setActionMessage("ROS bridge connected.");
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    });
    ros.on("close", () => {
      setConnected(false);
      setActionMessage("ROS bridge disconnected. Reconnecting...");
      scheduleReconnect();
    });
    ros.on("error", () => {
      setConnected(false);
      setActionMessage("ROS bridge error. Retrying connection...");
      scheduleReconnect();
    });

    const simulationTopic = new ROSLIB.Topic({
      ros,
      name: "/simulation/state",
      messageType: "surgical_msgs/msg/SimulationState",
    });
    const eventTopic = new ROSLIB.Topic({
      ros,
      name: "/simulation/event",
      messageType: "surgical_msgs/msg/SimulationEvent",
    });
    const surgeonTopic = new ROSLIB.Topic({
      ros,
      name: "/surgeon/state",
      messageType: "surgical_msgs/msg/SurgeonState",
    });
    const btDecisionTopic = new ROSLIB.Topic({
      ros,
      name: "/bt/decision",
      messageType: "surgical_msgs/msg/BTDecision",
    });
    const skillStatusTopic = new ROSLIB.Topic({
      ros,
      name: "/skill/status",
      messageType: "surgical_msgs/msg/SkillStatus",
    });
    const vlmHealthTopic = new ROSLIB.Topic({
      ros,
      name: "/vlm/health",
      messageType: "surgical_msgs/msg/VLMHealth",
    });
    const vlmResultTopic = new ROSLIB.Topic({
      ros,
      name: "/vlm/result",
      messageType: "surgical_msgs/msg/VLMResult",
    });
    const vlmReducerTopic = new ROSLIB.Topic({
      ros,
      name: "/vlm/reducer_decisions",
      messageType: "surgical_msgs/msg/VLMReducerDecision",
    });

    simulationTopic.subscribe((message: unknown) => {
      const nextState = message as SimulationState;
      simulationStateRef.current = nextState;
      setSimulationState(nextState);
    });
    eventTopic.subscribe((message: unknown) => {
      startTransition(() => {
        eventSequenceRef.current += 1;
        const event = message as SimulationEvent;
        setEvents((current) => [
          {
            ...event,
            ui_id: `${Date.now()}-${eventSequenceRef.current}`,
          },
          ...current,
        ].slice(0, 16));
      });
    });
    surgeonTopic.subscribe((message: unknown) => {
      startTransition(() => {
        setSurgeonState(message as SurgeonState);
      });
    });
    btDecisionTopic.subscribe((message: unknown) => {
      startTransition(() => {
        setBtDecision(message as BTDecision);
      });
    });
    skillStatusTopic.subscribe((message: unknown) => {
      startTransition(() => {
        setSkillStatus(message as SkillStatus);
      });
    });
    vlmHealthTopic.subscribe((message: unknown) => {
      startTransition(() => {
        setVlmHealth(message as VLMHealth);
        setVlmHealthReceivedAt(Date.now());
      });
    });
    vlmResultTopic.subscribe((message: unknown) => {
      startTransition(() => {
        setVlmResult(message as VLMResult);
        setVlmResultReceivedAt(Date.now());
      });
    });
    vlmReducerTopic.subscribe((message: unknown) => {
      startTransition(() => {
        setVlmReducerDecisions((current) => [message as VLMReducerDecision, ...current].slice(0, 8));
      });
    });

    rosRef.current = ros;

    return () => {
      disposed = true;
      simulationTopic.unsubscribe();
      eventTopic.unsubscribe();
      surgeonTopic.unsubscribe();
      btDecisionTopic.unsubscribe();
      skillStatusTopic.unsubscribe();
      vlmHealthTopic.unsubscribe();
      vlmResultTopic.unsubscribe();
      vlmReducerTopic.unsubscribe();
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      ros.close();
      rosRef.current = null;
    };
  }, [url]);

  useEffect(() => {
    simulationStateRef.current = simulationState;
  }, [simulationState]);

  useEffect(() => {
    if (!bundleDirtyRef.current && simulationState.active_bundle && bundle !== simulationState.active_bundle) {
      setBundle(simulationState.active_bundle);
    }
  }, [simulationState.active_bundle, bundle]);

  useEffect(() => {
    if (simulationState.execution_state !== "idle" || simulationState.running) return;
    setEvents([]);
    setSurgeonState({
      ...DEFAULT_SURGEON,
      procedure_id: simulationState.active_bundle || bundle,
      phase_id: simulationState.filtered_phase,
    });
  }, [simulationState.execution_state, simulationState.running, simulationState.active_bundle, simulationState.filtered_phase, bundle]);

  useEffect(() => {
    setOverrideAck(null);
  }, [activeBundle]);

  function setBundleSelection(nextBundle: string) {
    bundleDirtyRef.current = true;
    setBundle(nextBundle);
  }

  async function callService(
    name: string,
    serviceType: string,
    request: Record<string, unknown>,
    timeoutMs = 20000,
  ) {
    if (!rosRef.current || !connected) {
      throw new Error("ROS bridge is offline.");
    }
    const service = new ROSLIB.Service({
      ros: rosRef.current as never,
      name,
      serviceType,
    });
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        reject(new Error(`Timed out waiting for service response from ${name}`));
      }, timeoutMs);
      service.callService(
        new ROSLIB.ServiceRequest(request),
        (response: Record<string, unknown>) => {
          window.clearTimeout(timeout);
          resolve(response);
        },
        (error: unknown) => {
          window.clearTimeout(timeout);
          reject(error instanceof Error ? error : new Error(String(error)));
        },
      );
    });
  }

  async function runAction(label: string, work: () => Promise<void>) {
    const runId = actionRunIdRef.current + 1;
    actionRunIdRef.current = runId;
    setActionPending(label);
    setActionMessage(`${label}...`);
    try {
      await work();
    } catch (error) {
      setActionMessage(error instanceof Error ? error.message : String(error));
    } finally {
      if (actionRunIdRef.current === runId) {
        setActionPending("");
      }
    }
  }

  async function waitForControlTarget(command: ControlCommand, timeoutMs: number) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const current = simulationStateRef.current;
      const reachedTarget =
        (command === "start" && current.running && current.execution_state === "running") ||
        (command === "pause" && current.running && current.execution_state === "paused") ||
        (command === "resume" && current.running && current.execution_state === "running") ||
        (command === "reset" && !current.running && current.execution_state === "idle") ||
        (command === "stop" && !current.running && current.execution_state === "halted");
      if (reachedTarget) {
        return runtimeStatusMessage(current);
      }
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    throw new Error(
      command === "start"
        ? "Start was accepted, but the runtime did not reach running state."
        : command === "pause"
          ? "Pause was accepted, but the runtime did not reach paused state."
          : command === "resume"
            ? "Resume was accepted, but the runtime did not reach running state."
            : command === "reset"
              ? "Reset was accepted, but the runtime did not reach idle state."
              : "Stop was accepted, but the runtime did not reach halted state.",
    );
  }

  async function applyBundle() {
    await runAction("Applying bundle", async () => {
      const response = await callService(
        "/simulation/select_bundle",
        "surgical_msgs/srv/SelectSimulationBundle",
        {
          bundle_name: bundle,
          restart_if_running: simulationState.running,
        },
        simulationState.running ? 22000 : 12000,
      );
      const success = response.success === undefined ? true : Boolean(response.success);
      if (!success) {
        throw new Error(String(response.message || `Failed to apply ${bundle}.`));
      }
      bundleDirtyRef.current = false;
      setOverrideAck(null);
      setSimulationState((current) => ({
        ...current,
        active_bundle: String(response.active_bundle || bundle),
        procedure_id: String(response.active_bundle || bundle),
      }));
      setActionMessage(String(response.message || `Bundle switched to ${bundle}.`));
    });
  }

  async function control(command: ControlCommand) {
    const label =
      command === "start"
        ? "Starting simulation"
        : command === "pause"
          ? "Pausing simulation"
          : command === "resume"
            ? "Resuming simulation"
            : command === "stop"
              ? "Stopping simulation"
              : "Resetting simulation";
    await runAction(label, async () => {
      try {
        const response = await callService(
          "/simulation/control",
          "surgical_msgs/srv/ControlSimulation",
          { command },
          command === "start" ? 45000 : command === "reset" ? 30000 : 20000,
        );
        const success = response.success === undefined ? true : Boolean(response.success);
        if (!success) {
          throw new Error(String(response.message || `${label} failed.`));
        }
        setOverrideAck(null);
        if (command === "reset") {
          setEvents([]);
          setSurgeonState({
            ...DEFAULT_SURGEON,
            procedure_id: simulationState.active_bundle || bundle,
            phase_id: simulationState.filtered_phase,
          });
        }
        const fallbackMessage =
          command === "start"
            ? "simulation started"
            : command === "pause"
              ? "simulation paused"
              : command === "resume"
                ? "simulation resumed"
                : command === "stop"
                  ? "simulation stopped"
                  : "simulation runtime reset to idle";
        const rawMessage = String(response.message || fallbackMessage);
        setActionMessage(rawMessage === "ok" ? fallbackMessage : rawMessage);
        if (rawMessage.endsWith("requested") && command !== "start") {
          const stableMessage = await waitForControlTarget(
            command,
            command === "reset" ? 30000 : 20000,
          );
          if (stableMessage) {
            setActionMessage(stableMessage);
          }
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (
          message.includes("Timed out waiting for service response") ||
          message.includes("Timeout exceeded while waiting for service response")
        ) {
          try {
            const stableMessage = await waitForControlTarget(
              command,
              command === "start" ? 45000 : command === "reset" ? 30000 : 20000,
            );
            if (command === "reset") {
              setOverrideAck(null);
              setEvents([]);
              const current = simulationStateRef.current;
              setSurgeonState({
                ...DEFAULT_SURGEON,
                procedure_id: current.active_bundle || bundle,
                phase_id: current.filtered_phase,
              });
            }
            if (stableMessage) {
              setActionMessage(stableMessage);
              return;
            }
          } catch {
            // Fall through to the original timeout error if the state topic never reaches the target.
          }
        }
        throw error;
      }
    });
  }

  async function sendOverride(payload: OverridePayload) {
    await runAction(payload.eventType === "voice_request" ? "Sending voice override" : "Sending surgeon override", async () => {
      const response = await callService(
        "/simulation/inject_surgeon_override",
        "surgical_msgs/srv/InjectSurgeonOverride",
        {
          event_type: payload.eventType,
          requested_tool: payload.requestedTool,
          voice_text: payload.eventType === "voice_request" ? payload.voiceText : "",
          ready_for_handover: payload.eventType !== "return_tool",
          ready_for_retrieval: payload.eventType === "return_tool",
          clear_pending_requests: true,
        },
        12000,
      );
      const success = response.success === undefined ? true : Boolean(response.success);
      if (!success) {
        throw new Error(String(response.message || "Override request failed."));
      }
      const message =
        payload.eventType === "return_tool"
          ? `${payload.toolLabel} return/recovery transaction requested.`
          : payload.eventType === "voice_request"
            ? `${payload.toolLabel} voice handover requested.`
            : `${payload.toolLabel} handover requested.`;
      setOverrideAck({
        eventType: payload.eventType,
        toolId: payload.requestedTool,
        message,
        voiceText: payload.eventType === "voice_request" ? payload.voiceText : "",
      });
      setActionMessage(String(response.message || "Override accepted."));
    });
  }

  const runtimeMessage = runtimeStatusMessage(simulationState);
  const simulationReady = connected && simulationState.instrument_states.length > 0;
  const shouldPreferRuntimeMessage =
    !actionPending && Boolean(runtimeMessage) && (actionMessage === "Ready." || actionMessage === "ROS bridge connected.");
  const displayActionMessage = shouldPreferRuntimeMessage ? runtimeMessage : actionMessage;

  return {
    url,
    setUrl,
    connected,
    bundle,
    setBundleSelection,
    activeBundle,
    simulationState,
    surgeonState,
    btDecision,
    skillStatus,
    vlmHealth,
    vlmResult,
    vlmReducerDecisions,
    vlmHealthReceivedAt,
    vlmResultReceivedAt,
    events,
    actionPending,
    actionMessage: displayActionMessage,
    runtimeMessage,
    simulationReady,
    overrideAck,
    applyBundle,
    control,
    sendOverride,
  };
}

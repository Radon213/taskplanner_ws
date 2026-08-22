import { Ros, Topic } from "roslib-monitor";

const ROSLIB = { Ros, Topic };

const noOp = () => {};

function errorMessage(error) {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  return "rosbridge connection error";
}

function normalizeReconnectPolicy(policy = {}) {
  return {
    initialDelayMs: Math.max(100, Number(policy.initialDelayMs) || 1000),
    maxDelayMs: Math.max(1000, Number(policy.maxDelayMs) || 15000),
    multiplier: Math.max(1, Number(policy.multiplier) || 1.8),
    jitterRatio: Math.min(0.5, Math.max(0, Number(policy.jitterRatio) || 0)),
  };
}

export class RosBridgeClient {
  constructor({
    url,
    subscriptions = [],
    connectTimeoutMs = 8000,
    reconnect,
    onMessage = noOp,
    onStatus = noOp,
    roslib = ROSLIB,
    random = Math.random,
  } = {}) {
    if (!url) throw new Error("rosbridge WebSocket URL is required");
    this.url = String(url);
    this.subscriptionDefinitions = subscriptions.map((definition) => ({ ...definition }));
    this.connectTimeoutMs = Math.max(0, Number(connectTimeoutMs) || 0);
    this.reconnect = normalizeReconnectPolicy(reconnect);
    this.onMessage = onMessage;
    this.onStatus = onStatus;
    this.roslib = roslib;
    this.random = random;

    this.ros = null;
    this.topicBindings = [];
    this.started = false;
    this.stopped = false;
    this.connecting = false;
    this.retryAttempt = 0;
    this.retryTimer = null;
    this.connectTimer = null;
    this.pendingReconnectReason = "";
    this.forceReconnectPending = false;
    this.lastStatus = null;

    this.handleConnection = this.handleConnection.bind(this);
    this.handleClose = this.handleClose.bind(this);
    this.handleError = this.handleError.bind(this);
    this.ros = this.createRosTransport();
  }

  createRosTransport() {
    const ros = new this.roslib.Ros();
    ros.on("connection", this.handleConnection);
    ros.on("close", this.handleClose);
    ros.on("error", this.handleError);
    return ros;
  }

  report(state, detail = {}) {
    const status = {
      state,
      url: this.url,
      retryAttempt: this.retryAttempt,
      ...detail,
    };
    this.lastStatus = status;
    this.onStatus(status);
  }

  createTopics() {
    if (this.topicBindings.length) return;
    const transport = this.ros;
    this.topicBindings = this.subscriptionDefinitions.map((definition) => {
      const topicName = definition.name || definition.topic;
      const messageType = definition.messageType || definition.type;
      if (!topicName || !messageType) {
        throw new Error("Each ROS subscription requires a name and messageType");
      }
      const topic = new this.roslib.Topic({
        ros: this.ros,
        name: topicName,
        messageType,
        compression: definition.compression || "none",
        throttle_rate: Math.max(0, Number(definition.throttle_rate) || 0),
        queue_length: Math.max(0, Number(definition.queue_length) || 0),
        reconnect_on_close: true,
      });
      const callback = (message) => {
        // A callback can already be queued when a silent/half-open transport is
        // retired. Never let that retired socket reset backoff or publish stale
        // data into the active app session.
        if (this.stopped || this.ros !== transport) return;
        this.noteActivity();
        this.onMessage(topicName, message, definition);
      };
      topic.subscribe(callback);
      return { topic, callback };
    });
  }

  start() {
    if (this.stopped) throw new Error("A stopped RosBridgeClient cannot be restarted");
    if (this.started) return this;
    this.started = true;
    this.connect(false);
    return this;
  }

  connect(isRetry) {
    if (this.stopped || this.connecting || this.ros.isConnected) return;
    this.clearRetryTimer();
    this.clearConnectTimer();
    this.connecting = true;
    this.report(isRetry ? "reconnecting" : "connecting");

    if (this.connectTimeoutMs > 0) {
      this.connectTimer = setTimeout(() => {
        if (this.stopped || this.ros.isConnected) return;
        this.connecting = false;
        this.report("error", { error: "connection timeout" });
        try {
          this.ros.close();
        } catch {
          // A socket may not exist yet. The reconnect timer below is authoritative.
        }
        this.scheduleReconnect();
      }, this.connectTimeoutMs);
    }

    try {
      this.ros.connect(this.url);
    } catch (error) {
      this.connecting = false;
      this.clearConnectTimer();
      this.report("error", { error: errorMessage(error) });
      this.scheduleReconnect();
    }
  }

  handleConnection() {
    if (this.stopped) return;
    this.connecting = false;
    this.forceReconnectPending = false;
    this.pendingReconnectReason = "";
    this.clearConnectTimer();
    this.clearRetryTimer();
    // Subscribe only after the first successful socket connection. In roslib
    // 1.4.1, subscribing while disconnected queues a frame; a failed first
    // connection then queues the same frame again from reconnect_on_close.
    // Creating the topics here prevents that duplicate while still letting
    // Topic handle exactly one resubscribe on later reconnects.
    this.createTopics();
    this.report("connected");
  }

  noteActivity() {
    if (this.stopped) return;
    this.retryAttempt = 0;
    this.forceReconnectPending = false;
  }

  handleError(error) {
    if (this.stopped) return;
    this.report("error", { error: errorMessage(error) });
    // Browser WebSocket emits close after error. Reconnect only from close or timeout.
  }

  handleClose(event = {}) {
    if (this.stopped) return;
    this.connecting = false;
    this.clearConnectTimer();
    this.report("closed", {
      closeCode: Number.isInteger(event?.code) ? event.code : null,
      closeReason: typeof event?.reason === "string" ? event.reason.slice(0, 160) : "",
      wasClean: event?.wasClean === true,
    });
    this.scheduleReconnect();
  }

  forceReconnect(reason = "forced_reconnect") {
    if (
      this.stopped
      || this.forceReconnectPending
      || this.retryTimer
      || this.connecting
    ) return false;
    this.forceReconnectPending = true;
    this.connecting = false;
    this.clearConnectTimer();
    this.pendingReconnectReason = String(reason || "forced_reconnect");

    const previousRos = this.ros;
    this.disposeTopics();
    this.removeRosListener("connection", this.handleConnection, previousRos);
    this.removeRosListener("close", this.handleClose, previousRos);
    this.removeRosListener("error", this.handleError, previousRos);
    try {
      previousRos.close();
    } catch {
      // Retiring an already-broken transport remains best-effort.
    }
    if (typeof previousRos.removeAllListeners === "function") previousRos.removeAllListeners();
    this.ros = this.createRosTransport();
    this.scheduleReconnect();
    return true;
  }

  scheduleReconnect() {
    if (this.stopped || this.retryTimer) return;
    const { initialDelayMs, maxDelayMs, multiplier, jitterRatio } = this.reconnect;
    const baseDelay = Math.min(maxDelayMs, initialDelayMs * multiplier ** this.retryAttempt);
    const jitter = baseDelay * jitterRatio * (this.random() * 2 - 1);
    const delayMs = Math.max(100, Math.round(baseDelay + jitter));
    const reason = this.pendingReconnectReason;
    this.pendingReconnectReason = "";
    this.retryAttempt += 1;
    this.report("reconnecting", {
      nextRetryMs: delayMs,
      ...(reason ? { reason } : {}),
    });
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect(true);
    }, delayMs);
  }

  clearRetryTimer() {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  clearConnectTimer() {
    if (this.connectTimer) clearTimeout(this.connectTimer);
    this.connectTimer = null;
  }

  removeRosListener(event, callback, ros = this.ros) {
    if (typeof ros?.off === "function") ros.off(event, callback);
    else if (typeof ros?.removeListener === "function") ros.removeListener(event, callback);
  }

  disposeTopics() {
    this.topicBindings.forEach(({ topic, callback }) => {
      try {
        topic.unsubscribe(callback);
      } catch {
        // Cleanup remains best-effort when the transport has already failed.
      }
    });
    this.topicBindings = [];
  }

  stop() {
    if (this.stopped) return;
    this.stopped = true;
    this.pendingReconnectReason = "";
    this.forceReconnectPending = false;
    this.connecting = false;
    this.clearRetryTimer();
    this.clearConnectTimer();

    this.disposeTopics();

    this.removeRosListener("connection", this.handleConnection);
    this.removeRosListener("close", this.handleClose);
    this.removeRosListener("error", this.handleError);
    try {
      this.ros.close();
    } catch {
      // No active socket is a valid stopped state.
    }
    // Topic.unsubscribe() queues an unsubscribe while offline, and a previous
    // reconnect may already have queued a subscribe. This client is one-shot,
    // so discard every late transport callback after stop.
    if (typeof this.ros.removeAllListeners === "function") this.ros.removeAllListeners();
    this.report("stopped");
  }
}

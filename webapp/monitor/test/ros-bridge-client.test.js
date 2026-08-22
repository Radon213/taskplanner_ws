import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { Topic } from "roslib-monitor";

import { RosBridgeClient } from "../ros/ros-bridge-client.js";

class FakeRos extends EventEmitter {
  static instances = [];

  constructor() {
    super();
    this.isConnected = false;
    this.connectCalls = [];
    this.closeCalls = 0;
    FakeRos.instances.push(this);
  }

  connect(url) {
    this.connectCalls.push(url);
  }

  close() {
    this.closeCalls += 1;
  }
}

class FakeTopic {
  static instances = [];

  constructor(options) {
    this.options = options;
    this.callback = null;
    this.unsubscribedWith = null;
    FakeTopic.instances.push(this);
  }

  subscribe(callback) {
    this.callback = callback;
  }

  unsubscribe(callback) {
    this.unsubscribedWith = callback;
  }
}

const fakeRoslib = { Ros: FakeRos, Topic: FakeTopic };

test("client creates each read-only topic once and cleans up the same callbacks", () => {
  FakeTopic.instances = [];
  const messages = [];
  const statuses = [];
  const client = new RosBridgeClient({
    url: "ws://127.0.0.1:9092",
    subscriptions: [
      { name: "/surgery/context", messageType: "surgical_interop_msgs/msg/SurgeryContext" },
      { name: "/surgery/speech", messageType: "surgical_interop_msgs/msg/SpeechRecognitionState" },
    ],
    onMessage: (...args) => messages.push(args),
    onStatus: (status) => statuses.push(status.state),
    roslib: fakeRoslib,
  });

  client.start();
  assert.deepEqual(client.ros.connectCalls, ["ws://127.0.0.1:9092"]);
  assert.equal(FakeTopic.instances.length, 0);
  client.ros.isConnected = true;
  client.ros.emit("connection");
  assert.equal(FakeTopic.instances.length, 2);
  FakeTopic.instances[0].callback({ current_phase: "P03" });
  assert.equal(messages[0][0], "/surgery/context");
  assert.equal(statuses.includes("connected"), true);

  const callbacks = FakeTopic.instances.map((topic) => topic.callback);
  client.stop();
  FakeTopic.instances.forEach((topic, index) => {
    assert.equal(topic.unsubscribedWith, callbacks[index]);
  });
  assert.equal(client.ros.closeCalls, 1);
});

class WireSpyRos extends EventEmitter {
  constructor() {
    super();
    this.isConnected = false;
    this.idCounter = 0;
    this.connectCalls = [];
    this.closeCalls = 0;
    this.frames = [];
  }

  connect(url) {
    this.connectCalls.push(url);
  }

  close() {
    this.closeCalls += 1;
  }

  callOnConnection(message) {
    const send = () => this.frames.push(structuredClone(message));
    if (this.isConnected) send();
    else this.once("connection", send);
  }
}

test("a failed first connection and later reconnect each send one subscribe frame", () => {
  const client = new RosBridgeClient({
    url: "ws://127.0.0.1:9092",
    subscriptions: [
      { name: "/surgery/context", messageType: "surgical_interop_msgs/msg/SurgeryContext" },
    ],
    reconnect: { initialDelayMs: 100, maxDelayMs: 1000, multiplier: 1, jitterRatio: 0 },
    roslib: { Ros: WireSpyRos, Topic },
  });

  client.start();
  client.ros.emit("close");
  assert.equal(client.topicBindings.length, 0);
  assert.equal(client.ros.frames.length, 0);

  client.ros.isConnected = true;
  client.ros.emit("connection");
  assert.equal(client.topicBindings.length, 1);
  assert.equal(client.ros.frames.filter((frame) => frame.op === "subscribe").length, 1);

  client.ros.isConnected = false;
  client.ros.emit("close");
  client.ros.isConnected = true;
  client.ros.emit("connection");
  assert.equal(client.topicBindings.length, 1);
  assert.equal(client.ros.frames.filter((frame) => frame.op === "subscribe").length, 2);

  client.ros.isConnected = false;
  client.ros.emit("close");
  client.stop();
  const frameCountAtStop = client.ros.frames.length;
  assert.equal(client.ros.listenerCount("connection"), 0);
  client.ros.isConnected = true;
  client.ros.emit("connection");
  assert.equal(client.ros.frames.length, frameCountAtStop);
});

test("stopping during backoff prevents a reconnect", async () => {
  FakeTopic.instances = [];
  const client = new RosBridgeClient({
    url: "ws://127.0.0.1:9092",
    subscriptions: [],
    reconnect: { initialDelayMs: 100, maxDelayMs: 1000, multiplier: 1, jitterRatio: 0 },
    roslib: fakeRoslib,
    random: () => 0.5,
  });
  client.start();
  client.ros.emit("close");
  client.stop();
  await new Promise((resolve) => setTimeout(resolve, 130));
  assert.equal(client.ros.connectCalls.length, 1);
});

test("connection timeout and retry states expose structured diagnostics", async () => {
  FakeTopic.instances = [];
  const statuses = [];
  const client = new RosBridgeClient({
    url: "ws://127.0.0.1:9092",
    subscriptions: [],
    connectTimeoutMs: 20,
    reconnect: { initialDelayMs: 100, maxDelayMs: 1000, multiplier: 1, jitterRatio: 0 },
    onStatus: (status) => statuses.push(structuredClone(status)),
    roslib: fakeRoslib,
  });

  client.start();
  await new Promise((resolve) => setTimeout(resolve, 45));

  assert.deepEqual(statuses.map(({ state }) => state), [
    "connecting",
    "error",
    "reconnecting",
  ]);
  assert.deepEqual(statuses[1], {
    state: "error",
    url: "ws://127.0.0.1:9092",
    retryAttempt: 0,
    error: "connection timeout",
  });
  assert.equal(statuses[2].nextRetryMs, 100);
  assert.equal(statuses[2].retryAttempt, 1);
  assert.equal(client.ros.closeCalls, 1);

  client.stop();
  assert.equal(statuses.at(-1).state, "stopped");
});

test("transport errors report only the safe error summary", () => {
  const statuses = [];
  const client = new RosBridgeClient({
    url: "wss://bridge.example.test/",
    subscriptions: [],
    connectTimeoutMs: 0,
    onStatus: (status) => statuses.push(structuredClone(status)),
    roslib: fakeRoslib,
  });

  client.start();
  const transportError = Object.assign(new Error("TLS handshake failed"), {
    payload: { token: "must-not-be-logged" },
    data: "raw-wire-frame",
  });
  client.ros.emit("error", transportError);

  assert.deepEqual(statuses.at(-1), {
    state: "error",
    url: "wss://bridge.example.test/",
    retryAttempt: 0,
    error: "TLS handshake failed",
  });
  assert.doesNotMatch(JSON.stringify(statuses), /must-not-be-logged|raw-wire-frame/);
  client.stop();
});

test("socket close metadata is reported before reconnect backoff", () => {
  const statuses = [];
  const client = new RosBridgeClient({
    url: "wss://bridge.example.test/",
    subscriptions: [],
    connectTimeoutMs: 0,
    reconnect: { initialDelayMs: 100, maxDelayMs: 1000, multiplier: 1, jitterRatio: 0 },
    onStatus: (status) => statuses.push(structuredClone(status)),
    roslib: fakeRoslib,
  });

  client.start();
  client.ros.emit("close", {
    code: 1006,
    reason: "upstream gateway unavailable",
    wasClean: false,
  });

  assert.deepEqual(statuses.map(({ state }) => state), [
    "connecting",
    "closed",
    "reconnecting",
  ]);
  assert.deepEqual(statuses[1], {
    state: "closed",
    url: "wss://bridge.example.test/",
    retryAttempt: 0,
    closeCode: 1006,
    closeReason: "upstream gateway unavailable",
    wasClean: false,
  });
  assert.equal(statuses[2].nextRetryMs, 100);
  assert.equal(statuses[2].retryAttempt, 1);
  client.stop();
});

test("forced reconnect retires the old transport and escalates until first topic activity", async () => {
  FakeRos.instances = [];
  FakeTopic.instances = [];
  const statuses = [];
  const messages = [];
  const client = new RosBridgeClient({
    url: "wss://bridge.example.test/",
    subscriptions: [
      { name: "/surgery/context", messageType: "surgical_interop_msgs/msg/SurgeryContext" },
    ],
    connectTimeoutMs: 0,
    reconnect: { initialDelayMs: 100, maxDelayMs: 1000, multiplier: 2, jitterRatio: 0 },
    onMessage: (...args) => messages.push(args),
    onStatus: (status) => statuses.push(structuredClone(status)),
    roslib: fakeRoslib,
  });

  client.start();
  const firstRos = client.ros;
  firstRos.isConnected = true;
  firstRos.emit("connection");
  const firstTopic = client.topicBindings[0];
  assert.ok(firstTopic);

  assert.equal(client.forceReconnect("all_topics_silent"), true);
  const secondRos = client.ros;
  assert.notStrictEqual(secondRos, firstRos, "forced recovery must use a fresh Ros transport");
  assert.equal(firstRos.closeCalls, 1);
  assert.equal(firstTopic.topic.unsubscribedWith, firstTopic.callback);
  assert.equal(firstRos.listenerCount("connection"), 0);
  assert.equal(firstRos.listenerCount("close"), 0);
  assert.equal(firstRos.listenerCount("error"), 0);
  assert.equal(client.topicBindings.length, 0, "the retired transport cannot retain old Topic bindings");
  assert.equal(client.retryAttempt, 1);
  assert.ok(client.retryTimer, "forced reconnect must use the normal one-shot backoff timer");
  assert.deepEqual(statuses.at(-1), {
    state: "reconnecting",
    url: "wss://bridge.example.test/",
    retryAttempt: 1,
    nextRetryMs: 100,
    reason: "all_topics_silent",
  });

  firstTopic.callback({ current_phase: "STALE_RETIRED_PAYLOAD" });
  assert.equal(messages.length, 0, "a queued callback from the retired transport must not reach the app");
  assert.equal(
    client.retryAttempt,
    1,
    "retired traffic must not reset the active transport's reconnect backoff",
  );

  const firstRetryTimer = client.retryTimer;
  assert.equal(client.forceReconnect("all_topics_silent"), false);
  assert.strictEqual(client.retryTimer, firstRetryTimer, "a duplicate force request must keep the existing timer");
  assert.strictEqual(client.ros, secondRos, "a duplicate force request must not allocate another Ros transport");
  assert.equal(client.retryAttempt, 1);
  assert.equal(firstRos.closeCalls, 1);

  await new Promise((resolve) => setTimeout(resolve, 130));
  assert.deepEqual(secondRos.connectCalls, ["wss://bridge.example.test/"]);
  secondRos.isConnected = true;
  secondRos.emit("connection");
  assert.equal(client.retryAttempt, 1, "socket-open alone must not reset failure history");
  assert.equal(client.topicBindings.length, 1);

  assert.equal(client.forceReconnect("all_topics_silent"), true);
  const thirdRos = client.ros;
  assert.notStrictEqual(thirdRos, secondRos);
  assert.equal(secondRos.closeCalls, 1);
  assert.equal(client.retryAttempt, 2);
  assert.deepEqual(statuses.at(-1), {
    state: "reconnecting",
    url: "wss://bridge.example.test/",
    retryAttempt: 2,
    nextRetryMs: 200,
    reason: "all_topics_silent",
  });

  await new Promise((resolve) => setTimeout(resolve, 230));
  assert.deepEqual(thirdRos.connectCalls, ["wss://bridge.example.test/"]);
  thirdRos.isConnected = true;
  thirdRos.emit("connection");
  assert.equal(client.retryAttempt, 2);
  const activeTopic = client.topicBindings[0];
  activeTopic.topic.callback({ current_phase: "P03" });
  assert.equal(messages.length, 1);
  assert.equal(client.retryAttempt, 0, "the first subscribed-topic callback proves recovery");

  const activeRos = client.ros;
  client.stop();
  assert.equal(client.forceReconnect("all_topics_silent"), false);
  assert.equal(activeRos.closeCalls, 1, "a stopped client must not close the transport again");
});

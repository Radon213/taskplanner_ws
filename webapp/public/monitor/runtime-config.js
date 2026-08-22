(function configureSurgiMate(global) {
  const location = global.location || {};
  const hostname = String(location.hostname || "127.0.0.1");
  const websocketHost = hostname.includes(":") && !hostname.startsWith("[")
    ? `[${hostname}]`
    : hostname;
  const websocketScheme = location.protocol === "https:" ? "wss" : "ws";
  const queryMode = new URLSearchParams(String(location.search || "")).get("mode");

  global.SURGIMATE_CONFIG = Object.freeze({
    // The monitor honors an explicit ?mode=dummy without creating a WebSocket.
    mode: queryMode === "dummy" ? "dummy" : "ros",
    dummyDataFile: "/monitor/dummy-data.json",
    rosbridge: Object.freeze({
      // Use the same browser-visible host and the reviewed subscribe-only port.
      // Brackets are required when location.hostname is an IPv6 literal.
      url: `${websocketScheme}://${websocketHost}:9092`,
      gatewayStaleAfterMs: 3000,
      // Any subscribed public topic proves the rosbridge data path is alive.
      // Recycle the socket only when every topic is silent for this long.
      topicSilenceTimeoutMs: 3000,
      connectTimeoutMs: 8000,
      reconnect: Object.freeze({
        initialDelayMs: 1000,
        maxDelayMs: 15000,
        multiplier: 1.8,
        jitterRatio: 0.2,
      }),
      cameraStreams: Object.freeze({
        enabled: true,
        throttleRateMs: 100,
        fit: "contain",
        playoutMode: "latest",
      }),
    }),
  });
})(window);

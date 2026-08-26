import { createHash } from "node:crypto";
import { closeSync, createReadStream, fstatSync, openSync, readFileSync, statSync } from "node:fs";
import { createServer, request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { brotliCompress, constants as zlibConstants, gzip } from "node:zlib";

// Keep the production server rooted at `webapp/dist`.  Resolving `..` from
// this module URL already yields `webapp/`; applying another `../dist` made
// the server look for the host workspace's unrelated `dist/` directory.
const scriptRoot = fileURLToPath(new URL(".", import.meta.url));
export const DEFAULT_DIST_ROOT = resolve(scriptRoot, "../dist");
const HASHED_ASSET_RE = /(?:^|\/)[^/]+-[A-Za-z0-9_-]{8,}\.[^/]+$/;
const COMPRESSIBLE_TYPES = new Set([
  "application/javascript",
  "application/json",
  "application/manifest+json",
  "application/vnd.apple.mpegurl",
  "image/svg+xml",
  "text/css",
  "text/html",
  "text/plain",
]);
const MIME_TYPES = Object.freeze({
  ".br": "application/octet-stream",
  ".css": "text/css; charset=utf-8",
  ".gif": "image/gif",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".jpeg": "image/jpeg",
  ".jpg": "image/jpeg",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".m3u8": "application/vnd.apple.mpegurl; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".mp4": "video/mp4",
  ".png": "image/png",
  ".svg": "image/svg+xml; charset=utf-8",
  ".ts": "video/mp2t",
  ".webp": "image/webp",
  ".woff2": "font/woff2",
  ".xml": "application/xml; charset=utf-8",
});

function contentType(pathname) {
  return MIME_TYPES[extname(pathname).toLowerCase()] || "application/octet-stream";
}

export function cacheControlFor(pathname, { media = false } = {}) {
  const normalizedPath = String(pathname || "").toLowerCase();
  if (media) {
    return normalizedPath.endsWith(".m3u8") || normalizedPath.endsWith("health.json")
      ? "no-store"
      : "public, max-age=10, stale-while-revalidate=10";
  }
  if (
    normalizedPath.endsWith(".html")
    || normalizedPath.endsWith("runtime-config.js")
    || normalizedPath.endsWith(".json")
  ) return "no-cache";
  if (HASHED_ASSET_RE.test(pathname)) return "public, max-age=31536000, immutable";
  return "public, max-age=604800, stale-while-revalidate=86400";
}

export function safeResolve(root, requestPath) {
  let decoded;
  try {
    decoded = decodeURIComponent(String(requestPath || "/").split("?")[0]);
  } catch {
    return null;
  }
  if (decoded.includes("\0")) return null;
  const relative = normalize(decoded.replace(/^\/+/, ""));
  if (relative === ".." || relative.startsWith(`..${sep}`)) return null;
  const resolved = resolve(root, relative);
  const resolvedRoot = resolve(root);
  return resolved === resolvedRoot || resolved.startsWith(`${resolvedRoot}${sep}`)
    ? resolved
    : null;
}

function etagFor(stat) {
  return `W/\"${stat.size.toString(16)}-${Math.trunc(stat.mtimeMs).toString(16)}\"`;
}

function commonHeaders() {
  return {
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
  };
}

function sendJson(response, status, payload, extraHeaders = {}) {
  const body = Buffer.from(JSON.stringify(payload));
  response.writeHead(status, {
    ...commonHeaders(),
    "Cache-Control": "no-store",
    "Content-Length": body.length,
    "Content-Type": "application/json; charset=utf-8",
    ...extraHeaders,
  });
  response.end(body);
}

function parseByteRange(value, size) {
  const match = /^bytes=(\d*)-(\d*)$/.exec(String(value || "").trim());
  if (!match) return null;
  let start = match[1] ? Number(match[1]) : null;
  let end = match[2] ? Number(match[2]) : null;
  if (start === null && end === null) return null;
  if (start === null) {
    const suffix = Math.min(size, end);
    start = size - suffix;
    end = size - 1;
  } else {
    end = end === null ? size - 1 : Math.min(end, size - 1);
  }
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || start > end || start >= size) {
    return null;
  }
  return { start, end };
}

function serveFile(request, response, filePath, requestPath, { media = false } = {}) {
  const normalizedRequestPath = String(requestPath || "").toLowerCase();
  // Native HLS clients commonly send `Range: bytes=0-` even though MPEG-TS
  // segments are complete, independently decodable objects. Chrome's media
  // pipeline (and some webOS generations) can reject an otherwise valid TS
  // segment when that full object is wrapped in a 206 response or arrives
  // while the live playlist is being atomically replaced. Read HLS objects
  // from one file descriptor and send one coherent 200 response. Ordinary
  // static assets retain byte ranges and streaming reads.
  const completeHlsObject = media && (
    normalizedRequestPath.endsWith(".m3u8") || normalizedRequestPath.endsWith(".ts")
  );
  let stat;
  let completeHlsBody = null;
  try {
    if (completeHlsObject) {
      const descriptor = openSync(filePath, "r");
      try {
        stat = fstatSync(descriptor);
        if (stat.isFile()) completeHlsBody = readFileSync(descriptor);
      } finally {
        closeSync(descriptor);
      }
    } else {
      stat = statSync(filePath);
    }
  } catch {
    return false;
  }
  if (!stat.isFile()) return false;

  const etag = etagFor(stat);
  const baseHeaders = {
    ...commonHeaders(),
    "Accept-Ranges": completeHlsObject ? "none" : "bytes",
    "Cache-Control": cacheControlFor(requestPath, { media }),
    "Content-Type": contentType(filePath),
    ETag: etag,
    "Last-Modified": stat.mtime.toUTCString(),
  };
  if (request.headers["if-none-match"] === etag) {
    response.writeHead(304, baseHeaders);
    response.end();
    return true;
  }

  const rangeHeader = completeHlsObject ? null : request.headers.range;
  if (rangeHeader) {
    const range = parseByteRange(rangeHeader, stat.size);
    if (!range) {
      response.writeHead(416, { ...baseHeaders, "Content-Range": `bytes */${stat.size}` });
      response.end();
      return true;
    }
    response.writeHead(206, {
      ...baseHeaders,
      "Content-Length": range.end - range.start + 1,
      "Content-Range": `bytes ${range.start}-${range.end}/${stat.size}`,
    });
    if (request.method === "HEAD") response.end();
    else createReadStream(filePath, range).pipe(response);
    return true;
  }

  if (completeHlsBody) {
    response.writeHead(200, { ...baseHeaders, "Content-Length": completeHlsBody.length });
    if (request.method === "HEAD") response.end();
    else response.end(completeHlsBody);
    return true;
  }

  const accepts = String(request.headers["accept-encoding"] || "");
  const mime = String(baseHeaders["Content-Type"]).split(";")[0];
  const canCompress = COMPRESSIBLE_TYPES.has(mime) && stat.size >= 1024 && stat.size <= 2 * 1024 * 1024;
  if (request.method !== "HEAD" && canCompress && /\b(?:br|gzip)\b/.test(accepts)) {
    const raw = readFileSync(filePath);
    const useBrotli = /\bbr\b/.test(accepts);
    const compress = useBrotli
      ? (callback) => brotliCompress(raw, {
          params: { [zlibConstants.BROTLI_PARAM_QUALITY]: 5 },
        }, callback)
      : (callback) => gzip(raw, { level: 6 }, callback);
    compress((error, compressed) => {
      if (error) {
        response.destroy(error);
        return;
      }
      response.writeHead(200, {
        ...baseHeaders,
        "Content-Encoding": useBrotli ? "br" : "gzip",
        "Content-Length": compressed.length,
        Vary: "Accept-Encoding",
      });
      response.end(compressed);
    });
    return true;
  }

  response.writeHead(200, { ...baseHeaders, "Content-Length": stat.size });
  if (request.method === "HEAD") response.end();
  else createReadStream(filePath).pipe(response);
  return true;
}

function runtimeProxyConfig(environment = process.env) {
  const target = String(environment.TASKPLANNER_RUNTIME_CONTROL_URL || "").trim();
  const tokenFile = String(environment.TASKPLANNER_RUNTIME_CONTROL_TOKEN_FILE || "").trim();
  if (!target || !tokenFile) return null;
  let token = "";
  try {
    token = readFileSync(tokenFile, "utf8").trim();
  } catch {
    return null;
  }
  if (!token) return null;
  return { target: new URL(target), token };
}

function proxyRuntime(request, response, config) {
  if (!config) {
    sendJson(response, 503, { error: "runtime_control_unavailable" });
    return;
  }
  const requestUrl = new URL(request.url, "http://taskplanner.local");
  const upstreamPath = requestUrl.pathname.replace(/^\/api\/runtime/, "/v1/runtime") + requestUrl.search;
  const requester = config.target.protocol === "https:" ? httpsRequest : httpRequest;
  const upstream = requester({
    protocol: config.target.protocol,
    hostname: config.target.hostname,
    port: config.target.port,
    method: request.method,
    path: upstreamPath,
    headers: {
      accept: request.headers.accept || "application/json",
      "content-type": request.headers["content-type"] || "application/json",
      ...(request.headers["content-length"]
        ? { "content-length": request.headers["content-length"] }
        : {}),
      ...(request.headers["x-taskplanner-request-id"]
        ? { "x-taskplanner-request-id": request.headers["x-taskplanner-request-id"] }
        : {}),
      "x-taskplanner-runtime-control-token": config.token,
    },
    timeout: 5000,
  }, (upstreamResponse) => {
    response.writeHead(upstreamResponse.statusCode || 502, {
      ...commonHeaders(),
      "Cache-Control": "no-store",
      "Content-Type": upstreamResponse.headers["content-type"] || "application/json; charset=utf-8",
    });
    upstreamResponse.pipe(response);
  });
  upstream.on("timeout", () => upstream.destroy(new Error("runtime control timeout")));
  upstream.on("error", () => {
    if (!response.headersSent) sendJson(response, 502, { error: "runtime_control_unreachable" });
    else response.destroy();
  });
  let received = 0;
  request.on("data", (chunk) => {
    received += chunk.length;
    if (received > 1024 * 1024) {
      upstream.destroy();
      if (!response.headersSent) sendJson(response, 413, { error: "request_too_large" });
      return;
    }
    upstream.write(chunk);
  });
  request.on("end", () => upstream.end());
}

export function createProductionServer({
  distRoot = DEFAULT_DIST_ROOT,
  mediaRoot = process.env.MONITOR_MEDIA_ROOT || "/var/run/taskplanner-monitor-media",
  runtimeProxy = runtimeProxyConfig(),
} = {}) {
  const absoluteDistRoot = resolve(distRoot);
  const absoluteMediaRoot = resolve(mediaRoot);
  return createServer((request, response) => {
    if (!new Set(["GET", "HEAD", "POST"]).has(request.method || "")) {
      sendJson(response, 405, { error: "method_not_allowed" }, { Allow: "GET, HEAD, POST" });
      return;
    }
    const requestUrl = new URL(request.url, "http://taskplanner.local");
    if (requestUrl.pathname === "/healthz") {
      sendJson(response, 200, {
        service: "taskplanner-webapp",
        build: createHash("sha256").update(absoluteDistRoot).digest("hex").slice(0, 12),
      });
      return;
    }
    if (requestUrl.pathname.startsWith("/api/runtime")) {
      proxyRuntime(request, response, runtimeProxy);
      return;
    }
    if (requestUrl.pathname === "/display" || requestUrl.pathname === "/tv") {
      response.writeHead(302, {
        ...commonHeaders(),
        "Cache-Control": "no-store",
        Location: "/monitor/index.html?profile=tv",
      });
      response.end();
      return;
    }
    if (requestUrl.pathname === "/" && /(?:web0s|webos|netcast)/i.test(String(request.headers["user-agent"] || ""))) {
      response.writeHead(302, {
        ...commonHeaders(),
        "Cache-Control": "no-store",
        Location: "/monitor/index.html?profile=tv",
      });
      response.end();
      return;
    }

    if (requestUrl.pathname.startsWith("/media/")) {
      const mediaPath = safeResolve(absoluteMediaRoot, requestUrl.pathname.slice("/media/".length));
      if (mediaPath && serveFile(request, response, mediaPath, requestUrl.pathname, { media: true })) return;
      sendJson(response, 404, { error: "media_not_ready" });
      return;
    }

    let pathname = requestUrl.pathname === "/" ? "/index.html" : requestUrl.pathname;
    let filePath = safeResolve(absoluteDistRoot, pathname);
    if (filePath && serveFile(request, response, filePath, pathname)) return;
    if (!extname(pathname)) {
      pathname = "/index.html";
      filePath = join(absoluteDistRoot, "index.html");
      if (serveFile(request, response, filePath, pathname)) return;
    }
    sendJson(response, 404, { error: "not_found" });
  });
}

function parseArguments(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--host") options.host = argv[index += 1];
    else if (argv[index] === "--port") options.port = Number(argv[index += 1]);
    else if (argv[index] === "--dist") options.distRoot = argv[index += 1];
    else if (argv[index] === "--media") options.mediaRoot = argv[index += 1];
  }
  return options;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const options = parseArguments(process.argv.slice(2));
  const host = options.host || process.env.WEBAPP_HOST || "127.0.0.1";
  const port = options.port || Number(process.env.WEBAPP_PORT) || 4173;
  const server = createProductionServer(options);
  server.listen(port, host, () => {
    process.stdout.write(`Taskplanner production webapp listening on http://${host}:${port}\n`);
  });
  const shutdown = () => server.close(() => process.exit(0));
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

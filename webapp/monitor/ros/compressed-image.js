function mimeTypeFromFormat(format) {
  const normalized = String(format || "jpeg").toLowerCase();
  if (normalized.includes("png")) return "image/png";
  if (normalized.includes("webp")) return "image/webp";
  return "image/jpeg";
}

function toUint8Array(data) {
  if (data instanceof Uint8Array) return data;
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (ArrayBuffer.isView(data)) {
    return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  }
  if (!Array.isArray(data)) return null;
  if (data.some((value) => !Number.isInteger(value) || value < 0 || value > 255)) {
    return null;
  }
  return Uint8Array.from(data);
}

export function normalizeCompressedImage(message) {
  if (!message || typeof message !== "object") return null;
  const mimeType = mimeTypeFromFormat(message.format);

  if (typeof message.data === "string") {
    const data = message.data.trim().replace(/\s+/g, "");
    if (!data) return null;
    if (data.startsWith("data:")) {
      if (!/^data:image\/(?:jpeg|png|webp);base64,[a-z0-9+/]+={0,2}$/i.test(data)) return null;
      return { mimeType, dataUrl: data, bytes: null };
    }
    if (!/^[a-z0-9+/]+={0,2}$/i.test(data)) return null;
    return {
      mimeType,
      dataUrl: `data:${mimeType};base64,${data}`,
      bytes: null,
    };
  }

  const bytes = toUint8Array(message.data);
  if (!bytes?.byteLength) return null;
  return { mimeType, dataUrl: null, bytes };
}

function finiteInteger(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) ? number : null;
}

export function compressedImageTiming(message) {
  if (!message || typeof message !== "object") return null;
  const header = message.header;
  if (!header || typeof header !== "object") return null;
  const stamp = header.stamp;
  if (!stamp || typeof stamp !== "object") return null;
  const seconds = finiteInteger(stamp.sec ?? stamp.secs);
  const nanoseconds = finiteInteger(stamp.nanosec ?? stamp.nsecs);
  if (
    seconds === null
    || nanoseconds === null
    || seconds < 0
    || nanoseconds < 0
    || nanoseconds >= 1_000_000_000
  ) {
    return null;
  }
  return {
    sourceTimestampMs: seconds * 1000 + nanoseconds / 1_000_000,
    seconds,
    nanoseconds,
    frameId: typeof header.frame_id === "string" ? header.frame_id.trim().slice(0, 128) : "",
  };
}

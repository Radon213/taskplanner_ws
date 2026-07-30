"use strict";

const target = new URL("/", window.location.origin);
const current = new URL(window.location.href);
current.searchParams.forEach((value, key) => {
  target.searchParams.set(key, value);
});
target.searchParams.delete("workspace");
target.searchParams.delete("layer");
target.searchParams.delete("review_mode");
target.searchParams.delete("clinical_mode");
window.location.replace(target);

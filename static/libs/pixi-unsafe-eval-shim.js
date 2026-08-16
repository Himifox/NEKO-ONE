/*! pixi-unsafe-eval-shim
 * CSP-safe shim for pixi.js v7.
 *
 * pixi.js's ShaderSystem.systemCheck() probes whether `new Function(...)`
 * is allowed (the `@pixi/unsafe-eval` requirement). Under a strict
 * Content-Security-Policy without `unsafe-eval` the probe throws and the
 * Live2D canvas never initialises, even though pixi v7 only uses eval for
 * this one capability probe.
 *
 * This shim runs BEFORE pixi.min.js and permanently replaces the Function
 * constructor with a static implementation that:
 *   1. never executes a body string (CSP-safe, no eval), and
 *   2. returns a callable that yields a truthy result so pixi's probe
 *      (`bs = new Function(...)(...)`) caches `true` and does not throw.
 *
 * pixi.js v7.4.3 references `new Function` only inside this probe, and the
 * Live2D libraries in this project use no dynamic compilation, so a static
 * replacement is safe. All other globals keep the native Function via the
 * captured reference exposed on `window.PIXIUnsafeEvalShimNative`.
 */
(function () {
  "use strict";
  if (typeof window === "undefined" || typeof window.Function === "undefined") return;

  var nativeFunction = window.Function;

  function StaticFunction() {
    /* eslint-disable-next-line prefer-arrow-callback */
    return function ProbeResult() {
      return true;
    };
  }
  /* Match the constructor surface pixi's probe touches. */
  StaticFunction.prototype = nativeFunction.prototype;
  try {
    StaticFunction.prototype.constructor = StaticFunction;
  } catch (e) { /* ignore */ }

  window.Function = StaticFunction;
  /* Expose the original for any consumer that needs it. */
  window.PIXIUnsafeEvalShimNative = nativeFunction;
})();

(() => {
  "use strict";

  const canvas = document.getElementById("live2d-canvas");
  const placeholder = document.getElementById("avatar-placeholder");
  const stage = canvas?.closest(".stage");
  let app = null;
  let model = null;
  let resizeObserver = null;
  let speaking = false;
  let mouthPhase = 0;
  let soullinkRuntime = null;

  function nowSeconds() {
    return performance.now() / 1000;
  }

  async function initializeSoullink(config) {
    if (!config?.enabled) return;
    const api = globalThis.NekoSoullinkEmotion;
    if (!api?.SoullinkRuntime || !config.profile_url) {
      console.warn("Soullink was enabled but its browser runtime is unavailable");
      return;
    }
    const response = await fetch(config.profile_url, { credentials: "same-origin" });
    if (!response.ok) throw new Error(`Soullink profile: ${response.status}`);
    const profile = await response.json();
    soullinkRuntime = new api.SoullinkRuntime({
      profile,
      motionStyle: api.motionStylePresets?.[config.motion_style] || api.motionStylePresets?.natural,
    });
    soullinkRuntime.triggerIntent({
      emotion: "neutral",
      intensity: 0.3,
      contextTags: ["room-ready"],
    }, nowSeconds());
  }

  function applySoullink(delta) {
    const coreModel = model?.internalModel?.coreModel;
    if (!coreModel?.setParameterValueById || !soullinkRuntime) return false;
    const snapshot = soullinkRuntime.update(nowSeconds(), Math.max(delta / 60, 1 / 240));
    for (const [parameterId, value] of Object.entries(snapshot.live2dParams)) {
      if (Number.isFinite(value)) coreModel.setParameterValueById(parameterId, value);
    }
    return true;
  }

  function showStatus(message, failed = false) {
    if (!placeholder) return;
    placeholder.hidden = false;
    placeholder.dataset.failed = failed ? "true" : "false";
    const detail = placeholder.querySelector("small");
    if (detail) detail.textContent = message;
  }

  function fitModel() {
    if (!model || !stage) return;
    const width = Math.max(stage.clientWidth, 320);
    const height = Math.max(stage.clientHeight, 320);
    // Live2D content is not guaranteed to fill its bbox or sit at the model
    // origin, so measure the real visible bounds and pin their bottom-center
    // to the bottom-center of the stage via pivot.
    const bounds = model.getLocalBounds();
    const scale = Math.min(width / bounds.width, height / bounds.height) * 0.92;
    model.scale.set(scale);
    model.pivot.set(bounds.x + bounds.width / 2, bounds.y + bounds.height);
    model.position.set(width / 2, height);
  }

  async function setEmotion(emotion = "neutral") {
    if (!model) return false;
    const supported = new Set(["neutral", "happy", "sad", "angry", "surprised"]);
    const normalized = supported.has(emotion) ? emotion : "neutral";
    try {
      if (soullinkRuntime) {
        soullinkRuntime.triggerIntent({
          emotion: normalized,
          intensity: normalized === "neutral" ? 0.3 : 0.78,
          contextTags: ["assistant-reply"],
        }, nowSeconds());
      }
      const groups = model.internalModel?.settings?.motions || {};
      if (groups[normalized]?.length) {
        await model.motion(normalized);
      }
      return true;
    } catch (error) {
      console.warn("Live2D emotion failed", error);
      return false;
    }
  }

  async function initialize() {
    if (!canvas || !stage) {
      return;
    }
    try {
      const response = await fetch("/api/v1/avatar", { credentials: "same-origin" });
      if (!response.ok) throw new Error(`avatar manifest: ${response.status}`);
      const manifest = await response.json();
      if (!manifest.enabled) {
        const invalid = manifest.status === "invalid_configuration";
        const missing = manifest.status === "missing_model";
        const invalidModel = manifest.status === "invalid_model";
        showStatus(
          invalid
            ? "Live2D 配置无效"
            : invalidModel
              ? "Live2D 模型文件不完整"
            : missing
              ? "找不到已配置的 Live2D 模型"
              : "尚未配置已授权的 Live2D 模型",
          invalid || missing || invalidModel,
        );
        return;
      }
      if (!globalThis.PIXI?.live2d?.Live2DModel) {
        showStatus("Live2D 运行库不可用", true);
        return;
      }

      app = new PIXI.Application({
        view: canvas,
        autoStart: true,
        resizeTo: stage,
        resolution: Math.max(window.devicePixelRatio || 1, 1),
        autoDensity: true,
        transparent: true,
        backgroundAlpha: 0,
        antialias: true,
      });
      model = await PIXI.live2d.Live2DModel.from(manifest.model_url, {
        autoHitTest: true,
        // Soullink drives gaze directly when enabled. Otherwise preserve the
        // original cursor-follow behavior.
        autoFocus: !manifest.soullink?.enabled,
      });
      app.stage.addChild(model);
      app.ticker.add((delta) => {
        if (applySoullink(delta)) return;
        const coreModel = model?.internalModel?.coreModel;
        if (!coreModel?.setParameterValueById) return;
        mouthPhase += delta * 0.34;
        const value = speaking ? 0.18 + Math.abs(Math.sin(mouthPhase)) * 0.48 : 0;
        coreModel.setParameterValueById("ParamMouthOpenY", value);
      }, undefined, (PIXI.UPDATE_PRIORITY?.LOW ?? -25) - 1);
      fitModel();
      resizeObserver = new ResizeObserver(fitModel);
      resizeObserver.observe(stage);
      await initializeSoullink(manifest.soullink);
      placeholder.hidden = true;
      await setEmotion("neutral");
    } catch (error) {
      console.error("Live2D initialization failed", error);
      showStatus("Live2D 加载失败，请刷新重试", true);
    }
  }

  globalThis.NekoPublicAvatar = {
    setEmotion,
    setSpeaking(value) {
      speaking = Boolean(value);
      soullinkRuntime?.setVoicePlaybackActive(speaking);
    },
    get ready() { return Boolean(model); },
  };

  window.addEventListener("pagehide", (event) => {
    if (event.persisted) return;
    resizeObserver?.disconnect();
    app?.destroy?.(true, { children: true, texture: true, baseTexture: true });
  });

  initialize();
})();

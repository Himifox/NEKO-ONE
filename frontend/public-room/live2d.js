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
    const scale = Math.min(width / model.width, height / model.height) * 0.92;
    model.scale.set(scale);
    model.anchor?.set?.(0.5, 0.5);
    model.position.set(width / 2, height / 2 + height * 0.04);
  }

  async function setEmotion(emotion = "neutral") {
    if (!model) return false;
    const supported = new Set(["neutral", "happy", "sad", "angry", "surprised"]);
    const normalized = supported.has(emotion) ? emotion : "neutral";
    try {
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
    if (!canvas || !stage || !globalThis.PIXI?.live2d?.Live2DModel) {
      showStatus("Live2D 运行库不可用", true);
      return;
    }
    try {
      const response = await fetch("/api/v1/avatar", { credentials: "same-origin" });
      if (!response.ok) throw new Error(`avatar manifest: ${response.status}`);
      const manifest = await response.json();
      if (!manifest.enabled) throw new Error("avatar is disabled");

      app = new PIXI.Application({
        view: canvas,
        autoStart: true,
        resizeTo: stage,
        transparent: true,
        backgroundAlpha: 0,
        antialias: true,
      });
      model = await PIXI.live2d.Live2DModel.from(manifest.model_url, {
        autoInteract: true,
      });
      app.stage.addChild(model);
      app.ticker.add((delta) => {
        const coreModel = model?.internalModel?.coreModel;
        if (!coreModel?.setParameterValueById) return;
        mouthPhase += delta * 0.34;
        const value = speaking ? 0.18 + Math.abs(Math.sin(mouthPhase)) * 0.48 : 0;
        coreModel.setParameterValueById("ParamMouthOpenY", value);
      }, undefined, (PIXI.UPDATE_PRIORITY?.LOW ?? -25) - 1);
      fitModel();
      resizeObserver = new ResizeObserver(fitModel);
      resizeObserver.observe(stage);
      placeholder.hidden = true;
      await setEmotion("neutral");
    } catch (error) {
      console.error("Live2D initialization failed", error);
      showStatus("Live2D 加载失败，请刷新重试", true);
    }
  }

  globalThis.NekoPublicAvatar = {
    setEmotion,
    setSpeaking(value) { speaking = Boolean(value); },
    get ready() { return Boolean(model); },
  };

  window.addEventListener("pagehide", () => {
    resizeObserver?.disconnect();
    app?.destroy?.(true, { children: true, texture: true, baseTexture: true });
  }, { once: true });

  initialize();
})();

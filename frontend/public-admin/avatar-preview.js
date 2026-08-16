(() => {
  "use strict";

  const stage = document.getElementById("avatar-preview-stage");
  const canvas = document.getElementById("avatar-preview-canvas");
  const placeholder = document.getElementById("avatar-preview-placeholder");
  let app = null;
  let model = null;
  let currentUrl = "";
  let loadVersion = 0;
  let resizeObserver = null;

  function setPlaceholder(message, failed = false) {
    if (!placeholder) return;
    placeholder.hidden = false;
    placeholder.dataset.failed = failed ? "true" : "false";
    const detail = placeholder.querySelector("small");
    if (detail) detail.textContent = message;
  }

  function clearModel() {
    if (!model) return;
    app?.stage?.removeChild(model);
    model.destroy?.({ children: true, texture: true, baseTexture: true });
    model = null;
  }

  function fitModel() {
    if (!model || !stage) return;
    const width = Math.max(stage.clientWidth, 220);
    const height = Math.max(stage.clientHeight, 180);
    const bounds = model.getLocalBounds();
    if (!bounds.width || !bounds.height) return;
    const scale = Math.min(width / bounds.width, height / bounds.height) * 0.9;
    model.scale.set(scale);
    model.pivot.set(bounds.x + bounds.width / 2, bounds.y + bounds.height);
    model.position.set(width / 2, height);
  }

  function ensureApp() {
    if (app || !canvas || !stage || !globalThis.PIXI) return Boolean(app);
    app = new PIXI.Application({
      view: canvas,
      autoStart: true,
      resizeTo: stage,
      transparent: true,
      backgroundAlpha: 0,
      antialias: true,
    });
    resizeObserver = new ResizeObserver(fitModel);
    resizeObserver.observe(stage);
    return true;
  }

  async function show(manifest = {}) {
    const modelUrl = manifest.enabled && typeof manifest.model_url === "string"
      ? manifest.model_url
      : "";
    if (!modelUrl) {
      loadVersion += 1;
      currentUrl = "";
      clearModel();
      setPlaceholder("当前未启用 Live2D 模型");
      return;
    }
    if (modelUrl === currentUrl && model) return;
    if (!ensureApp() || !globalThis.PIXI?.live2d?.Live2DModel) {
      setPlaceholder("Live2D 预览运行库不可用", true);
      return;
    }

    const requestedVersion = ++loadVersion;
    currentUrl = modelUrl;
    clearModel();
    setPlaceholder("正在加载模型预览");
    try {
      const nextModel = await PIXI.live2d.Live2DModel.from(modelUrl, {
        autoInteract: false,
      });
      if (requestedVersion !== loadVersion) {
        nextModel.destroy?.({ children: true, texture: true, baseTexture: true });
        return;
      }
      model = nextModel;
      app.stage.addChild(model);
      fitModel();
      placeholder.hidden = true;
    } catch (error) {
      if (requestedVersion !== loadVersion) return;
      currentUrl = "";
      console.error("Admin Live2D preview failed", error);
      setPlaceholder("模型预览加载失败，公共房间不受影响", true);
    }
  }

  globalThis.NekoAdminAvatar = {
    show,
    get ready() { return Boolean(model); },
  };

  window.addEventListener("pagehide", (event) => {
    if (event.persisted) return;
    loadVersion += 1;
    resizeObserver?.disconnect();
    app?.destroy?.(true, { children: true, texture: true, baseTexture: true });
  });
})();

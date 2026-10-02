/* Trusted local browser controls. Remote product pages never load this script. */
(() => {
  "use strict";

  const allowedActions = new Set(["ready", "navigate", "back", "forward", "home", "reload", "choose-project", "toggle-panel"]);
  const surface = location.hash === "#toolbar" ? "toolbar" : "sidebar";
  document.documentElement.dataset.surface = surface;
  const byId = (id) => document.getElementById(id);
  let model = {
    projectPath: "", projectName: "", address: "", canBack: false, canForward: false,
    busy: false, status: "", statusTone: "idle", lastResult: null, logs: [], panelOpen: false,
  };
  let addressDirty = false;
  let previousLogKey = "";
  let previousTone = "idle";
  const input = byId("address-input");

  function send(action, value) {
    if (!allowedActions.has(action)) return;
    const message = {version: 1, action};
    if (typeof value === "string") message.value = value;
    if (window.lcscShell && typeof window.lcscShell.postMessage === "function") {
      window.lcscShell.postMessage(JSON.stringify(message));
    }
  }

  function text(id, value) {
    byId(id).textContent = value == null ? "" : String(value);
  }

  function updateLogs(entries) {
    const logs = Array.isArray(entries) ? entries.slice(-160).map(String) : [];
    const key = JSON.stringify(logs);
    if (key === previousLogKey) return;
    previousLogKey = key;
    const list = byId("log-list");
    const nearEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 35;
    const fragment = document.createDocumentFragment();
    logs.forEach((entry) => {
      const row = document.createElement("div");
      row.className = "log-entry";
      row.textContent = entry;
      fragment.appendChild(row);
    });
    list.replaceChildren(fragment);
    document.querySelector(".log-panel").hidden = logs.length === 0;
    text("log-count", logs.length);
    if (nearEnd) list.scrollTop = list.scrollHeight;
  }

  function render() {
    const path = String(model.projectPath || "");
    const name = String(model.projectName || "");
    const projectName = name || (path ? path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() : "");
    text("project-name", projectName || "未选择");
    text("toolbar-project", projectName || "选择工程");
    text("project-path", path);
    byId("project-path").hidden = !path;
    byId("project-path").title = path;
    byId("project-button").disabled = Boolean(model.busy);
    byId("project-button").title = path || "选择工程";
    byId("project-button").setAttribute("aria-label", projectName ? "切换工程：" + projectName : "选择工程");
    ["details-toggle", "toolbar-status"].forEach((id) => {
      byId(id).setAttribute("aria-expanded", String(Boolean(model.panelOpen)));
    });
    byId("nav-back").disabled = !model.canBack;
    byId("nav-forward").disabled = !model.canForward;
    if (!addressDirty || document.activeElement !== input) input.value = String(model.address || "");

    const tone = model.busy ? "busy" : (["error", "danger", "warning"].includes(model.statusTone) ? "error" : model.statusTone === "success" ? "success" : "idle");
    const states = {
      idle: ["提示", "icon-alert"],
      busy: ["导入中", "icon-download"],
      success: [model.lastResult && model.lastResult.alreadyPresent ? "已存在" : "已导入", "icon-check"],
      error: ["未完成", "icon-alert"],
    };
    const state = states[tone];
    const message = String(model.status || "");
    const result = model.lastResult;
    const isResultSummary = tone === "success" && result && message === result.componentId + " " + state[0];
    const hasStatus = tone !== "idle" || Boolean(message);
    byId("status-card").dataset.tone = tone;
    byId("status-card").hidden = !hasStatus;
    byId("status-card").setAttribute("aria-busy", String(Boolean(model.busy)));
    text("status-title", state[0]);
    text("status-message", message);
    byId("status-message").hidden = !message || isResultSummary;
    byId("status-icon").querySelector("use").setAttribute("href", "#" + state[1]);

    const compactStatus = result && result.componentId ? result.componentId + " · " + state[0] : state[0];
    const toolbarText = (tone === "idle" || (tone === "success" && !isResultSummary)) && message ? message : compactStatus;
    byId("toolbar-status").hidden = !hasStatus;
    byId("toolbar-status").dataset.tone = tone;
    byId("toolbar-status").title = message || compactStatus;
    byId("toolbar-status").setAttribute("aria-label", toolbarText + "，导入记录");
    text("toolbar-status-text", toolbarText);
    byId("result-section").hidden = !result;
    if (result) {
      const imported = Boolean(result.libraryName && !model.busy && model.statusTone === "success");
      text("result-component", result.componentId || "元件库");
      text("result-library", result.libraryName || "");
      byId("result-assets").hidden = !imported;
      byId("result-model").hidden = !result.has3d;
    }
    updateLogs(model.logs);
    if (tone === "error" && previousTone !== "error") document.querySelector(".log-panel").open = true;
    previousTone = tone;
  }

  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => send(button.dataset.action));
  });
  input.addEventListener("input", () => { addressDirty = true; });
  byId("address-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) { input.focus(); return; }
    addressDirty = false;
    send("navigate", value);
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "l" && surface === "toolbar") {
      event.preventDefault();
      input.focus();
      input.select();
    }
  });

  window.LCSCBridgeUI = Object.freeze({
    update(next) {
      if (!next || typeof next !== "object" || Array.isArray(next)) return;
      model = Object.assign({}, model, next);
      render();
    },
  });
  render();
  send("ready");
})();

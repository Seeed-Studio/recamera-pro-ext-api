"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "muse.webui.token";

function tokenFromHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const params = new URLSearchParams(raw);
  return params.get("token") || "";
}

function readToken() {
  const fromHash = tokenFromHash();
  if (fromHash) sessionStorage.setItem(TOKEN_KEY, fromHash);
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

function setText(id, text) {
  $(id).textContent = text;
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  const token = readToken();
  if (token) headers["Authorization"] = "Bearer " + token;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, Object.assign({}, options, { headers }));
  let data = {};
  try { data = await response.json(); } catch (_) { /* empty */ }
  if (response.status === 401) {
    $("auth").classList.remove("hidden");
    throw new Error("unauthorized");
  }
  if (!response.ok) throw new Error(data.error || ("HTTP " + response.status));
  return data;
}

function showAuthIfNeeded() {
  if (!readToken()) $("auth").classList.remove("hidden");
}

function render(status) {
  const service = status.service || {};
  const pairing = status.pairing || {};
  const ca = status.ca || {};
  setText("st-paired", status.paired ? "已配对" : "未配对");
  setText("st-ble", status.ble_name || "—");
  setText("st-node", status.node_id || "—");
  setText("st-service", service.running ? ("运行中 (pid " + service.pid + ")") : "已停止");
  setText("st-hci", status.hci0 ? "hci0 可用" : "无 hci0");
  setText("st-dbus", status.dbus && status.ble_bindings ? "可用" : "不可用");
  setText("st-ca", (ca.dropped || 0) + " / " + (ca.total || 0) + " 已移除");
  setText("st-token", status.sdk_token_set ? (status.sdk_token_hint || "已设置") : "未设置");

  const health = $("health");
  if (!status.core_imports) {
    health.textContent = "依赖缺失";
    health.className = "pill bad";
  } else if (status.paired && service.running) {
    health.textContent = "已连接";
    health.className = "pill good";
  } else if (!status.hci0) {
    health.textContent = "蓝牙未就绪";
    health.className = "pill warn";
  } else {
    health.textContent = "等待配对";
    health.className = "pill warn";
  }

  const pairBits = [];
  if (pairing.active) pairBits.push("配对中，剩余 " + pairing.remaining_s + " 秒");
  if (pairing.state && pairing.state !== "idle") pairBits.push("配对状态: " + pairing.state);
  if (pairing.result) pairBits.push(pairing.result);
  if (status.sdk_token_error) pairBits.push(status.sdk_token_error);
  setText("st-pair", pairBits.join(" · "));

  $("pair-log").textContent = (pairing.lines || []).join("\n");
  $("unpair").disabled = false;
  $("pair-cancel").disabled = !pairing.active;
  $("pair").disabled = pairing.active;

  // Do not overwrite a field the user is editing.
  const active = document.activeElement;
  if (active !== $("s-sdk")) $("s-sdk").value = "";
  $("s-sdk").placeholder = status.sdk_token_set ? "已保存（留空不修改）" : "mgst_...";
  if (active !== $("s-proxy")) $("s-proxy").value = "";
  $("s-proxy").placeholder = status.proxy_configured
    ? "已配置（留空不修改）"
    : "http://host:port";
  if (active !== $("s-noproxy")) $("s-noproxy").value = status.no_proxy || "";
  if (active !== $("s-runas")) $("s-runas").value = status.run_as || "admin";
  if (active !== $("s-timeout")) $("s-timeout").value = status.pair_timeout_s || 600;
  if (active !== $("s-autopair")) $("s-autopair").checked = !!status.auto_pair;
  if (active !== $("s-force")) $("s-force").checked = !!status.pair_force;
  if (active !== $("s-wifiprov")) $("s-wifiprov").checked = !!status.wifi_provisioning;
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    render(status);
  } catch (error) {
    if (error.message !== "unauthorized") setText("health", "连接失败");
  }
}

async function refreshLogs() {
  if (!$("logs-auto").checked) return;
  try {
    const data = await api("/api/logs?tail=200");
    $("logs").textContent = (data.lines || []).join("\n");
  } catch (_) { /* ignored */ }
}

async function post(path, body) {
  return api(path, { method: "POST", body: JSON.stringify(body || {}) });
}

$("token-save").addEventListener("click", () => {
  const value = $("token-input").value.trim();
  if (!value) return;
  sessionStorage.setItem(TOKEN_KEY, value);
  $("auth").classList.add("hidden");
  refreshStatus();
});

$("pair").addEventListener("click", async () => {
  try { await post("/api/pair", { force: false }); } catch (e) { alert(e.message); }
  refreshStatus();
});

$("pair-force").addEventListener("click", async () => {
  if (!confirm("强制重新配对会替换现有配对，继续？")) return;
  try { await post("/api/pair", { force: true }); } catch (e) { alert(e.message); }
  refreshStatus();
});

$("pair-cancel").addEventListener("click", async () => {
  try { await post("/api/pair/cancel", {}); } catch (e) { alert(e.message); }
  refreshStatus();
});

$("unpair").addEventListener("click", async () => {
  if (!confirm("解除配对后需要重新用手机配对，继续？")) return;
  try { await post("/api/unpair", {}); } catch (e) { alert(e.message); }
  refreshStatus();
});

$("restart").addEventListener("click", async () => {
  try { await post("/api/service/restart", {}); } catch (e) { alert(e.message); }
  setTimeout(refreshStatus, 600);
});

$("save").addEventListener("click", async () => {
  const payload = {
    no_proxy: $("s-noproxy").value.trim(),
    run_as: $("s-runas").value,
    pair_timeout_s: Number($("s-timeout").value),
    autopair: $("s-autopair").checked,
    pair_force: $("s-force").checked,
    wifi_provisioning: $("s-wifiprov").checked,
  };
  const sdk = $("s-sdk").value.trim();
  if (sdk) payload.sdk_token = sdk;
  if ($("s-proxy-clear").checked) {
    payload.proxy_url = "";
  } else if ($("s-proxy").value.trim()) {
    payload.proxy_url = $("s-proxy").value.trim();
  }
  try {
    await post("/api/settings", payload);
    $("save-msg").textContent = "已保存";
    $("s-sdk").value = "";
    $("s-proxy").value = "";
    $("s-proxy-clear").checked = false;
  } catch (error) {
    $("save-msg").textContent = "保存失败: " + error.message;
  }
  refreshStatus();
});

$("doctor").addEventListener("click", async () => {
  try {
    const data = await api("/api/doctor");
    const out = $("doctor-out");
    out.classList.remove("hidden");
    out.textContent = data.output || "(no output)";
  } catch (error) {
    alert(error.message);
  }
});

$("logs-refresh").addEventListener("click", refreshLogs);

showAuthIfNeeded();
refreshStatus();
refreshLogs();
setInterval(() => { refreshStatus(); refreshLogs(); }, 3000);
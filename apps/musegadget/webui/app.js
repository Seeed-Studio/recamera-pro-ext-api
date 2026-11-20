"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "muse.webui.token";
const LANG_KEY = "muse.webui.lang";

const I18N = {
  en: {
    subtitle: "reCamera Pro · Bluetooth pairing and connection status",
    access_token: "Access token",
    access_token_help: "The App Center Open link should include the token. If it does not, paste the contents of appdata/webui_token.",
    use_token: "Use token",
    status: "Status",
    pairing_label: "Pairing",
    ble_name: "BLE name",
    node_id: "Node ID",
    service: "Service",
    bluetooth_adapter: "Bluetooth adapter",
    dbus_gi: "D-Bus / gi",
    ca_filter: "CA filter",
    sdk_token_label: "SDK token",
    pairing: "Pairing",
    pairing_help: "Click Start pairing, then in the Muse app open Settings → Devices → Developer mode → Add Device and choose the BLE name above.",
    start_pairing: "Start pairing",
    force_repair: "Force re-pair",
    cancel_pairing: "Cancel pairing",
    unpair: "Unpair",
    restart_service: "Restart service",
    settings: "Settings",
    https_proxy: "HTTPS proxy",
    clear_proxy: "Clear proxy settings",
    proxy_bypass: "Proxy bypass",
    command_account: "Command account",
    runas_admin: "admin (recommended)",
    runas_root: "root (high risk)",
    pairing_window: "Pairing window (seconds)",
    auto_pair: "Auto-pair on first start",
    allow_replace: "Allow replacing an existing pairing",
    provision_wifi: "Provision WiFi during pairing (off: bind only, use current network)",
    save_settings: "Save settings",
    run_doctor: "Run Doctor",
    logs: "Logs",
    refresh: "Refresh",
    auto_refresh: "Auto refresh",
    loading: "Loading…",
    deps_missing: "Missing dependencies",
    connected: "Connected",
    bt_not_ready: "Bluetooth not ready",
    waiting_pair: "Waiting for pairing",
    connection_failed: "Connection failed",
    paired: "Paired",
    unpaired: "Not paired",
    service_running: "Running (pid {pid})",
    service_stopped: "Stopped",
    hci_ok: "hci0 available",
    hci_missing: "No hci0",
    available: "Available",
    unavailable: "Unavailable",
    ca_removed: "{dropped} / {total} removed",
    token_set: "Set",
    token_not_set: "Not set",
    pairing_remaining: "Pairing, {seconds}s remaining",
    pairing_status: "Pairing status: {state}",
    pair_state_idle: "Idle",
    pair_state_pairing: "Pairing",
    pair_state_paired: "Paired",
    pair_state_already_paired: "Already paired",
    pair_state_invalid_sdk_token: "Invalid SDK token",
    pair_state_window_closed: "Pairing window closed",
    pair_state_cancelled: "Cancelled",
    pair_state_error: "Error",
    result_paired: "Paired.",
    result_already_paired: "Already paired.",
    result_invalid_token: "Pairing rejected the SDK token.",
    result_window_closed: "Pairing window closed without pairing.",
    result_cancelled: "Pairing cancelled.",
    result_expired: "Pairing window expired.",
    result_exception: "Pairing raised an exception; see the log.",
    precond_no_hci: "No Bluetooth adapter (hci0); check S39bt-firmware / BlueZ.",
    precond_no_dbus: "The system D-Bus is not running.",
    precond_no_ble: "dbus/gi Python bindings are unavailable.",
    precond_start_failed: "Could not start pairing: {error}",
    token_format_invalid: "SDK token format is invalid.",
    save_saved: "Saved",
    save_failed: "Save failed: {error}",
    force_confirm: "Force re-pair replaces the current pairing. Continue?",
    unpair_confirm: "Unpairing requires pairing again from the phone. Continue?",
    saved_placeholder: "Saved (leave blank to keep)",
    configured_placeholder: "Configured (leave blank to keep)",
    no_output: "(no output)",
  },
  zh: {
    subtitle: "reCamera Pro · 蓝牙配对与连接状态",
    access_token: "访问令牌",
    access_token_help: "App Center 的 Open 链接应自动带上令牌。如果没有，请粘贴 appdata/webui_token。",
    use_token: "使用令牌",
    status: "状态",
    pairing_label: "配对",
    ble_name: "BLE 名",
    node_id: "Node ID",
    service: "服务",
    bluetooth_adapter: "蓝牙适配器",
    dbus_gi: "D-Bus / gi",
    ca_filter: "CA 过滤",
    sdk_token_label: "SDK 令牌",
    pairing: "配对",
    pairing_help: "点击“开始配对”，然后在手机 Muse App 的 Settings → Devices → Developer mode → Add Device 中选择上面的 BLE 名。",
    start_pairing: "开始配对",
    force_repair: "强制重新配对",
    cancel_pairing: "取消配对",
    unpair: "解除配对",
    restart_service: "重启服务",
    settings: "设置",
    https_proxy: "HTTPS 代理",
    clear_proxy: "清除代理设置",
    proxy_bypass: "代理绕过",
    command_account: "命令账户",
    runas_admin: "admin（推荐）",
    runas_root: "root（高风险）",
    pairing_window: "配对窗口（秒）",
    auto_pair: "首次启动自动配对",
    allow_replace: "允许替换现有配对",
    provision_wifi: "配对时配置 WiFi（关闭则只绑定，使用当前网络）",
    save_settings: "保存设置",
    run_doctor: "运行 Doctor",
    logs: "日志",
    refresh: "刷新",
    auto_refresh: "自动刷新",
    loading: "加载中…",
    deps_missing: "依赖缺失",
    connected: "已连接",
    bt_not_ready: "蓝牙未就绪",
    waiting_pair: "等待配对",
    connection_failed: "连接失败",
    paired: "已配对",
    unpaired: "未配对",
    service_running: "运行中 (pid {pid})",
    service_stopped: "已停止",
    hci_ok: "hci0 可用",
    hci_missing: "无 hci0",
    available: "可用",
    unavailable: "不可用",
    ca_removed: "{dropped} / {total} 已移除",
    token_set: "已设置",
    token_not_set: "未设置",
    pairing_remaining: "配对中，剩余 {seconds} 秒",
    pairing_status: "配对状态: {state}",
    pair_state_idle: "空闲",
    pair_state_pairing: "配对中",
    pair_state_paired: "已配对",
    pair_state_already_paired: "已配对",
    pair_state_invalid_sdk_token: "SDK 令牌无效",
    pair_state_window_closed: "配对窗口已关闭",
    pair_state_cancelled: "已取消",
    pair_state_error: "错误",
    result_paired: "已配对。",
    result_already_paired: "已配对。",
    result_invalid_token: "配对被 SDK 令牌拒绝。",
    result_window_closed: "配对窗口关闭，未完成配对。",
    result_cancelled: "配对已取消。",
    result_expired: "配对窗口已过期。",
    result_exception: "配对异常，请查看日志。",
    precond_no_hci: "没有蓝牙适配器（hci0）；请检查 S39bt-firmware / BlueZ。",
    precond_no_dbus: "系统 D-Bus 未运行。",
    precond_no_ble: "dbus/gi Python 绑定不可用。",
    precond_start_failed: "无法启动配对：{error}",
    token_format_invalid: "SDK 令牌格式无效。",
    save_saved: "已保存",
    save_failed: "保存失败: {error}",
    force_confirm: "强制重新配对会替换现有配对，继续？",
    unpair_confirm: "解除配对后需要重新用手机配对，继续？",
    saved_placeholder: "已保存（留空不修改）",
    configured_placeholder: "已配置（留空不修改）",
    no_output: "(无输出)",
  },
};

const RESULT_KEYS = {
  "Paired.": "result_paired",
  "Already paired.": "result_already_paired",
  "Pairing rejected the SDK token.": "result_invalid_token",
  "Pairing window closed without pairing.": "result_window_closed",
  "Pairing cancelled.": "result_cancelled",
  "pairing window expired": "result_expired",
  "pairing raised an exception; see the log": "result_exception",
  "no Bluetooth adapter (hci0); check S39bt-firmware / BlueZ": "precond_no_hci",
  "system D-Bus is not running": "precond_no_dbus",
  "dbus/gi Python bindings are unavailable": "precond_no_ble",
  "SDK token format is invalid": "token_format_invalid",
};

function readStoredLanguage() {
  try {
    const value = localStorage.getItem(LANG_KEY);
    return value === "zh" || value === "en" ? value : null;
  } catch (_) {
    return null;
  }
}

let lang = readStoredLanguage() ||
  (((navigator.language || "").toLowerCase().startsWith("zh")) ? "zh" : "en");
let lastStatus = null;

function t(key, vars) {
  const table = I18N[lang] || I18N.en;
  let text = Object.prototype.hasOwnProperty.call(table, key) ? table[key] : I18N.en[key];
  if (typeof text !== "string") text = key;
  if (vars) {
    for (const name of Object.keys(vars)) {
      text = text.split("{" + name + "}").join(String(vars[name]));
    }
  }
  return text;
}

function localizedPairState(state) {
  const key = "pair_state_" + String(state || "idle");
  const table = I18N[lang] || I18N.en;
  return Object.prototype.hasOwnProperty.call(table, key) ? t(key) : String(state || "");
}

function localizedResult(result) {
  if (typeof result !== "string") return "";
  const key = RESULT_KEYS[result];
  if (key) return t(key);
  const prefix = "could not start pair: ";
  if (result.startsWith(prefix)) return t("precond_start_failed", { error: result.slice(prefix.length) });
  return result;
}

function applyLanguage() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.getAttribute("data-i18n"));
  });
  const select = $("lang");
  if (select) select.value = lang;
  if (lastStatus) render(lastStatus);
}

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
  lastStatus = status;
  const service = status.service || {};
  const pairing = status.pairing || {};
  const ca = status.ca || {};
  setText("st-paired", status.paired ? t("paired") : t("unpaired"));
  setText("st-ble", status.ble_name || "—");
  setText("st-node", status.node_id || "—");
  setText("st-service", service.running
    ? t("service_running", { pid: service.pid })
    : t("service_stopped"));
  setText("st-hci", status.hci0 ? t("hci_ok") : t("hci_missing"));
  setText("st-dbus", status.dbus && status.ble_bindings ? t("available") : t("unavailable"));
  setText("st-ca", t("ca_removed", {
    dropped: ca.dropped || 0,
    total: ca.total || 0,
  }));
  setText("st-token", status.sdk_token_set
    ? (status.sdk_token_hint || t("token_set"))
    : t("token_not_set"));

  const health = $("health");
  if (!status.core_imports) {
    health.textContent = t("deps_missing");
    health.className = "pill bad";
  } else if (status.paired && service.running) {
    health.textContent = t("connected");
    health.className = "pill good";
  } else if (!status.hci0) {
    health.textContent = t("bt_not_ready");
    health.className = "pill warn";
  } else {
    health.textContent = t("waiting_pair");
    health.className = "pill warn";
  }

  const pairBits = [];
  if (pairing.active) pairBits.push(t("pairing_remaining", { seconds: pairing.remaining_s }));
  if (pairing.state && pairing.state !== "idle") {
    pairBits.push(t("pairing_status", { state: localizedPairState(pairing.state) }));
  }
  if (pairing.result) pairBits.push(localizedResult(pairing.result));
  if (status.sdk_token_error) pairBits.push(localizedResult(status.sdk_token_error));
  setText("st-pair", pairBits.join(" · "));

  $("pair-log").textContent = (pairing.lines || []).join("\n");
  $("unpair").disabled = false;
  $("pair-cancel").disabled = !pairing.active;
  $("pair").disabled = pairing.active;

  // Do not overwrite a field the user is editing.
  const active = document.activeElement;
  if (active !== $("s-sdk")) $("s-sdk").value = "";
  $("s-sdk").placeholder = status.sdk_token_set ? t("saved_placeholder") : "mgst_...";
  if (active !== $("s-proxy")) $("s-proxy").value = "";
  $("s-proxy").placeholder = status.proxy_configured
    ? t("configured_placeholder")
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
    if (error.message !== "unauthorized") setText("health", t("connection_failed"));
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

$("lang").addEventListener("change", () => {
  lang = $("lang").value === "zh" ? "zh" : "en";
  try { localStorage.setItem(LANG_KEY, lang); } catch (_) { /* ignored */ }
  $("save-msg").textContent = "";
  applyLanguage();
});

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
  if (!confirm(t("force_confirm"))) return;
  try { await post("/api/pair", { force: true }); } catch (e) { alert(e.message); }
  refreshStatus();
});

$("pair-cancel").addEventListener("click", async () => {
  try { await post("/api/pair/cancel", {}); } catch (e) { alert(e.message); }
  refreshStatus();
});

$("unpair").addEventListener("click", async () => {
  if (!confirm(t("unpair_confirm"))) return;
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
    $("save-msg").textContent = t("save_saved");
    $("s-sdk").value = "";
    $("s-proxy").value = "";
    $("s-proxy-clear").checked = false;
  } catch (error) {
    $("save-msg").textContent = t("save_failed", { error: error.message });
  }
  refreshStatus();
});

$("doctor").addEventListener("click", async () => {
  try {
    const data = await api("/api/doctor");
    const out = $("doctor-out");
    out.classList.remove("hidden");
    out.textContent = data.output || t("no_output");
  } catch (error) {
    alert(error.message);
  }
});

$("logs-refresh").addEventListener("click", refreshLogs);

showAuthIfNeeded();
applyLanguage();
refreshStatus();
refreshLogs();
setInterval(() => { refreshStatus(); refreshLogs(); }, 3000);
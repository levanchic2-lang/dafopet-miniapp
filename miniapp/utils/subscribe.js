// 订阅消息必须在用户点击的调用栈里同步发起，不能先 await 别的请求。
// 模板 ID 在页面 onShow 时拉配置并写入本地，点击时只读缓存。

const REVIEW_FIELDS = [
  ["wechat_tmpl_application_result", "WECHAT_TMPL_APPLICATION_RESULT"],
  ["wechat_tmpl_rejection", "WECHAT_TMPL_REJECTION"],
  ["wechat_tmpl_pending_manual", "WECHAT_TMPL_PENDING_MANUAL"]
];
const APPOINTMENT_FIELDS = [
  ["wechat_tmpl_appointment", "WECHAT_TMPL_APPOINTMENT"],
  ["wechat_tmpl_surgery_reminder", "WECHAT_TMPL_SURGERY_REMINDER"],
  ["wechat_tmpl_surgery_done", "WECHAT_TMPL_SURGERY_DONE"]
];

const PROMPT = "请开启通知提醒，否则收不到审核/预约/手术进度";

function cacheGet(key) {
  try { return wx.getStorageSync(key) || ""; } catch (e) { return ""; }
}

function cacheSet(key, value) {
  try { if (value) wx.setStorageSync(key, value); } catch (e) {}
}

const STATUS_FIELDS = [
  ["wechat_tmpl_application_result", "WECHAT_TMPL_APPLICATION_RESULT"],
  ["wechat_tmpl_appointment", "WECHAT_TMPL_APPOINTMENT"],
  ["wechat_tmpl_surgery_reminder", "WECHAT_TMPL_SURGERY_REMINDER"]
];

function fieldsFor(group) {
  if (group === "appointment") return APPOINTMENT_FIELDS;
  if (group === "status") return STATUS_FIELDS;
  return REVIEW_FIELDS;
}

function cachedIds(group) {
  const ids = [];
  fieldsFor(group).forEach(([, key]) => {
    const value = String(cacheGet(key) || "").trim();
    if (value && ids.indexOf(value) < 0) ids.push(value);
  });
  return ids.slice(0, 3);
}

function loadConfig() {
  const app = getApp();
  return new Promise((resolve) => {
    wx.request({
      url: (app.globalData.apiBase || "") + "/api/wechat/config",
      method: "GET",
      success: (res) => resolve((res && res.statusCode < 300 && res.data) || {}),
      fail: () => resolve({})
    });
  });
}

async function prefetch(group) {
  let cfg = {};
  try { cfg = await loadConfig(); } catch (e) { cfg = {}; }
  [...REVIEW_FIELDS, ...APPOINTMENT_FIELDS].forEach(([field, key]) => {
    if (cfg[field]) cacheSet(key, cfg[field]);
  });
  const ids = [];
  fieldsFor(group).forEach(([field, key]) => {
    const value = String((cfg[field] || cacheGet(key) || "")).trim();
    if (value && ids.indexOf(value) < 0) ids.push(value);
    if (value) cacheSet(key, value);
  });
  return ids.slice(0, 3);
}

function requestSubscribe(tmplIds) {
  const ids = (tmplIds || []).map((id) => String(id || "").trim()).filter(Boolean).slice(0, 3);
  if (!ids.length) return Promise.resolve({});
  return new Promise((resolve, reject) => {
    wx.requestSubscribeMessage({
      tmplIds: ids,
      success: (res) => resolve(res || {}),
      fail: reject
    });
  });
}

function summarize(result) {
  const out = {};
  Object.keys(result || {}).forEach((key) => {
    if (key === "errMsg" || key === "errCode") return;
    const value = result[key];
    if (value === "accept" || value === "reject" || value === "ban" || value === "filter") {
      out[key] = value;
    }
  });
  return out;
}

function acceptedAny(result) {
  const summary = summarize(result);
  return Object.keys(summary).some((key) => summary[key] === "accept");
}

function report(openid, scene, result, mainSwitch) {
  const results = summarize(result);
  if (!openid || !Object.keys(results).length) return Promise.resolve();
  const app = getApp();
  const data = { openid, scene: scene || "", results };
  if (mainSwitch === true) data.main_switch = "on";
  if (mainSwitch === false) data.main_switch = "off";
  return new Promise((resolve) => {
    wx.request({
      url: (app.globalData.apiBase || "") + "/api/wechat/subscribe-report",
      method: "POST",
      header: { "content-type": "application/json" },
      data,
      complete: () => resolve()
    });
  });
}

function readSetting() {
  return new Promise((resolve) => {
    if (!wx.getSetting) return resolve({});
    wx.getSetting({
      withSubscriptions: true,
      success: (res) => resolve((res && res.subscriptionsSetting) || {}),
      fail: () => resolve({})
    });
  });
}

function shouldOpenSettings(setting, tmplIds) {
  if (!setting) return false;
  if (setting.mainSwitch === false) return true;
  const items = setting.itemSettings || {};
  return (tmplIds || []).some((id) => items[id] === "ban");
}

function openSettings() {
  return new Promise((resolve) => {
    if (!wx.openSetting) return resolve({});
    wx.openSetting({
      withSubscriptions: true,
      complete: () => resolve({})
    });
  });
}

function fetchStatus(openid, scene) {
  if (!openid) return Promise.resolve(null);
  const app = getApp();
  const query = "openid=" + encodeURIComponent(openid) + "&scene=" + encodeURIComponent(scene || "status");
  return new Promise((resolve) => {
    wx.request({
      url: (app.globalData.apiBase || "") + "/api/wechat/subscribe-status?" + query,
      method: "GET",
      success: (res) => resolve((res && res.statusCode < 300 && res.data) || null),
      fail: () => resolve(null)
    });
  });
}

module.exports = {
  PROMPT,
  cachedIds,
  prefetch,
  requestSubscribe,
  summarize,
  acceptedAny,
  report,
  readSetting,
  shouldOpenSettings,
  openSettings,
  fetchStatus
};

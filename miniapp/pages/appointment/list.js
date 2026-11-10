const { postJson } = require("../../utils/api");
const subscribe = require("../../utils/subscribe");

Page({
  onShareAppMessage() {
    return { title: "大风动物医院 · 流浪猫 TNR 申请", path: "/pages/index/index" };
  },
  onShareTimeline() {
    return { title: "大风动物医院 · 流浪猫 TNR 申请" };
  },
  data: {
    loading: false,
    error: "",
    items: [],
    highlightId: "",
    notifyPrompt: false,
    notifyOpenSettings: false
  },

  onLoad(query) {
    this.setData({ highlightId: String((query && query.highlight) || "") });
  },

  onShow() {
    subscribe.prefetch("appointment");
    this.fetchList();
    this._refreshNotifyPrompt();
  },

  async _refreshNotifyPrompt() {
    let openid = "";
    try { openid = wx.getStorageSync("WECHAT_OPENID") || ""; } catch (e) {}
    if (!openid) return;
    const [status, setting] = await Promise.all([
      subscribe.fetchStatus(openid, "appointment"),
      subscribe.readSetting()
    ]);
    const ids = (status && status.suggested_template_ids) || subscribe.cachedIds("appointment");
    this._suggestedIds = ids;
    this.setData({
      notifyPrompt: !!(status && status.needs_prompt),
      notifyOpenSettings: subscribe.shouldOpenSettings(setting, ids) || !!(status && status.open_settings)
    });
  },

  onReauthNotify() {
    if (this.data.notifyOpenSettings) {
      subscribe.openSettings();
      return;
    }
    const ids = (this._suggestedIds && this._suggestedIds.length)
      ? this._suggestedIds.slice(0, 3)
      : subscribe.cachedIds("appointment");
    let openid = "";
    try { openid = wx.getStorageSync("WECHAT_OPENID") || ""; } catch (e) {}
    if (!ids.length) return;
    wx.requestSubscribeMessage({
      tmplIds: ids,
      success: (res) => {
        subscribe.report(openid, "appointment_list_reauth", res);
        this.setData({ notifyPrompt: !subscribe.acceptedAny(res) });
      },
      fail: () => subscribe.openSettings()
    });
  },

  async ensureOpenid() {
    try {
      const saved = wx.getStorageSync("WECHAT_OPENID") || "";
      if (saved) return String(saved);
    } catch (e) {}
    const loginRes = await wx.login();
    if (!loginRes || !loginRes.code) throw new Error("微信登录失败");
    const data = await postJson("/api/wechat/login", { code: loginRes.code });
    const openid = (data && data.openid) || "";
    if (!openid) throw new Error("未获取到 openid");
    try {
      wx.setStorageSync("WECHAT_OPENID", openid);
    } catch (e) {}
    return openid;
  },

  async fetchList() {
    this.setData({ loading: true, error: "" });
    try {
      const openid = await this.ensureOpenid();
      const data = await postJson("/api/wechat/my-appointments", { openid });
      const highlightId = this.data.highlightId;
      const items = Array.isArray(data.items)
        ? data.items.map((item) =>
            Object.assign({}, item, {
              isHighlight: String(item.id || "") === highlightId,
              cancelling: false
            })
          )
        : [];
      this.setData({ loading: false, items });
    } catch (e) {
      this.setData({
        loading: false,
        error: (e && (e.detail || e.message || e.errMsg)) || "加载预约失败",
        items: []
      });
    }
  },

  onEditAppt(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    if (!id) return;
    wx.navigateTo({ url: `/pages/appointment/index?mode=edit&id=${id}` });
  },

  onCancelAppt(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const idx = Number(e.currentTarget.dataset.index || 0);
    if (!id) return;
    wx.showModal({
      title: "取消预约",
      content: "确定要取消这条预约吗？取消后不可撤回。",
      confirmText: "确认取消",
      confirmColor: "#7a2828",
      success: (res) => {
        if (!res.confirm) return;
        const ids = subscribe.cachedIds("appointment");
        const go = () => this._doCancel(id, idx);
        if (!ids.length) {
          go();
          return;
        }
        wx.requestSubscribeMessage({
          tmplIds: ids,
          complete: (sub) => {
            let openid = "";
            try { openid = wx.getStorageSync("WECHAT_OPENID") || ""; } catch (e) {}
            subscribe.report(openid, "appointment_cancel", sub || {});
            go();
          }
        });
      }
    });
  },

  async _doCancel(id, idx) {
    this.setData({ [`items[${idx}].cancelling`]: true });
    try {
      const openid = await this.ensureOpenid();
      await postJson(`/api/wechat/appointments/${id}/cancel`, { openid });
      wx.showToast({ title: "已取消", icon: "success" });
      // 更新本地状态，不重新拉取整个列表
      this.setData({
        [`items[${idx}].status`]: "cancelled",
        [`items[${idx}].status_zh`]: "已取消",
        [`items[${idx}].cancelling`]: false
      });
    } catch (e) {
      this.setData({ [`items[${idx}].cancelling`]: false });
      const msg = (e && (e.detail || e.message || e.errMsg)) || "取消失败，请稍后重试";
      wx.showModal({ title: "取消失败", content: String(msg), showCancel: false });
    }
  }
});

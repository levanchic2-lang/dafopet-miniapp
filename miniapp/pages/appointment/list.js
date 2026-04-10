const { postJson } = require("../../utils/api");

Page({
  data: {
    loading: false,
    error: "",
    items: [],
    highlightId: ""
  },

  onLoad(query) {
    this.setData({ highlightId: String((query && query.highlight) || "") });
  },

  onShow() {
    this.fetchList();
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

  onCancelAppt(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const idx = Number(e.currentTarget.dataset.index || 0);
    if (!id) return;
    wx.showModal({
      title: "取消预约",
      content: "确定要取消这条预约吗？取消后不可撤回。",
      confirmText: "确认取消",
      confirmColor: "#ef4444",
      success: (res) => {
        if (!res.confirm) return;
        this._doCancel(id, idx);
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

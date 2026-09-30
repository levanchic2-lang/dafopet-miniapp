const { postJson } = require("../../../utils/api");

Page({
  data: { username: "", password: "", loading: false, error: "" },
  onLoad() {
    try {
      if (wx.getStorageSync("STAFF_TOKEN")) wx.reLaunch({ url: "/pages/staff/today/today" });
    } catch (e) {}
  },
  onUsername(e) { this.setData({ username: (e.detail.value || "").trim(), error: "" }); },
  onPassword(e) { this.setData({ password: e.detail.value || "", error: "" }); },
  async onLogin() {
    if (this.data.loading) return;
    if (!this.data.username || !this.data.password) {
      this.setData({ error: "请输入员工账号和密码" }); return;
    }
    this.setData({ loading: true, error: "" });
    try {
      const login = await new Promise((resolve, reject) => wx.login({ success: resolve, fail: reject }));
      const result = await postJson("/api/staff-miniapp/login", {
        username: this.data.username, password: this.data.password, code: login.code
      });
      wx.setStorageSync("STAFF_TOKEN", result.token || "");
      wx.setStorageSync("STAFF_PROFILE", result.profile || {});
      const app = getApp();
      if (app && typeof app.startStaffReminderPolling === "function") app.startStaffReminderPolling(true);
      wx.reLaunch({ url: "/pages/staff/today/today" });
    } catch (e) {
      const message = e && (e.detail || (e.data && e.data.detail) || e.errMsg);
      this.setData({ error: message || "登录失败，请重试" });
    } finally { this.setData({ loading: false }); }
  },
  backToCustomer() { wx.reLaunch({ url: "/pages/index/index" }); }
});

const { staffGet, staffPost } = require("../../../utils/api");

Page({
  data: { loading: true, error: "", profile: {} },
  onShow() { this.loadProfile(); },
  async loadProfile() {
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/me");
      this.setData({ profile: result.profile || {} });
      wx.setStorageSync("STAFF_PROFILE", result.profile || {});
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "员工信息加载失败" });
    } finally { this.setData({ loading: false }); }
  },
  async logout() {
    const confirmed = await new Promise((resolve) => wx.showModal({
      title: "退出员工端", content: "退出后需要重新输入员工账号和密码。",
      success: (res) => resolve(!!res.confirm), fail: () => resolve(false)
    }));
    if (!confirmed) return;
    try { await staffPost("/api/staff-miniapp/logout"); } catch (e) {}
    try { wx.removeStorageSync("STAFF_TOKEN"); wx.removeStorageSync("STAFF_PROFILE"); } catch (e) {}
    wx.reLaunch({ url: "/pages/index/index" });
  },
  goCustomerService() { wx.reLaunch({ url: "/pages/index/index" }); },
  goToday() { wx.redirectTo({ url: "/pages/staff/today/today" }); },
  goCustomers() { wx.redirectTo({ url: "/pages/staff/customers/customers" }); },
  goMe() {}
});

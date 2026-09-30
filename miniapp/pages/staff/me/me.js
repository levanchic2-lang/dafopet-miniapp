const { staffGet, staffPost } = require("../../../utils/api");

Page({
  data: { loading: true, error: "", profile: {}, reminderBusy: false, reminderAuthorized: false },
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
  async enableMedicationReminder() {
    const templateId = (this.data.profile.medication_reminder_template_id || "").trim();
    if (!templateId || this.data.reminderBusy) {
      wx.showToast({ title: "提醒模板尚未配置", icon: "none" });
      return;
    }
    this.setData({ reminderBusy: true });
    try {
      const result = await new Promise((resolve, reject) => wx.requestSubscribeMessage({
        tmplIds: [templateId], success: resolve, fail: reject
      }));
      const status = result && result[templateId];
      if (status !== "accept" && status !== "acceptWithAudio") throw new Error("未允许接收服务通知");
      this.setData({ reminderAuthorized: true });
      wx.showToast({ title: "微信提醒已授权", icon: "success" });
    } catch (e) {
      wx.showToast({ title: (e && (e.errMsg || e.message)) || "授权未完成", icon: "none" });
    } finally {
      this.setData({ reminderBusy: false });
    }
  },
  goCustomerService() { wx.reLaunch({ url: "/pages/index/index?staff_bypass=1" }); },
  goToday() { wx.redirectTo({ url: "/pages/staff/today/today" }); },
  goCalendar() { wx.redirectTo({ url: "/pages/staff/calendar/calendar" }); },
  goCustomers() { wx.redirectTo({ url: "/pages/staff/customers/customers" }); },
  goMe() {}
});

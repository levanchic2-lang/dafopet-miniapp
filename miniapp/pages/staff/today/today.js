const { staffGet } = require("../../../utils/api");

Page({
  data: { loading: true, error: "", profile: {}, dateLabel: "", stats: {}, tasks: [], next: null },
  onShow() { this.loadData(); },
  async loadData() {
    let token = "";
    try { token = wx.getStorageSync("STAFF_TOKEN") || ""; } catch (e) {}
    if (!token) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/dashboard");
      const parts = (result.date || "").split("-");
      this.setData({
        profile: result.profile || {}, stats: result.stats || {}, tasks: result.tasks || [],
        next: result.next_appointment || null,
        dateLabel: parts.length === 3 ? parts[1] + " / " + parts[2] : result.date || ""
      });
      wx.setStorageSync("STAFF_PROFILE", result.profile || {});
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "工作台加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  onPullDownRefresh() { this.loadData(); },
  goTnr() { wx.navigateTo({ url: "/pages/staff/tnr/tnr" }); },
  goToday() {},
  goCalendar() { wx.redirectTo({ url: "/pages/staff/calendar/calendar" }); },
  goCustomers() { wx.redirectTo({ url: "/pages/staff/customers/customers" }); },
  goMe() { wx.redirectTo({ url: "/pages/staff/me/me" }); }
});

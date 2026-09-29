const { staffGet } = require("../../../utils/api");

Page({
  data: { query: "", loading: true, error: "", items: [], profile: {} },
  onLoad() {
    try { this.setData({ profile: wx.getStorageSync("STAFF_PROFILE") || {} }); } catch (e) {}
    this.loadCustomers("");
  },
  onQuery(e) { this.setData({ query: e.detail.value || "" }); },
  onSearch() { this.loadCustomers((this.data.query || "").trim()); },
  onClear() { this.setData({ query: "" }); this.loadCustomers(""); },
  async loadCustomers(query) {
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/customers", { q: query || "" });
      this.setData({ items: result.items || [] });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "客户列表加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  openCustomer(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    if (id) wx.navigateTo({ url: "/pages/staff/customer/customer?id=" + id });
  },
  onPullDownRefresh() { this.loadCustomers((this.data.query || "").trim()); },
  goToday() { wx.redirectTo({ url: "/pages/staff/today/today" }); },
  goCalendar() { wx.redirectTo({ url: "/pages/staff/calendar/calendar" }); },
  goCustomers() {},
  goMe() { wx.redirectTo({ url: "/pages/staff/me/me" }); }
});

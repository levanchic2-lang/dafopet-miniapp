const { staffGet } = require("../../../utils/api");

Page({
  data: { loading: true, error: "", tab: "today", scope: "mine", category: "", items: [], counts: {}, today: "" },
  onShow() { this.load(); },
  async load() {
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/follow-ups", { tab: this.data.tab, scope: this.data.scope, category: this.data.category });
      this.setData({ items: result.items || [], counts: result.counts || {}, today: result.today || "" });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "随访任务加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  setTab(e) { this.setData({ tab: e.currentTarget.dataset.tab || "today", items: [] }); this.load(); },
  setScope(e) { this.setData({ scope: e.currentTarget.dataset.scope || "mine", items: [] }); this.load(); },
  setCategory(e) { this.setData({ category: e.currentTarget.dataset.category || "", items: [] }); this.load(); },
  openItem(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    if (id) wx.navigateTo({ url: "/pages/staff/follow-up/follow-up?id=" + id });
  },
  callCustomer(e) {
    const phone = e.currentTarget.dataset.phone || "";
    if (phone) wx.makePhoneCall({ phoneNumber: phone });
  },
  onPullDownRefresh() { this.load(); }
});

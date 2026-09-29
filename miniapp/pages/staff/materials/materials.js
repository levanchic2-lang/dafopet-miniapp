const { staffGet } = require("../../../utils/api");

Page({
  data: { loading: true, error: "", query: "", items: [] },
  onShow() { this.load(); },
  onPullDownRefresh() { this.load((this.data.query || "").trim()); },
  onQuery(e) { this.setData({ query: e.detail.value || "" }); },
  onSearch() { this.load((this.data.query || "").trim()); },
  onClear() { this.setData({ query: "" }); this.load(""); },
  async load(query = "") {
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/visit-materials", { q: query });
      this.setData({ items: result.items || [] });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "病例列表加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  openVisit(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    if (id) wx.navigateTo({ url: "/pages/staff/material/material?id=" + id });
  }
});

const { staffGet, staffPost } = require("../../../utils/api");

Page({
  data: { loading: true, busy: false, error: "", query: "", view: "open", monitors: [], visits: [] },
  onShow() { this.load(); },
  onPullDownRefresh() { this.load((this.data.query || "").trim()); },
  onQuery(e) { this.setData({ query: e.detail.value || "" }); },
  onSearch() { this.load((this.data.query || "").trim()); },
  onClear() { this.setData({ query: "" }); this.load(""); },
  setView(e) {
    const view = e.currentTarget.dataset.view === "closed" ? "closed" : "open";
    if (view === this.data.view) return;
    this.setData({ view, query: "" }); this.load("");
  },
  async load(q = "") {
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/anesthesia-monitors", { q, view: this.data.view });
      this.setData({ monitors: result.monitors || result.active || [], visits: result.visits || [] });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "麻醉病例加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  openMonitor(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    if (id) wx.navigateTo({ url: "/pages/staff/anesthesia-monitor/anesthesia-monitor?id=" + id });
  },
  startMonitor(e) {
    const visitId = Number(e.currentTarget.dataset.id || 0);
    const existing = Number(e.currentTarget.dataset.sheet || 0);
    if (existing) { this.openMonitor({ currentTarget: { dataset: { id: existing } } }); return; }
    if (!visitId || this.data.busy) return;
    wx.showModal({
      title: "开启麻醉监护", content: "将以当前时间作为麻醉开始时间。进入后可补充术式、ASA分级和术前用药。",
      confirmText: "开始监护", confirmColor: "#1d4d3a",
      success: async res => {
        if (!res.confirm) return;
        this.setData({ busy: true });
        try {
          const result = await staffPost(`/api/staff-miniapp/visits/${visitId}/anesthesia-monitor`, {});
          wx.navigateTo({ url: "/pages/staff/anesthesia-monitor/anesthesia-monitor?id=" + result.id });
        } catch (err) { wx.showModal({ title: "无法开启", content: (err && err.detail) || "请稍后重试", showCancel: false }); }
        finally { this.setData({ busy: false }); }
      }
    });
  }
});

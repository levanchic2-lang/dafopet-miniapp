const { staffGet, staffPost } = require("../../../utils/api");
const app = getApp();

const visitLabels = { outpatient: "门诊", followup: "复诊", postop: "术后", vaccine: "疫苗", surgery_consult: "手术", other: "其他" };
const wormLabels = { external: "体外驱虫", internal: "体内驱虫", combo: "内外同驱" };

Page({
  data: { id: 0, loading: true, opening: 0, error: "", activeTab: "visits", canConsultation: false, pet: {}, customer: {}, summary: {}, visits: [], prescriptions: [], reports: [], vaccinations: [], dewormings: [], groomings: [], invoices: [] },
  onLoad(options) { this.setData({ id: Number(options.id || 0) }); this.load(); },
  onShow() { if (this.data.id && !this.data.loading) this.load(); },
  async load() {
    if (!this.data.id) return;
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/pets/" + this.data.id);
      const visits = (result.visits || []).map(x => Object.assign({}, x, { type_label: visitLabels[x.type] || x.type }));
      const dewormings = (result.dewormings || []).map(x => Object.assign({}, x, { type_label: wormLabels[x.type] || "驱虫" }));
      this.setData({ canConsultation: !!(result.permissions && result.permissions.consultation), pet: result.pet || {}, customer: result.customer || {}, summary: result.summary || {}, visits, prescriptions: result.prescriptions || [], reports: result.reports || [], vaccinations: result.vaccinations || [], dewormings, groomings: result.groomings || [], invoices: result.invoices || [] });
      wx.setNavigationBarTitle({ title: (result.pet && result.pet.name) || "宠物档案" });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "宠物档案加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  setTab(e) { this.setData({ activeTab: e.currentTarget.dataset.tab }); },
  openGroomingOrder() { if (this.data.id) wx.navigateTo({ url: "/pages/staff/grooming-order/grooming-order?pet_id=" + this.data.id }); },
  openPreventionOrder(e) {
    if (!this.data.id) return;
    const mode = e.currentTarget.dataset.mode === "deworming" ? "deworming" : "vaccine";
    wx.navigateTo({ url: `/pages/staff/prevention-order/prevention-order?pet_id=${this.data.id}&mode=${mode}` });
  },
  openUnifiedOrder(e) { const id = Number(e.currentTarget.dataset.id || 0); if (id) wx.navigateTo({ url: "/pages/staff/unified-order/unified-order?id=" + id }); },
  openFollowupNew(e) { const id = Number(e.currentTarget.dataset.id || 0); if (id) wx.navigateTo({ url: "/pages/staff/follow-up-new/follow-up-new?id=" + id }); },
  openConsultation(e) { const id = Number(e.currentTarget.dataset.id || 0); if (id) wx.navigateTo({ url: "/pages/staff/consultation/consultation?id=" + id }); },
  openMaterials(e) { const id = Number(e.currentTarget.dataset.id || 0); if (id) wx.navigateTo({ url: "/pages/staff/material/material?id=" + id }); },
  openAnesthesia(e) {
    const id = Number(e.currentTarget.dataset.id || 0); if (!id || this.data.opening) return;
    wx.showModal({ title: "开启麻醉监护", content: "已有未结束监护时会直接继续。确认进入手术室流程？", confirmText: "进入监护", success: async res => { if (!res.confirm) return; this.setData({ opening: id }); try { const result = await staffPost(`/api/staff-miniapp/visits/${id}/anesthesia-monitor`, {}); wx.navigateTo({ url: "/pages/staff/anesthesia-monitor/anesthesia-monitor?id=" + result.id }); } catch (err) { wx.showToast({ title: (err && err.detail) || "无法开启", icon: "none" }); } finally { this.setData({ opening: 0 }); } } });
  },
  openReport(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const type = e.currentTarget.dataset.type || "pdf";
    if (!id || this.data.opening) return;
    let token = ""; try { token = wx.getStorageSync("STAFF_TOKEN") || ""; } catch (err) {}
    this.setData({ opening: id }); wx.showLoading({ title: "打开报告" });
    wx.downloadFile({
      url: app.globalData.apiBase + "/api/staff-miniapp/reports/" + id + "/file",
      header: { Authorization: "Bearer " + token },
      success: res => {
        if (res.statusCode !== 200) { wx.showToast({ title: "报告下载失败", icon: "none" }); return; }
        if (type === "image") wx.previewImage({ urls: [res.tempFilePath], current: res.tempFilePath });
        else wx.openDocument({ filePath: res.tempFilePath, showMenu: true, fail: () => wx.showToast({ title: "无法打开该报告", icon: "none" }) });
      },
      fail: () => wx.showToast({ title: "报告下载失败", icon: "none" }),
      complete: () => { wx.hideLoading(); this.setData({ opening: 0 }); }
    });
  },
  onPullDownRefresh() { this.load(); }
});

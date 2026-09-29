const { staffGet } = require("../../../utils/api");
const app = getApp();

const visitLabels = { outpatient: "门诊", followup: "复诊", postop: "术后", vaccine: "疫苗", surgery_consult: "手术", other: "其他" };
const wormLabels = { external: "体外驱虫", internal: "体内驱虫", combo: "内外同驱" };

Page({
  data: { id: 0, loading: true, opening: 0, error: "", activeTab: "visits", pet: {}, customer: {}, summary: {}, visits: [], prescriptions: [], reports: [], vaccinations: [], dewormings: [], invoices: [] },
  onLoad(options) { this.setData({ id: Number(options.id || 0) }); this.load(); },
  async load() {
    if (!this.data.id) return;
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/pets/" + this.data.id);
      const visits = (result.visits || []).map(x => Object.assign({}, x, { type_label: visitLabels[x.type] || x.type }));
      const dewormings = (result.dewormings || []).map(x => Object.assign({}, x, { type_label: wormLabels[x.type] || "驱虫" }));
      this.setData({ pet: result.pet || {}, customer: result.customer || {}, summary: result.summary || {}, visits, prescriptions: result.prescriptions || [], reports: result.reports || [], vaccinations: result.vaccinations || [], dewormings, invoices: result.invoices || [] });
      wx.setNavigationBarTitle({ title: (result.pet && result.pet.name) || "宠物档案" });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "宠物档案加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  setTab(e) { this.setData({ activeTab: e.currentTarget.dataset.tab }); },
  openMaterials(e) { const id = Number(e.currentTarget.dataset.id || 0); if (id) wx.navigateTo({ url: "/pages/staff/material/material?id=" + id }); },
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

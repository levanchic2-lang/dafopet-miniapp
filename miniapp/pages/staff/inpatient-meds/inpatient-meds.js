const { staffGet, staffPost } = require("../../../utils/api");

Page({
  data: {
    loading: true, busy: false, error: "", view: "pending",
    items: [], temporary: [], hospitalizations: [], inventory: [], filteredInventory: [], doctors: [],
    showTemporaryForm: false, drugQuery: "", selectedDrug: null,
    routes: ["静脉注射", "肌肉注射", "皮下注射", "口服", "外用", "滴眼", "其他"],
    selectedHospLabel: "", selectedDoctor: "", selectedRoute: "静脉注射",
    form: { hospitalizationIndex: 0, doctorIndex: 0, routeIndex: 0, dose_actual: "", notes: "" }
  },
  onShow() { this.loadData(); },
  onPullDownRefresh() { this.loadData(); },
  async loadData() {
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/inpatient-medications", { view: this.data.view });
      const inventory = result.inventory || [];
      this.setData({
        items: result.items || [], temporary: result.temporary || [],
        hospitalizations: result.hospitalizations || [], inventory,
        filteredInventory: inventory.slice(0, 20), doctors: result.doctors || [],
        selectedHospLabel: (result.hospitalizations || [])[0] ? result.hospitalizations[0].label : "",
        selectedDoctor: (result.doctors || [])[0] || "",
        selectedRoute: this.data.routes[0]
      });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "住院用药加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  setView(e) {
    const view = e.currentTarget.dataset.view;
    if (!view || view === this.data.view) return;
    this.setData({ view, showTemporaryForm: false, selectedDrug: null, drugQuery: "" }, () => this.loadData());
  },
  completeTask(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const dose = e.currentTarget.dataset.dose || "";
    if (!id || this.data.busy) return;
    wx.showModal({
      title: "确认已经给药", content: dose ? `实际剂量按处方：${dose}` : "确认该次用药已经执行？",
      confirmText: "确认给药", confirmColor: "#1d4d3a",
      success: async res => {
        if (!res.confirm) return;
        this.setData({ busy: true });
        try {
          await staffPost(`/api/staff-miniapp/inpatient-medications/${id}/check`, { dose_actual: dose });
          wx.showToast({ title: "已完成", icon: "success" }); this.loadData();
        } catch (err) { wx.showModal({ title: "操作失败", content: (err && err.detail) || "请稍后重试", showCancel: false }); }
        finally { this.setData({ busy: false }); }
      }
    });
  },
  skipTask(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    if (!id || this.data.busy) return;
    wx.showModal({
      title: "跳过本次用药", editable: true, placeholderText: "必须填写原因，例如：动物拒绝、医生停药",
      confirmText: "确认跳过", confirmColor: "#7a2828",
      success: async res => {
        if (!res.confirm) return;
        const notes = (res.content || "").trim();
        if (!notes) { wx.showToast({ title: "请填写原因", icon: "none" }); return; }
        this.setData({ busy: true });
        try { await staffPost(`/api/staff-miniapp/inpatient-medications/${id}/skip`, { notes }); this.loadData(); }
        catch (err) { wx.showModal({ title: "操作失败", content: (err && err.detail) || "请稍后重试", showCancel: false }); }
        finally { this.setData({ busy: false }); }
      }
    });
  },
  undoTask(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    if (!id || this.data.busy) return;
    wx.showModal({ title: "撤销执行记录", content: "撤销后会重新回到待执行列表。", confirmText: "确认撤销", success: async res => {
      if (!res.confirm) return;
      this.setData({ busy: true });
      try { await staffPost(`/api/staff-miniapp/inpatient-medications/${id}/uncheck`, {}); this.loadData(); }
      catch (err) { wx.showModal({ title: "操作失败", content: (err && err.detail) || "请稍后重试", showCancel: false }); }
      finally { this.setData({ busy: false }); }
    }});
  },
  openTemporaryForm() {
    if (!this.data.hospitalizations.length) { wx.showToast({ title: "当前没有住院动物", icon: "none" }); return; }
    this.setData({ showTemporaryForm: true });
  },
  closeTemporaryForm() { this.setData({ showTemporaryForm: false, selectedDrug: null, drugQuery: "" }); },
  onDrugQuery(e) {
    const q = (e.detail.value || "").trim().toLowerCase();
    const rows = this.data.inventory.filter(item => !q || item.name.toLowerCase().indexOf(q) >= 0).slice(0, 20);
    this.setData({ drugQuery: e.detail.value, filteredInventory: rows, selectedDrug: null });
  },
  selectDrug(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const item = this.data.inventory.find(row => row.id === id) || null;
    this.setData({ selectedDrug: item, drugQuery: item ? item.name : "", filteredInventory: [] });
  },
  onHospChange(e) { const i = Number(e.detail.value || 0); this.setData({ "form.hospitalizationIndex": i, selectedHospLabel: (this.data.hospitalizations[i] || {}).label || "" }); },
  onDoctorChange(e) { const i = Number(e.detail.value || 0); this.setData({ "form.doctorIndex": i, selectedDoctor: this.data.doctors[i] || "" }); },
  onRouteChange(e) { const i = Number(e.detail.value || 0); this.setData({ "form.routeIndex": i, selectedRoute: this.data.routes[i] || "" }); },
  onDoseInput(e) { this.setData({ "form.dose_actual": e.detail.value }); },
  onNotesInput(e) { this.setData({ "form.notes": e.detail.value }); },
  async submitTemporary() {
    if (this.data.busy) return;
    const hosp = this.data.hospitalizations[this.data.form.hospitalizationIndex];
    const doctor = this.data.doctors[this.data.form.doctorIndex];
    const route = this.data.routes[this.data.form.routeIndex];
    const dose = (this.data.form.dose_actual || "").trim();
    if (!hosp || !this.data.selectedDrug || !doctor || !route || !dose) {
      wx.showToast({ title: "请填写全部必填项", icon: "none" }); return;
    }
    this.setData({ busy: true });
    try {
      const result = await staffPost("/api/staff-miniapp/inpatient-medications/temporary", {
        hospitalization_id: hosp.id, inventory_item_id: this.data.selectedDrug.id,
        dose_actual: dose, route, ordered_by: doctor, notes: this.data.form.notes || ""
      });
      wx.showModal({ title: "临时用药已记录", content: result.message || "等待医生补开正式处方", showCancel: false });
      this.setData({ showTemporaryForm: false, selectedDrug: null, drugQuery: "", "form.dose_actual": "", "form.notes": "" });
      this.loadData();
    } catch (err) { wx.showModal({ title: "记录失败", content: (err && err.detail) || "请稍后重试", showCancel: false }); }
    finally { this.setData({ busy: false }); }
  },
  voidTemporary(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    wx.showModal({ title: "作废临时用药", content: "仅用于误录。作废后不会进入待补处方。", confirmText: "确认作废", confirmColor: "#7a2828", success: async res => {
      if (!res.confirm) return;
      try { await staffPost(`/api/staff-miniapp/inpatient-medications/temporary/${id}/void`, {}); this.loadData(); }
      catch (err) { wx.showModal({ title: "作废失败", content: (err && err.detail) || "请稍后重试", showCancel: false }); }
    }});
  }
});

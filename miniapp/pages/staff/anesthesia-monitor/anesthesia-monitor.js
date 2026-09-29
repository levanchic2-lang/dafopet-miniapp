const { staffGet, staffPost } = require("../../../utils/api");
const app = getApp();

function nowHm() {
  const d = new Date();
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

Page({
  data: {
    id: 0, loading: true, busy: false, error: "", tab: "monitor", showSetup: false, showOpenVial: false,
    sheet: {}, header: {}, vital: { time_hhmm: "", hr: "", rr: "", spo2: "", etco2: "", temperature_c: "", bp_sys: "", bp_dia: "", bp_map: "", agent_pct: "", o2_flow: "", depth: "", event: "" },
    medication: { open_vial_id: 0, phase: "premedication", dose_text: "", qty: "", route: "IV", time_hhmm: "", note: "" },
    vial: { item_id: 0, batch_id: 0, opened_qty: "", notes: "" }, inventoryIndex: 0, batchIndex: 0, vialIndex: 0, currentBatches: [],
    recovery: { end_time: "", extubation_time: "", recovery_status: "", recovery_notes: "" }, reminderText: "", reminderLate: false
  },
  onLoad(options) { this.setData({ id: Number(options.id || 0) }); this.load(); },
  onShow() { this.startClock(); },
  onHide() { this.stopClock(); },
  onUnload() { this.stopClock(); },
  onPullDownRefresh() { this.load(); },
  startClock() { this.stopClock(); this.clock = setInterval(() => this.updateReminder(), 15000); this.updateReminder(); },
  stopClock() { if (this.clock) clearInterval(this.clock); this.clock = null; },
  updateReminder() {
    const due = (this.data.sheet && this.data.sheet.next_due_time) || "";
    if (!due || this.data.sheet.status === "closed") { this.setData({ reminderText: "", reminderLate: false }); return; }
    const parts = due.split(":").map(Number); const d = new Date(); const dueMin = parts[0] * 60 + parts[1]; const nowMin = d.getHours() * 60 + d.getMinutes();
    const late = nowMin >= dueMin;
    this.setData({ reminderText: late ? `应在 ${due} 记录，请现在记录` : `下一次记录 ${due}`, reminderLate: late });
    if (late && !this.didVibrateForDue) { this.didVibrateForDue = due; wx.vibrateShort({ type: "medium" }); }
    if (!late) this.didVibrateForDue = "";
  },
  async load() {
    if (!this.data.id) return;
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet(`/api/staff-miniapp/anesthesia-monitors/${this.data.id}`); const s = result.sheet || {};
      this.setData({ sheet: s, header: { procedure: s.procedure || "", anesthetist: s.anesthetist || "", surgeon: s.surgeon || "", asa_grade: s.asa_grade || "", agent: s.agent || "", weight_kg: s.weight_kg || "", start_time: s.start_time || "", notes: s.notes || "" }, recovery: { end_time: s.end_time || nowHm(), extubation_time: s.extubation_time || "", recovery_status: s.recovery_status || "", recovery_notes: s.recovery_notes || "" }, medication: Object.assign({}, this.data.medication, { time_hhmm: nowHm(), open_vial_id: s.open_vials && s.open_vials.length ? s.open_vials[0].id : 0 }), vialIndex: 0 });
      wx.setNavigationBarTitle({ title: (s.pet && s.pet.name ? s.pet.name + " · 麻醉监护" : "麻醉监护") }); this.updateReminder();
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "麻醉监护加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  setTab(e) { this.setData({ tab: e.currentTarget.dataset.tab }); },
  toggleSetup() { this.setData({ showSetup: !this.data.showSetup }); },
  toggleOpenVial() { this.setData({ showOpenVial: !this.data.showOpenVial }); },
  field(e, group) { const key = e.currentTarget.dataset.field; this.setData({ [`${group}.${key}`]: e.detail.value }); },
  headerField(e) { this.field(e, "header"); }, vitalField(e) { this.field(e, "vital"); }, medField(e) { this.field(e, "medication"); }, vialField(e) { this.field(e, "vial"); }, recoveryField(e) { this.field(e, "recovery"); },
  setDepth(e) { const value = e.currentTarget.dataset.value; this.setData({ "vital.depth": this.data.vital.depth === value ? "" : value }); },
  setPhase(e) { this.setData({ "medication.phase": e.currentTarget.dataset.value || "intraoperative" }); },
  setRecoveryStatus(e) { this.setData({ "recovery.recovery_status": e.currentTarget.dataset.value || "" }); },
  selectInventory(e) {
    const index = Number(e.detail.value || 0); const item = (this.data.sheet.inventory || [])[index] || {}; const batches = item.batches || [];
    this.setData({ inventoryIndex: index, currentBatches: batches, batchIndex: 0, "vial.item_id": item.id || 0, "vial.batch_id": batches.length ? batches[0].id : 0, "vial.opened_qty": item.unit2_ratio || 1 });
  },
  selectBatch(e) { const index = Number(e.detail.value || 0); const row = this.data.currentBatches[index] || {}; this.setData({ batchIndex: index, "vial.batch_id": row.id || 0 }); },
  selectVial(e) { const index = Number(e.detail.value || 0); const row = (this.data.sheet.open_vials || [])[index] || {}; this.setData({ vialIndex: index, "medication.open_vial_id": row.id || 0 }); },
  async saveHeader() { await this.submit(`/api/staff-miniapp/anesthesia-monitors/${this.data.id}/header`, this.data.header, "表头已保存", () => this.setData({ showSetup: false })); },
  async openVial() {
    if (!this.data.vial.item_id) { wx.showToast({ title: "请选择药品", icon: "none" }); return; }
    await this.submit(`/api/staff-miniapp/anesthesia-monitors/${this.data.id}/open-vial`, this.data.vial, "开瓶已登记", () => this.setData({ showOpenVial: false, vial: { item_id: 0, batch_id: 0, opened_qty: "", notes: "" } }));
  },
  async recordMedication() {
    if (!this.data.medication.open_vial_id || !this.data.medication.qty) { wx.showToast({ title: "请选择开瓶并填写出库量", icon: "none" }); return; }
    await this.submit(`/api/staff-miniapp/anesthesia-monitors/${this.data.id}/medications`, this.data.medication, "给药已记录", () => this.setData({ medication: Object.assign({}, this.data.medication, { dose_text: "", qty: "", note: "", time_hhmm: nowHm() }) }));
  },
  async recordVital() {
    await this.submit(`/api/staff-miniapp/anesthesia-monitors/${this.data.id}/entries`, this.data.vital, "体征已记录", () => { this.didVibrateForDue = ""; this.setData({ vital: { time_hhmm: "", hr: "", rr: "", spo2: "", etco2: "", temperature_c: "", bp_sys: "", bp_dia: "", bp_map: "", agent_pct: "", o2_flow: "", depth: "", event: "" } }); });
  },
  finish() {
    if (!this.data.recovery.recovery_status) { wx.showToast({ title: "请选择复苏状态", icon: "none" }); return; }
    wx.showModal({ title: "结束麻醉监护", content: "结束后将停止五分钟提醒，并生成完整监护记录。", confirmText: "确认结束", confirmColor: "#7c302c", success: async res => { if (res.confirm) await this.submit(`/api/staff-miniapp/anesthesia-monitors/${this.data.id}/finish`, this.data.recovery, "监护已结束"); } });
  },
  async reopen() { await this.submit(`/api/staff-miniapp/anesthesia-monitors/${this.data.id}/reopen`, {}, "已重新开启"); },
  async submit(path, payload, successText, after) {
    if (this.data.busy) return false; this.setData({ busy: true });
    try { await staffPost(path, payload); if (after) after(); wx.showToast({ title: successText, icon: "success" }); await this.load(); return true; }
    catch (e) { wx.showModal({ title: "操作未完成", content: (e && (e.detail || e.errMsg)) || "请稍后重试", showCancel: false }); return false; }
    finally { this.setData({ busy: false }); }
  },
  openPdf() {
    const token = wx.getStorageSync("STAFF_TOKEN") || ""; wx.showLoading({ title: "生成PDF", mask: true });
    wx.downloadFile({ url: `${app.globalData.apiBase}/api/staff-miniapp/anesthesia-monitors/${this.data.id}/pdf`, header: { Authorization: `Bearer ${token}` }, success: res => { if (res.statusCode === 200) wx.openDocument({ filePath: res.tempFilePath, fileType: "pdf", showMenu: true }); else wx.showToast({ title: "PDF生成失败", icon: "none" }); }, fail: () => wx.showToast({ title: "PDF下载失败", icon: "none" }), complete: () => wx.hideLoading() });
  }
});

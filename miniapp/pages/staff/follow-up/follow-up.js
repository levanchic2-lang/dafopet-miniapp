const { staffGet, staffPost } = require("../../../utils/api");

const outcomes = [
  { value: "recovered", label: "恢复良好" },
  { value: "improved", label: "有所改善" },
  { value: "unchanged", label: "无明显改善" },
  { value: "worse", label: "情况加重" },
  { value: "no_reply", label: "未联系上" },
  { value: "no_need", label: "拒绝或无需继续" }
];
const actions = [
  { value: "finish", label: "结束随访" },
  { value: "reschedule", label: "延期联系" },
  { value: "doctor", label: "转医生" },
  { value: "revisit", label: "建议复诊" },
  { value: "appointment", label: "创建预约" },
  { value: "revisited", label: "已经复诊" }
];

function isoAfter(days) {
  const d = new Date(); d.setDate(d.getDate() + days);
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

Page({
  data: {
    id: 0, loading: true, busy: false, error: "", item: {}, outcomes, actions,
    outcome: "", nextAction: "", note: "", nextDate: "", appointmentDate: "",
    appointmentTime: "09:00", closeVisit: false, closeDecision: "", closeFollowupDate: ""
  },
  onLoad(options) {
    this.setData({ id: Number(options.id || 0), nextDate: isoAfter(2), appointmentDate: isoAfter(1), closeFollowupDate: isoAfter(3) });
    this.load();
  },
  async load() {
    if (!this.data.id) return;
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet(`/api/staff-miniapp/follow-ups/${this.data.id}`);
      const item = result.item || {};
      this.setData({ item, outcome: item.staff_outcome || "", nextAction: item.next_action || "", note: item.handle_note || "" });
      wx.setNavigationBarTitle({ title: `${(item.pet && item.pet.name) || "宠物"} · 随访` });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "随访任务读取失败" });
    } finally { this.setData({ loading: false }); }
  },
  chooseOutcome(e) {
    const outcome = e.currentTarget.dataset.value || "";
    this.setData({ outcome, closeVisit: outcome === "recovered" ? this.data.closeVisit : false, closeDecision: outcome === "recovered" ? this.data.closeDecision : "" });
  },
  chooseAction(e) { this.setData({ nextAction: e.currentTarget.dataset.value || "" }); },
  onNote(e) { this.setData({ note: e.detail.value || "" }); },
  onNextDate(e) { this.setData({ nextDate: e.detail.value || "" }); },
  onAppointmentDate(e) { this.setData({ appointmentDate: e.detail.value || "" }); },
  onAppointmentTime(e) { this.setData({ appointmentTime: e.detail.value || "" }); },
  onCloseVisit(e) { this.setData({ closeVisit: !!e.detail.value, closeDecision: e.detail.value ? this.data.closeDecision : "" }); },
  chooseCloseDecision(e) { this.setData({ closeDecision: e.currentTarget.dataset.value || "" }); },
  onCloseFollowupDate(e) { this.setData({ closeFollowupDate: e.detail.value || "" }); },
  callCustomer() {
    const phone = (this.data.item.customer || {}).phone || "";
    if (phone) wx.makePhoneCall({ phoneNumber: phone });
  },
  async submit() {
    if (this.data.busy) return;
    if (!this.data.outcome) { wx.showToast({ title: "请选择随访结果", icon: "none" }); return; }
    if (!this.data.nextAction) { wx.showToast({ title: "请选择下一步", icon: "none" }); return; }
    if (this.data.nextAction === "reschedule" && !this.data.nextDate) { wx.showToast({ title: "请选择再次联系日期", icon: "none" }); return; }
    if (this.data.closeVisit && !this.data.closeDecision) { wx.showToast({ title: "请选择后续是否随访", icon: "none" }); return; }
    if (this.data.closeVisit && this.data.closeDecision === "keep" && !(this.data.item.future_count > 0) && !this.data.closeFollowupDate) {
      wx.showToast({ title: "请选择下一次随访日期", icon: "none" }); return;
    }
    const confirmed = await new Promise(resolve => wx.showModal({
      title: this.data.closeVisit ? "完成随访并结束病历" : "确认处理结果",
      content: this.data.closeVisit ? "本次操作会同时结束关联病历，并记录当前员工账号。病历结束后不可修改。" : "确认提交本次随访结果？",
      confirmText: "确认提交", success: res => resolve(!!res.confirm), fail: () => resolve(false)
    }));
    if (!confirmed) return;
    this.setData({ busy: true, error: "" });
    try {
      let appointmentId = 0;
      if (this.data.nextAction === "appointment") {
        if (!this.data.appointmentDate || !this.data.appointmentTime) throw { detail: "请选择复诊预约日期和时间" };
        const item = this.data.item;
        const created = await staffPost("/api/staff-miniapp/appointments", {
          category: "outpatient", service_name: "复诊",
          customer_id: item.customer.id, pet_id: item.pet.id,
          appointment_date: this.data.appointmentDate, appointment_time: this.data.appointmentTime,
          duration_minutes: 30, notes: `由随访任务 #${item.id} 创建`
        });
        appointmentId = Number((created.appointment || {}).id || 0);
      }
      await staffPost(`/api/staff-miniapp/follow-ups/${this.data.id}/handle`, {
        outcome: this.data.outcome, next_action: this.data.nextAction, note: this.data.note,
        next_date: this.data.nextDate, appointment_id: appointmentId,
        close_visit: this.data.closeVisit,
        close_followup_decision: this.data.closeDecision,
        close_followup_date: this.data.closeFollowupDate
      });
      wx.showToast({ title: "随访已记录", icon: "success" });
      setTimeout(() => wx.navigateBack(), 600);
    } catch (e) {
      this.setData({ error: (e && (e.detail || e.errMsg)) || "随访处理失败" });
      wx.showModal({ title: "未能提交", content: (e && (e.detail || e.errMsg)) || "请稍后重试", showCancel: false });
    } finally { this.setData({ busy: false }); }
  }
});

const { staffGet, staffPost } = require("../../../utils/api");

const kinds = [
  { value: "case", label: "病例回访" }, { value: "surgery", label: "手术随访" },
  { value: "discharge", label: "出院随访" }, { value: "chronic", label: "慢病复查" },
  { value: "screening", label: "健康筛查" }, { value: "recall", label: "客户唤回" }
];
function isoAfter(days) { const d = new Date(); d.setDate(d.getDate() + days); const p = n => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`; }

Page({
  data: { id: 0, loading: true, busy: false, error: "", visit: {}, pet: {}, customer: {}, kinds, kind: "case", plannedDate: "", title: "", question: "", reason: "", priority: "normal" },
  onLoad(options) { this.setData({ id: Number(options.id || 0), plannedDate: isoAfter(2) }); this.load(); },
  async load() {
    try {
      const result = await staffGet(`/api/staff-miniapp/visits/${this.data.id}/follow-up-context`);
      this.setData({ visit: result.visit || {}, pet: result.pet || {}, customer: result.customer || {}, loading: false });
      wx.setNavigationBarTitle({ title: `${(result.pet || {}).name || "宠物"} · 安排随访` });
    } catch (e) { this.setData({ loading: false, error: (e && (e.detail || e.errMsg)) || "病例读取失败" }); }
  },
  chooseKind(e) { this.setData({ kind: e.currentTarget.dataset.value || "case" }); },
  onDate(e) { this.setData({ plannedDate: e.detail.value || "" }); },
  onTitle(e) { this.setData({ title: e.detail.value || "" }); },
  onQuestion(e) { this.setData({ question: e.detail.value || "" }); },
  onReason(e) { this.setData({ reason: e.detail.value || "" }); },
  choosePriority(e) { this.setData({ priority: e.currentTarget.dataset.value || "normal" }); },
  async submit() {
    if (this.data.busy) return;
    if (!this.data.plannedDate) { wx.showToast({ title: "请选择随访日期", icon: "none" }); return; }
    this.setData({ busy: true, error: "" });
    try {
      const result = await staffPost(`/api/staff-miniapp/visits/${this.data.id}/follow-ups`, {
        kind: this.data.kind, planned_date: this.data.plannedDate, title: this.data.title,
        question: this.data.question, reason: this.data.reason, priority: this.data.priority
      });
      wx.showToast({ title: "随访已安排", icon: "success" });
      setTimeout(() => wx.redirectTo({ url: `/pages/staff/follow-up/follow-up?id=${result.item.id}` }), 500);
    } catch (e) { this.setData({ error: (e && (e.detail || e.errMsg)) || "随访安排失败" }); }
    finally { this.setData({ busy: false }); }
  }
});

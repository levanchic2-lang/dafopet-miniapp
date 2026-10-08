const { staffGet, staffPost } = require("../../../utils/api");

function isoAfter(days) { const d = new Date(); d.setDate(d.getDate() + days); const p = n => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`; }

Page({
  data: { id: 0, loading: true, busy: false, error: "", visit: {}, pet: {}, customer: {}, kind: "case", templates: [], templateIndex: -1, selectedTemplate: {}, assignees: [], assigneeIndex: -1, assignedTo: "", plannedDate: "", title: "", question: "", reason: "", priority: "normal" },
  onLoad(options) { this.setData({ id: Number(options.id || 0), plannedDate: isoAfter(2) }); this.load(); },
  async load() {
    try {
      const result = await staffGet(`/api/staff-miniapp/visits/${this.data.id}/follow-up-context`);
      const assignees = result.assignees || [];
      let assigneeIndex = assignees.findIndex(row => row.value === result.current_username);
      if (assigneeIndex < 0 && assignees.length === 1) assigneeIndex = 0;
      this.setData({ visit: result.visit || {}, pet: result.pet || {}, customer: result.customer || {}, templates: result.templates || [], assignees, assigneeIndex, assignedTo: assigneeIndex >= 0 ? assignees[assigneeIndex].value : "", loading: false });
      wx.setNavigationBarTitle({ title: `${(result.pet || {}).name || "宠物"} · 安排随访` });
    } catch (e) { this.setData({ loading: false, error: (e && (e.detail || e.errMsg)) || "病例读取失败" }); }
  },
  onTemplate(e) {
    const index = Number(e.detail.value);
    const selectedTemplate = this.data.templates[index] || {};
    this.setData({
      templateIndex: index,
      selectedTemplate,
      kind: selectedTemplate.kind || "case",
      plannedDate: selectedTemplate.day_offset ? isoAfter(Number(selectedTemplate.day_offset)) : this.data.plannedDate
    });
  },
  onDate(e) { this.setData({ plannedDate: e.detail.value || "" }); },
  onAssignee(e) { const index = Number(e.detail.value); const row = this.data.assignees[index] || {}; this.setData({ assigneeIndex: index, assignedTo: row.value || "" }); },
  onTitle(e) { this.setData({ title: e.detail.value || "" }); },
  onQuestion(e) { this.setData({ question: e.detail.value || "" }); },
  onReason(e) { this.setData({ reason: e.detail.value || "" }); },
  choosePriority(e) { this.setData({ priority: e.currentTarget.dataset.value || "normal" }); },
  async submit() {
    if (this.data.busy) return;
    if (this.data.templateIndex < 0) { wx.showToast({ title: "请选择随访模板", icon: "none" }); return; }
    if (!this.data.assignedTo) { wx.showToast({ title: "请选择负责人", icon: "none" }); return; }
    if (!this.data.plannedDate) { wx.showToast({ title: "请选择随访日期", icon: "none" }); return; }
    this.setData({ busy: true, error: "" });
    try {
      const result = await staffPost(`/api/staff-miniapp/visits/${this.data.id}/follow-ups`, {
        kind: this.data.kind, planned_date: this.data.plannedDate, title: this.data.title,
        question: this.data.question, reason: this.data.reason, priority: this.data.priority,
        template_id: this.data.selectedTemplate.template_id,
        template_round: this.data.selectedTemplate.round_no, assigned_to: this.data.assignedTo
      });
      wx.showToast({ title: "随访已安排", icon: "success" });
      setTimeout(() => wx.redirectTo({ url: `/pages/staff/follow-up/follow-up?id=${result.item.id}` }), 500);
    } catch (e) { this.setData({ error: (e && (e.detail || e.errMsg)) || "随访安排失败" }); }
    finally { this.setData({ busy: false }); }
  }
});

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
const scaleOptions = [1, 2, 3, 4, 5];
const uploadOptions = ["已收到", "未收到", "不适用"];

function buildRecommendation(rows) {
  const answered = rows.filter(row => row.value !== "" && row.value !== null && row.value !== undefined && (!Array.isArray(row.value) || row.value.length));
  if (!answered.length) return {};
  const redTerms = ["加重", "频繁", "喷射", "便血", "排尿困难", "嚎叫", "裂开", "化脓", "张口呼吸", "发紫", "紧急", "立即就诊", "完全没喂", "完全没拉", "严重", "新皮损", "带血鼻涕"];
  const amberTerms = ["无变化", "无明显变化", "明显急促", "红肿热痛", "血尿", "量少", "脓性鼻涕", "经常漏", "宠物拒食", "2-3次", "希望线上咨询", "需要复诊", "未排便", "少量渗液"];
  const positiveTerms = ["正常", "明显改善", "有所改善", "干燥愈合", "完全按时", "不需要，已好转", "无"];
  const red = [], amber = [];
  let positive = 0, explicitRevisit = false;
  answered.forEach(row => {
    const values = Array.isArray(row.value) ? row.value : [row.value];
    values.forEach(value => {
      const number = Number(value);
      if (["spirit", "appetite", "water", "energy"].includes(row.key) && Number.isFinite(number)) {
        if (number <= 2) red.push(`${row.label} ${number}/5`); else if (number === 3) amber.push(`${row.label} ${number}/5`); else positive += 1;
        return;
      }
      if (row.key === "itch" && Number.isFinite(number)) {
        if (number >= 4) red.push(`${row.label} ${number}/5`); else if (number === 3) amber.push(`${row.label} ${number}/5`); else positive += 1;
        return;
      }
      const text = String(value || "");
      if (row.key === "med_side" && !["", "无", "没有", "无明显不良反应"].includes(text.trim())) amber.push(row.label);
      if (text.includes("需要复诊") && !text.includes("不需要")) explicitRevisit = true;
      if (redTerms.some(term => text.includes(term))) red.push(`${row.label}：${text}`);
      else if (amberTerms.some(term => text.includes(term))) amber.push(`${row.label}：${text}`);
      else if (positiveTerms.some(term => term === text)) positive += 1;
    });
  });
  if (red.length) return { level: "urgent", outcome: "worse", action: "doctor", message: "发现警示项，建议立即转医生判断，必要时通知客户尽快复诊。", signals: red.slice(0, 4) };
  if (explicitRevisit) return { level: "warning", outcome: "unchanged", action: "revisit", message: "客户表达复诊需求，建议直接转为复诊安排。", signals: amber.slice(0, 4) };
  if (amber.length) return { level: "warning", outcome: "unchanged", action: "doctor", message: "存在需要关注的项目，建议转医生判断是否复诊或调整方案。", signals: amber.slice(0, 4) };
  if (positive) return { level: "good", outcome: "recovered", action: "finish", message: "目前未发现明显警示项，可结合沟通情况结束本次随访。", signals: [] };
  return { level: "neutral", outcome: "improved", action: "reschedule", message: "信息不足以判断完全恢复，建议结合沟通情况决定是否延期观察。", signals: [] };
}

function hydrateQuestion(question, value) {
  const row = { ...question, value };
  row.scale_choices = scaleOptions.map(number => ({ value: number, selected: Number(value) === number }));
  const sourceOptions = question.type === "upload" ? uploadOptions : (question.options || []);
  row.choice_options = sourceOptions.map(label => ({
    label,
    selected: question.type === "multi" ? (Array.isArray(value) && value.includes(label)) : value === label
  }));
  return row;
}

function hydrateRecommendation(recommendation) {
  const result = recommendation && typeof recommendation === "object" ? { ...recommendation } : {};
  result.signals_text = Array.isArray(result.signals) ? result.signals.join("；") : "";
  return result;
}

function isoAfter(days) {
  const d = new Date(); d.setDate(d.getDate() + days);
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

Page({
  data: {
    id: 0, loading: true, busy: false, error: "", item: {}, outcomes, actions,
    outcome: "", nextAction: "", note: "", nextDate: "", appointmentDate: "",
    appointmentTime: "09:00", closeVisit: false, scaleOptions, uploadOptions,
    questionRows: [], structuredAnswers: {}, recommendation: {}, manualOutcome: false, manualAction: false
  },
  onLoad(options) {
    this.setData({ id: Number(options.id || 0), nextDate: isoAfter(2), appointmentDate: isoAfter(1) });
    this.load();
  },
  async load() {
    if (!this.data.id) return;
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet(`/api/staff-miniapp/follow-ups/${this.data.id}`);
      const item = result.item || {};
      const savedAnswers = item.staff_answers || {};
      const questionRows = (item.questions || []).map(q => hydrateQuestion(q, savedAnswers[q.key] !== undefined ? savedAnswers[q.key] : (q.type === "multi" ? [] : "")));
      this.setData({ item, questionRows, structuredAnswers: savedAnswers, recommendation: hydrateRecommendation(item.recommendation), outcome: item.staff_outcome || "", nextAction: item.next_action || "", note: item.handle_note || "" });
      wx.setNavigationBarTitle({ title: `${(item.pet && item.pet.name) || "宠物"} · 随访` });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "随访任务读取失败" });
    } finally { this.setData({ loading: false }); }
  },
  chooseOutcome(e) {
    const outcome = e.currentTarget.dataset.value || "";
    this.setData({ outcome, closeVisit: outcome === "recovered" ? this.data.closeVisit : false, manualOutcome: true });
  },
  chooseAction(e) { this.setData({ nextAction: e.currentTarget.dataset.value || "", manualAction: true }); },
  setQuestionValue(index, value) {
    const questionRows = this.data.questionRows.slice();
    if (!questionRows[index]) return;
    questionRows[index] = hydrateQuestion(questionRows[index], value);
    const structuredAnswers = {};
    questionRows.forEach(row => {
      if (row.value !== "" && row.value !== null && row.value !== undefined && (!Array.isArray(row.value) || row.value.length)) structuredAnswers[row.key] = row.value;
    });
    const recommendation = hydrateRecommendation(buildRecommendation(questionRows));
    const updates = { questionRows, structuredAnswers, recommendation };
    if (recommendation.outcome && !this.data.manualOutcome) updates.outcome = recommendation.outcome;
    if (recommendation.action && !this.data.manualAction) updates.nextAction = recommendation.action;
    if (updates.outcome !== "recovered") updates.closeVisit = false;
    this.setData(updates);
  },
  chooseScale(e) { this.setQuestionValue(Number(e.currentTarget.dataset.index), Number(e.currentTarget.dataset.value)); },
  chooseQuestionOption(e) { this.setQuestionValue(Number(e.currentTarget.dataset.index), e.currentTarget.dataset.value || ""); },
  toggleMulti(e) {
    const index = Number(e.currentTarget.dataset.index), value = e.currentTarget.dataset.value || "";
    const current = Array.isArray((this.data.questionRows[index] || {}).value) ? this.data.questionRows[index].value.slice() : [];
    const found = current.indexOf(value);
    if (found >= 0) current.splice(found, 1); else current.push(value);
    this.setQuestionValue(index, current);
  },
  onQuestionInput(e) { this.setQuestionValue(Number(e.currentTarget.dataset.index), e.detail.value || ""); },
  onNote(e) { this.setData({ note: e.detail.value || "" }); },
  onNextDate(e) { this.setData({ nextDate: e.detail.value || "" }); },
  onAppointmentDate(e) { this.setData({ appointmentDate: e.detail.value || "" }); },
  onAppointmentTime(e) { this.setData({ appointmentTime: e.detail.value || "" }); },
  onCloseVisit(e) { this.setData({ closeVisit: !!e.detail.value }); },
  callCustomer() {
    const phone = (this.data.item.customer || {}).phone || "";
    if (phone) wx.makePhoneCall({ phoneNumber: phone });
  },
  async submit() {
    if (this.data.busy) return;
    if (!this.data.outcome) { wx.showToast({ title: "请选择随访结果", icon: "none" }); return; }
    if (!this.data.nextAction) { wx.showToast({ title: "请选择下一步", icon: "none" }); return; }
    if (this.data.questionRows.length && !Object.keys(this.data.structuredAnswers).length) { wx.showToast({ title: "请至少记录一项随访内容", icon: "none" }); return; }
    if (this.data.nextAction === "reschedule" && !this.data.nextDate) { wx.showToast({ title: "请选择再次联系日期", icon: "none" }); return; }
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
        close_visit: this.data.closeVisit, structured_answers: this.data.structuredAnswers
      });
      wx.showToast({ title: "随访已记录", icon: "success" });
      setTimeout(() => wx.navigateBack(), 600);
    } catch (e) {
      this.setData({ error: (e && (e.detail || e.errMsg)) || "随访处理失败" });
      wx.showModal({ title: "未能提交", content: (e && (e.detail || e.errMsg)) || "请稍后重试", showCancel: false });
    } finally { this.setData({ busy: false }); }
  }
});

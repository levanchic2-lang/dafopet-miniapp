const { getJson, postJson } = require("../../utils/api");

const QUESTIONS = [
  { key: "mental_7d", label: "近7天精神面貌是否异常" },
  { key: "appetite_7d", label: "近7天食欲是否异常" },
  { key: "vomit_diarrhea_7d", label: "近7天是否有呕吐或腹泻" },
  { key: "cough_nasal_7d", label: "近7天是否有咳嗽或流鼻涕" },
  { key: "medication_15d", label: "近15天是否有用药或治疗" },
  { key: "other_vaccine_15d", label: "近15天是否接种过其他疫苗" },
  { key: "prior_reaction", label: "既往接种疫苗是否有不良反应" },
  { key: "pregnancy_lactation_15d", label: "近15天是否处于怀孕或哺乳期" },
  { key: "surgery_anesthesia_15d", label: "近15天是否接受过手术或麻醉" }
];

Page({
  data: {
    phone: "", lookupLoading: false, lookupDone: false, customer: null, pets: [],
    selectedPetId: 0, useNewPet: false, ownerName: "",
    petName: "", petSpecies: "cat", petBreed: "", petGender: "unknown", petBirthday: "",
    storeOptions: ["请选择接种门店", "东环店", "横岗店"], storeIndex: 0,
    stageOptions: ["请选择免疫阶段", "首免第1针", "首免第2针", "首免第3针", "加强免疫", "年度加强"],
    stageValues: ["", "primary_1", "primary_2", "primary_3", "booster", "annual"], stageIndex: 0,
    requestedDate: "", questions: QUESTIONS.map(q => ({ ...q, value: "", detail: "" })),
    submitting: false, error: "", currentRegistrationId: 0
  },

  onLoad() {
    const d = new Date();
    const pad = n => String(n).padStart(2, "0");
    this.setData({ requestedDate: `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}` });
  },

  async onShow() {
    if (!this.data.currentRegistrationId || !this.data.phone) return;
    try {
      const r = await getJson(`/api/vaccine-registration/${this.data.currentRegistrationId}`, { phone: this.data.phone });
      if (r.status === "pending_review") {
        wx.showModal({ title: "同意书已签署", content: "登记已进入医院待审核列表。医护现场检查后会确认是否接种。", showCancel: false,
          success: () => wx.navigateBack({ delta: 1 }) });
      }
    } catch (e) {}
  },

  onPhoneInput(e) { this.setData({ phone: (e.detail.value || "").trim(), lookupDone: false, error: "" }); },
  async lookupCustomer() {
    if (!/^\d{11}$/.test(this.data.phone)) return this.setData({ error: "请输入11位手机号" });
    this.setData({ lookupLoading: true, error: "" });
    try {
      const r = await getJson("/api/customer/lookup", { phone: this.data.phone });
      this.setData({
        lookupDone: true,
        customer: r.found ? { id: r.customer_id, name: r.name || "" } : null,
        pets: r.pets || [], ownerName: r.name || "", selectedPetId: 0,
        useNewPet: !r.found || !(r.pets || []).length
      });
    } catch (e) { this.setData({ lookupDone: true, customer: null, pets: [], useNewPet: true, error: "查询失败，请重试" }); }
    finally { this.setData({ lookupLoading: false }); }
  },
  selectPet(e) { this.setData({ selectedPetId: Number(e.detail.value || 0), useNewPet: false, error: "" }); },
  chooseNewPet() { this.setData({ selectedPetId: 0, useNewPet: true, error: "" }); },
  fieldInput(e) { this.setData({ [e.currentTarget.dataset.field]: e.detail.value || "", error: "" }); },
  chooseSpecies(e) { this.setData({ petSpecies: e.detail.value }); },
  chooseGender(e) { this.setData({ petGender: e.detail.value }); },
  chooseStore(e) { this.setData({ storeIndex: Number(e.detail.value) }); },
  chooseStage(e) { this.setData({ stageIndex: Number(e.detail.value) }); },
  chooseDate(e) { this.setData({ requestedDate: e.detail.value }); },
  answerQuestion(e) {
    const idx = Number(e.currentTarget.dataset.index); const qs = this.data.questions.slice();
    qs[idx].value = e.detail.value; this.setData({ questions: qs, error: "" });
  },
  questionDetail(e) {
    const idx = Number(e.currentTarget.dataset.index); const qs = this.data.questions.slice();
    qs[idx].detail = e.detail.value || ""; this.setData({ questions: qs });
  },

  async submit() {
    if (this.data.submitting) return;
    if (!/^\d{11}$/.test(this.data.phone) || !this.data.lookupDone) return this.setData({ error: "请先查询主人手机号" });
    if (!this.data.selectedPetId && (!this.data.useNewPet || !this.data.petName || !this.data.ownerName)) return this.setData({ error: "请选择宠物，或完整填写新档案" });
    if (!this.data.storeIndex) return this.setData({ error: "请选择接种门店" });
    if (!this.data.stageIndex) return this.setData({ error: "请选择免疫阶段" });
    const unanswered = this.data.questions.find(q => !q.value);
    if (unanswered) return this.setData({ error: `请回答：${unanswered.label}` });
    const questionnaire = {};
    this.data.questions.forEach(q => { questionnaire[q.key] = { value: q.value, detail: q.detail || "" }; });
    const payload = {
      phone: this.data.phone, pet_id: this.data.selectedPetId,
      owner_name: this.data.ownerName, pet_name: this.data.petName,
      pet_species: this.data.petSpecies, pet_breed: this.data.petBreed,
      pet_gender: this.data.petGender, pet_birthday: this.data.petBirthday,
      clinic_store: this.data.storeOptions[this.data.storeIndex],
      immunization_stage: this.data.stageValues[this.data.stageIndex],
      requested_date: this.data.requestedDate, questionnaire
    };
    this.setData({ submitting: true, error: "" });
    try {
      const r = await postJson("/api/vaccine-registration/create", payload);
      this.setData({ currentRegistrationId: r.registration_id });
      try { wx.setStorageSync("VACCINE_REG_ID", r.registration_id); wx.setStorageSync("VACCINE_REG_PHONE", this.data.phone); } catch (e) {}
      wx.navigateTo({ url: "/pages/vaccine/consent?url=" + encodeURIComponent(r.consent_url) });
    } catch (e) { this.setData({ error: (e && (e.detail || e.error)) || "提交失败，请重试" }); }
    finally { this.setData({ submitting: false }); }
  }
});

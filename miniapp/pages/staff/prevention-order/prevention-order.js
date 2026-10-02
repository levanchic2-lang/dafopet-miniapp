const { staffGet, staffPost } = require("../../../utils/api");

const number = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;

Page({
  data: {
    petId: 0, mode: "vaccine", loading: true, error: "", pet: {}, customer: {},
    orderDate: "", nextDueDate: "", vets: [], vetIndex: -1,
    vaccineItems: [], dewormingItems: [], query: "", results: [], selected: null,
    vaccineTypes: ["狂犬疫苗", "猫三联", "犬六联", "犬八联", "其他疫苗"],
    vaccineTypeValues: ["rabies", "combo_3", "combo_6", "canine_8", "other"], vaccineTypeIndex: 1,
    doseLabels: ["第1针", "第2针", "第3针", "加强针"], doseValues: [1, 2, 3, 99], doseIndex: 0,
    dewormType: "external", weightKg: "", dose: "", quantity: 1, unitPrice: 0,
    batchNo: "", notes: "", isFree: false, requestConsent: true, submitting: false, total: "0.00"
  },
  onLoad(options) {
    this.setData({ petId: Number(options.pet_id || 0), mode: options.mode === "deworming" ? "deworming" : "vaccine" });
    this.load();
  },
  async load() {
    if (!this.data.petId) { this.setData({ loading: false, error: "未找到宠物档案" }); return; }
    try {
      const data = await staffGet(`/api/staff-miniapp/pets/${this.data.petId}/prevention-order`);
      const vets = data.vets || [];
      const vetIndex = data.default_vet ? vets.indexOf(data.default_vet) : -1;
      const vaccineItems = data.vaccine_items || [];
      const dewormingItems = data.deworming_items || [];
      const results = this.data.mode === "vaccine" ? vaccineItems : dewormingItems;
      this.setData({
        pet: data.pet || {}, customer: data.customer || {}, orderDate: data.order_date || "",
        nextDueDate: data.default_next_due || "", vets, vetIndex, vaccineItems, dewormingItems, results,
        weightKg: data.latest_weight ? String(data.latest_weight) : ""
      });
      this.updateTitle();
    } catch (e) { this.setData({ error: (e && e.detail) || "预防开单信息读取失败" }); }
    finally { this.setData({ loading: false }); }
  },
  updateTitle() { wx.setNavigationBarTitle({ title: `${this.data.pet.name || "宠物"} · ${this.data.mode === "vaccine" ? "疫苗" : "驱虫"}开单` }); },
  switchMode(e) {
    const mode = e.currentTarget.dataset.mode;
    if (mode === this.data.mode) return;
    this.setData({ mode, query: "", selected: null, batchNo: "", quantity: 1, unitPrice: 0, total: "0.00", results: mode === "vaccine" ? this.data.vaccineItems : this.data.dewormingItems });
    this.updateTitle();
  },
  searchInput(e) {
    const query = (e.detail.value || "").trim();
    const items = this.data.mode === "vaccine" ? this.data.vaccineItems : this.data.dewormingItems;
    this.setData({ query, results: query ? items.filter(item => (item.name || "").includes(query)) : items });
  },
  chooseItem(e) {
    const item = this.data.results[Number(e.currentTarget.dataset.index)];
    if (!item) return;
    const batch = (item.batches || [])[0] || {};
    this.setData({ selected: item, unitPrice: number(item.sell_price), batchNo: batch.batch_no || "" });
    this.recalculate();
  },
  clearItem() { this.setData({ selected: null, batchNo: "", unitPrice: 0, total: "0.00" }); },
  chooseVet(e) { this.setData({ vetIndex: Number(e.detail.value) }); },
  chooseVaccineType(e) { this.setData({ vaccineTypeIndex: Number(e.detail.value) }); },
  chooseDose(e) { this.setData({ doseIndex: Number(e.detail.value) }); },
  changeDate(e) { this.setData({ orderDate: e.detail.value }); },
  changeNextDue(e) { this.setData({ nextDueDate: e.detail.value }); },
  setDewormType(e) { this.setData({ dewormType: e.currentTarget.dataset.value }); },
  setField(e) { this.setData({ [e.currentTarget.dataset.field]: e.detail.value }); if (["quantity", "unitPrice"].includes(e.currentTarget.dataset.field)) this.recalculate(); },
  toggleFree(e) { this.setData({ isFree: e.detail.value.length > 0 }); this.recalculate(); },
  toggleConsent(e) { this.setData({ requestConsent: e.detail.value.length > 0 }); },
  recalculate() {
    const qty = this.data.mode === "vaccine" ? 1 : Math.max(1, number(this.data.quantity, 1));
    const total = this.data.mode === "vaccine" && this.data.isFree ? 0 : qty * Math.max(0, number(this.data.unitPrice));
    this.setData({ total: total.toFixed(2) });
  },
  async submitOrder() {
    if (!this.data.selected) { wx.showToast({ title: `请选择${this.data.mode === "vaccine" ? "疫苗" : "驱虫药"}`, icon: "none" }); return; }
    if (this.data.vetIndex < 0) { wx.showToast({ title: "请选择操作医生", icon: "none" }); return; }
    if (this.data.mode === "deworming" && number(this.data.weightKg) <= 0) { wx.showToast({ title: "请填写本次体重", icon: "none" }); return; }
    this.setData({ submitting: true });
    try {
      const result = await staffPost(`/api/staff-miniapp/pets/${this.data.petId}/prevention-order`, {
        mode: this.data.mode, item_id: this.data.selected.id, order_date: this.data.orderDate,
        vet_name: this.data.vets[this.data.vetIndex], batch_no: this.data.batchNo,
        next_due_date: this.data.nextDueDate, unit_price: number(this.data.unitPrice), notes: this.data.notes,
        vaccine_type: this.data.vaccineTypeValues[this.data.vaccineTypeIndex],
        dose_number: this.data.doseValues[this.data.doseIndex], is_free: this.data.isFree,
        request_vaccine_consent: this.data.requestConsent,
        deworm_type: this.data.dewormType, weight_kg: number(this.data.weightKg), dose: this.data.dose,
        quantity: Math.max(1, number(this.data.quantity, 1))
      });
      wx.showToast({ title: result.message || "开单完成", icon: "success" });
      setTimeout(() => wx.navigateBack(), 800);
    } catch (e) { wx.showToast({ title: (e && e.detail) || "预防开单失败", icon: "none" }); }
    finally { this.setData({ submitting: false }); }
  }
});

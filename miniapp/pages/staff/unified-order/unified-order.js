const { staffGet, staffPost } = require("../../../utils/api");

const typeOptions = [
  { key: "prescription", label: "处方" }, { key: "exam", label: "检查" },
  { key: "product", label: "商品" }, { key: "vaccine", label: "疫苗" },
  { key: "deworming", label: "驱虫" }, { key: "inpatient", label: "住院" }
];
const typeLabels = typeOptions.reduce((o, x) => { o[x.key] = x.label; return o; }, {});
const routeOptions = [
  { key: "oral", label: "口服" }, { key: "subcutaneous", label: "皮下注射" },
  { key: "intramuscular", label: "肌肉注射" }, { key: "intravenous", label: "静脉注射" },
  { key: "topical", label: "外用" }, { key: "nebulize", label: "雾化" },
  { key: "service", label: "处置 / 服务" }, { key: "other", label: "其他" }
];
const number = (v, fallback = 0) => Number.isFinite(Number(v)) ? Number(v) : fallback;

Page({
  data: { id: 0, loading: true, error: "", visit: {}, pet: {}, customer: {}, orderDate: "", vetName: "", query: "", searching: false, results: [], rows: [], grandTotal: "0.00", submitting: false, typeOptions, routeOptions },
  onLoad(options) { this.setData({ id: Number(options.id || 0) }); this.load(); },
  async load() {
    try {
      const data = await staffGet(`/api/staff-miniapp/visits/${this.data.id}/unified-order`);
      this.setData({ visit: data.visit || {}, pet: data.pet || {}, customer: data.customer || {}, orderDate: data.order_date || "", vetName: data.vet_name || "" });
    } catch (e) { this.setData({ error: (e && e.detail) || "病例读取失败" }); }
    finally { this.setData({ loading: false }); }
  },
  changeDate(e) { this.setData({ orderDate: e.detail.value }); },
  setTopField(e) { this.setData({ [e.currentTarget.dataset.field]: e.detail.value }); },
  searchInput(e) {
    const query = e.detail.value || ""; this.setData({ query }); clearTimeout(this.searchTimer);
    if (!query.trim()) { this.setData({ results: [], searching: false }); return; }
    this.searchTimer = setTimeout(async () => {
      this.setData({ searching: true });
      try { const data = await staffGet("/api/staff-miniapp/unified-order/items", { q: query.trim() }); this.setData({ results: data.items || [] }); }
      catch (err) { wx.showToast({ title: (err && err.detail) || "搜索失败", icon: "none" }); }
      finally { this.setData({ searching: false }); }
    }, 220);
  },
  addItem(e) {
    const item = this.data.results[Number(e.currentTarget.dataset.index)]; if (!item) return;
    const orderType = item.order_type === "manual" ? "" : item.order_type;
    if (orderType && !typeLabels[orderType]) { wx.showToast({ title: "请使用该项目的专用流程", icon: "none" }); return; }
    const rows = this.data.rows.concat([{ key: Date.now() + "-" + Math.random(), item, item_id: item.id, order_type: orderType, typeLabel: typeLabels[orderType] || "", quantity: 1, unit_price: number(item.sell_price), notes: "", drug_type: "", dose_amount: "", dose_unit: item.unit || "", times_per_day: "", duration_days: "", totalTimes: 0, print_note: "", dose_number: 1, batch_no: "", deworm_type: "external", dose: "", weight_kg: "", quantityEdited: false }]);
    this.setData({ rows, query: "", results: [] }); this.recalculate();
  },
  removeItem(e) { const rows = this.data.rows.slice(); rows.splice(Number(e.currentTarget.dataset.index), 1); this.setData({ rows }); this.recalculate(); },
  changeType(e) { const i = Number(e.currentTarget.dataset.index), option = typeOptions[Number(e.detail.value)]; if (!option) return; this.setData({ [`rows[${i}].order_type`]: option.key, [`rows[${i}].typeLabel`]: option.label }); this.recalculate(); },
  selectRoute(e) { this.setData({ [`rows[${Number(e.currentTarget.dataset.index)}].drug_type`]: e.currentTarget.dataset.route }); },
  selectDewormType(e) { this.setData({ [`rows[${Number(e.currentTarget.dataset.index)}].deworm_type`]: e.currentTarget.dataset.value }); },
  setRowField(e) {
    const i = Number(e.currentTarget.dataset.index), field = e.currentTarget.dataset.field, value = e.detail.value;
    this.setData({ [`rows[${i}].${field}`]: value });
    if (field === "quantity") this.setData({ [`rows[${i}].quantityEdited`]: true });
    this.recalculate();
  },
  recalculate() {
    const rows = this.data.rows.map(row => {
      const next = Object.assign({}, row); const times = number(next.times_per_day) * number(next.duration_days); next.totalTimes = times > 0 ? times : 0;
      if (next.order_type === "prescription" && !next.item.is_service && !next.quantityEdited && number(next.dose_amount) > 0 && times > 0 && (next.dose_unit || "") === (next.item.unit || "")) next.quantity = Math.round(number(next.dose_amount) * times * 10000) / 10000;
      const qty = Math.max(0, number(next.quantity, 1)), price = Math.max(0, number(next.unit_price));
      const amount = next.order_type !== "inpatient" && next.item.single_use_pack ? Math.ceil(qty / Math.max(1, number(next.item.unit2_ratio, 1))) * price : qty * price;
      next.subtotal = amount.toFixed(2); return next;
    });
    const grandTotal = rows.reduce((sum, row) => sum + number(row.subtotal), 0).toFixed(2); this.setData({ rows, grandTotal });
  },
  async submitOrder() {
    const rows = this.data.rows;
    if (!rows.length || rows.some(x => !x.order_type)) { wx.showToast({ title: "请完成所有项目的开单类型", icon: "none" }); return; }
    if (rows.some(x => x.order_type === "prescription") && !this.data.vetName.trim()) { wx.showToast({ title: "处方必须填写医生", icon: "none" }); return; }
    const incomplete = rows.find(x => x.order_type === "prescription" && !x.item.is_service && (!x.drug_type || number(x.dose_amount) <= 0 || number(x.times_per_day) <= 0 || number(x.duration_days) <= 0));
    if (incomplete) { wx.showToast({ title: `请完善 ${incomplete.item.name} 的用法`, icon: "none" }); return; }
    this.setData({ submitting: true });
    try {
      const items = rows.map(({ item, key, typeLabel, subtotal, totalTimes, quantityEdited, ...row }) => row);
      await staffPost(`/api/staff-miniapp/visits/${this.data.id}/unified-order`, { order_date: this.data.orderDate, vet_name: this.data.vetName.trim(), items, request_vaccine_consent: true });
      wx.showToast({ title: "开单完成", icon: "success" }); setTimeout(() => wx.navigateBack(), 800);
    } catch (e) { wx.showToast({ title: (e && e.detail) || "开单失败", icon: "none" }); }
    finally { this.setData({ submitting: false }); }
  }
});

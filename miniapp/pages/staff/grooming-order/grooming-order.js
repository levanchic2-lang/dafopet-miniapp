const { staffGet, staffPost } = require("../../../utils/api");

const number = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;

Page({
  data: {
    petId: 0, loading: true, error: "", pet: {}, customer: {}, orderDate: "",
    groomers: [], assistants: [], groomerIndex: -1, assistantIndex: -1,
    allItems: [], query: "", results: [], rows: [], grandTotal: "0.00",
    skinCondition: "", behaviorNote: "", notes: "", submitting: false
  },
  onLoad(options) { this.setData({ petId: Number(options.pet_id || 0) }); this.load(); },
  async load() {
    if (!this.data.petId) { this.setData({ loading: false, error: "未找到宠物档案" }); return; }
    try {
      const data = await staffGet(`/api/staff-miniapp/pets/${this.data.petId}/grooming-order`);
      const groomers = data.groomers || [];
      const defaultIndex = data.default_groomer ? groomers.indexOf(data.default_groomer) : -1;
      this.setData({
        pet: data.pet || {}, customer: data.customer || {}, orderDate: data.order_date || "",
        groomers, groomerIndex: defaultIndex, assistants: data.assistants || [], allItems: data.items || [],
        results: data.items || []
      });
      wx.setNavigationBarTitle({ title: `${(data.pet || {}).name || "宠物"} · 美容开单` });
    } catch (e) { this.setData({ error: (e && e.detail) || "美容开单信息读取失败" }); }
    finally { this.setData({ loading: false }); }
  },
  changeDate(e) { this.setData({ orderDate: e.detail.value }); },
  chooseGroomer(e) { this.setData({ groomerIndex: Number(e.detail.value) }); },
  chooseAssistant(e) { this.setData({ assistantIndex: Number(e.detail.value) }); },
  searchInput(e) {
    const query = (e.detail.value || "").trim();
    const results = !query ? this.data.allItems : this.data.allItems.filter(item => (item.name || "").includes(query));
    this.setData({ query, results });
  },
  addItem(e) {
    const item = this.data.results[Number(e.currentTarget.dataset.index)];
    if (!item) return;
    if (this.data.rows.some(row => row.item_id === item.id)) { wx.showToast({ title: "该项目已经添加", icon: "none" }); return; }
    const rows = this.data.rows.concat([{
      key: `${Date.now()}-${item.id}`, item_id: item.id, name: item.name,
      unit: item.unit || "次", quantity: 1, unit_price: number(item.sell_price), notes: "", subtotal: "0.00"
    }]);
    this.setData({ rows }); this.recalculate();
  },
  removeItem(e) { const rows = this.data.rows.slice(); rows.splice(Number(e.currentTarget.dataset.index), 1); this.setData({ rows }); this.recalculate(); },
  setRowField(e) { this.setData({ [`rows[${Number(e.currentTarget.dataset.index)}].${e.currentTarget.dataset.field}`]: e.detail.value }); this.recalculate(); },
  setField(e) { this.setData({ [e.currentTarget.dataset.field]: e.detail.value }); },
  recalculate() {
    const rows = this.data.rows.map(row => Object.assign({}, row, {
      subtotal: (Math.max(0, number(row.quantity, 1)) * Math.max(0, number(row.unit_price))).toFixed(2)
    }));
    this.setData({ rows, grandTotal: rows.reduce((sum, row) => sum + number(row.subtotal), 0).toFixed(2) });
  },
  async submitOrder() {
    if (this.data.groomerIndex < 0) { wx.showToast({ title: "请选择美容师", icon: "none" }); return; }
    if (!this.data.rows.length) { wx.showToast({ title: "请至少选择一个美容项目", icon: "none" }); return; }
    this.setData({ submitting: true });
    try {
      const result = await staffPost(`/api/staff-miniapp/pets/${this.data.petId}/grooming-order`, {
        order_date: this.data.orderDate,
        groomer_name: this.data.groomers[this.data.groomerIndex],
        assistant_name: this.data.assistantIndex >= 0 ? this.data.assistants[this.data.assistantIndex] : "",
        skin_condition: this.data.skinCondition, behavior_note: this.data.behaviorNote, notes: this.data.notes,
        items: this.data.rows.map(({ item_id, quantity, unit_price, notes }) => ({ item_id, quantity, unit_price, notes }))
      });
      wx.showToast({ title: result.message || "美容开单完成", icon: "success" });
      setTimeout(() => wx.navigateBack(), 800);
    } catch (e) { wx.showToast({ title: (e && e.detail) || "美容开单失败", icon: "none" }); }
    finally { this.setData({ submitting: false }); }
  }
});

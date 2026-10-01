const { staffGet, staffPost } = require("../../../utils/api");

const pad = (n) => String(n).padStart(2, "0");
const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseDay = (s) => {
  const p = String(s || "").split("-").map(Number);
  return p.length === 3 ? new Date(p[0], p[1] - 1, p[2], 12, 0, 0) : new Date();
};
const isBeauty = (item) => ["beauty", "grooming", "washcare"].includes(item.category);
const beautySpeciesLabel = (species) => species === "cat" ? "猫" : species === "dog" ? "犬" : "";
const beautyServiceLabel = (category) => category === "grooming" ? "造型" : category === "washcare" ? "洗护" : "";
const beautyServiceName = (species, category) => `${beautySpeciesLabel(species)}${beautyServiceLabel(category)}`;
const beautyDuration = (species) => species === "cat" ? 60 : 90;
const buildColumns = (dates, appointments, blocks, track) => (dates || []).map((day) => ({
  ...day,
  blocks: track === "beauty" ? (blocks || []).filter((block) => block.date === day.date) : [],
  events: (appointments || []).filter((item) => item.date === day.date && (track === "beauty" ? isBeauty(item) : !isBeauty(item))).map((item) => {
    const hm = String(item.time || "08:00").split(":").map(Number);
    const minutes = Math.max(0, (hm[0] || 8) * 60 + (hm[1] || 0) - 480);
    return { ...item, top: Math.round(minutes * 1.3), height: Math.max(58, Math.round((item.duration || 30) * 1.3)) };
  })
}));

Page({
  data: {
    loading: true, error: "", profile: {}, start: "", dates: [], columns: [], track: "medical",
    rawAppointments: [], rawBlocks: [],
    hours: [8,9,10,11,12,13,14,15,16,17,18,19,20,21,22],
    selected: null, editing: false, editDate: "", editTime: "", editService: "",
    editDuration: 30, editNotes: "", saving: false,
    createOpen: false, createStep: "choice", createDate: "", createTime: "", createStore: "",
    stores: [], storeIndex: 0, fixedStore: "", createCategory: "outpatient",
    categories: [{value:"outpatient",label:"门诊"},{value:"surgery",label:"手术"},{value:"tnr",label:"TNR"}],
    createService: "", createBeautySpecies: "", createDuration: 30, createNotes: "", customerQuery: "",
    customerResults: [], selectedCustomer: null, selectedPet: null
  },
  onLoad() {
    try { this.setData({ profile: wx.getStorageSync("STAFF_PROFILE") || {} }); } catch (e) {}
    this.setData({ start: iso(new Date()) });
    this.loadCalendar();
  },
  async loadCalendar() {
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/calendar", { start: this.data.start, days: 3 });
      const rawAppointments = result.appointments || [];
      const rawBlocks = result.blocks || [];
      const columns = buildColumns(result.dates || [], rawAppointments, rawBlocks, this.data.track);
      const stores = result.stores || [];
      const fixedStore = result.fixed_store || "";
      let storeIndex = Math.max(0, stores.findIndex((s) => s.value === (fixedStore || this.data.createStore)));
      this.setData({ dates: result.dates || [], columns, rawAppointments, rawBlocks, stores, fixedStore, storeIndex,
        createStore: fixedStore || (stores[storeIndex] && stores[storeIndex].value) || "" });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "预约日历加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  onPullDownRefresh() { this.loadCalendar(); },
  shiftDays(e) {
    const d = parseDay(this.data.start);
    d.setDate(d.getDate() + Number(e.currentTarget.dataset.days || 0));
    this.setData({ start: iso(d), selected: null, editing: false });
    this.loadCalendar();
  },
  goTodayDate() { this.setData({ start: iso(new Date()), selected: null, editing: false }); this.loadCalendar(); },
  setTrack(e) {
    const track = e.currentTarget.dataset.track === "beauty" ? "beauty" : "medical";
    const categories = track === "beauty"
      ? [{value:"grooming",label:"造型"},{value:"washcare",label:"洗护"}]
      : [{value:"outpatient",label:"门诊"},{value:"surgery",label:"手术"},{value:"tnr",label:"TNR"}];
    this.setData({ track, categories, createCategory: categories[0].value,
      columns: buildColumns(this.data.dates, this.data.rawAppointments, this.data.rawBlocks, track),
      selected: null, createOpen: false });
  },
  openEvent(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    let selected = null;
    (this.data.columns || []).some((col) => {
      selected = (col.events || []).find((item) => item.id === id) || null;
      return !!selected;
    });
    if (!selected) return;
    this.setData({ selected, editing: false, editDate: selected.date, editTime: selected.time,
      editService: selected.service_name, editDuration: selected.duration, editNotes: selected.notes || "" });
  },
  closeEvent() { if (!this.data.saving) this.setData({ selected: null, editing: false }); },
  stopBubble() {},
  openCustomer() {
    const customerId = Number((this.data.selected || {}).customer_id || 0);
    if (!customerId) {
      wx.showToast({ title: "该预约尚未关联客户档案", icon: "none" });
      return;
    }
    this.setData({ selected: null, editing: false });
    wx.navigateTo({ url: `/pages/staff/customer/customer?id=${customerId}` });
  },
  startEdit() { this.setData({ editing: true }); },
  onEditDate(e) { this.setData({ editDate: e.detail.value }); },
  onEditTime(e) { this.setData({ editTime: e.detail.value }); },
  onEditService(e) { this.setData({ editService: e.detail.value }); },
  chooseEditBeautyService(e) {
    const editService = e.currentTarget.dataset.value;
    this.setData({ editService, editDuration: beautyDuration(editService.startsWith("猫") ? "cat" : "dog") });
  },
  onEditDuration(e) { this.setData({ editDuration: e.detail.value }); },
  onEditNotes(e) { this.setData({ editNotes: e.detail.value }); },
  async saveEdit() {
    if (this.data.saving || !this.data.selected) return;
    this.setData({ saving: true });
    try {
      const id = this.data.selected.id;
      if (this.data.editDate !== this.data.selected.date || this.data.editTime !== this.data.selected.time) {
        await staffPost(`/api/staff-miniapp/appointments/${id}/reschedule`, { date: this.data.editDate, time: this.data.editTime });
      }
      await staffPost(`/api/staff-miniapp/appointments/${id}/service`, {
        service_name: this.data.editService, duration: Number(this.data.editDuration || 30), notes: this.data.editNotes
      });
      wx.showToast({ title: "预约已更新", icon: "success" });
      this.setData({ selected: null, editing: false });
      this.loadCalendar();
    } catch (e) { wx.showModal({ title: "保存失败", content: (e && (e.detail || e.errMsg)) || "请稍后重试", showCancel: false }); }
    finally { this.setData({ saving: false }); }
  },
  updateStatus(e) {
    if (!this.data.selected || this.data.saving) return;
    const status = e.currentTarget.dataset.status;
    const label = e.currentTarget.dataset.label;
    wx.showModal({ title: `确认${label}`, content: `预约：${this.data.selected.pet_name} · ${this.data.selected.service_name}`,
      success: async (res) => {
        if (!res.confirm) return;
        this.setData({ saving: true });
        try {
          await staffPost(`/api/staff-miniapp/appointments/${this.data.selected.id}/status`, { status });
          wx.showToast({ title: `已${label}`, icon: "success" });
          this.setData({ selected: null, editing: false });
          this.loadCalendar();
        } catch (err) { wx.showModal({ title: "操作失败", content: (err && (err.detail || err.errMsg)) || "请稍后重试", showCancel: false }); }
        finally { this.setData({ saving: false }); }
      }
    });
  },
  openBlank(e) {
    if (this.data.selected || this.data.createOpen) return;
    const hour = Number(e.currentTarget.dataset.hour || 8);
    this.setData({ createOpen: true, createStep: "choice", createDate: e.currentTarget.dataset.date,
      createTime: `${pad(hour)}:00`, createService: "", createDuration: 30, createNotes: "",
      createCategory: this.data.track === "beauty" ? "grooming" : "outpatient",
      createBeautySpecies: "",
      customerQuery: "", customerResults: [], selectedCustomer: null, selectedPet: null });
  },
  closeCreate() { if (!this.data.saving) this.setData({ createOpen: false, createStep: "choice" }); },
  backCreateChoice() { if (!this.data.saving) this.setData({ createStep: "choice" }); },
  startCreateAppointment() { this.setData({ createStep: "appointment" }); },
  async createBeautyDayOff() {
    if (this.data.saving) return;
    const ok = await new Promise((resolve) => wx.showModal({ title: "美容师休息", content: `${this.data.createDate} 全天停止美容、洗护和造型预约，医疗业务不受影响。`, success: (r) => resolve(r.confirm), fail: () => resolve(false) }));
    if (!ok) return;
    this.setData({ saving: true });
    try {
      await staffPost("/api/staff-miniapp/calendar/beauty-day-off", { date: this.data.createDate, store: this.data.createStore });
      wx.showToast({ title: "已封锁美容预约", icon: "success" });
      this.setData({ createOpen: false }); this.loadCalendar();
    } catch (e) { wx.showModal({ title: "设置失败", content: (e && (e.detail || e.errMsg)) || "请稍后重试", showCancel: false }); }
    finally { this.setData({ saving: false }); }
  },
  chooseCategory(e) {
    const createCategory = e.currentTarget.dataset.value;
    const patch = { createCategory };
    if (this.data.track === "beauty") {
      patch.createService = beautyServiceName(this.data.createBeautySpecies, createCategory);
    }
    this.setData(patch);
  },
  chooseBeautySpecies(e) {
    const createBeautySpecies = e.currentTarget.dataset.value;
    this.setData({
      createBeautySpecies,
      createService: beautyServiceName(createBeautySpecies, this.data.createCategory),
      createDuration: beautyDuration(createBeautySpecies)
    });
  },
  onCreateDate(e) { this.setData({ createDate: e.detail.value }); },
  onCreateTime(e) { this.setData({ createTime: e.detail.value }); },
  onCreateService(e) { this.setData({ createService: e.detail.value }); },
  onCreateDuration(e) { this.setData({ createDuration: e.detail.value }); },
  onCreateNotes(e) { this.setData({ createNotes: e.detail.value }); },
  onStoreChange(e) {
    const storeIndex = Number(e.detail.value || 0);
    this.setData({ storeIndex, createStore: (this.data.stores[storeIndex] || {}).value || "" });
  },
  onCustomerQuery(e) { this.setData({ customerQuery: e.detail.value || "" }); },
  async searchCustomers() {
    const q = (this.data.customerQuery || "").trim();
    if (q.length < 2) { wx.showToast({ title: "至少输入两个字", icon: "none" }); return; }
    try {
      const result = await staffGet("/api/staff-miniapp/customers", { q });
      this.setData({ customerResults: result.items || [], selectedCustomer: null, selectedPet: null });
    } catch (e) { wx.showToast({ title: "客户搜索失败", icon: "none" }); }
  },
  selectCustomer(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const selectedCustomer = (this.data.customerResults || []).find((c) => c.id === id) || null;
    this.setData({ selectedCustomer, selectedPet: null, customerResults: [] });
  },
  selectPet(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const selectedPet = ((this.data.selectedCustomer || {}).pets || []).find((p) => p.id === id) || null;
    const patch = { selectedPet };
    if (this.data.track === "beauty" && selectedPet) {
      const species = selectedPet.species === "cat" || selectedPet.species === "dog" ? selectedPet.species : "";
      patch.createBeautySpecies = species;
      patch.createService = beautyServiceName(species, this.data.createCategory);
      if (species) patch.createDuration = beautyDuration(species);
    }
    this.setData(patch);
  },
  async submitAppointment() {
    if (this.data.saving) return;
    if (!this.data.selectedCustomer || !this.data.selectedPet) { wx.showToast({ title: "请选择客户和宠物", icon: "none" }); return; }
    if (this.data.track === "beauty" && !this.data.createBeautySpecies) { wx.showToast({ title: "请选择犬或猫", icon: "none" }); return; }
    if (!(this.data.createService || "").trim()) { wx.showToast({ title: "请填写服务项目", icon: "none" }); return; }
    this.setData({ saving: true });
    try {
      await staffPost("/api/staff-miniapp/appointments", {
        category: this.data.createCategory, service_name: this.data.createService,
        customer_id: this.data.selectedCustomer.id, pet_id: this.data.selectedPet.id,
        store: this.data.createStore, appointment_date: this.data.createDate,
        appointment_time: this.data.createTime, duration_minutes: Number(this.data.createDuration || 30),
        notes: this.data.createNotes
      });
      wx.showToast({ title: "预约已创建", icon: "success" });
      this.setData({ createOpen: false, createStep: "choice" }); this.loadCalendar();
    } catch (e) { wx.showModal({ title: "创建失败", content: (e && (e.detail || e.errMsg)) || "请检查预约信息", showCancel: false }); }
    finally { this.setData({ saving: false }); }
  },
  goToday() { wx.redirectTo({ url: "/pages/staff/today/today" }); },
  goCalendar() {},
  goCustomers() { wx.redirectTo({ url: "/pages/staff/customers/customers" }); },
  goMe() { wx.redirectTo({ url: "/pages/staff/me/me" }); }
});

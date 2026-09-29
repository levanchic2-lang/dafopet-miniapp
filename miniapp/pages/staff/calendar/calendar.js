const { staffGet, staffPost } = require("../../../utils/api");

const pad = (n) => String(n).padStart(2, "0");
const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseDay = (s) => {
  const p = String(s || "").split("-").map(Number);
  return p.length === 3 ? new Date(p[0], p[1] - 1, p[2], 12, 0, 0) : new Date();
};

Page({
  data: {
    loading: true, error: "", profile: {}, start: "", dates: [], columns: [],
    hours: [8,9,10,11,12,13,14,15,16,17,18,19,20,21,22],
    selected: null, editing: false, editDate: "", editTime: "", editService: "",
    editDuration: 30, editNotes: "", saving: false
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
      const columns = (result.dates || []).map((day) => ({
        ...day,
        events: (result.appointments || []).filter((item) => item.date === day.date).map((item) => {
          const hm = String(item.time || "08:00").split(":").map(Number);
          const minutes = Math.max(0, (hm[0] || 8) * 60 + (hm[1] || 0) - 480);
          return { ...item, top: Math.round(minutes * 1.3), height: Math.max(58, Math.round((item.duration || 30) * 1.3)) };
        })
      }));
      this.setData({ dates: result.dates || [], columns });
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
  startEdit() { this.setData({ editing: true }); },
  onEditDate(e) { this.setData({ editDate: e.detail.value }); },
  onEditTime(e) { this.setData({ editTime: e.detail.value }); },
  onEditService(e) { this.setData({ editService: e.detail.value }); },
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
  goToday() { wx.redirectTo({ url: "/pages/staff/today/today" }); },
  goCalendar() {},
  goCustomers() { wx.redirectTo({ url: "/pages/staff/customers/customers" }); },
  goMe() { wx.redirectTo({ url: "/pages/staff/me/me" }); }
});

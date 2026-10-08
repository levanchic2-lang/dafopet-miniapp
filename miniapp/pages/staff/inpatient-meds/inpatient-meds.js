const { staffGet, staffPost } = require("../../../utils/api");

function buildMedicationGroups(items, previousGroups) {
  const expanded = {};
  (previousGroups || []).forEach(group => { expanded[group.hospitalization_id] = !!group.expanded; });
  const byHosp = {};
  (items || []).forEach(row => {
    const hospKey = String(row.hospitalization_id || 0);
    if (!byHosp[hospKey]) {
      byHosp[hospKey] = {
        hospitalization_id: row.hospitalization_id,
        pet_name: row.pet_name,
        cage_code: row.cage_code || "",
        total_count: 0,
        overdue_count: 0,
        selected_count: 0,
        first_at: row.scheduled_at,
        next_time: row.scheduled_time,
        timeMap: {}
      };
    }
    const group = byHosp[hospKey];
    group.total_count += 1;
    if (row.is_overdue) group.overdue_count += 1;
    if (row.scheduled_at < group.first_at) {
      group.first_at = row.scheduled_at;
      group.next_time = row.scheduled_time;
    }
    const timeKey = `${row.scheduled_date}|${row.scheduled_time}`;
    if (!group.timeMap[timeKey]) {
      group.timeMap[timeKey] = {
        key: timeKey,
        date: row.scheduled_date,
        time: row.scheduled_time,
        first_at: row.scheduled_at,
        all_selected: false,
        items: []
      };
    }
    group.timeMap[timeKey].items.push(Object.assign({}, row, { selected: false }));
  });
  const groups = Object.keys(byHosp).map(key => {
    const group = byHosp[key];
    group.time_groups = Object.keys(group.timeMap).map(timeKey => group.timeMap[timeKey])
      .sort((a, b) => a.first_at.localeCompare(b.first_at));
    delete group.timeMap;
    return group;
  }).sort((a, b) => {
    if (!!a.overdue_count !== !!b.overdue_count) return a.overdue_count ? -1 : 1;
    return a.first_at.localeCompare(b.first_at);
  });
  groups.forEach((group, index) => {
    group.expanded = Object.prototype.hasOwnProperty.call(expanded, group.hospitalization_id)
      ? expanded[group.hospitalization_id] : index === 0;
  });
  return groups;
}

function completedAtParts(row) {
  const value = row.administered_at || row.scheduled_at || "";
  return {
    at: value,
    date: value.slice(0, 10) || row.scheduled_date || "日期未知",
    time: value.slice(11, 16) || row.scheduled_time || "--:--"
  };
}

function buildCompletedGroups(items, mode, previousGroups) {
  const expanded = {};
  (previousGroups || []).forEach(group => { expanded[group.key] = !!group.expanded; });
  const groupMap = {};

  (items || []).forEach(source => {
    const row = Object.assign({}, source, { selected: false });
    const completed = completedAtParts(row);
    const byTime = mode === "time";
    const groupKey = byTime ? `date:${completed.date}` : `pet:${row.hospitalization_id || 0}`;
    if (!groupMap[groupKey]) {
      groupMap[groupKey] = {
        key: groupKey,
        hospitalization_id: row.hospitalization_id,
        title: byTime ? completed.date : `${row.pet_name}${row.cage_code ? " · " + row.cage_code : ""}`,
        total_count: 0,
        latest_at: completed.at,
        petMap: {},
        dateMap: {}
      };
    }
    const group = groupMap[groupKey];
    group.total_count += 1;
    group.petMap[String(row.hospitalization_id || row.pet_name)] = true;
    if (completed.at > group.latest_at) group.latest_at = completed.at;

    const splitDate = mode === "pet_date";
    const dateKey = splitDate ? completed.date : "all";
    if (!group.dateMap[dateKey]) {
      group.dateMap[dateKey] = {
        key: `${groupKey}|${dateKey}`,
        label: splitDate ? completed.date : "",
        latest_at: completed.at,
        timeMap: {}
      };
    }
    const dateGroup = group.dateMap[dateKey];
    if (completed.at > dateGroup.latest_at) dateGroup.latest_at = completed.at;
    const timeKey = mode === "pet" ? `${completed.date}|${completed.time}` : completed.time;
    if (!dateGroup.timeMap[timeKey]) {
      dateGroup.timeMap[timeKey] = {
        key: `${dateGroup.key}|${timeKey}`,
        date: mode === "pet" ? completed.date : "",
        time: completed.time,
        first_at: completed.at,
        items: []
      };
    }
    dateGroup.timeMap[timeKey].items.push(row);
  });

  const groups = Object.keys(groupMap).map(key => {
    const group = groupMap[key];
    group.pet_count = Object.keys(group.petMap).length;
    group.subtitle = mode === "time" ? `${group.pet_count} 只住院动物` : "已完成用药记录";
    group.date_groups = Object.keys(group.dateMap).map(dateKey => {
      const dateGroup = group.dateMap[dateKey];
      dateGroup.time_groups = Object.keys(dateGroup.timeMap).map(timeKey => dateGroup.timeMap[timeKey])
        .sort((a, b) => b.first_at.localeCompare(a.first_at));
      delete dateGroup.timeMap;
      return dateGroup;
    }).sort((a, b) => b.latest_at.localeCompare(a.latest_at));
    delete group.petMap;
    delete group.dateMap;
    return group;
  }).sort((a, b) => b.latest_at.localeCompare(a.latest_at));

  groups.forEach((group, index) => {
    group.expanded = Object.prototype.hasOwnProperty.call(expanded, group.key)
      ? expanded[group.key] : index === 0;
  });
  return groups;
}

function refreshGroupSelection(group) {
  let selectedCount = 0;
  group.time_groups.forEach(slot => {
    slot.all_selected = !!slot.items.length && slot.items.every(item => item.selected);
    selectedCount += slot.items.filter(item => item.selected).length;
  });
  group.selected_count = selectedCount;
}

Page({
  data: {
    loading: true, busy: false, error: "", view: "pending",
    completedGroupMode: "pet_date", completedGroupLabel: "动物+日期",
    canDiscontinue: false,
    items: [], groups: [], temporary: [], hospitalizations: [], inventory: [], filteredInventory: [], doctors: [],
    showTemporaryForm: false, drugQuery: "", selectedDrug: null,
    routes: ["静脉注射", "肌肉注射", "皮下注射", "口服", "外用", "滴眼", "其他"],
    selectedHospLabel: "", selectedDoctor: "", selectedRoute: "静脉注射",
    form: { hospitalizationIndex: 0, doctorIndex: 0, routeIndex: 0, dose_actual: "", notes: "" }
  },
  onLoad() {
    const saved = wx.getStorageSync("inpatientMedsCompletedGroupMode");
    const labels = { time: "时间", pet: "动物", pet_date: "动物+日期" };
    if (labels[saved]) this.setData({ completedGroupMode: saved, completedGroupLabel: labels[saved] });
  },
  onShow() { this.loadData(); },
  onPullDownRefresh() { this.loadData(); },
  async loadData() {
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/inpatient-medications", { view: this.data.view });
      const inventory = result.inventory || [];
      const items = result.items || [];
      const groups = this.data.view === "completed"
        ? buildCompletedGroups(items, this.data.completedGroupMode, this.data.groups)
        : buildMedicationGroups(items, this.data.groups);
      this.setData({
        items, groups, temporary: result.temporary || [],
        canDiscontinue: !!((result.permissions || {}).can_discontinue),
        hospitalizations: result.hospitalizations || [], inventory,
        filteredInventory: inventory.slice(0, 20), doctors: result.doctors || [],
        selectedHospLabel: (result.hospitalizations || [])[0] ? result.hospitalizations[0].label : "",
        selectedDoctor: (result.doctors || [])[0] || "",
        selectedRoute: this.data.routes[0]
      });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "住院用药加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  setView(e) {
    const view = e.currentTarget.dataset.view;
    if (!view || view === this.data.view) return;
    this.setData({ view, groups: [], showTemporaryForm: false, selectedDrug: null, drugQuery: "" }, () => this.loadData());
  },
  setCompletedGroupMode(e) {
    const mode = e.currentTarget.dataset.mode;
    const labels = { time: "时间", pet: "动物", pet_date: "动物+日期" };
    if (!labels[mode] || mode === this.data.completedGroupMode) return;
    wx.setStorageSync("inpatientMedsCompletedGroupMode", mode);
    this.setData({
      completedGroupMode: mode,
      completedGroupLabel: labels[mode],
      groups: buildCompletedGroups(this.data.items, mode, [])
    });
  },
  toggleCompletedGroup(e) {
    const key = e.currentTarget.dataset.groupKey || "";
    this.setData({ groups: this.data.groups.map(group => Object.assign({}, group, {
      expanded: group.key === key ? !group.expanded : group.expanded
    })) });
  },
  toggleGroup(e) {
    const hospitalizationId = Number(e.currentTarget.dataset.hospitalizationId || 0);
    const groups = this.data.groups.map(group => Object.assign({}, group, {
      expanded: group.hospitalization_id === hospitalizationId ? !group.expanded : group.expanded
    }));
    this.setData({ groups });
  },
  toggleMedication(e) {
    if (this.data.view !== "pending" || this.data.busy) return;
    const id = Number(e.currentTarget.dataset.id || 0);
    if (!id) return;
    const groups = this.data.groups.map(group => {
      group.time_groups.forEach(slot => slot.items.forEach(item => {
        if (item.id === id) item.selected = !item.selected;
      }));
      refreshGroupSelection(group);
      return group;
    });
    this.setData({ groups });
  },
  toggleTimeGroup(e) {
    if (this.data.busy) return;
    const hospitalizationId = Number(e.currentTarget.dataset.hospitalizationId || 0);
    const timeKey = e.currentTarget.dataset.timeKey || "";
    const groups = this.data.groups.map(group => {
      if (group.hospitalization_id !== hospitalizationId) return group;
      group.time_groups.forEach(slot => {
        if (slot.key !== timeKey) return;
        const shouldSelect = !slot.items.every(item => item.selected);
        slot.items.forEach(item => { item.selected = shouldSelect; });
      });
      refreshGroupSelection(group);
      return group;
    });
    this.setData({ groups });
  },
  batchComplete(e) {
    const hospitalizationId = Number(e.currentTarget.dataset.hospitalizationId || 0);
    const group = this.data.groups.find(row => row.hospitalization_id === hospitalizationId);
    if (!group || !group.selected_count || this.data.busy) return;
    const selected = [];
    group.time_groups.forEach(slot => slot.items.forEach(item => {
      if (item.selected) selected.push(item);
    }));
    wx.showModal({
      title: `确认完成 ${selected.length} 项用药`,
      content: `${group.pet_name}：请确认以上药物均已实际给药。`,
      confirmText: "确认完成", confirmColor: "#1d4d3a",
      success: async res => {
        if (!res.confirm) return;
        this.setData({ busy: true });
        try {
          await staffPost("/api/staff-miniapp/inpatient-medications/batch-check", { ids: selected.map(item => item.id) });
          wx.showToast({ title: `已完成${selected.length}项`, icon: "success" });
          this.loadData();
        } catch (err) {
          wx.showModal({ title: "操作失败", content: (err && err.detail) || "请刷新后重试", showCancel: false });
        } finally { this.setData({ busy: false }); }
      }
    });
  },
  completeTask(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const dose = e.currentTarget.dataset.dose || "";
    if (!id || this.data.busy) return;
    wx.showModal({
      title: "确认已经给药", content: dose ? `实际剂量按处方：${dose}` : "确认该次用药已经执行？",
      confirmText: "确认给药", confirmColor: "#1d4d3a",
      success: async res => {
        if (!res.confirm) return;
        this.setData({ busy: true });
        try {
          await staffPost(`/api/staff-miniapp/inpatient-medications/${id}/check`, { dose_actual: dose });
          wx.showToast({ title: "已完成", icon: "success" }); this.loadData();
        } catch (err) { wx.showModal({ title: "操作失败", content: (err && err.detail) || "请稍后重试", showCancel: false }); }
        finally { this.setData({ busy: false }); }
      }
    });
  },
  skipTask(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    if (!id || this.data.busy) return;
    wx.showModal({
      title: "跳过本次用药", editable: true, placeholderText: "必须填写原因，例如：动物拒绝、医生停药",
      confirmText: "确认跳过", confirmColor: "#7a2828",
      success: async res => {
        if (!res.confirm) return;
        const notes = (res.content || "").trim();
        if (!notes) { wx.showToast({ title: "请填写原因", icon: "none" }); return; }
        this.setData({ busy: true });
        try { await staffPost(`/api/staff-miniapp/inpatient-medications/${id}/skip`, { notes }); this.loadData(); }
        catch (err) { wx.showModal({ title: "操作失败", content: (err && err.detail) || "请稍后重试", showCancel: false }); }
        finally { this.setData({ busy: false }); }
      }
    });
  },
  discontinueTask(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const drugName = e.currentTarget.dataset.drug || "该药品";
    if (!id || this.data.busy) return;
    wx.showModal({
      title: `停止 ${drugName}`,
      content: "将取消本次住院中该药全部未执行剂次，已执行记录和原处方不会删除。",
      editable: true,
      placeholderText: "必须填写医生停药原因",
      confirmText: "确认停药",
      confirmColor: "#7a2828",
      success: async res => {
        if (!res.confirm) return;
        const reason = (res.content || "").trim();
        if (!reason) { wx.showToast({ title: "请填写停药原因", icon: "none" }); return; }
        this.setData({ busy: true });
        try {
          const result = await staffPost(`/api/staff-miniapp/inpatient-medications/${id}/discontinue`, { reason });
          wx.showToast({ title: `已停止${result.cancelled_count || 0}次`, icon: "success" });
          this.loadData();
        } catch (err) {
          wx.showModal({ title: "操作失败", content: (err && err.detail) || "请稍后重试", showCancel: false });
        } finally { this.setData({ busy: false }); }
      }
    });
  },
  undoTask(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    if (!id || this.data.busy) return;
    wx.showModal({ title: "撤销执行记录", content: "撤销后会重新回到待执行列表。", confirmText: "确认撤销", success: async res => {
      if (!res.confirm) return;
      this.setData({ busy: true });
      try { await staffPost(`/api/staff-miniapp/inpatient-medications/${id}/uncheck`, {}); this.loadData(); }
      catch (err) { wx.showModal({ title: "操作失败", content: (err && err.detail) || "请稍后重试", showCancel: false }); }
      finally { this.setData({ busy: false }); }
    }});
  },
  openTemporaryForm() {
    if (!this.data.hospitalizations.length) { wx.showToast({ title: "当前没有住院动物", icon: "none" }); return; }
    this.setData({ showTemporaryForm: true });
  },
  closeTemporaryForm() { this.setData({ showTemporaryForm: false, selectedDrug: null, drugQuery: "" }); },
  onDrugQuery(e) {
    const q = (e.detail.value || "").trim().toLowerCase();
    const rows = this.data.inventory.filter(item => !q || item.name.toLowerCase().indexOf(q) >= 0).slice(0, 20);
    this.setData({ drugQuery: e.detail.value, filteredInventory: rows, selectedDrug: null });
  },
  selectDrug(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const item = this.data.inventory.find(row => row.id === id) || null;
    this.setData({ selectedDrug: item, drugQuery: item ? item.name : "", filteredInventory: [] });
  },
  onHospChange(e) { const i = Number(e.detail.value || 0); this.setData({ "form.hospitalizationIndex": i, selectedHospLabel: (this.data.hospitalizations[i] || {}).label || "" }); },
  onDoctorChange(e) { const i = Number(e.detail.value || 0); this.setData({ "form.doctorIndex": i, selectedDoctor: this.data.doctors[i] || "" }); },
  onRouteChange(e) { const i = Number(e.detail.value || 0); this.setData({ "form.routeIndex": i, selectedRoute: this.data.routes[i] || "" }); },
  onDoseInput(e) { this.setData({ "form.dose_actual": e.detail.value }); },
  onNotesInput(e) { this.setData({ "form.notes": e.detail.value }); },
  async submitTemporary() {
    if (this.data.busy) return;
    const hosp = this.data.hospitalizations[this.data.form.hospitalizationIndex];
    const doctor = this.data.doctors[this.data.form.doctorIndex];
    const route = this.data.routes[this.data.form.routeIndex];
    const dose = (this.data.form.dose_actual || "").trim();
    if (!hosp || !this.data.selectedDrug || !doctor || !route || !dose) {
      wx.showToast({ title: "请填写全部必填项", icon: "none" }); return;
    }
    this.setData({ busy: true });
    try {
      const result = await staffPost("/api/staff-miniapp/inpatient-medications/temporary", {
        hospitalization_id: hosp.id, inventory_item_id: this.data.selectedDrug.id,
        dose_actual: dose, route, ordered_by: doctor, notes: this.data.form.notes || ""
      });
      wx.showModal({ title: "临时用药已记录", content: result.message || "等待医生补开正式处方", showCancel: false });
      this.setData({ showTemporaryForm: false, selectedDrug: null, drugQuery: "", "form.dose_actual": "", "form.notes": "" });
      this.loadData();
    } catch (err) { wx.showModal({ title: "记录失败", content: (err && err.detail) || "请稍后重试", showCancel: false }); }
    finally { this.setData({ busy: false }); }
  },
  voidTemporary(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    wx.showModal({ title: "作废临时用药", content: "仅用于误录。作废后不会进入待补处方。", confirmText: "确认作废", confirmColor: "#7a2828", success: async res => {
      if (!res.confirm) return;
      try { await staffPost(`/api/staff-miniapp/inpatient-medications/temporary/${id}/void`, {}); this.loadData(); }
      catch (err) { wx.showModal({ title: "作废失败", content: (err && err.detail) || "请稍后重试", showCancel: false }); }
    }});
  }
});

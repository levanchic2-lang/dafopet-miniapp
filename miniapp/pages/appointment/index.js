const { getJson, postJson } = require("../../utils/api");

const BEAUTY_ADDON_OPTIONS = ["去浮毛", "SPA", "护发素", "纯手剪", "药浴", "去油"];
const BEAUTY_COAT_OPTIONS  = ["长毛", "短毛"];
const BEAUTY_SIZE_DOG      = ["微小型犬（4kg 以下）", "小型犬（4–10kg）", "中型犬（10–15kg）", "中大型犬（15–25kg）", "大型犬（25kg 以上）"];
const BEAUTY_SIZE_CAT      = ["大型猫（4kg 以上）", "中型猫（2.5–4kg）", "小型猫（2.5kg 以下）"];

function todayString() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

Page({
  data: {
    loading: true,
    submitting: false,
    categories: [],
    stores: [],
    petGenders: [],
    serviceOptions: [],
    selectedCategoryMeta: null,
    selectedServiceMeta: null,
    timeSlots: [],
    dateStart: "",
    timeRangeStart: "",
    timeRangeEnd: "",
    categoryIndex: 0,
    serviceIndex: 0,
    storeIndex: 0,
    petGenderIndex: 0,
    // 美容专属
    isBeauty: false,
    beautyAddonOptions: BEAUTY_ADDON_OPTIONS.map(n => ({ name: n, checked: false })),
    beautySizeOptions: [],
    beautyCoatOptions: BEAUTY_COAT_OPTIONS,
    beautySizeIndex: 0,
    beautyCoatIndex: 0,
    // 美容时段
    beautyDuration: 0,
    beautyDurationDisplay: "",
    beautySlots: [],
    beautySlotIndex: -1,
    beautySlotLoading: false,
    beautySlotError: "",
    beautyDisclaimer: "以上占用时间为估算时间。如动物不配合或毛量超出预估，实际服务时长可能有所浮动，具体以门店实际执行时间为准。",
    form: {
      category: "",
      service_name: "",
      duration_minutes: 30,
      store: "",
      appointment_date: "",
      appointment_time: "13:00",
      customer_name: "",
      phone: "",
      pet_name: "",
      pet_gender: "unknown",
      related_application_id: "",
      notes: "",
      // 美容附加（提交时填充）
      pet_size: "",
      coat_length: "",
      addon_services: ""
    }
  },

  onLoad(options) {
    if (options) {
      this._prefill = {};
      if (options.from_app)      this._prefill.related_application_id = options.from_app;
      if (options.store)         this._prefill.store = decodeURIComponent(options.store);
      if (options.customer_name) this._prefill.customer_name = decodeURIComponent(options.customer_name);
      if (options.phone)         this._prefill.phone = decodeURIComponent(options.phone);
      if (options.pet_name)      this._prefill.pet_name = decodeURIComponent(options.pet_name);
      if (options.pet_gender)    this._prefill.pet_gender = decodeURIComponent(options.pet_gender);
      if (options.category)      this._prefill.category = decodeURIComponent(options.category);
    }
    this.loadConfig();
  },

  async ensureOpenid() {
    try {
      const saved = wx.getStorageSync("WECHAT_OPENID") || "";
      if (saved) return String(saved);
    } catch (e) {}
    const loginRes = await wx.login();
    if (!loginRes || !loginRes.code) throw new Error("微信登录失败");
    const data = await postJson("/api/wechat/login", { code: loginRes.code });
    const openid = (data && data.openid) || "";
    if (!openid) throw new Error("未获取到 openid");
    try { wx.setStorageSync("WECHAT_OPENID", openid); } catch (e) {}
    return openid;
  },

  async loadConfig() {
    this.setData({ loading: true });
    try {
      // 并行拉取配置 + 用户 TNR 审核状态
      const openid = await this.ensureOpenid().catch(() => "");
      const [cfg, tnrStatus] = await Promise.all([
        getJson("/api/appointments/config"),
        openid ? getJson("/api/wechat/my-tnr-status?openid=" + encodeURIComponent(openid)).catch(() => ({ has_approved: false })) : Promise.resolve({ has_approved: false }),
      ]);
      const hasApprovedTnr = !!(tnrStatus && tnrStatus.has_approved);
      this._hasApprovedTnr = hasApprovedTnr;

      let categories  = Array.isArray(cfg.categories)  ? cfg.categories  : [];
      // TNR 类别只有已通过申请的用户才能选择
      if (!hasApprovedTnr) {
        categories = categories.filter(c => c.value !== "tnr");
      }
      const stores      = Array.isArray(cfg.stores)       ? cfg.stores      : [];
      const petGenders  = Array.isArray(cfg.pet_genders)  ? cfg.pet_genders : [];
      const bookingWindow = cfg.booking_window || {};
      const firstCategory = categories[0] || { value: "", services: [], time_slots: [] };
      const serviceOptions = Array.isArray(firstCategory.services) ? firstCategory.services : [];
      const firstService  = serviceOptions[0] || { name: "", duration_minutes: 30 };
      const firstStore    = stores[0] || "";
      const firstGender   = petGenders[0] || { value: "unknown" };
      const startDate     = bookingWindow.start_date || todayString();
      const timeRange     = firstCategory.time_range || {};
      const firstTime =
        (Array.isArray(firstCategory.time_slots) && firstCategory.time_slots[0]) ||
        timeRange.start || firstService.default_time || "13:00";

      const isBeauty = firstCategory.value === "beauty";
      const initSizeOpts = isBeauty ? this._beautySizeFor(firstService.name) : [];
      this.setData({
        loading: false,
        categories,
        stores,
        petGenders,
        serviceOptions,
        selectedCategoryMeta: firstCategory,
        selectedServiceMeta: firstService,
        timeSlots: Array.isArray(firstCategory.time_slots) ? firstCategory.time_slots : [],
        dateStart: startDate,
        timeRangeStart: timeRange.start || "",
        timeRangeEnd:   timeRange.end   || "",
        isBeauty,
        beautySizeOptions: initSizeOpts,
        beautySizeIndex: 0,
        beautyCoatIndex: 0,
        "form.category":          firstCategory.value || "",
        "form.service_name":      firstService.name   || "",
        "form.duration_minutes":  firstService.duration_minutes || 30,
        "form.store":             firstStore,
        "form.pet_gender":        firstGender.value || "unknown",
        "form.appointment_date":  startDate,
        "form.appointment_time":  firstTime,
        "form.pet_size":    isBeauty ? (initSizeOpts[0] || "") : "",
        "form.coat_length": isBeauty ? (BEAUTY_COAT_OPTIONS[0] || "") : "",
      });
      this._applyPrefill(categories, stores, petGenders);
    } catch (e) {
      this.setData({ loading: false });
      wx.showModal({
        title: "加载预约配置失败",
        content: (e && (e.detail || e.message || e.errMsg)) || "请检查本地联调后端是否已启动。",
        showCancel: false
      });
    }
  },

  // ── 本地估算时长（与后端 _calc_beauty_duration 逻辑一致） ──
  _calcBeautyDuration(sn, sz, cl, addonOptions) {
    const isLong  = cl === "长毛";
    const isWash  = sn.indexOf("洗护") >= 0;
    let base = 60;
    if (sn.indexOf("犬") >= 0) {
      if      (sz.indexOf("微小型") >= 0) base = isWash ? 30  : 90;
      else if (sz.indexOf("小型犬") >= 0) base = isWash ? 60  : 120;
      else if (sz.indexOf("中大型") >= 0) base = isWash ? (isLong ? 120 : 90)  : 180;
      else if (sz.indexOf("中型犬") >= 0) base = isWash ? (isLong ? 90  : 60)  : 150;
      else if (sz.indexOf("大型犬") >= 0) base = isWash ? (isLong ? 150 : 120) : 210;
    } else if (sn.indexOf("猫") >= 0) {
      if      (sz.indexOf("大型猫") >= 0) base = isWash ? (isLong ? 150 : 120) : 150;
      else if (sz.indexOf("中型猫") >= 0) base = isWash ? (isLong ? 120 : 90)  : 120;
      else if (sz.indexOf("小型猫") >= 0) base = isWash ? (isLong ? 90  : 60)  : 120;
    }
    const checked = Array.isArray(addonOptions) ? addonOptions.filter(a => a.checked).length : 0;
    base += checked * 30;
    return base;
  },

  _durationDisplay(mins) {
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    if (h === 0) return `${m}分钟`;
    return m ? `${h}小时${m}分钟` : `${h}小时`;
  },

  // ── 触发时段查询（防抖 400ms） ──
  scheduleBeautySlots() {
    if (this._beautySlotTimer) clearTimeout(this._beautySlotTimer);
    this._beautySlotTimer = setTimeout(() => this.fetchBeautySlots(), 400);
  },

  async fetchBeautySlots() {
    const { form, beautyAddonOptions } = this.data;
    const sn = form.service_name  || "";
    const sz = form.pet_size      || "";
    const cl = form.coat_length   || "";

    // 先本地计算时长，立即更新显示
    const dur = this._calcBeautyDuration(sn, sz, cl, beautyAddonOptions);
    const durDisplay = this._durationDisplay(dur);
    const patch = {
      beautyDuration: dur,
      beautyDurationDisplay: durDisplay,
      "form.duration_minutes": dur,
    };

    if (!sn || !sz || !cl) {
      this.setData(Object.assign(patch, { beautySlots: [], beautySlotIndex: -1 }));
      return;
    }

    // 若无日期，只更新时长不查时段
    if (!form.appointment_date || !form.store) {
      this.setData(Object.assign(patch, { beautySlots: [], beautySlotIndex: -1 }));
      return;
    }

    this.setData(Object.assign(patch, { beautySlotLoading: true, beautySlotError: "" }));
    try {
      const checked = beautyAddonOptions.filter(a => a.checked).map(a => a.name).join(",");
      const qs = `service=${encodeURIComponent(sn)}&pet_size=${encodeURIComponent(sz)}&coat_length=${encodeURIComponent(cl)}&addons=${encodeURIComponent(checked)}&date=${form.appointment_date}&store=${encodeURIComponent(form.store)}`;
      const res = await getJson(`/api/appointments/beauty-slots?${qs}`);
      const slots = res.available_slots || [];
      const slotIndex = slots.length > 0 ? 0 : -1;
      this.setData({
        beautySlotLoading: false,
        beautySlots: slots,
        beautySlotIndex: slotIndex,
        "form.appointment_time": slotIndex >= 0 ? slots[0] : form.appointment_time,
        "form.duration_minutes": res.duration_minutes || dur,
      });
    } catch (e) {
      const msg = (e && (e.errMsg || e.detail || e.message)) ? `获取时段失败（${e.errMsg || e.detail || e.message}）` : "获取可预约时段失败，请稍后重试。";
      this.setData({
        beautySlotLoading: false,
        beautySlotError: msg,
      });
    }
  },

  onRetryBeautySlots() {
    this.setData({ beautySlotError: "" });
    this.fetchBeautySlots();
  },

  onBeautySlotTap(e) {
    const idx  = Number(e.currentTarget.dataset.idx);
    const time = e.currentTarget.dataset.time || "";
    this.setData({ beautySlotIndex: idx, "form.appointment_time": time });
  },

  _beautySizeFor(serviceName) {
    if (!serviceName) return [];
    if (serviceName.indexOf("犬") >= 0) return BEAUTY_SIZE_DOG;
    if (serviceName.indexOf("猫") >= 0) return BEAUTY_SIZE_CAT;
    return [];
  },

  _applyPrefill(categories, stores, petGenders) {
    const p = this._prefill;
    if (!p) return;
    const patch = {};
    if (p.category) {
      const catIdx = categories.findIndex((c) => c.value === p.category);
      if (catIdx >= 0) {
        const cat  = categories[catIdx];
        const svcs = Array.isArray(cat.services) ? cat.services : [];
        const firstSvc = svcs[0] || { name: "", duration_minutes: 30 };
        const slots = Array.isArray(cat.time_slots) ? cat.time_slots : [];
        const isBeauty = cat.value === "beauty";
        Object.assign(patch, {
          categoryIndex: catIdx,
          serviceIndex: 0,
          serviceOptions: svcs,
          selectedCategoryMeta: cat,
          selectedServiceMeta: firstSvc,
          timeSlots: slots,
          isBeauty,
          beautySizeOptions: isBeauty ? this._beautySizeFor(firstSvc.name) : [],
          "form.category":         cat.value,
          "form.service_name":     firstSvc.name || "",
          "form.duration_minutes": firstSvc.duration_minutes || 30,
          "form.appointment_time": slots[0] || this.data.form.appointment_time
        });
      }
    }
    if (p.store) {
      const stIdx = stores.indexOf(p.store);
      if (stIdx >= 0) { patch.storeIndex = stIdx; patch["form.store"] = p.store; }
    }
    if (p.customer_name) patch["form.customer_name"] = p.customer_name;
    if (p.phone)         patch["form.phone"]         = p.phone;
    if (p.pet_name)      patch["form.pet_name"]      = p.pet_name;
    if (p.pet_gender) {
      const gIdx = petGenders.findIndex((g) => g.value === p.pet_gender);
      if (gIdx >= 0) { patch.petGenderIndex = gIdx; patch["form.pet_gender"] = p.pet_gender; }
    }
    if (p.related_application_id) patch["form.related_application_id"] = p.related_application_id;
    if (Object.keys(patch).length) this.setData(patch);
    this._prefill = null;
  },

  onCategoryChange(e) {
    const idx = Number(e.detail.value || 0);
    const category     = this.data.categories[idx] || { value: "", services: [], time_slots: [] };
    const serviceOptions = Array.isArray(category.services) ? category.services : [];
    const firstService  = serviceOptions[0] || { name: "", duration_minutes: 30 };
    const timeSlots     = Array.isArray(category.time_slots) ? category.time_slots : [];
    const timeRange     = category.time_range || {};
    const nextTime = timeSlots[0] || timeRange.start || firstService.default_time || this.data.form.appointment_time || "10:00";
    const isBeauty = category.value === "beauty";
    const stores = this.data.stores;
    // 美容只能横岗店
    let storeIndex = this.data.storeIndex;
    let storeVal   = this.data.form.store;
    if (isBeauty) {
      const gangIdx = stores.findIndex(s => s.indexOf("横岗") >= 0);
      if (gangIdx >= 0) { storeIndex = gangIdx; storeVal = stores[gangIdx]; }
    }
    this.setData({
      categoryIndex: idx,
      serviceIndex: 0,
      serviceOptions,
      selectedCategoryMeta: category,
      selectedServiceMeta: firstService,
      timeSlots,
      timeRangeStart: timeRange.start || "",
      timeRangeEnd:   timeRange.end   || "",
      isBeauty,
      storeIndex,
      beautySizeOptions: isBeauty ? this._beautySizeFor(firstService.name) : [],
      beautySizeIndex: 0,
      beautyCoatIndex: 0,
      beautyAddonOptions: BEAUTY_ADDON_OPTIONS.map(n => ({ name: n, checked: false })),
      "form.category":         category.value || "",
      "form.service_name":     firstService.name || "",
      "form.duration_minutes": firstService.duration_minutes || 30,
      "form.appointment_time": nextTime,
      "form.store":            storeVal,
      "form.related_application_id": category.supports_related_application ? this.data.form.related_application_id : "",
      "form.pet_size":    isBeauty ? (this._beautySizeFor(firstService.name)[0] || "") : "",
      "form.coat_length": isBeauty ? (BEAUTY_COAT_OPTIONS[0] || "") : "",
      "form.addon_services": ""
    });
    if (isBeauty) this.scheduleBeautySlots();
  },

  onServiceChange(e) {
    const idx = Number(e.detail.value || 0);
    const row = this.data.serviceOptions[idx] || { name: "", duration_minutes: 30 };
    const patch = {
      serviceIndex: idx,
      selectedServiceMeta: row,
      "form.service_name":     row.name || "",
      "form.duration_minutes": row.duration_minutes || 30
    };
    if (this.data.isBeauty) {
      const newSizeOpts = this._beautySizeFor(row.name);
      patch.beautySizeOptions       = newSizeOpts;
      patch.beautySizeIndex         = 0;
      patch["form.pet_size"]        = newSizeOpts[0] || "";
      // 毛发长度保持当前选中值（或重置为默认第一项）
      if (!this.data.form.coat_length) {
        patch["form.coat_length"] = BEAUTY_COAT_OPTIONS[0] || "";
        patch.beautyCoatIndex     = 0;
      }
    }
    // 门诊：时长由科目决定（疫苗/驱虫=30，其余=60）
    if (this.data.form.category === "outpatient") {
      const isVaccine = row.name && (row.name.indexOf("疫苗") >= 0 || row.name.indexOf("驱虫") >= 0);
      patch["form.duration_minutes"] = isVaccine ? 30 : 60;
    }
    this.setData(patch);
    if (this.data.isBeauty) this.scheduleBeautySlots();
  },

  onStoreChange(e) {
    const idx = Number(e.detail.value || 0);
    this.setData({ storeIndex: idx, "form.store": this.data.stores[idx] || "" });
  },

  onPetGenderChange(e) {
    const idx = Number(e.detail.value || 0);
    const row = this.data.petGenders[idx] || { value: "unknown" };
    this.setData({ petGenderIndex: idx, "form.pet_gender": row.value || "unknown" });
  },

  onDateChange(e) {
    this.setData({ "form.appointment_date": e.detail.value || "" });
    if (this.data.isBeauty) this.scheduleBeautySlots();
  },
  onTimeChange(e)    { this.setData({ "form.appointment_time": e.detail.value || "" }); },
  onQuickTimeTap(e)  { const t = e.currentTarget.dataset.time || ""; if (t) this.setData({ "form.appointment_time": t }); },
  onInput(e)         { const k = e.currentTarget.dataset.k; this.setData({ [`form.${k}`]: e.detail.value || "" }); },

  // 美容：体型选择
  onBeautySizeChange(e) {
    const idx = Number(e.detail.value || 0);
    this.setData({
      beautySizeIndex: idx,
      "form.pet_size": this.data.beautySizeOptions[idx] || ""
    });
    this.scheduleBeautySlots();
  },

  // 美容：毛发长度
  onBeautyCoatChange(e) {
    const idx = Number(e.detail.value || 0);
    this.setData({
      beautyCoatIndex: idx,
      "form.coat_length": BEAUTY_COAT_OPTIONS[idx] || ""
    });
    this.scheduleBeautySlots();
  },

  // 美容：附加服务 checkbox toggle
  onAddonTap(e) {
    const idx = Number(e.currentTarget.dataset.idx);
    const list = this.data.beautyAddonOptions.map((item, i) =>
      i === idx ? { ...item, checked: !item.checked } : item
    );
    this.setData({ beautyAddonOptions: list });
    if (this.data.isBeauty) this.scheduleBeautySlots();
  },

  async onSubmit() {
    if (this.data.submitting) return;
    this.setData({ submitting: true });
    try {
      const openid = await this.ensureOpenid();
      const payload = Object.assign({}, this.data.form, { openid });
      // 美容：附加字段
      if (this.data.isBeauty) {
        const checked = this.data.beautyAddonOptions.filter(a => a.checked).map(a => a.name);
        payload.addon_services = checked.join(",");
        payload.pet_size    = this.data.form.pet_size;
        payload.coat_length = this.data.form.coat_length;
      }
      const res = await postJson("/api/appointments/create", payload);
      // 预约提交成功后，趁用户刚操作，订阅预约状态和手术完成通知（≤3个，不超微信限制）
      try {
        const _c = (k) => { try { return wx.getStorageSync(k) || ""; } catch(e2) { return ""; } };
        const apptTmpl = _c("WECHAT_TMPL_APPOINTMENT");
        const surgTmpl = _c("WECHAT_TMPL_SURGERY_DONE");
        const apptIds = [apptTmpl, surgTmpl].filter(Boolean);
        if (apptIds.length) await wx.requestSubscribeMessage({ tmplIds: apptIds });
      } catch(e2) { /* 订阅失败不阻断跳转 */ }
      wx.showToast({ title: "预约已提交", icon: "success" });
      if (res && res.appointment && res.appointment.id) {
        wx.navigateTo({ url: `/pages/appointment/list?highlight=${res.appointment.id}` });
      } else {
        wx.navigateTo({ url: "/pages/appointment/list" });
      }
    } catch (e) {
      wx.showModal({
        title: "提交预约失败",
        content: (e && (e.detail || e.message || e.errMsg)) || "请稍后重试。",
        showCancel: false
      });
    } finally {
      this.setData({ submitting: false });
    }
  }
});

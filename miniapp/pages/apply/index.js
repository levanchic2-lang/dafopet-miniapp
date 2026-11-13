const { postJson, getJson, wxLoginCode } = require("../../utils/api");
const { runWithPrivacyGuard } = require("../../utils/privacy");
const subscribe = require("../../utils/subscribe");
const draftLib = require("../../utils/apply_draft");
// 优先 .js 模块（require 更可靠），缺失时退 .json 兼容
const shenzhenRegionsLocal = (() => {
  try { return require("../../utils/shenzhen_regions.js"); } catch (e) {}
  try { return require("../../utils/shenzhen_regions.json"); } catch (e) {}
  return null;
})();

function maskPhone(s) {
  if (!s) return "";
  const t = String(s);
  if (t.length < 7) return t;
  return t.slice(0, 3) + "****" + t.slice(-4);
}

Page({
  onShareAppMessage() {
    return { title: "大风动物医院 · 流浪猫 TNR 申请", path: "/pages/apply/index" };
  },
  onShareTimeline() {
    return { title: "大风动物医院 · 流浪猫 TNR 申请" };
  },
  data: {
    form: {
      applicant_name: "",
      phone: "",
      address: "",
      clinic_store: "大风动物医院（东环店）",
      location_lat: "",
      location_lng: "",
      location_address: "",
      id_number: "",
      post_surgery_plan: "原地放归",
      cat_nickname: "",
      cat_gender: "male",
      cat_breed: "",
      cat_color: "",
      age_estimate: "",
      health_note: "",
      suture_preference: "external"
    },
    storeOptions: [
      { value: "", label: "请选择门店" },
      { value: "大风动物医院（东环店）", label: "大风动物医院（东环店）" },
      { value: "大风动物医院（横岗店）", label: "大风动物医院（横岗店）" }
    ],
    storeIndex: 0,
    planOptions: [
      { value: "原地放归", label: "原地放归" },
      { value: "短期术后寄养 / 笼养观察后放归", label: "短期术后寄养 / 笼养观察后放归" },
      { value: "在固定喂养点长期管理", label: "在固定喂养点长期管理" },
      { value: "社会化后尝试送养/找领养", label: "社会化后尝试送养/找领养" },
      { value: "留作长期安置 / 中途 / 收编", label: "留作长期安置 / 中途 / 收编" },
      { value: "转移到更安全区域放归安置", label: "转移到更安全区域放归安置" }
    ],
    planIndex: 0,
    genderOptions: [
      { value: "male", label: "公猫" },
      { value: "female", label: "母猫" },
      { value: "unknown", label: "未知" }
    ],
    genderIndex: 0,
    sutureOptions: [
      { value: "external", label: "皮外缝合（需返院复查拆线）" },
      { value: "intradermal", label: "皮内可吸收缝合（通常无需拆线）" }
    ],
    sutureIndex: 0,
    checks: { ear: true, fraud: true },
    images: [],
    videos: [],
    mediaReady: false,  // 必须至少 2 张图片；视频仅作补充
    submitting: false,
    result: null,
    error: "",
    notifyStatusText: "",
    notifyReady: false,
    notifyPrompt: false,
    notifyOpenSettings: false,
    openid: "",
    idConsent: false,
    isProxy: false,
    proxyName: "",
    proxyPhone: "",
    proxyRelationOptions: ["请选择关系", "家人", "朋友", "同事/员工代录", "志愿者", "其他"],
    proxyRelationIndex: 0,
    proxyConsent: false,
    addrReady: false,
    cityNames: ["深圳市", "东莞市", "惠州市"],
    cityIndex: 0,
    districtNames: ["加载中…"],
    streetNames: ["请先选区"],
    districtIndex: 0,
    streetIndex: 0,
    addressDetailInput: "",
    healthNoteSeed: "",
    healthNoteSeedKey: 0,
    formEpoch: 1
  },

  onHealthNoteInput(e) {
    // 文本在组件内部更新，页面不 setData，避免整页重排把滚动条拉回顶部。
    this._healthNoteDraft = e && e.detail ? e.detail.value : "";
    this._scheduleDraftSave();
  },

  onHealthNoteBlur(e) {
    const v = (e && e.detail ? e.detail.value : "") || "";
    this._healthNoteDraft = v;
    this.setData({ "form.health_note": v });
    this._scheduleDraftSave();
  },

  onLoad() {
    this._loadShenzhenRegions();
    this._bootDraftAndPrefill();
    this._syncNotifyState();
    if (this.data.form.location_lat && this.data.form.location_lng) return;
    this._autoGetLocationOnce();
  },

  onShow() {
    if (!this.data.addrReady) this._loadShenzhenRegions();
    this._syncNotifyState();
    subscribe.prefetch("review");
    this._refreshNotifyPrompt();
    if (!this._booted) this._bootDraftAndPrefill();
  },

  onHide() {
    this._saveDraftNow();
  },

  onUnload() {
    this._saveDraftNow();
  },

  _bootDraftAndPrefill() {
    if (this._booted) return;
    this._booted = true;
    let raw = null;
    try { raw = wx.getStorageSync(draftLib.DRAFT_KEY); } catch (e) { raw = null; }
    const inspected = draftLib.inspectDraft(raw, Date.now());
    if (inspected.action === "expired") {
      this._removeStoredFiles(inspected.removeFiles);
      try { wx.removeStorageSync(draftLib.DRAFT_KEY); } catch (e) {}
      this._prefillPersonal();
      return;
    }
    if (inspected.action !== "restore") {
      this._prefillPersonal();
      return;
    }
    this._restoredDraft = true;
    this._allowDraftSave = true;
    this._applyDraftFields(inspected.draft);
    this._pruneRestoredMedia(inspected.draft.images, inspected.draft.videos);
  },

  async _prefillPersonal() {
    if (this._restoredDraft) return;
    const gen = (this._prefillGen || 0) + 1;
    this._prefillGen = gen;
    let profile = null;
    let tnr = null;
    try {
      const code = await wxLoginCode();
      if (gen !== this._prefillGen || this._restoredDraft) return;
      profile = await getJson("/api/wechat/my-profile", { code: code });
    } catch (e) {
      profile = null;
    }
    try {
      if (gen !== this._prefillGen || this._restoredDraft) return;
      const code = await wxLoginCode();
      if (gen !== this._prefillGen || this._restoredDraft) return;
      tnr = await postJson("/api/wechat/tnr-prefill", { code: code });
    } catch (e) {
      tnr = null;
    }
    if (gen !== this._prefillGen || this._restoredDraft) return;
    let local = null;
    try { local = wx.getStorageSync(draftLib.LAST_PERSONAL_KEY) || null; } catch (e) { local = null; }
    const form = Object.assign({}, this.data.form);
    // 门店选择器初始停在「请选择」，默认值不能挡住上次提交的门店。
    if ((this.data.storeIndex || 0) === 0) form.clinic_store = "";
    const resolved = draftLib.resolvePersonalPrefill({
      profile: profile,
      tnr: tnr,
      local: local,
      form: form,
    });
    this._applyResolvedPersonal(resolved, gen);
  },

  _applyResolvedPersonal(resolved, gen) {
    if (!resolved || this._restoredDraft) return;
    if (gen != null && gen !== this._prefillGen) return;
    const fields = resolved.patch || {};
    const patch = {};
    Object.keys(fields).forEach((key) => {
      patch["form." + key] = fields[key];
    });
    if (fields.clinic_store) {
      const store = draftLib.canonicalStore(fields.clinic_store);
      patch["form.clinic_store"] = store;
      patch.storeIndex = draftLib.storeIndexFor(this.data.storeOptions, store);
    }
    if (!this._userEdited) patch.idConsent = false;
    const done = () => {
      if (this._restoredDraft) return;
      if (gen != null && gen !== this._prefillGen) return;
      if (resolved.structured && (resolved.structured.districtName || resolved.structured.detail || resolved.structured.streetName)) {
        this._showAddress(resolved.structured);
        return;
      }
      if (fields.address && !String(this.data.addressDetailInput || "").trim()) {
        this._showAddress({ rawAddress: fields.address });
      }
    };
    if (Object.keys(patch).length) this.setData(patch, done);
    else done();
  },

  _scheduleDraftSave() {
    this._draftClosed = false;
    this._userEdited = true;
    this._allowDraftSave = true;
    if (this._draftTimer) clearTimeout(this._draftTimer);
    this._draftTimer = setTimeout(() => {
      this._draftTimer = null;
      this._saveDraftNow();
    }, draftLib.DRAFT_DEBOUNCE_MS);
  },

  _saveDraftNow() {
    if (this._draftClosed || !this._allowDraftSave) return;
    if (this._draftTimer) {
      clearTimeout(this._draftTimer);
      this._draftTimer = null;
    }
    const draft = draftLib.buildDraft(this._collectDraftInput(), Date.now());
    try {
      if (!draftLib.draftHasContent(draft)) {
        if (this._restorePending) return;
        wx.removeStorageSync(draftLib.DRAFT_KEY);
        return;
      }
      wx.setStorageSync(draftLib.DRAFT_KEY, draft);
    } catch (e) {}
  },

  _persistMediaFile(tempPath) {
    const fsm = wx.getFileSystemManager();
    const root = (wx.env && wx.env.USER_DATA_PATH) || "";
    if (!root || !tempPath) return Promise.resolve(tempPath);
    const ext = (String(tempPath).match(/(\.[A-Za-z0-9]{1,8})$/) || [])[1] || "";
    const dest = root.replace(/\/+$/, "") + "/tnr_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8) + ext;
    return new Promise((resolve) => {
      const copy = () => {
        fsm.copyFile({
          srcPath: tempPath,
          destPath: dest,
          success: () => resolve(dest),
          fail: () => resolve(tempPath),
        });
      };
      fsm.saveFile({
        tempFilePath: tempPath,
        filePath: dest,
        success: (res) => resolve((res && res.savedFilePath) || dest),
        fail: () => copy(),
      });
    });
  },

  _removeStoredFiles(paths) {
    const root = (wx.env && wx.env.USER_DATA_PATH) || "";
    let fsm = null;
    try { fsm = wx.getFileSystemManager(); } catch (e) { return; }
    draftLib.filesToDelete(paths, root).forEach((filePath) => {
      try { fsm.unlink({ filePath: filePath, fail() {} }); } catch (e) {}
    });
  },

  _addressNames() {
    const ignore = {
      "请选择": true,
      "请先选区": true,
      "加载中…": true,
      "该城市无数据": true,
      "该区无街道数据": true,
      "请检查网络与 apiBase 后重进": true,
    };
    const city = (this.data.cityNames || [])[this.data.cityIndex] || "";
    const district = (this.data.districtNames || [])[this.data.districtIndex] || "";
    const street = (this.data.streetNames || [])[this.data.streetIndex] || "";
    return {
      cityName: ignore[city] ? "" : city,
      districtName: ignore[district] ? "" : district,
      streetName: ignore[street] ? "" : street,
    };
  },

  _collectDraftInput() {
    return Object.assign({
      form: this.data.form,
      healthNoteDraft: this._healthNoteDraft,
      addressDetailInput: this.data.addressDetailInput,
      cityIndex: this.data.cityIndex,
      checks: this.data.checks,
      idConsent: this.data.idConsent,
      isProxy: this.data.isProxy,
      proxyName: this.data.proxyName,
      proxyPhone: this.data.proxyPhone,
      proxyRelationIndex: this.data.proxyRelationIndex,
      proxyConsent: this.data.proxyConsent,
      planIndex: this.data.planIndex,
      genderIndex: this.data.genderIndex,
      sutureIndex: this.data.sutureIndex,
      storeIndex: this.data.storeIndex,
      images: this.data.images,
      videos: this.data.videos,
    }, this._addressNames());
  },

  _applyDraftFields(draft) {
    this._restorePending = true;
    const form = Object.assign(draftLib.blankForm(), this.data.form, draft.form || {});
    form.clinic_store = draftLib.canonicalStore(form.clinic_store);
    const patch = {
      form: form,
      checks: draft.checks || { ear: true, fraud: true },
      idConsent: !!draft.idConsent,
      isProxy: !!draft.isProxy,
      proxyName: draft.proxyName || "",
      proxyPhone: draft.proxyPhone || "",
      proxyRelationIndex: Number(draft.proxyRelationIndex || 0),
      proxyConsent: !!draft.proxyConsent,
      planIndex: draftLib.optionIndex(this.data.planOptions, form.post_surgery_plan, draft.planIndex || 0),
      genderIndex: draftLib.optionIndex(this.data.genderOptions, form.cat_gender, draft.genderIndex || 0),
      sutureIndex: draftLib.optionIndex(this.data.sutureOptions, form.suture_preference, draft.sutureIndex || 0),
      storeIndex: draftLib.storeIndexFor(this.data.storeOptions, form.clinic_store),
      images: draft.images || [],
      videos: draft.videos || [],
      mediaReady: (draft.images || []).length >= 2,
      healthNoteSeed: form.health_note || "",
      healthNoteSeedKey: (this.data.healthNoteSeedKey || 0) + 1,
      formEpoch: (this.data.formEpoch || 0) + 1,
      addressDetailInput: draft.addressDetailInput || "",
    };
    this._healthNoteDraft = typeof form.health_note === "string" ? form.health_note : "";
    this.setData(patch, () => {
      this._restorePending = false;
      if (draft.districtName || draft.streetName) {
        this._showAddress({
          cityName: draft.cityName || "",
          districtName: draft.districtName || "",
          streetName: draft.streetName || "",
          detail: draft.addressDetailInput || "",
        });
        return;
      }
      if (form.address) this._showAddress({ rawAddress: form.address });
    });
  },

  async _pruneRestoredMedia(images, videos) {
    const original = [].concat(images || [], videos || []);
    const accessMap = {};
    let fsm = null;
    try { fsm = wx.getFileSystemManager(); } catch (e) { fsm = null; }
    const can = (filePath) => new Promise((ok) => {
      if (!fsm || !filePath) { ok(false); return; }
      fsm.access({ path: filePath, success: () => ok(true), fail: () => ok(false) });
    });
    for (let i = 0; i < original.length; i += 1) {
      const filePath = original[i];
      accessMap[filePath] = await can(filePath);
    }
    if (this._draftClosed || !this._restoredDraft) return;
    const missingImages = draftLib.classifyMedia(images, (filePath) => accessMap[filePath] === true).missing;
    const missingVideos = draftLib.classifyMedia(videos, (filePath) => accessMap[filePath] === true).missing;
    const missing = {};
    missingImages.concat(missingVideos).forEach((filePath) => { missing[filePath] = true; });
    const nextImages = (this.data.images || []).filter((filePath) => !missing[filePath]);
    const nextVideos = (this.data.videos || []).filter((filePath) => !missing[filePath]);
    this.setData({
      images: nextImages,
      videos: nextVideos,
      mediaReady: nextImages.length >= 2,
    });
    const prompt = draftLib.missingMediaPrompt(missingImages.length, missingVideos.length);
    if (prompt) wx.showModal({ title: "请重新选择", content: prompt, showCancel: false });
    this._saveDraftNow();
  },

  _showAddress(input) {
    if (!input) return;
    if (!this._regionData) {
      this._pendingAddress = input;
      return;
    }
    let next = input;
    if (input.rawAddress && !input.districtName) {
      const parsed = draftLib.parseStoredAddress(input.rawAddress, this._regionData, this.data.cityNames);
      if (parsed) {
        next = {
          cityName: parsed.cityName,
          districtName: parsed.districtName,
          streetName: parsed.streetName,
          detail: parsed.recognized ? parsed.detail : (parsed.detail || input.rawAddress),
        };
      }
    }
    this._applyParsedAddress(next);
  },

  _applyParsedAddress(parsed) {
    if (!parsed || !this._regionData) {
      if (parsed) this._pendingAddress = parsed;
      return;
    }
    const cityNames = this.data.cityNames || [];
    let cityIndex = parsed.cityName ? cityNames.indexOf(parsed.cityName) : -1;
    if (cityIndex < 0) cityIndex = this.data.cityIndex || 0;
    const cityName = cityNames[cityIndex] || parsed.cityName || "深圳市";
    const cityData = this._regionData[cityName] || {};
    const districts = Object.keys(cityData).sort((a, b) => String(a).localeCompare(b, "zh"));
    const ph = "请选择";
    const districtNames = districts.length ? [ph].concat(districts) : ["该城市无数据"];
    let districtIndex = parsed.districtName ? districtNames.indexOf(parsed.districtName) : 0;
    if (districtIndex < 0) districtIndex = 0;
    let streetNames = ["请先选区"];
    let streetIndex = 0;
    if (districtIndex > 0) {
      const raw = cityData[districtNames[districtIndex]];
      const streetArr = Array.isArray(raw) ? raw.slice() : Object.keys(raw || {});
      streetArr.sort((a, b) => String(a).localeCompare(b, "zh"));
      streetNames = streetArr.length ? [ph].concat(streetArr) : ["该区无街道数据"];
      streetIndex = parsed.streetName ? streetNames.indexOf(parsed.streetName) : 0;
      if (streetIndex < 0) streetIndex = 0;
    }
    this._addrGen = (this._addrGen || 0) + 1;
    const gen = this._addrGen;
    this.setData({
      addrReady: districts.length > 0,
      cityIndex: cityIndex,
      districtNames: districtNames,
      streetNames: streetNames,
      districtIndex: districtIndex,
      streetIndex: streetIndex,
      addressDetailInput: parsed.detail || "",
    }, () => {
      if (gen !== this._addrGen) return;
      this._syncFormAddress();
    });
  },

  _rememberSubmittedPersonal() {
    try {
      wx.setStorageSync(
        draftLib.LAST_PERSONAL_KEY,
        draftLib.buildLastPersonal(this._collectDraftInput(), Date.now())
      );
    } catch (e) {}
  },

  _wipeLocalDraft(extraPaths) {
    let raw = null;
    try { raw = wx.getStorageSync(draftLib.DRAFT_KEY); } catch (e) { raw = null; }
    const paths = []
      .concat((raw && raw.images) || [])
      .concat((raw && raw.videos) || [])
      .concat(this.data.images || [])
      .concat(this.data.videos || [])
      .concat(extraPaths || []);
    try { wx.removeStorageSync(draftLib.DRAFT_KEY); } catch (e) {}
    this._removeStoredFiles(paths);
  },

  onClearDraft() {
    wx.showModal({
      title: "清空并重新填写",
      content: "这台手机上未提交的申请内容会被删掉，包括已选的照片和视频。",
      confirmText: "清空",
      cancelText: "取消",
      success: (res) => {
        if (!res.confirm) return;
        this._wipeDraftAndRefill();
      },
    });
  },

  _wipeDraftAndRefill() {
    if (this._draftTimer) {
      clearTimeout(this._draftTimer);
      this._draftTimer = null;
    }
    this._wipeLocalDraft();
    this._healthNoteDraft = undefined;
    this._userEdited = false;
    this._allowDraftSave = false;
    this._restoredDraft = false;
    this._draftClosed = false;
    this._pendingAddress = null;
    this._prefillGen = (this._prefillGen || 0) + 1;
    const form = draftLib.blankForm();
    this.setData({
      form: form,
      error: "",
      result: null,
      submitting: false,
      healthNoteSeed: "",
      healthNoteSeedKey: (this.data.healthNoteSeedKey || 0) + 1,
      formEpoch: (this.data.formEpoch || 0) + 1,
      mediaReady: false,
      idConsent: false,
      images: [],
      videos: [],
      isProxy: false,
      proxyName: "",
      proxyPhone: "",
      proxyRelationIndex: 0,
      proxyConsent: false,
      checks: { ear: true, fraud: true },
      storeIndex: 0,
      planIndex: 0,
      genderIndex: 0,
      sutureIndex: 0,
      addressDetailInput: "",
      cityIndex: 0,
      districtIndex: 0,
      streetIndex: 0,
    }, () => {
      if (this._regionData) {
        this._loadDistrictsForCity((this.data.cityNames || [])[0] || "深圳市");
      }
      this._prefillPersonal();
    });
  },

  onSutureChange(e) {
    const idx = Number(e.detail.value || 0);
    const row = this.data.sutureOptions[idx] || this.data.sutureOptions[0];
    this.setData({
      sutureIndex: idx,
      "form.suture_preference": row.value
    });
    this._scheduleDraftSave();
  },

  _syncNotifyState() {
    // 首页和申请页共用同一绑定状态；已绑定时不再重复要求授权。
    try {
      const saved = String(wx.getStorageSync("WECHAT_OPENID") || "");
      this.setData({ openid: saved, notifyReady: !!saved });
    } catch (e) {
      this.setData({ notifyReady: !!this.data.openid });
    }
  },

  _loadShenzhenRegions() {
    const app = getApp();
    const finish = () => this._initShenzhenAddressPickers();
    if (app.globalData.shenzhenRegions && typeof app.globalData.shenzhenRegions === "object" && Object.keys(app.globalData.shenzhenRegions).length) {
      finish();
      return;
    }
    if (shenzhenRegionsLocal && typeof shenzhenRegionsLocal === "object" && Object.keys(shenzhenRegionsLocal).length) {
      app.globalData.shenzhenRegions = shenzhenRegionsLocal;
      finish();
      return;
    }
    wx.request({
      url: app.globalData.apiBase + "/api/regions/shenzhen",
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300 && res.data && typeof res.data === "object") {
          app.globalData.shenzhenRegions = res.data;
          finish();
        } else {
          wx.showToast({ title: "地区数据加载失败", icon: "none" });
          const ph = "请选择";
          this.setData({
            districtNames: ["请检查网络与 apiBase 后重进"],
            streetNames: [ph]
          });
        }
      },
      fail: () => {
        wx.showToast({ title: "地区数据加载失败", icon: "none" });
      }
    });
  },

  _initShenzhenAddressPickers() {
    const allRegions = getApp().globalData.shenzhenRegions;
    const ph = "请选择";
    if (!allRegions || !Object.keys(allRegions).length) return;
    // 新格式：{ "深圳市": { district: [streets] }, ... }
    // 兼容旧格式（直接是 { district: [streets] }，没有城市层）
    const isMultiCity = Object.values(allRegions)[0] && typeof Object.values(allRegions)[0] === "object" && !Array.isArray(Object.values(allRegions)[0]);
    this._regionData = isMultiCity ? allRegions : { "深圳市": allRegions };
    if (this._pendingAddress) {
      const pending = this._pendingAddress;
      this._pendingAddress = null;
      this._showAddress(pending);
      return;
    }
    this._loadDistrictsForCity(this.data.cityNames[this.data.cityIndex]);
  },

  _loadDistrictsForCity(cityName) {
    const ph = "请选择";
    const cityData = (this._regionData || {})[cityName] || {};
    const districts = Object.keys(cityData).sort((a, b) => String(a).localeCompare(b, "zh"));
    this._addrGen = (this._addrGen || 0) + 1;
    const gen = this._addrGen;
    this.setData(
      {
        addrReady: districts.length > 0,
        districtNames: districts.length ? [ph, ...districts] : ["该城市无数据"],
        streetNames: ["请先选区"],
        districtIndex: 0,
        streetIndex: 0,
        addressDetailInput: ""
      },
      () => {
        if (gen !== this._addrGen) return;
        this._syncFormAddress();
      }
    );
  },

  onAddrCityPick(e) {
    if (!this.data.addrReady) return;
    const idx = Number(e.detail.value || 0);
    const cityName = this.data.cityNames[idx] || "深圳市";
    this.setData({ cityIndex: idx });
    this._loadDistrictsForCity(cityName);
    this._scheduleDraftSave();
  },

  _syncFormAddress() {
    const ph = "请选择";
    const FIX_P = "广东省";
    const { cityNames, cityIndex, districtNames, streetNames, districtIndex, streetIndex, addressDetailInput } = this.data;
    const city = cityNames[cityIndex] || "深圳市";
    const d = districtNames[districtIndex];
    const s = streetNames[streetIndex];
    const detail = String(addressDetailInput || "").trim();
    let prefix = "";
    if (d && d !== ph && s && s !== ph) prefix = FIX_P + city + d + s;
    else if (d && d !== ph) prefix = FIX_P + city + d;
    if (!prefix && !detail) {
      if (this.data.form.address !== "") this.setData({ "form.address": "" });
      return;
    }
    const full = detail ? (prefix ? prefix + " " + detail : detail) : prefix;
    if (this.data.form.address !== full) this.setData({ "form.address": full });
  },

  onAddrDistrictPick(e) {
    if (!this.data.addrReady) return;
    const idx = Number(e.detail.value || 0);
    const ph = "请选择";
    const cityName = this.data.cityNames[this.data.cityIndex] || "深圳市";
    const cityData = (this._regionData || {})[cityName] || {};
    const dist = this.data.districtNames[idx];
    let streetNames = ["请先选区"];
    if (idx > 0 && dist && dist !== ph && cityData[dist]) {
      // 新结构：cityData[dist] 是 object {街道: [社区]}；旧结构：是 array
      const raw = cityData[dist];
      const streetArr = Array.isArray(raw) ? raw : Object.keys(raw);
      const sorted = [...streetArr].sort((a, b) => String(a).localeCompare(b, "zh"));
      streetNames = sorted.length ? [ph, ...sorted] : ["该区无街道数据"];
    }
    this.setData({ districtIndex: idx, streetNames, streetIndex: 0 }, () => this._syncFormAddress());
    this._scheduleDraftSave();
  },

  onAddrStreetPick(e) {
    if (!this.data.addrReady) return;
    const idx = Number(e.detail.value || 0);
    this.setData({ streetIndex: idx }, () => this._syncFormAddress());
    this._scheduleDraftSave();
  },

  onAddressDetailInput(e) {
    const v = e.detail.value || "";
    this.setData({ addressDetailInput: v.length > 240 ? v.slice(0, 240) : v }, () => this._syncFormAddress());
    this._scheduleDraftSave();
  },

  onAgeEstimateChange(e) {
    this.setData({ "form.age_estimate": e.detail.value });
    this._scheduleDraftSave();
  },

  _reverseGeocode(lat, lng) {
    const app = getApp();
    return new Promise((resolve) => {
      wx.request({
        url: app.globalData.apiBase + "/api/geocode/regeo",
        method: "GET",
        data: { lat: String(lat), lng: String(lng) },
        success: (res) => resolve((res && res.data) || {}),
        fail: () => resolve({})
      });
    });
  },

  _autoGetLocationOnce() {
    if (this._didAutoLoc) return;
    this._didAutoLoc = true;
    runWithPrivacyGuard(
      "定位功能",
      () =>
        new Promise((resolve, reject) => {
          wx.getLocation({
            type: "wgs84",
            success: (res) => {
              const lat = String(res.latitude);
              const lng = String(res.longitude);
              this.setData({
                "form.location_lat": String(res.latitude),
                "form.location_lng": String(res.longitude),
                "form.location_address": "正在解析地址…"
              });
              this._reverseGeocode(lat, lng).then((j) => {
                if (j && j.ok && j.address) {
                  this.setData({ "form.location_address": j.address });
                } else if (j && String(j.amap_infocode) === "10009") {
                  this.setData({ "form.location_address": "地址解析失败：高德Key类型不匹配（需Web服务Key）" });
                } else {
                  this.setData({ "form.location_address": "已获取定位（未解析出地址）" });
                }
                resolve(res);
              });
            },
            fail: reject
          });
        }),
      { silent: true }
    ).catch(() => {
      // 静默失败：用户仍可手动点"获取定位"
    });
  },

  _withTimeout(promise, ms, label) {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error(label + " 超时")), ms);
      Promise.resolve(promise)
        .then((v) => {
          clearTimeout(t);
          resolve(v);
        })
        .catch((e) => {
          clearTimeout(t);
          reject(e);
        });
    });
  },

  onInput(e) {
    const k = e.currentTarget.dataset.k;
    const v = e.detail.value;
    const patch = { [`form.${k}`]: v };
    if (k === "id_number" && !String(v || "").trim()) {
      patch.idConsent = false;
    }
    this.setData(patch);
    this._scheduleDraftSave();
  },

  onIdConsent(e) {
    const vals = e.detail.value || [];
    this.setData({ idConsent: vals.includes("id") });
    this._scheduleDraftSave();
  },

  onGenderChange(e) {
    const idx = Number(e.detail.value || 0);
    this.setData({
      genderIndex: idx,
      "form.cat_gender": this.data.genderOptions[idx].value
    });
    this._scheduleDraftSave();
  },

  onToggleProxy() {
    this.setData({ isProxy: !this.data.isProxy, proxyName: "", proxyPhone: "", proxyRelationIndex: 0, proxyConsent: false });
    this._scheduleDraftSave();
  },

  onProxyName(e) { this.setData({ proxyName: e.detail.value }); this._scheduleDraftSave(); },
  onProxyPhone(e) { this.setData({ proxyPhone: e.detail.value.replace(/\D/g, "").slice(0, 11) }); this._scheduleDraftSave(); },
  onProxyRelation(e) { this.setData({ proxyRelationIndex: Number(e.detail.value) }); this._scheduleDraftSave(); },
  onProxyConsent(e) { this.setData({ proxyConsent: (e.detail.value || []).includes("consent") }); this._scheduleDraftSave(); },

  onChecks(e) {
    const vals = e.detail.value || [];
    this.setData({
      checks: {
        ear: vals.includes("ear"),
        fraud: vals.includes("fraud")
      }
    });
    this._scheduleDraftSave();
  },

  async onEnableNotify() {
    // 立刻给用户反馈，避免"点了没反应"
    this.setData({ notifyStatusText: "正在开启通知提醒…" });
    wx.showToast({ title: "处理中…", icon: "loading", duration: 1200 });
    wx.showLoading({ title: "加载中…" });
    const app = getApp();
    // 微信每次最多订阅 3 个模板，此按钮只订阅 TNR 申请状态相关的 3 个：
    //   审核通过(s1)、审核不通过(s4)、待人工审核(s5)
    // 预约/手术完成通知(s2/s3)在预约提交时另行订阅
    const tmplIds = [];
    const _getCached = (key) => { try { return wx.getStorageSync(key) || ""; } catch(e) { return ""; } };
    let s1 = _getCached("WECHAT_TMPL_APPLICATION_RESULT");
    let s4 = _getCached("WECHAT_TMPL_REJECTION");
    let s5 = _getCached("WECHAT_TMPL_PENDING_MANUAL");
    // 每次都从后端拉取最新模板列表；API 值优先，API 为空则保留 Storage 缓存
    try {
      const cfg = await this._withTimeout(
        new Promise((resolve, reject) => {
          wx.request({
            url: app.globalData.apiBase + "/api/wechat/config",
            method: "GET",
            success: (res) => {
              if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data || {});
              else reject({ statusCode: res.statusCode, data: res.data });
            },
            fail: reject
          });
        }),
        6000,
        "获取模板配置"
      );
      if (cfg.wechat_tmpl_application_result) { s1 = cfg.wechat_tmpl_application_result; wx.setStorageSync("WECHAT_TMPL_APPLICATION_RESULT", s1); }
      if (cfg.wechat_tmpl_rejection)          { s4 = cfg.wechat_tmpl_rejection;          wx.setStorageSync("WECHAT_TMPL_REJECTION", s4); }
      if (cfg.wechat_tmpl_pending_manual)     { s5 = cfg.wechat_tmpl_pending_manual;     wx.setStorageSync("WECHAT_TMPL_PENDING_MANUAL", s5); }
      // 同时缓存预约/手术模板ID，供预约页订阅时使用（不在此处请求授权）
      if (cfg.wechat_tmpl_surgery_done)     wx.setStorageSync("WECHAT_TMPL_SURGERY_DONE", cfg.wechat_tmpl_surgery_done);
      if (cfg.wechat_tmpl_appointment)      wx.setStorageSync("WECHAT_TMPL_APPOINTMENT", cfg.wechat_tmpl_appointment);
      if (cfg.wechat_tmpl_surgery_reminder) wx.setStorageSync("WECHAT_TMPL_SURGERY_REMINDER", cfg.wechat_tmpl_surgery_reminder);
    } catch (e) { /* 网络失败则继续用 Storage 缓存 */ }
    if (s1) tmplIds.push(s1);
    if (s4) tmplIds.push(s4);
    if (s5) tmplIds.push(s5);
    if (!tmplIds.length) {
      wx.showModal({
        title: "缺少模板ID",
        content:
          "手机预览/真机不会读取电脑端 Storage。\n\n我已尝试从后端拉取模板配置，但没有成功。\n\n当前 apiBase：\n" +
          app.globalData.apiBase +
          "\n\n错误信息：\n" +
          (fetchErr || "（无）") +
          "\n\n请确认：\n1) 电脑后端用【一键启动_手机联调.bat】启动（监听 0.0.0.0）\n2) 手机与电脑同一 Wi-Fi，且 apiBase 为电脑局域网 IP\n3) Windows 防火墙允许 python.exe\n4) 开发者工具已勾选【不校验合法域名、web-view、TLS版本】\n",
        showCancel: false
      });
      wx.hideLoading();
      return;
    }

    try {
      // 订阅弹窗：某些环境可能不返回回调导致卡住，增加超时保护
      console.log("[notify] subscribing tmplIds:", JSON.stringify(tmplIds));
      const subRes = await this._withTimeout(wx.requestSubscribeMessage({ tmplIds }), 12000, "订阅授权");
      const accepted = subscribe.acceptedAny(subRes);
      console.log("[notify] subscribe result", JSON.stringify(subscribe.summarize(subRes)));
      this.setData({
        notifyStatusText: accepted
          ? "已允许通知，审核结果会推送到这个微信。"
          : "您没有点允许。仍可提交申请，但收不到审核通知。"
      });

      // 登录换 openid
      const loginRes = await this._withTimeout(wx.login(), 8000, "微信登录");
      const code = loginRes.code;
      const data = await this._withTimeout(postJson("/api/wechat/login", { code }), 8000, "换取openid");
      const openid = data.openid || "";
      this.setData({
        openid,
        notifyReady: !!openid,
        notifyPrompt: !accepted,
        notifyStatusText: accepted
          ? "账号已绑定。手机号将用于医院联系，订阅消息会推送到本微信。"
          : "账号已绑定，可以提交申请。通知未允许，审核结果需要自己打开小程序查看。"
      });
      try {
        wx.setStorageSync("WECHAT_OPENID", openid);
      } catch (e2) {}
      const setting = await subscribe.readSetting();
      await subscribe.report(openid, "apply", subRes, setting.mainSwitch);
    } catch (e) {
      const msg =
        (e && (e.errMsg || e.message)) ||
        (typeof e === "string" ? e : "") ||
        JSON.stringify(e);
      console.log("[notify] error:", msg, "tmplIds:", JSON.stringify(tmplIds));
      this.setData({
        notifyStatusText: "订阅/登录失败：" + msg
      });
      const isWxApiErr = msg.includes("requestSubscribeMessage:fail");
      wx.showModal({
        title: "开启失败",
        content: isWxApiErr
          ? "微信通知订阅失败：\n" + msg + "\n\n已尝试订阅的模板：\n" + tmplIds.join("\n") + "\n\n请联系管理员核查模板配置。"
          : "错误信息：\n" + msg + "\n\n当前 apiBase：\n" + app.globalData.apiBase + "\n\n建议：\n- 确保手机与电脑同一 Wi-Fi\n- 后端用\"一键启动_手机联调.bat\"启动\n- 开发者工具勾选\"不校验合法域名、web-view、TLS版本\"\n- 重新预览扫码获取最新包\n",
        showCancel: false
      });
    } finally {
      wx.hideLoading();
    }
  },

  async onPickImages() {
    const remain = draftLib.PHOTO_LIMIT - (this.data.images || []).length;
    if (remain <= 0) {
      wx.showToast({ title: "最多 6 张照片", icon: "none" });
      return;
    }
    this._draftClosed = false;
    this._allowDraftSave = true;
    this._saveDraftNow();
    try {
      const res = await runWithPrivacyGuard("选择照片", () =>
        new Promise((resolve, reject) => {
          wx.chooseMedia({
            count: remain,
            mediaType: ["image"],
            sourceType: ["album", "camera"],
            success: resolve,
            fail: reject
          });
        })
      );
      const files = ((res && res.tempFiles) || []).map((f) => f.tempFilePath).filter(Boolean).slice(0, remain);
      const compress = (src) => new Promise((ok) => {
        wx.compressImage({
          src,
          quality: 60,
          success: (r) => ok((r && r.tempFilePath) || src),
          fail: () => ok(src),
        });
      });
      const compressed = await Promise.all(files.map(compress));
      const persisted = [];
      for (let i = 0; i < compressed.length; i += 1) {
        persisted.push(await this._persistMediaFile(compressed[i]));
      }
      const merged = draftLib.appendLimited(this.data.images, persisted, draftLib.PHOTO_LIMIT);
      this._allowDraftSave = true;
      this.setData({ images: merged.items, mediaReady: merged.items.length >= 2 });
      this._saveDraftNow();
      if (merged.overflow) wx.showToast({ title: "最多 6 张照片", icon: "none" });
    } catch (e) {
      const msg = (e && e.errMsg) || "";
      if (msg.includes("cancel")) return;
      wx.showModal({
        title: "选择照片失败",
        content: msg || "请检查隐私授权、相册权限或网络配置。",
        showCancel: false
      });
    }
  },

  async onPickVideo() {
    const remain = draftLib.VIDEO_LIMIT - (this.data.videos || []).length;
    if (remain <= 0) {
      wx.showToast({ title: "最多 2 个视频", icon: "none" });
      return;
    }
    this._draftClosed = false;
    this._allowDraftSave = true;
    this._saveDraftNow();
    try {
      const res = await runWithPrivacyGuard("选择视频", () =>
        new Promise((resolve, reject) => {
          wx.chooseMedia({
            count: remain,
            mediaType: ["video"],
            sourceType: ["album", "camera"],
            success: resolve,
            fail: reject
          });
        })
      );
      const files = ((res && res.tempFiles) || []).map((f) => f.tempFilePath).filter(Boolean).slice(0, remain);
      const persisted = [];
      for (let i = 0; i < files.length; i += 1) {
        persisted.push(await this._persistMediaFile(files[i]));
      }
      const merged = draftLib.appendLimited(this.data.videos, persisted, draftLib.VIDEO_LIMIT);
      this._allowDraftSave = true;
      this.setData({ videos: merged.items, mediaReady: (this.data.images || []).length >= 2 });
      this._saveDraftNow();
      if (merged.overflow) wx.showToast({ title: "最多 2 个视频", icon: "none" });
    } catch (e) {
      const msg = (e && e.errMsg) || "";
      if (msg.includes("cancel")) return;
      wx.showModal({
        title: "选择视频失败",
        content: msg || "请检查隐私授权、相册权限或网络配置。",
        showCancel: false
      });
    }
  },

  onStoreChange(e) {
    const idx = Number(e.detail.value || 0);
    this.setData({
      storeIndex: idx,
      "form.clinic_store": this.data.storeOptions[idx].value
    });
    this._scheduleDraftSave();
  },

  onPlanChange(e) {
    const idx = Number(e.detail.value || 0);
    this.setData({
      planIndex: idx,
      "form.post_surgery_plan": this.data.planOptions[idx].value
    });
    this._scheduleDraftSave();
  },

  async onGetLocation() {
    try {
      await runWithPrivacyGuard("定位功能", () =>
        new Promise((resolve, reject) => {
          wx.getLocation({
            type: "wgs84",
            success: (res) => {
              const lat = String(res.latitude);
              const lng = String(res.longitude);
              this.setData({
                "form.location_lat": lat,
                "form.location_lng": lng,
                "form.location_address": "正在解析地址…"
              });
              this._reverseGeocode(lat, lng).then((j) => {
                if (j && j.ok && j.address) {
                  this.setData({ "form.location_address": j.address });
                } else if (j && String(j.amap_infocode) === "10009") {
                  this.setData({ "form.location_address": "地址解析失败：高德Key类型不匹配（需Web服务Key）" });
                } else {
                  this.setData({ "form.location_address": "已获取定位（未解析出地址）" });
                }
                this._scheduleDraftSave();
                resolve(res);
              });
            },
            fail: reject
          });
        })
      );
    } catch (e) {
      const msg = (e && e.errMsg) || "请检查定位权限";
      if (String(msg).includes("auth deny") || String(msg).includes("authorize")) return;
      wx.showModal({
        title: "定位失败",
        content: msg,
        showCancel: false
      });
    }
  },

  async onSubmit() {
    this._draftClosed = false;
    this._allowDraftSave = true;
    this.setData({ error: "", result: null });
    this._syncFormAddress();
    const hnDraft = typeof this._healthNoteDraft === "string" ? this._healthNoteDraft : "";
    const form = hnDraft ? { ...this.data.form, health_note: hnDraft } : this.data.form;
    const { checks, images, videos, openid, idConsent } = this.data;
    if (!checks.ear || !checks.fraud) {
      this.setData({ error: "请勾选同意剪耳标记与承诺非家养猫冒充。" });
      return;
    }
    const ph = "请选择";
    const { districtNames, streetNames, districtIndex, streetIndex, addressDetailInput } = this.data;
    const ad = districtNames[districtIndex];
    const as = streetNames[streetIndex];
    const adetail = String(addressDetailInput || "").trim();
    if (!ad || ad === ph || !as || as === ph || !adetail) {
      this.setData({ error: "请选择区、街道，并填写详细地址。" });
      return;
    }
    if (String(form.address || "").length > 500) {
      this.setData({ error: "地址总长度不能超过 500 字。" });
      return;
    }
    if (!form.applicant_name || !form.phone || !form.address) {
      this.setData({ error: "请填写姓名、手机号与完整地址。" });
      return;
    }
    if (!/^1\d{10}$/.test(String(form.phone || "").trim())) {
      this.setData({ error: "请填写 11 位中国大陆手机号。" });
      return;
    }
    if (!form.clinic_store || form.clinic_store === "") {
      this.setData({ error: "请选择预约门店。" });
      return;
    }
    if (!String(form.post_surgery_plan || "").trim()) {
      this.setData({ error: "请选择术后打算。" });
      return;
    }
    const idn = String(form.id_number || "").trim().toUpperCase();
    if (!idn) {
      this.setData({ error: "请填写身份证号。" });
      return;
    }
    if (idn.length === 18) {
      if (!/^\d{17}[\dX]$/.test(idn)) {
        this.setData({ error: "请填写 18 位身份证号（末位可为 X）。" });
        return;
      }
    } else if (idn.length === 15) {
      if (!/^\d{15}$/.test(idn)) {
        this.setData({ error: "请填写 15 位身份证号。" });
        return;
      }
    } else {
      this.setData({ error: "请填写 15 或 18 位身份证号。" });
      return;
    }
    if (!idConsent) {
      this.setData({ error: "请勾选身份证号知情同意。" });
      return;
    }
    if (!String(form.cat_nickname || "").trim()) {
      this.setData({ error: "请填写流浪猫名字（无名字可按特征命名）。" });
      return;
    }
    const { form: f2 } = this.data;
    if (!String(f2.age_estimate || "").trim()) {
      this.setData({ error: "请选择出生年月。" });
      return;
    }
    if (!String(f2.health_note || "").trim()) {
      this.setData({ error: "请填写流浪状况说明。" });
      return;
    }
    if (images.length < 2) {
      this.setData({ error: "请至少上传 2 张清晰照片；视频只能作为补充，不能替代照片。" });
      return;
    }
    // 代预约校验
    const { isProxy, proxyName, proxyPhone, proxyRelationOptions, proxyRelationIndex, proxyConsent } = this.data;
    if (isProxy) {
      if (!proxyName.trim()) { this.setData({ error: "代预约：请填写实际申请人姓名。" }); return; }
      if (!/^1\d{10}$/.test(proxyPhone.trim())) { this.setData({ error: "代预约：请填写实际申请人 11 位手机号。" }); return; }
      if (proxyRelationIndex === 0) { this.setData({ error: "代预约：请选择与实际申请人的关系。" }); return; }
      if (!proxyConsent) { this.setData({ error: "代预约：请勾选实际申请人已授权确认。" }); return; }
    }
    if (!openid) {
      this.setData({ error: "请先点击「开启通知提醒」绑定账号后再提交（用于订单归属与推送通知）。" });
      return;
    }
    // 缓存手机号供预约页 TNR 配额检查使用
    try { wx.setStorageSync("USER_PHONE", String(form.phone || "").trim()); } catch(e2) {}
    // 必须在本次点击里同步拉起授权，不能先 await 网络请求。
    this._askReviewSubscribeThenSubmit();
  },

  _askReviewSubscribeThenSubmit() {
    this.setData({ submitting: true });
    const ids = subscribe.cachedIds("review");
    const openid = this.data.openid || "";
    const submit = () => this._submitNow();
    if (!ids.length) {
      this.setData({ notifyPrompt: true });
      submit();
      return;
    }
    wx.requestSubscribeMessage({
      tmplIds: ids,
      success: (res) => {
        subscribe.report(openid, "apply_submit", res);
        if (subscribe.acceptedAny(res) || this._notifyPersuaded) {
          this.setData({ notifyPrompt: !subscribe.acceptedAny(res) });
          submit();
          return;
        }
        this._notifyPersuaded = true;
        this.setData({ notifyPrompt: true });
        wx.showModal({
          title: "收不到审核通知",
          content: "您没有允许通知，将收不到审核结果。可以再允许一次；不授权也能继续提交申请。",
          confirmText: "再允许一次",
          cancelText: "仍然提交",
          success: (modal) => {
            if (!modal.confirm) {
              submit();
              return;
            }
            wx.requestSubscribeMessage({
              tmplIds: ids,
              complete: (again) => {
                subscribe.report(openid, "apply_submit_retry", again || {});
                this.setData({ notifyPrompt: !subscribe.acceptedAny(again) });
                submit();
              }
            });
          },
          fail: () => submit()
        });
      },
      fail: () => {
        this.setData({ notifyPrompt: true, notifyOpenSettings: true });
        wx.showModal({
          title: "请打开订阅消息",
          content: "没有弹出授权。若您关闭了订阅消息或选择了不再询问，请到设置里打开。不授权也能继续提交申请。",
          confirmText: "去设置",
          cancelText: "仍然提交",
          success: (modal) => {
            if (modal.confirm) subscribe.openSettings();
            submit();
          },
          fail: () => submit()
        });
      }
    });
  },

  async _refreshNotifyPrompt() {
    const openid = this.data.openid || "";
    if (!openid) return;
    const [status, setting] = await Promise.all([
      subscribe.fetchStatus(openid, "apply"),
      subscribe.readSetting()
    ]);
    const ids = (status && status.suggested_template_ids) || subscribe.cachedIds("review");
    const openSettings = subscribe.shouldOpenSettings(setting, ids) || !!(status && status.open_settings);
    this.setData({
      notifyPrompt: !!(status && status.needs_prompt),
      notifyOpenSettings: openSettings
    });
  },

  onReauthNotify() {
    if (this.data.notifyOpenSettings) {
      subscribe.openSettings();
      return;
    }
    const ids = subscribe.cachedIds("review");
    const openid = this.data.openid || "";
    if (!ids.length) {
      wx.showToast({ title: "通知模板未就绪", icon: "none" });
      return;
    }
    wx.requestSubscribeMessage({
      tmplIds: ids,
      success: (res) => {
        subscribe.report(openid, "apply_reauth", res);
        const ok = subscribe.acceptedAny(res);
        this.setData({ notifyPrompt: !ok });
        if (ok) wx.showToast({ title: "通知已开启", icon: "success" });
        else {
          wx.showModal({
            title: "还没有允许通知",
            content: subscribe.PROMPT + "。若弹窗没出现，请到设置里打开订阅消息。",
            confirmText: "去设置",
            cancelText: "知道了",
            success: (modal) => { if (modal.confirm) subscribe.openSettings(); }
          });
        }
      },
      fail: () => {
        this.setData({ notifyOpenSettings: true });
        subscribe.openSettings();
      }
    });
  },

  async _submitNow() {
    this._syncFormAddress();
    this.setData({ submitting: true });
    const hnDraft = typeof this._healthNoteDraft === "string" ? this._healthNoteDraft : "";
    const form = hnDraft ? { ...this.data.form, health_note: hnDraft } : this.data.form;
    const { checks, images, videos, openid, isProxy, proxyName, proxyPhone, proxyRelationOptions, proxyRelationIndex } = this.data;
    const app = getApp();

    const requestForm = (url, data) =>
      new Promise((resolve, reject) => {
        wx.request({
          url,
          method: "POST",
          header: { "content-type": "application/x-www-form-urlencoded" },
          data,
          success: (res) => {
            if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data || {});
            else reject(res);
          },
          fail: reject
        });
      });

    // 单次 uploadFile
    const uploadOnce = (appId, kind, filePath) =>
      new Promise((resolve, reject) => {
        wx.uploadFile({
          url: app.globalData.apiBase + `/api/apply/${encodeURIComponent(appId)}/upload-media`,
          filePath,
          name: "file",
          formData: { kind },
          success: (res) => {
            if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data);
            else reject(res);
          },
          fail: reject
        });
      });

    // 带重试的 uploadFile：网络层错误（unreachable / timeout / aborted）自动重试 3 次
    const uploadOne = async (appId, kind, filePath) => {
      const maxAttempts = 3;
      let lastErr = null;
      for (let i = 1; i <= maxAttempts; i++) {
        try {
          return await uploadOnce(appId, kind, filePath);
        } catch (e) {
          lastErr = e;
          const msg = (e && e.errMsg) || "";
          // 只有网络层错误才重试；4xx/5xx 不重试（业务错误）
          const isNetErr =
            msg.includes("ADDRESS_UNREACHABLE") ||
            msg.includes("CONNECTION_RESET") ||
            msg.includes("NETWORK_CHANGED") ||
            msg.includes("CONNECTION_ABORTED") ||
            msg.includes("CONNECTION_REFUSED") ||
            msg.includes("CONNECTION_FAILED") ||
            msg.includes("TIMED_OUT") ||
            msg.includes("timeout") ||
            msg.includes("interrupted");
          if (!isNetErr || i === maxAttempts) throw e;
          // 1s, 2s 退避后重试
          await new Promise((r) => setTimeout(r, i * 1000));
        }
      }
      throw lastErr;
    };

    const postEmpty = (url) =>
      new Promise((resolve, reject) => {
        wx.request({
          url,
          method: "POST",
          success: (res) => {
            if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data || {});
            else reject(res);
          },
          fail: reject
        });
      });

    // ── 提交前先校验所有本地临时文件还在 ──
    // 微信 tempFilePath 有生命周期，用户选完图后停留太久 / 切后台返回时
    // 临时文件可能已被清理，此时 wx.uploadFile 会报 "file doesn't exist"。
    try {
      const fsm = wx.getFileSystemManager();
      const checkOne = (p) => new Promise((ok) => {
        fsm.access({ path: p, success: () => ok(true), fail: () => ok(false) });
      });
      const validImages = [];
      const validVideos = [];
      for (const p of images)        if (await checkOne(p)) validImages.push(p);
      for (const p of (videos || [])) if (await checkOne(p)) validVideos.push(p);
      const lostImg = images.length - validImages.length;
      const lostVid = (videos || []).length - validVideos.length;
      if (lostImg || lostVid) {
        const parts = [];
        if (lostImg) parts.push(`${lostImg} 张照片`);
        if (lostVid) parts.push(`${lostVid} 个视频`);
        // 把仍有效的文件回填到 state，让用户接着补选
        this.setData({
          images: validImages,
          videos: validVideos,
          mediaReady: validImages.length >= 2,
          submitting: false,
          error: `${parts.join("、")}因停留过久已失效，请重新选择后再次提交。`,
        });
        this._saveDraftNow();
        return;
      }
    } catch (e) {
      // 校验本身出错不阻断提交，让原流程兜底
      console.warn("[media validate]", e);
    }

    try {
      const idNorm = String(form.id_number || "").trim().toUpperCase();
      const created = await this._withTimeout(
        requestForm(app.globalData.apiBase + "/api/apply/create", {
          ...form,
          id_number: idNorm,
          wechat_openid: openid,
          agree_ear_tip: checks.ear ? "true" : "false",
          agree_no_pet_fraud: checks.fraud ? "true" : "false",
          is_proxy: isProxy ? "true" : "false",
          proxy_name: isProxy ? proxyName.trim() : "",
          proxy_phone: isProxy ? proxyPhone.trim() : "",
          proxy_relation: isProxy ? (proxyRelationOptions[proxyRelationIndex] || "") : ""
        }),
        12000,
        "创建申请"
      );
      const appId = created.id;

      for (const p of images) {
        await this._withTimeout(uploadOne(appId, "image", p), 30000, "上传照片");
      }
      for (const p of videos || []) {
        await this._withTimeout(uploadOne(appId, "video", p), 60000, "上传视频");
      }

      const j = await this._withTimeout(
        postEmpty(app.globalData.apiBase + `/api/apply/${encodeURIComponent(appId)}/finalize`),
        60000,
        "提交审核"
      );

      this._rememberSubmittedPersonal();
      this.setData({ result: j, submitting: false, images: [], videos: [], mediaReady: false });
      this._draftClosed = true;
      this._allowDraftSave = false;
      if (this._draftTimer) {
        clearTimeout(this._draftTimer);
        this._draftTimer = null;
      }
      this._wipeLocalDraft(images.concat(videos || []));
      wx.setStorageSync("LAST_APP_ID", j.id);
      try {
        const prev = wx.getStorageSync("MY_APPS") || [];
        const arr = Array.isArray(prev) ? prev.slice(0) : [];
        const sid = String(j.id);
        const next = [sid, ...arr.filter((x) => String(x) !== sid)].slice(0, 20);
        wx.setStorageSync("MY_APPS", next);
      } catch (e) {}
      // 手术完成/预约提醒不在这里补弹：上传照片之后已经离开点击手势，微信不会再出授权窗。
      // 那 3 个名额留给预约页和进度页。
    } catch (e) {
      let msg = "提交失败";
      if (e && e.data) {
        const d = e.data;
        msg = (typeof d === "string" ? d : (d.detail || d.message || JSON.stringify(d))) || msg;
      } else if (e && e.errMsg) {
        msg = e.errMsg;
      } else if (e && e.message) {
        msg = e.message;
      }
      // 微信端常见英文错误中文化
      const lower = String(msg).toLowerCase();
      if (lower.includes("file doesn't exist") || lower.includes("file not exist")) {
        msg = "照片/视频已失效，请重新选择后再次提交。";
      } else if (lower.includes("address_unreachable") || lower.includes("connection_refused") || lower.includes("connection_failed")) {
        msg = "网络无法连接到服务器，请切换 4G/Wi-Fi 后重试。";
      } else if (lower.includes("connection_reset") || lower.includes("connection_aborted") || lower.includes("network_changed") || lower.includes("interrupted")) {
        msg = "上传中断，请检查网络后重试。";
      } else if (lower.includes("timeout") || lower.includes("timed_out")) {
        msg = "网络超时，请检查网络后重试。";
      } else if (lower.includes("fail") && lower.includes("uploadfile")) {
        msg = "上传失败，请检查网络后重试。";
      }
      // 附加状态码，方便排查
      if (e && e.statusCode && e.statusCode !== 200) {
        msg = `[${e.statusCode}] ${msg}`;
      }
      this.setData({ error: msg, submitting: false });
      // 控制台打印完整错误，开发者工具可见
      console.error("[TNR submit error]", JSON.stringify(e));
    }
  },

  goStatus() {
    const id = this.data.result?.id || wx.getStorageSync("LAST_APP_ID");
    if (!id) return;
    wx.navigateTo({ url: `/pages/status/status?id=${id}` });
  },

  goStatusPage() {
    wx.navigateTo({ url: "/pages/status/status" });
  },

  goTnrGuide() {
    wx.navigateTo({ url: "/pages/tnr-guide/index" });
  },

  goAppointmentListPage() {
    wx.navigateTo({ url: "/pages/appointment/list" });
  },

  goShowcase() {
    wx.navigateTo({ url: "/pages/showcase/showcase" });
  },

  goFeedbackPage() {
    wx.navigateTo({ url: "/pages/feedback/index" });
  },

  goRabiesPage() {
    wx.navigateTo({ url: "/pages/rabies/index" });
  },

  goAdoptionPage() {
    wx.navigateTo({ url: "/pages/adoption/list" });
  },

  goBindPage() {
    wx.navigateTo({ url: "/pages/bind/bind" });
  },

  goImmunizationPage() {
    wx.navigateTo({ url: "/pages/immunization/index" });
  }
});

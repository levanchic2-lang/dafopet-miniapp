const { postJson } = require("../../utils/api");
const { runWithPrivacyGuard } = require("../../utils/privacy");
const shenzhenRegionsLocal = (() => {
  try {
    return require("../../utils/shenzhen_regions.json");
  } catch (e) {
    return null;
  }
})();

function maskPhone(s) {
  if (!s) return "";
  const t = String(s);
  if (t.length < 7) return t;
  return t.slice(0, 3) + "****" + t.slice(-4);
}

Page({
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
      age_estimate: "",
      health_note: ""
    },
    storeOptions: [
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
    ageNumInput: "",
    ageUnitOptions: [
      { value: "", label: "单位" },
      { value: "day", label: "天" },
      { value: "month", label: "月" },
      { value: "year", label: "年" }
    ],
    ageUnitIndex: 0,
    checks: { ear: true, fraud: true },
    images: [],
    videos: [],
    submitting: false,
    result: null,
    error: "",
    notifyStatusText: "",
    openid: "",
    idConsent: false,
    districtNames: ["加载中…"],
    streetNames: ["请选择"],
    districtIndex: 0,
    streetIndex: 0,
    addressDetailInput: ""
  },

  onHealthNoteInput(e) {
    // 避免 textarea 每次输入都 setData 触发页面重排导致滚动跳到顶部
    this._healthNoteDraft = e && e.detail ? e.detail.value : "";
  },

  onHealthNoteBlur(e) {
    const v = (e && e.detail ? e.detail.value : "") || "";
    this._healthNoteDraft = "";
    this.setData({ "form.health_note": v });
  },

  onLoad() {
    this._syncAgeEstimate();
    this._loadShenzhenRegions();
    // 若之前已绑定 openid，直接复用，确保每单都能出现在"我的订单"
    try {
      const saved = wx.getStorageSync("WECHAT_OPENID") || "";
      if (saved && !this.data.openid) this.setData({ openid: String(saved) });
    } catch (e) {}
    // 默认自动尝试获取定位（不强制；失败不阻断）
    const { form } = this.data;
    if (form.location_lat && form.location_lng) return;
    this._autoGetLocationOnce();
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
    const sz = getApp().globalData.shenzhenRegions;
    const ph = "请选择";
    if (!sz || !Object.keys(sz).length) return;
    const districts = Object.keys(sz).sort();
    this.setData(
      {
        districtNames: [ph, ...districts],
        streetNames: [ph],
        districtIndex: 0,
        streetIndex: 0,
        addressDetailInput: ""
      },
      () => this._syncFormAddress()
    );
  },

  _syncFormAddress() {
    const ph = "请选择";
    const FIX_P = "广东省";
    const FIX_C = "深圳市";
    const { districtNames, streetNames, districtIndex, streetIndex, addressDetailInput } = this.data;
    const d = districtNames[districtIndex];
    const s = streetNames[streetIndex];
    const detail = String(addressDetailInput || "").trim();
    let prefix = "";
    if (d && d !== ph && s && s !== ph) prefix = FIX_P + FIX_C + d + s;
    else if (d && d !== ph) prefix = FIX_P + FIX_C + d;
    if (!prefix && !detail) {
      if (this.data.form.address !== "") this.setData({ "form.address": "" });
      return;
    }
    const full = detail ? (prefix ? prefix + " " + detail : detail) : prefix;
    if (this.data.form.address !== full) this.setData({ "form.address": full });
  },

  onAddrDistrictPick(e) {
    const idx = Number(e.detail.value || 0);
    const ph = "请选择";
    const sz = getApp().globalData.shenzhenRegions;
    const dist = this.data.districtNames[idx];
    let streetNames = [ph];
    if (idx > 0 && dist && dist !== ph && sz && sz[dist]) {
      const arr = [...sz[dist]].sort((a, b) => String(a).localeCompare(b, "zh"));
      streetNames = [ph, ...arr];
    }
    this.setData({ districtIndex: idx, streetNames, streetIndex: 0 }, () => this._syncFormAddress());
  },

  onAddrStreetPick(e) {
    const idx = Number(e.detail.value || 0);
    this.setData({ streetIndex: idx }, () => this._syncFormAddress());
  },

  onAddressDetailInput(e) {
    const v = e.detail.value || "";
    this.setData({ addressDetailInput: v.length > 240 ? v.slice(0, 240) : v }, () => this._syncFormAddress());
  },

  _syncAgeEstimate() {
    const units = this.data.ageUnitOptions || [];
    const uidx = this.data.ageUnitIndex;
    const u = units[uidx] ? units[uidx].value : "";
    const raw = String(this.data.ageNumInput || "").trim();
    const n = parseInt(raw, 10);
    let s = "";
    if (u && raw !== "" && Number.isFinite(n) && n >= 1) {
      if (u === "day") s = n + "天";
      else if (u === "month") s = n + "个月";
      else if (u === "year") s = n + "岁";
    }
    if (this.data.form.age_estimate !== s) {
      this.setData({ "form.age_estimate": s });
    }
  },

  onAgeNumInput(e) {
    const v = (e.detail.value || "").replace(/\D/g, "").slice(0, 3);
    this.setData({ ageNumInput: v }, () => this._syncAgeEstimate());
  },

  onAgeUnitPick(e) {
    const idx = Number(e.detail.value || 0);
    this.setData({ ageUnitIndex: idx }, () => this._syncAgeEstimate());
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
  },

  onIdConsent(e) {
    const vals = e.detail.value || [];
    this.setData({ idConsent: vals.includes("id") });
  },

  onGenderChange(e) {
    const idx = Number(e.detail.value || 0);
    this.setData({
      genderIndex: idx,
      "form.cat_gender": this.data.genderOptions[idx].value
    });
  },

  onChecks(e) {
    const vals = e.detail.value || [];
    this.setData({
      checks: {
        ear: vals.includes("ear"),
        fraud: vals.includes("fraud")
      }
    });
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
      if (cfg.wechat_tmpl_surgery_done)   wx.setStorageSync("WECHAT_TMPL_SURGERY_DONE", cfg.wechat_tmpl_surgery_done);
      if (cfg.wechat_tmpl_appointment)    wx.setStorageSync("WECHAT_TMPL_APPOINTMENT", cfg.wechat_tmpl_appointment);
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
      await this._withTimeout(wx.requestSubscribeMessage({ tmplIds }), 12000, "订阅授权");
      console.log("[notify] subscribe success");
      this.setData({ notifyStatusText: "已弹出授权（请在弹窗里点允许）。" });

      // 登录换 openid
      const loginRes = await this._withTimeout(wx.login(), 8000, "微信登录");
      const code = loginRes.code;
      const data = await this._withTimeout(postJson("/api/wechat/login", { code }), 8000, "换取openid");
      const openid = data.openid || "";
      this.setData({
        openid,
        notifyStatusText:
          "openid已获取，可提交申请。手机号将用于医院联系，订阅消息将推送到本微信。"
      });
      try {
        wx.setStorageSync("WECHAT_OPENID", openid);
      } catch (e2) {}
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
    try {
      await runWithPrivacyGuard("选择照片", () =>
        new Promise((resolve, reject) => {
          wx.chooseMedia({
            count: 6,
            mediaType: ["image"],
            sourceType: ["album", "camera"],
            success: (res) => {
              const files = (res.tempFiles || []).map((f) => f.tempFilePath);
              this.setData({ images: files.slice(0, 6) });
              resolve(res);
            },
            fail: reject
          });
        })
      );
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
    try {
      await runWithPrivacyGuard("选择视频", () =>
        new Promise((resolve, reject) => {
          wx.chooseMedia({
            count: 2,
            mediaType: ["video"],
            sourceType: ["album", "camera"],
            success: (res) => {
              const files = (res.tempFiles || []).map((f) => f.tempFilePath);
              this.setData({ videos: files.slice(0, 2) });
              resolve(res);
            },
            fail: reject
          });
        })
      );
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
  },

  onPlanChange(e) {
    const idx = Number(e.detail.value || 0);
    this.setData({
      planIndex: idx,
      "form.post_surgery_plan": this.data.planOptions[idx].value
    });
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
    this.setData({ error: "", result: null });
    this._syncFormAddress();
    const hnDraft = typeof this._healthNoteDraft === "string" ? this._healthNoteDraft : "";
    const form = hnDraft ? { ...this.data.form, health_note: hnDraft } : this.data.form;
    const { checks, images, openid, idConsent } = this.data;
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
    if (!form.clinic_store) {
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
    this._syncAgeEstimate();
    const { form: f2 } = this.data;
    if (!String(f2.age_estimate || "").trim()) {
      this.setData({ error: "请填写年龄估计（数字并选择单位）。" });
      return;
    }
    if (!String(f2.health_note || "").trim()) {
      this.setData({ error: "请填写流浪状况说明。" });
      return;
    }
    if (!images.length) {
      this.setData({ error: "请至少上传 1 张申请照片。" });
      return;
    }
    if (!openid) {
      this.setData({ error: "请先点击「开启通知提醒」绑定账号后再提交（用于订单归属与推送通知）。" });
      return;
    }
    this._submitNow();
  },

  async _submitNow() {
    this._syncAgeEstimate();
    this._syncFormAddress();
    this.setData({ submitting: true });
    const hnDraft = typeof this._healthNoteDraft === "string" ? this._healthNoteDraft : "";
    const form = hnDraft ? { ...this.data.form, health_note: hnDraft } : this.data.form;
    const { checks, images, videos, openid } = this.data;
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

    const uploadOne = (appId, kind, filePath) =>
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

    try {
      const idNorm = String(form.id_number || "").trim().toUpperCase();
      const created = await this._withTimeout(
        requestForm(app.globalData.apiBase + "/api/apply/create", {
          ...form,
          id_number: idNorm,
          wechat_openid: openid,
          agree_ear_tip: checks.ear ? "true" : "false",
          agree_no_pet_fraud: checks.fraud ? "true" : "false"
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

      this.setData({ result: j, submitting: false });
      wx.setStorageSync("LAST_APP_ID", j.id);
      try {
        const prev = wx.getStorageSync("MY_APPS") || [];
        const arr = Array.isArray(prev) ? prev.slice(0) : [];
        const sid = String(j.id);
        const next = [sid, ...arr.filter((x) => String(x) !== sid)].slice(0, 20);
        wx.setStorageSync("MY_APPS", next);
      } catch (e) {}
    } catch (e) {
      let msg = "提交失败";
      if (e && e.data) {
        msg = (e.data && (e.data.detail || e.data.message)) || msg;
      } else if (e && e.errMsg) {
        msg = e.errMsg;
      } else if (e && e.message) {
        msg = e.message;
      }
      this.setData({ error: msg, submitting: false });
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

  goAppointmentPage() {
    wx.navigateTo({ url: "/pages/appointment/index" });
  },

  goAppointmentListPage() {
    wx.navigateTo({ url: "/pages/appointment/list" });
  },

  goShowcase() {
    wx.navigateTo({ url: "/pages/showcase/showcase" });
  }
});

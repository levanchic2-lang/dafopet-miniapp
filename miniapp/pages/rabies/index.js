const { getJson, postJson } = require("../../utils/api");
const app = getApp();
const shenzhenRegionsLocal = (() => {
  try { return require("../../utils/shenzhen_regions.json"); } catch (e) { return null; }
})();

const GENDER_OPTIONS = [
  { label: "不详", value: "unknown" },
  { label: "公（雄）", value: "male" },
  { label: "母（雌）", value: "female" },
];

const INVALID_NAMES = new Set(["先生", "女士", "mr", "mrs", "ms", "主人", "不详"]);

function isInvalidName(name) {
  return INVALID_NAMES.has((name || "").trim().toLowerCase());
}

Page({
  data: {
    form: {
      owner_name: "",
      owner_phone: "",
      owner_address: "",
      animal_name: "",
      animal_breed: "",
      animal_dob: "",
      animal_gender: "unknown",
      animal_color: "",
      clinic_store: "横岗店",
    },
    genderOptions: GENDER_OPTIONS,
    genderIndex: 0,
    pets: [],
    selectedPetId: null,
    customerId: null,
    lookupDone: false,
    customerFound: false,
    districtNames: ["请选择"],
    streetNames: ["请选择"],
    districtIndex: 0,
    streetIndex: 0,
    addressDetailInput: "",
    hasSig: false,
    submitting: false,
    error: "",
  },

  onLoad() {
    this._loadShenzhenRegions();
  },

  onReady() {
    this._setupCanvas();
  },

  _loadShenzhenRegions() {
    const finish = () => this._initAddrPickers();
    if (app.globalData.shenzhenRegions && Object.keys(app.globalData.shenzhenRegions).length) {
      finish(); return;
    }
    if (shenzhenRegionsLocal && Object.keys(shenzhenRegionsLocal).length) {
      app.globalData.shenzhenRegions = shenzhenRegionsLocal;
      finish(); return;
    }
    wx.request({
      url: app.globalData.apiBase + "/api/regions/shenzhen",
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300 && res.data) {
          app.globalData.shenzhenRegions = res.data;
          finish();
        }
      }
    });
  },

  _initAddrPickers() {
    const sz = app.globalData.shenzhenRegions;
    const ph = "请选择";
    if (!sz || !Object.keys(sz).length) return;
    const districts = Object.keys(sz).sort();
    this.setData({ districtNames: [ph, ...districts], streetNames: [ph], districtIndex: 0, streetIndex: 0 });
  },

  _syncAddr() {
    const ph = "请选择";
    const { districtNames, streetNames, districtIndex, streetIndex, addressDetailInput } = this.data;
    const d = districtNames[districtIndex];
    const s = streetNames[streetIndex];
    const detail = (addressDetailInput || "").trim();
    let addr = "";
    if (d && d !== ph) addr = "广东省深圳市" + d;
    if (s && s !== ph) addr += s;
    if (detail) addr += detail;
    this.setData({ "form.owner_address": addr });
  },

  onAddrDistrictPick(e) {
    const idx = Number(e.detail.value || 0);
    const ph = "请选择";
    const sz = app.globalData.shenzhenRegions;
    const dist = this.data.districtNames[idx];
    let streetNames = [ph];
    if (idx > 0 && dist && sz && sz[dist]) {
      const arr = [...sz[dist]].sort((a, b) => String(a).localeCompare(b, "zh"));
      streetNames = [ph, ...arr];
    }
    this.setData({ districtIndex: idx, streetNames, streetIndex: 0 }, () => this._syncAddr());
  },

  onAddrStreetPick(e) {
    this.setData({ streetIndex: Number(e.detail.value || 0) }, () => this._syncAddr());
  },

  onAddressDetailInput(e) {
    this.setData({ addressDetailInput: e.detail.value || "" }, () => this._syncAddr());
  },

  _setupCanvas() {
    const query = wx.createSelectorQuery().in(this);
    query.select("#sig-canvas").fields({ node: true, size: true }).exec(res => {
      if (!res || !res[0] || !res[0].node) return;
      const canvas = res[0].node;
      const dpr = wx.getSystemInfoSync().pixelRatio;
      canvas.width = res[0].width * dpr;
      canvas.height = res[0].height * dpr;
      const ctx = canvas.getContext("2d");
      ctx.scale(dpr, dpr);
      ctx.strokeStyle = "#1a1a1a";
      ctx.lineWidth = 2;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      this._canvas = canvas;
      this._ctx = ctx;
      this._dpr = dpr;
    });
  },

  onPhoneInput(e) {
    this.setData({ "form.owner_phone": e.detail.value, lookupDone: false });
  },

  onPhoneLookup() {
    const phone = (this.data.form.owner_phone || "").trim();
    if (phone.length < 11) return;
    getJson("/api/customer/lookup", { phone }).then(res => {
      this.setData({
        lookupDone: true,
        customerFound: res.found,
        customerId: res.customer_id || null,
        pets: res.pets || [],
        selectedPetId: null,
      });
      if (res.found) {
        const updates = {};
        if (res.name && !isInvalidName(res.name)) {
          updates["form.owner_name"] = res.name;
        }
        if (res.address) {
          updates["addressDetailInput"] = res.address;
        }
        if (Object.keys(updates).length) this.setData(updates);
      }
    }).catch(() => {
      this.setData({ lookupDone: true, customerFound: false, pets: [] });
    });
  },

  onSelectPet(e) {
    const pet = e.currentTarget.dataset.pet;
    const genderMap = { male: 1, female: 2, unknown: 0 };
    this.setData({
      selectedPetId: pet.id,
      "form.animal_name": pet.name || "",
      "form.animal_breed": pet.breed || "",
      "form.animal_dob": pet.birthday_estimate || "",
      "form.animal_gender": pet.gender || "unknown",
      "form.animal_color": pet.color_pattern || "",
      genderIndex: genderMap[pet.gender] !== undefined ? genderMap[pet.gender] : 0,
    });
  },

  onSelectNewPet() {
    this.setData({
      selectedPetId: 0,
      "form.animal_name": "",
      "form.animal_breed": "",
      "form.animal_dob": "",
      "form.animal_gender": "unknown",
      "form.animal_color": "",
      genderIndex: 0,
    });
  },

  onNameInput(e) { this.setData({ "form.owner_name": e.detail.value }); },
  onAnimalNameInput(e) { this.setData({ "form.animal_name": e.detail.value }); },
  onAnimalBreedInput(e) { this.setData({ "form.animal_breed": e.detail.value }); },
  onAnimalDobChange(e) { this.setData({ "form.animal_dob": e.detail.value }); },
  onAnimalColorInput(e) { this.setData({ "form.animal_color": e.detail.value }); },

  onGenderChange(e) {
    const idx = parseInt(e.detail.value);
    this.setData({ genderIndex: idx, "form.animal_gender": GENDER_OPTIONS[idx].value });
  },

  // ── 签名 ──
  onSigStart(e) {
    if (!this._ctx) return;
    const t = e.touches[0];
    this._ctx.beginPath();
    this._ctx.moveTo(t.x, t.y);
    this._drawing = true;
  },
  onSigMove(e) {
    if (!this._drawing || !this._ctx) return;
    const t = e.touches[0];
    this._ctx.lineTo(t.x, t.y);
    this._ctx.stroke();
    if (!this.data.hasSig) this.setData({ hasSig: true });
  },
  onSigEnd() { this._drawing = false; },

  onClearSig() {
    if (!this._ctx || !this._canvas) return;
    this._ctx.clearRect(0, 0, this._canvas.width / this._dpr, this._canvas.height / this._dpr);
    this.setData({ hasSig: false });
  },

  // ── 提交 ──
  onSubmit() {
    const { form, hasSig, customerId, selectedPetId, districtNames, districtIndex } = this.data;
    if (!form.owner_phone || form.owner_phone.length < 11) {
      return this.setData({ error: "请填写11位手机号" });
    }
    if (!form.owner_name || isInvalidName(form.owner_name)) {
      return this.setData({ error: "请填写真实姓名（不可填写先生/女士）" });
    }
    if (!form.owner_address || districtIndex === 0) {
      return this.setData({ error: "请选择联系地址（至少选择区/县）" });
    }
    if (!form.animal_name || !form.animal_name.trim()) {
      return this.setData({ error: "请填写动物名称" });
    }
    if (!form.animal_breed || !form.animal_breed.trim()) {
      return this.setData({ error: "请填写动物品种" });
    }
    if (!form.animal_dob) {
      return this.setData({ error: "请选择动物出生年月" });
    }
    if (!form.animal_color || !form.animal_color.trim()) {
      return this.setData({ error: "请填写动物毛色" });
    }
    if (!hasSig) {
      return this.setData({ error: "请完成手写签名" });
    }

    if (!this._canvas) {
      return this.setData({ error: "签名画板未就绪，请稍候重试" });
    }

    const sigDataURL = this._canvas.toDataURL("image/png");
    this.setData({ submitting: true, error: "" });

    postJson("/api/rabies/submit", {
      ...form,
      owner_signature: sigDataURL,
      customer_id: customerId,
      pet_id: selectedPetId,
    }).then(res => {
      this.setData({ submitting: false });
      const name = encodeURIComponent(form.owner_name);
      const phone = encodeURIComponent(form.owner_phone);
      const animal = encodeURIComponent(form.animal_name);
      wx.redirectTo({ url: `/pages/rabies/done?id=${res.id}&name=${name}&phone=${phone}&animal=${animal}` });
    }).catch(err => {
      this.setData({
        submitting: false,
        error: (err && err.detail) || "提交失败，请重试",
      });
    });
  },
});

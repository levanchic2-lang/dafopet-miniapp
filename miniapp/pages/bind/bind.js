const { postJson } = require("../../utils/api");

Page({
  onShareAppMessage() {
    return { title: "大风动物医院 · 流浪猫 TNR 申请", path: "/pages/index/index" };
  },
  onShareTimeline() {
    return { title: "大风动物医院 · 流浪猫 TNR 申请" };
  },
  data: {
    phone: "",
    code: "",
    cooldown: 0,
    sending: false,
    submitting: false,
    error: "",
    custPreview: null,
    devCode: "",
    bound: false,
    boundName: "",
  },

  _timer: null,

  onPhoneInput(e) { this.setData({ phone: (e.detail.value || "").trim(), error: "" }); },
  onCodeInput(e)  { this.setData({ code:  (e.detail.value || "").trim(), error: "" }); },

  startCooldown() {
    this.setData({ cooldown: 60 });
    this._timer = setInterval(() => {
      const next = this.data.cooldown - 1;
      if (next <= 0) {
        clearInterval(this._timer); this._timer = null;
        this.setData({ cooldown: 0 });
      } else {
        this.setData({ cooldown: next });
      }
    }, 1000);
  },
  onUnload() { if (this._timer) clearInterval(this._timer); },

  async onSendCode() {
    const phone = this.data.phone;
    if (!/^\d{11}$/.test(phone)) {
      this.setData({ error: "请输入 11 位手机号" });
      return;
    }
    this.setData({ sending: true, error: "" });
    try {
      const r = await postJson("/api/customer-binding/send-code", { phone });
      if (!r.ok) {
        this.setData({ error: r.error || "发送失败", custPreview: null, devCode: "" });
        return;
      }
      this.setData({
        custPreview: r.customer || null,
        devCode: r.dev_code || "",
      });
      wx.showToast({ title: r.sms_sent ? "已发送" : "验证码已生成", icon: "success" });
      this.startCooldown();
    } catch (e) {
      this.setData({ error: (e && e.detail) || "网络错误" });
    } finally {
      this.setData({ sending: false });
    }
  },

  async onSubmit() {
    if (this.data.submitting) return;
    if (!/^\d{11}$/.test(this.data.phone)) {
      this.setData({ error: "请输入 11 位手机号" }); return;
    }
    if (!/^\d{6}$/.test(this.data.code)) {
      this.setData({ error: "请输入 6 位验证码" }); return;
    }
    this.setData({ submitting: true, error: "" });
    try {
      // 取 openid（如果已存储就直接用，否则 wx.login 换）
      const cachedOpenid = wx.getStorageSync("WECHAT_OPENID") || "";
      let payload = { phone: this.data.phone, code: this.data.code };
      if (cachedOpenid) {
        payload.openid = cachedOpenid;
      } else {
        const loginRes = await new Promise((resolve, reject) => {
          wx.login({ success: resolve, fail: reject });
        });
        payload.js_code = loginRes.code;
      }
      const r = await postJson("/api/customer-binding/verify", payload);
      if (!r.ok) {
        this.setData({ error: r.error || "绑定失败" });
        return;
      }
      this.setData({
        bound: true,
        boundName: r.customer_name || "",
        error: "",
      });
      // 缓存 customer_id，方便其他页面读用
      try { wx.setStorageSync("CUSTOMER_ID", r.customer_id); } catch (e) {}
    } catch (e) {
      this.setData({ error: (e && e.detail) || "网络错误，请重试" });
    } finally {
      this.setData({ submitting: false });
    }
  },

  onDone() { wx.navigateBack({ delta: 1 }); },
});

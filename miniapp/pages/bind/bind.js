const { postJson, wxLoginCode } = require("../../utils/api");

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
    bound: false,
    boundName: "",
    boundCount: 0,
    boundMessage: "",
    fromCs: false,
    canGoBack: false,
  },

  _timer: null,

  onLoad(options) {
    const fromCs = !!(options && String(options.from || "") === "cs");
    const patch = { fromCs };
    // 上游页（如预约页）跳过来时可带 phone 预填。从客服卡片进来时这里就是入口页。
    if (options && options.phone) {
      try { patch.phone = decodeURIComponent(options.phone); } catch (e) {}
    }
    this.setData(patch);
    this._syncStack();
    // 入口页没有上一页的登录态。先唤起一次微信登录，提交时再取新的 code。
    wxLoginCode().catch(() => {});
  },

  onShow() { this._syncStack(); },

  _syncStack() {
    let depth = 1;
    try { depth = getCurrentPages().length; } catch (e) {}
    this.setData({ canGoBack: depth > 1 });
  },

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
        this.setData({ error: r.error || "发送失败" });
        return;
      }
      wx.showToast({ title: "请查收短信", icon: "success" });
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
      const jsCode = await wxLoginCode();
      const r = await postJson("/api/customer-binding/verify", {
        phone: this.data.phone,
        code: this.data.code,
        js_code: jsCode,
      });
      if (!r.ok) {
        this.setData({ error: r.error || "绑定失败" });
        return;
      }
      this.setData({
        bound: true,
        boundName: r.customer_name || "",
        boundCount: r.bound_count || 1,
        boundMessage: r.message || "",
        error: "",
      });
      // 缓存 customer_id，方便其他页面读用
      try { wx.setStorageSync("CUSTOMER_ID", r.customer_id); } catch (e) {}
      if (this.data.fromCs) {
        wx.showToast({
          title: "绑定成功，可以回到客服继续聊",
          icon: "none",
          duration: 2500,
        });
      }
    } catch (e) {
      this.setData({ error: (e && e.detail) || "网络错误，请重试" });
    } finally {
      this.setData({ submitting: false });
    }
  },

  onDone() {
    if (this.data.canGoBack) {
      wx.navigateBack({ delta: 1 });
      return;
    }
    wx.reLaunch({ url: "/pages/index/index" });
  },
});

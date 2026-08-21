const { postJson } = require("../../utils/api");

Page({
  data: {
    notifyLoading: false,
    notifyReady: false,
    notifyStatusText: ""
  },

  onLoad() {
    try {
      const openid = wx.getStorageSync("WECHAT_OPENID") || "";
      this.setData({ notifyReady: !!openid });
    } catch (e) {}
  },

  onShow() {
    try {
      const openid = wx.getStorageSync("WECHAT_OPENID") || "";
      if (!!openid !== this.data.notifyReady) this.setData({ notifyReady: !!openid });
    } catch (e) {}
  },

  onShareAppMessage() {
    return { title: "大风动物医院 · 线上服务", path: "/pages/index/index" };
  },

  onShareTimeline() {
    return { title: "大风动物医院 · 线上服务" };
  },

  _request(url, method = "GET") {
    return new Promise((resolve, reject) => {
      wx.request({
        url,
        method,
        success: (res) => {
          if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data || {});
          else reject({ statusCode: res.statusCode, data: res.data });
        },
        fail: reject
      });
    });
  },

  async onEnableNotify() {
    if (this.data.notifyLoading) return;
    this.setData({ notifyLoading: true, notifyStatusText: "正在开启通知提醒…" });
    const app = getApp();
    try {
      const fields = [
        ["wechat_tmpl_application_result", "WECHAT_TMPL_APPLICATION_RESULT"],
        ["wechat_tmpl_rejection", "WECHAT_TMPL_REJECTION"],
        ["wechat_tmpl_pending_manual", "WECHAT_TMPL_PENDING_MANUAL"]
      ];
      let cfg = {};
      try {
        cfg = await this._request(app.globalData.apiBase + "/api/wechat/config");
      } catch (e) {
        // The last successfully loaded template IDs remain usable during brief API outages.
      }
      const tmplIds = [];
      fields.forEach(([field, key]) => {
        let value = cfg[field] || "";
        if (!value) {
          try { value = wx.getStorageSync(key) || ""; } catch (e) {}
        }
        if (value) {
          tmplIds.push(value);
          try { wx.setStorageSync(key, value); } catch (e) {}
        }
      });
      if (cfg.wechat_tmpl_surgery_done) wx.setStorageSync("WECHAT_TMPL_SURGERY_DONE", cfg.wechat_tmpl_surgery_done);
      if (cfg.wechat_tmpl_appointment) wx.setStorageSync("WECHAT_TMPL_APPOINTMENT", cfg.wechat_tmpl_appointment);
      if (cfg.wechat_tmpl_surgery_reminder) wx.setStorageSync("WECHAT_TMPL_SURGERY_REMINDER", cfg.wechat_tmpl_surgery_reminder);

      if (!tmplIds.length) throw new Error("暂未取得通知模板，请稍后重试");
      await new Promise((resolve, reject) => {
        wx.requestSubscribeMessage({ tmplIds: tmplIds.slice(0, 3), success: resolve, fail: reject });
      });
      const loginRes = await new Promise((resolve, reject) => wx.login({ success: resolve, fail: reject }));
      const data = await postJson("/api/wechat/login", { code: loginRes.code });
      const openid = data.openid || "";
      if (!openid) throw new Error("未能完成微信账号绑定");
      wx.setStorageSync("WECHAT_OPENID", openid);
      this.setData({
        notifyReady: true,
        notifyStatusText: "通知提醒已开启，可正常接收业务进度通知。"
      });
      wx.showToast({ title: "通知已开启", icon: "success" });
    } catch (e) {
      const msg = (e && (e.errMsg || e.message)) || "开启失败，请稍后重试";
      this.setData({ notifyStatusText: msg });
      wx.showModal({ title: "通知提醒未开启", content: msg, showCancel: false });
    } finally {
      this.setData({ notifyLoading: false });
    }
  },

  goTnrGuidePage() { wx.navigateTo({ url: "/pages/tnr-guide/index" }); },
  goApplyPage() { wx.navigateTo({ url: "/pages/apply/index" }); },
  goStatusPage() { wx.navigateTo({ url: "/pages/status/status" }); },
  goBindPage() { wx.navigateTo({ url: "/pages/bind/bind" }); },
  goAppointmentPage() { wx.navigateTo({ url: "/pages/appointment/index" }); },
  goAppointmentListPage() { wx.navigateTo({ url: "/pages/appointment/list" }); },
  goImmunizationPage() { wx.navigateTo({ url: "/pages/immunization/index" }); },
  goAdoptionPage() { wx.navigateTo({ url: "/pages/adoption/list" }); },
  goShowcase() { wx.navigateTo({ url: "/pages/showcase/showcase" }); },
  goRabiesPage() { wx.navigateTo({ url: "/pages/rabies/index" }); },
  goFeedbackPage() { wx.navigateTo({ url: "/pages/feedback/index" }); }
});

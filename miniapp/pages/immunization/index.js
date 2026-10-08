const { getJson, postJson } = require("../../utils/api");

function detailMessage(error) {
  if (!error) return "加载失败，请稍后重试";
  if (typeof error === "string") return error;
  return error.detail || error.errMsg || "加载失败，请稍后重试";
}

Page({
  data: {
    loading: true,
    downloadingId: 0,
    bound: false,
    ownerName: "",
    pets: [],
    error: "",
    reminderBusy: false,
    reminderConfigured: false,
    reminderAuthorized: false,
    preventiveTemplateId: ""
  },

  onShow() {
    this.loadCertificates();
  },

  async loadCertificates() {
    this.setData({ loading: true, error: "" });
    try {
      const login = await new Promise((resolve, reject) => {
        wx.login({ success: resolve, fail: reject });
      });
      if (!login.code) throw new Error("微信身份校验失败");
      const data = await postJson("/api/wechat/immunization-certificates", {
        code: login.code
      });
      let templateId = "";
      try {
        const config = await getJson("/api/wechat/config");
        templateId = (config.wechat_tmpl_vaccine_reminder || "").trim();
        if (templateId) wx.setStorageSync("WECHAT_TMPL_VACCINE_REMINDER", templateId);
      } catch (configError) {
        templateId = wx.getStorageSync("WECHAT_TMPL_VACCINE_REMINDER") || "";
      }
      this.setData({
        loading: false,
        bound: !!data.bound,
        ownerName: data.owner_name || "",
        pets: data.pets || [],
        preventiveTemplateId: templateId,
        reminderConfigured: !!templateId,
        reminderAuthorized: !!wx.getStorageSync("PREVENTIVE_REMINDER_AUTHORIZED")
      });
    } catch (error) {
      this.setData({ loading: false, error: detailMessage(error) });
    }
  },

  goBind() {
    wx.navigateTo({ url: "/pages/bind/bind" });
  },

  async enablePreventiveReminder() {
    if (this.data.reminderBusy) return;
    if (!this.data.bound) {
      wx.showModal({ title: "请先绑定档案", content: "绑定后才能将到期提醒发送到您的微信。", showCancel: false });
      return;
    }
    const templateId = (this.data.preventiveTemplateId || "").trim();
    if (!templateId) {
      wx.showModal({ title: "暂未开放", content: "医院尚未完成到期提醒配置。", showCancel: false });
      return;
    }
    this.setData({ reminderBusy: true });
    try {
      const result = await new Promise((resolve, reject) => {
        wx.requestSubscribeMessage({ tmplIds: [templateId], success: resolve, fail: reject });
      });
      if (!result || result[templateId] !== "accept") {
        throw new Error("您没有同意本次提醒授权");
      }
      const login = await new Promise((resolve, reject) => wx.login({ success: resolve, fail: reject }));
      const session = await postJson("/api/wechat/login", { code: login.code });
      if (session.openid) wx.setStorageSync("WECHAT_OPENID", session.openid);
      wx.setStorageSync("PREVENTIVE_REMINDER_AUTHORIZED", true);
      this.setData({ reminderAuthorized: true });
      wx.showToast({ title: "本次授权成功", icon: "success" });
    } catch (error) {
      wx.showModal({
        title: "未完成授权",
        content: (error && (error.message || error.errMsg)) || "请稍后重试",
        showCancel: false
      });
    } finally {
      this.setData({ reminderBusy: false });
    }
  },

  downloadCertificate(e) {
    const petId = Number(e.currentTarget.dataset.petId || 0);
    const path = e.currentTarget.dataset.url || "";
    if (!petId || !path || this.data.downloadingId) return;
    this.setData({ downloadingId: petId });
    wx.showLoading({ title: "正在生成" });
    wx.downloadFile({
      url: getApp().globalData.apiBase + path,
      success: (result) => {
        if (result.statusCode !== 200) {
          wx.showToast({ title: "下载链接已失效，请重试", icon: "none" });
          this.loadCertificates();
          return;
        }
        wx.openDocument({
          filePath: result.tempFilePath,
          fileType: "pdf",
          showMenu: true,
          fail: () => wx.showToast({ title: "无法打开文件", icon: "none" })
        });
      },
      fail: () => wx.showToast({ title: "下载失败，请检查网络", icon: "none" }),
      complete: () => {
        wx.hideLoading();
        this.setData({ downloadingId: 0 });
      }
    });
  }
});

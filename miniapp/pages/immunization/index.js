const { postJson } = require("../../utils/api");

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
    error: ""
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
      this.setData({
        loading: false,
        bound: !!data.bound,
        ownerName: data.owner_name || "",
        pets: data.pets || []
      });
    } catch (error) {
      this.setData({ loading: false, error: detailMessage(error) });
    }
  },

  goBind() {
    wx.navigateTo({ url: "/pages/bind/bind" });
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

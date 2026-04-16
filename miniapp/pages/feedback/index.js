const { postJson } = require("../../utils/api");

Page({
  data: {
    content: "",
    images: [],
    submitting: false,
    success: false,
    error: ""
  },

  _contentDraft: "",

  onContentInput(e) { this._contentDraft = e.detail.value || ""; },
  onContentBlur(e) { this.setData({ content: e.detail.value || "" }); this._contentDraft = ""; },

  onPickImages() {
    wx.chooseMedia({
      count: 4 - this.data.images.length,
      mediaType: ["image"],
      sourceType: ["album", "camera"],
      success: (res) => {
        const files = (res.tempFiles || []).map(f => f.tempFilePath);
        this.setData({ images: [...this.data.images, ...files].slice(0, 4) });
      },
      fail() {}
    });
  },

  onDelImage(e) {
    const i = Number(e.currentTarget.dataset.i);
    const imgs = [...this.data.images];
    imgs.splice(i, 1);
    this.setData({ images: imgs });
  },

  async onSubmit() {
    const content = this._contentDraft || this.data.content;
    if (!content.trim()) {
      this.setData({ error: "请填写问题描述" });
      return;
    }
    this.setData({ submitting: true, error: "", success: false });
    try {
      const openid = wx.getStorageSync("WECHAT_OPENID") || "";
      const app = getApp();
      // 1. Create feedback record
      const res = await postJson("/api/wechat/feedback/create", { openid, content: content.trim() });
      const fbId = res.id;
      // 2. Upload images
      for (const imgPath of this.data.images) {
        await new Promise((resolve) => {
          wx.uploadFile({
            url: app.globalData.apiBase + `/api/wechat/feedback/${fbId}/upload`,
            filePath: imgPath,
            name: "file",
            success: resolve,
            fail: resolve  // don't block on image upload failure
          });
        });
      }
      this.setData({ submitting: false, success: true, content: "", images: [], error: "" });
      this._contentDraft = "";
    } catch(e) {
      const msg = (e && (e.detail || e.message || e.errMsg)) || "提交失败，请稍后重试";
      this.setData({ submitting: false, error: String(msg) });
    }
  }
});

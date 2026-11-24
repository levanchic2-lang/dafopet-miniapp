const { getJson, postJson, wxLoginCode } = require("../../utils/api");

Page({
  data: {
    loading: true,
    loadError: "",
    error: "",
    submitting: false,
    notice: "该申请已进入隔离区。更新近照并经医院审核通过前，不能预约手术。审核通过后即可预约。",
    rejectReason: "",
    catName: "",
    images: []
  },

  onLoad(query) {
    this.appId = String((query && query.id) || "").replace(/^#/, "");
    if (!this.appId) {
      this.setData({ loading: false, loadError: "缺少申请编号。" });
      return;
    }
    this.loadApplication();
  },

  async loadApplication() {
    this.setData({ loading: true, loadError: "" });
    try {
      const code = await wxLoginCode();
      const info = await getJson(`/api/app/${encodeURIComponent(this.appId)}/status`, { code });
      if (!info || !info.can_restart) {
        this.setData({
          loading: false,
          loadError: info && info.restart_pending
            ? "重新申请已提交，请等待医院审核。"
            : "这份申请现在不在隔离区，不能从这里重新申请。"
        });
        return;
      }
      this.setData({
        loading: false,
        notice: info.quarantine_notice || this.data.notice,
        rejectReason: info.restart_reject_reason || "",
        catName: info.cat_nickname || ""
      });
    } catch (err) {
      const detail = err && (err.detail || err.message);
      this.setData({ loading: false, loadError: detail || "读取申请失败，请稍后重试。" });
    }
  },

  choosePhotos() {
    wx.chooseMedia({
      count: 6,
      mediaType: ["image"],
      sourceType: ["album", "camera"],
      success: (res) => {
        const paths = (res.tempFiles || []).map((file) => file.tempFilePath).filter(Boolean);
        this.setData({ images: paths, error: "" });
      }
    });
  },

  uploadOne(code, filePath) {
    const app = getApp();
    return new Promise((resolve, reject) => {
      wx.uploadFile({
        url: `${app.globalData.apiBase}/api/app/${encodeURIComponent(this.appId)}/restart/media`,
        filePath,
        name: "file",
        formData: { code, kind: "image" },
        success: (res) => {
          if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data);
          else {
            let detail = "上传照片失败";
            try {
              const body = JSON.parse(res.data || "{}");
              detail = body.detail || detail;
            } catch (e) {}
            reject(new Error(detail));
          }
        },
        fail: reject
      });
    });
  },

  async submit() {
    if (this.data.submitting) return;
    if ((this.data.images || []).length < 2) {
      this.setData({ error: "请至少上传 2 张猫咪近照。" });
      return;
    }
    this.setData({ submitting: true, error: "" });
    try {
      const beginCode = await wxLoginCode();
      await postJson(`/api/app/${encodeURIComponent(this.appId)}/restart/begin`, { code: beginCode });
      for (const path of this.data.images) {
        const uploadCode = await wxLoginCode();
        await this.uploadOne(uploadCode, path);
      }
      const finalCode = await wxLoginCode();
      await postJson(`/api/app/${encodeURIComponent(this.appId)}/restart/finalize`, { code: finalCode });
      wx.showToast({ title: "已提交", icon: "success" });
      setTimeout(() => {
        wx.redirectTo({ url: `/pages/status/status?id=${this.appId}` });
      }, 600);
    } catch (err) {
      const detail = (err && (err.detail || err.message || err.errMsg)) || "提交失败，请稍后重试。";
      this.setData({ submitting: false, error: String(detail) });
    }
  }
});

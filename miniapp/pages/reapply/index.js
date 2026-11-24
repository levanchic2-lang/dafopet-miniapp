const { getJson, postJson, wxLoginCode } = require("../../utils/api");

const GENDERS = [
  { value: "male", label: "公猫" },
  { value: "female", label: "母猫" },
  { value: "unknown", label: "未知" }
];

function genderIndex(value) {
  const idx = GENDERS.findIndex((item) => item.value === value);
  return idx >= 0 ? idx : 2;
}

Page({
  data: {
    loading: true,
    loadError: "",
    error: "",
    submitting: false,
    notice: "该申请已进入隔离区。更新近照和资料并经医院审核通过前，不能预约手术。",
    rejectReason: "",
    genderOptions: GENDERS,
    genderIndex: 2,
    images: [],
    form: {
      cat_nickname: "",
      cat_gender: "unknown",
      cat_breed: "",
      cat_color: "",
      age_estimate: "",
      weight_estimate: "",
      health_note: ""
    }
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
      const gender = info.cat_gender || "unknown";
      this.setData({
        loading: false,
        notice: info.quarantine_notice || this.data.notice,
        rejectReason: info.restart_reject_reason || "",
        genderIndex: genderIndex(gender),
        form: {
          cat_nickname: info.cat_nickname || "",
          cat_gender: gender,
          cat_breed: info.cat_breed || "",
          cat_color: info.cat_color || "",
          age_estimate: info.age_estimate || "",
          weight_estimate: info.weight_estimate || "",
          health_note: info.health_note || ""
        }
      });
    } catch (err) {
      const detail = err && (err.detail || err.message);
      this.setData({ loading: false, loadError: detail || "读取申请失败，请稍后重试。" });
    }
  },

  onInput(e) {
    const key = e.currentTarget.dataset.k;
    this.setData({ [`form.${key}`]: e.detail.value });
  },

  onGender(e) {
    const idx = Number(e.detail.value || 0);
    const option = GENDERS[idx] || GENDERS[2];
    this.setData({ genderIndex: idx, "form.cat_gender": option.value });
  },

  onAge(e) {
    this.setData({ "form.age_estimate": e.detail.value || "" });
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
    const form = this.data.form;
    if (!String(form.cat_nickname || "").trim()) {
      this.setData({ error: "请填写猫咪名字。" });
      return;
    }
    if (!String(form.cat_color || "").trim()) {
      this.setData({ error: "请填写毛色。" });
      return;
    }
    if (!String(form.weight_estimate || "").trim()) {
      this.setData({ error: "请填写体型或体重。" });
      return;
    }
    if ((this.data.images || []).length < 2) {
      this.setData({ error: "请至少上传 2 张猫咪近照。" });
      return;
    }
    this.setData({ submitting: true, error: "" });
    try {
      const beginCode = await wxLoginCode();
      await postJson(`/api/app/${encodeURIComponent(this.appId)}/restart/begin`, {
        ...form,
        code: beginCode
      });
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

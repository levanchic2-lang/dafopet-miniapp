const { staffGet, staffPost, staffUpload } = require("../../../utils/api");
const app = getApp();

function token() { try { return wx.getStorageSync("STAFF_TOKEN") || ""; } catch (e) { return ""; } }

Page({
  data: { id: 0, loading: true, error: "", busy: false, playingVideoPath: "", visit: {}, pet: {}, customer: {}, stages: [], stage: "during", notes: "", media: [] },
  onLoad(options) { this.setData({ id: Number(options.id || 0) }); this.load(); },
  onPullDownRefresh() { this.load(); },
  async load() {
    if (!this.data.id) return;
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet(`/api/staff-miniapp/visits/${this.data.id}/materials`);
      this.setData({ visit: result.visit || {}, pet: result.pet || {}, customer: result.customer || {}, stages: result.stages || [], media: result.media || [] });
      wx.setNavigationBarTitle({ title: (result.pet && result.pet.name ? result.pet.name + " · 病例素材" : "病例素材") });
      this.loadImagePreviews(result.media || []);
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "病例素材加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  async loadImagePreviews(rows) {
    const auth = token();
    const hydrated = await Promise.all(rows.map((row) => new Promise((resolve) => {
      if (row.media_type !== "image") { resolve(Object.assign({}, row, { localPath: "" })); return; }
      wx.downloadFile({ url: `${app.globalData.apiBase}/api/staff-miniapp/visit-materials/${row.id}/file`, header: { Authorization: `Bearer ${auth}` }, success: res => resolve(Object.assign({}, row, { localPath: res.statusCode === 200 ? res.tempFilePath : "" })), fail: () => resolve(Object.assign({}, row, { localPath: "" })) });
    })));
    this.setData({ media: hydrated });
  },
  setStage(e) { this.setData({ stage: e.currentTarget.dataset.stage || "during" }); },
  onNotes(e) { this.setData({ notes: e.detail.value || "" }); },
  chooseImage() { this.choose("image"); },
  chooseVideo() { this.choose("video"); },
  choose(mediaType) {
    if (this.data.busy) return;
    wx.chooseMedia({ count: mediaType === "image" ? 9 : 1, mediaType: [mediaType], sourceType: ["camera", "album"], maxDuration: 90, camera: "back", success: res => this.uploadFiles(res.tempFiles || [], mediaType) });
  },
  async uploadFiles(files, mediaType) {
    if (!files.length) return;
    this.setData({ busy: true });
    try {
      for (let i = 0; i < files.length; i += 1) {
        wx.showLoading({ title: `上传 ${i + 1}/${files.length}`, mask: true });
        await staffUpload(`/api/staff-miniapp/visits/${this.data.id}/materials/upload`, files[i].tempFilePath, { stage: this.data.stage, media_type: mediaType, notes: this.data.notes || "" });
      }
      wx.hideLoading(); this.setData({ notes: "" }); wx.showToast({ title: "素材已保存", icon: "success" }); this.load();
    } catch (e) {
      wx.hideLoading(); wx.showModal({ title: "上传失败", content: (e && (e.detail || e.errMsg)) || "请检查网络后重试", showCancel: false });
    } finally { this.setData({ busy: false }); }
  },
  previewImage(e) {
    const current = e.currentTarget.dataset.src;
    const urls = this.data.media.filter(x => x.media_type === "image" && x.localPath).map(x => x.localPath);
    if (current && urls.length) wx.previewImage({ current, urls });
  },
  playVideo(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    if (!id || this.data.busy) return;
    this.setData({ busy: true }); wx.showLoading({ title: "载入视频", mask: true });
    wx.downloadFile({ url: `${app.globalData.apiBase}/api/staff-miniapp/visit-materials/${id}/file`, header: { Authorization: `Bearer ${token()}` }, success: res => { if (res.statusCode === 200) this.setData({ playingVideoPath: res.tempFilePath }); else wx.showToast({ title: "视频下载失败", icon: "none" }); }, fail: () => wx.showToast({ title: "视频下载失败", icon: "none" }), complete: () => { wx.hideLoading(); this.setData({ busy: false }); } });
  },
  closeVideo() { this.setData({ playingVideoPath: "" }); },
  noop() {},
  deleteMedia(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    if (!id || this.data.busy) return;
    wx.showModal({ title: "删除素材", content: "确认删除这份病例素材？删除后无法恢复。", confirmColor: "#7c302c", success: async res => { if (!res.confirm) return; this.setData({ busy: true }); try { await staffPost(`/api/staff-miniapp/visits/${this.data.id}/materials/${id}/delete`, {}); wx.showToast({ title: "已删除" }); this.load(); } catch (err) { wx.showToast({ title: (err && err.detail) || "删除失败", icon: "none" }); } finally { this.setData({ busy: false }); } } });
  }
});

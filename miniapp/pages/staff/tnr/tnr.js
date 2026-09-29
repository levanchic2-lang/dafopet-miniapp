const { staffGet, staffPost, staffUpload } = require("../../../utils/api");

const apiBase = () => getApp().globalData.apiBase;
const token = () => {
  try { return wx.getStorageSync("STAFF_TOKEN") || ""; } catch (e) { return ""; }
};

Page({
  data: { loading: true, error: "", profile: {}, date: "", items: [], busyId: 0 },
  onShow() { this.loadData(); },
  onPullDownRefresh() { this.loadData(); },
  async loadData() {
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/tnr/today");
      const items = (result.items || []).map((item) => ({ ...item, previewPaths: [] }));
      this.setData({ profile: result.profile || {}, date: result.date || "", items });
      this.loadPreviews(items);
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "今日TNR加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  async loadPreviews(items) {
    const auth = token();
    const download = (id) => new Promise((resolve) => wx.downloadFile({
      url: `${apiBase()}/api/staff-miniapp/tnr/media/${id}`,
      header: { Authorization: `Bearer ${auth}` },
      success: (res) => resolve(res.statusCode === 200 ? res.tempFilePath : ""),
      fail: () => resolve("")
    }));
    const hydrated = await Promise.all(items.map(async (item) => ({
      ...item,
      previewPaths: (await Promise.all((item.application_media_ids || []).slice(0, 4).map(download))).filter(Boolean)
    })));
    this.setData({ items: hydrated });
  },
  previewImage(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const current = e.currentTarget.dataset.src;
    const item = this.data.items.find((row) => row.id === id);
    if (item && item.previewPaths.length) wx.previewImage({ current, urls: item.previewPaths });
  },
  verifyCat(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const item = this.data.items.find((row) => row.id === id);
    if (!item || this.data.busyId) return;
    wx.showModal({
      title: "确认到院猫只",
      content: `请核对申请照片，确认到院的是「${item.cat_name}」这只猫。确认后申请照片会自动作为术前资料。`,
      success: async (res) => {
        if (!res.confirm) return;
        this.setData({ busyId: id });
        try {
          await staffPost(`/api/staff-miniapp/tnr/${id}/verify`, {});
          wx.showToast({ title: "已确认到院", icon: "success" });
          this.loadData();
        } catch (err) { wx.showModal({ title: "确认失败", content: (err && (err.detail || err.errMsg)) || "请稍后重试", showCancel: false }); }
        finally { this.setData({ busyId: 0 }); }
      }
    });
  },
  chooseMedia(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const kind = e.currentTarget.dataset.kind || "after";
    if (this.data.busyId) return;
    wx.chooseMedia({
      count: 6, mediaType: ["image", "video"], sourceType: ["camera", "album"],
      maxDuration: 60, camera: "back",
      success: async (res) => {
        const files = res.tempFiles || [];
        if (!files.length) return;
        this.setData({ busyId: id });
        wx.showLoading({ title: `上传 0/${files.length}`, mask: true });
        try {
          for (let i = 0; i < files.length; i += 1) {
            wx.showLoading({ title: `上传 ${i + 1}/${files.length}`, mask: true });
            const file = files[i];
            await staffUpload(`/api/staff-miniapp/tnr/${id}/upload`, file.tempFilePath, {
              kind, media_type: file.fileType === "video" ? "video" : "image"
            });
          }
          wx.hideLoading();
          wx.showToast({ title: kind === "after" ? "术后资料已上传" : "术前资料已上传", icon: "success" });
          this.loadData();
        } catch (err) {
          wx.hideLoading();
          wx.showModal({ title: "上传失败", content: (err && (err.detail || err.errMsg)) || "请检查网络后重试", showCancel: false });
        } finally { this.setData({ busyId: 0 }); }
      }
    });
  },
  completeSurgery(e) {
    const id = Number(e.currentTarget.dataset.id || 0);
    const item = this.data.items.find((row) => row.id === id);
    if (!item || !item.can_complete || this.data.busyId) return;
    wx.showModal({
      title: "标记手术完成",
      content: `${item.cat_name}的术前、术后资料已齐全。确认手术已经完成？`,
      confirmText: "确认完成",
      success: async (res) => {
        if (!res.confirm) return;
        this.setData({ busyId: id });
        try {
          await staffPost(`/api/staff-miniapp/tnr/${id}/complete`, {});
          wx.showToast({ title: "已标记完成", icon: "success" });
          this.loadData();
        } catch (err) { wx.showModal({ title: "操作失败", content: (err && (err.detail || err.errMsg)) || "请稍后重试", showCancel: false }); }
        finally { this.setData({ busyId: 0 }); }
      }
    });
  },
  goToday() { wx.redirectTo({ url: "/pages/staff/today/today" }); },
  goCalendar() { wx.redirectTo({ url: "/pages/staff/calendar/calendar" }); },
  goCustomers() { wx.redirectTo({ url: "/pages/staff/customers/customers" }); },
  goMe() { wx.redirectTo({ url: "/pages/staff/me/me" }); }
});

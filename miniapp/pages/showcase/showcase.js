const app = getApp();

Page({
  data: {
    items: [],
    loading: true,
  },

  onLoad() {
    this.loadShowcase();
  },

  onPullDownRefresh() {
    this.loadShowcase(() => wx.stopPullDownRefresh());
  },

  loadShowcase(callback) {
    this.setData({ loading: true });
    wx.request({
      url: app.globalData.apiBase + "/api/showcase",
      method: "GET",
      success: (res) => {
        if (res.statusCode === 200 && res.data && res.data.items) {
          this.setData({ items: res.data.items, loading: false });
        } else {
          this.setData({ items: [], loading: false });
          wx.showToast({ title: "加载失败，请稍后重试", icon: "none" });
        }
      },
      fail: () => {
        this.setData({ items: [], loading: false });
        wx.showToast({ title: "网络错误，请检查连接", icon: "none" });
      },
      complete: () => {
        if (callback) callback();
      }
    });
  },

  onPreviewImage(e) {
    const src = e.currentTarget.dataset.src;
    const all = e.currentTarget.dataset.all || [src];
    wx.previewImage({
      current: src,
      urls: all,
    });
  },
});

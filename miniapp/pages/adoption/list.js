const { getJson } = require("../../utils/api");
const app = getApp();

Page({
  onShareAppMessage() {
    return { title: "这些猫狗在等你回家 · 大风动物医院", path: "/pages/adoption/list" };
  },
  onShareTimeline() {
    return { title: "这些猫狗在等你回家 · 大风动物医院" };
  },
  data: { pets: [], loading: true, apiBase: "" },

  onLoad() {
    this.setData({ apiBase: app.globalData.apiBase });
    this._load();
  },

  onShow() {
    this._load();
  },

  _load() {
    this.setData({ loading: true });
    getJson("/api/adoption").then(list => {
      this.setData({ pets: list, loading: false });
    }).catch(() => {
      this.setData({ loading: false });
    });
  },

  onTapPet(e) {
    const id = e.currentTarget.dataset.id;
    wx.navigateTo({ url: `/pages/adoption/detail?id=${id}` });
  },
});

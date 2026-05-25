Page({
  onShareAppMessage() {
    return { title: "大风动物医院 · 流浪猫 TNR 申请", path: "/pages/index/index" };
  },
  onShareTimeline() {
    return { title: "大风动物医院 · 流浪猫 TNR 申请" };
  },
  data: { rec: null },

  onLoad(options) {
    this.setData({
      rec: {
        id: options.id || "",
        owner_name: decodeURIComponent(options.name || ""),
        owner_phone: decodeURIComponent(options.phone || ""),
        animal_name: decodeURIComponent(options.animal || ""),
      }
    });
  },
});

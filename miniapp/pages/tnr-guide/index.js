Page({
  goApply() {
    wx.navigateTo({ url: "/pages/apply/index" });
  },

  goStatus() {
    wx.navigateTo({ url: "/pages/status/status" });
  },

  onShareAppMessage() {
    return {
      title: "流浪猫 TNR 申请流程｜大风动物医院",
      path: "/pages/tnr-guide/index"
    };
  },

  onShareTimeline() {
    return {
      title: "流浪猫 TNR 申请流程｜大风动物医院"
    };
  }
});

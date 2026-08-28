Page({
  data: { url: "" },
  onLoad(options) {
    try { this.setData({ url: decodeURIComponent(options.url || "") }); }
    catch (e) { wx.showModal({ title: "链接错误", content: "无法打开接种同意书", showCancel: false }); }
  }
});

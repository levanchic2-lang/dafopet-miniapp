const { getJson, wxLoginCode } = require("../../utils/api");

Page({
  data: { recent: null },
  async onShow() {
    try {
      const id = Number(wx.getStorageSync("VACCINE_REG_ID") || 0);
      if (!id) return;
      const code = await wxLoginCode();
      const recent = await getJson(`/api/vaccine-registration/${id}`, { code });
      this.setData({ recent });
    } catch (e) {}
  },
  continueConsent() {
    const url = this.data.recent && this.data.recent.consent_url;
    if (!url) return;
    wx.navigateTo({ url: "/pages/vaccine/consent?url=" + encodeURIComponent(url) });
  },
  goCombo() { wx.navigateTo({ url: "/pages/vaccine/combo" }); },
  goRabies() { wx.navigateTo({ url: "/pages/rabies/index" }); },
  onShareAppMessage() { return { title: "大风动物医院 · 疫苗接种登记", path: "/pages/vaccine/index" }; }
});

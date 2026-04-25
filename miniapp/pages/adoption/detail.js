const { getJson } = require("../../utils/api");
const app = getApp();

const PHONE_MOBILE = "17820633031";
const PHONE_LANDLINE = "075528704890";

Page({
  data: { pet: null, images: [], apiBase: "" },

  onLoad(options) {
    const id = parseInt(options.id || "0");
    const base = app.globalData.apiBase;
    this.setData({ apiBase: base });
    if (!id) return;
    getJson("/api/adoption").then(list => {
      const pet = list.find(p => p.id === id);
      if (!pet) return;
      wx.setNavigationBarTitle({ title: pet.name || "待领养动物" });
      const images = [];
      if (pet.has_image1) images.push(`${base}/api/adoption/${id}/image/1`);
      if (pet.has_image2) images.push(`${base}/api/adoption/${id}/image/2`);
      this.setData({ pet, images });
    });
  },

  onCallPhone() {
    wx.showActionSheet({
      itemList: ["手机：" + PHONE_MOBILE, "座机：0755-28704890"],
      success: (res) => {
        wx.makePhoneCall({
          phoneNumber: res.tapIndex === 0 ? PHONE_MOBILE : PHONE_LANDLINE,
        });
      }
    });
  },
});

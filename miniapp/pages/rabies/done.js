Page({
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

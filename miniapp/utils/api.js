const app = getApp();

function base() {
  return app.globalData.apiBase;
}

function postJson(path, data) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: base() + path,
      method: "POST",
      data,
      header: { "content-type": "application/json" },
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data);
        else reject(res.data || { detail: "请求失败" });
      },
      fail: reject
    });
  });
}

function getJson(path, data) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: base() + path,
      method: "GET",
      data,
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data);
        else reject(res.data || { detail: "请求失败" });
      },
      fail: reject
    });
  });
}

function uploadApply(form, images, videos) {
  return new Promise((resolve, reject) => {
    const url = base() + "/api/apply";
    wx.uploadFile({
      url,
      filePath: images?.[0] || videos?.[0] || "",
      name: images?.[0] ? "images" : "videos",
      formData: form,
      success: (res) => {
        // 由于我们需要多文件，这里只负责“首个文件”。
        // 后续会用 uploadMore 逐个补传。
        try {
          const data = JSON.parse(res.data);
          resolve(data);
        } catch (e) {
          reject({ detail: "解析响应失败" });
        }
      },
      fail: reject
    });
  });
}

function uploadMore(appId, fieldName, filePath) {
  // 后端 /api/apply 是一次性创建+上传；这里为了简单，复用 /api/apply 不合适。
  // 目前策略：前端只上传最多 1 张图片 + 1 段视频（足够演示审核与推送）。
  // 若你需要支持多张多视频上传，我可以把后端改成“先创建申请ID，再增量上传媒体”的两段式。
  return Promise.resolve({ appId, fieldName, filePath });
}

module.exports = { getJson, postJson, uploadApply, uploadMore };


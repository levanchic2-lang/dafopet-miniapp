function ensurePrivacyAuthorized() {
  return new Promise((resolve, reject) => {
    if (!wx.requirePrivacyAuthorize || typeof wx.requirePrivacyAuthorize !== "function") {
      resolve();
      return;
    }
    wx.requirePrivacyAuthorize({
      success: resolve,
      fail: reject
    });
  });
}

function openPrivacyContract() {
  if (!wx.openPrivacyContract || typeof wx.openPrivacyContract !== "function") return;
  wx.openPrivacyContract({
    fail: () => {}
  });
}

function showPrivacyDeniedModal(featureLabel) {
  wx.showModal({
    title: "需要先同意隐私保护指引",
    content: `${featureLabel}涉及个人信息处理，请先阅读并同意小程序隐私保护指引后再继续。`,
    confirmText: "查看指引",
    cancelText: "稍后再试",
    success: (res) => {
      if (res.confirm) openPrivacyContract();
    }
  });
}

async function runWithPrivacyGuard(featureLabel, action, options = {}) {
  try {
    await ensurePrivacyAuthorized();
  } catch (err) {
    if (!options.silent) showPrivacyDeniedModal(featureLabel);
    throw err;
  }
  return action();
}

module.exports = {
  ensurePrivacyAuthorized,
  runWithPrivacyGuard,
  showPrivacyDeniedModal,
  openPrivacyContract
};

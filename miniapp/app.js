const API_BASE_BY_ENV = {
  develop: "https://dafopet.com",
  trial: "https://dafopet.com",
  release: "https://dafopet.com"
};

// 留空 = 跟随微信环境自动切换（develop/trial/release）
// 本地调试时可临时改为 "develop" 并在 resolveDevelopApiBase 里指定局域网地址
const FORCE_ENV = "";

function resolveEnvVersion() {
  if (FORCE_ENV) return FORCE_ENV;
  try {
    const info = wx.getAccountInfoSync && wx.getAccountInfoSync();
    const envVersion = info && info.miniProgram ? info.miniProgram.envVersion : "";
    return envVersion || "develop";
  } catch (e) {
    return "develop";
  }
}

function resolveDevelopApiBase() {
  try {
    const saved = wx.getStorageSync("DEV_API_BASE") || "";
    if (saved && /^https?:\/\//i.test(saved)) return String(saved);
  } catch (e) {}
  return API_BASE_BY_ENV.develop;
}

const envVersion = resolveEnvVersion();
const apiBase =
  envVersion === "develop"
    ? resolveDevelopApiBase()
    : API_BASE_BY_ENV[envVersion] || API_BASE_BY_ENV.develop;

App({
  globalData: {
    envVersion,
    apiBase,
    shenzhenRegions: null
  }
});

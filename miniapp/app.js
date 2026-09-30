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
  _staffReminderTimer: null,
  _staffReminderModalOpen: false,
  _lastStaffReminderSignature: "",
  _lastStaffReminderAt: 0,

  onShow() {
    this.startStaffReminderPolling(true);
  },

  onHide() {
    this.stopStaffReminderPolling();
  },

  stopStaffReminderPolling() {
    if (this._staffReminderTimer) clearInterval(this._staffReminderTimer);
    this._staffReminderTimer = null;
  },

  startStaffReminderPolling(immediate) {
    this.stopStaffReminderPolling();
    if (immediate) setTimeout(() => this.checkStaffMedicationReminders(), 1200);
    this._staffReminderTimer = setInterval(() => this.checkStaffMedicationReminders(), 60000);
  },

  checkStaffMedicationReminders() {
    let token = "";
    try { token = wx.getStorageSync("STAFF_TOKEN") || ""; } catch (e) {}
    if (!token || this._staffReminderModalOpen) return;
    wx.request({
      url: this.globalData.apiBase + "/api/staff-miniapp/medication-reminders",
      method: "GET",
      header: { "Authorization": "Bearer " + token },
      success: (res) => {
        if (res.statusCode !== 200) return;
        const data = res.data || {};
        const ids = data.ids || [];
        if (!data.count || !ids.length) {
          this._lastStaffReminderSignature = "";
          return;
        }
        const signature = ids.join(",");
        const now = Date.now();
        if (signature === this._lastStaffReminderSignature && now - this._lastStaffReminderAt < 5 * 60 * 1000) return;
        this._lastStaffReminderSignature = signature;
        this._lastStaffReminderAt = now;
        const groups = (data.groups || []).slice(0, 3);
        const lines = groups.map(item => `${item.first_time} ${item.pet_name}${item.cage_code ? " · " + item.cage_code : ""}（${item.count}项）`);
        if ((data.groups || []).length > 3) lines.push(`另有${data.groups.length - 3}只动物`);
        try { wx.vibrateLong(); } catch (e) {}
        this._staffReminderModalOpen = true;
        wx.showModal({
          title: "住院用药到时",
          content: lines.join("\n"),
          confirmText: "立即处理",
          cancelText: "稍后",
          confirmColor: "#171717",
          complete: () => { this._staffReminderModalOpen = false; },
          success: (modal) => {
            if (!modal.confirm) return;
            const pages = getCurrentPages();
            const current = pages.length ? pages[pages.length - 1] : null;
            if (current && current.route === "pages/staff/inpatient-meds/inpatient-meds") {
              if (typeof current.loadData === "function") current.loadData();
              return;
            }
            wx.navigateTo({ url: "/pages/staff/inpatient-meds/inpatient-meds" });
          }
        });
      }
    });
  },

  globalData: {
    envVersion,
    apiBase,
    shenzhenRegions: null
  }
});

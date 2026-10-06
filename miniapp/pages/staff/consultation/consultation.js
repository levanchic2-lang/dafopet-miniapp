const { staffGet, staffPost, staffUpload } = require("../../../utils/api");
const { runWithPrivacyGuard } = require("../../../utils/privacy");

const EMPTY_FORM = {
  chief_complaint: "",
  physical_exam: "",
  diagnosis: "",
  treatment_plan: "",
  notes: ""
};

function pad2(value) { return String(value).padStart(2, "0"); }

Page({
  data: {
    id: 0,
    loading: true,
    busy: false,
    error: "",
    speechConfigured: true,
    visit: {},
    pet: {},
    customer: {},
    draft: {},
    form: Object.assign({}, EMPTY_FORM),
    recording: false,
    elapsedMs: 0,
    elapsedLabel: "00:00",
    showTranscript: false
  },

  onLoad(options) {
    this.setData({ id: Number(options.id || 0) });
    this.setupRecorder();
    this.load();
  },

  onUnload() {
    this.clearTimers();
    if (this.data.recording && this.recorder) this.recorder.stop();
  },

  onPullDownRefresh() { this.load(); },

  setupRecorder() {
    this.recorder = wx.getRecorderManager();
    this.recorder.onStart(() => {
      this.recordStartedAt = Date.now();
      this.setData({ recording: true, elapsedMs: 0, elapsedLabel: "00:00", error: "" });
      this.recordTimer = setInterval(() => this.updateElapsed(Date.now() - this.recordStartedAt), 500);
    });
    this.recorder.onStop((res) => {
      if (this.recordTimer) clearInterval(this.recordTimer);
      this.recordTimer = null;
      const duration = Number(res.duration || this.data.elapsedMs || 0);
      this.setData({ recording: false, elapsedMs: duration });
      if (res.tempFilePath) this.uploadRecording(res.tempFilePath, duration);
      else this.setData({ error: "没有取得录音文件，请重新录音" });
    });
    this.recorder.onError((err) => {
      if (this.recordTimer) clearInterval(this.recordTimer);
      this.recordTimer = null;
      this.setData({ recording: false, error: (err && err.errMsg) || "录音失败，请检查麦克风权限" });
    });
  },

  clearTimers() {
    if (this.recordTimer) clearInterval(this.recordTimer);
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.recordTimer = null;
    this.pollTimer = null;
  },

  updateElapsed(ms) {
    const seconds = Math.floor(Math.max(0, ms) / 1000);
    this.setData({ elapsedMs: ms, elapsedLabel: `${pad2(Math.floor(seconds / 60))}:${pad2(seconds % 60)}` });
  },

  async load(silent = false) {
    if (!this.data.id) return;
    if (!silent) this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet(`/api/staff-miniapp/visits/${this.data.id}/consultation`);
      const draft = result.draft || {};
      const source = draft.status === "ready" || draft.status === "confirmed" ? draft : (result.visit || {});
      this.setData({
        visit: result.visit || {}, pet: result.pet || {}, customer: result.customer || {},
        draft, speechConfigured: result.speech_configured !== false,
        form: {
          chief_complaint: source.chief_complaint || "",
          physical_exam: source.physical_exam || "",
          diagnosis: source.diagnosis || "",
          treatment_plan: source.treatment_plan || "",
          notes: source.notes || ""
        }
      });
      wx.setNavigationBarTitle({ title: `${(result.pet && result.pet.name) || "宠物"} · 接诊记录` });
      if (draft.status === "processing") this.schedulePoll();
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "接诊记录加载失败" });
    } finally {
      if (!silent) this.setData({ loading: false });
      wx.stopPullDownRefresh();
    }
  },

  schedulePoll() {
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => this.load(true), 2500);
  },

  startRecording() {
    if (this.data.busy || this.data.recording) return;
    if (this.data.visit.status === "closed") { wx.showToast({ title: "病历已结束", icon: "none" }); return; }
    if (!this.data.speechConfigured) {
      wx.showModal({ title: "语音服务未配置", content: "服务器尚未配置接诊录音转写服务，请联系管理员。", showCancel: false });
      return;
    }
    wx.showModal({
      title: "开始接诊录音",
      content: "请先确认已告知主人：本次录音仅用于整理院内病历，医生确认后录音文件会删除。",
      confirmText: "已告知，开始",
      success: (res) => {
        if (!res.confirm) return;
        runWithPrivacyGuard("接诊录音", () => new Promise((resolve, reject) => {
          wx.authorize({ scope: "scope.record", success: resolve, fail: reject });
        })).then(() => this.recorder.start({
          duration: 600000, sampleRate: 16000, numberOfChannels: 1,
          encodeBitRate: 48000, format: "mp3", frameSize: 50
        })).catch(err => {
          if (err && String(err.errMsg || "").includes("privacy")) return;
          wx.showModal({
            title: "需要麦克风权限", content: "请在设置中允许录音后再试。", confirmText: "去设置",
            success: setting => { if (setting.confirm) wx.openSetting(); }
          });
        });
      }
    });
  },

  stopRecording() {
    if (this.data.recording && this.recorder) this.recorder.stop();
  },

  async uploadRecording(filePath, duration) {
    this.setData({ busy: true, error: "" });
    wx.showLoading({ title: "上传录音", mask: true });
    try {
      const baseId = this.data.draft.status === "ready" ? Number(this.data.draft.id || 0) : 0;
      const result = await staffUpload(
        `/api/staff-miniapp/visits/${this.data.id}/consultation/upload`,
        filePath,
        { duration_ms: String(duration || 0), base_draft_id: String(baseId) }
      );
      this.setData({ draft: result.draft || { status: "processing" } });
      wx.showToast({ title: "正在整理", icon: "success" });
      this.schedulePoll();
    } catch (e) {
      this.setData({ error: (e && (e.detail || e.errMsg)) || "录音上传失败，请重试" });
    } finally {
      wx.hideLoading();
      this.setData({ busy: false });
    }
  },

  bindField(e) {
    const field = e.currentTarget.dataset.field;
    if (!Object.prototype.hasOwnProperty.call(EMPTY_FORM, field)) return;
    this.setData({ [`form.${field}`]: e.detail.value || "" });
  },

  toggleTranscript() { this.setData({ showTranscript: !this.data.showTranscript }); },

  async retryDraft() {
    if (!this.data.draft.id || this.data.busy) return;
    this.setData({ busy: true, error: "" });
    try {
      const result = await staffPost(`/api/staff-miniapp/visits/${this.data.id}/consultation/${this.data.draft.id}/retry`, {});
      this.setData({ draft: result.draft || { status: "processing" } });
      this.schedulePoll();
    } catch (e) {
      this.setData({ error: (e && (e.detail || e.errMsg)) || "重新整理失败" });
    } finally { this.setData({ busy: false }); }
  },

  confirmDraft() {
    if (!this.data.draft.id || this.data.draft.status !== "ready" || this.data.busy) return;
    wx.showModal({
      title: "确认写入病历",
      content: "请确认主诉、检查、评估和计划已经核对。写入后将删除本次录音文件。",
      confirmText: "确认写入",
      success: async res => {
        if (!res.confirm) return;
        this.setData({ busy: true, error: "" });
        try {
          await staffPost(`/api/staff-miniapp/visits/${this.data.id}/consultation/${this.data.draft.id}/confirm`, this.data.form);
          wx.showToast({ title: "病历已更新", icon: "success" });
          setTimeout(() => wx.navigateBack(), 700);
        } catch (e) {
          this.setData({ error: (e && (e.detail || e.errMsg)) || "写入病历失败" });
        } finally { this.setData({ busy: false }); }
      }
    });
  }
});

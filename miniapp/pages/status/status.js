const STATUS_ZH = {
  draft: "草稿",
  pending_ai: "系统处理中",
  pending_manual: "待人工审核",
  pre_approved: "预通过（待复核）",
  approved: "已通过",
  scheduled: "已预约",
  arrived_verified: "到院已核对",
  surgery_completed: "手术已完成",
  rejected: "未通过",
  cancelled: "已取消",
  no_show: "爽约"
};

const STATUS_DESC = {
  pending_ai: "系统正在进行预审，请稍后刷新或等待通知。",
  pending_manual: "需要医院人工复核，请保持电话畅通。",
  pre_approved: "已进入优先复核队列，医院会尽快确认。",
  approved: "已通过预审，可等待医院联系预约或主动联系医院确认到院时间。",
  scheduled: "已预约，请按约定时间携带猫咪到院。",
  arrived_verified: "到院已核对，等待手术安排或按医嘱处理。",
  surgery_completed: "手术已完成，请按医嘱护理。",
  rejected: "未通过审核，可联系医院了解原因，必要时重新提交材料。",
  cancelled: "本次申请已取消，如需继续请联系医院或重新提交。",
  no_show: "已标记爽约，如需继续请联系医院重新安排。"
};

const CHANNEL_ZH = {
  wechat_miniapp: "小程序订阅消息",
  email: "邮件",
  log: "系统记录"
};

const FLOW_STEPS = [
  { key: "submitted", label: "已提交" },
  { key: "review", label: "复核中" },
  { key: "approved", label: "已通过" },
  { key: "scheduled", label: "已预约" },
  { key: "arrived_verified", label: "到院核对" },
  { key: "surgery_completed", label: "手术完成" }
];

function statusZh(code) {
  const k = String(code || "").trim();
  return STATUS_ZH[k] || k || "—";
}

function statusDesc(code) {
  const k = String(code || "").trim();
  return STATUS_DESC[k] || "—";
}

function isTerminated(code) {
  const s = String(code || "").trim();
  return s === "rejected" || s === "cancelled" || s === "no_show";
}

function deriveStepIndex(code) {
  const s = String(code || "").trim();
  if (!s) return 0;
  if (s === "draft") return 0;
  if (s === "pending_ai" || s === "pending_manual" || s === "pre_approved") return 1;
  if (s === "approved") return 2;
  if (s === "scheduled") return 3;
  if (s === "arrived_verified") return 4;
  if (s === "surgery_completed") return 5;
  if (isTerminated(s)) return 1;
  return 1;
}

function buildTimelineSteps(code) {
  const stepIndex = deriveStepIndex(code);
  const terminated = isTerminated(code);
  const currentStatusZh = statusZh(code);
  return FLOW_STEPS.map((step, idx) => {
    let state = "upcoming";
    if (idx < stepIndex) state = "done";
    else if (idx === stepIndex) state = terminated ? "halted" : "current";
    return {
      ...step,
      idx,
      state,
      tag: state === "current" ? "当前" : state === "halted" ? "终止" : "",
      meta: idx === stepIndex && currentStatusZh && currentStatusZh !== step.label ? currentStatusZh : "",
      is_last: idx === FLOW_STEPS.length - 1,
      line_state: idx < stepIndex ? "done" : ""
    };
  });
}

function nextActionText(info) {
  const s = String(info?.status || "").trim();
  if (s === "pending_ai" || s === "pending_manual" || s === "pre_approved") {
    return "等待医院复核。请保持电话畅通，如需补充材料医院会联系你。";
  }
  if (s === "approved") {
    return "等待医院联系预约；如较久未联系，可主动联系门店确认到院时间。";
  }
  if (s === "scheduled") {
    const d = (info?.appointment_at || "").trim();
    return d ? `请按预约日期 ${d} 到院。` : "请按约定时间到院。";
  }
  if (s === "arrived_verified") {
    return "到院已核对，请按医院安排与医嘱处理。";
  }
  if (s === "surgery_completed") {
    return "手术已完成，请按医嘱护理；如同意展示，可在展示页查看术前术后资料。";
  }
  if (s === "rejected") {
    return "可联系医院了解原因；如需继续，可补充材料后重新提交。";
  }
  if (s === "cancelled") {
    return "如需继续，请联系医院或重新提交申请。";
  }
  if (s === "no_show") {
    return "如需继续，请联系医院重新安排。";
  }
  return "请联系医院确认进度。";
}

function normalizeNotifications(list) {
  const arr = Array.isArray(list) ? list : [];
  return arr.map((n) => ({
    ...n,
    channel_zh: CHANNEL_ZH[String(n?.channel || "").trim()] || (n?.channel || "—"),
    success_zh: n?.success ? "成功" : "失败"
  }));
}

// 门店联系信息
const CLINIC_CONTACTS = {
  "大风动物医院（东环店）": {
    phone: "18026901718",
    landline: "075528018071",
    address: "广东省深圳市龙华区龙华街道建设东路聚豪国际a栋8号铺",
    lat: null,
    lng: null
  },
  "大风动物医院（横岗店）": {
    phone: "17820633031",
    landline: "075528704890",
    address: "广东省深圳市龙岗区横岗街道华侨新村社区隆盛花园S2商铺A1013",
    lat: null,
    lng: null
  }
};

function clinicContact(storeName) {
  const key = String(storeName || "").trim();
  return CLINIC_CONTACTS[key] || { phone: "", address: "", lat: null, lng: null };
}

function genderZh(g) {
  const k = String(g || "").trim().toLowerCase();
  if (!k) return "未填";
  if (k === "male") return "公";
  if (k === "female") return "母";
  if (k === "unknown") return "未知";
  return k || "未填";
}

Page({
  data: {
    id: "",
    loading: false,
    info: null,
    view: null,
    error: "",
    myOrders: [],
    myOrdersLoading: false,
    openid: "",
    bindLoading: false,
    bindStatusText: "",
    claimPhone: "",
    claimIdNumber: "",
    claimLoading: false,
    claimStatusText: ""
  },
  onLoad(q) {
    const id = q.id || wx.getStorageSync("LAST_APP_ID") || "";
    const openid = wx.getStorageSync("WECHAT_OPENID") || "";
    this.setData({
      id: String(id || ""),
      openid: String(openid || "")
    });
    // 优先拉取“本账号所有订单”
    this.fetchMyOrders();
    if (id) this.fetchStatus();
  },
  fetchStatus() {
    const app = getApp();
    const id = String(this.data.id || "").trim().replace(/^#/, "");
    if (!id) {
      this.setData({ error: "请输入申请编号。", info: null, view: null });
      return;
    }
    this.setData({ loading: true, error: "", info: null, view: null });
    wx.request({
      url: app.globalData.apiBase + `/api/app/${encodeURIComponent(id)}/status`,
      method: "GET",
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          const info = res.data || null;
          const st = String(info?.status || "").trim();
          const note = (info?.note || "").trim();
          const terminated = isTerminated(st);
          const reasonText = terminated ? (note || "如需了解原因，请联系医院。") : note;
          const contact = clinicContact(info?.clinic_store);
          const view = {
            status: st,
            status_zh: statusZh(st),
            status_desc: statusDesc(st),
            step_index: deriveStepIndex(st),
            timeline_steps: buildTimelineSteps(st),
            terminated,
            next_action: nextActionText(info),
            reason_text: reasonText,
            contact,
            notifications: normalizeNotifications(info?.notifications)
          };
          this.setData({ info, view, loading: false });
        } else {
          const msg =
            (res.data && (res.data.detail || res.data.message)) ||
            `查询失败（${res.statusCode}）`;
          this.setData({ error: msg, loading: false });
        }
      },
      fail: (e) => {
        this.setData({
          error: (e && e.errMsg) || "请求失败，请检查网络与后端地址。",
          loading: false
        });
      }
    });
  },

  tapOrder(e) {
    const id = e.currentTarget.dataset.id || "";
    if (!id) return;
    this.setData({ id: String(id) });
    this.fetchStatus();
    try {
      wx.pageScrollTo({ scrollTop: 0, duration: 200 });
    } catch (e2) {}
  },

  fetchMyOrders() {
    const app = getApp();
    const openid = String(this.data.openid || "").trim();
    if (!openid) {
      // 没有 openid 时不阻断：仍可用手动输入编号查询
      return;
    }
    this.setData({ myOrdersLoading: true });
    wx.request({
      url: app.globalData.apiBase + "/api/wechat/my-apps",
      method: "POST",
      data: { openid },
      header: { "content-type": "application/json" },
      success: (res) => {
        const j = res.data || {};
        const items = Array.isArray(j.items) ? j.items : [];
        const myOrders = items.map((it) => ({
          ...it,
          status_zh: statusZh(it.status),
          updated: it.updated_at || it.created_at || "",
          cat_title:
            String(it.cat_nickname || "").trim() ||
            (String(it.health_note_brief || "").trim() ? "未命名（可按流浪特征称呼）" : "未命名（按特征称呼）"),
          cat_gender_zh: genderZh(it.cat_gender),
        }));
        this.setData({ myOrders, myOrdersLoading: false });
        // 若当前未选中任何订单，默认打开最新一条
        if ((!this.data.id || !String(this.data.id).trim()) && myOrders.length) {
          const firstId = String(myOrders[0].id || "");
          if (firstId) {
            this.setData({ id: firstId });
            this.fetchStatus();
          }
        }
      },
      fail: () => {
        this.setData({ myOrdersLoading: false });
      }
    });
  },

  onClaimPhoneInput(e) {
    const v = (e && e.detail ? e.detail.value : "") || "";
    this.setData({ claimPhone: String(v).trim() });
  },
  onClaimIdInput(e) {
    const v = (e && e.detail ? e.detail.value : "") || "";
    this.setData({ claimIdNumber: String(v).trim().toUpperCase() });
  },

  claimHistoryOrders() {
    const app = getApp();
    const openid = String(this.data.openid || "").trim();
    const phone = String(this.data.claimPhone || "").trim();
    const id_number = String(this.data.claimIdNumber || "").trim().toUpperCase();
    if (!openid) return;
    if (!/^1\d{10}$/.test(phone)) {
      wx.showModal({ title: "提示", content: "请填写 11 位中国大陆手机号。", showCancel: false });
      return;
    }
    if (!(id_number.length === 18 ? /^\d{17}[\dX]$/.test(id_number) : id_number.length === 15 ? /^\d{15}$/.test(id_number) : false)) {
      wx.showModal({ title: "提示", content: "请填写正确的 15 或 18 位身份证号（末位可为 X）。", showCancel: false });
      return;
    }
    this.setData({ claimLoading: true, claimStatusText: "正在找回历史订单…" });
    wx.request({
      url: app.globalData.apiBase + "/api/wechat/claim-apps",
      method: "POST",
      data: { openid, phone, id_number },
      header: { "content-type": "application/json" },
      success: (res) => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          const updated = Number((res.data || {}).updated || 0);
          this.setData({ claimStatusText: updated ? `找回成功：已关联 ${updated} 条历史订单。` : "未找到可关联的历史订单（可能之前已关联或信息不匹配）。" });
          this.fetchMyOrders();
        } else {
          const msg = (res.data && (res.data.detail || res.data.message)) || `找回失败（${res.statusCode}）`;
          this.setData({ claimStatusText: "找回失败：" + msg });
          wx.showModal({ title: "找回失败", content: String(msg), showCancel: false });
        }
      },
      fail: (e) => {
        const msg = (e && e.errMsg) || "请求失败，请检查网络与后端地址。";
        this.setData({ claimStatusText: "找回失败：" + msg });
        wx.showModal({ title: "找回失败", content: String(msg), showCancel: false });
      },
      complete: () => {
        this.setData({ claimLoading: false });
      }
    });
  },

  async bindAccount() {
    const app = getApp();
    this.setData({ bindLoading: true, bindStatusText: "正在绑定账号…" });

    const withTimeout = (promise, ms, label) =>
      new Promise((resolve, reject) => {
        const t = setTimeout(() => reject(new Error(label + " 超时")), ms);
        Promise.resolve(promise)
          .then((v) => {
            clearTimeout(t);
            resolve(v);
          })
          .catch((e) => {
            clearTimeout(t);
            reject(e);
          });
      });

    const fetchConfig = () =>
      new Promise((resolve) => {
        wx.request({
          url: app.globalData.apiBase + "/api/wechat/config",
          method: "GET",
          success: (res) => resolve((res && res.data) || {}),
          fail: () => resolve({})
        });
      });

    const postJson = (path, data) =>
      new Promise((resolve, reject) => {
        wx.request({
          url: app.globalData.apiBase + path,
          method: "POST",
          data,
          header: { "content-type": "application/json" },
          success: (res) => {
            if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data || {});
            else reject(res);
          },
          fail: reject
        });
      });

    try {
      // 1) 取模板ID（尽量不阻断；拿不到也可继续只绑定 openid）
      let tmplIds = [];
      const c = await withTimeout(fetchConfig(), 6000, "获取模板配置");
      const t1 = (c.wechat_tmpl_application_result || "").trim();
      const t2 = (c.wechat_tmpl_surgery_done || "").trim();
      if (t1) tmplIds.push(t1);
      if (t2) tmplIds.push(t2);
      tmplIds = Array.from(new Set(tmplIds)).slice(0, 2);

      if (tmplIds.length) {
        this.setData({ bindStatusText: "正在弹出订阅授权…" });
        await withTimeout(wx.requestSubscribeMessage({ tmplIds }), 12000, "订阅授权");
      }

      // 2) 登录换 openid
      this.setData({ bindStatusText: "正在登录…" });
      const loginRes = await withTimeout(wx.login(), 8000, "微信登录");
      const code = loginRes.code;
      const data = await withTimeout(postJson("/api/wechat/login", { code }), 8000, "换取openid");
      const openid = (data.openid || "").trim();
      if (!openid) throw new Error("未获取到 openid");

      try {
        wx.setStorageSync("WECHAT_OPENID", openid);
      } catch (e2) {}
      this.setData({ openid, bindStatusText: "绑定成功，正在加载订单…" });
      this.fetchMyOrders();
      this.setData({ bindStatusText: "绑定成功。" });
    } catch (e) {
      const msg = (e && (e.errMsg || e.message)) || "绑定失败";
      this.setData({ bindStatusText: "绑定失败：" + msg });
      wx.showModal({
        title: "绑定失败",
        content: String(msg),
        showCancel: false
      });
    } finally {
      this.setData({ bindLoading: false });
    }
  },
  callClinic() {
    const phone = this.data.view?.contact?.phone || "";
    if (!phone) {
      wx.showModal({
        title: "暂无电话",
        content: "该门店电话尚未配置，请联系医院或在代码里补全门店联系方式。",
        showCancel: false
      });
      return;
    }
    wx.makePhoneCall({ phoneNumber: phone });
  },
  copyClinic() {
    const store = this.data.info?.clinic_store || "";
    const c = this.data.view?.contact || {};
    const text = [store, c.address, c.phone, c.landline ? ("座机 " + c.landline) : ""]
      .filter(Boolean)
      .join(" | ");
    if (!text) return;
    wx.setClipboardData({ data: text });
  },
  openClinicMap() {
    const store = this.data.info?.clinic_store || "门店";
    const c = this.data.view?.contact || {};
    const lat = Number(c.lat);
    const lng = Number(c.lng);
    if (Number.isFinite(lat) && Number.isFinite(lng) && lat && lng) {
      wx.openLocation({
        latitude: lat,
        longitude: lng,
        name: store,
        address: c.address || ""
      });
      return;
    }
    if (c.address) {
      wx.setClipboardData({ data: c.address });
      wx.showToast({ title: "已复制地址", icon: "none" });
    } else {
      wx.showToast({ title: "暂无地址信息", icon: "none" });
    }
  },
  // 移除“复制编号/手动查询入口”后，不再需要对应方法
});

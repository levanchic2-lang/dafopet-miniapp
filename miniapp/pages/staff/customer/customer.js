const { staffGet } = require("../../../utils/api");

const depositLabels = { surgery: "手术押金", boarding: "寄养押金", beauty: "美容押金", other: "其他押金" };
const statusLabels = { active: "使用中", exhausted: "已用完", expired: "已过期", refunded: "已退款", held: "可用", applied: "已抵扣", partial_refund: "部分退款", issued: "可使用", used: "已使用", cancelled: "已取消" };
const txLabels = { recharge: "充值", consume: "消费", refund: "退款", adjust: "余额调整" };

Page({
  data: { id: 0, loading: true, error: "", activeTab: "pets", customer: {}, summary: {}, invoices: [], walletTransactions: [], packages: [], deposits: [], coupons: [] },
  onLoad(options) { this.setData({ id: Number(options.id || 0) }); this.load(); },
  async load() {
    if (!this.data.id) return;
    this.setData({ loading: true, error: "" });
    try {
      const result = await staffGet("/api/staff-miniapp/customers/" + this.data.id);
      const packages = (result.packages || []).map(x => Object.assign({}, x, { status_label: statusLabels[x.status] || x.status }));
      const deposits = (result.deposits || []).map(x => Object.assign({}, x, { category_label: depositLabels[x.category] || "押金", status_label: statusLabels[x.status] || x.status }));
      const coupons = (result.coupons || []).map(x => Object.assign({}, x, { status_label: statusLabels[x.status] || x.status, value_label: x.kind === "discount" ? Math.round(x.discount_pct * 10) + "折" : "¥" + x.face_value.toFixed(2) }));
      const walletTransactions = (result.wallet_transactions || []).map(x => Object.assign({}, x, { type_label: txLabels[x.type] || x.type, amount_label: (x.amount > 0 ? "+" : "") + x.amount.toFixed(2) }));
      this.setData({ customer: result.customer || {}, summary: result.summary || {}, invoices: result.invoices || [], packages, deposits, coupons, walletTransactions });
      wx.setNavigationBarTitle({ title: (result.customer && result.customer.name) || "客户档案" });
    } catch (e) {
      if (e && e.statusCode === 401) { wx.redirectTo({ url: "/pages/staff/login/login" }); return; }
      this.setData({ error: (e && (e.detail || e.errMsg)) || "客户档案加载失败" });
    } finally { this.setData({ loading: false }); wx.stopPullDownRefresh(); }
  },
  setTab(e) { this.setData({ activeTab: e.currentTarget.dataset.tab }); },
  openPet(e) { const id = Number(e.currentTarget.dataset.id || 0); if (id) wx.navigateTo({ url: "/pages/staff/pet/pet?id=" + id }); },
  callCustomer() { if (this.data.customer.phone) wx.makePhoneCall({ phoneNumber: this.data.customer.phone }); },
  onPullDownRefresh() { this.load(); }
});

/* 客户上下文 sidebar：开单页右侧异步加载小卡片
 *
 * 用法：在表单右栏放：
 *   <div data-customer-context data-customer-id="X" data-pet-id="Y"></div>
 *
 * 自动 fetch /api/admin/customer-context 并渲染：
 *   钱包 / 健康警示 / 未付单据 / 最近就诊 / 防疫近况 / 体重
 */
(function () {
  const STATE_COLOR = {
    ok: "#10b981",
    due_soon: "#f59e0b",
    expired: "#dc2626",
  };
  const STATE_LABEL = {
    ok: "正常",
    due_soon: "即将到期",
    expired: "已过期",
  };

  function el(html) {
    const d = document.createElement("div");
    d.innerHTML = html.trim();
    return d.firstElementChild;
  }

  function moneyFmt(n) {
    if (!n && n !== 0) return "—";
    return "¥ " + Number(n).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function renderInto(host, data) {
    const cards = [];

    // 📇 档案摘要（始终显示，避免右栏空白）
    if (data.summary) {
      const s = data.summary;
      const items = [];
      if (s.register_date) items.push(`<div>注册 <b>${s.register_date}</b></div>`);
      if (s.pet_count) items.push(`<div>名下 <b>${s.pet_count}</b> 只宠物</div>`);
      if (s.lifetime_paid > 0) items.push(`<div>累计消费 <b>${moneyFmt(s.lifetime_paid)}</b></div>`);
      if (s.last_visit) items.push(`<div>上次到店 <b>${s.last_visit}</b></div>`);
      if (items.length) {
        cards.push(`
          <div class="card" style="padding:.75rem 1rem;">
            <div style="font-size:.72rem;font-weight:700;color:var(--muted);letter-spacing:.04em;margin-bottom:.4rem;">档案摘要</div>
            <div style="font-size:.82rem;color:#475569;line-height:1.85;">${items.join('')}</div>
          </div>`);
      }
    }

    // 💰 钱包
    if (data.wallet) {
      cards.push(`
        <div class="card" style="padding:.75rem 1rem;">
          <div style="font-size:.72rem;font-weight:700;color:var(--muted);letter-spacing:.04em;margin-bottom:.35rem;">钱包余额</div>
          <div style="font-size:1.15rem;font-weight:700;color:#059669;">${moneyFmt(data.wallet.balance)}</div>
          <div style="font-size:.74rem;color:#94a3b8;margin-top:.15rem;">累计充值 ${moneyFmt(data.wallet.lifetime_recharge)}</div>
        </div>`);
    }

    // ⚠ 健康警示
    if (data.warnings && data.warnings.length) {
      const items = data.warnings.map(w =>
        `<div style="font-size:.85rem;padding:.2rem 0;"><span style="margin-right:.4rem;">${w.emoji}</span>${w.label}</div>`
      ).join("");
      cards.push(`
        <div class="card" style="padding:.75rem 1rem;background:#fef3c7;border-color:#fbbf24;">
          <div style="font-size:.72rem;font-weight:700;color:#92400e;letter-spacing:.04em;margin-bottom:.35rem;">健康警示</div>
          ${items}
        </div>`);
    }

    // 💸 未付单
    if (data.unpaid && data.unpaid.count > 0) {
      cards.push(`
        <a href="/admin/invoices?status=unpaid" style="text-decoration:none;color:inherit;">
        <div class="card" style="padding:.75rem 1rem;background:#fef2f2;border-color:#fca5a5;">
          <div style="font-size:.72rem;font-weight:700;color:#991b1b;letter-spacing:.04em;margin-bottom:.35rem;">未付单据 <span style="background:#dc2626;color:#fff;padding:1px 6px;border-radius:8px;font-size:.65rem;margin-left:.3rem;">${data.unpaid.count}</span></div>
          <div style="font-size:1.05rem;font-weight:700;color:#b91c1c;">${moneyFmt(data.unpaid.total)}</div>
          <div style="font-size:.72rem;color:#7f1d1d;margin-top:.15rem;">→ 收银台处理</div>
        </div></a>`);
    }

    // 📋 最近就诊
    if (data.recent_visits && data.recent_visits.length) {
      const items = data.recent_visits.map(v =>
        `<a href="/admin/visits/${v.id}" style="display:block;padding:.3rem 0;border-bottom:1px dashed #e2e8f0;text-decoration:none;color:inherit;">
          <div style="font-size:.78rem;color:#64748b;font-variant-numeric:tabular-nums;">${v.date}</div>
          <div style="font-size:.85rem;color:#1e293b;font-weight:500;">${v.diagnosis}</div>
        </a>`
      ).join("");
      cards.push(`
        <div class="card" style="padding:.75rem 1rem;">
          <div style="font-size:.72rem;font-weight:700;color:var(--muted);letter-spacing:.04em;margin-bottom:.35rem;">最近就诊</div>
          ${items}
        </div>`);
    }

    // 💉 防疫近况
    if (data.immunity && data.immunity.length) {
      const items = data.immunity.map(im => {
        const color = STATE_COLOR[im.state] || "#94a3b8";
        const stateText = STATE_LABEL[im.state] || "";
        return `<div style="display:flex;justify-content:space-between;align-items:center;padding:.2rem 0;font-size:.78rem;">
          <span style="color:#475569;">${im.type}</span>
          <span style="color:${color};font-weight:600;">${im.next || im.date}${im.state !== 'ok' ? ' · ' + stateText : ''}</span>
        </div>`;
      }).join("");
      cards.push(`
        <div class="card" style="padding:.75rem 1rem;">
          <div style="font-size:.72rem;font-weight:700;color:var(--muted);letter-spacing:.04em;margin-bottom:.35rem;">防疫近况</div>
          ${items}
        </div>`);
    }

    // ⚖ 体重
    if (data.weight) {
      const delta = data.weight.delta;
      let deltaTag = "";
      if (delta !== null && delta !== undefined && delta !== 0) {
        const arrow = delta > 0 ? "↑" : "↓";
        const color = delta > 0 ? "#059669" : "#dc2626";
        deltaTag = `<span style="color:${color};font-size:.78rem;margin-left:.5rem;">${arrow} ${Math.abs(delta).toFixed(2)} kg</span>`;
      }
      cards.push(`
        <div class="card" style="padding:.75rem 1rem;">
          <div style="font-size:.72rem;font-weight:700;color:var(--muted);letter-spacing:.04em;margin-bottom:.35rem;">体重</div>
          <div style="font-size:1.05rem;font-weight:700;">${data.weight.current.toFixed(2)} kg${deltaTag}</div>
          <div style="font-size:.72rem;color:#94a3b8;margin-top:.15rem;">${data.weight.date}</div>
        </div>`);
    }

    if (!cards.length) {
      host.style.display = "none";
      return;
    }

    host.innerHTML = cards.join("");
    host.style.display = "flex";
    host.style.flexDirection = "column";
    host.style.gap = ".75rem";
    host.style.marginTop = ".75rem";
  }

  async function loadFor(host) {
    const customerId = host.dataset.customerId || "0";
    const petId = host.dataset.petId || "0";
    if (customerId === "0" && petId === "0") return;
    try {
      const r = await fetch(`/api/admin/customer-context?customer_id=${customerId}&pet_id=${petId}`);
      if (!r.ok) return;
      const data = await r.json();
      renderInto(host, data || {});
    } catch (e) {
      // 静默失败：开单页不能因为 sidebar 挂了就崩
      console.debug("customer-context load failed", e);
    }
  }

  function init() {
    document.querySelectorAll("[data-customer-context]").forEach(loadFor);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

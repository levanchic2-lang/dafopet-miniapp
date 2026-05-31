/* 诊断 autocomplete — 给 textarea[name="diagnosis"] 挂下拉推荐 + 自由输入 */
(function () {
  const SYSTEM_COLOR = {
    gi: "#f59e0b", respiratory: "#06b6d4", skin: "#10b981",
    dental: "#a78bfa", ophthalmic: "#f472b6", urinary: "#fbbf24",
    renal: "#3b82f6", cardio: "#ef4444", neuro: "#8b5cf6",
    endocrine: "#ec4899", hemato: "#dc2626", oncology: "#7c3aed",
    ortho: "#22c55e", reproduction: "#f97316", infectious: "#dc2626",
    surgical: "#0ea5e9", general: "#94a3b8",
  };

  const SEVERITY_LABEL = {
    mild: "轻", moderate: "中", severe: "重", chronic: "慢",
  };

  function debounce(fn, ms) {
    let t;
    return function () {
      clearTimeout(t);
      const a = arguments;
      const self = this;
      t = setTimeout(function () { fn.apply(self, a); }, ms);
    };
  }

  function getCurrentToken(textarea) {
    // 获取光标所在位置的"当前词"：以 ， , ; 、 换行 等为分隔符
    const val = textarea.value;
    const pos = textarea.selectionStart || val.length;
    const before = val.substring(0, pos);
    // 找最后一个分隔符
    const m = before.match(/[，,;；、\n]\s*([^，,;；、\n]*)$/);
    if (m) {
      return { start: pos - m[1].length, end: pos, text: m[1].trim() };
    }
    return { start: 0, end: pos, text: before.trim() };
  }

  function setupAutocomplete(textarea) {
    if (textarea.dataset.diagAuto) return;
    textarea.dataset.diagAuto = "1";

    // 包一层 wrapper，方便定位 dropdown
    const wrap = document.createElement("div");
    wrap.style.cssText = "position:relative;";
    textarea.parentNode.insertBefore(wrap, textarea);
    wrap.appendChild(textarea);

    const dropdown = document.createElement("div");
    dropdown.className = "diag-ac-dropdown";
    dropdown.style.cssText = [
      "position:absolute",
      "left:0", "right:0",
      "top:100%",
      "max-height:280px",
      "overflow-y:auto",
      "background:#fff",
      "border:1px solid #cbd5e1",
      "border-radius:6px",
      "box-shadow:0 6px 14px rgba(0,0,0,.08)",
      "z-index:1000",
      "display:none",
      "font-size:.9rem",
    ].join(";");
    wrap.appendChild(dropdown);

    let activeIndex = -1;
    let currentResults = [];

    function close() {
      dropdown.style.display = "none";
      activeIndex = -1;
    }

    function renderResults(results, token) {
      currentResults = results;
      if (!results.length) {
        close();
        return;
      }
      dropdown.innerHTML = results.map((r, i) => {
        const color = SYSTEM_COLOR[r.system] || "#94a3b8";
        const sev = SEVERITY_LABEL[r.severity] || "";
        const sevTag = sev ? `<span style="font-size:.7rem;background:#fee2e2;color:#991b1b;padding:0 .35rem;border-radius:3px;margin-left:.3rem;">${sev}</span>` : "";
        // 高亮匹配段
        let displayName = r.name;
        if (token) {
          const safeToken = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
          const re = new RegExp(safeToken, "i");
          displayName = displayName.replace(re, m => `<b style="background:#fef3c7;">${m}</b>`);
        }
        return `<div class="diag-ac-item" data-idx="${i}" style="padding:.45rem .7rem;cursor:pointer;border-bottom:1px solid #f1f5f9;display:flex;align-items:center;gap:.5rem;">
          <span style="display:inline-block;width:3px;height:18px;background:${color};border-radius:2px;"></span>
          <span style="flex:1;">${displayName}${sevTag}</span>
          <span style="font-size:.72rem;color:#94a3b8;">${r.system_zh || ""}</span>
        </div>`;
      }).join("");
      dropdown.style.display = "block";
      activeIndex = -1;

      Array.from(dropdown.children).forEach(el => {
        el.addEventListener("mousedown", e => {
          e.preventDefault();
          insert(parseInt(el.dataset.idx, 10));
        });
        el.addEventListener("mouseenter", () => setActive(parseInt(el.dataset.idx, 10)));
      });
    }

    function setActive(i) {
      activeIndex = i;
      Array.from(dropdown.children).forEach((el, idx) => {
        el.style.background = idx === i ? "#dbeafe" : "";
      });
    }

    function insert(i) {
      if (i < 0 || i >= currentResults.length) return;
      const r = currentResults[i];
      const tok = getCurrentToken(textarea);
      const before = textarea.value.substring(0, tok.start);
      const after = textarea.value.substring(tok.end);
      // 插入诊断名 + 逗号空格分隔（如果后面还有字）
      const sep = (after && !/^[\s，,;；、]/.test(after)) ? "，" : "";
      textarea.value = before + r.name + sep + after;
      const newPos = before.length + r.name.length + sep.length;
      textarea.setSelectionRange(newPos, newPos);
      textarea.focus();
      // 触发 input 事件让自动保存生效
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      close();
    }

    const search = debounce(async function () {
      const tok = getCurrentToken(textarea);
      if (!tok.text || tok.text.length < 1) {
        close();
        return;
      }
      try {
        const r = await fetch("/api/diseases/search?q=" + encodeURIComponent(tok.text));
        if (!r.ok) { close(); return; }
        const data = await r.json();
        renderResults(data, tok.text);
      } catch (e) {
        close();
      }
    }, 180);

    textarea.addEventListener("input", search);
    textarea.addEventListener("focus", search);
    textarea.addEventListener("click", search);
    textarea.addEventListener("blur", () => setTimeout(close, 150));
    textarea.addEventListener("keydown", e => {
      if (dropdown.style.display === "none") return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive(Math.min(activeIndex + 1, currentResults.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive(Math.max(activeIndex - 1, 0));
      } else if (e.key === "Enter" && activeIndex >= 0) {
        e.preventDefault();
        insert(activeIndex);
      } else if (e.key === "Escape") {
        close();
      }
    });
  }

  function init() {
    document.querySelectorAll('textarea[name="diagnosis"]').forEach(setupAutocomplete);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

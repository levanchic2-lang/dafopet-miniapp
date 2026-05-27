/*
 * 体格检查辅助：模板下拉 + 快捷标签。
 * 自动给 <textarea data-exam-helper="1"> 的输入框上方加一行工具条。
 */
(function () {
  if (window.__EXAM_HELPER_LOADED) return;
  window.__EXAM_HELPER_LOADED = true;

  /* ─────────────────────────  数据  ───────────────────────── */
  // 整段模板（点 → 替换/追加到 textarea）
  const TEMPLATES = [
    {
      name: "常规体检",
      desc: "全面 9 系统检查",
      text:
        "体温___℃；心率___次/分；呼吸___次/分；体重___kg；BCS___/9\n" +
        "精神状态：\n" +
        "口腔/牙齿：\n" +
        "眼/鼻/耳：\n" +
        "浅表淋巴：未触及肿大\n" +
        "心肺：心音___；肺音___\n" +
        "腹部触诊：\n" +
        "皮肤被毛：\n" +
        "其他：",
    },
    {
      name: "简化版",
      desc: "疫苗 / 驱虫 / 短诊",
      text:
        "体温___℃；体重___kg；精神状态：良好\n" +
        "口腔/眼鼻耳：未见异常\n" +
        "心肺：未闻及明显杂音\n" +
        "腹部触诊：柔软无压痛",
    },
    {
      name: "术前评估",
      desc: "麻醉前必检项",
      text:
        "体温___℃；心率___次/分；呼吸___次/分；体重___kg\n" +
        "血压___mmHg；CRT___秒；BCS___/9\n" +
        "心肺：\n" +
        "腹部触诊：\n" +
        "脱水评估：\n" +
        "术前禁食：______小时\n" +
        "ASA 分级：",
    },
    {
      name: "急诊评估",
      desc: "ABCD + 黏膜",
      text:
        "意识：\n" +
        "TPR：体温___℃；心率___次/分；呼吸___次/分\n" +
        "黏膜色：；CRT___秒\n" +
        "脱水程度：\n" +
        "主诉相关重点：",
    },
  ];

  // 快捷标签（点 → 在光标位置插入文本）
  const CHIPS = [
    // 数值类
    { group: "vitals", label: "体温",  snippet: "体温___℃；" },
    { group: "vitals", label: "心率",  snippet: "心率___次/分；" },
    { group: "vitals", label: "呼吸",  snippet: "呼吸___次/分；" },
    { group: "vitals", label: "体重",  snippet: "体重___kg；" },
    { group: "vitals", label: "BCS",   snippet: "BCS___/9；" },
    { group: "vitals", label: "血压",  snippet: "血压___mmHg；" },
    { group: "vitals", label: "CRT",   snippet: "CRT___秒；" },
    // 系统类
    { group: "system", label: "精神",  snippet: "精神状态：" },
    { group: "system", label: "口腔",  snippet: "口腔/牙齿：" },
    { group: "system", label: "眼鼻耳", snippet: "眼/鼻/耳：" },
    { group: "system", label: "淋巴",  snippet: "浅表淋巴：" },
    { group: "system", label: "心音",  snippet: "心音：" },
    { group: "system", label: "肺音",  snippet: "肺音：" },
    { group: "system", label: "腹诊",  snippet: "腹部触诊：" },
    { group: "system", label: "皮毛",  snippet: "皮肤被毛：" },
    { group: "system", label: "神经",  snippet: "神经反射：" },
    { group: "system", label: "关节",  snippet: "关节：" },
    { group: "system", label: "生殖",  snippet: "生殖：" },
    // 常用结论
    { group: "verdict", label: "正常", snippet: "正常 " },
    { group: "verdict", label: "未见异常", snippet: "未见异常 " },
    { group: "verdict", label: "异常→", snippet: "异常：" },
  ];

  /* ─────────────────────────  样式  ───────────────────────── */
  const css = `
.exam-helper { margin: 0 0 .5rem; padding: .5rem .65rem; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; }
.exam-helper-row { display: flex; flex-wrap: wrap; gap: .3rem; align-items: center; margin-bottom: .4rem; }
.exam-helper-row:last-child { margin-bottom: 0; }
.exam-helper-tag { font-size: .68rem; color: #64748b; margin-right: .2rem; min-width: 2.4rem; flex-shrink: 0; }
.exam-chip {
  display: inline-flex; align-items: center; gap: .15rem;
  font-size: .75rem; padding: 2px 9px; border-radius: 12px;
  border: 1px solid #cbd5e1; background: #fff; color: #334155;
  cursor: pointer; user-select: none; transition: all .12s; line-height: 1.5;
}
.exam-chip:hover { background: #e0f2fe; border-color: #0284c7; color: #0c4a6e; }
.exam-chip.vitals { background: #fffbeb; border-color: #fcd34d; color: #92400e; }
.exam-chip.vitals:hover { background: #fde68a; }
.exam-chip.system { background: #f0fdf4; border-color: #86efac; color: #14532d; }
.exam-chip.system:hover { background: #bbf7d0; }
.exam-chip.verdict { background: #faf5ff; border-color: #d8b4fe; color: #581c87; }
.exam-chip.verdict:hover { background: #e9d5ff; }
.exam-tpl-btn {
  font-size: .75rem; padding: 3px 11px; border-radius: 12px;
  border: 1px solid #1d4ed8; background: #1d4ed8; color: #fff;
  cursor: pointer; user-select: none; transition: all .12s;
}
.exam-tpl-btn:hover { background: #1e40af; }
.exam-tpl-dropdown {
  position: absolute; z-index: 100; margin-top: 4px;
  background: #fff; border: 1px solid #cbd5e1; border-radius: 8px;
  box-shadow: 0 6px 18px rgba(0,0,0,.12); min-width: 220px; padding: .35rem 0;
}
.exam-tpl-item {
  padding: .5rem .75rem; cursor: pointer; font-size: .82rem;
  border-bottom: 1px solid #f1f5f9;
}
.exam-tpl-item:last-child { border-bottom: none; }
.exam-tpl-item:hover { background: #f1f5f9; }
.exam-tpl-item b { display: block; font-size: .85rem; color: #1e293b; }
.exam-tpl-item small { color: #64748b; font-size: .72rem; }
@media (max-width: 768px) {
  .exam-helper-tag { display: none; }
  .exam-chip { font-size: .78rem; padding: 3px 10px; }
}
`;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  /* ─────────────────────────  逻辑  ───────────────────────── */
  function insertAtCursor(ta, text) {
    const start = ta.selectionStart, end = ta.selectionEnd;
    const before = ta.value.substring(0, start);
    const after = ta.value.substring(end);
    // 前面没换行且非空 → 自动加空格（避免连写）
    let prefix = '';
    if (before && !/[\s\n>]$/.test(before)) prefix = ' ';
    ta.value = before + prefix + text + after;
    const newPos = start + prefix.length + text.length;
    ta.selectionStart = ta.selectionEnd = newPos;
    ta.focus();
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function applyTemplate(ta, tpl) {
    const cur = (ta.value || '').trim();
    if (cur) {
      const choice = confirm('已有内容。点「确定」追加到末尾，点「取消」清空后插入。');
      if (choice) {
        ta.value = cur + '\n' + tpl.text;
      } else {
        ta.value = tpl.text;
      }
    } else {
      ta.value = tpl.text;
    }
    ta.selectionStart = ta.selectionEnd = ta.value.length;
    ta.focus();
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function attach(textarea) {
    if (textarea.__examHelperAttached) return;
    textarea.__examHelperAttached = true;

    const helper = document.createElement('div');
    helper.className = 'exam-helper';

    // 第 1 行：模板按钮 + verdict chips
    const row1 = document.createElement('div');
    row1.className = 'exam-helper-row';
    const tplBtn = document.createElement('button');
    tplBtn.type = 'button';
    tplBtn.className = 'exam-tpl-btn';
    tplBtn.textContent = '📋 模板 ▾';
    row1.appendChild(tplBtn);
    CHIPS.filter(c => c.group === 'verdict').forEach(c => row1.appendChild(makeChip(textarea, c)));
    helper.appendChild(row1);

    // 第 2 行：数值类
    const row2 = makeRow('数值', 'vitals', textarea);
    helper.appendChild(row2);

    // 第 3 行：系统类
    const row3 = makeRow('系统', 'system', textarea);
    helper.appendChild(row3);

    textarea.parentNode.insertBefore(helper, textarea);

    // 模板下拉
    let dropdown = null;
    tplBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (dropdown) { dropdown.remove(); dropdown = null; return; }
      dropdown = document.createElement('div');
      dropdown.className = 'exam-tpl-dropdown';
      TEMPLATES.forEach(t => {
        const item = document.createElement('div');
        item.className = 'exam-tpl-item';
        item.innerHTML = '<b>' + t.name + '</b><small>' + t.desc + '</small>';
        item.addEventListener('click', function () {
          applyTemplate(textarea, t);
          dropdown.remove(); dropdown = null;
        });
        dropdown.appendChild(item);
      });
      // 定位
      const rect = tplBtn.getBoundingClientRect();
      dropdown.style.position = 'absolute';
      dropdown.style.left = (window.scrollX + rect.left) + 'px';
      dropdown.style.top = (window.scrollY + rect.bottom + 4) + 'px';
      document.body.appendChild(dropdown);
    });
    document.addEventListener('click', function () {
      if (dropdown) { dropdown.remove(); dropdown = null; }
    });
  }

  function makeRow(label, group, textarea) {
    const row = document.createElement('div');
    row.className = 'exam-helper-row';
    const tag = document.createElement('span');
    tag.className = 'exam-helper-tag';
    tag.textContent = label;
    row.appendChild(tag);
    CHIPS.filter(c => c.group === group).forEach(c => row.appendChild(makeChip(textarea, c)));
    return row;
  }

  function makeChip(textarea, c) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'exam-chip ' + c.group;
    chip.textContent = c.label;
    chip.title = '插入：' + c.snippet;
    chip.addEventListener('click', function (e) {
      e.preventDefault();
      insertAtCursor(textarea, c.snippet);
    });
    return chip;
  }

  function scan() {
    document.querySelectorAll('textarea[data-exam-helper="1"]').forEach(attach);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scan);
  } else {
    scan();
  }
  new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
})();

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

  // 结构化体格检查项：默认正常，异常才点选。生成结果仍写回原 physical_exam textarea。
  const STRUCTURED_GROUPS = [
    {
      key: "mental",
      title: "意识 / 行为",
      normal: "意识警觉，行为温顺/友善",
      options: [
        "沉郁", "嗜睡", "反应迟钝", "紧张/害怕", "顺从性差", "有攻击性", "疼痛反应明显"
      ],
    },
    {
      key: "posture",
      title: "身体姿势 / 步态",
      normal: "身体姿势正常，站立及行走未见明显异常",
      options: [
        "共济失调", "头倾斜", "弓背", "不能站立", "跛行", "关节活动受限"
      ],
    },
    {
      key: "ears",
      title: "耳检",
      normal: "可视耳道干净清洁，未见明显异味及大量分泌物",
      options: [
        "耳道红肿", "褐色分泌物", "黄色分泌物", "耳道异味", "抓耳/甩头", "耳道疼痛"
      ],
    },
    {
      key: "eyes",
      title: "眼睛",
      normal: "双眼清亮，未见大量分泌物",
      options: [
        "流泪", "眼分泌物增多", "结膜充血", "角膜混浊", "眼睑红肿", "畏光"
      ],
    },
    {
      key: "nose",
      title: "鼻部",
      normal: "鼻镜湿润，未见明显鼻分泌物",
      options: [
        "鼻镜干燥", "水样鼻涕", "脓性鼻涕", "鼻塞", "打喷嚏"
      ],
    },
    {
      key: "mm",
      title: "黏膜 / CRT",
      normal: "可视黏膜淡粉，CRT 约 1-2 秒",
      options: [
        "黏膜苍白", "黏膜发绀", "黏膜黄染", "黏膜潮红", "CRT 延长", "轻度脱水", "中度脱水", "重度脱水"
      ],
    },
    {
      key: "oral",
      title: "口腔 / 牙齿",
      normal: "口腔检查未见明显异常，未见明显口臭",
      options: [
        "牙结石", "牙龈红肿", "口臭", "口腔溃疡", "乳牙滞留", "牙齿松动", "轻度牙垢", "重度牙结石"
      ],
    },
    {
      key: "skin",
      title: "皮肤 / 被毛",
      normal: "皮肤被毛眼观未见明显异常",
      options: [
        "脱毛", "红斑", "丘疹", "结痂", "皮屑", "瘙痒", "抓挠痕", "潮湿性皮炎", "外寄生虫"
      ],
    },
    {
      key: "abdomen",
      title: "腹部触诊",
      normal: "腹部触诊柔软，未触及明显疼痛及包块",
      options: [
        "腹部紧张", "腹部压痛", "腹胀", "膀胱充盈", "可疑包块", "肠管积气", "便秘样触感"
      ],
    },
    {
      key: "auscultation",
      title: "听诊",
      normal: "心肺听诊未闻及明显异常，肠鸣音未见明显异常",
      options: [
        "心率过快", "心率过慢", "心律不齐", "心杂音", "肺音粗粝", "呼吸杂音", "肠鸣音增强", "肠鸣音减弱"
      ],
    },
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
.exam-expand-btn {
  font-size: .72rem; padding: 2px 9px; border-radius: 12px;
  border: 1px dashed #94a3b8; background: transparent; color: #64748b;
  cursor: pointer; user-select: none; transition: all .12s; margin-left: auto;
}
.exam-expand-btn:hover { border-color: #475569; color: #1e293b; background: #f1f5f9; }
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
.exam-structured {
  margin-top: 8px; padding: 12px; border: 1px solid #d8d4cc;
  background: #fdfcf8; color: #1a1a1a;
}
.exam-structured-head {
  display: flex; justify-content: space-between; gap: 12px; align-items: center;
  margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid #d8d4cc;
}
.exam-structured-title {
  font-family: Georgia, "Source Han Serif SC", "Noto Serif SC", serif;
  font-weight: 700; letter-spacing: 2px; font-size: 13px;
}
.exam-structured-note {
  font-family: Georgia, "Source Han Serif SC", "Noto Serif SC", serif;
  font-size: 11px; color: #6f6a61;
}
.exam-vitals-grid {
  display: grid; grid-template-columns: repeat(6, minmax(82px, 1fr)); gap: 8px;
  margin-bottom: 12px;
}
.exam-vital label {
  display: block; font-family: Georgia, "Source Han Serif SC", "Noto Serif SC", serif;
  font-size: 11px; color: #6f6a61; letter-spacing: 1px; margin-bottom: 3px;
}
.exam-vital input, .exam-vital select {
  width: 100%; box-sizing: border-box; height: 32px; border: 1px solid #d8d4cc;
  background: #fff; color: #1a1a1a; padding: 4px 7px; border-radius: 0;
  font-family: Georgia, "Source Han Serif SC", "Noto Serif SC", serif;
}
.exam-pe-group { padding: 10px 0; border-top: 1px solid #ece8df; }
.exam-pe-group:first-of-type { border-top: 0; }
.exam-pe-title {
  font-family: Georgia, "Source Han Serif SC", "Noto Serif SC", serif;
  font-weight: 700; letter-spacing: 1.5px; font-size: 12px; margin-bottom: 7px;
}
.exam-pe-options { display: flex; flex-wrap: wrap; gap: 6px; }
.exam-pe-option {
  border: 1px solid #d8d4cc; background: #fff; color: #4a4a4a;
  padding: 4px 9px; border-radius: 0; cursor: pointer;
  font-family: Georgia, "Source Han Serif SC", "Noto Serif SC", serif;
  font-size: 12px; letter-spacing: 1px; line-height: 1.5;
}
.exam-pe-option.normal { border-color: #9ab2a7; color: #1d4d3a; }
.exam-pe-option.checked {
  background: #1a1a1a; border-color: #1a1a1a; color: #fdfcf8;
}
.exam-pe-option.abnormal.checked {
  background: #7a2828; border-color: #7a2828; color: #fdfcf8;
}
.exam-generate-btn {
  font-size: .75rem; padding: 3px 11px; border-radius: 12px;
  border: 1px solid #1d4d3a; background: #1d4d3a; color: #fff;
  cursor: pointer; user-select: none; transition: all .12s;
}
.exam-normal-btn {
  font-size: .75rem; padding: 3px 11px; border-radius: 12px;
  border: 1px solid #6b4423; background: #fff; color: #6b4423;
  cursor: pointer; user-select: none; transition: all .12s;
}
@media (max-width: 768px) {
  .exam-helper-tag { display: none; }
  .exam-chip { font-size: .78rem; padding: 3px 10px; }
  .exam-vitals-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .exam-structured { padding: 10px; }
}
`;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  /* ─────────────────────────  逻辑  ───────────────────────── */
  function insertAtCursor(ta, text, opts) {
    opts = opts || {};
    const start = ta.selectionStart, end = ta.selectionEnd;
    const before = ta.value.substring(0, start);
    const after = ta.value.substring(end);
    let prefix = '';
    if (opts.newline) {
      // 系统类：插入前确保单独成行（前面是行首 / 已换行 / 空 都不加）
      if (before && !/\n$/.test(before)) prefix = '\n';
    } else if (before && !/[\s\n>]$/.test(before)) {
      // 默认：避免连写，前面非空+非空白 加空格
      prefix = ' ';
    }
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

    // 第 1 行：模板按钮 + verdict chips + 展开/收起按钮
    const row1 = document.createElement('div');
    row1.className = 'exam-helper-row';
    const tplBtn = document.createElement('button');
    tplBtn.type = 'button';
    tplBtn.className = 'exam-tpl-btn';
    tplBtn.textContent = '模板 ▾';
    row1.appendChild(tplBtn);
    const normalBtn = document.createElement('button');
    normalBtn.type = 'button';
    normalBtn.className = 'exam-normal-btn';
    normalBtn.textContent = '一键正常';
    normalBtn.title = '生成一段完整的正常体格检查描述';
    row1.appendChild(normalBtn);
    const structuredBtn = document.createElement('button');
    structuredBtn.type = 'button';
    structuredBtn.className = 'exam-generate-btn';
    structuredBtn.textContent = '体检表';
    structuredBtn.title = '展开结构化体格检查表';
    row1.appendChild(structuredBtn);
    const generateBtn = document.createElement('button');
    generateBtn.type = 'button';
    generateBtn.className = 'exam-generate-btn';
    generateBtn.textContent = '生成描述';
    generateBtn.title = '根据体检表生成文字';
    row1.appendChild(generateBtn);
    CHIPS.filter(c => c.group === 'verdict').forEach(c => row1.appendChild(makeChip(textarea, c)));

    const expandBtn = document.createElement('button');
    expandBtn.type = 'button';
    expandBtn.className = 'exam-expand-btn';
    expandBtn.textContent = '⇣ 更多';
    expandBtn.title = '展开数值 / 系统 检查项';
    row1.appendChild(expandBtn);
    helper.appendChild(row1);

    // 第 2 行 + 第 3 行：默认隐藏
    const row2 = makeRow('数值', 'vitals', textarea);
    const row3 = makeRow('系统', 'system', textarea);
    row2.style.display = 'none';
    row3.style.display = 'none';
    helper.appendChild(row2);
    helper.appendChild(row3);

    const structuredPanel = buildStructuredPanel(textarea);
    structuredPanel.style.display = 'none';
    helper.appendChild(structuredPanel);

    // 折叠 / 展开切换（本地存住偏好）
    let expanded = localStorage.getItem('exam_helper_expanded') === '1';
    function applyExpanded() {
      row2.style.display = expanded ? '' : 'none';
      row3.style.display = expanded ? '' : 'none';
      expandBtn.textContent = expanded ? '⇡ 收起' : '⇣ 更多';
    }
    applyExpanded();
    expandBtn.addEventListener('click', function (e) {
      e.preventDefault();
      expanded = !expanded;
      localStorage.setItem('exam_helper_expanded', expanded ? '1' : '0');
      applyExpanded();
    });

    structuredBtn.addEventListener('click', function (e) {
      e.preventDefault();
      const open = structuredPanel.style.display === 'none';
      structuredPanel.style.display = open ? '' : 'none';
      structuredBtn.textContent = open ? '收起表' : '体检表';
      if (open) {
        const firstControl = structuredPanel.querySelector('input,button');
        if (firstControl) firstControl.focus();
      }
    });
    normalBtn.addEventListener('click', function (e) {
      e.preventDefault();
      setStructuredNormal(structuredPanel);
      textarea.value = generateStructuredText(structuredPanel, { includeAllNormals: true });
      textarea.focus();
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    generateBtn.addEventListener('click', function (e) {
      e.preventDefault();
      const text = generateStructuredText(structuredPanel, { includeAllNormals: true });
      const current = (textarea.value || '').trim();
      if (current && !confirm('用生成的体格检查描述替换当前内容？点“取消”则追加到末尾。')) {
        textarea.value = current + '\n' + text;
      } else {
        textarea.value = text;
      }
      textarea.focus();
      textarea.selectionStart = textarea.selectionEnd = textarea.value.length;
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });

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
      // 系统类 chip：插入前自动换行（每项一行更易读）
      insertAtCursor(textarea, c.snippet, { newline: c.group === 'system' });
    });
    return chip;
  }

  function buildStructuredPanel(textarea) {
    const panel = document.createElement('div');
    panel.className = 'exam-structured';
    panel.innerHTML = `
      <div class="exam-structured-head">
        <div>
          <div class="exam-structured-title">临 床 体 格 检 查</div>
          <div class="exam-structured-note">默认正常，异常才点；生成后仍可直接修改文字。</div>
        </div>
        <button type="button" class="exam-normal-btn" data-pe-normal-all>全部正常</button>
      </div>
      <div class="exam-vitals-grid">
        ${makeVital("weight", "体重 kg", "如 3.2")}
        ${makeVital("temp", "体温 ℃", "如 38.6")}
        ${makeVital("hr", "心率 次/分", "如 148")}
        ${makeVital("rr", "呼吸 次/分", "如 32")}
        ${makeVital("bcs", "BCS /9", "如 5")}
        ${makeVital("crt", "CRT 秒", "如 1")}
        <div class="exam-vital">
          <label>黏膜颜色</label>
          <select data-pe-vital="mucosa">
            <option value="">未填</option>
            <option value="淡粉">淡粉</option>
            <option value="苍白">苍白</option>
            <option value="潮红">潮红</option>
            <option value="发绀">发绀</option>
            <option value="黄染">黄染</option>
          </select>
        </div>
        <div class="exam-vital">
          <label>脱水</label>
          <select data-pe-vital="dehydration">
            <option value="">未填</option>
            <option value="未见明显脱水">未见明显脱水</option>
            <option value="轻度脱水">轻度脱水</option>
            <option value="中度脱水">中度脱水</option>
            <option value="重度脱水">重度脱水</option>
          </select>
        </div>
      </div>
      <div data-pe-groups></div>
    `;
    const groupsBox = panel.querySelector('[data-pe-groups]');
    STRUCTURED_GROUPS.forEach(group => {
      const wrap = document.createElement('div');
      wrap.className = 'exam-pe-group';
      wrap.dataset.peGroup = group.key;
      const title = document.createElement('div');
      title.className = 'exam-pe-title';
      title.textContent = group.title;
      wrap.appendChild(title);
      const opts = document.createElement('div');
      opts.className = 'exam-pe-options';
      const normal = document.createElement('button');
      normal.type = 'button';
      normal.className = 'exam-pe-option normal checked';
      normal.dataset.peNormal = group.normal;
      normal.textContent = '正常';
      opts.appendChild(normal);
      group.options.forEach(text => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'exam-pe-option abnormal';
        b.dataset.peAbnormal = text;
        b.textContent = text;
        opts.appendChild(b);
      });
      wrap.appendChild(opts);
      groupsBox.appendChild(wrap);
    });
    panel.addEventListener('click', function (e) {
      const btn = e.target.closest('button');
      if (!btn) return;
      if (btn.hasAttribute('data-pe-normal-all')) {
        e.preventDefault();
        setStructuredNormal(panel);
        return;
      }
      if (btn.classList.contains('exam-pe-option')) {
        e.preventDefault();
        const groupEl = btn.closest('[data-pe-group]');
        if (!groupEl) return;
        if (btn.dataset.peNormal) {
          groupEl.querySelectorAll('.exam-pe-option').forEach(x => x.classList.remove('checked'));
          btn.classList.add('checked');
        } else {
          btn.classList.toggle('checked');
          const normal = groupEl.querySelector('[data-pe-normal]');
          const hasAbnormal = !!groupEl.querySelector('.exam-pe-option.abnormal.checked');
          if (normal) normal.classList.toggle('checked', !hasAbnormal);
        }
      }
    });
    return panel;
  }

  function makeVital(key, label, placeholder) {
    return `<div class="exam-vital"><label>${label}</label><input data-pe-vital="${key}" placeholder="${placeholder || ''}"/></div>`;
  }

  function setStructuredNormal(panel) {
    panel.querySelectorAll('[data-pe-group]').forEach(groupEl => {
      groupEl.querySelectorAll('.exam-pe-option').forEach(b => b.classList.remove('checked'));
      const normal = groupEl.querySelector('[data-pe-normal]');
      if (normal) normal.classList.add('checked');
    });
  }

  function vitalValue(panel, key) {
    const el = panel.querySelector(`[data-pe-vital="${key}"]`);
    return el ? (el.value || '').trim() : '';
  }

  function generateStructuredText(panel, opts) {
    opts = opts || {};
    const parts = [];
    const weight = vitalValue(panel, 'weight');
    const temp = vitalValue(panel, 'temp');
    const hr = vitalValue(panel, 'hr');
    const rr = vitalValue(panel, 'rr');
    const bcs = vitalValue(panel, 'bcs');
    const crt = vitalValue(panel, 'crt');
    const mucosa = vitalValue(panel, 'mucosa');
    const dehydration = vitalValue(panel, 'dehydration');

    const vitals = [];
    if (weight) vitals.push(`体重 ${weight}kg`);
    if (temp) vitals.push(`体温 ${temp}℃`);
    if (hr) vitals.push(`心率 ${hr} 次/分`);
    if (rr) vitals.push(`呼吸 ${rr} 次/分`);
    if (bcs) vitals.push(`BCS ${bcs}/9`);
    if (crt) vitals.push(`CRT 约 ${crt} 秒`);
    if (mucosa) vitals.push(`可视黏膜${mucosa}`);
    if (dehydration) vitals.push(dehydration);
    if (vitals.length) {
      parts.push(vitals.join('；') + '。');
    }

    const abnormalSentences = [];
    const normalSentences = [];
    panel.querySelectorAll('[data-pe-group]').forEach(groupEl => {
      const abnormal = Array.from(groupEl.querySelectorAll('[data-pe-abnormal].checked'))
        .map(b => b.dataset.peAbnormal)
        .filter(Boolean);
      if (abnormal.length) {
        const titleEl = groupEl.querySelector('.exam-pe-title');
        const title = (titleEl && titleEl.textContent) ? titleEl.textContent : '检查';
        abnormalSentences.push(`${title}：${abnormal.join('、')}。`);
      } else {
        const normalEl = groupEl.querySelector('[data-pe-normal]');
        const normal = normalEl ? (normalEl.dataset.peNormal || '') : '';
        if (normal) normalSentences.push(normal);
      }
    });

    if (abnormalSentences.length) {
      if (normalSentences.length) {
        parts.push(normalSentences.join('；') + '。');
      }
      parts.push(abnormalSentences.join('\n'));
    } else if (normalSentences.length) {
      parts.push(normalSentences.join('；') + '。');
    }

    return parts.join('\n').trim() || '体格检查未见明显异常。';
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

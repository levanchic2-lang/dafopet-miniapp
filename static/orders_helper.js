/*
 * 医嘱模板辅助：给 <textarea data-orders-helper="1"> 上方加模板下拉。
 * 6 个预制模板：绝育术后 / 洗牙术后 / 胃肠道 / 泌尿系统 / 过敏 / 呼吸道
 */
(function () {
  if (window.__ORDERS_HELPER_LOADED) return;
  window.__ORDERS_HELPER_LOADED = true;

  const TEMPLATES = [
    {
      key: 'spay',
      name: "绝育术后",
      desc: "公猫/母猫绝育后护理",
      text:
        "一、术后观察（24 小时内）：\n" +
        "1. 麻醉苏醒：观察精神状态，6 小时内逐渐清醒；如持续昏睡或反复呕吐请立即联系。\n" +
        "2. 进食水：苏醒后 4 小时可少量饮水，6-8 小时后少量进食软食。\n" +
        "3. 伤口护理：保持干燥不可舔咬，全程佩戴伊丽莎白圈直到拆线（或自溶线吸收）。\n\n" +
        "二、用药：\n" +
        "1. 抗生素：___ ___ x ___ 天\n" +
        "2. 止痛药：___ ___ x ___ 天\n\n" +
        "三、生活管理：\n" +
        "1. 限制活动 7-10 天，禁跳跃和剧烈运动。\n" +
        "2. 单独安静环境休养，避免与其他宠物追逐。\n" +
        "3. 排尿排便：母猫 24 小时内可能无尿，48 小时仍无尿请就诊。\n" +
        "4. 14 天内不可洗澡。\n\n" +
        "四、复诊：\n" +
        "术后 7-10 天来院拆线（自溶线无需）。\n\n" +
        "⚠️ 异常请立即就诊：持续呕吐 / 拒食超 24 小时 / 伤口红肿出血异味 / 精神萎靡呼吸异常",
    },
    {
      key: 'dental',
      name: "洗牙 / 拔牙术后",
      desc: "牙周治疗后护理",
      text:
        "一、麻醉苏醒：\n" +
        "6 小时内可能嗜睡、走路不稳，属正常现象。\n\n" +
        "二、进食：\n" +
        "1. 苏醒 4 小时后少量饮水。\n" +
        "2. 24 小时内吃软食或泡软的干粮。\n" +
        "3. 48 小时内避免硬食、骨头、牙咬胶。\n" +
        "4. 拔牙创口位置 2 周内禁咬硬物。\n\n" +
        "三、口腔护理：\n" +
        "1. 当天不漱口、不刷牙。\n" +
        "2. 第 2 天起每日用宠物专用洁齿水或纱布擦拭。\n" +
        "3. 1 周后恢复刷牙习惯。\n\n" +
        "四、用药：\n" +
        "1. 抗生素 / 消炎药：___ x ___ 天\n" +
        "2. 止痛药（拔牙者）：___ x ___ 天\n\n" +
        "五、复诊：\n" +
        "拔牙 / 牙周治疗者 1 个月内回访检查愈合情况。",
    },
    {
      key: 'gi',
      name: "胃肠道疾病",
      desc: "拉稀 / 呕吐 / 急性肠胃炎",
      text:
        "一、饮食管理：\n" +
        "1. 禁食 12-24 小时（医生确认后），逐步少量多餐恢复。\n" +
        "2. 头 3 天：胃肠处方粮、无盐鸡胸肉粥、米汤。\n" +
        "3. 5-7 天后逐渐过渡回原粮。\n" +
        "4. 禁忌：油腻、生冷、含乳糖、人类零食。\n\n" +
        "二、补液（脱水患者）：\n" +
        "口服补液盐少量多次，或来院皮下补液。\n\n" +
        "三、用药：\n" +
        "1. 止吐：___ ___ x ___ 天\n" +
        "2. 益生菌：___ 餐前 30 分钟，2 次/天 x 7-14 天\n" +
        "3. 消炎 / 抗生素：___ ___ x ___ 天\n" +
        "4. 胃黏膜保护剂：___\n\n" +
        "四、观察指标：\n" +
        "1. 排便：颜色 / 形状 / 是否带血 / 是否拉稀\n" +
        "2. 食欲：每餐能吃多少 / 是否有呕吐\n" +
        "3. 精神状态\n\n" +
        "五、复诊：\n" +
        "2-3 天后回访（电话或来院）；症状无改善或加重立即就诊。",
    },
    {
      key: 'urinary',
      name: "泌尿系统",
      desc: "膀胱炎 / 结石 / 尿闭",
      text:
        "一、饮食：\n" +
        "1. 泌尿处方粮（U/D 或专用），3-6 个月起效。\n" +
        "2. 鼓励多饮水：换湿粮、加饮水泉、增加水盆数量。\n" +
        "3. 禁忌：高镁高钙食物、零食。\n\n" +
        "二、生活：\n" +
        "1. 保证排尿环境清洁，及时清理猫砂盆（多猫家庭：N+1 个）。\n" +
        "2. 减少应激：固定作息、避免环境突变。\n" +
        "3. 体重管理（肥胖是高危因素）。\n\n" +
        "三、用药：\n" +
        "1. 抗生素（细菌感染者）：___ x ___ 天\n" +
        "2. 止血药（血尿者）：___ x ___ 天\n" +
        "3. 解痉 / 止痛：___\n\n" +
        "四、监测：\n" +
        "1. 排尿次数和量\n" +
        "2. 尿色（透明 / 淡黄 / 红 / 浑浊）\n" +
        "3. 排尿姿势、是否痛叫\n\n" +
        "⚠️ 公猫尤其注意：完全无尿超过 12 小时 → 立即急诊！可能尿道梗阻致命。\n\n" +
        "五、复诊：\n" +
        "1-2 周回院复查尿常规 + 尿比重。",
    },
    {
      key: 'allergy',
      name: "过敏性疾病",
      desc: "皮肤过敏 / 食物过敏",
      text:
        "一、过敏源排查：\n" +
        "1. 食物过敏：换处方粮（水解蛋白 / 单一蛋白源）8-12 周。\n" +
        "2. 环境过敏：清洁床品、避免接触花粉 / 尘螨。\n" +
        "3. 跳蚤过敏：每月驱虫 + 环境喷雾。\n\n" +
        "二、皮肤护理：\n" +
        "1. 药浴每周 1-2 次（医生指定药浴液）。\n" +
        "2. 保持患处干燥通风。\n" +
        "3. 戴伊丽莎白圈防止舔咬抓挠。\n\n" +
        "三、用药：\n" +
        "1. 抗组胺 / 止痒：___ x ___ 天\n" +
        "2. 短期激素：___（严格按医嘱递减，不可自行停药）\n" +
        "3. 外用药膏：___\n" +
        "4. 局部消毒 / 喷雾：___\n\n" +
        "四、长期管理：\n" +
        "1. 定期复查皮肤状况\n" +
        "2. 不可自行停药浴 / 换粮\n" +
        "3. 记录过敏发作时间和触发因素\n\n" +
        "五、复诊：\n" +
        "2 周后回院评估治疗反应。",
    },
    {
      key: 'respiratory',
      name: "呼吸道疾病",
      desc: "猫鼻支 / 上呼吸道感染",
      text:
        "一、环境管理：\n" +
        "1. 保暖：室温 22-26℃，避免风口。\n" +
        "2. 加湿：使用加湿器，湿度 50-60%。\n" +
        "3. 通风：每天开窗 10-15 分钟，避免穿堂风。\n" +
        "4. 隔离：避免接触其他动物（传染性高）。\n\n" +
        "二、饮食：\n" +
        "1. 软食、温食，少量多餐。\n" +
        "2. 多饮水，可加少许鸡汤吸引。\n" +
        "3. 食欲极差时：营养膏 / 强制喂食。\n\n" +
        "三、用药：\n" +
        "1. 抗生素：___ x ___ 天\n" +
        "2. 化痰：___ x ___ 天\n" +
        "3. 退烧 / 止咳：___\n" +
        "4. 雾化：生理盐水 + ___ ，每日 2 次\n\n" +
        "四、观察指标：\n" +
        "1. 呼吸频率：静息 < 40 次/分\n" +
        "2. 呼吸方式：腹式 vs 胸式\n" +
        "3. 黏膜颜色：粉红正常\n" +
        "4. 体温、食欲、精神\n\n" +
        "⚠️ 急诊征兆：呼吸困难 / 张口呼吸 / 舌头发紫 → 立即就医\n\n" +
        "五、复诊：\n" +
        "3-5 天回访，体温正常且咳嗽明显减轻可逐步停药。",
    },
  ];

  /* ─────  样式（复用 exam-helper 的 token，避免重复定义）  ───── */
  const style = document.createElement('style');
  style.textContent = `
.orders-helper { margin: 0 0 .5rem; padding: .5rem .65rem; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; }
.orders-tpl-btn { font-size: .82rem; padding: 5px 14px; border-radius: 14px; border: 1px solid #0e7490; background: #0e7490; color: #fff; cursor: pointer; user-select: none; transition: all .12s; }
.orders-tpl-btn:hover { background: #155e75; }
.orders-helper-hint { font-size: .72rem; color: #64748b; }
.orders-tpl-dropdown { position: absolute; z-index: 100; margin-top: 4px; background: #fff; border: 1px solid #cbd5e1; border-radius: 8px; box-shadow: 0 6px 18px rgba(0,0,0,.12); min-width: 260px; padding: .35rem 0; max-height: 70vh; overflow-y: auto; }
.orders-tpl-item { padding: .55rem .85rem; cursor: pointer; font-size: .85rem; border-bottom: 1px solid #f1f5f9; }
.orders-tpl-item:last-child { border-bottom: none; }
.orders-tpl-item:hover { background: #ecfeff; }
.orders-tpl-item b { display: block; font-size: .9rem; color: #0e7490; }
.orders-tpl-item small { color: #64748b; font-size: .74rem; }
`;
  document.head.appendChild(style);

  function applyTemplate(ta, tpl) {
    const cur = (ta.value || '').trim();
    if (cur) {
      const choice = confirm('已有医嘱内容。\n点「确定」追加到末尾，点「取消」清空后插入。');
      ta.value = choice ? cur + '\n\n' + tpl.text : tpl.text;
    } else {
      ta.value = tpl.text;
    }
    ta.selectionStart = ta.selectionEnd = ta.value.length;
    ta.focus();
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function attach(textarea) {
    if (textarea.__ordersHelperAttached) return;
    textarea.__ordersHelperAttached = true;

    const helper = document.createElement('div');
    helper.className = 'orders-helper';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'orders-tpl-btn';
    btn.textContent = '📋 医嘱模板 ▾';
    helper.appendChild(btn);

    const hint = document.createElement('span');
    hint.className = 'orders-helper-hint';
    hint.textContent = '常见 6 类术后/疾病护理预制模板';
    helper.appendChild(hint);

    textarea.parentNode.insertBefore(helper, textarea);

    let dropdown = null;
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (dropdown) { dropdown.remove(); dropdown = null; return; }
      dropdown = document.createElement('div');
      dropdown.className = 'orders-tpl-dropdown';
      TEMPLATES.forEach(t => {
        const item = document.createElement('div');
        item.className = 'orders-tpl-item';
        item.innerHTML = '<b>' + t.name + '</b><small>' + t.desc + '</small>';
        item.addEventListener('click', function () {
          applyTemplate(textarea, t);
          dropdown.remove(); dropdown = null;
        });
        dropdown.appendChild(item);
      });
      const rect = btn.getBoundingClientRect();
      dropdown.style.position = 'absolute';
      dropdown.style.left = (window.scrollX + rect.left) + 'px';
      dropdown.style.top = (window.scrollY + rect.bottom + 4) + 'px';
      document.body.appendChild(dropdown);
    });
    document.addEventListener('click', function () {
      if (dropdown) { dropdown.remove(); dropdown = null; }
    });
  }

  function scan() {
    document.querySelectorAll('textarea[data-orders-helper="1"]').forEach(attach);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scan);
  } else {
    scan();
  }
  new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
})();

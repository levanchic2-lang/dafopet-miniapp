/*
 * 通用语音输入：自动给页面里所有 <textarea data-voice="1"> 加一个 🎤 按钮。
 *
 * 用 Web Speech API（webkitSpeechRecognition / SpeechRecognition），
 * 浏览器支持：Chrome / Edge / 企业微信 PC 内置浏览器 / Safari 16+。
 * 不支持的浏览器（旧 Safari / 部分移动端）按钮会显示「点击切到语音键盘」提示，
 * 让用户使用系统输入法的语音按键。
 */
(function () {
  if (window.__VOICE_INPUT_LOADED) return;
  window.__VOICE_INPUT_LOADED = true;

  // 注入按钮样式
  const style = document.createElement('style');
  style.textContent = `
    .voice-input-wrap { position: relative; display: block; }
    .voice-input-btn {
      position: absolute; top: 8px; right: 10px; z-index: 5;
      display: inline-flex; align-items: center; gap: 4px;
      font-size: 12px; padding: 3px 9px; border-radius: 14px;
      border: 1px solid #d1d5db; background: rgba(255,255,255,.92);
      color: #374151; cursor: pointer; user-select: none;
      transition: all .15s; box-shadow: 0 1px 3px rgba(0,0,0,.12);
    }
    .voice-input-btn:hover { background: #f3f4f6; border-color: #9ca3af; }
    .voice-input-btn.recording {
      background: #dc2626; color: #fff; border-color: #dc2626;
      animation: voice-pulse 1.2s ease-in-out infinite;
    }
    @keyframes voice-pulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(220,38,38,.5); }
      50% { box-shadow: 0 0 0 6px rgba(220,38,38,0); }
    }
    .voice-input-btn.disabled { opacity: .55; cursor: not-allowed; }
    /* 手机端：按钮缩小成纯图标，节省横向空间 */
    @media (max-width: 768px) {
      .voice-input-btn { padding: 4px 7px; font-size: 13px; border-radius: 50%; }
      .voice-input-btn .voice-input-label { display: none; }
    }
  `;
  document.head.appendChild(style);

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const supported = !!SR;

  function attach(textarea) {
    if (textarea.__voiceAttached) return;
    textarea.__voiceAttached = true;

    // 确保父级有 position:relative（不破坏现有布局，必要时用 wrap div）
    const parent = textarea.parentElement;
    // 始终用 wrap 包住 textarea，保证按钮相对 textarea 定位（不会跑到面板顶部压住工具条）
    const wrap = document.createElement('div');
    wrap.className = 'voice-input-wrap';
    parent.insertBefore(wrap, textarea);
    wrap.appendChild(textarea);
    const host = wrap;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'voice-input-btn';
    btn.title = supported ? '点击开始语音输入（再点结束）' : '您的浏览器不支持语音 API，请用键盘语音按键';
    btn.innerHTML = '🎤 <span class="voice-input-label">语音</span>';
    if (!supported) btn.classList.add('disabled');
    host.appendChild(btn);

    // 给 textarea 留出按钮的空间（避免文字钻到按钮下面）
    const isMobile = window.matchMedia('(max-width: 768px)').matches;
    const need = isMobile ? 36 : 72;
    const cur = parseFloat(getComputedStyle(textarea).paddingRight) || 0;
    if (cur < need) {
      textarea.style.paddingRight = need + 'px';
    }

    if (!supported) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        alert('当前浏览器不支持语音识别。\n建议使用 Chrome / Edge / 企业微信 PC 客户端，或者在手机键盘上长按空格切换到语音输入法。');
      });
      return;
    }

    let recognition = null;
    let isRecording = false;
    let baseValue = '';      // 录音开始时的内容（避免临时识别覆盖已有）
    let finalAddon = '';     // 已确认的转写（累加）

    function setBtnRecording(on) {
      isRecording = on;
      btn.classList.toggle('recording', on);
      btn.innerHTML = on ? '⏹<span class="voice-input-label"> 结束</span>' : '🎤<span class="voice-input-label"> 语音</span>';
    }

    function start() {
      try {
        recognition = new SR();
      } catch (e) {
        alert('启动语音识别失败：' + e.message);
        return;
      }
      recognition.lang = 'zh-CN';
      recognition.continuous = true;
      recognition.interimResults = true;
      baseValue = (textarea.value || '').replace(/\s+$/, '');
      finalAddon = '';

      recognition.onresult = function (event) {
        let interim = '';
        for (let i = event.resultIndex; i < event.results.length; i++) {
          const res = event.results[i];
          if (res.isFinal) {
            finalAddon += res[0].transcript;
          } else {
            interim += res[0].transcript;
          }
        }
        const sep = baseValue && !/[\s\n。！？.]$/.test(baseValue) ? '，' : '';
        textarea.value = baseValue + (baseValue ? sep : '') + finalAddon + interim;
        // 触发 input 事件让自动保存等监听器接收变化
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
      };
      recognition.onerror = function (event) {
        console.warn('[voice]', event.error);
        if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
          alert('麦克风权限被拒绝，请在浏览器地址栏左侧设置中允许使用麦克风');
        } else if (event.error === 'no-speech') {
          // 静默 — 没说话很正常
        } else if (event.error === 'network') {
          alert('语音识别需要联网（连不上 Google 语音服务时也会报这个错）');
        }
        setBtnRecording(false);
      };
      recognition.onend = function () {
        setBtnRecording(false);
        recognition = null;
      };

      try {
        recognition.start();
        setBtnRecording(true);
        textarea.focus();
      } catch (e) {
        alert('启动失败：' + e.message);
      }
    }

    function stop() {
      if (recognition) {
        try { recognition.stop(); } catch (e) {}
      }
      setBtnRecording(false);
    }

    btn.addEventListener('click', function (e) {
      e.preventDefault();
      if (isRecording) stop(); else start();
    });
  }

  function scan() {
    document.querySelectorAll('textarea[data-voice="1"]').forEach(attach);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scan);
  } else {
    scan();
  }
  // 监听 DOM 后续变动（部分页面动态注入 textarea）
  const mo = new MutationObserver(scan);
  mo.observe(document.body, { childList: true, subtree: true });
})();

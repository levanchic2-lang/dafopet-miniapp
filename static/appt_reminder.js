/* 预约 15 分钟提醒（员工端）。
 * 桌面 + 手机 PWA 通用：每 60s 轮询 /api/appointments/upcoming-reminders，
 * 把即将开始的预约渲染成右上角卡片（手机为顶部通栏），并尝试弹一次浏览器通知 + 提示音。
 * 只在页面打开时生效（不依赖推送基建）。
 */
(function () {
  if (window.__apptReminderInit) return;
  window.__apptReminderInit = true;

  var POLL_MS = 60000;
  var notified = {};   // id -> 已弹过浏览器通知/提示音（本次会话）
  var CAL_URL = location.pathname.indexOf('/m') === 0 ? '/m/calendar' : '/admin/calendar';

  // ── 样式 ──
  var css = ''
    + '#apptRemBox{position:fixed;top:12px;right:12px;z-index:99999;display:flex;flex-direction:column;gap:8px;max-width:340px;}'
    + '@media(max-width:560px){#apptRemBox{left:8px;right:8px;top:8px;max-width:none;}}'
    + '.apptRem{background:#fdfcf8;border:0.5px solid #6b4423;border-left:4px solid #6b4423;'
    + 'box-shadow:0 6px 24px rgba(0,0,0,.16);padding:10px 12px;font-family:Georgia,"Source Han Serif SC",serif;color:#1a1a1a;}'
    + '.apptRem .hd{display:flex;align-items:baseline;justify-content:space-between;gap:8px;}'
    + '.apptRem .t{font-weight:700;font-size:13px;letter-spacing:1px;color:#6b4423;}'
    + '.apptRem .x{cursor:pointer;color:#8a8a8a;font-size:18px;line-height:1;border:none;background:none;padding:0 2px;}'
    + '.apptRem .x:hover{color:#1a1a1a;}'
    + '.apptRem a.bd{display:block;text-decoration:none;color:#1a1a1a;margin-top:4px;}'
    + '.apptRem .cat{display:inline-block;border:0.5px solid #6b4423;color:#6b4423;font-size:10px;padding:0 6px;margin-right:6px;}'
    + '.apptRem .svc{font-weight:700;font-size:13px;}'
    + '.apptRem .sub{font-size:11px;color:#4a4a4a;margin-top:3px;font-style:italic;}'
    + '.apptRem .mono{font-variant-numeric:tabular-nums;}';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  var box = document.createElement('div'); box.id = 'apptRemBox';
  function ensureBox() { if (!box.parentNode) document.body.appendChild(box); }

  function dKey(id) {
    var d = new Date(); // 本地=北京（设备时区），按日去重
    return 'apptRemDismiss:' + id + ':' + d.getFullYear() + (d.getMonth() + 1) + d.getDate();
  }
  function isDismissed(id) { try { return localStorage.getItem(dKey(id)) === '1'; } catch (e) { return false; } }
  function dismiss(id) { try { localStorage.setItem(dKey(id), '1'); } catch (e) {} var el = document.getElementById('apptRem-' + id); if (el) el.remove(); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function beep() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      var ctx = new Ctx();
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.type = 'sine'; o.frequency.value = 880;
      g.gain.value = 0.06;
      o.connect(g); g.connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + 0.18);
      o.onended = function () { try { ctx.close(); } catch (e) {} };
    } catch (e) {}
  }

  function browserNotify(r) {
    try {
      if (!('Notification' in window) || Notification.permission !== 'granted') return;
      var when = r.minutes <= 0 ? '已到点' : ('还有 ' + r.minutes + ' 分钟');
      new Notification('预约提醒 · ' + when, {
        body: r.time + ' ' + (r.category || '') + ' ' + (r.service || '') + '\n' + (r.customer || '') + (r.pet ? ' · ' + r.pet : ''),
        tag: 'appt-' + r.id, renotify: false,
      });
    } catch (e) {}
  }

  function render(list) {
    ensureBox();
    var keep = {};
    list.forEach(function (r) {
      if (isDismissed(r.id)) return;
      keep[r.id] = true;
      var when = r.minutes <= 0 ? '已到点' : ('还有 ' + r.minutes + ' 分钟');
      var el = document.getElementById('apptRem-' + r.id);
      var html = ''
        + '<div class="hd"><span class="t mono">⏰ ' + esc(when) + '</span>'
        + '<button class="x" title="知道了" data-id="' + r.id + '">×</button></div>'
        + '<a class="bd" href="' + CAL_URL + '">'
        + '<div><span class="cat">' + esc(r.category || '预约') + '</span>'
        + '<span class="svc">' + esc(r.service || '—') + '</span></div>'
        + '<div class="sub"><span class="mono">' + esc(r.time) + '</span> · '
        + esc(r.customer || '—') + (r.pet ? ' · ' + esc(r.pet) : '')
        + (r.store ? ' · ' + esc(r.store) : '') + '</div></a>';
      if (!el) {
        el = document.createElement('div'); el.className = 'apptRem'; el.id = 'apptRem-' + r.id;
        box.appendChild(el);
      }
      el.innerHTML = html;
      el.querySelector('.x').addEventListener('click', function () { dismiss(r.id); });
      if (!notified[r.id]) { notified[r.id] = true; browserNotify(r); beep(); }
    });
    // 移除已不在窗口内（已开始很久/被取消）的卡片
    Array.prototype.forEach.call(box.querySelectorAll('.apptRem'), function (el) {
      var id = el.id.replace('apptRem-', '');
      if (!keep[id]) el.remove();
    });
  }

  function poll() {
    fetch('/api/appointments/upcoming-reminders', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : { reminders: [] }; })
      .then(function (d) { render((d && d.reminders) || []); })
      .catch(function () {});
  }

  // 首次尝试申请通知权限（被拒也不影响页内卡片）
  function askPerm() {
    try {
      if ('Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission().catch(function () {});
      }
    } catch (e) {}
  }

  document.addEventListener('click', function once() { askPerm(); document.removeEventListener('click', once); }, { once: true });
  poll();
  setInterval(poll, POLL_MS);
})();

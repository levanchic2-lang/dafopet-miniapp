(function () {
  'use strict';

  const TOOL_META = {
    arrow: { points: 2, hint: '依次点击箭头起点和异常位置' },
    length: { points: 2, hint: '依次点击测量起点和终点' },
    angle: { points: 3, hint: '依次点击角的两端和顶点：第一端 → 顶点 → 第二端' },
    rect: { points: 2, hint: '点击异常区域的两个对角' },
    ellipse: { points: 2, hint: '点击椭圆外接框的两个对角' },
    freehand: { points: 0, hint: '按住鼠标沿异常边缘描画，松开完成轮廓' },
    text: { points: 1, hint: '点击标注位置，然后输入文字' },
    calibrate: { points: 2, hint: '在影像标尺上点击已知距离的两端' },
    vhs: { points: 6, hint: 'VHS：心长轴两点 → 心短轴两点 → T4-T5 单椎体参考长度两点' },
    vlas: { points: 4, hint: 'VLAS：左房测量线两点 → T4-T5 单椎体参考长度两点' },
    norberg: { points: 3, hint: 'Norberg角：对侧股骨头中心 → 待测侧股骨头中心 → 该侧髋臼前缘' },
    coverage: { points: 4, hint: '覆盖率：被髋臼覆盖宽度两点 → 股骨头直径两点' },
    tpa: { points: 4, hint: 'TPA：胫骨平台两点 → 胫骨机械轴两点' }
  };

  function distance(a, b, study) {
    const dx = b.x - a.x, dy = b.y - a.y;
    const sx = Number(study.spacing_x_mm), sy = Number(study.spacing_y_mm);
    const cal = Number(study.calibrated_mm_per_px);
    if (sx > 0 && sy > 0) return { value: Math.hypot(dx * sx, dy * sy), unit: 'mm' };
    if (cal > 0) return { value: Math.hypot(dx, dy) * cal, unit: 'mm' };
    return { value: Math.hypot(dx, dy), unit: 'px' };
  }

  function rawDistance(a, b) { return Math.hypot(b.x - a.x, b.y - a.y); }
  function angleAt(a, vertex, c) {
    const u = { x: a.x - vertex.x, y: a.y - vertex.y };
    const v = { x: c.x - vertex.x, y: c.y - vertex.y };
    const denom = Math.hypot(u.x, u.y) * Math.hypot(v.x, v.y);
    if (!denom) return 0;
    return Math.acos(Math.max(-1, Math.min(1, (u.x * v.x + u.y * v.y) / denom))) * 180 / Math.PI;
  }
  function lineAngle(a, b, c, d) {
    const u = { x: b.x - a.x, y: b.y - a.y }, v = { x: d.x - c.x, y: d.y - c.y };
    const denom = Math.hypot(u.x, u.y) * Math.hypot(v.x, v.y);
    if (!denom) return 0;
    let value = Math.acos(Math.max(-1, Math.min(1, Math.abs(u.x * v.x + u.y * v.y) / denom))) * 180 / Math.PI;
    return Math.min(value, 180 - value);
  }
  function fmt(value, digits) { return Number(value).toFixed(digits == null ? 1 : digits); }

  class XrayWorkbench {
    constructor(options) {
      this.options = options;
      this.studies = Array.isArray(options.studies) ? options.studies : [];
      this.activeIndex = this.studies.length ? 0 : -1;
      this.tool = 'arrow';
      this.pending = [];
      this.color = '#d53a32';
      this.freehandDrawing = false;
      this.image = new Image();
      this.canvas = document.getElementById(options.canvasId);
      this.ctx = this.canvas.getContext('2d');
      this.stage = document.getElementById(options.stageId);
      this.thumbWrap = document.getElementById(options.thumbId);
      this.status = document.getElementById(options.statusId);
      this.fileInput = document.getElementById(options.fileId);
      this.bind();
      this.renderThumbs();
      if (this.activeIndex >= 0) this.loadActive(); else this.renderEmpty();
      this.setTool('arrow');
    }

    bind() {
      document.querySelectorAll('[data-xr-tool]').forEach(btn => btn.addEventListener('click', () => this.setTool(btn.dataset.xrTool)));
      document.querySelectorAll('[data-xr-color]').forEach(btn => btn.addEventListener('click', () => {
        this.color = btn.dataset.xrColor;
        document.querySelectorAll('[data-xr-color]').forEach(x => x.classList.toggle('active', x === btn));
      }));
      document.getElementById(this.options.undoId).addEventListener('click', () => this.undo());
      document.getElementById(this.options.removeId).addEventListener('click', () => this.removeActive());
      document.getElementById(this.options.zoomId).addEventListener('input', e => {
        const z = Number(e.target.value || 100);
        this.canvas.style.width = z + '%';
      });
      this.canvas.addEventListener('pointerdown', e => {
        if (this.tool === 'freehand') {
          e.preventDefault(); this.freehandDrawing = true; this.pending = [this.pointFromEvent(e)];
          this.canvas.setPointerCapture(e.pointerId); this.draw();
        } else this.onPointer(e);
      });
      this.canvas.addEventListener('pointermove', e => {
        if (!this.freehandDrawing || this.tool !== 'freehand') return;
        e.preventDefault(); this.pending.push(this.pointFromEvent(e)); this.draw();
      });
      const finishFreehand = e => {
        if (!this.freehandDrawing) return;
        this.freehandDrawing = false;
        const study = this.active();
        if (study && this.pending.length > 2) {
          study.annotations = Array.isArray(study.annotations) ? study.annotations : [];
          study.annotations.push({ id: Date.now().toString(36), kind: 'freehand', points: this.pending.slice(), color: this.color, label: '' });
        }
        this.pending = []; this.draw();
      };
      this.canvas.addEventListener('pointerup', finishFreehand);
      this.canvas.addEventListener('pointercancel', finishFreehand);
      this.fileInput.addEventListener('change', () => this.uploadFiles(this.fileInput.files));
    }

    setTool(name) {
      if (!TOOL_META[name]) return;
      this.tool = name;
      this.pending = [];
      document.querySelectorAll('[data-xr-tool]').forEach(btn => btn.classList.toggle('active', btn.dataset.xrTool === name));
      this.status.textContent = TOOL_META[name].hint;
      this.canvas.style.cursor = 'crosshair';
      this.draw();
    }

    active() { return this.activeIndex >= 0 ? this.studies[this.activeIndex] : null; }
    pointFromEvent(e) {
      const r = this.canvas.getBoundingClientRect();
      return { x: (e.clientX - r.left) * this.canvas.width / r.width, y: (e.clientY - r.top) * this.canvas.height / r.height };
    }

    onPointer(e) {
      const study = this.active();
      if (!study || !this.image.complete) return;
      e.preventDefault();
      this.pending.push(this.pointFromEvent(e));
      const meta = TOOL_META[this.tool];
      if (this.pending.length < meta.points) {
        this.status.textContent = `${meta.hint}（已选 ${this.pending.length}/${meta.points} 点）`;
        this.draw();
        return;
      }
      this.finishTool(study);
      this.pending = [];
      this.status.textContent = meta.hint;
      this.draw();
    }

    finishTool(study) {
      const p = this.pending.slice();
      const ann = { id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6), kind: this.tool, points: p, color: this.color, label: '' };
      if (this.tool === 'text') {
        const value = window.prompt('标注文字：', '') || '';
        if (!value.trim()) return;
        ann.label = value.trim().slice(0, 80);
      } else if (this.tool === 'length') {
        const d = distance(p[0], p[1], study);
        ann.label = `${fmt(d.value)} ${d.unit}${d.unit === 'px' ? '（未校准）' : ''}`;
        this.emitMeasurement('长度', fmt(d.value), d.unit);
      } else if (this.tool === 'angle') {
        const value = angleAt(p[0], p[1], p[2]);
        ann.label = `${fmt(value)}°`;
        this.emitMeasurement('角度', fmt(value), '°');
      } else if (this.tool === 'calibrate') {
        const known = Number(window.prompt('请输入这段标尺的真实长度（mm）：', '10'));
        const px = rawDistance(p[0], p[1]);
        if (!(known > 0) || !(px > 0)) return;
        study.calibrated_mm_per_px = known / px;
        ann.label = `校准 ${fmt(known)} mm`;
      } else if (this.tool === 'vhs') {
        const ref = rawDistance(p[4], p[5]);
        if (!ref) return;
        const value = (rawDistance(p[0], p[1]) + rawDistance(p[2], p[3])) / ref;
        ann.label = `VHS ${fmt(value)} v（医师复核）`;
        this.emitMeasurement('VHS', fmt(value), 'v');
      } else if (this.tool === 'vlas') {
        const ref = rawDistance(p[2], p[3]);
        if (!ref) return;
        const value = rawDistance(p[0], p[1]) / ref;
        ann.label = `VLAS ${fmt(value)} v（医师复核）`;
        this.emitMeasurement('VLAS', fmt(value), 'v');
      } else if (this.tool === 'norberg') {
        const value = angleAt(p[0], p[1], p[2]);
        ann.label = `Norberg角 ${fmt(value)}°`;
        this.emitMeasurement('Norberg角', fmt(value), '°');
      } else if (this.tool === 'coverage') {
        const diameter = rawDistance(p[2], p[3]);
        if (!diameter) return;
        const value = rawDistance(p[0], p[1]) / diameter * 100;
        ann.label = `股骨头覆盖率 ${fmt(value)}%`;
        this.emitMeasurement('股骨头覆盖率', fmt(value), '%');
      } else if (this.tool === 'tpa') {
        const axisAngle = lineAngle(p[0], p[1], p[2], p[3]);
        const value = Math.abs(90 - axisAngle);
        ann.label = `TPA ${fmt(value)}°`;
        this.emitMeasurement('TPA', fmt(value), '°');
      }
      study.annotations = Array.isArray(study.annotations) ? study.annotations : [];
      study.annotations.push(ann);
    }

    emitMeasurement(name, value, unit) {
      if (typeof this.options.onMeasurement === 'function') this.options.onMeasurement(name, value, unit);
    }

    undo() {
      const study = this.active();
      if (!study || !Array.isArray(study.annotations) || !study.annotations.length) return;
      study.annotations.pop();
      this.pending = [];
      this.draw();
    }

    removeActive() {
      if (this.activeIndex < 0 || !window.confirm('从本次报告移除当前影像？')) return;
      this.studies.splice(this.activeIndex, 1);
      this.activeIndex = this.studies.length ? Math.min(this.activeIndex, this.studies.length - 1) : -1;
      this.renderThumbs();
      if (this.activeIndex >= 0) this.loadActive(); else this.renderEmpty();
    }

    renderEmpty() {
      this.canvas.width = 1000; this.canvas.height = 620;
      this.ctx.fillStyle = '#151515'; this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
      this.ctx.fillStyle = '#aaa'; this.ctx.font = '28px serif'; this.ctx.textAlign = 'center';
      this.ctx.fillText('上传 DICOM 或 X 光图片后开始阅片', 500, 300);
      this.status.textContent = '尚未载入影像';
    }

    loadActive() {
      const study = this.active();
      if (!study) return this.renderEmpty();
      this.image = new Image();
      this.image.onload = () => {
        this.canvas.width = this.image.naturalWidth;
        this.canvas.height = this.image.naturalHeight;
        this.canvas.style.width = document.getElementById(this.options.zoomId).value + '%';
        this.draw();
        const calibrated = (Number(study.spacing_x_mm) > 0 && Number(study.spacing_y_mm) > 0) || Number(study.calibrated_mm_per_px) > 0;
        this.status.textContent = `${study.filename} · ${study.width}×${study.height} · ${calibrated ? '已具备尺寸标定' : '无尺寸标定，长度暂以像素显示'}`;
      };
      this.image.onerror = () => { this.status.textContent = '影像预览载入失败'; };
      this.image.src = '/uploads/' + study.preview_path;
      this.renderThumbs();
    }

    draw() {
      if (!this.ctx) return;
      const study = this.active();
      if (!study || !this.image.complete || !this.image.naturalWidth) return;
      this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
      this.ctx.drawImage(this.image, 0, 0, this.canvas.width, this.canvas.height);
      (study.annotations || []).forEach(a => this.drawAnnotation(a));
      if (this.pending.length) this.drawAnnotation({ kind: this.tool, points: this.pending, color: this.color, label: '' }, true);
    }

    drawAnnotation(a, pending) {
      const pts = a.points || [];
      if (!pts.length) return;
      const ctx = this.ctx, scale = Math.max(1, Math.max(this.canvas.width, this.canvas.height) / 1600);
      ctx.save(); ctx.strokeStyle = a.color || '#d53a32'; ctx.fillStyle = a.color || '#d53a32'; ctx.lineWidth = 3 * scale;
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      if (['arrow', 'length', 'angle', 'vhs', 'vlas', 'norberg', 'coverage', 'tpa', 'calibrate'].includes(a.kind)) {
        const paired = ['vhs', 'vlas', 'coverage', 'tpa'].includes(a.kind);
        if (paired) {
          for (let i = 0; i + 1 < pts.length; i += 2) {
            ctx.beginPath(); ctx.moveTo(pts[i].x, pts[i].y); ctx.lineTo(pts[i + 1].x, pts[i + 1].y); ctx.stroke();
          }
        } else {
          ctx.beginPath(); ctx.moveTo(pts[0].x, pts[0].y); pts.slice(1).forEach(p => ctx.lineTo(p.x, p.y)); ctx.stroke();
        }
        pts.forEach(p => { ctx.beginPath(); ctx.arc(p.x, p.y, 5 * scale, 0, Math.PI * 2); ctx.fill(); });
        if (a.kind === 'arrow' && pts.length >= 2) {
          const p1 = pts[pts.length - 2], p2 = pts[pts.length - 1], ang = Math.atan2(p2.y - p1.y, p2.x - p1.x), size = 18 * scale;
          ctx.beginPath(); ctx.moveTo(p2.x, p2.y); ctx.lineTo(p2.x - size * Math.cos(ang - .55), p2.y - size * Math.sin(ang - .55));
          ctx.lineTo(p2.x - size * Math.cos(ang + .55), p2.y - size * Math.sin(ang + .55)); ctx.closePath(); ctx.fill();
        }
      } else if ((a.kind === 'rect' || a.kind === 'ellipse') && pts.length >= 2) {
        const x = Math.min(pts[0].x, pts[1].x), y = Math.min(pts[0].y, pts[1].y), w = Math.abs(pts[1].x - pts[0].x), h = Math.abs(pts[1].y - pts[0].y);
        ctx.beginPath(); if (a.kind === 'rect') ctx.rect(x, y, w, h); else ctx.ellipse(x + w / 2, y + h / 2, w / 2, h / 2, 0, 0, Math.PI * 2); ctx.stroke();
      } else if (a.kind === 'freehand') {
        ctx.beginPath(); ctx.moveTo(pts[0].x, pts[0].y); pts.slice(1).forEach(p => ctx.lineTo(p.x, p.y)); ctx.stroke();
      } else if (a.kind === 'text') {
        ctx.beginPath(); ctx.arc(pts[0].x, pts[0].y, 5 * scale, 0, Math.PI * 2); ctx.fill();
      }
      if (a.label && !pending) {
        const p = pts[pts.length - 1]; ctx.font = `bold ${18 * scale}px serif`; const width = ctx.measureText(a.label).width;
        ctx.fillStyle = 'rgba(0,0,0,.82)'; ctx.fillRect(p.x + 7, p.y + 7, width + 12, 27 * scale);
        ctx.fillStyle = '#fff'; ctx.fillText(a.label, p.x + 13, p.y + 27 * scale);
      }
      ctx.restore();
    }

    renderThumbs() {
      this.thumbWrap.innerHTML = '';
      this.studies.forEach((study, index) => {
        const btn = document.createElement('button'); btn.type = 'button'; btn.className = 'xrw-thumb' + (index === this.activeIndex ? ' active' : '');
        const img = document.createElement('img'); img.src = '/uploads/' + study.preview_path; img.alt = '';
        const text = document.createElement('span'); text.textContent = study.filename || `影像 ${index + 1}`;
        btn.append(img, text); btn.addEventListener('click', () => { this.activeIndex = index; this.pending = []; this.loadActive(); });
        this.thumbWrap.appendChild(btn);
      });
    }

    async uploadFiles(files) {
      const selected = Array.from(files || []);
      if (!selected.length) return;
      this.fileInput.disabled = true;
      for (let i = 0; i < selected.length; i++) {
        this.status.textContent = `正在处理 ${i + 1}/${selected.length}：${selected[i].name}`;
        const form = new FormData(); form.append('csrf_token', this.options.csrf); form.append('order_id', this.options.orderId); form.append('study', selected[i]);
        try {
          const response = await fetch('/admin/xray/workbench/prepare', { method: 'POST', body: form });
          const data = await response.json();
          if (!response.ok || !data.ok) throw new Error(data.error || '上传失败');
          this.studies.push(data.study); this.activeIndex = this.studies.length - 1; this.loadActive();
          if (data.study.projection && !document.querySelector('[name=projection]').value) document.querySelector('[name=projection]').value = data.study.projection;
        } catch (error) {
          this.status.textContent = `${selected[i].name}：${error.message || '处理失败'}`;
          this.status.classList.add('error');
        }
      }
      this.fileInput.value = ''; this.fileInput.disabled = false; this.renderThumbs();
    }

    serialize() { return this.studies; }
  }

  window.XrayWorkbench = XrayWorkbench;
})();

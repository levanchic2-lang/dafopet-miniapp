/* TNR 后台手术资料：拖拽 / 粘贴 / 选择文件都追加，并立刻走原上传接口。 */
(function () {
  var reloadTimer = null;
  var inflight = 0;
  var activeZone = null;

  function fieldInput(zone) {
    return zone.querySelector('input[type="file"]');
  }

  function fileKey(files) {
    return files.map(function (f) {
      return [f.name, f.size, f.lastModified].join(":");
    }).join("|");
  }

  function accepts(input, file) {
    var accept = (input.getAttribute("accept") || "").toLowerCase();
    var type = (file.type || "").toLowerCase();
    var name = (file.name || "").toLowerCase();
    var imageExt = /\.(jpe?g|png|gif|webp|bmp|heic|heif|avif)$/;
    var videoExt = /\.(mp4|mov|webm|mkv|avi|m4v)$/;
    if (accept.indexOf("image") !== -1) {
      if (type.indexOf("image/") === 0) return true;
      if (type.indexOf("video/") === 0) return false;
      return imageExt.test(name);
    }
    if (accept.indexOf("video") !== -1) {
      if (type.indexOf("video/") === 0) return true;
      if (type.indexOf("image/") === 0) return false;
      return videoExt.test(name);
    }
    return true;
  }

  function ensureName(file, input) {
    if (file.name) return file;
    var image = (input.getAttribute("accept") || "").indexOf("image") !== -1;
    var ext = image ? "jpg" : "mp4";
    var type = file.type || (image ? "image/jpeg" : "video/mp4");
    return new File([file], "粘贴-" + Date.now() + "." + ext, { type: type, lastModified: Date.now() });
  }

  function filesFromClipboard(data) {
    if (!data) return [];
    var out = [];
    if (data.files && data.files.length) {
      out = Array.prototype.slice.call(data.files);
    } else if (data.items && data.items.length) {
      Array.prototype.forEach.call(data.items, function (item) {
        if (item.kind === "file") {
          var f = item.getAsFile();
          if (f) out.push(f);
        }
      });
    }
    return out;
  }

  function assignFiles(input, files) {
    var dt = new DataTransfer();
    files.forEach(function (f) { dt.items.add(f); });
    input._tnrSync = true;
    input._tnrAssignedKey = fileKey(files);
    try {
      input.files = dt.files;
    } finally {
      input._tnrSync = false;
    }
  }

  function render(input) {
    var zone = input.closest(".tnr-drop");
    if (!zone) return;
    var ul = zone.querySelector(".tnr-drop-files");
    var err = zone.querySelector(".tnr-drop-error");
    if (!ul) return;
    ul.textContent = "";
    var rows = [];
    (input._tnrDone || []).forEach(function (f) {
      rows.push({ cls: "is-done", text: "已上传 · " + f.name });
    });
    (input._tnrPending || []).forEach(function (f) {
      var st = input._tnrState && input._tnrState.get(f);
      if (st === "error") rows.push({ cls: "is-error", text: "上传失败，仍保留 · " + f.name });
      else if (st === "uploading") rows.push({ cls: "is-uploading", text: "正在上传 · " + f.name });
      else rows.push({ cls: "", text: "待上传 · " + f.name });
    });
    rows.forEach(function (row) {
      var li = document.createElement("li");
      if (row.cls) li.className = row.cls;
      li.textContent = row.text;
      ul.appendChild(li);
    });
    ul.hidden = rows.length === 0;
    if (err) {
      err.textContent = input._tnrError || "";
      err.hidden = !input._tnrError;
    }
  }

  function setBusy(form) {
    var btn = form.querySelector(".admin-upload-submit");
    if (btn) btn.disabled = (form._tnrInflight || 0) > 0;
  }

  function totalPending() {
    var n = 0;
    document.querySelectorAll('form[action*="/upload-surgery"] input[type="file"]').forEach(function (input) {
      n += (input._tnrPending || []).length;
    });
    return n;
  }

  function appIdFromForm(form) {
    var match = (form.getAttribute("action") || "").match(/\/admin\/app\/(\d+)\/upload-surgery/);
    return match ? match[1] : "";
  }

  function scheduleReload(appId) {
    window.clearTimeout(reloadTimer);
    reloadTimer = window.setTimeout(function () {
      if (inflight !== 0 || totalPending() !== 0) return;
      if (appId && location.hash !== "#app-" + appId) location.hash = "app-" + appId;
      location.reload();
    }, 700);
  }

  function uploadNew(form, input, files) {
    if (!files.length) return;
    inflight += 1;
    form._tnrInflight = (form._tnrInflight || 0) + 1;
    setBusy(form);
    files.forEach(function (f) { input._tnrState.set(f, "pending"); });
    render(input);

    var run = function () {
      files.forEach(function (f) { input._tnrState.set(f, "uploading"); });
      render(input);
      var body = new FormData();
      var csrf = form.querySelector('input[name="csrf_token"]');
      if (csrf) body.append("csrf_token", csrf.value);
      files.forEach(function (f) { body.append(input.name, f, f.name); });
      return fetch(form.action, {
        method: "POST",
        body: body,
        credentials: "same-origin",
        redirect: "follow",
      }).then(function (res) {
        if (res.redirected && /\/admin\/login/.test(res.url)) {
          throw new Error("登录已失效，请重新登录后再上传");
        }
        if (!res.ok) {
          return res.json().catch(function () { return {}; }).then(function (data) {
            var msg = data && typeof data.detail === "string" ? data.detail : "上传失败";
            throw new Error(msg);
          });
        }
        input._tnrPending = (input._tnrPending || []).filter(function (f) {
          return files.indexOf(f) === -1;
        });
        input._tnrDone = (input._tnrDone || []).concat(files);
        files.forEach(function (f) { input._tnrState.set(f, "done"); });
        input._tnrError = "";
        assignFiles(input, input._tnrPending);
        render(input);
        scheduleReload(appIdFromForm(form));
      }).catch(function (err) {
        files.forEach(function (f) { input._tnrState.set(f, "error"); });
        input._tnrError = (err && err.message) || "上传失败，文件仍保留，可再点上传按钮";
        render(input);
      }).then(function () {
        inflight -= 1;
        form._tnrInflight = Math.max(0, (form._tnrInflight || 1) - 1);
        setBusy(form);
      });
    };

    var prev = form._tnrChain || Promise.resolve();
    form._tnrChain = prev.then(run, run);
  }

  function addFiles(form, input, fileList) {
    var fresh = [];
    Array.prototype.forEach.call(fileList || [], function (raw) {
      if (!raw || !raw.size) return;
      var file = ensureName(raw, input);
      if (!accepts(input, file)) return;
      fresh.push(file);
    });
    if (!fresh.length) {
      var wantVideo = (input.getAttribute("accept") || "").indexOf("video") !== -1;
      input._tnrError = wantVideo ? "请拖入视频文件" : "请拖入图片文件";
      render(input);
      return;
    }
    input._tnrError = "";
    input._tnrPending = (input._tnrPending || []).concat(fresh);
    fresh.forEach(function (f) { input._tnrState.set(f, "pending"); });
    assignFiles(input, input._tnrPending);
    render(input);
    uploadNew(form, input, fresh);
  }

  function bind(zone) {
    var input = fieldInput(zone);
    var form = zone.closest("form");
    if (!input || !form || zone.getAttribute("data-tnr-bound") === "1") return;
    zone.setAttribute("data-tnr-bound", "1");
    input._tnrPending = [];
    input._tnrDone = [];
    input._tnrState = new Map();
    var depth = 0;

    zone.addEventListener("dragenter", function (e) {
      e.preventDefault();
      depth += 1;
      zone.classList.add("is-over");
    });
    zone.addEventListener("dragover", function (e) {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      zone.classList.add("is-over");
    });
    zone.addEventListener("dragleave", function () {
      depth = Math.max(0, depth - 1);
      if (depth === 0) zone.classList.remove("is-over");
    });
    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      e.stopPropagation();
      depth = 0;
      zone.classList.remove("is-over");
      activeZone = zone;
      addFiles(form, input, e.dataTransfer && e.dataTransfer.files);
    });
    input.addEventListener("change", function () {
      if (input._tnrSync) return;
      var picked = Array.prototype.slice.call(input.files || []);
      if (fileKey(picked) && fileKey(picked) === input._tnrAssignedKey) return;
      addFiles(form, input, picked);
    });
    zone.addEventListener("paste", function (e) {
      var files = filesFromClipboard(e.clipboardData);
      if (!files.length) return;
      e.preventDefault();
      e.stopPropagation();
      activeZone = zone;
      addFiles(form, input, files);
    });
    zone.addEventListener("mouseenter", function () { activeZone = zone; });
    zone.addEventListener("focusin", function () { activeZone = zone; });
    if (form.getAttribute("data-tnr-submit") !== "1") {
      form.setAttribute("data-tnr-submit", "1");
      form.addEventListener("submit", function (e) {
        if (form._tnrInflight) e.preventDefault();
      });
    }
  }

  document.addEventListener("paste", function (e) {
    var target = e.target;
    if (target && target.closest && target.closest(".tnr-drop")) return;
    var tag = target && target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || (target && target.isContentEditable)) {
      if (!target || target.type !== "file") return;
    }
    if (!activeZone || !activeZone.isConnected) return;
    var files = filesFromClipboard(e.clipboardData);
    if (!files.length) return;
    var input = fieldInput(activeZone);
    var form = activeZone.closest("form");
    if (!input || !form) return;
    e.preventDefault();
    addFiles(form, input, files);
  });

  document.addEventListener("dragover", function (e) {
    if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") !== -1) {
      e.preventDefault();
    }
  });
  document.addEventListener("drop", function (e) {
    if (e.target && e.target.closest && e.target.closest(".tnr-drop")) return;
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) e.preventDefault();
  });

  function init() {
    document.querySelectorAll('form[action*="/upload-surgery"] .tnr-drop').forEach(bind);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();

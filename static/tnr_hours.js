/* TNR 门店开放时间。配置来自后端 window.TNR_HOURS（app.main._tnr_hours_public）。
   只收紧比默认窗口更晚的门店（目前是东环店 13:00）。横岗店仍用完整下拉，由后端维持 11:00–18:00。 */
(function (root) {
  function hours() {
    return root.TNR_HOURS || { default_start: "11:00", default_end: "18:00", by_store: {} };
  }

  function hhmm(value) {
    var matched = String(value || "").match(/^(\d{1,2}):(\d{2})/);
    if (!matched) return "";
    var hour = matched[1].length === 1 ? "0" + matched[1] : matched[1];
    return hour + ":" + matched[2];
  }

  function windowFor(store) {
    var cfg = hours();
    var by = cfg.by_store || {};
    if (store && by[store]) return by[store];
    var text = store || "";
    var keys = Object.keys(by);
    for (var i = 0; i < keys.length; i++) {
      if (text && (text.indexOf(keys[i]) >= 0 || keys[i].indexOf(text) >= 0)) return by[keys[i]];
    }
    return { start: cfg.default_start || "11:00", end: cfg.default_end || "18:00" };
  }

  function uiFloor(category, store) {
    if (category !== "tnr") return "";
    var cfg = hours();
    var win = windowFor(store);
    var start = hhmm((win && win.start) || "");
    var base = hhmm(cfg.default_start || "");
    if (start && base && start > base) return start;
    return "";
  }

  function storeLabel(store) {
    var text = store || "";
    if (text.indexOf("东环") >= 0) return "东环店";
    if (text.indexOf("横岗") >= 0) return "横岗店";
    return text || "该门店";
  }

  function hintText(floor, store) {
    if (!floor) return "";
    var win = windowFor(store);
    var end = hhmm((win && win.end) || "");
    var range = end ? "（开放至 " + end + " 前）" : "";
    return storeLabel(store) + " TNR 手术从 " + floor + " 开始" + range + "，更早的时段不可预约。";
  }

  function applySelect(select, category, store, opts) {
    if (!select) return "";
    opts = opts || {};
    var floor = uiFloor(category, store);
    var keep = hhmm(opts.keepValue || "");
    var current = hhmm(select.value || "");
    for (var i = 0; i < select.options.length; i++) {
      var opt = select.options[i];
      var value = hhmm(opt.value || "");
      var hide = !!(floor && value && value < floor && value !== keep);
      opt.disabled = hide;
      opt.hidden = hide;
    }
    if (floor && current && current < floor && current !== keep) {
      for (var j = 0; j < select.options.length; j++) {
        var candidate = hhmm(select.options[j].value || "");
        if (candidate && candidate >= floor && !select.options[j].disabled) {
          select.value = select.options[j].value;
          break;
        }
      }
    }
    return floor;
  }

  function applyTimeInput(input, category, store, opts) {
    if (!input) return "";
    opts = opts || {};
    var floor = uiFloor(category, store);
    var keep = hhmm(opts.keepValue || "");
    var current = hhmm(input.value || "");
    if (floor && current && current < floor && current !== keep) input.value = floor;
    input.min = (keep && floor && keep < floor) ? keep : (floor || "");
    return floor;
  }

  root.TnrHours = {
    windowFor: windowFor,
    uiFloor: uiFloor,
    hintText: hintText,
    applySelect: applySelect,
    applyTimeInput: applyTimeInput,
  };
})(window);

(function () {
  function storeOf(form) {
    var select = form.querySelector("[data-offer-store]");
    return select ? select.value : "";
  }

  function meta(item) {
    var bits = [item.store || "两店通用"];
    if (item.unit) bits.push("单位 " + item.unit);
    if (item.sell_price || item.sell_price === 0) bits.push("单价 ¥" + item.sell_price);
    return bits.join(" · ");
  }

  function bindSearch(form, input, results, onPick) {
    var timer = null;
    var seq = 0;
    input.addEventListener("input", function () {
      var text = input.value.trim();
      if (timer) clearTimeout(timer);
      if (!text) {
        seq += 1;
        results.innerHTML = "";
        results.hidden = true;
        return;
      }
      timer = setTimeout(function () {
        var current = ++seq;
        var url = "/admin/packages/offer-items?q=" + encodeURIComponent(text);
        var store = storeOf(form);
        if (store) url += "&store=" + encodeURIComponent(store);
        fetch(url, { headers: { "Accept": "application/json" } })
          .then(function (res) { return res.json(); })
          .then(function (data) {
            if (current !== seq) return;
            var items = (data && data.items) || [];
            results.innerHTML = "";
            items.forEach(function (item) {
              var btn = document.createElement("button");
              btn.type = "button";
              btn.className = "offer-option";
              var strong = document.createElement("strong");
              strong.textContent = item.name || "";
              var small = document.createElement("small");
              small.textContent = meta(item);
              btn.appendChild(strong);
              btn.appendChild(small);
              btn.addEventListener("click", function () {
                onPick(item);
                results.innerHTML = "";
                results.hidden = true;
                input.value = item.name || "";
              });
              results.appendChild(btn);
            });
            results.hidden = items.length === 0;
          })
          .catch(function () {
            results.hidden = true;
          });
      }, 250);
    });
  }

  function preview(form) {
    var name = form.querySelector("[data-offer-name]");
    if (!name) return;
    var primary = form.querySelector("[data-offer-primary]");
    var params = new URLSearchParams();
    params.set("as_package", "1");
    params.set("primary_item_id", primary ? primary.value : "");
    params.set("buy_count", (form.querySelector("[name=buy_count]") || {}).value || "");
    params.set("gift_count", (form.querySelector("[name=gift_count]") || {}).value || "0");
    params.set("price", (form.querySelector("[name=price]") || {}).value || "");
    params.set("validity_days", (form.querySelector("[name=validity_days]") || {}).value || "0");
    var store = form.querySelector("[name=store]");
    if (store) params.set("store", store.value);
    form.querySelectorAll("[data-gift-row]").forEach(function (row) {
      var id = row.querySelector("[data-gift-id]");
      var qty = row.querySelector("[name=gift_qty]");
      if (id && id.value) {
        params.append("gift_item_id", id.value);
        params.append("gift_qty", qty ? qty.value : "1");
      }
    });
    fetch("/admin/packages/offer-preview?" + params.toString(), { headers: { "Accept": "application/json" } })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data && data.error) {
          name.textContent = data.error;
          return;
        }
        var text = (data && data.name) || "";
        if (data && data.reused) text += "（已有相同组合，将直接复用）";
        name.textContent = text || "填写后这里显示系统生成的名称";
      })
      .catch(function () {
        name.textContent = "名称预览失败，提交时仍以系统生成的为准";
      });
  }

  function addGiftRow(form) {
    var host = form.querySelector("[data-gift-rows]");
    var tpl = form.querySelector("[data-gift-template]");
    if (!host || !tpl) return;
    var row = tpl.content.firstElementChild.cloneNode(true);
    host.appendChild(row);
    var input = row.querySelector("[data-gift-search]");
    var results = row.querySelector("[data-gift-results]");
    var hidden = row.querySelector("[data-gift-id]");
    var picked = row.querySelector("[data-gift-picked]");
    bindSearch(form, input, results, function (item) {
      hidden.value = String(item.id);
      if (picked) picked.textContent = "已选 " + (item.name || "") + " · " + meta(item);
    });
    row.querySelector("[data-gift-remove]").addEventListener("click", function () {
      row.remove();
      preview(form);
    });
    row.querySelector("[name=gift_qty]").addEventListener("input", function () { preview(form); });
  }

  document.querySelectorAll("[data-offer-form]").forEach(function (form) {
    var search = form.querySelector("[data-offer-search]");
    var results = form.querySelector("[data-offer-results]");
    var primary = form.querySelector("[data-offer-primary]");
    var picked = form.querySelector("[data-offer-picked]");
    var check = form.querySelector("[data-offer-check]");
    var deal = form.querySelector("[data-offer-deal]");
    if (search && results && primary) {
      bindSearch(form, search, results, function (item) {
        primary.value = String(item.id);
        if (picked) picked.textContent = "已选 " + (item.name || "") + " · " + meta(item);
        var price = form.querySelector("[name=price]");
        var buy = form.querySelector("[name=buy_count]");
        if (price && !price.value && item.sell_price) {
          var times = parseInt((buy && buy.value) || "1", 10) || 1;
          price.value = (item.sell_price * times).toFixed(2);
        }
        preview(form);
      });
    }
    if (check && deal) {
      var sync = function () {
        deal.hidden = !check.checked;
      };
      check.addEventListener("change", sync);
      sync();
    }
    var add = form.querySelector("[data-gift-add]");
    if (add) add.addEventListener("click", function () { addGiftRow(form); });
    ["buy_count", "gift_count", "price", "validity_days", "store"].forEach(function (field) {
      var node = form.querySelector("[name=" + field + "]");
      if (node) node.addEventListener("input", function () { preview(form); });
      if (node) node.addEventListener("change", function () { preview(form); });
    });
  });
})();

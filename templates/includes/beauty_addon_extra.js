function dafopetBandContains(band, weight) {
  var lo = band.lo_kg;
  var hi = band.hi_kg;
  if (lo !== null && lo !== undefined) {
    if (band.lo_inclusive ? weight < lo : weight <= lo) return false;
  }
  if (hi !== null && hi !== undefined) {
    if (band.hi_inclusive ? weight > hi : weight >= hi) return false;
  }
  return true;
}
function dafopetAddonRuleMinutes(rule, service, size, weight) {
  if (typeof rule === "number") return rule > 0 ? rule : 0;
  if (!rule || rule.mode !== "weight") return 0;
  var species = "";
  if (service && service.indexOf("猫") >= 0) species = "cat";
  else if (service && service.indexOf("犬") >= 0) species = "dog";
  else if (size && size.indexOf("猫") >= 0) species = "cat";
  else if (size && size.indexOf("犬") >= 0) species = "dog";
  var w = Number(weight);
  if (w > 0) {
    var speciesList = (species === "cat" || species === "dog") ? [species] : ["cat", "dog"];
    var best = 0;
    speciesList.forEach(function (sp) {
      (rule.bands[sp] || []).forEach(function (band) {
        if (dafopetBandContains(band, w)) best = Math.max(best, Number(band.minutes) || 0);
      });
    });
    if (best > 0) return best;
  }
  var order = rule.size_order || [];
  for (var i = 0; i < order.length; i++) {
    if (size && size.indexOf(order[i]) >= 0) return Number((rule.size_minutes || {})[order[i]]) || 0;
  }
  if (species && rule.species_fallback_minutes && rule.species_fallback_minutes[species]) {
    return Number(rule.species_fallback_minutes[species]) || 0;
  }
  return Number(rule.unknown_minutes) || 0;
}
function dafopetBeautyAddonExtra(map, names, service, size, weight) {
  var total = 0;
  var seen = {};
  (names || []).forEach(function (name) {
    var key = String(name || "").replace(/^\s+|\s+$/g, "");
    if (!key || !map) return;
    var rule = map[key];
    if (rule && typeof rule === "object") {
      var label = rule.label || key;
      if (seen[label]) return;
      seen[label] = true;
      total += dafopetAddonRuleMinutes(rule, service, size, weight);
      return;
    }
    var n = Number(rule);
    if (n > 0) total += n;
  });
  return total;
}

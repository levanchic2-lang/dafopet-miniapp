/**
 * TNR 申请页的本机草稿与个人资料预填。
 * 这里不碰 wx，方便单测；页面负责存储、文件和网络。
 * 草稿只留在手机上，不要把进行中的内容发到服务器。
 */

const DRAFT_KEY = "TNR_APPLY_DRAFT";
const LAST_PERSONAL_KEY = "TNR_APPLY_LAST_PERSONAL";
const DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const DRAFT_DEBOUNCE_MS = 400;
const PHOTO_LIMIT = 6;
const VIDEO_LIMIT = 2;

const PERSONAL_KEYS = ["applicant_name", "phone", "id_number", "address", "clinic_store"];

const STORE_BY_SHORT = {
  "东环店": "大风动物医院（东环店）",
  "横岗店": "大风动物医院（横岗店）",
};

function canonicalStore(value) {
  const text = String(value || "").trim();
  return STORE_BY_SHORT[text] || text;
}

function pickPersonal(source) {
  const out = {};
  if (!source || typeof source !== "object") return out;
  PERSONAL_KEYS.forEach((key) => {
    if (source[key] == null) return;
    const text = String(source[key]).trim();
    if (!text) return;
    out[key] = key === "clinic_store" ? canonicalStore(text) : text;
  });
  return out;
}

function blankForm() {
  return {
    applicant_name: "",
    phone: "",
    address: "",
    clinic_store: "大风动物医院（东环店）",
    location_lat: "",
    location_lng: "",
    location_address: "",
    id_number: "",
    post_surgery_plan: "原地放归",
    cat_nickname: "",
    cat_gender: "male",
    cat_breed: "",
    cat_color: "",
    age_estimate: "",
    health_note: "",
    suture_preference: "external",
  };
}

function applyPersonalFields(form, incoming) {
  const patch = {};
  const current = form || {};
  const safe = pickPersonal(incoming);
  PERSONAL_KEYS.forEach((key) => {
    const value = safe[key] || "";
    if (!value) return;
    if (String(current[key] || "").trim()) return;
    patch[key] = value;
  });
  return patch;
}

function resolvePersonalPrefill(input) {
  const src = input || {};
  const profile = src.profile;
  const tnr = src.tnr;
  const local = src.local && typeof src.local === "object" ? src.local : null;
  const form = src.form || {};
  const merged = {};
  const localPersonal = pickPersonal(local);
  PERSONAL_KEYS.forEach((key) => {
    if (localPersonal[key]) merged[key] = localPersonal[key];
  });
  if (profile && profile.bound) {
    if (profile.name) merged.applicant_name = String(profile.name).trim();
    if (profile.phone) merged.phone = String(profile.phone).trim();
  }
  let serverAddress = "";
  if (tnr && tnr.found) {
    const fromApp = pickPersonal(tnr);
    if (fromApp.address) serverAddress = fromApp.address;
    PERSONAL_KEYS.forEach((key) => {
      if (fromApp[key]) merged[key] = fromApp[key];
    });
  }
  const patch = applyPersonalFields(form, merged);
  let structured = null;
  const usingLocalAddress = !!(patch.address && patch.address !== serverAddress && local);
  if (usingLocalAddress && (local.districtName || local.streetName || local.addressDetailInput)) {
    structured = {
      cityName: local.cityName || "",
      districtName: local.districtName || "",
      streetName: local.streetName || "",
      detail: local.addressDetailInput || "",
    };
  }
  return {
    patch: patch,
    structured: structured,
    idConsent: false,
    handFill: true,
  };
}

function buildLastPersonal(state, now) {
  const src = state || {};
  const personal = pickPersonal((src.form) || {});
  return {
    savedAt: now || Date.now(),
    applicant_name: personal.applicant_name || "",
    phone: personal.phone || "",
    id_number: personal.id_number || "",
    address: personal.address || "",
    clinic_store: personal.clinic_store || "",
    cityName: src.cityName || "",
    districtName: src.districtName || "",
    streetName: src.streetName || "",
    addressDetailInput: src.addressDetailInput || "",
  };
}

function buildDraft(state, now) {
  const src = state || {};
  const form = Object.assign(blankForm(), src.form || {});
  if (typeof src.healthNoteDraft === "string") form.health_note = src.healthNoteDraft;
  return {
    v: 1,
    savedAt: now || Date.now(),
    form: form,
    cityName: src.cityName || "",
    districtName: src.districtName || "",
    streetName: src.streetName || "",
    addressDetailInput: src.addressDetailInput || "",
    cityIndex: Number(src.cityIndex || 0),
    checks: {
      ear: !(src.checks && src.checks.ear === false),
      fraud: !(src.checks && src.checks.fraud === false),
    },
    idConsent: !!src.idConsent,
    isProxy: !!src.isProxy,
    proxyName: src.proxyName || "",
    proxyPhone: src.proxyPhone || "",
    proxyRelationIndex: Number(src.proxyRelationIndex || 0),
    proxyConsent: !!src.proxyConsent,
    planIndex: Number(src.planIndex || 0),
    genderIndex: Number(src.genderIndex || 0),
    sutureIndex: Number(src.sutureIndex || 0),
    storeIndex: Number(src.storeIndex || 0),
    images: (src.images || []).slice(),
    videos: (src.videos || []).slice(),
  };
}

function draftHasContent(draft) {
  if (!draft || typeof draft !== "object") return false;
  const form = draft.form || {};
  const textKeys = [
    "applicant_name", "phone", "id_number", "address", "location_address",
    "cat_nickname", "cat_breed", "cat_color", "age_estimate", "health_note",
  ];
  if (textKeys.some((key) => String(form[key] || "").trim())) return true;
  if (String(draft.addressDetailInput || "").trim()) return true;
  if ((draft.images || []).length || (draft.videos || []).length) return true;
  if (draft.isProxy || draft.proxyName || draft.proxyPhone || draft.proxyConsent) return true;
  if (draft.idConsent) return true;
  if (draft.districtName || draft.streetName) return true;
  if (form.cat_gender && form.cat_gender !== "male") return true;
  if (form.suture_preference && form.suture_preference !== "external") return true;
  if (form.post_surgery_plan && form.post_surgery_plan !== "原地放归") return true;
  if (form.clinic_store && canonicalStore(form.clinic_store) !== "大风动物医院（东环店）") return true;
  if (draft.checks && (draft.checks.ear === false || draft.checks.fraud === false)) return true;
  return false;
}

function normalizeDraft(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  return raw;
}

function mediaPaths(draft) {
  if (!draft) return [];
  return [].concat(draft.images || [], draft.videos || []).filter(Boolean);
}

function inspectDraft(raw, now) {
  const draft = normalizeDraft(raw);
  if (!draft) return { action: "empty", removeFiles: [], draft: null };
  const files = mediaPaths(draft);
  const savedAt = Number(draft.savedAt || 0);
  const at = Number(now || Date.now());
  if (!savedAt || at - savedAt >= DRAFT_TTL_MS) {
    return { action: "expired", removeFiles: files, draft: draft };
  }
  return { action: "restore", removeFiles: [], draft: draft };
}

function classifyMedia(paths, canAccess) {
  const kept = [];
  const missing = [];
  (Array.isArray(paths) ? paths : []).forEach((item) => {
    if (!item) return;
    if (canAccess && canAccess(item)) kept.push(item);
    else missing.push(item);
  });
  return { kept: kept, missing: missing };
}

function missingMediaPrompt(missingImages, missingVideos) {
  const parts = [];
  const images = Number(missingImages || 0);
  const videos = Number(missingVideos || 0);
  if (images > 0) parts.push(images + " 张照片");
  if (videos > 0) parts.push(videos + " 个视频");
  if (!parts.length) return "";
  return "有 " + parts.join("、") + "已不在这台手机上，请重新选择。已恢复其余填写内容。";
}

function restorePlan(raw, now, accessMap) {
  const inspected = inspectDraft(raw, now);
  if (inspected.action !== "restore") {
    return {
      type: inspected.action,
      prompt: "",
      removeFiles: inspected.removeFiles || [],
      draft: null,
      idConsent: false,
    };
  }
  const access = accessMap || {};
  const images = classifyMedia(inspected.draft.images, (item) => access[item] === true);
  const videos = classifyMedia(inspected.draft.videos, (item) => access[item] === true);
  return {
    type: "restore",
    prompt: missingMediaPrompt(images.missing.length, videos.missing.length),
    removeFiles: images.missing.concat(videos.missing),
    idConsent: !!inspected.draft.idConsent,
    draft: Object.assign({}, inspected.draft, {
      images: images.kept,
      videos: videos.kept,
    }),
  };
}

function appendLimited(existing, incoming, limit) {
  const cap = Number(limit) || 0;
  const base = (Array.isArray(existing) ? existing : []).filter(Boolean).slice(0, cap);
  const add = (Array.isArray(incoming) ? incoming : []).filter(Boolean);
  const room = Math.max(0, cap - base.length);
  const accepted = add.slice(0, room);
  return {
    items: base.concat(accepted),
    overflow: Math.max(0, add.length - accepted.length),
    limit: cap,
  };
}

function filesToDelete(paths, userDataPath) {
  const root = String(userDataPath || "").replace(/\/+$/, "");
  const seen = new Set();
  const out = [];
  (paths || []).forEach((item) => {
    const text = String(item || "");
    if (!text || seen.has(text)) return;
    const underRoot = !!root && (text === root || text.indexOf(root + "/") === 0);
    const namedCopy = text.indexOf("/tnr_") >= 0
      && text.indexOf("wxfile://tmp") !== 0
      && text.indexOf("http://tmp") !== 0
      && (text.indexOf("wxfile://") === 0 || text.indexOf("http://usr") === 0);
    if (!underRoot && !namedCopy) return;
    seen.add(text);
    out.push(text);
  });
  return out;
}

function optionIndex(options, value, fallback) {
  const list = options || [];
  const idx = list.findIndex((item) => item && item.value === value);
  if (idx >= 0) return idx;
  return Number(fallback || 0);
}

function storeIndexFor(options, store) {
  const want = canonicalStore(store);
  const list = options || [];
  const idx = list.findIndex((item) => item && (item.value === want || item.value === store));
  return idx >= 0 ? idx : 0;
}

function parseStoredAddress(address, regionData, cityNames) {
  const raw = String(address || "").trim();
  if (!raw) return null;
  let rest = raw.indexOf("广东省") === 0 ? raw.slice("广东省".length) : raw;
  const cities = (cityNames && cityNames.length ? cityNames.slice() : Object.keys(regionData || {}))
    .sort((a, b) => String(b).length - String(a).length);
  let city = "";
  for (let i = 0; i < cities.length; i += 1) {
    const name = cities[i];
    if (name && rest.indexOf(name) === 0) {
      city = name;
      rest = rest.slice(name.length);
      break;
    }
  }
  const cityData = city && regionData ? regionData[city] : null;
  if (!cityData) {
    return {
      cityName: city,
      districtName: "",
      streetName: "",
      detail: raw,
      recognized: false,
    };
  }
  const districts = Object.keys(cityData).sort((a, b) => String(b).length - String(a).length);
  let district = "";
  for (let i = 0; i < districts.length; i += 1) {
    const name = districts[i];
    if (name && rest.indexOf(name) === 0) {
      district = name;
      rest = rest.slice(name.length);
      break;
    }
  }
  let street = "";
  if (district) {
    const rawStreets = cityData[district];
    const streets = (Array.isArray(rawStreets) ? rawStreets.slice() : Object.keys(rawStreets || {}))
      .sort((a, b) => String(b).length - String(a).length);
    for (let i = 0; i < streets.length; i += 1) {
      const name = streets[i];
      if (name && rest.indexOf(name) === 0) {
        street = name;
        rest = rest.slice(name.length);
        break;
      }
    }
  }
  return {
    cityName: city,
    districtName: district,
    streetName: street,
    detail: rest.trim(),
    recognized: !!(district && street),
  };
}

module.exports = {
  DRAFT_KEY: DRAFT_KEY,
  LAST_PERSONAL_KEY: LAST_PERSONAL_KEY,
  DRAFT_TTL_MS: DRAFT_TTL_MS,
  DRAFT_DEBOUNCE_MS: DRAFT_DEBOUNCE_MS,
  PHOTO_LIMIT: PHOTO_LIMIT,
  VIDEO_LIMIT: VIDEO_LIMIT,
  PERSONAL_KEYS: PERSONAL_KEYS,
  canonicalStore: canonicalStore,
  pickPersonal: pickPersonal,
  blankForm: blankForm,
  applyPersonalFields: applyPersonalFields,
  resolvePersonalPrefill: resolvePersonalPrefill,
  buildLastPersonal: buildLastPersonal,
  buildDraft: buildDraft,
  draftHasContent: draftHasContent,
  inspectDraft: inspectDraft,
  classifyMedia: classifyMedia,
  missingMediaPrompt: missingMediaPrompt,
  restorePlan: restorePlan,
  appendLimited: appendLimited,
  filesToDelete: filesToDelete,
  optionIndex: optionIndex,
  storeIndexFor: storeIndexFor,
  parseStoredAddress: parseStoredAddress,
};

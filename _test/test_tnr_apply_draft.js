const assert = require("assert");
const fs = require("fs");
const path = require("path");
const draft = require("../miniapp/utils/apply_draft");

const now = 1800000000000;
const regions = {
  深圳市: {
    罗湖区: { 桂园街道: ["人民桥社区"] },
    福田区: { 园岭街道: ["红荔社区"] },
  },
  东莞市: { 城区: ["莞城街道", "东城街道"] },
};
const cities = ["深圳市", "东莞市", "惠州市"];

const saved = draft.buildDraft({
  form: {
    applicant_name: "甲",
    phone: "13800000000",
    id_number: "110101199001011234",
    address: "广东省深圳市罗湖区桂园街道 1号",
    clinic_store: "大风动物医院（东环店）",
    cat_nickname: "花",
    health_note: "旧说明",
  },
  healthNoteDraft: "还没失焦的说明",
  idConsent: true,
  isProxy: true,
  proxyName: "代理人",
  proxyPhone: "13800000000",
  proxyRelationIndex: 2,
  proxyConsent: true,
  checks: { ear: false, fraud: true },
  images: ["wxfile://usr/a.jpg", "wxfile://usr/b.jpg"],
  videos: ["wxfile://usr/c.mp4"],
  cityName: "深圳市",
  districtName: "罗湖区",
  streetName: "桂园街道",
  addressDetailInput: "1号",
}, now);

assert.strictEqual(saved.form.health_note, "还没失焦的说明");
assert.strictEqual(saved.form.cat_nickname, "花");
assert.strictEqual(saved.idConsent, true);
assert.strictEqual(saved.proxyName, "代理人");
assert.strictEqual(saved.checks.ear, false);

const fresh = draft.restorePlan(
  Object.assign({}, saved, { savedAt: now - draft.DRAFT_TTL_MS + 1 }),
  now,
  {
    "wxfile://usr/a.jpg": true,
    "wxfile://usr/b.jpg": false,
    "wxfile://usr/c.mp4": true,
  }
);
assert.strictEqual(fresh.type, "restore");
assert.strictEqual(fresh.idConsent, true);
assert.deepStrictEqual(fresh.draft.images, ["wxfile://usr/a.jpg"]);
assert.deepStrictEqual(fresh.draft.videos, ["wxfile://usr/c.mp4"]);
assert.strictEqual(fresh.draft.form.cat_nickname, "花");
assert.strictEqual(fresh.prompt, "有 1 张照片已不在这台手机上，请重新选择。已恢复其余填写内容。");

const justExpired = draft.inspectDraft(
  Object.assign({}, saved, { savedAt: now - draft.DRAFT_TTL_MS }),
  now
);
assert.strictEqual(justExpired.action, "expired");
assert.deepStrictEqual(justExpired.removeFiles, [
  "wxfile://usr/a.jpg",
  "wxfile://usr/b.jpg",
  "wxfile://usr/c.mp4",
]);
assert.strictEqual(draft.restorePlan(justExpired.draft, now, {}).type, "expired");
assert.strictEqual(draft.DRAFT_TTL_MS, 7 * 24 * 60 * 60 * 1000);

const bothMissing = draft.restorePlan(saved, now, {});
assert.strictEqual(
  bothMissing.prompt,
  "有 2 张照片、1 个视频已不在这台手机上，请重新选择。已恢复其余填写内容。"
);
assert.deepStrictEqual(bothMissing.draft.images, []);
assert.deepStrictEqual(bothMissing.draft.videos, []);

assert.strictEqual(draft.inspectDraft(null, now).action, "empty");
assert.strictEqual(draft.missingMediaPrompt(0, 0), "");

const resolved = draft.resolvePersonalPrefill({
  profile: { bound: true, name: "档案名", phone: "13700001111", pets: [{ name: "甲犬", breed: "柯基" }] },
  tnr: {
    found: true,
    applicant_name: "新名",
    phone: "13900002222",
    id_number: "110101199002021234",
    address: "广东省深圳市福田区园岭街道 新门牌",
    clinic_store: "横岗店",
    cat_nickname: "新猫",
    cat_breed: "田园猫",
    cat_color: "橘色",
    age_estimate: "2020-05",
    health_note: "新流浪说明",
    suture_preference: "intradermal",
    post_surgery_plan: "原地放归",
    is_proxy: true,
    proxy_name: "代办人张三",
    images: ["a.jpg"],
    videos: ["b.mp4"],
  },
  local: { applicant_name: "本地", cat_nickname: "本地猫", id_number: "不应覆盖" },
  form: { applicant_name: "", phone: "", id_number: "", address: "", clinic_store: "" },
});
assert.strictEqual(resolved.patch.applicant_name, "新名");
assert.strictEqual(resolved.patch.phone, "13900002222");
assert.strictEqual(resolved.patch.id_number, "110101199002021234");
assert.strictEqual(resolved.patch.clinic_store, "大风动物医院（横岗店）");
assert.strictEqual(resolved.idConsent, false);
assert.strictEqual(resolved.handFill, true);
assert.strictEqual(resolved.patch.cat_nickname, undefined);
const blob = JSON.stringify(resolved);
["新猫", "甲犬", "柯基", "代办人", "流浪", "intradermal", "本地猫", "不应覆盖", "原地放归"].forEach((secret) => {
  assert.ok(!blob.includes(secret), secret);
});

const hand = draft.resolvePersonalPrefill({
  profile: null,
  tnr: null,
  local: null,
  form: { applicant_name: "手填", phone: "", id_number: "", address: "", clinic_store: "" },
});
assert.deepStrictEqual(hand.patch, {});
assert.strictEqual(hand.handFill, true);
assert.strictEqual(hand.idConsent, false);

const unbound = draft.resolvePersonalPrefill({
  profile: { bound: false, name: "不该用档案名", pets: [{ name: "不该出现" }] },
  tnr: null,
  local: {
    applicant_name: "上次",
    phone: "13600000000",
    id_number: "110101199001011234",
    address: "广东省深圳市罗湖区桂园街道 1号",
    clinic_store: "大风动物医院（东环店）",
    cat_nickname: "不该出现的猫",
    cityName: "深圳市",
    districtName: "罗湖区",
    streetName: "桂园街道",
    addressDetailInput: "1号",
  },
  form: {},
});
assert.strictEqual(unbound.patch.applicant_name, "上次");
assert.strictEqual(unbound.patch.id_number, "110101199001011234");
assert.strictEqual(unbound.structured.districtName, "罗湖区");
assert.strictEqual(unbound.idConsent, false);
assert.ok(!JSON.stringify(unbound).includes("不该出现的猫"));
assert.ok(!JSON.stringify(unbound).includes("不该用档案名"));

const kept = draft.resolvePersonalPrefill({
  profile: { bound: true, name: "档案", phone: "13700001111" },
  tnr: { found: true, applicant_name: "新", phone: "13900002222", id_number: "110", address: "址", clinic_store: "东环店" },
  form: { applicant_name: "手写姓名", phone: "", id_number: "", address: "", clinic_store: "" },
});
assert.strictEqual(kept.patch.applicant_name, undefined);
assert.strictEqual(kept.patch.phone, "13900002222");
assert.strictEqual(kept.idConsent, false);

const last = draft.buildLastPersonal({
  form: {
    applicant_name: "甲",
    phone: "13800000000",
    id_number: "110",
    address: "址",
    clinic_store: "东环店",
    cat_nickname: "猫",
    health_note: "病",
  },
  idConsent: true,
  proxyName: "代",
  cityName: "深圳市",
  addressDetailInput: "门牌",
}, now);
assert.strictEqual(last.applicant_name, "甲");
assert.strictEqual(last.clinic_store, "大风动物医院（东环店）");
assert.strictEqual(last.addressDetailInput, "门牌");
assert.ok(!("cat_nickname" in last));
assert.ok(!("health_note" in last));
assert.ok(!("idConsent" in last));
assert.ok(!("proxyName" in last));

const photos = draft.appendLimited(["p1"], ["p2", "p3"], draft.PHOTO_LIMIT);
assert.deepStrictEqual(photos.items, ["p1", "p2", "p3"]);
assert.strictEqual(photos.overflow, 0);
const capped = draft.appendLimited(["1", "2", "3", "4", "5"], ["6", "7"], 6);
assert.deepStrictEqual(capped.items, ["1", "2", "3", "4", "5", "6"]);
assert.strictEqual(capped.overflow, 1);
const videos = draft.appendLimited(["v1"], ["v2", "v3"], draft.VIDEO_LIMIT);
assert.deepStrictEqual(videos.items, ["v1", "v2"]);
assert.strictEqual(draft.PHOTO_LIMIT, 6);
assert.strictEqual(draft.VIDEO_LIMIT, 2);

assert.deepStrictEqual(
  draft.filesToDelete(
    ["wxfile://usr/tnr_a.jpg", "http://tmp/x.jpg", "wxfile://usr/tnr_b.mp4", "wxfile://usr-evil/a.jpg", "wxfile://store/tnr_c.jpg"],
    "wxfile://usr"
  ),
  ["wxfile://usr/tnr_a.jpg", "wxfile://usr/tnr_b.mp4", "wxfile://store/tnr_c.jpg"]
);

const shenzhen = draft.parseStoredAddress("广东省深圳市罗湖区桂园街道 人民路 1 号", regions, cities);
assert.strictEqual(shenzhen.cityName, "深圳市");
assert.strictEqual(shenzhen.districtName, "罗湖区");
assert.strictEqual(shenzhen.streetName, "桂园街道");
assert.strictEqual(shenzhen.detail, "人民路 1 号");
assert.strictEqual(shenzhen.recognized, true);
const dongguan = draft.parseStoredAddress("广东省东莞市城区莞城街道 松山湖", regions, cities);
assert.strictEqual(dongguan.districtName, "城区");
assert.strictEqual(dongguan.streetName, "莞城街道");
assert.strictEqual(dongguan.detail, "松山湖");

const page = fs.readFileSync(path.join(__dirname, "../miniapp/pages/apply/index.js"), "utf8");
const wxml = fs.readFileSync(path.join(__dirname, "../miniapp/pages/apply/index.wxml"), "utf8");
const prefillStart = page.indexOf("async _prefillPersonal()");
const prefillEnd = page.indexOf("_applyResolvedPersonal(resolved, gen)");
const prefillFn = page.slice(prefillStart, prefillEnd);
assert.ok(prefillStart >= 0 && prefillEnd > prefillStart);
assert.ok(prefillFn.includes("/api/wechat/my-profile"));
assert.ok(prefillFn.includes("/api/wechat/tnr-prefill"));
assert.ok(prefillFn.includes("wxLoginCode"));
assert.ok(!prefillFn.includes("WECHAT_OPENID"));
assert.ok(!prefillFn.includes("openid"));
assert.ok(page.includes("idConsent: !!draft.idConsent"));
assert.ok(page.includes("patch.idConsent = false"));
assert.ok(page.includes("appendLimited"));
assert.ok(page.includes("missingMediaPrompt"));
assert.ok(page.includes("saveFile"));
assert.ok(page.includes("copyFile"));
assert.ok(page.includes("USER_DATA_PATH"));
const saveStart = page.indexOf("  _saveDraftNow() {");
const saveEnd = page.indexOf("  _persistMediaFile(");
const saveFn = page.slice(saveStart, saveEnd);
assert.ok(saveFn.includes("setStorageSync"));
assert.ok(saveFn.includes("DRAFT_KEY"));
assert.ok(!saveFn.includes("wx.request"));
assert.ok(!saveFn.includes("postJson"));
assert.ok(page.includes("onHide()"));
assert.ok(page.includes("onShow()"));
assert.ok(!page.includes("LEGACY_MINIAPP_COMPAT"));
assert.ok(wxml.includes("清空并重新填写"));
assert.ok(wxml.includes("只保存在这台手机"));
assert.ok(wxml.includes("身份证号"));
assert.ok(!/disabled="\{\{[^"]*prefill/.test(wxml));
assert.ok(wxml.includes('data-k="applicant_name"'));
assert.ok(!wxml.includes('data-k="applicant_name" disabled'));

console.log("tnr apply draft test passed");

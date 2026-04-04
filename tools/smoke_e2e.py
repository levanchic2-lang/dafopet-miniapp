import json
import time
import urllib.parse
import urllib.request


def _read_json(resp) -> dict:
    raw = resp.read()
    txt = raw.decode("utf-8", errors="replace")
    return json.loads(txt or "{}")


def http_get_json(base: str, path: str, timeout: int = 10) -> tuple[int, dict]:
    with urllib.request.urlopen(base + path, timeout=timeout) as resp:
        return resp.status, _read_json(resp)


def http_post_json(base: str, path: str, obj: dict, timeout: int = 10) -> tuple[int, dict]:
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, _read_json(resp)


def http_post_form(base: str, path: str, obj: dict, timeout: int = 15) -> tuple[int, dict]:
    data = urllib.parse.urlencode(obj).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, _read_json(resp)


def http_post_empty(base: str, path: str, timeout: int = 15) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, _read_json(resp)


def _expect_raises(label: str, fn) -> None:
    try:
        fn()
    except Exception as e:
        s = str(e).splitlines()[0] if e else "error"
        print(f"  OK (expected failure): {label}: {s}")
        return
    raise AssertionError(f"Expected failure but succeeded: {label}")


def main() -> None:
    base = "http://127.0.0.1:8765"
    print("== E2E smoke start ==")

    print("1) GET /api/wechat/config")
    code, cfg = http_get_json(base, "/api/wechat/config")
    print("  status", code, "keys", sorted(cfg.keys()))
    assert code == 200

    print("2) POST /api/wechat/my-apps (missing openid should fail)")
    _expect_raises("my-apps missing openid", lambda: http_post_json(base, "/api/wechat/my-apps", {}))

    print("3) POST /api/apply/create (fake openid)")
    fake_openid = "test_openid_" + str(int(time.time()))
    form = {
        "applicant_name": "测试用户",
        "phone": "13800138000",
        "address": "广东省深圳市南山区测试路1号",
        "clinic_store": "大风动物医院（横岗店）",
        "appointment_at": time.strftime("%Y-%m-%d"),
        "post_surgery_plan": "原地放归",
        "id_number": "11010519900101001X",
        "cat_nickname": "小灰",
        "cat_gender": "male",
        "age_estimate": "1岁",
        "health_note": "测试流浪状况",
        "wechat_openid": fake_openid,
        "agree_ear_tip": "true",
        "agree_no_pet_fraud": "true",
    }
    code, created = http_post_form(base, "/api/apply/create", form, timeout=20)
    app_id = created.get("id")
    print("  status", code, "id", app_id)
    assert code == 200 and app_id

    print("4) POST /api/apply/{id}/finalize (should fail: no images)")
    _expect_raises("finalize without images", lambda: http_post_empty(base, f"/api/apply/{app_id}/finalize", timeout=20))

    print("5) GET /api/app/{id}/status (keys present)")
    code, st = http_get_json(base, f"/api/app/{app_id}/status", timeout=10)
    print("  status", code, "keys", sorted(st.keys()))
    assert code == 200
    for k in ("cat_nickname", "cat_gender", "age_estimate", "health_note", "address"):
        assert k in st, f"missing {k} in status payload"

    print("6) POST /api/wechat/my-apps should include cat_nickname fields")
    code, mine = http_post_json(base, "/api/wechat/my-apps", {"openid": fake_openid}, timeout=10)
    items = mine.get("items") or []
    print("  status", code, "items", len(items))
    assert code == 200 and items
    assert items[0].get("cat_nickname") == "小灰"
    assert "health_note_brief" in items[0]

    print("7) POST /api/wechat/claim-apps (should update 0: already has openid)")
    code, claimed = http_post_json(
        base,
        "/api/wechat/claim-apps",
        {"openid": fake_openid, "phone": "13800138000", "id_number": "11010519900101001X"},
        timeout=10,
    )
    print("  status", code, "updated", claimed.get("updated"))
    assert code == 200

    print("== E2E smoke OK ==")


if __name__ == "__main__":
    main()


"""基于多模态模型的辅助判断：是否为疑似流浪猫。结论仅供医院参考，不可替代人工终审。"""
from __future__ import annotations

import base64
import json
import secrets
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings

STRAY_REVIEW_PROMPT = """你是动物医院 TNR 预审助手。先审核申请照片质量，再辅助判断猫咪更像「流浪/无主」还是「家养/有主」。只输出 JSON（无 markdown），结构必须如下：
{
  "evidence_quality_pass": true/false,
  "same_cat": true/false,
  "distinct_view_count": 2,
  "photo_assessments": [{"index":1,"usable":true,"view":"face|full_body|side|other","issues":[]}],
  "quality_flags": ["too_few_distinct_views|duplicate_images|blurry|too_dark|screenshot_or_document|cat_not_fully_visible|not_cat|different_cats"],
  "is_likely_stray": true/false,
  "confidence": 0.0~1.0,
  "reasons": ["要点1","要点2"],
  "key_evidence_photo_indexes": [1,2],
  "anti_fraud_flags": ["仅填写有明确证据的冒用、重复素材或身份不一致问题"],
  "caveats": ["不确定性/需现场核实"],
  "suggested_next_step": "auto_approve_candidate" 或 "manual_review"
}
规则：
1. 系统会说明前 N 张是申请人必须提交的原始照片；其后的图片仅是视频抽帧，不能替代原始照片。
2. 原始照片必须至少有 2 张可用、属于同一只猫且视角有实质区别，evidence_quality_pass 才能为 true。
3. 检查模糊、过暗、重复截图、不是猫、无法看到猫咪主体、不同猫混入等质量问题。
4. 项圈、航空箱或室内背景只能写入 caveats，不能单独作为反欺诈问题或自动拒绝依据。
5. 看不清、信息不足、照片质量未通过或无法确认是同一只猫时，suggested_next_step 必须为 manual_review。
6. 模型只做辅助预审，不自动拒绝；key_evidence_photo_indexes 按全部输入图片顺序从 1 开始。"""


def _manual_review_result(
    caveats: list[str],
    *,
    model: str | None = None,
    quality_flags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "evidence_quality_pass": False,
        "same_cat": None,
        "distinct_view_count": 0,
        "photo_assessments": [],
        "quality_flags": quality_flags or [],
        "is_likely_stray": None,
        "confidence": None,
        "reasons": [],
        "key_evidence_photo_indexes": [],
        "anti_fraud_flags": [],
        "caveats": caveats,
        "suggested_next_step": "manual_review",
        "model": model,
    }


def _encode_image_b64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode("ascii")


def _extract_video_frames(video_path: Path, tmp_dir: Path, max_frames: int = 3) -> list[Path]:
    try:
        import cv2
    except ImportError:
        return []
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    out: list[Path] = []
    if n <= 0:
        ret, frame = cap.read()
        if ret:
            p = tmp_dir / f"vf_{secrets.token_hex(4)}_0.jpg"
            cv2.imwrite(str(p), frame)
            out.append(p)
        cap.release()
        return out
    for i in range(max_frames):
        idx = int((i + 1) * n / (max_frames + 1))
        idx = min(max(idx, 0), n - 1)
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret:
            continue
        p = tmp_dir / f"vf_{secrets.token_hex(4)}_{i}.jpg"
        cv2.imwrite(str(p), frame)
        out.append(p)
    cap.release()
    return out


async def _call_chat_vision(
    paths: list[Path], application_photo_count: int
) -> tuple[str, str]:
    from openai import AsyncOpenAI

    base = (settings.openai_base_url or "").strip() or None
    client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base)
    prompt = (
        f"{STRAY_REVIEW_PROMPT}\n本次共输入 {len(paths)} 张图片；"
        f"前 {application_photo_count} 张是申请人提交的原始照片，"
        "其余（如有）是视频抽帧。"
    )
    content: list[dict] = [{"type": "text", "text": prompt}]
    for p in paths:
        mime = "image/jpeg"
        suf = p.suffix.lower()
        if suf == ".png":
            mime = "image/png"
        elif suf == ".webp":
            mime = "image/webp"
        b64 = _encode_image_b64(p)
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        )

    model = (getattr(settings, "tnr_vision_model", "") or "").strip() \
        or settings.openai_model
    resp = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=1100,
        temperature=0.2,
    )
    text = (resp.choices[0].message.content or "").strip()
    return text, model


async def review_application_media(
    image_paths: list[Path],
    video_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """对申请阶段图片与（可选）视频抽帧做视觉分析；无 API Key 时返回需人工复核。"""
    application_photos = [p for p in image_paths if p.is_file()][:5]
    if len(application_photos) < 2:
        return _manual_review_result(
            ["申请照片不足 2 张，视频不能替代申请照片，需补充后再审核。"],
            model=(getattr(settings, "tnr_vision_model", "") or "").strip() or settings.openai_model,
            quality_flags=["too_few_application_photos"],
        )

    if not settings.openai_api_key.strip():
        return _manual_review_result(
            ["未配置视觉模型密钥，已跳过自动识别，需人工审核。"]
        )

    video_paths = video_paths or []
    caveats: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        combined: list[Path] = list(application_photos)
        if video_paths:
            got_frame = False
            for vp in video_paths[:2]:
                frames = _extract_video_frames(vp, tdir, max_frames=3)
                if frames:
                    got_frame = True
                combined.extend(frames)
            combined = combined[:8]
            if not got_frame:
                caveats.append(
                    "未能从视频抽取有效画面（可安装 opencv-python-headless 或检查视频编码/mp4）；原视频已保存，请人工播放复核。"
                )
            else:
                caveats.append("含视频抽帧辅助判断，动态细节仍以原视频与现场为准。")

        try:
            text, model_used = await _call_chat_vision(
                combined, len(application_photos)
            )
        except Exception as exc:
            return _manual_review_result(
                caveats + [f"视觉模型调用失败，已转人工审核：{str(exc)[:240]}"],
                model=(getattr(settings, "tnr_vision_model", "") or "").strip() or settings.openai_model,
                quality_flags=["model_call_failed"],
            )

    try:
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _manual_review_result(
            ["模型返回格式异常，需人工审核。", text[:500]],
            model=model_used,
            quality_flags=["invalid_model_response"],
        )
    if not isinstance(data, dict):
        data = _manual_review_result(
            ["模型返回结构异常，需人工审核。", text[:500]],
            model=model_used,
            quality_flags=["invalid_model_response"],
        )
    for key in (
        "photo_assessments",
        "quality_flags",
        "reasons",
        "key_evidence_photo_indexes",
        "anti_fraud_flags",
        "caveats",
    ):
        if not isinstance(data.get(key), list):
            data[key] = []

    try:
        distinct_view_count = int(data.get("distinct_view_count") or 0)
    except (TypeError, ValueError):
        distinct_view_count = 0
    data["distinct_view_count"] = distinct_view_count
    if (
        data.get("evidence_quality_pass") is not True
        or data.get("same_cat") is not True
        or distinct_view_count < 2
    ):
        data["evidence_quality_pass"] = False
        data["suggested_next_step"] = "manual_review"
    if caveats:
        data.setdefault("caveats", [])
        for c in caveats:
            if c not in data["caveats"]:
                data["caveats"].append(c)
    data["model"] = model_used
    return data


def apply_auto_status_from_ai(result: dict[str, Any]) -> tuple[str, bool]:
    """返回 (新状态, 是否触发自动通过)."""
    if result.get("evidence_quality_pass") is not True:
        return "pending_manual", False
    if result.get("same_cat") is not True:
        return "pending_manual", False
    try:
        distinct_view_count = int(result.get("distinct_view_count") or 0)
    except (TypeError, ValueError):
        return "pending_manual", False
    if distinct_view_count < 2:
        return "pending_manual", False
    flags = result.get("anti_fraud_flags") or []
    if isinstance(flags, list) and len(flags) > 0:
        return "pending_manual", False
    if (result.get("suggested_next_step") or "").strip().lower() == "manual_review":
        return "pending_manual", False
    if result.get("is_likely_stray") is not True:
        return "pending_manual", False
    conf = float(result.get("confidence") or 0)
    if conf >= settings.stray_auto_approve_min_confidence:
        return "approved", True
    # 疑似流浪猫但置信度不够：进入“预通过”队列，方便医院优先人工复核
    return "pre_approved", False

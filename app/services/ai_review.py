"""基于多模态模型的辅助判断：是否为疑似流浪猫。结论仅供医院参考，不可替代人工终审。"""
from __future__ import annotations

import base64
import json
import secrets
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings

STRAY_REVIEW_PROMPT = """你是动物医院 TNR 预审助手。根据猫咪图片（可能含视频抽帧）判断更像「流浪/无主」还是「家养/有主」。只输出 JSON（无 markdown），结构必须如下：
{
  "is_likely_stray": true/false,
  "confidence": 0.0~1.0,
  "reasons": ["要点1","要点2"],
  "key_evidence_photo_indexes": [1,2],
  "anti_fraud_flags": ["collar","carrier","indoor_luxury"],
  "caveats": ["不确定性/需现场核实"],
  "suggested_next_step": "auto_approve_candidate" 或 "manual_review"
}
规则：anti_fraud_flags 非空或看不清/信息不足时，suggested_next_step=manual_review；key_evidence_photo_indexes 按图片顺序从 1 开始。"""


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


async def _call_chat_vision(paths: list[Path]) -> tuple[str, str]:
    from openai import AsyncOpenAI

    base = (settings.openai_base_url or "").strip() or None
    client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base)
    content: list[dict] = [{"type": "text", "text": STRAY_REVIEW_PROMPT}]
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

    resp = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[{"role": "user", "content": content}],
        max_tokens=600,
        temperature=0.2,
    )
    text = (resp.choices[0].message.content or "").strip()
    return text, settings.openai_model


async def review_application_media(
    image_paths: list[Path],
    video_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """对申请阶段图片与（可选）视频抽帧做视觉分析；无 API Key 时返回需人工复核。"""
    if not settings.openai_api_key.strip():
        return {
            "is_likely_stray": None,
            "confidence": None,
            "reasons": [],
            "caveats": ["未配置 OPENAI_API_KEY（或国内兼容端点用的密钥），已跳过自动识别，需人工审核。"],
            "suggested_next_step": "manual_review",
            "model": None,
        }

    video_paths = video_paths or []
    caveats: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        combined: list[Path] = list(image_paths[:5])
        if video_paths:
            got_frame = False
            for vp in video_paths[:2]:
                frames = _extract_video_frames(vp, tdir, max_frames=3)
                if frames:
                    got_frame = True
                combined.extend(frames)
            combined = combined[:6]
            if not got_frame:
                caveats.append(
                    "未能从视频抽取有效画面（可安装 opencv-python-headless 或检查视频编码/mp4）；原视频已保存，请人工播放复核。"
                )
            else:
                caveats.append("含视频抽帧辅助判断，动态细节仍以原视频与现场为准。")

        if not combined:
            return {
                "is_likely_stray": False,
                "confidence": 0.0,
                "reasons": [],
                "caveats": (caveats or []) + ["未提供可分析的申请照片，且视频无可用抽帧。"],
                "suggested_next_step": "manual_review",
                "model": settings.openai_model,
            }

        text, model_used = await _call_chat_vision(combined)

    try:
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {
            "is_likely_stray": False,
            "confidence": 0.0,
            "reasons": [],
            "caveats": ["模型返回非 JSON，需人工审核。", text[:500]],
            "suggested_next_step": "manual_review",
        }
    if caveats:
        data.setdefault("caveats", [])
        for c in caveats:
            if c not in data["caveats"]:
                data["caveats"].append(c)
    data["model"] = model_used
    return data


def apply_auto_status_from_ai(result: dict[str, Any]) -> tuple[str, bool]:
    """返回 (新状态, 是否触发自动通过)."""
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

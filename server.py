"""
Grok Imagine MCP server
-----------------------
Custom MCP connector ที่ห่อ xAI Imagine API (image + video)
ใช้กับ Claude / Cowork ผ่าน "Add custom connector"

Tools:
  - generate_image(prompt, aspect_ratio, resolution, n, model)
  - generate_video(prompt, duration, aspect_ratio, resolution, model)  # text->video
  - check_video(request_id)  # เช็คคลิปที่ยังเรนเดอร์ไม่เสร็จ

Env vars (ตั้งบน Render / โฮสต์):
  XAI_API_KEY       = คีย์จาก x.ai  (จำเป็น)
  CONNECTOR_SECRET  = สตริงลับสำหรับใส่ในพาธ URL (จำเป็น) เช่น  k9f2a...
  PORT              = พอร์ต (โฮสต์ส่วนใหญ่ตั้งให้อัตโนมัติ)
  IMAGE_MODEL       = ดีฟอลต์ grok-imagine-image-quality
  VIDEO_MODEL       = ดีฟอลต์ grok-imagine-video

URL ที่เอาไปใส่ใน Claude:
  https://<your-host>/<CONNECTOR_SECRET>/mcp
"""

import base64
import os
import time

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

XAI_API_KEY = os.environ.get("XAI_API_KEY", "").strip()
CONNECTOR_SECRET = os.environ.get("CONNECTOR_SECRET", "").strip()
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "grok-imagine-image-quality").strip()
VIDEO_MODEL = os.environ.get("VIDEO_MODEL", "grok-imagine-video").strip()
PORT = int(os.environ.get("PORT", "8000"))
XAI_BASE = "https://api.x.ai/v1"

# กันลืมตั้งค่า: ยอมให้สตาร์ทได้ แต่ tool จะเตือน
if not CONNECTOR_SECRET:
    CONNECTOR_SECRET = "CHANGE_ME"

mcp = FastMCP("grok-imagine", host="0.0.0.0", port=PORT)
# ซ่อน endpoint ไว้หลังพาธลับ -> /<secret>/mcp
mcp.settings.streamable_http_path = f"/{CONNECTOR_SECRET}/mcp"


def _headers() -> dict:
    if not XAI_API_KEY:
        raise ValueError("ยังไม่ได้ตั้ง XAI_API_KEY บนเซิร์ฟเวอร์")
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {XAI_API_KEY}",
    }


@mcp.tool()
def generate_image(
    prompt: str,
    aspect_ratio: str = "1:1",
    resolution: str = "1k",
    n: int = 1,
    model: str = "",
):
    """สร้างภาพจากข้อความ (text-to-image) ด้วย Grok Imagine แล้วส่งภาพกลับมาแสดงในแชท.

    Args:
        prompt: คำอธิบายภาพ (ภาษาอังกฤษให้ผลดีสุด)
        aspect_ratio: 1:1, 16:9, 9:16, 4:3, 3:4, 3:2, 2:3, 2:1, 1:2, auto
        resolution: 1k หรือ 2k
        n: จำนวนภาพ (1-10)
        model: เว้นว่างเพื่อใช้ดีฟอลต์ (grok-imagine-image-quality)
    """
    n = max(1, min(int(n), 10))
    payload = {
        "model": model.strip() or IMAGE_MODEL,
        "prompt": prompt,
        "n": n,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "response_format": "b64_json",
    }
    with httpx.Client(timeout=180) as client:
        r = client.post(f"{XAI_BASE}/images/generations", headers=_headers(), json=payload)
    if r.status_code >= 400:
        raise ValueError(f"xAI image error {r.status_code}: {r.text[:500]}")
    data = r.json().get("data", [])
    if not data:
        raise ValueError(f"ไม่มีภาพกลับมา: {r.text[:500]}")
    images: list[Image] = []
    for item in data:
        b64 = item.get("b64_json")
        if b64:
            images.append(Image(data=base64.b64decode(b64), format="jpeg"))
    if not images:
        raise ValueError("ตอบกลับไม่มี b64_json (อาจโดน moderation)")
    return images


@mcp.tool()
def generate_video(
    prompt: str,
    duration: int = 6,
    aspect_ratio: str = "9:16",
    resolution: str = "720p",
    model: str = "",
    wait_seconds: int = 240,
) -> str:
    """สร้างคลิปจากข้อความ (text-to-video) ด้วย Grok Imagine.

    คลิปใช้เวลาเรนเดอร์หลายนาที — ฟังก์ชันจะ poll จนเสร็จหรือครบ wait_seconds
    ถ้ายังไม่เสร็จจะคืน request_id ให้ไปเช็คต่อด้วย check_video.

    Args:
        prompt: คำอธิบายฉาก (อังกฤษดีสุด)
        duration: ความยาว 1-15 วินาที
        aspect_ratio: 9:16 (แนวตั้ง), 16:9, 1:1, 4:3, 3:4, 3:2, 2:3
        resolution: 720p หรือ 480p
        model: เว้นว่าง = ดีฟอลต์ (grok-imagine-video)
        wait_seconds: เวลารอสูงสุดก่อนคืน request_id (กัน connector timeout)
    """
    duration = max(1, min(int(duration), 15))
    payload = {
        "model": model.strip() or VIDEO_MODEL,
        "prompt": prompt,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    with httpx.Client(timeout=60) as client:
        r = client.post(f"{XAI_BASE}/videos/generations", headers=_headers(), json=payload)
        if r.status_code >= 400:
            raise ValueError(f"xAI video error {r.status_code}: {r.text[:500]}")
        request_id = r.json().get("request_id")
        if not request_id:
            raise ValueError(f"ไม่ได้ request_id: {r.text[:500]}")

        deadline = time.time() + max(10, int(wait_seconds))
        while time.time() < deadline:
            g = client.get(f"{XAI_BASE}/videos/{request_id}", headers=_headers())
            d = g.json()
            status = d.get("status")
            if status == "done":
                url = d.get("video", {}).get("url", "")
                return f"✅ คลิปเสร็จแล้ว ({duration}s, {aspect_ratio}, {resolution})\nURL (ลิงก์ชั่วคราว โหลดเก็บไว): {url}"
            if status in ("failed", "expired"):
                err = d.get("error", {})
                return f"❌ สร้างคลิปไม่สำเร็จ ({status}): {err}"
            time.sleep(5)

    return (
        f"⏳ คลิปยังเรนเดอร์อยู่ request_id = {request_id}\n"
        f"เรียก check_video(\"{request_id}\") อีกครั้งในอีกสักครู่เพื่อรับ URL"
    )


@mcp.tool()
def check_video(request_id: str) -> str:
    """เช็คสถานะ/รับ URL ของคลิปที่สั่งไว้ก่อนหน้า ด้วย request_id."""
    with httpx.Client(timeout=60) as client:
        g = client.get(f"{XAI_BASE}/videos/{request_id}", headers=_headers())
    if g.status_code >= 400:
        raise ValueError(f"xAI error {g.status_code}: {g.text[:500]}")
    d = g.json()
    status = d.get("status")
    if status == "done":
        url = d.get("video", {}).get("url", "")
        return f"✅ เสร็จแล้ว URL: {url}"
    if status in ("failed", "expired"):
        return f"❌ {status}: {d.get('error', {})}"
    return f"⏳ ยังเรนเดอร์อยู่ (status={status}) ลองเช็คใหม่อีกครั้ง"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

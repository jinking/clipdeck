from __future__ import annotations

import base64
import logging
from typing import Any
import httpx

logger = logging.getLogger(__name__)


class ImageOCRExtractor:
    """Multimodal VLM-based OCR and poster information extractor."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.minimaxi.com/v1",
        model: str = "MiniMax-Text-01",
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def extract_text(self, image_data: bytes, mime_type: str | None = None) -> tuple[bool, str]:
        """Extract text / table / poster information from image bytes using VLM.

        Returns (has_text, extracted_markdown).
        """
        if not self.api_key or not image_data:
            return False, ""

        # Skip tiny icons, tracking pixels, or thumbnails (< 5KB)
        if len(image_data) < 5120:
            return False, ""

        resolved_mime = mime_type or "image/jpeg"
        if ";" in resolved_mime:
            resolved_mime = resolved_mime.split(";", 1)[0].strip()
        if resolved_mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            resolved_mime = "image/jpeg"

        b64_image = base64.b64encode(image_data).decode("utf-8")
        prompt = """你是一个高精度的科技文献与临床招募图文 OCR 提取助手。
请识别并提取这张图片（如招募海报、架构图、表格、通知长图）中的所有有价值文字与数据信息。

【硬性要求】：
1. 结构化还原：保持原图的标题、章节小标、招募条件（入组/排他标准）、试验分期、研究机构与医院、联系方式等逻辑结构。
2. 绝对真实：严格识别图内文字，不添加任何推测或虚构内容。
3. 无文字过滤：如果该图片是纯照片、纯示意插画、风景图、LOGO 或装饰图标（无任何有意义的文字），请务必且仅回复五个字：【无文字内容】。
4. 输出格式：直接输出提取后的 Markdown 文本内容，不要输出任何开场白或解释。
"""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{resolved_mime};base64,{b64_image}"
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 3000,
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                transport=self.transport,
                trust_env=False,
            ) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code != 200:
                    logger.warning("Image OCR API returned status %s: %s", resp.status_code, resp.text[:200])
                    return False, ""

                data: dict[str, Any] = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    return False, ""

                content: str = choices[0].get("message", {}).get("content", "").strip()
                if not content or "无文字内容" in content or len(content) < 10:
                    return False, ""

                # Remove wrapping markdown code fences if model accidentally emitted them
                if content.startswith("```markdown"):
                    content = content.removeprefix("```markdown").removesuffix("```").strip()
                elif content.startswith("```"):
                    content = content.removeprefix("```").removesuffix("```").strip()

                return True, content
        except Exception as exc:
            logger.warning("Image OCR extraction failed with error: %s", exc)
            return False, ""

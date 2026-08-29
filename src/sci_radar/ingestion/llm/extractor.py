from __future__ import annotations

import logging
import re
from typing import Any
from bs4 import BeautifulSoup
import httpx

logger = logging.getLogger(__name__)


class LLMArticleExtractor:
    """LLM-based web article extractor for pristine Markdown generation."""

    provider_name = "openai-compatible"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.minimaxi.com/v1",
        model: str = "MiniMax-Text-01",
        timeout_seconds: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def extract(self, html_content: str | bytes, *, url: str | None = None) -> tuple[bool, str]:
        """Extract core article markdown using LLM.

        Returns (success, markdown_text).
        """
        if not self.api_key:
            return False, ""

        if isinstance(html_content, bytes):
            html_text = html_content.decode("utf-8", errors="replace")
        else:
            html_text = str(html_content)

        # 1. Pre-clean heavy non-content tags to save token window
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
            tag.decompose()

        body = soup.find("body") or soup
        text_content = body.get_text(separator="\n", strip=True)

        # If body is tiny or empty, skip LLM
        if len(text_content) < 40:
            return False, ""

        # Truncate input if excessively long (e.g. > 30k chars) to prevent context limit errors
        trimmed_input = text_content[:30000]

        prompt = f"""你是一个专业的学术与科技文献正文提取引擎。
请从以下网页文本中提取完整的核心新闻/论文/科技报道正文，并输出为纯净、格式规整的标准 Markdown。

【硬性约束】：
1. 完整提取：文章标题（使用一级标题 #）、发布日期、作者/来源机构、小标题（## / ###）、正文段落、关键数据与列表。
2. 彻底剔除：导航菜单、顶部横幅、页脚版权声明、免责条款、社交分享引导、股票行情数据、无关广告与侧边推荐等一切网页噪音。
3. 事实忠实：必须 100% 严格忠实于原文，绝不进行主观总结、夸大、删减或改写核心事实段落。
4. 输出格式：直接输出 Markdown 正文文本。严禁使用 ```markdown ``` 代码块包裹，严禁输出任何问候语、开场白或思考过程。

【待提取网页文本】：
{trimmed_input}
"""

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
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
                    logger.warning("LLM extraction API returned status %s: %s", resp.status_code, resp.text[:200])
                    return False, ""

                data: dict[str, Any] = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    return False, ""

                message_content = choices[0].get("message", {}).get("content", "").strip()

                # Clean any thinking tags like <think>...</think> if model output contains it
                cleaned_md = re.sub(r"(?s)<think>.*?</think>", "", message_content).strip()

                # Clean markdown wrapper if any
                if cleaned_md.startswith("```markdown"):
                    cleaned_md = cleaned_md.removeprefix("```markdown").removesuffix("```").strip()
                elif cleaned_md.startswith("```"):
                    cleaned_md = cleaned_md.removeprefix("```").removesuffix("```").strip()

                if len(cleaned_md) >= 50:
                    return True, cleaned_md + "\n"
                return False, ""
        except Exception as exc:
            logger.warning("LLM extraction failed: %s", exc)
            return False, ""

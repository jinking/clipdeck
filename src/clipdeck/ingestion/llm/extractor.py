from __future__ import annotations

import logging
import re
from typing import Any
from bs4 import BeautifulSoup
import httpx

from clipdeck.ingestion.site_profiles import prompt_template

logger = logging.getLogger(__name__)


def prune_high_link_density_elements(soup: BeautifulSoup, density_threshold: float = 0.5) -> None:
    """方案一：链接密度过滤（Text-to-Link Density Ratio Pruning）。

    在 DOM 层面剔除超链接占比过高的容器（典型的导航条、相关推荐列表、热搜排行、页脚友链），
    无需消耗任何大模型 Token。
    """
    # 1. 先清除显式已知的噪音标签与类名
    explicit_noise_selectors = [
        "header", "footer", "nav", "aside",
        "[class*='footer']", "[class*='header']", "[class*='nav']", "[class*='sidebar']",
        "[class*='share']", "[class*='copyright']", "[class*='menu']", "[class*='comment']",
        "[class*='recommend']", "[class*='relate']", "[class*='advertisement']",
        "#footer", "#header", "#sidebar",
    ]
    for sel in explicit_noise_selectors:
        for el in soup.select(sel):
            el.decompose()

    # 2. 启发式链接密度剪枝
    candidate_tags = ["nav", "ul", "ol", "menu", "aside", "div", "section"]
    to_decompose = []
    for tag in soup.find_all(candidate_tags):
        tag_id = tag.get("id") or ""
        tag_class = " ".join(tag.get("class") or [])
        combined_attrs = f"{tag_id} {tag_class}".lower()

        # 保护明显包含核心正文特征的容器
        if any(core in combined_attrs for core in ["article", "content", "main", "body", "post", "detail", "zoom"]):
            continue

        total_text = tag.get_text(strip=True)
        if len(total_text) < 6:
            continue

        link_text = "".join(a.get_text(strip=True) for a in tag.find_all("a"))
        if not link_text:
            continue

        density = len(link_text) / len(total_text)
        if density >= density_threshold:
            to_decompose.append(tag)

    for tag in to_decompose:
        tag.decompose()


def find_candidate_main_content(soup: BeautifulSoup) -> Any:
    """方案二：正文候选区聚焦（Candidate Main Content Targeting）。

    1. 优先根据微信与主流 CMS 经典正文特征定位；
    2. 若未命中，通过段落加权与链接惩罚打分，选取最可能是正文主体的容器；
    3. 安全兜底：如果候选容器文本过短（< 100 字符），回退 None。
    """
    specific_selectors = [
        "#js_content",  # 微信公众号
        "article",
        "main",
        "[role='main']",
        "#ContentBody", "#content", "#main-content", "#article-content", "#article_content",
        ".article-content", ".article-body", ".post-content", ".entry-content",
        ".detail-content", ".content-main", ".news-content", ".main_content",
    ]
    for sel in specific_selectors:
        found = soup.select_one(sel)
        if found:
            text = found.get_text(strip=True)
            if len(text) >= 120:
                return found

    body = soup.find("body") or soup
    best_candidate = None
    best_score = 0

    for container in body.find_all(["div", "section"]):
        text = container.get_text(strip=True)
        if len(text) < 100:
            continue

        paragraphs = container.find_all("p")
        # 打分：文本长度 + 段落数量加权 (每段+40分)
        score = len(text) + len(paragraphs) * 40
        # 链接惩罚：链接字数越多越不可能是纯正文主体
        links = container.find_all("a")
        link_len = sum(len(a.get_text(strip=True)) for a in links)
        score -= link_len * 2

        if score > best_score:
            best_score = score
            best_candidate = container

    if best_candidate and len(best_candidate.get_text(strip=True)) >= 100:
        return best_candidate

    return None


def prepare_focused_article_text(html_text: str) -> str:
    """结合方案一与方案二，将复杂 HTML 转化为紧凑、聚焦的核心正文输入。"""
    soup = BeautifulSoup(html_text, "html.parser")

    # 1. 基础垃圾标签移除
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()

    # 2. 提取候选标题（避免标题处于正文容器外导致丢失）
    candidate_title = ""
    h1 = soup.find("h1")
    if h1 and len(h1.get_text(strip=True)) >= 2:
        candidate_title = h1.get_text(strip=True)
    elif soup.title and len(soup.title.get_text(strip=True)) >= 2:
        candidate_title = soup.title.get_text(strip=True)

    # 3. 方案一：高链接密度节点剪枝
    prune_high_link_density_elements(soup, density_threshold=0.5)

    # 4. 方案二：正文候选区定位
    target = find_candidate_main_content(soup)
    if target is None:
        target = soup.find("body") or soup

    focused_text = target.get_text(separator="\n", strip=True)

    # 如果提取出的正文不包含标题，主动在头部补充
    if candidate_title and candidate_title not in focused_text:
        return f"【页面标题】：{candidate_title}\n\n{focused_text}"
    return focused_text


class LLMArticleExtractor:
    """LLM-based web article extractor for pristine Markdown generation."""

    provider_name = "openai-compatible"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.minimaxi.com/v1",
        model: str = "MiniMax-M3",
        timeout_seconds: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
        thinking_mode: str | None = "disabled",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.thinking_mode = thinking_mode

    DEFAULT_PROMPT_TEMPLATE = """你是一个专业的网页正文提取引擎。
请从以下网页文本中提取完整的核心文章正文（新闻、博客、报告、论文报道等任何类型的公开网页内容），并输出为纯净、格式规整的标准 Markdown。

【硬性约束】：
1. 完整提取：文章标题（使用一级标题 #）、发布日期、作者/来源机构、小标题（## / ###）、正文段落、关键数据与列表。
2. 彻底剔除：导航菜单、顶部横幅、页脚版权声明、免责条款、社交分享引导、无关广告与侧边推荐等一切网页噪音。
3. 事实忠实：必须 100% 严格忠实于原文，绝不进行主观总结、夸大、删减或改写核心事实段落。
4. 输出格式：直接输出 Markdown 正文文本。严禁使用 ```markdown ``` 代码块包裹，严禁输出任何问候语、开场白或思考过程。

【待提取网页文本】：
{content}
"""

    def _build_prompt(self, content: str) -> str:
        """Use the configured custom template when present, else the default."""
        custom = prompt_template("llm_extract")
        template = custom if custom is not None else self.DEFAULT_PROMPT_TEMPLATE
        return template.replace("{content}", content)

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

        # 方案一 + 方案二：前置纯算法剪枝与正文聚焦，大幅节省 Token 并提升提取质量
        text_content = prepare_focused_article_text(html_text)

        # If body is tiny or empty, skip LLM
        if len(text_content.strip()) < 40:
            return False, ""

        # Truncate input if excessively long (e.g. > 30k chars) to prevent context limit errors
        trimmed_input = text_content[:30000]

        prompt = self._build_prompt(trimmed_input)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }
        if self.thinking_mode:
            payload["thinking"] = {"type": self.thinking_mode}

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

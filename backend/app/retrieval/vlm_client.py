"""
Vision Language Model (VLM) Client - 视觉语言模型客户端

用于多模态 RAG 的图片理解阶段：
- 给图片生成中文 caption（用作检索文本）
- 提取图片中的关键词（增强 BM25 召回）
- 表格/图表/截图/流程图等不同类型图片的语义描述

设计原则：
1. 复用现有 OpenAI-compatible 协议（与 ai_service.py 一致）
2. 支持多 provider：OpenAI (gpt-4o-mini) / DashScope (qwen-vl-max) / 智谱 (glm-4v)
3. 失败降级：API 失败时返回空 caption，让上层用 OCR 文本兜底
4. 默认关闭：通过 settings.MULTIMODAL_ENABLED 控制是否启用
"""

import base64
import json
from abc import ABC, abstractmethod
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from loguru import logger

import httpx

from ..core.config import settings


# 用于图片理解的统一 prompt（中文友好，强调检索可用性）
_DEFAULT_VISION_PROMPT = """请仔细分析这张图片，输出 JSON 格式结果（不要 markdown 代码块），字段：
{
  "caption": "图片内容的中文详细描述，50-150字。包含：图片类型（流程图/架构图/截图/表格/图表等）、核心内容、关键信息、与其他元素的关系（如有）",
  "keywords": ["关键词1", "关键词2", "关键词3"],
  "image_type": "diagram|screenshot|chart|table|formula|photo|other"
}

要求：
1. caption 要包含足够的信息量，能独立支撑后续检索
2. keywords 3-5 个，是图片核心语义
3. 不要输出 JSON 以外的内容"""


class VLMProvider(ABC):
    """VLM 提供商抽象基类"""

    @abstractmethod
    async def describe_image(self, image_bytes: bytes, mime_type: str = "image/png") -> Dict:
        """
        请求 VLM 描述图片

        Args:
            image_bytes: 图片二进制
            mime_type: MIME 类型

        Returns:
            {
                "caption": str,
                "keywords": List[str],
                "image_type": str,
                "raw_response": str,
            }
        """
        pass


class OpenAICompatibleVLM(VLMProvider):
    """
    OpenAI 兼容协议的 VLM 客户端

    适用于：
    - OpenAI gpt-4o, gpt-4o-mini
    - DashScope (阿里) qwen-vl-max, qwen-vl-plus
    - 智谱 glm-4v, glm-4v-flash
    - 其他兼容 OpenAI Vision API 的服务
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        prompt: str = _DEFAULT_VISION_PROMPT,
        max_tokens: int = 600,
        timeout: float = 60.0,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.prompt = prompt
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )
        return self._client

    async def describe_image(self, image_bytes: bytes, mime_type: str = "image/png") -> Dict:
        """调用 VLM API 描述图片"""
        # base64 编码
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                }
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}/chat/completions"
        client = await self._get_client()

        try:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return self._parse_response(content)
        except httpx.HTTPStatusError as e:
            logger.warning(
                f"VLM API HTTP 错误 status={e.response.status_code} model={self.model}: "
                f"{e.response.text[:200]}"
            )
            return self._fallback(content=str(e))
        except Exception as e:
            logger.warning(f"VLM 调用失败 model={self.model}: {e}")
            return self._fallback(content=str(e))

    @staticmethod
    def _parse_response(content: str) -> Dict:
        """解析 VLM 返回，容错处理"""
        result = {
            "caption": "",
            "keywords": [],
            "image_type": "other",
            "raw_response": content,
        }

        if not content:
            return result

        # 尝试解析 JSON（可能包裹在 ```json ... ``` 中）
        text = content.strip()
        if text.startswith("```"):
            # 去掉 markdown 代码块
            lines = text.split("\n")
            json_lines = [
                ln for ln in lines
                if not ln.strip().startswith("```") and ln.strip()
            ]
            text = "\n".join(json_lines)

        try:
            parsed = json.loads(text)
            result["caption"] = str(parsed.get("caption", "")).strip()
            kws = parsed.get("keywords", [])
            if isinstance(kws, list):
                result["keywords"] = [str(k) for k in kws][:8]
            result["image_type"] = str(parsed.get("image_type", "other")).strip().lower()
        except (json.JSONDecodeError, ValueError):
            # JSON 解析失败：把整段内容当作 caption
            result["caption"] = content.strip()[:500]
            logger.debug("VLM 返回非 JSON 格式，整段作为 caption")

        return result

    @staticmethod
    def _fallback(content: str = "") -> Dict:
        """失败降级：返回空 caption，上层走 OCR 兜底"""
        return {
            "caption": "",
            "keywords": [],
            "image_type": "other",
            "raw_response": content,
        }

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ========== Provider 工厂 ==========

# 各 provider 的默认 VLM 模型与 base_url
_VLM_PROVIDER_DEFAULTS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "qwen": {
        # DashScope 兼容模式
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-max",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4v-flash",
    },
    "deepseek": {
        # DeepSeek 当前无 vision 模型，仅占位
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    },
}


def create_vlm_provider(
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Optional[VLMProvider]:
    """
    创建 VLM 客户端

    优先级：显式参数 > settings > provider 默认值

    Args:
        provider: "openai" | "qwen" | "zhipu" | None（None 时从 settings.VLM_PROVIDER 推断）
        api_key: API Key（None 时从 settings.VLM_API_KEY 或 settings.AI_API_KEY 推断）
        model: 模型名
        base_url: API base URL

    Returns:
        VLMProvider 实例，或 None（未配置 API Key 时）
    """
    provider = (provider or getattr(settings, "VLM_PROVIDER", "") or "").lower()
    if not provider:
        # 默认从 AI_MODEL 推断（如果 AI_MODEL 是 vision 模型）
        ai_model = (getattr(settings, "AI_MODEL", "") or "").lower()
        if "gpt-4o" in ai_model or "vl" in ai_model or "vision" in ai_model or "4v" in ai_model:
            # 解析 provider/model
            if "/" in ai_model:
                provider = ai_model.split("/", 1)[0]
            else:
                provider = "openai"
        else:
            provider = "openai"  # 默认 OpenAI

    defaults = _VLM_PROVIDER_DEFAULTS.get(provider, _VLM_PROVIDER_DEFAULTS["openai"])

    # API Key 优先级：显式 > VLM_API_KEY > AI_API_KEY
    api_key = api_key or getattr(settings, "VLM_API_KEY", None) or getattr(settings, "AI_API_KEY", None)
    if not api_key:
        logger.warning(f"VLM 未配置 API Key（provider={provider}），将无法调用")
        return None

    # 模型优先级：显式 > VLM_MODEL > provider 默认
    model = (
        model
        or getattr(settings, "VLM_MODEL", None)
        or defaults["model"]
    )

    # base_url 优先级：显式 > VLM_BASE_URL > AI_BASE_URL > provider 默认
    base_url = (
        base_url
        or getattr(settings, "VLM_BASE_URL", None)
        or getattr(settings, "AI_BASE_URL", None)
        or defaults["base_url"]
    )

    logger.info(f"创建 VLM 客户端: provider={provider} model={model} base_url={base_url}")
    return OpenAICompatibleVLM(
        api_key=api_key,
        model=model,
        base_url=base_url,
    )


# 模块级单例
_vlm_provider: Optional[VLMProvider] = None


def get_vlm_provider() -> Optional[VLMProvider]:
    """获取全局 VLM 单例（懒加载）"""
    global _vlm_provider
    if _vlm_provider is None:
        if not getattr(settings, "MULTIMODAL_ENABLED", False):
            return None
        _vlm_provider = create_vlm_provider()
    return _vlm_provider


async def close_vlm_provider():
    """关闭全局 VLM 单例（应用退出时调用）"""
    global _vlm_provider
    if _vlm_provider is not None:
        if isinstance(_vlm_provider, OpenAICompatibleVLM):
            await _vlm_provider.close()
        _vlm_provider = None

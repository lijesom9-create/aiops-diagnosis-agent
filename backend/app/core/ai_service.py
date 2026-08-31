"""
AI模型服务模块
支持多个AI提供商，统一接口
"""

import json
from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from .config import settings


class AIModelProvider(ABC):
    """AI模型提供商抽象基类"""

    @abstractmethod
    async def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None) -> Dict:
        """发送聊天请求"""
        pass

    @abstractmethod
    async def chat_stream(self, messages: List[Dict]) -> AsyncIterator[str]:
        """流式聊天"""
        pass


# 提供商默认配置
PROVIDER_CONFIGS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat"
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash"
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini"
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-sonnet-4-20250514"
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-turbo"
    }
}


def parse_model_name(model_name: str) -> Tuple[str, str]:
    """
    解析模型名称
    格式：provider/model 或 model

    返回：(provider, model)
    """
    if "/" in model_name:
        parts = model_name.split("/", 1)
        return parts[0], parts[1]

    # 如果没有指定提供商，尝试从模型名称推断
    model_lower = model_name.lower()
    if "deepseek" in model_lower:
        return "deepseek", model_name
    elif "glm" in model_lower or "zhipu" in model_lower:
        return "zhipu", model_name
    elif "gpt" in model_lower or "o1" in model_lower or "o3" in model_lower:
        return "openai", model_name
    elif "claude" in model_lower:
        return "anthropic", model_name
    elif "qwen" in model_lower:
        return "qwen", model_name
    else:
        # 默认使用deepseek
        return "deepseek", model_name


class OpenAICompatibleProvider(AIModelProvider):
    """OpenAI兼容API提供商（适用于DeepSeek、智谱、OpenAI、通义千问等）"""

    def __init__(self, api_key: str, model: str, base_url: str):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.max_retries = 3
        self.timeout = 120.0  # 增加超时到120秒
        self._client: Optional[httpx.AsyncClient] = None

        # 熔断器 + 限流器（按 model 名隔离，防止 DeepSeek 故障级联到其他 provider）
        from .circuit_breaker import get_breaker, get_limiter
        self._breaker = get_breaker(f"llm:{model}")
        self._limiter = get_limiter(f"llm:{model}")

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端（复用连接池）"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20
                )
            )
        return self._client

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None) -> Dict:
        """发送聊天请求，带重试机制 + 熔断限流

        熔断器在重试外层：整个重试流程都失败才算 1 次熔断失败
        （重试本身已经处理了瞬时抖动，熔断器关注持续性故障）
        """
        from .circuit_breaker import ResilienceContext

        async with ResilienceContext(self._breaker, self._limiter):
            return await self._chat_with_retry(messages, tools)

    async def _chat_with_retry(self, messages: List[Dict], tools: Optional[List[Dict]] = None) -> Dict:
        """内部：带重试的 chat 调用（不含熔断限流）"""
        last_error = None
        for attempt in range(self.max_retries):
            try:
                client = await self._get_client()
                payload = {
                    "model": self.model,
                    "messages": messages,
                }

                if tools:
                    payload["tools"] = tools

                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    }
                )

                if response.status_code != 200:
                    raise Exception(f"API请求失败: {response.text}")

                data = response.json()
                return {
                    "content": data["choices"][0]["message"]["content"],
                    "tool_calls": data["choices"][0]["message"].get("tool_calls"),
                    "finish_reason": data["choices"][0]["finish_reason"]
                }

            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    logger.warning(f"AI请求失败，重试 {attempt + 1}/{self.max_retries}: {type(e).__name__}: {str(e)[:200]}")
                    import asyncio
                    await asyncio.sleep(1 * (attempt + 1))  # 递增延迟

        raise Exception(f"AI请求失败（已重试{self.max_retries}次）: {type(last_error).__name__}: {str(last_error)[:200]}")

    async def chat_stream(self, messages: List[Dict]) -> AsyncIterator[str]:
        """流式聊天，带重试机制 + 熔断限流

        熔断器只在连接阶段生效：一旦开始流式输出就视为成功
        （流式传输中断视为业务失败，由调用方处理）
        """
        from .circuit_breaker import CircuitOpenError, RateLimitExceededError

        # 1. 熔断+限流检查（在生成第一个 token 之前）
        await self._limiter.acquire()
        await self._breaker.acquire()
        _success_recorded = False

        try:
            async for chunk in self._chat_stream_with_retry(messages):
                if not _success_recorded:
                    # 第一个 chunk 成功输出 → 视为调用成功
                    await self._breaker.record_success()
                    _success_recorded = True
                yield chunk
            # 全部流式输出完成
            if not _success_recorded:
                await self._breaker.record_success()
                _success_recorded = True
        except CircuitOpenError:
            raise  # 熔断自身异常直接传播
        except RateLimitExceededError:
            raise  # 限流自身异常直接传播
        except Exception:
            # 流式过程中失败且未记录过成功 → 记录熔断失败
            if not _success_recorded:
                await self._breaker.record_failure()
            raise

    async def _chat_stream_with_retry(self, messages: List[Dict]) -> AsyncIterator[str]:
        """内部：带重试的流式 chat 调用（不含熔断限流）"""
        last_error = None
        for attempt in range(self.max_retries):
            try:
                client = await self._get_client()
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": True
                    },
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    timeout=self.timeout
                ) as response:
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data != "[DONE]":
                                chunk = json.loads(data)
                                if "choices" in chunk and len(chunk["choices"]) > 0:
                                    delta = chunk["choices"][0].get("delta", {})
                                    content = delta.get("content")
                                    if content:
                                        yield content
                # 成功完成，直接返回
                return

            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    logger.warning(f"AI流式请求失败，重试 {attempt + 1}/{self.max_retries}: {e}")
                    import asyncio
                    await asyncio.sleep(1 * (attempt + 1))

        raise Exception(f"AI流式请求失败（已重试{self.max_retries}次）: {last_error}")


class AnthropicProvider(AIModelProvider):
    """Anthropic Claude提供商"""

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self._client = None

    def _get_client(self):
        """获取或创建 Anthropic 客户端（复用连接）"""
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def close(self):
        """关闭客户端"""
        if self._client:
            await self._client.close()
            self._client = None

    async def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None) -> Dict:
        """发送聊天请求"""

        try:
            client = self._get_client()

            # 转换消息格式
            system_message = None
            chat_messages = []

            for msg in messages:
                if msg["role"] == "system":
                    system_message = msg["content"]
                else:
                    chat_messages.append(msg)

            kwargs = {
                "model": self.model,
                "max_tokens": 2048,
                "messages": chat_messages,
            }

            if system_message:
                kwargs["system"] = system_message

            if tools:
                kwargs["tools"] = tools

            response = await client.messages.create(**kwargs)

            # 提取内容
            content = ""
            tool_calls = None

            for block in response.content:
                if block.type == "text":
                    content += block.text
                elif block.type == "tool_use":
                    if tool_calls is None:
                        tool_calls = []
                    tool_calls.append({
                        "id": block.id,
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input)
                        }
                    })

            return {
                "content": content,
                "tool_calls": tool_calls,
                "finish_reason": response.stop_reason
            }

        except ImportError:
            raise Exception("请安装anthropic库: pip install anthropic")

    async def chat_stream(self, messages: List[Dict]) -> AsyncIterator[str]:
        """流式聊天"""

        try:
            client = self._get_client()

            system_message = None
            chat_messages = []

            for msg in messages:
                if msg["role"] == "system":
                    system_message = msg["content"]
                else:
                    chat_messages.append(msg)

            kwargs = {
                "model": self.model,
                "max_tokens": 2048,
                "messages": chat_messages,
            }

            if system_message:
                kwargs["system"] = system_message

            async with client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text

        except ImportError:
            raise Exception("请安装anthropic库: pip install anthropic")


class MockProvider(AIModelProvider):
    """模拟提供商（用于开发测试）"""

    async def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None) -> Dict:
        """模拟聊天"""

        last_message = messages[-1]["content"] if messages else ""

        # 简单的回复逻辑
        if "你好" in last_message or "hello" in last_message.lower():
            response = "你好！我是你的教育助手，很高兴为你服务。有什么我可以帮助你的吗？"
        elif "题目" in last_message or "练习" in last_message:
            response = "好的，我来为你生成一道练习题。\n\n**题目**：下列哪个是Python的数据类型？\nA. int\nB. string\nC. number\nD. char\n\n**答案**：A\n\n**解析**：Python中整数类型用int表示，字符串类型用str表示。"
        elif "进度" in last_message:
            response = "根据你的学习记录，你目前的学习进度如下：\n\n- Python基础：85%\n- 数据结构：60%\n- 算法：40%\n\n建议你继续学习算法部分，这对你提升编程能力很有帮助。"
        else:
            response = f"我收到了你的消息：{last_message}\n\n作为教育助手，我可以帮你：\n1. 生成练习题\n2. 解答问题\n3. 查看学习进度\n4. 提供学习建议\n\n请问有什么具体需要帮助的吗？"

        return {
            "content": response,
            "tool_calls": None,
            "finish_reason": "end_turn"
        }

    async def chat_stream(self, messages: List[Dict]) -> AsyncIterator[str]:
        """模拟流式聊天"""
        import asyncio

        response = (await self.chat(messages))["content"]

        # 模拟逐字输出
        for char in response:
            yield char
            await asyncio.sleep(0.02)


def create_ai_provider() -> AIModelProvider:
    """创建AI模型提供商"""

    # 检查是否配置了API密钥
    if not settings.AI_API_KEY:
        logger.warning("未配置AI_API_KEY，使用模拟模式")
        return MockProvider()

    # 解析模型名称
    provider_name, model_name = parse_model_name(settings.AI_MODEL)
    logger.info(f"使用AI提供商: {provider_name}, 模型: {model_name}")

    # 获取提供商配置
    provider_config = PROVIDER_CONFIGS.get(provider_name)
    if not provider_config:
        logger.warning(f"未知的提供商: {provider_name}，使用模拟模式")
        return MockProvider()

    # 确定base_url
    base_url = settings.AI_BASE_URL or provider_config["base_url"]

    # 根据提供商创建实例
    if provider_name == "anthropic":
        return AnthropicProvider(settings.AI_API_KEY, model_name)
    else:
        # DeepSeek、智谱、OpenAI、通义千问都使用OpenAI兼容API
        return OpenAICompatibleProvider(settings.AI_API_KEY, model_name, base_url)


# 全局AI服务实例
ai_service = create_ai_provider()

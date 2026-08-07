"""
CLIP Embedder - 图文对齐向量嵌入

为多模态 RAG 提供图像/文本的统一向量空间：
- 图像子块用 CLIP image encoder 生成图像向量，入独立向量库
- 检索时用户 query 用 CLIP text encoder 生成文本向量，查图像库
- 与 BGE 文本向量库的结果做 RRF 融合

与 VLM caption 的关系：
- VLM caption：图片 → 文本描述 → BGE 文本向量 → 文本库（保留语义供 LLM 阅读）
- CLIP 向量：图片 → CLIP 图像向量 → 图像库（保留视觉特征供向量召回）
- 两者互补：CLIP 能匹配 caption 难以描述的视觉特征（颜色/布局/形状）

模型选择：
- OFA-Sys/chinese-clip-vit-base-patch16：中文场景首选（项目默认）
- openai/clip-vit-base-patch32：英文场景经典模型

优雅降级：
- MULTIMODAL_VECTOR_ENABLED=False：不加载模型，所有接口返回 None
- 模型未下载/加载失败：记录日志，返回 None，上层跳过 CLIP 检索
"""
import os
from typing import Optional, List, Tuple
from loguru import logger


# 在 import transformers 之前就启用离线模式（与 embeddings.py 一致）
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


def _extract_tensor(features):
    """
    从 model.get_text_features/get_image_features 的返回中提取 tensor

    兼容性处理：
    - 标准 CLIPModel：返回 tensor
    - ChineseCLIPModel（某些 transformers 版本）：返回 BaseModelOutputWithPooling
      需要取 .pooler_output 或 .last_hidden_state[:, 0, :]
    """
    import torch

    if isinstance(features, torch.Tensor):
        return features

    # BaseModelOutputWithPooling
    if hasattr(features, "pooler_output") and features.pooler_output is not None:
        return features.pooler_output
    if hasattr(features, "last_hidden_state"):
        # CLS token pooling
        return features.last_hidden_state[:, 0, :]

    raise TypeError(f"无法从 {type(features).__name__} 提取 tensor")


def _get_text_features_safe(model, inputs, device):
    """
    安全获取文本特征（兼容 transformers 4.5x 与 ChineseCLIP 的组合）

    背景：transformers 4.57 中 ChineseCLIPModel.get_text_features 依赖
    text_model 的 pooler_output，而 ChineseCLIP 配置未启用 pooler（返回 None），
    导致 text_projection(pooled_output) 抛 TypeError。
    这里降级为手动 forward：取 last_hidden_state 的 CLS token 再过 text_projection，
    与官方实现等价。
    """
    import torch

    try:
        return model.get_text_features(**inputs)
    except (TypeError, AttributeError, ValueError):
        # 手动 forward：text_model -> CLS pooling -> text_projection
        kwargs = {"input_ids": inputs["input_ids"]}
        if "attention_mask" in inputs:
            kwargs["attention_mask"] = inputs["attention_mask"]
        if "token_type_ids" in inputs:
            kwargs["token_type_ids"] = inputs["token_type_ids"]
        with torch.no_grad():
            text_outputs = model.text_model(**kwargs)
            pooled = text_outputs.last_hidden_state[:, 0, :]
            return model.text_projection(pooled)


class CLIPEmbedder:
    """
    CLIP 图文对齐嵌入器

    用法：
        emb = CLIPEmbedder()
        if emb.is_available():
            img_vec = emb.embed_image(image_bytes)
            txt_vec = emb.embed_text("一张流程图")
            # img_vec 和 txt_vec 在同一向量空间，可直接做余弦相似度
    """

    def __init__(self, model_name: Optional[str] = None):
        """
        Args:
            model_name: CLIP 模型名，None 时从 settings.CLIP_MODEL_NAME 读取
        """
        self._model_name = model_name
        self._model = None
        self._processor = None
        self._device = None
        self._dimension: Optional[int] = None
        self._is_chinese: bool = False  # 是否中文 CLIP（影响 import 和调用方式）
        self._load_attempted = False
        self._load_error: Optional[str] = None

    def _get_model_name(self) -> str:
        if self._model_name:
            return self._model_name
        try:
            from ..core.config import settings
            return settings.CLIP_MODEL_NAME
        except Exception:
            return "OFA-Sys/chinese-clip-vit-base-patch16"

    def _load_model(self) -> None:
        """延迟加载 CLIP 模型（首次使用时调用）"""
        if self._load_attempted:
            return  # 已经尝试过，不重复加载
        self._load_attempted = True

        try:
            from ..core.config import settings
            if not getattr(settings, "MULTIMODAL_VECTOR_ENABLED", False):
                self._load_error = "MULTIMODAL_VECTOR_ENABLED=False"
                logger.info("CLIP embedder 未启用（MULTIMODAL_VECTOR_ENABLED=False）")
                return
        except Exception as e:
            self._load_error = f"读取配置失败: {e}"
            return

        model_name = self._get_model_name()

        # 判断是否中文 CLIP（根据模型名）
        is_chinese = "chinese-clip" in model_name.lower() or "cn-clip" in model_name.lower()
        self._is_chinese = is_chinese

        # 选择设备
        try:
            import torch
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            self._device = "cpu"

        try:
            if is_chinese:
                # 中文 CLIP：用 transformers 的 ChineseCLIP
                from transformers import ChineseCLIPProcessor, ChineseCLIPModel
                import torch
                logger.info(f"加载 ChineseCLIP 模型: {model_name} (device={self._device})")
                self._model = ChineseCLIPModel.from_pretrained(
                    model_name, local_files_only=True
                )
                self._processor = ChineseCLIPProcessor.from_pretrained(
                    model_name, local_files_only=True
                )
                self._model.to(self._device)
                self._model.eval()
            else:
                # 英文 CLIP：用 transformers 的 CLIP
                from transformers import CLIPProcessor, CLIPModel
                import torch
                logger.info(f"加载 CLIP 模型: {model_name} (device={self._device})")
                self._model = CLIPModel.from_pretrained(
                    model_name, local_files_only=True
                )
                self._processor = CLIPProcessor.from_pretrained(
                    model_name, local_files_only=True
                )
                self._model.to(self._device)
                self._model.eval()

            # 探测维度（用空文本做一次前向）
            with torch.no_grad():
                inputs = self._processor(text=["test"], return_tensors="pt", padding=True)
                inputs = {k: v.to(self._device) for k, v in inputs.items()}
                features = _get_text_features_safe(self._model, inputs, self._device)
                tensor = _extract_tensor(features)
                self._dimension = tensor.shape[-1]

            logger.info(
                f"CLIP 模型加载完成: dim={self._dimension}, device={self._device}, "
                f"chinese={is_chinese}"
            )
        except Exception as e:
            self._load_error = f"{type(e).__name__}: {e}"
            logger.warning(
                f"CLIP 模型加载失败（多模态向量检索将不可用，降级为纯文本检索）: {self._load_error}"
            )
            logger.warning(
                f"如需启用：1) 下载模型 {model_name} 到本地 HF 缓存；"
                f"2) 设置 MULTIMODAL_VECTOR_ENABLED=True"
            )
            self._model = None
            self._processor = None

    def is_available(self) -> bool:
        """CLIP 是否可用（已启用且模型加载成功）"""
        if self._load_attempted:
            return self._model is not None
        # 未尝试过加载时，先触发懒加载
        self._load_model()
        return self._model is not None

    @property
    def dimension(self) -> Optional[int]:
        """向量维度"""
        if self._dimension is None:
            self._load_model()
        return self._dimension

    def embed_image(self, image_bytes: bytes) -> Optional[List[float]]:
        """
        生成图像向量

        Args:
            image_bytes: 图片二进制数据

        Returns:
            向量列表，或 None（不可用/失败时）
        """
        if not self.is_available():
            return None

        try:
            from PIL import Image
            import io
            import torch

            image = Image.open(io.BytesIO(image_bytes))
            if image.mode != "RGB":
                image = image.convert("RGB")

            with torch.no_grad():
                inputs = self._processor(images=image, return_tensors="pt")
                inputs = {k: v.to(self._device) for k, v in inputs.items()}
                features = self._model.get_image_features(**inputs)
                tensor = _extract_tensor(features)
                # L2 归一化（CLIP 标准做法，便于余弦相似度）
                tensor = tensor / tensor.norm(dim=-1, keepdim=True)
                return tensor[0].cpu().tolist()
        except Exception as e:
            logger.warning(f"CLIP 图像向量生成失败: {type(e).__name__}: {e}")
            return None

    def embed_text(self, text: str) -> Optional[List[float]]:
        """
        生成文本向量（与图像向量在同一空间，可直接做相似度）

        Args:
            text: 用户查询

        Returns:
            向量列表，或 None（不可用/失败时）
        """
        if not self.is_available():
            return None

        try:
            import torch

            with torch.no_grad():
                inputs = self._processor(text=[text], return_tensors="pt", padding=True)
                inputs = {k: v.to(self._device) for k, v in inputs.items()}
                features = _get_text_features_safe(self._model, inputs, self._device)
                tensor = _extract_tensor(features)
                # L2 归一化
                tensor = tensor / tensor.norm(dim=-1, keepdim=True)
                return tensor[0].cpu().tolist()
        except Exception as e:
            logger.warning(f"CLIP 文本向量生成失败: {type(e).__name__}: {e}")
            return None

    def embed_image_batch(self, images: List[bytes]) -> List[Optional[List[float]]]:
        """
        批量生成图像向量（效率高于循环调用 embed_image）

        Args:
            images: 图片二进制列表

        Returns:
            向量列表（与输入一一对应，失败的项为 None）
        """
        if not self.is_available():
            return [None] * len(images)

        try:
            from PIL import Image
            import io
            import torch

            # 加载所有图片
            pil_images = []
            valid_indices = []  # 记录成功加载的索引
            for i, img_bytes in enumerate(images):
                try:
                    img = Image.open(io.BytesIO(img_bytes))
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    pil_images.append(img)
                    valid_indices.append(i)
                except Exception as e:
                    logger.debug(f"图片 {i} 加载失败: {e}")

            if not pil_images:
                return [None] * len(images)

            with torch.no_grad():
                inputs = self._processor(images=pil_images, return_tensors="pt")
                inputs = {k: v.to(self._device) for k, v in inputs.items()}
                features = self._model.get_image_features(**inputs)
                tensor = _extract_tensor(features)
                tensor = tensor / tensor.norm(dim=-1, keepdim=True)
                vectors = tensor.cpu().tolist()

            # 组装结果（按原索引）
            result = [None] * len(images)
            for vec_idx, orig_idx in enumerate(valid_indices):
                result[orig_idx] = vectors[vec_idx]
            return result
        except Exception as e:
            logger.warning(f"CLIP 批量图像向量生成失败: {type(e).__name__}: {e}")
            return [None] * len(images)

    def get_state_info(self) -> dict:
        """获取状态信息（用于 /health 或调试）"""
        return {
            "enabled": self.is_available(),
            "model_name": self._get_model_name() if self._load_attempted else None,
            "dimension": self._dimension,
            "device": self._device,
            "is_chinese": self._is_chinese,
            "error": self._load_error,
        }


# ========== 模块级单例 ==========

_default_embedder: Optional[CLIPEmbedder] = None


def get_default_clip_embedder() -> CLIPEmbedder:
    """获取全局 CLIP embedder 单例（懒加载）"""
    global _default_embedder
    if _default_embedder is None:
        _default_embedder = CLIPEmbedder()
    return _default_embedder

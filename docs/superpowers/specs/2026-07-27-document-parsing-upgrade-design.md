# 文档解析升级设计方案

> 日期: 2026-07-27
> 状态: 设计稿
> 关联: 将文档解析从"纯文本提取"升级为"结构化文档理解"

---

## 1. 现状与问题

### 当前架构

```
上传 → 解析(flat text) → 分块(重检测结构) → ChromaDB
```

### 核心问题

| 问题 | 影响 |
|------|------|
| 解析结果全是纯文本 | 丢失标题层级、表格结构、代码块、公式 |
| 分块器自己"猜"结构 | 用正则检测标题，精度低，跨页段落丢失 |
| 表格被拼成管道文本 | 检索时无法按列匹配，RAG 质量下降 |
| 无图片/公式提取 | 这部分信息完全丢失 |
| OCR 依赖重 | Tesseract 中文效果差，未集成到主流程 |

---

## 2. 目标

从"文字提取"升级为"文档理解"：

```
上传 → 结构化解析(Element类型) → 结构感知分块 → ChromaDB
                                   ↑
                         保留标题、表格、代码、公式、图片
```

---

## 3. 方案选择

### 3.1 解析后端

| 场景 | 选择 | 理由 |
|------|------|------|
| PDF（主场景） | **Docling** (MIT) | 结构化输出原生支持，TableFormer SOTA，CPU 可跑 |
| DOCX | Docling | 同一管道，统一输出 |
| MD/TXT | 轻量自实现 | 简单快速，无需 ML 模型 |
| 降级/兜底 | PyMuPDF（现有） | 已安装，无额外依赖 |

### 3.2 向量数据库

**保持 ChromaDB 不变。** 当前规模（<50万 chunks）下性能充足，升级解析质量是真正的瓶颈。

### 3.3 分块策略

**by_title（标题边界分块）** 作为默认策略。依据：

- Unstructured.io 实测：纯文本切分 RAG 准确率 34% → by_title 89%
- 教育资料天然有清晰的标题层级
- 每个块是语义完整的章节，适合知识点检索

---

## 4. 核心数据模型

```python
@dataclass
class DocumentElement:
    """文档元素——解析的最小单元"""
    type: ElementType          # TITLE | HEADING | PARAGRAPH | TABLE | CODE | FORMULA | IMAGE | LIST
    text: str
    metadata: ElementMetadata
    
    # 类型专属字段（非 None 表示该类型）
    text_as_html: Optional[str]   # 表格的 HTML 表示
    formula_latex: Optional[str]  # 公式的 LaTeX
    image_base64: Optional[str]   # 图片 base64
    image_desc: Optional[str]     # 图片描述（后续可走多模态）

@dataclass
class ElementMetadata:
    page_number: Optional[int]
    bbox: Optional[Tuple[float, float, float, float]]
    heading_path: List[str]       # ["1. Introduction", "1.1 Supervised"]
    font: Optional[str]
    font_size: Optional[float]

@dataclass 
class StructuredDocument:
    """解析器的统一输出"""
    metadata: DocumentMetadata    # 文件名、页数、title
    elements: List[DocumentElement]  # 线性阅读顺序
```

### 关键设计决策

1. **Content/Furniture 分离**：页眉页脚在解析阶段就过滤掉（模仿 DoclingDocument 设计）
2. **类型化存储**：Table 保留 `text_as_html`，Formula 保留 `formula_latex`
3. **标题面包屑**：每个 element 知道自己的标题路径，分块时直接继承

---

## 5. 解析器架构

### 统一接口

```python
class BaseParser(ABC):
    """所有解析器的抽象接口"""
    
    SUPPORTED_EXTENSIONS: Set[str]
    
    @abstractmethod
    def parse(self, content: bytes, filename: str) -> StructuredDocument:
        ...
```

### 实现类

#### DoclingParser（主解析器）

```python
class DoclingParser(BaseParser):
    SUPPORTED_EXTENSIONS = {".pdf", ".docx"}
    
    def parse(self, content, filename) -> StructuredDocument:
        # 1. 保存到临时文件（Docling 需要文件路径）
        # 2. converter.convert(path) → DoclingDocument
        # 3. 遍历 doc.iterate_items() → 映射为我们的 DocumentElement
        # 4. 过滤 PAGE_HEADER/FOOTER
        # 5. 提取表格 DataFrame → text_as_html
        # 6. 构造 StructuredDocument 返回
        pass
```

#### PyMuPDFParser（降级解析器）

```python
class PyMuPDFParser(BaseParser):
    SUPPORTED_EXTENSIONS = {".pdf", ".docx"}
    
    def parse(self, content, filename) -> StructuredDocument:
        # 1. PyMuPDF 提取块 + pdfplumber 提取表格
        # 2. 根据字体/大小推断元素类型
        # 3. 构造 StructuredDocument（质量低于 Docling，但不需要模型）
        pass
```

#### TextParser（轻量）

```python
class TextParser(BaseParser):
    SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown"}
    
    def parse(self, content, filename) -> StructuredDocument:
        # 编码检测 → 按标题/段落分割 → 构造元素
        pass
```

### 工厂

```python
class ParserFactory:
    _PARSERS = {
        ".pdf": DoclingParser,
        ".docx": DoclingParser,
        ".txt": TextParser,
        ".md": TextParser,
        ".markdown": TextParser,
    }
    
    @classmethod
    def get_parser(cls, filename: str) -> BaseParser:
        ext = Path(filename).suffix.lower()
        parser_cls = cls._PARSERS.get(ext)
        if not parser_cls:
            raise ValueError(f"不支持的格式: {ext}")
        return parser_cls()
    
    @classmethod
    def get_supported_extensions(cls) -> Set[str]:
        return set(cls._PARSERS.keys())
```

---

## 6. 结构感知分块器

### 策略：by_title（默认）

```python
class StructureAwareChunker:
    def chunk(self, doc: StructuredDocument, strategy="by_title") -> List[Chunk]:
        if strategy == "by_title":
            return self._chunk_by_title(doc)
        # 后续可扩展：by_page, semantic
    
    def _chunk_by_title(self, doc):
        # 规则：
        # 1. HEADING/TITLE 元素 → 开新块
        # 2. TABLE/FORMULA/CODE → 独立成块（不与其他类型合并）
        # 3. 同一标题下的 PARAGRAPH/LIST → 合并到同一块
        # 4. 每个块继承父标题的面包屑路径
        # 5. 块大小限制 ~500-1000 chars，超长段落按句子切分
        ...
```

### Chunk 输出

```python
@dataclass
class Chunk:
    id: str
    text: str                          # 含标题路径前缀
    element_type: str                  # text | table | code | formula
    metadata: Dict
    # metadata 包含:
    #   heading_path: List[str]
    #   doc_id: str
    #   page_number: int
    #   text_as_html: Optional[str]    # 表格专用
    #   formula_latex: Optional[str]   # 公式专用
```

---

## 7. Uploader 集成

最小化改动，保持现有接口不变：

```python
class DocumentUploader:
    async def upload(self, content, filename, ...):
        # 1. 解析（升级）
        parser = ParserFactory.get_parser(filename)
        doc = parser.parse(content, filename)       # ← 原来返回 str
        
        # 2. 分块（升级）
        chunker = StructureAwareChunker()
        chunks = chunker.chunk(doc)                 # ← 基于结构化文档
        
        # 3. 存入知识库（基本不变）
        items = [KnowledgeItem(...) for chunk in chunks]
        self.knowledge_store.add_batch(items)       # ← 接口不变
```

---

## 8. 检索层

### 变更最小

| 组件 | 变更 |
|------|------|
| ChromaDB | **无变更**（只使用新的 chunk metadata） |
| BM25 索引 | **无变更**（仍然基于 chunk text） |
| ChromaStore.clean_markdown | **保留**（继续清理后再向量化） |
| CrossEncoder Reranker | **已有代码，可启用**（配置化开关） |
| QueryRewriter | **无变更** |

### 主要提升点

检索结果的 metadata 更丰富了：

```python
# 之前
{"title": "xxx", "source": "user_document"}

# 之后
{
    "title": "xxx",
    "source": "user_document",
    "element_type": "table",          # 新增
    "heading_path": ["1.", "1.1"],    # 新增
    "text_as_html": "<table>...</table>",  # 新增
    "page_number": 3,                  # 新增
}
```

LLM 在生成答案时可以：
- 根据 element_type 决定如何展示（表格用 HTML 渲染）
- 用 heading_path 定位答案来源
- 引用时标注页码

---

## 9. 错误处理与降级

```python
# 按优先级降级
def get_parser(filename):
    try:
        return DoclingParser()    # 首选
    except (ImportError, OSError):
        return PyMuPDFParser()    # 降级
    except:
        return TextParser()       # 最简降级
```

### 降级矩阵

| 场景 | 行为 |
|------|------|
| Docling 模型下载失败 | 自动切到 PyMuPDFParser |
| Docling 解析异常 | 捕获异常，切到 PyMuPDFParser |
| PyMuPDF 也失败 | 返回空结果 + 日志 + 错误状态 |
| 网络超时（模型下载） | 多次重试后降级 |
| 大文件 | 仍全部在内存处理（当前限制，后续可改为流式） |

---

## 10. 分阶段实施

### Phase 1: 结构化数据模型 + DoclingParser

- [ ] 定义 `DocumentElement`、`StructuredDocument` 等数据类
- [ ] 实现 `DoclingParser` 和 `PyMuPDFParser`
- [ ] 实现 `ParserFactory`
- [ ] 单元测试：3 种格式 × 2 种 parser

### Phase 2: 结构感知分块器

- [ ] 实现 `StructureAwareChunker`（by_title 策略）
- [ ] 编写分块验证测试
- [ ] 对比旧 chunker 和新 chunker 的输出质量

### Phase 3: Uploader 集成

- [ ] 修改 `DocumentUploader` 使用新管道
- [ ] 保持 `KnowledgeItem` 接口不变
- [ ] 端到端测试：上传→解析→分块→入库

### Phase 4: 验证

- [ ] 用真实文档跑对比（before/after）
- [ ] 检查检索结果质量
- [ ] 确认降级逻辑生效

---

## 11. 附录

### 11.1 调研参考

- **Docling** (IBM, MIT): https://github.com/docling-project/docling
- **Unstructured.io** (Apache 2.0): https://github.com/Unstructured-IO/unstructured
- **MinerU** (OpenDataLab): https://github.com/opendatalab/MinerU
- **Marker** (VikParuchuri): https://github.com/VikParuchuri/marker

### 11.2 实测数据

测试文档：2 页 PDF，含标题、表格、代码块、列表、公式
- Docling 解析耗时：8.5s（首次，含模型下载）/ 后续 ~3-5s
- 元素提取：19 个文本元素 + 1 个表格 + 代码块识别 + 列表项识别
- 分块结果：8 个语义完整的 Chunk（by_title）

### 11.3 技术栈

| 组件 | 版本 | 说明 |
|------|------|------|
| Docling | v2.115+ | MIT 许可，结构化解析 |
| PyMuPDF | v1.27+ | 降级方案，已有 |
| ChromaDB | v1.5+ | 保持不动 |
| Python | 3.12 | 保持 |
| 环境 | CPU (Intel Iris Xe) | 无需 GPU |

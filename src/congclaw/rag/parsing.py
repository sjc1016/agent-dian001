"""P3-4/5/6：异构文档解析 + 中文句子边界切分 + Parent-Child 父子分片。

- 解析（离线，不属于在线子图）：PDF / Word / HTML 走 Docling 统一解析为文本；
  Markdown / TXT 直接按多编码兜底读取；Docling 不可用时自动降级为纯文本读取，
  保证在无模型下载的离线环境里知识库仍可入库。
- Child：句子边界切分（中文标点规则分句）后按 1~3 句聚合，保障召回精度。
- Parent：段落块（标题并入紧邻段落），父文本由其 child 有序拼接还原，
  保障生成时上下文完整、无断句。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 句末标点（中英文）；右引号/右括号允许跟在句末标点之后
_SENTENCE_END = "。！？!?；;"
_SENTENCE_SPLIT_RE = re.compile(rf"(?<=[{_SENTENCE_END}])[”’」』）)]*")
_PARA_SPLIT_RE = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")
_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_WS_RUN_RE = re.compile(r"[ \t\u3000]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# child 聚合参数
CHILD_TARGET_CHARS = 90  # 达到该字数且至少 1 句即可收口
CHILD_MAX_CHARS = 180  # 单 child 硬上限
CHILD_MAX_SENTENCES = 3
PARENT_MIN_CHARS = 12  # 短于该值的段落视为标题，并入下一段

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".html", ".htm", ".md", ".markdown", ".txt"}
DOCLING_SUFFIXES = {".pdf", ".docx", ".html", ".htm"}


@dataclass
class ChildChunk:
    """文档内局部 child（position 为文档内序号，入库时确定）。"""

    text: str
    position: int


@dataclass
class ParentChunk:
    parent_index: int
    text: str
    children: list[ChildChunk] = field(default_factory=list)


@dataclass
class ParsedDocument:
    source: str
    parents: list[ParentChunk]

    @property
    def child_count(self) -> int:
        return sum(len(parent.children) for parent in self.parents)


def parse_document(path: str | Path) -> str:
    """把单个文档解析为纯文本（Docling 优先，失败降级多编码纯文本读取）。"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in DOCLING_SUFFIXES:
        text = _parse_with_docling(path)
        if text.strip():
            return _normalize_text(text)
    return _normalize_text(_read_text_lossy(path))


def _parse_with_docling(path: Path) -> str:
    """Docling 统一解析；任何异常（含离线缺模型）都降级为纯文本读取。"""
    try:
        from docling.document_converter import DocumentConverter
        from docling.datamodel.pipeline_options import PdfPipelineOptions

        options = PdfPipelineOptions()
        options.do_ocr = False  # 知识文档为电子文本，无需 OCR，也避免下载 OCR 模型
        options.do_table_structure = False
        converter = DocumentConverter()
        result = converter.convert(str(path))
        return result.document.export_to_markdown()
    except Exception:
        return _read_text_lossy(path)


def _read_text_lossy(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HTML_COMMENT_RE.sub("", text)
    text = _IMAGE_RE.sub("", text)
    lines = []
    for line in text.split("\n"):
        line = _MARKDOWN_HEADING_RE.sub("", line)
        line = line.rstrip()
        lines.append(line)
    return "\n".join(lines).strip()


def split_paragraphs(text: str) -> list[str]:
    """按空行切段；非空行按中英文规则粘连（中文硬换行不插空格）。"""
    paragraphs: list[str] = []
    for block in _PARA_SPLIT_RE.split(text):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        merged = lines[0]
        for line in lines[1:]:
            if merged and _needs_join_space(merged[-1], line[0]):
                merged += " " + line
            else:
                merged += line
        paragraphs.append(merged)

    # 短段落视为标题：优先并入紧邻的下一段（标题在前），
    # 文末孤立标题则并入上一段，避免产生超短父分片。
    merged_paragraphs: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        if len(paragraph) >= PARENT_MIN_CHARS:
            merged_paragraphs.append(paragraph)
            continue
        if index + 1 < len(paragraphs):
            paragraphs[index + 1] = paragraph + "\n" + paragraphs[index + 1]
        elif merged_paragraphs:
            merged_paragraphs[-1] += "\n" + paragraph
        else:
            merged_paragraphs.append(paragraph)
    return merged_paragraphs


def _needs_join_space(prev_last: str, next_first: str) -> bool:
    """两侧都是拉丁字母/数字时补空格；中文之间直接粘连。"""
    return bool(re.match(r"[A-Za-z0-9]", prev_last) and re.match(r"[A-Za-z0-9]", next_first))


def split_sentences(paragraph: str) -> list[str]:
    """中文标点/规则分句，句末标点保留在句尾；无标点长句按硬上限兜底切分。"""
    text = paragraph.strip()
    if not text:
        return []
    # 标题与正文之间的换行保留为分隔
    parts: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        candidates = [segment.strip() for segment in _SENTENCE_SPLIT_RE.split(line) if segment.strip()]
        parts.extend(_hard_split_long(candidates))
    return parts


def _hard_split_long(sentences: list[str]) -> list[str]:
    """对没有句末标点的超长句（常见于列表项）按逗号/分号做一次兜底切分。"""
    result: list[str] = []
    for sentence in sentences:
        if len(sentence) <= CHILD_MAX_CHARS:
            result.append(sentence)
            continue
        pieces = re.split(r"(?<=[，,、：:])", sentence)
        buffer = ""
        for piece in pieces:
            if len(buffer) + len(piece) <= CHILD_MAX_CHARS or not buffer:
                buffer += piece
            else:
                result.append(buffer)
                buffer = piece
        if buffer:
            result.append(buffer)
    return result


def _group_children(sentences: list[str]) -> list[str]:
    """句子聚合为 1~3 句的 child，目标字数 CHILD_TARGET_CHARS、硬顶 CHILD_MAX_CHARS。"""
    children: list[str] = []
    buffer: list[str] = []
    buffer_len = 0
    for sentence in sentences:
        if (
            buffer
            and (
                len(buffer) >= CHILD_MAX_SENTENCES
                or buffer_len + len(sentence) > CHILD_MAX_CHARS
                or (buffer_len >= CHILD_TARGET_CHARS and len(buffer) >= 1)
            )
        ):
            children.append("".join(buffer))
            buffer = []
            buffer_len = 0
        buffer.append(sentence)
        buffer_len += len(sentence)
        if buffer_len >= CHILD_MAX_CHARS:
            children.append("".join(buffer))
            buffer = []
            buffer_len = 0
    if buffer:
        children.append("".join(buffer))
    return [child.strip() for child in children if child.strip()]


def build_parent_child_chunks(text: str, *, source: str) -> ParsedDocument:
    """原始文本 → 父子分片结构（child 的 position 为文档内全局序号）。"""
    parents: list[ParentChunk] = []
    position = 0
    for parent_index, paragraph in enumerate(split_paragraphs(text)):
        sentences = split_sentences(paragraph)
        child_texts = _group_children(sentences)
        if not child_texts:
            continue
        children = []
        for child_text in child_texts:
            children.append(ChildChunk(text=child_text, position=position))
            position += 1
        parent_text = "".join(child_texts)
        parents.append(
            ParentChunk(parent_index=parent_index, text=parent_text, children=children)
        )
    return ParsedDocument(source=source, parents=parents)


def parse_and_chunk(path: str | Path, *, source: str | None = None) -> ParsedDocument:
    """解析磁盘文档并生成父子分片；source 默认取文件名。"""
    path = Path(path)
    return build_parent_child_chunks(parse_document(path), source=source or path.name)

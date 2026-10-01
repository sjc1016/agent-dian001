"""SQLAlchemy ORM 模型。

表结构定义在 ``schema.sql`` 中（由 :func:`mokioclaw.db.init_db` 执行），
本模块仅声明对应的 ORM 映射，供业务层进行类型安全的读写。

阶段 1 引入 ``session`` 表模型；阶段 3 追加 RAG ``chunk_meta`` 父子分片元信息表。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionModel(Base):
    """多轮会话状态表。

    ``workspace`` 是主键，与会话的物理工作区一一对应；
    ``recent_turns`` / ``pending_slots`` 以 JSON 文本存储。
    """

    __tablename__ = "session"

    session_id: Mapped[str] = mapped_column(nullable=False)
    workspace: Mapped[str] = mapped_column(primary_key=True)
    turn_index: Mapped[int] = mapped_column(nullable=False, default=0)
    last_route: Mapped[str] = mapped_column(nullable=False, default="")
    last_task: Mapped[str] = mapped_column(nullable=False, default="")
    last_final_answer: Mapped[str] = mapped_column(nullable=False, default="")
    summary: Mapped[str] = mapped_column(nullable=False, default="")
    recent_turns: Mapped[str] = mapped_column(nullable=False, default="[]")
    pending_slots: Mapped[str] = mapped_column(nullable=False, default="[]")
    # 阶段 2：跨轮对话管控计数（追问轮数 / 连续 unknown 轮数）
    clarify_count: Mapped[int] = mapped_column(nullable=False, default=0)
    unknown_count: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(nullable=False)
    updated_at: Mapped[str] = mapped_column(nullable=False)


class ChunkMetaModel(Base):
    """阶段 3：RAG child 分片元信息表（child→parent 映射 + 文档来源 + 位置）。"""

    __tablename__ = "chunk_meta"

    child_id: Mapped[str] = mapped_column(primary_key=True)
    parent_id: Mapped[str] = mapped_column(nullable=False)
    doc_source: Mapped[str] = mapped_column(nullable=False)
    position: Mapped[int] = mapped_column(nullable=False)
    child_text: Mapped[str] = mapped_column(nullable=False)

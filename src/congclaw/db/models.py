"""SQLAlchemy ORM 模型。

表结构定义在 ``schema.sql`` 中（由 :func:`congclaw.db.init_db` 执行），
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


# ---------------------------------------------------------------------------
# 阶段 4：电信模拟业务表（业务层用 aiosqlite 短连接访问，此处仅声明 ORM 映射）
# ---------------------------------------------------------------------------


class AccountModel(Base):
    """账户：话费余额与本月实时话费。"""

    __tablename__ = "account"

    phone: Mapped[str] = mapped_column(primary_key=True)
    owner_name: Mapped[str] = mapped_column(nullable=False, default="")
    balance: Mapped[float] = mapped_column(nullable=False, default=0.0)
    real_time_fee: Mapped[float] = mapped_column(nullable=False, default=0.0)
    bill_cycle: Mapped[str] = mapped_column(nullable=False, default="")
    updated_at: Mapped[str] = mapped_column(nullable=False, default="")


class PackageCatalogModel(Base):
    """可办理套餐目录。"""

    __tablename__ = "package_catalog"

    package_id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
    monthly_fee: Mapped[float] = mapped_column(nullable=False)
    data_quota_gb: Mapped[float] = mapped_column(nullable=False, default=0.0)
    voice_minutes: Mapped[int] = mapped_column(nullable=False, default=0)
    broadband_mbps: Mapped[int] = mapped_column(nullable=False, default=0)
    description: Mapped[str] = mapped_column(nullable=False, default="")
    active: Mapped[int] = mapped_column(nullable=False, default=1)


class UserPackageModel(Base):
    """用户当前套餐与已用量。"""

    __tablename__ = "user_package"

    phone: Mapped[str] = mapped_column(primary_key=True)
    package_id: Mapped[str] = mapped_column(nullable=False)
    package_name: Mapped[str] = mapped_column(nullable=False, default="")
    data_used_gb: Mapped[float] = mapped_column(nullable=False, default=0.0)
    voice_used_min: Mapped[int] = mapped_column(nullable=False, default=0)
    effective_date: Mapped[str] = mapped_column(nullable=False, default="")
    updated_at: Mapped[str] = mapped_column(nullable=False, default="")


class FaultTicketModel(Base):
    """故障报修工单。"""

    __tablename__ = "fault_ticket"

    ticket_id: Mapped[str] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(nullable=False)
    fault_type: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str] = mapped_column(nullable=False, default="")
    address: Mapped[str] = mapped_column(nullable=False, default="")
    contact: Mapped[str] = mapped_column(nullable=False, default="")
    status: Mapped[str] = mapped_column(nullable=False, default="已受理")
    status_note: Mapped[str] = mapped_column(nullable=False, default="")
    created_at: Mapped[str] = mapped_column(nullable=False)
    updated_at: Mapped[str] = mapped_column(nullable=False)


class PendingApprovalModel(Base):
    """跨轮人工确认：高危写操作执行前落库，用户下一轮确认/取消。"""

    __tablename__ = "pending_approval"

    approval_id: Mapped[str] = mapped_column(primary_key=True)
    workspace: Mapped[str] = mapped_column(nullable=False)
    skill_name: Mapped[str] = mapped_column(nullable=False)
    args_json: Mapped[str] = mapped_column(nullable=False, default="{}")
    summary: Mapped[str] = mapped_column(nullable=False, default="")
    status: Mapped[str] = mapped_column(nullable=False, default="pending")
    created_at: Mapped[str] = mapped_column(nullable=False)
    updated_at: Mapped[str] = mapped_column(nullable=False)


class UserProfileModel(Base):
    """阶段 5：跨会话长期用户摘要（按号码维度）。

    ``topics`` / ``open_tickets`` 以 JSON 文本存储；``turn_count`` 为已压缩
    进摘要的轮次水位，增量压缩时只处理水位之后的新轮次。
    """

    __tablename__ = "user_profile"

    phone: Mapped[str] = mapped_column(primary_key=True)
    owner_name: Mapped[str] = mapped_column(nullable=False, default="")
    summary: Mapped[str] = mapped_column(nullable=False, default="")
    topics: Mapped[str] = mapped_column(nullable=False, default="[]")
    open_tickets: Mapped[str] = mapped_column(nullable=False, default="[]")
    preferred_package: Mapped[str] = mapped_column(nullable=False, default="")
    turn_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_session_workspace: Mapped[str] = mapped_column(nullable=False, default="")
    created_at: Mapped[str] = mapped_column(nullable=False)
    updated_at: Mapped[str] = mapped_column(nullable=False)


class FaqEntryModel(Base):
    """阶段 7：常见问答解决方案沉淀库（跨会话、跨用户的全局知识沉淀）。

    与 ``user_profile``（按号码的个人记忆）对称：本表沉淀的是「任何用户都可能
    遇到的常见问答解决方案」。``question_variants`` / ``preconditions`` /
    ``related_skills`` / ``sources_json`` 以 JSON 文本存储；``status`` 决定条目
    是否可被复用（draft 须人工审核后转 approved / published）。
    """

    __tablename__ = "faq_entry"

    faq_id: Mapped[str] = mapped_column(primary_key=True)
    canonical_question: Mapped[str] = mapped_column(nullable=False)
    question_variants: Mapped[str] = mapped_column(nullable=False, default="[]")
    category: Mapped[str] = mapped_column(nullable=False, default="")
    solution: Mapped[str] = mapped_column(nullable=False, default="")
    preconditions: Mapped[str] = mapped_column(nullable=False, default="[]")
    related_skills: Mapped[str] = mapped_column(nullable=False, default="[]")
    keywords: Mapped[str] = mapped_column(nullable=False, default="")
    status: Mapped[str] = mapped_column(nullable=False, default="draft")
    confidence: Mapped[float] = mapped_column(nullable=False, default=0.0)
    source_route: Mapped[str] = mapped_column(nullable=False, default="")
    source_session_id: Mapped[str] = mapped_column(nullable=False, default="")
    source_turn_index: Mapped[int] = mapped_column(nullable=False, default=0)
    sources_json: Mapped[str] = mapped_column(nullable=False, default="[]")
    merge_count: Mapped[int] = mapped_column(nullable=False, default=1)
    hit_count: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(nullable=False)
    updated_at: Mapped[str] = mapped_column(nullable=False)

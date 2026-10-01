-- 电信客服智能体 SQLite 表结构
-- 所有建表语句均使用 IF NOT EXISTS，保证 init_db 可重复执行。

CREATE TABLE IF NOT EXISTS app_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

INSERT OR IGNORE INTO app_meta (key, value) VALUES ('schema_version', '0');

-- 阶段 1：多轮会话状态（session.json 文件迁移到此表）
-- workspace 作为会话的物理定位键，与旧文件路径一一对应；
-- recent_turns / pending_slots 以 JSON 文本存储，便于直接反序列化为 Python 对象。
CREATE TABLE IF NOT EXISTS session (
    session_id      TEXT NOT NULL,
    workspace       TEXT PRIMARY KEY,
    turn_index      INTEGER NOT NULL DEFAULT 0,
    last_route      TEXT NOT NULL DEFAULT '',
    last_task       TEXT NOT NULL DEFAULT '',
    last_final_answer TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    recent_turns    TEXT NOT NULL DEFAULT '[]',
    pending_slots   TEXT NOT NULL DEFAULT '[]',
    -- 阶段 2：跨轮对话管控计数
    clarify_count   INTEGER NOT NULL DEFAULT 0,
    unknown_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 阶段 3：RAG 知识库父子分片元信息
-- child 细粒度切片入 Milvus（向量按 child_id 关联），父分片文本由同 parent_id
-- 的 child 按 position 有序拼接还原；position 为文档内全局序号，供邻域扩展。
CREATE TABLE IF NOT EXISTS chunk_meta (
    child_id    TEXT PRIMARY KEY,
    parent_id   TEXT NOT NULL,
    doc_source  TEXT NOT NULL,
    position    INTEGER NOT NULL,
    child_text  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunk_meta_parent ON chunk_meta(parent_id);
CREATE INDEX IF NOT EXISTS idx_chunk_meta_source ON chunk_meta(doc_source);
CREATE INDEX IF NOT EXISTS idx_chunk_meta_position ON chunk_meta(position);

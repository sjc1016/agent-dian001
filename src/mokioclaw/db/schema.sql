-- 电信客服智能体 SQLite 表结构
-- 阶段 0：仅建立最小元信息表，用于验证数据库初始化与 WAL 模式。
-- 后续阶段在此追加：
--   阶段 1：session（多轮会话状态）
--   阶段 3：chunk_meta（RAG 切片元信息 / 父子映射）
--   阶段 4：account、user_package、package_catalog、fault_ticket（模拟业务表）
-- 所有建表语句均使用 IF NOT EXISTS，保证 init_db 可重复执行。

CREATE TABLE IF NOT EXISTS app_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

INSERT OR IGNORE INTO app_meta (key, value) VALUES ('schema_version', '0');

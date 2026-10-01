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

-- 阶段 4：电信模拟业务表（接口形态对齐真实 BOSS/CRM，数据为本地演示种子）
-- 账户：话费余额与实时话费
CREATE TABLE IF NOT EXISTS account (
    phone           TEXT PRIMARY KEY,
    owner_name      TEXT NOT NULL DEFAULT '',
    balance         REAL NOT NULL DEFAULT 0.0,   -- 账户可用余额（元）
    real_time_fee   REAL NOT NULL DEFAULT 0.0,   -- 本月实时话费（元）
    bill_cycle      TEXT NOT NULL DEFAULT '',    -- 当前账期，如 2026-09
    updated_at      TEXT NOT NULL DEFAULT ''
);

-- 套餐目录：可办理套餐
CREATE TABLE IF NOT EXISTS package_catalog (
    package_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    monthly_fee     REAL NOT NULL,              -- 月费（元）
    data_quota_gb   REAL NOT NULL DEFAULT 0.0,  -- 国内流量（GB）
    voice_minutes   INTEGER NOT NULL DEFAULT 0, -- 国内语音（分钟）
    broadband_mbps  INTEGER NOT NULL DEFAULT 0, -- 宽带速率（Mbps，0 表示不含宽带）
    description     TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1
);

-- 用户当前套餐与余量
CREATE TABLE IF NOT EXISTS user_package (
    phone           TEXT PRIMARY KEY,
    package_id      TEXT NOT NULL,
    package_name    TEXT NOT NULL DEFAULT '',
    data_used_gb    REAL NOT NULL DEFAULT 0.0,
    voice_used_min  INTEGER NOT NULL DEFAULT 0,
    effective_date  TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (package_id) REFERENCES package_catalog(package_id)
);

-- 故障工单
CREATE TABLE IF NOT EXISTS fault_ticket (
    ticket_id       TEXT PRIMARY KEY,
    phone           TEXT NOT NULL,
    fault_type      TEXT NOT NULL,              -- 宽带故障/手机网络故障/通话故障/IPTV故障/其他
    description     TEXT NOT NULL DEFAULT '',
    address         TEXT NOT NULL DEFAULT '',
    contact         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT '已受理',
    status_note     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fault_ticket_phone ON fault_ticket(phone);
CREATE INDEX IF NOT EXISTS idx_fault_ticket_status ON fault_ticket(status);

-- 阶段 4（P4-16）：跨轮人工确认。写操作 Skill（如套餐变更）办理前落一条
-- pending 记录，用户下一轮回复"确认/取消"后由 agent 子图恢复执行或作废。
CREATE TABLE IF NOT EXISTS pending_approval (
    approval_id     TEXT PRIMARY KEY,
    workspace       TEXT NOT NULL,
    skill_name      TEXT NOT NULL,
    args_json       TEXT NOT NULL DEFAULT '{}',
    summary         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending', -- pending/approved/executed/cancelled/failed
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_approval_ws ON pending_approval(workspace, status);

-- 演示种子数据（INSERT OR IGNORE：幂等建库不覆盖用户改动）
INSERT OR IGNORE INTO account (phone, owner_name, balance, real_time_fee, bill_cycle, updated_at)
VALUES ('13800138000', '张伟', 86.50, 113.50, '2026-09', '2026-09-30T08:00:00+00:00');

INSERT OR IGNORE INTO package_catalog
    (package_id, name, monthly_fee, data_quota_gb, voice_minutes, broadband_mbps, description, active)
VALUES
    ('P129', '5G畅享129元档', 129.0, 30.0, 500, 0,
     '月费129元，含30GB国内流量、500分钟国内语音，超量后流量按5元/GB计费。', 1),
    ('P199', '5G畅享199元档', 199.0, 60.0, 1000, 0,
     '月费199元，含60GB国内流量、1000分钟国内语音，享5G极速速率权益。', 1),
    ('P299', '5G畅享299元档', 299.0, 100.0, 1500, 0,
     '月费299元，含100GB国内流量、1500分钟国内语音，含国际漫游流量优惠。', 1),
    ('BB169', '全屋WiFi融合169元档', 169.0, 40.0, 800, 500,
     '月费169元，含40GB国内流量、800分钟语音与500M家庭宽带，附赠全屋WiFi组网服务。', 1);

INSERT OR IGNORE INTO user_package
    (phone, package_id, package_name, data_used_gb, voice_used_min, effective_date, updated_at)
VALUES ('13800138000', 'P129', '5G畅享129元档', 18.6, 120, '2026-08-01', '2026-09-30T08:00:00+00:00');

INSERT OR IGNORE INTO fault_ticket
    (ticket_id, phone, fault_type, description, address, contact, status, status_note, created_at, updated_at)
VALUES ('FT202609280001', '13800138000', '宽带故障', '9月27日光猫亮红灯无法上网，已上门更换光纤恢复。',
        '杭州市西湖区文三路100号3栋502', '13800138000', '已完成', '维修师傅已上门更换光纤，测速恢复正常。',
        '2026-09-27T01:20:00+00:00', '2026-09-27T06:40:00+00:00');

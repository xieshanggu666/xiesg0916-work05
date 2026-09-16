-- 双盲仲裁：annotations 新增仲裁归属列（新表 arbitrations / arbitration_decisions
-- 由应用启动时的 create_all 自动创建）。幂等，可重复执行。
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS arbitration_id INTEGER;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS arbitration_side VARCHAR(1);
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS arbitration_submitted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS arbitration_submitted_version INTEGER;
-- 盲标访问令牌（隔离期唯一凭证）
ALTER TABLE annotations ADD COLUMN IF NOT EXISTS blind_token VARCHAR(64);

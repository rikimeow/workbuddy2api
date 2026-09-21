"""SQLAlchemy 引擎 / 会话 / Base，并负责建库建表。"""
import logging
import re
import time

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from admin.config import settings

logger = logging.getLogger("admin.db")

# SQL 标识符白名单：仅允许常规字母数字下划线，杜绝任何拼接注入。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"非法 SQL 标识符: {name!r}")
    return name


def _quote_db_name(name: str) -> str:
    """库名（来自配置）去除反引号转义后安全引用。"""
    return name.replace("`", "").replace("\\", "").strip()

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, pool_recycle=3600,
                        pool_timeout=30, pool_size=20, max_overflow=40, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_database():
    """若 DATABASE_URL 指向的库不存在则创建（仅 MySQL）。"""
    url = settings.DATABASE_URL
    if not url.startswith("mysql"):
        return
    db_name = url.split("/")[-1].split("?")[0]
    rest = url.split("://", 1)[1]
    user_pass, host_rest = rest.split("@", 1)
    user, pwd = (user_pass.split(":", 1) + [""])[:2] if ":" in user_pass else (user_pass, "")
    host_port = host_rest.split("/", 1)[0]
    host = host_port.split(":")[0]
    port = int(host_port.split(":")[1]) if ":" in host_port else 3306
    import pymysql

    conn = pymysql.connect(host=host, port=port, user=user, password=pwd, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{_quote_db_name(db_name)}` CHARACTER SET utf8mb4")
        conn.commit()
    finally:
        conn.close()


#: 启动阶段访问数据库的重试次数与间隔（秒）。
#:
#: 为什么必须重试而不是直接抛：`_startup()` 里任何异常都会让 uvicorn 启动失败、
#: 进程直接退出。而这台机器内存只有 1870MB，MySQL 已被内核 OOM 杀掉过多次；
#: MySQL 重启的那几十秒里，服务启动必然失败并退出，nginx 侧表现为 502
#: —— 这正是「线上老是崩、老师就断开了」的直接成因。
#: 改为「重试 + 降级启动」后：MySQL 短暂不可用只会让请求返回 503，
#: 进程不再退出，MySQL 一恢复（连接池有 pool_pre_ping）就自动恢复正常。
_STARTUP_RETRIES = 30
_STARTUP_RETRY_INTERVAL = 2.0


def _retry_db_startup(label: str, fn) -> bool:
    """带退避地执行启动期数据库操作；全部失败时返回 False（调用方降级启动）。"""
    last: Exception | None = None
    for attempt in range(1, _STARTUP_RETRIES + 1):
        try:
            fn()
            if attempt > 1:
                logger.warning("数据库%s在第 %d 次尝试后恢复", label, attempt)
            return True
        except Exception as e:  # 含 pymysql 连接异常 / SQLAlchemy 异常
            last = e
            if attempt == 1 or attempt % 5 == 0:
                logger.warning(
                    "数据库%s失败（第 %d/%d 次）：%s；%ss 后重试",
                    label, attempt, _STARTUP_RETRIES, e, _STARTUP_RETRY_INTERVAL,
                )
            time.sleep(_STARTUP_RETRY_INTERVAL)
    logger.error(
        "数据库%s在 %d 次重试后仍失败，服务将以降级模式启动"
        "（进程保持存活，请求返回 503；数据库恢复后无需重启即可自愈）。最后错误：%s",
        label, _STARTUP_RETRIES, last,
    )
    return False


def init_db() -> bool:
    """建表 + 迁移。返回 True 表示成功；False 表示数据库暂不可用（降级启动）。

    绝不向上抛异常：调用方在 FastAPI 的 startup 钩子里，抛出去会让整个服务退出。
    """
    def _do():
        from admin import models  # noqa: F401  确保模型已注册

        Base.metadata.create_all(bind=engine)

        # 迁移：给 api_keys 表加 key_full 列（若不存在）
        _ensure_column("api_keys", "key_full", "VARCHAR(2048)", "DEFAULT ''")

        # 迁移：给 model_configs 表补齐倍率相关列（旧实例可能缺）
        _ensure_column("model_configs", "credit_multiplier", "FLOAT", "DEFAULT 0")
        _ensure_column("model_configs", "credits_raw", "VARCHAR(120)", "DEFAULT ''")

        # 迁移：给 schedules 表加 stop_after 列（daily_checkin 任务的「停止领取时间」）
        _ensure_column("schedules", "stop_after", "DATETIME", "NULL")

        # 迁移：给 usage_logs 表加详细用量与真实客户端 IP 列
        _ensure_column("usage_logs", "prompt_tokens", "INT", "NULL")
        _ensure_column("usage_logs", "completion_tokens", "INT", "NULL")
        _ensure_column("usage_logs", "total_tokens", "INT", "NULL")
        _ensure_column("usage_logs", "cached_tokens", "INT", "NULL")
        _ensure_column("usage_logs", "client_ip", "VARCHAR(64)", "DEFAULT ''")
        _ensure_column("usage_logs", "use_case", "VARCHAR(64)", "DEFAULT ''")
        # 迁移：给 usage_logs 表加请求级表格日志字段
        _ensure_column("usage_logs", "seq", "INT", "DEFAULT 0")
        _ensure_column("usage_logs", "ttfb_ms", "INT", "NULL")
        _ensure_column("usage_logs", "latency_ms", "INT", "NULL")
        _ensure_column("usage_logs", "error_kind", "VARCHAR(32)", "DEFAULT ''")
        # 迁移：auto-with-jev 门限的影子观测字段
        _ensure_column("usage_logs", "gate_model", "VARCHAR(64)", "DEFAULT ''")
        _ensure_column("usage_logs", "gate_conf", "FLOAT", "NULL")
        _ensure_column("usage_logs", "gate_ms", "INT", "NULL")
        _ensure_column("usage_logs", "gate_note", "VARCHAR(255)", "DEFAULT ''")

        # 迁移：给 accounts 表加稳定性状态机字段
        _ensure_column("accounts", "err_count", "INT", "DEFAULT 0")
        _ensure_column("accounts", "cool_until", "DATETIME", "NULL")
        _ensure_column("accounts", "cool_kind", "VARCHAR(24)", "DEFAULT ''")
        _ensure_column("accounts", "last_err_at", "DATETIME", "NULL")
        _ensure_column("accounts", "last_err_msg", "VARCHAR(255)", "DEFAULT ''")
        _ensure_column("accounts", "last_picked_at", "DATETIME", "NULL")

        # 迁移：加宽已存在但过窄的字符列（_ensure_column 只「缺则新增」，不会改宽度）。
        # cool_kind 早期建成 VARCHAR(16)，而代码会写入 "upstream_internal"（17 字符）；
        # MySQL 严格模式下该 UPDATE 报 1406 并被调用处的 rollback 静默吞掉，
        # 表现为「这类错误的账号冷却不落库」。
        _widen_column("accounts", "cool_kind", "VARCHAR(24)")

        # 迁移：熔断 / session-dead 连续计数 / 连败降权 / 快过期积分
        # 这些是「跨重启必须保留」的状态：重启后失忆会导致重新踩同一批雷
        # （冷账号被当健康号选中，再被上游拒一次）。
        _ensure_column("accounts", "breaker_until", "DATETIME", "NULL")
        _ensure_column("accounts", "breaker_fails", "INT", "DEFAULT 0")
        _ensure_column("accounts", "session_dead_fails", "INT", "DEFAULT 0")
        _ensure_column("accounts", "consecutive_fails", "INT", "DEFAULT 0")
        _ensure_column("accounts", "degrade_until", "DATETIME", "NULL")
        _ensure_column("accounts", "credits_expiring", "INT", "DEFAULT 0")

        # 迁移：api_keys 加 group_id 列（绑定模型分组；0=不限制）
        _ensure_column("api_keys", "group_id", "INT", "DEFAULT 0")

        # 迁移：创建 system_settings / schedules 表（create_all 已处理，这里仅兜底）

    return _retry_db_startup("建表/迁移", _do)


def wait_database_ready() -> bool:
    """确保数据库存在（带重试）。返回 False 表示暂不可用，调用方降级启动。"""
    return _retry_db_startup("连接/建库", ensure_database)


def _ensure_column(table: str, col: str, col_type: str, default: str = ""):
    """检查并添加缺失的列（MySQL 兼容）。表名/列名经白名单校验；其余参数走绑定。"""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:tbl AND COLUMN_NAME=:col"
                ),
                {"tbl": table, "col": col},
            )
            exists = result.scalar() or 0
            if not exists:
                # 表名/列名为编译期常量，经白名单校验后安全内插；col_type/default 亦为常量
                conn.execute(
                    text(
                        f"ALTER TABLE `{_safe_ident(table)}` "
                        f"ADD COLUMN `{_safe_ident(col)}` {col_type} {default}"
                    )
                )
                conn.commit()
    except Exception:
        pass  # 非 MySQL 或权限不足时静默跳过


def _widen_column(table: str, col: str, col_type: str):
    """把已存在但定义不同的字符列改成 col_type（幂等）。

    与 `_ensure_column` 互补：后者只管「缺则新增」，对已存在但过窄的列无能为力。
    仅当现有类型与目标不同才 ALTER，避免每次启动都做无谓的 DDL。

    注意：这是**放宽**型变更（VARCHAR(16) → VARCHAR(24)），不会截断既有数据。
    表名/列名经白名单校验，col_type 为编译期常量。
    """
    try:
        with engine.connect() as conn:
            cur_type = conn.execute(
                text(
                    "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:tbl AND COLUMN_NAME=:col"
                ),
                {"tbl": table, "col": col},
            ).scalar()
            if not cur_type:
                return  # 列不存在：由 _ensure_column 负责创建
            if str(cur_type).lower() == col_type.lower():
                return  # 已是目标宽度，幂等返回
            conn.execute(
                text(
                    f"ALTER TABLE `{_safe_ident(table)}` "
                    f"MODIFY COLUMN `{_safe_ident(col)}` {col_type}"
                )
            )
            conn.commit()
    except Exception:
        pass  # 非 MySQL 或权限不足时静默跳过

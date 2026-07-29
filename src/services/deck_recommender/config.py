from utils import *


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔环境变量，避免 Python 的 bool("0") 误判。"""

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# ================================ 多实例配置 ================================ #
# 同一份服务代码可分别运行正式实例与 preview 实例；环境变量优先，便于 Compose
# 复用同一镜像而不复制生产配置文件。
CONFIG = {}
CONFIG_PATH = os.getenv(
    "DECKREC_CONFIG_PATH",
    pjoin(os.path.dirname(os.path.abspath(__file__)), "config.yaml"),
)
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CONFIG = yaml.safe_load(f) or {}
else:
    log(f"未找到配置文件 {CONFIG_PATH}，使用默认配置")

HOST = os.getenv("DECKREC_HOST", CONFIG.get("host", "127.0.0.1"))
PORT = int(os.getenv("DECKREC_PORT", CONFIG.get("port", 45556)))
WORKER_NUM = int(os.getenv("DECKREC_WORKER_NUM", CONFIG.get("worker_num", 1)))
DATA_DIR = os.getenv(
    "DECKREC_DATA_DIR",
    CONFIG.get("data_dir", "lunabot_deckrec_data"),
)
USERDATA_CACHE_NUM = int(
    os.getenv(
        "DECKREC_USERDATA_CACHE_NUM",
        CONFIG.get("userdata_cache_num", 10),
    )
)
INSTANCE_ROLE = os.getenv(
    "DECKREC_INSTANCE_ROLE",
    CONFIG.get("instance_role", "official"),
).strip().lower()
DATA_SCOPE = os.getenv(
    "DECKREC_DATA_SCOPE",
    CONFIG.get("data_scope", "official"),
).strip()
NATIVE_REGION = os.getenv(
    "DECKREC_NATIVE_REGION",
    CONFIG.get("native_region", ""),
).strip().lower()
ATOMIC_DATA_PUBLISH = _env_bool(
    "DECKREC_ATOMIC_DATA_PUBLISH",
    bool(CONFIG.get("atomic_data_publish", INSTANCE_ROLE == "preview")),
)
DB_PATH = pjoin(DATA_DIR, "deckrec.json")
ACTIVE_POINTER_PATH = pjoin(DATA_DIR, "active.json")


# ================================ Preview实例失败关闭 ================================ #
# preview 配置缺失时必须直接拒绝启动，不能意外占用正式端口或正式数据目录。
if INSTANCE_ROLE not in {"official", "preview"}:
    raise RuntimeError(f"未知组卡服务 instance_role: {INSTANCE_ROLE}")
if WORKER_NUM <= 0:
    raise RuntimeError("组卡服务 worker_num 必须大于 0")
if INSTANCE_ROLE == "preview":
    if DATA_SCOPE != "cn_jp_preview_v1":
        raise RuntimeError("preview 实例必须显式配置 data_scope=cn_jp_preview_v1")
    if NATIVE_REGION != "cn":
        raise RuntimeError("preview 实例必须显式配置 native_region=cn")
    if PORT == 45556:
        raise RuntimeError("preview 实例不能使用正式组卡默认端口 45556")
    normalized_data_dir = os.path.normcase(os.path.abspath(DATA_DIR))
    official_default_dir = os.path.normcase(
        os.path.abspath("data/sekai/deckrec")
    )
    if normalized_data_dir == official_default_dir:
        raise RuntimeError("preview 实例不能使用正式组卡数据目录")
    if not ATOMIC_DATA_PUBLISH:
        raise RuntimeError("preview 实例必须启用 atomic_data_publish")

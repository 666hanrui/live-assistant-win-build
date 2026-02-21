# 配置文件
import os
from pathlib import Path


def _load_local_env():
    """
    轻量加载 .env（不依赖 python-dotenv）
    仅加载尚未存在于系统环境变量中的键。
    搜索顺序：
    1) LIVE_ASSISTANT_ENV 指定文件
    2) 当前工作目录 .env
    3) 项目根目录 .env（兼容源码运行）
    """
    candidates = []
    env_override = os.getenv("LIVE_ASSISTANT_ENV", "").strip()
    if env_override:
        candidates.append(Path(env_override).expanduser())
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")

    seen = set()
    for env_path in candidates:
        key = str(env_path.resolve()) if env_path.exists() else str(env_path)
        if key in seen:
            continue
        seen.add(key)
        if not env_path.exists():
            continue

        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, value = line.split("=", 1)
            k = k.strip()
            value = value.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = value
        break


_load_local_env()


def _split_csv_env(name, default=""):
    raw = os.getenv(name, default)
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _split_csv_lower_env(name, default=""):
    return [x.lower() for x in _split_csv_env(name, default)]


def _env_bool(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


LOCAL_FIRST_MODE = _env_bool("LOCAL_FIRST_MODE", "true")

# TikTok 直播间管理后台 URL (需要用户手动配置或通过脚本登录)
TIKTOK_LIVE_URL = "https://www.tiktok.com/live/dashboard"  # 示例地址，实际可能不同

# 浏览器配置
BROWSER_PORT = int(os.getenv("BROWSER_PORT", "9222"))
USER_DATA_PATH = os.getenv("USER_DATA_PATH", "./user_data")
CHROME_EXECUTABLE = os.getenv("CHROME_EXECUTABLE", "").strip()
STARTUP_CONNECT_RETRIES = int(os.getenv("STARTUP_CONNECT_RETRIES", "3"))
STARTUP_CONNECT_RETRY_INTERVAL_SECONDS = float(os.getenv("STARTUP_CONNECT_RETRY_INTERVAL_SECONDS", "1.0"))
STARTUP_THREAD_READY_TIMEOUT_SECONDS = float(os.getenv("STARTUP_THREAD_READY_TIMEOUT_SECONDS", "0.25"))
TIKTOK_FORCE_LIVE_TAB = os.getenv("TIKTOK_FORCE_LIVE_TAB", "true").lower() in ("1", "true", "yes", "on")
TIKTOK_TAB_MIN_SCORE = int(os.getenv("TIKTOK_TAB_MIN_SCORE", "3"))
TIKTOK_TAB_REQUIRED_KEYWORDS = _split_csv_lower_env(
    "TIKTOK_TAB_REQUIRED_KEYWORDS",
    "tiktok,live,直播"
)
TIKTOK_TAB_EXCLUDE_KEYWORDS = _split_csv_lower_env(
    "TIKTOK_TAB_EXCLUDE_KEYWORDS",
    "google 搜索,new tab,新标签页,推荐,for you,search,搜索,localhost,127.0.0.1"
)

# 内置 TikTok Shop Mock 测试页（项目内文件）
MOCK_SHOP_AUTO_CONNECT = os.getenv("MOCK_SHOP_AUTO_CONNECT", "false").lower() in ("1", "true", "yes", "on")
MOCK_SHOP_DEFAULT_VIEW = os.getenv("MOCK_SHOP_DEFAULT_VIEW", "dashboard_live").strip() or "dashboard_live"
MOCK_SHOP_CONNECT_RETRIES = int(os.getenv("MOCK_SHOP_CONNECT_RETRIES", "3"))
MOCK_SHOP_CONNECT_RETRY_INTERVAL_SECONDS = float(os.getenv("MOCK_SHOP_CONNECT_RETRY_INTERVAL_SECONDS", "0.35"))

# LLM 配置 (DeepSeek)
# 建议仅通过环境变量设置 API KEY，默认留空避免泄露
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "deepseek-chat")
LLM_REMOTE_ENABLED = _env_bool("LLM_REMOTE_ENABLED", "true")

# 语言配置（手动切换）
REPLY_LANGUAGES = {
    "简体中文": "zh-CN",
    "English": "en-US",
    "Español": "es-ES",
}
DEFAULT_REPLY_LANGUAGE = "zh-CN"

# 关键词回复配置（多语言）
KEYWORD_REPLIES = {
    "尺码": {
        "zh-CN": "亲，我们的尺码是标准的，您可参考详情页尺码表哦~",
        "en-US": "Our sizing is standard. Please check the size chart on the product page.",
        "es-ES": "Nuestras tallas son estándar. Revisa la guía de tallas en la página del producto.",
    },
    "size": {
        "zh-CN": "亲，我们的尺码是标准的，您可参考详情页尺码表哦~",
        "en-US": "Our sizing is standard. Please check the size chart on the product page.",
        "es-ES": "Nuestras tallas son estándar. Revisa la guía de tallas en la página del producto.",
    },
    "发货": {
        "zh-CN": "下单后48小时内发货哦，亲请耐心等待~",
        "en-US": "Orders are usually shipped within 48 hours after payment.",
        "es-ES": "Normalmente enviamos dentro de 48 horas después del pago.",
    },
    "shipping": {
        "zh-CN": "下单后48小时内发货哦，亲请耐心等待~",
        "en-US": "Orders are usually shipped within 48 hours after payment.",
        "es-ES": "Normalmente enviamos dentro de 48 horas después del pago.",
    },
    "delivery": {
        "zh-CN": "下单后48小时内发货哦，亲请耐心等待~",
        "en-US": "Orders are usually shipped within 48 hours after payment.",
        "es-ES": "Normalmente enviamos dentro de 48 horas después del pago.",
    },
    "refund": {
        "zh-CN": "支持售后服务，有问题可以联系在线客服处理哦~",
        "en-US": "We provide after-sales support. If needed, contact customer service for refund/return help.",
        "es-ES": "Ofrecemos soporte posventa. Si lo necesitas, contacta al servicio al cliente para devoluciones o reembolsos.",
    },
    "return": {
        "zh-CN": "支持售后服务，有问题可以联系在线客服处理哦~",
        "en-US": "We provide after-sales support. If needed, contact customer service for refund/return help.",
        "es-ES": "Ofrecemos soporte posventa. Si lo necesitas, contacta al servicio al cliente para devoluciones o reembolsos.",
    },
    "优惠": {
        "zh-CN": "关注直播间领取优惠券，下单更划算哦！",
        "en-US": "Follow the stream to claim coupons before checkout for a better deal!",
        "es-ES": "Sigue el directo para obtener cupones antes de comprar.",
    },
    "coupon": {
        "zh-CN": "关注直播间领取优惠券，下单更划算哦！",
        "en-US": "Follow the stream to claim coupons before checkout for a better deal!",
        "es-ES": "Sigue el directo para obtener cupones antes de comprar.",
    },
    "discount": {
        "zh-CN": "关注直播间领取优惠券，下单更划算哦！",
        "en-US": "Follow the stream to claim coupons before checkout for a better deal!",
        "es-ES": "Sigue el directo para obtener cupones antes de comprar.",
    },
    "price": {
        "zh-CN": "价格会随活动变化，建议以当前链接页显示为准哦~",
        "en-US": "Prices can change during live promotions. Please check the current product card for the latest price.",
        "es-ES": "Los precios pueden cambiar durante promociones en vivo. Revisa la tarjeta del producto para el precio actual.",
    },
    "材质": {
        "zh-CN": "这款主打舒适亲肤，具体材质看详情页参数哦~",
        "en-US": "This item is skin-friendly and comfortable. Exact materials are listed on the product page.",
        "es-ES": "Es cómodo y suave para la piel. El material exacto está en la página del producto.",
    },
    "material": {
        "zh-CN": "这款主打舒适亲肤，具体材质看详情页参数哦~",
        "en-US": "This item is skin-friendly and comfortable. Exact materials are listed on the product page.",
        "es-ES": "Es cómodo y suave para la piel. El material exacto está en la página del producto.",
    },
    "quality": {
        "zh-CN": "这款主打舒适亲肤，做工和质感都不错，详情可看商品页参数~",
        "en-US": "This item focuses on comfort and feel. You can check detailed specs on the product page.",
        "es-ES": "Este producto prioriza comodidad y tacto. Puedes revisar los detalles en la página del producto.",
    },
}

# 自动回复频率限制 (秒)
REPLY_INTERVAL = float(os.getenv("REPLY_INTERVAL_SECONDS", "0.35"))
SEND_MESSAGE_MAX_CHARS = 140
LLM_REPLY_MAX_CHARS = 80
LLM_REQUEST_TIMEOUT_SECONDS = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "3.5"))
LLM_MAX_TOKENS = 64
LLM_MIN_INTERVAL_SECONDS = float(os.getenv("LLM_MIN_INTERVAL_SECONDS", "0.8"))
MAX_MESSAGES_PER_CYCLE = int(os.getenv("MAX_MESSAGES_PER_CYCLE", "3"))

# 主循环节奏（降低体感延迟）
MAIN_LOOP_BUSY_INTERVAL_SECONDS = float(os.getenv("MAIN_LOOP_BUSY_INTERVAL_SECONDS", "0.16"))
MAIN_LOOP_IDLE_INTERVAL_SECONDS = float(os.getenv("MAIN_LOOP_IDLE_INTERVAL_SECONDS", "0.32"))
MAIN_LOOP_ERROR_BACKOFF_SECONDS = float(os.getenv("MAIN_LOOP_ERROR_BACKOFF_SECONDS", "0.7"))
NO_CHAT_WARN_INTERVAL_ROUNDS = int(os.getenv("NO_CHAT_WARN_INTERVAL_ROUNDS", "15"))
NO_CHAT_FORCE_RECONNECT_ROUNDS = int(os.getenv("NO_CHAT_FORCE_RECONNECT_ROUNDS", "90"))

# RAG / 向量化配置
EMBEDDING_MODEL_NAME = os.getenv(
    "EMBEDDING_MODEL_NAME",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
EMBEDDING_LOCAL_FILES_ONLY = os.getenv("EMBEDDING_LOCAL_FILES_ONLY", "true").lower() in ("1", "true", "yes", "on")
EMBEDDING_ENABLE_ONLINE_FALLBACK = os.getenv("EMBEDDING_ENABLE_ONLINE_FALLBACK", "false").lower() in ("1", "true", "yes", "on")
EMBEDDING_CACHE_DIR = os.getenv("EMBEDDING_CACHE_DIR", "").strip()
HF_HUB_ETAG_TIMEOUT_SECONDS = int(os.getenv("HF_HUB_ETAG_TIMEOUT_SECONDS", "2"))
HF_HUB_DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("HF_HUB_DOWNLOAD_TIMEOUT_SECONDS", "8"))
RAG_CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "240"))
RAG_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "60"))
RAG_RETRIEVAL_K = int(os.getenv("RAG_RETRIEVAL_K", "4"))
RAG_RETRIEVAL_FETCH_K = int(os.getenv("RAG_RETRIEVAL_FETCH_K", "12"))
RAG_MIN_RELEVANCE_SCORE = float(os.getenv("RAG_MIN_RELEVANCE_SCORE", "0.25"))

# 运营动作口令（主播说“置顶/秒杀上架”）
# 可在 .env 中配置：COMMAND_ALLOWED_USERS=bonusmom70,主播名2
COMMAND_ALLOWED_USERS = [u.lower() for u in _split_csv_env("COMMAND_ALLOWED_USERS")]
# 是否启用“运营口令发言人白名单”校验（默认关闭：不限制谁发送）
COMMAND_REQUIRE_ALLOWED_USERS = os.getenv(
    "COMMAND_REQUIRE_ALLOWED_USERS",
    "false",
).lower() in ("1", "true", "yes", "on")
# 弹幕运营动作冷却（秒）：用于抑制 OCR/DOM 重复抽取导致的同动作连触发。
DANMU_COMMAND_COOLDOWN_SECONDS = float(os.getenv("DANMU_COMMAND_COOLDOWN_SECONDS", "6"))
# screen_ocr 模式下，是否要求弹幕包含唤醒词才允许执行运营动作。
SCREEN_OCR_DANMU_REQUIRE_WAKE_WORD = os.getenv(
    "SCREEN_OCR_DANMU_REQUIRE_WAKE_WORD",
    "true",
).lower() in ("1", "true", "yes", "on")

# 运营执行模式：
# - dom: 显式开启 DOM 执行链路（仅手动选择时建议使用）
# - ocr_vision: OCR 视觉驱动模式（默认）
OPERATION_EXECUTION_MODE = os.getenv("OPERATION_EXECUTION_MODE", "ocr_vision").strip().lower()
OCR_VISION_PRECHECK_ENABLED = os.getenv("OCR_VISION_PRECHECK_ENABLED", "true").lower() in ("1", "true", "yes", "on")
OCR_VISION_REQUIRED_KEYWORDS = _split_csv_env("OCR_VISION_REQUIRED_KEYWORDS", "直播控制台,商品,活动,chat,viewer")
OCR_VISION_ALLOW_DOM_FALLBACK = os.getenv("OCR_VISION_ALLOW_DOM_FALLBACK", "false").lower() in ("1", "true", "yes", "on")
HUMAN_LIKE_ACTION_ENABLED = os.getenv("HUMAN_LIKE_ACTION_ENABLED", "true").lower() in ("1", "true", "yes", "on")
HUMAN_LIKE_ACTION_DELAY_MIN_SECONDS = float(os.getenv("HUMAN_LIKE_ACTION_DELAY_MIN_SECONDS", "0.04"))
HUMAN_LIKE_ACTION_DELAY_MAX_SECONDS = float(os.getenv("HUMAN_LIKE_ACTION_DELAY_MAX_SECONDS", "0.20"))
HUMAN_LIKE_ACTION_POST_DELAY_MIN_SECONDS = float(os.getenv("HUMAN_LIKE_ACTION_POST_DELAY_MIN_SECONDS", "0.03"))
HUMAN_LIKE_ACTION_POST_DELAY_MAX_SECONDS = float(os.getenv("HUMAN_LIKE_ACTION_POST_DELAY_MAX_SECONDS", "0.16"))
HUMAN_LIKE_CLICK_JITTER_PX = float(os.getenv("HUMAN_LIKE_CLICK_JITTER_PX", "1.8"))
OCR_VISION_FORCE_PHYSICAL_CLICK = os.getenv("OCR_VISION_FORCE_PHYSICAL_CLICK", "true").lower() in ("1", "true", "yes", "on")
# 视口坐标 -> 屏幕坐标映射微调（物理点击链路）
OCR_VIEWPORT_TO_SCREEN_USE_BORDER_COMPENSATION = os.getenv(
    "OCR_VIEWPORT_TO_SCREEN_USE_BORDER_COMPENSATION",
    "false",
).lower() in ("1", "true", "yes", "on")
OCR_VIEWPORT_TO_SCREEN_SCALE = float(os.getenv("OCR_VIEWPORT_TO_SCREEN_SCALE", "1.0"))
OCR_VIEWPORT_TO_SCREEN_OFFSET_X = float(os.getenv("OCR_VIEWPORT_TO_SCREEN_OFFSET_X", "0"))
OCR_VIEWPORT_TO_SCREEN_OFFSET_Y = float(os.getenv("OCR_VIEWPORT_TO_SCREEN_OFFSET_Y", "0"))
# screen_ocr 坐标映射兜底（Mac Retina 某些环境会出现点击整体偏右下）
SCREEN_OCR_MAC_RETINA_HALF_SCALE_FALLBACK = os.getenv(
    "SCREEN_OCR_MAC_RETINA_HALF_SCALE_FALLBACK",
    "true",
).lower() in ("1", "true", "yes", "on")
SCREEN_OCR_MAC_RETINA_HALF_SCALE_RATIO = float(os.getenv("SCREEN_OCR_MAC_RETINA_HALF_SCALE_RATIO", "0.5"))
FORCE_FULL_PHYSICAL_MOUSE_KEYBOARD = os.getenv("FORCE_FULL_PHYSICAL_MOUSE_KEYBOARD", "false").lower() in ("1", "true", "yes", "on")
OCR_ACTION_RETRY_WAIT_SECONDS = float(os.getenv("OCR_ACTION_RETRY_WAIT_SECONDS", "30"))
OCR_ACTION_RETRY_POLL_SECONDS = float(os.getenv("OCR_ACTION_RETRY_POLL_SECONDS", "0.65"))
OCR_ACTION_REACTION_LLM_ENABLED = os.getenv("OCR_ACTION_REACTION_LLM_ENABLED", "true").lower() in ("1", "true", "yes", "on")
OCR_ACTION_REACTION_LLM_MAX_CHECKS = int(os.getenv("OCR_ACTION_REACTION_LLM_MAX_CHECKS", "4"))
OCR_ACTION_REACTION_LLM_MIN_INTERVAL_SECONDS = float(os.getenv("OCR_ACTION_REACTION_LLM_MIN_INTERVAL_SECONDS", "1.2"))
# OCR 点击测试模式（pin/unpin）：
# 将点击点固定为“相对每个链接行”的位置，并在点击后展示结果提示（成功/超时失败）。
OCR_PIN_FIXED_ROW_CLICK_ENABLED = os.getenv("OCR_PIN_FIXED_ROW_CLICK_ENABLED", "true").lower() in ("1", "true", "yes", "on")
# pin/unpin 统一执行策略：默认强制只走“固定行相对点击”。
PIN_UNPIN_FORCE_FIXED_ROW_CLICK = os.getenv("PIN_UNPIN_FORCE_FIXED_ROW_CLICK", "true").lower() in ("1", "true", "yes", "on")
# link_index 为空时是否允许执行 pin/unpin：默认必须提供序号，避免误点。
PIN_UNPIN_REQUIRE_LINK_INDEX = os.getenv("PIN_UNPIN_REQUIRE_LINK_INDEX", "true").lower() in ("1", "true", "yes", "on")
OCR_PIN_FIXED_ROW_CLICK_X_RATIO = float(os.getenv("OCR_PIN_FIXED_ROW_CLICK_X_RATIO", "0"))
OCR_PIN_FIXED_ROW_CLICK_RIGHT_PADDING_PX = float(os.getenv("OCR_PIN_FIXED_ROW_CLICK_RIGHT_PADDING_PX", "56"))
OCR_PIN_FIXED_ROW_CLICK_RIGHT_PADDING_RATIO = float(os.getenv("OCR_PIN_FIXED_ROW_CLICK_RIGHT_PADDING_RATIO", "0.06"))
OCR_PIN_FIXED_ROW_CLICK_PANEL_X_RATIO = float(os.getenv("OCR_PIN_FIXED_ROW_CLICK_PANEL_X_RATIO", "0.90"))
OCR_PIN_FIXED_ROW_CLICK_OFFSET_X_PX = float(os.getenv("OCR_PIN_FIXED_ROW_CLICK_OFFSET_X_PX", "0"))
OCR_PIN_FIXED_ROW_CLICK_OFFSET_X_RATIO = float(os.getenv("OCR_PIN_FIXED_ROW_CLICK_OFFSET_X_RATIO", "0.30"))
OCR_PIN_FIXED_ROW_CLICK_OFFSET_Y_PX = float(os.getenv("OCR_PIN_FIXED_ROW_CLICK_OFFSET_Y_PX", "0"))
OCR_PIN_FIXED_ROW_CLICK_OFFSET_Y_RATIO = float(os.getenv("OCR_PIN_FIXED_ROW_CLICK_OFFSET_Y_RATIO", "0"))
OCR_PIN_CLICK_TEST_CONFIRM_POPUP = os.getenv("OCR_PIN_CLICK_TEST_CONFIRM_POPUP", "false").lower() in ("1", "true", "yes", "on")
OCR_PIN_CLICK_TEST_MAX_WAIT_SECONDS = float(os.getenv("OCR_PIN_CLICK_TEST_MAX_WAIT_SECONDS", "3.8"))
OCR_PIN_FIXED_ROW_CALIBRATION_LOG_ENABLED = os.getenv("OCR_PIN_FIXED_ROW_CALIBRATION_LOG_ENABLED", "true").lower() in ("1", "true", "yes", "on")
OCR_PIN_FIXED_ROW_CALIBRATION_LOG_PATH = os.getenv(
    "OCR_PIN_FIXED_ROW_CALIBRATION_LOG_PATH",
    "data/reports/pin_click_calibration.jsonl",
).strip()
# 运营 JS 执行时延控制：降低跨 frame 执行卡顿导致的单步 10s 阻塞
OPS_RUN_JS_TIMEOUT_SECONDS = float(os.getenv("OPS_RUN_JS_TIMEOUT_SECONDS", "1.2"))
OPS_RUN_JS_FALLBACK_TIMEOUT_SECONDS = float(os.getenv("OPS_RUN_JS_FALLBACK_TIMEOUT_SECONDS", "0.45"))
OPS_RUN_JS_MAX_CONTEXTS = int(os.getenv("OPS_RUN_JS_MAX_CONTEXTS", "3"))
OPS_RUN_JS_INCLUDE_FRAMES = os.getenv("OPS_RUN_JS_INCLUDE_FRAMES", "false").lower() in ("1", "true", "yes", "on")
# 真实页执行前，允许从 tiktok.com/live/dashboard 自动跳转到可操作的 Shop 控制台页
ACTION_PAGE_SHOP_DASHBOARD_URLS = _split_csv_env(
    "ACTION_PAGE_SHOP_DASHBOARD_URLS",
    "https://shop.tiktok.com/streamer/live/product/dashboard",
)
OS_KEYBOARD_INPUT_FALLBACK_ENABLED = os.getenv("OS_KEYBOARD_INPUT_FALLBACK_ENABLED", "false").lower() in ("1", "true", "yes", "on")
OS_KEYBOARD_TYPING_MIN_INTERVAL_SECONDS = float(os.getenv("OS_KEYBOARD_TYPING_MIN_INTERVAL_SECONDS", "0.018"))
OS_KEYBOARD_TYPING_MAX_INTERVAL_SECONDS = float(os.getenv("OS_KEYBOARD_TYPING_MAX_INTERVAL_SECONDS", "0.055"))
MESSAGE_KEYBOARD_ONLY_ENABLED = os.getenv("MESSAGE_KEYBOARD_ONLY_ENABLED", "true").lower() in ("1", "true", "yes", "on")
MESSAGE_KEYBOARD_INPUT_MODE = os.getenv("MESSAGE_KEYBOARD_INPUT_MODE", "type").strip().lower()
# 运营动作受限计划器（LLM 只做决策，执行层走白名单 API）
OPS_LLM_PLAN_ENABLED = os.getenv("OPS_LLM_PLAN_ENABLED", "false").lower() in ("1", "true", "yes", "on")
OPS_LLM_PLAN_SHADOW_MODE = os.getenv("OPS_LLM_PLAN_SHADOW_MODE", "true").lower() in ("1", "true", "yes", "on")
OPS_LLM_PLAN_MAX_STEPS = int(os.getenv("OPS_LLM_PLAN_MAX_STEPS", "3"))
OPS_LLM_PLAN_MAX_RETRIES = int(os.getenv("OPS_LLM_PLAN_MAX_RETRIES", "1"))
OPS_LLM_PLAN_TIMEOUT_SECONDS = float(os.getenv("OPS_LLM_PLAN_TIMEOUT_SECONDS", "18"))
OPS_LLM_PLAN_MIN_CONFIDENCE = float(os.getenv("OPS_LLM_PLAN_MIN_CONFIDENCE", "0.55"))
OPS_LLM_PLAN_NEXT_STEP_ENABLED = os.getenv("OPS_LLM_PLAN_NEXT_STEP_ENABLED", "true").lower() in ("1", "true", "yes", "on")
OPS_LLM_PLAN_NEXT_STEP_MAX_TURNS = int(os.getenv("OPS_LLM_PLAN_NEXT_STEP_MAX_TURNS", "6"))
OPS_LLM_PLAN_NEXT_STEP_MIN_CONFIDENCE = float(
    os.getenv("OPS_LLM_PLAN_NEXT_STEP_MIN_CONFIDENCE", "0.50")
)
OPS_LLM_PLAN_SITUATION_DRIVEN = os.getenv("OPS_LLM_PLAN_SITUATION_DRIVEN", "true").lower() in ("1", "true", "yes", "on")
OPS_LLM_PLAN_REPLAY_PATH = os.getenv(
    "OPS_LLM_PLAN_REPLAY_PATH",
    "data/reports/operation_plan_replay.jsonl",
).strip()
# 运营动作 LLM 导航兜底（规则链未命中时启用）
OPS_LLM_NAVIGATION_ENABLED = os.getenv("OPS_LLM_NAVIGATION_ENABLED", "true").lower() in ("1", "true", "yes", "on")
OPS_LLM_NAVIGATION_UNKNOWN_PAGE_ENABLED = os.getenv("OPS_LLM_NAVIGATION_UNKNOWN_PAGE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
OPS_LLM_NAVIGATION_MIN_CONFIDENCE = float(os.getenv("OPS_LLM_NAVIGATION_MIN_CONFIDENCE", "0.58"))
OPS_LLM_NAVIGATION_MAX_SCROLL_ROUNDS = int(os.getenv("OPS_LLM_NAVIGATION_MAX_SCROLL_ROUNDS", "6"))
OPS_LLM_NAVIGATION_MAX_LLM_CALLS = int(os.getenv("OPS_LLM_NAVIGATION_MAX_LLM_CALLS", "3"))
OPS_LLM_NAVIGATION_MIN_INTERVAL_SECONDS = float(os.getenv("OPS_LLM_NAVIGATION_MIN_INTERVAL_SECONDS", "0.9"))
OPS_LLM_NAVIGATION_SCROLL_COOLDOWN_SECONDS = float(os.getenv("OPS_LLM_NAVIGATION_SCROLL_COOLDOWN_SECONDS", "0.32"))
OPS_LLM_NAVIGATION_SCROLL_PIXELS = int(os.getenv("OPS_LLM_NAVIGATION_SCROLL_PIXELS", "180"))

# 网页信息读取模式：
# - dom: 主要依赖 DOM 信息读取
# - ocr_hybrid: 优先 OCR（文本+图片特征）读取，必要时回退 DOM
# - ocr_only: 严格 OCR 信息读取（页面上下文/弹幕抽取/动作回执均不读取 DOM 文本）
# - screen_ocr: 纯屏幕 OCR 信息读取（完全不读取浏览器 DOM 文本，浏览器仅作为被观察画面）
WEB_INFO_SOURCE_MODE = os.getenv("WEB_INFO_SOURCE_MODE", "ocr_only").strip().lower()
OCR_CHAT_MAX_MESSAGES = int(os.getenv("OCR_CHAT_MAX_MESSAGES", "6"))
SCREEN_CAPTURE_MONITOR_INDEX = int(os.getenv("SCREEN_CAPTURE_MONITOR_INDEX", "1"))
# screen_ocr 弹幕固定区域裁剪（相对整屏比例，优先用于聊天抓取）
SCREEN_OCR_FIXED_CHAT_REGION_ENABLED = os.getenv(
    "SCREEN_OCR_FIXED_CHAT_REGION_ENABLED",
    "true",
).lower() in ("1", "true", "yes", "on")
SCREEN_OCR_FIXED_CHAT_REGION_X1_RATIO = float(os.getenv("SCREEN_OCR_FIXED_CHAT_REGION_X1_RATIO", "0.44"))
SCREEN_OCR_FIXED_CHAT_REGION_Y1_RATIO = float(os.getenv("SCREEN_OCR_FIXED_CHAT_REGION_Y1_RATIO", "0.43"))
SCREEN_OCR_FIXED_CHAT_REGION_X2_RATIO = float(os.getenv("SCREEN_OCR_FIXED_CHAT_REGION_X2_RATIO", "0.72"))
SCREEN_OCR_FIXED_CHAT_REGION_Y2_RATIO = float(os.getenv("SCREEN_OCR_FIXED_CHAT_REGION_Y2_RATIO", "0.90"))
# screen_ocr 聊天 ROI 可视化调试（落盘标注图）
SCREEN_OCR_CHAT_ROI_DEBUG_ENABLED = os.getenv(
    "SCREEN_OCR_CHAT_ROI_DEBUG_ENABLED",
    "false",
).lower() in ("1", "true", "yes", "on")
SCREEN_OCR_CHAT_ROI_DEBUG_DIR = os.getenv(
    "SCREEN_OCR_CHAT_ROI_DEBUG_DIR",
    "logs/screen_chat_roi_debug",
).strip()
SCREEN_OCR_CHAT_ROI_DEBUG_INTERVAL_SECONDS = float(
    os.getenv("SCREEN_OCR_CHAT_ROI_DEBUG_INTERVAL_SECONDS", "2.0")
)
SCREEN_OCR_CHAT_ROI_DEBUG_MAX_FILES = int(os.getenv("SCREEN_OCR_CHAT_ROI_DEBUG_MAX_FILES", "240"))

# 语音口令监听（物理听到主播说话）
# 模式：
# - python_asr: 本地 Python 麦克风采集 + ASR（默认，依赖系统麦克风权限，不依赖 TikTok 网页权限）
# - system_loopback_asr: 本机系统回采（浏览器播放音频）+ ASR（需配置回采设备，如 BlackHole/Stereo Mix/VB-CABLE）
# - tab_audio_asr / system_audio_asr: 直接抓取当前页面播放器音频流（不录屏、不录麦）
# - web_speech: 浏览器 Web Speech API（依赖 TikTok 网页权限）
VOICE_COMMAND_INPUT_MODE = os.getenv("VOICE_COMMAND_INPUT_MODE", "python_asr").strip().lower()
VOICE_COMMAND_ENABLED = os.getenv("VOICE_COMMAND_ENABLED", "false").lower() in ("1", "true", "yes", "on")
VOICE_COMMAND_POLL_INTERVAL_SECONDS = float(os.getenv("VOICE_COMMAND_POLL_INTERVAL_SECONDS", "0.6"))
VOICE_COMMAND_COOLDOWN_SECONDS = int(os.getenv("VOICE_COMMAND_COOLDOWN_SECONDS", "6"))
VOICE_SUBTITLE_FALLBACK_ENABLED = os.getenv("VOICE_SUBTITLE_FALLBACK_ENABLED", "false").lower() in ("1", "true", "yes", "on")
VOICE_COMMAND_SILENCE_RESTART_SECONDS = int(os.getenv("VOICE_COMMAND_SILENCE_RESTART_SECONDS", "18"))
VOICE_COMMAND_HEALTH_LOG_INTERVAL_SECONDS = int(os.getenv("VOICE_COMMAND_HEALTH_LOG_INTERVAL_SECONDS", "15"))
VOICE_COMMAND_FORCE_RESTART_MIN_SECONDS = int(os.getenv("VOICE_COMMAND_FORCE_RESTART_MIN_SECONDS", "120"))
VOICE_COMMAND_FALLBACK_LANGUAGES = _split_csv_env("VOICE_COMMAND_FALLBACK_LANGUAGES", "zh-CN,en-US")
VOICE_COMMAND_CROSS_LANGUAGE_ENABLED = os.getenv("VOICE_COMMAND_CROSS_LANGUAGE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
VOICE_COMMAND_CROSS_LANGUAGE_ORDER = _split_csv_env("VOICE_COMMAND_CROSS_LANGUAGE_ORDER", "zh-CN,en-US")
VOICE_COMMAND_MAX_LANGS = int(os.getenv("VOICE_COMMAND_MAX_LANGS", "2"))
# provider: whisper_local / dashscope_funasr / hybrid_local_cloud / auto / google / sphinx
VOICE_PYTHON_ASR_PROVIDER = os.getenv("VOICE_PYTHON_ASR_PROVIDER", "whisper_local").strip().lower()
# dashscope_funasr 仅云上模式时，强制走系统回采（loopback），不走本地实体麦克风。
VOICE_DASHSCOPE_FORCE_LOOPBACK = os.getenv("VOICE_DASHSCOPE_FORCE_LOOPBACK", "true").lower() in ("1", "true", "yes", "on")
VOICE_ASR_ALLOW_GOOGLE_FALLBACK = os.getenv("VOICE_ASR_ALLOW_GOOGLE_FALLBACK", "false").lower() in ("1", "true", "yes", "on")
VOICE_ASR_AUTO_SWITCH_ON_TIMEOUT = os.getenv("VOICE_ASR_AUTO_SWITCH_ON_TIMEOUT", "false").lower() in ("1", "true", "yes", "on")
# 兼容旧配置：已不再用于自动注入 DashScope 备用链路（改为通过 VOICE_PYTHON_ASR_PROVIDER 显式选择）
VOICE_ASR_ALLOW_DASHSCOPE_FALLBACK = os.getenv("VOICE_ASR_ALLOW_DASHSCOPE_FALLBACK", "false").lower() in ("1", "true", "yes", "on")
VOICE_PYTHON_ASR_QUEUE_MAX = int(os.getenv("VOICE_PYTHON_ASR_QUEUE_MAX", "200"))
VOICE_PYTHON_LISTEN_TIMEOUT_SECONDS = float(os.getenv("VOICE_PYTHON_LISTEN_TIMEOUT_SECONDS", "2.5"))
VOICE_PYTHON_PHRASE_TIME_LIMIT_SECONDS = float(os.getenv("VOICE_PYTHON_PHRASE_TIME_LIMIT_SECONDS", "4.0"))
VOICE_PYTHON_AMBIENT_ADJUST_SECONDS = float(os.getenv("VOICE_PYTHON_AMBIENT_ADJUST_SECONDS", "0.25"))
VOICE_PYTHON_ENERGY_THRESHOLD = int(os.getenv("VOICE_PYTHON_ENERGY_THRESHOLD", "280"))
VOICE_PYTHON_DYNAMIC_ENERGY = os.getenv("VOICE_PYTHON_DYNAMIC_ENERGY", "true").lower() in ("1", "true", "yes", "on")
VOICE_PYTHON_NO_TEXT_WARN_RMS = int(os.getenv("VOICE_PYTHON_NO_TEXT_WARN_RMS", "120"))
VOICE_PYTHON_MIC_DEVICE_INDEX = int(os.getenv("VOICE_PYTHON_MIC_DEVICE_INDEX", "-1"))
VOICE_PYTHON_MIC_DEVICE_NAME_HINT = os.getenv("VOICE_PYTHON_MIC_DEVICE_NAME_HINT", "").strip()
VOICE_LOOPBACK_DEVICE_INDEX = int(os.getenv("VOICE_LOOPBACK_DEVICE_INDEX", "-1"))
VOICE_LOOPBACK_DEVICE_NAME_HINT = os.getenv("VOICE_LOOPBACK_DEVICE_NAME_HINT", "").strip()
VOICE_DASHSCOPE_API_KEY = os.getenv("VOICE_DASHSCOPE_API_KEY", os.getenv("DASHSCOPE_API_KEY", "")).strip()
VOICE_DASHSCOPE_MODEL = os.getenv("VOICE_DASHSCOPE_MODEL", "paraformer-realtime-v2").strip()
VOICE_DASHSCOPE_SAMPLE_RATE = int(os.getenv("VOICE_DASHSCOPE_SAMPLE_RATE", "16000"))
VOICE_DASHSCOPE_BASE_WEBSOCKET_API_URL = os.getenv("VOICE_DASHSCOPE_BASE_WEBSOCKET_API_URL", "").strip()
VOICE_DASHSCOPE_LANGUAGE_HINTS = _split_csv_env("VOICE_DASHSCOPE_LANGUAGE_HINTS", "")
VOICE_DASHSCOPE_ENABLE_PUNCTUATION = os.getenv("VOICE_DASHSCOPE_ENABLE_PUNCTUATION", "true").lower() in ("1", "true", "yes", "on")
VOICE_DASHSCOPE_DISABLE_ITN = os.getenv("VOICE_DASHSCOPE_DISABLE_ITN", "false").lower() in ("1", "true", "yes", "on")
VOICE_TAB_AUDIO_CHUNK_SECONDS = float(os.getenv("VOICE_TAB_AUDIO_CHUNK_SECONDS", "4.8"))
VOICE_TAB_AUDIO_MAX_CHUNK_SECONDS = float(os.getenv("VOICE_TAB_AUDIO_MAX_CHUNK_SECONDS", "9.0"))
VOICE_TAB_AUDIO_CHUNK_OVERLAP_SECONDS = float(os.getenv("VOICE_TAB_AUDIO_CHUNK_OVERLAP_SECONDS", "0.6"))
VOICE_TAB_AUDIO_EMIT_IDLE_SECONDS = float(os.getenv("VOICE_TAB_AUDIO_EMIT_IDLE_SECONDS", "1.2"))
VOICE_TAB_AUDIO_EMIT_MAX_WAIT_SECONDS = float(os.getenv("VOICE_TAB_AUDIO_EMIT_MAX_WAIT_SECONDS", "9.0"))
VOICE_TAB_AUDIO_EMIT_MAX_CHARS = int(os.getenv("VOICE_TAB_AUDIO_EMIT_MAX_CHARS", "96"))
VOICE_TAB_AUDIO_SILENCE_RMS = int(os.getenv("VOICE_TAB_AUDIO_SILENCE_RMS", "110"))
VOICE_TAB_AUDIO_FILTER_LOW_QUALITY = os.getenv("VOICE_TAB_AUDIO_FILTER_LOW_QUALITY", "false").lower() in ("1", "true", "yes", "on")
VOICE_TAB_AUDIO_WHISPER_NO_SPEECH_THRESHOLD = float(os.getenv("VOICE_TAB_AUDIO_WHISPER_NO_SPEECH_THRESHOLD", "0.35"))
VOICE_WHISPER_MODEL = os.getenv("VOICE_WHISPER_MODEL", "tiny").strip()
VOICE_WHISPER_DOWNLOAD_ROOT = os.getenv("VOICE_WHISPER_DOWNLOAD_ROOT", "data/whisper_cache").strip()
VOICE_WHISPER_MAX_LANGS = int(os.getenv("VOICE_WHISPER_MAX_LANGS", "2"))
VOICE_WHISPER_NO_SPEECH_THRESHOLD = float(os.getenv("VOICE_WHISPER_NO_SPEECH_THRESHOLD", "0.50"))
VOICE_COMMAND_WAKE_WORDS = _split_csv_lower_env(
    "VOICE_COMMAND_WAKE_WORDS",
    "助播,主播助手,assistant,cohost,co-host,liveassistant,live assistant,streamassistant,stream assistant"
)
# 是否强制要求唤醒词；关闭时“明确命令”可直接触发（更稳）
VOICE_STRICT_WAKE_WORD = os.getenv("VOICE_STRICT_WAKE_WORD", "false").lower() in ("1", "true", "yes", "on")

# 本地优先模式：仅在用户未显式配置对应项时应用默认覆盖。
if LOCAL_FIRST_MODE:
    if "VOICE_COMMAND_INPUT_MODE" not in os.environ:
        VOICE_COMMAND_INPUT_MODE = "python_asr"
    if "VOICE_PYTHON_ASR_PROVIDER" not in os.environ:
        VOICE_PYTHON_ASR_PROVIDER = "whisper_local"
    if "VOICE_ASR_ALLOW_GOOGLE_FALLBACK" not in os.environ:
        VOICE_ASR_ALLOW_GOOGLE_FALLBACK = False
    if "VOICE_ASR_ALLOW_DASHSCOPE_FALLBACK" not in os.environ:
        VOICE_ASR_ALLOW_DASHSCOPE_FALLBACK = False
    if "EMBEDDING_LOCAL_FILES_ONLY" not in os.environ:
        EMBEDDING_LOCAL_FILES_ONLY = True
    if "EMBEDDING_ENABLE_ONLINE_FALLBACK" not in os.environ:
        EMBEDDING_ENABLE_ONLINE_FALLBACK = False

# LLM 触发策略：仅对“问题类/咨询类”弹幕调用 LLM，避免成本和刷屏
REPLY_TRIGGER_KEYWORDS = [
    "多少", "多大", "怎么买", "怎么拍", "怎么下单", "怎么用", "能不能", "有没有",
    "尺寸", "尺码", "发货", "包邮", "退换", "质量", "材质", "价格", "优惠", "活动",
    "what", "how", "when", "where", "which", "can you", "could you", "would you",
    "do you have", "price", "size", "ship", "shipping", "delivery", "return", "refund",
    "material", "quality", "discount", "coupon", "deal", "promo", "promotion",
]
REPLY_IGNORE_KEYWORDS = [
    "joined", "sent", "shared", "followed", "liked", "🔥", "666", "up",
]

# 防重复回复策略
SAME_MESSAGE_COOLDOWN_SECONDS = 45
SAME_USER_REPLY_COOLDOWN_SECONDS = 120
GLOBAL_REPLY_COOLDOWN_SECONDS = 20

# 自身回声过滤（避免识别到自己刚发出的消息再次触发回复）
SELF_ECHO_IGNORE_ENABLED = os.getenv("SELF_ECHO_IGNORE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SELF_USERNAMES = _split_csv_lower_env("SELF_USERNAMES")
SELF_ECHO_TTL_SECONDS = int(os.getenv("SELF_ECHO_TTL_SECONDS", "180"))
SELF_ECHO_MIN_CHARS = int(os.getenv("SELF_ECHO_MIN_CHARS", "10"))

# 直播间暖场（主动发言）配置
PROACTIVE_ENABLED = True
PROACTIVE_MIN_INTERVAL = 90
PROACTIVE_MAX_INTERVAL = 180
PROACTIVE_SILENCE_SECONDS = 20
PROACTIVE_MESSAGES_BY_LANGUAGE = {
    "zh-CN": [
        "宝宝们有问题直接打在公屏，我都能看到～",
        "想看上身效果/细节的扣1，我马上安排～",
        "尺码、发货、优惠都可以问我，实时回复～",
        "还在犹豫的宝可以说下需求，我帮你推荐～",
    ],
    "en-US": [
        "Drop your questions in chat and I will answer in real time.",
        "Need sizing, shipping, or deals? Ask me now.",
        "If you want close-up details, type 1 and I will arrange it.",
        "Tell me your needs and I can suggest the right option.",
    ],
    "es-ES": [
        "Deja tus preguntas en el chat y respondo al momento.",
        "Si quieres talla, envío o promociones, pregúntame ahora.",
        "Si quieres ver más detalles, escribe 1 y lo mostramos.",
        "Cuéntame lo que buscas y te recomiendo una opción.",
    ],
}

# 数据分析与报表
ANALYTICS_AUTO_REPORT_ENABLED = os.getenv("ANALYTICS_AUTO_REPORT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
ANALYTICS_REPORT_CHECK_INTERVAL_SECONDS = float(os.getenv("ANALYTICS_REPORT_CHECK_INTERVAL_SECONDS", "300"))

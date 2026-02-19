from utils.logger import logger
from config.settings import KEYWORD_REPLIES, DEFAULT_REPLY_LANGUAGE

class AtmosphereAgent:
    def __init__(self):
        self.replies = KEYWORD_REPLIES

    def _pick_reply_by_language(self, reply_config, language):
        """支持字符串或多语言字典两种配置格式。"""
        if isinstance(reply_config, str):
            return reply_config
        if isinstance(reply_config, dict):
            return (
                reply_config.get(language)
                or reply_config.get(DEFAULT_REPLY_LANGUAGE)
                or next(iter(reply_config.values()), None)
            )
        return None

    def analyze_and_reply(self, message_text, language=DEFAULT_REPLY_LANGUAGE):
        """
        分析弹幕内容并返回回复
        :param message_text: 弹幕文本
        :param language: 回复语言代码
        :return: 回复文本 或 None
        """
        if not message_text:
            return None
            
        lowered = message_text.lower()
        for keyword, reply_cfg in self.replies.items():
            if keyword.lower() in lowered:
                reply = self._pick_reply_by_language(reply_cfg, language)
                if not reply:
                    continue
                logger.info(f"匹配到关键词: {keyword} -> 回复: {reply}")
                return reply
        
        return None

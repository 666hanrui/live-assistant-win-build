import cv2
import numpy as np
from utils.logger import logger
import os

def load_image(path):
    """
    读取图片，支持中文路径
    """
    # cv2.imread 不支持中文路径，使用 imdecode
    try:
        return cv2.imdecode(np.fromfile(path, dtype=np.uint8), -1)
    except Exception as e:
        logger.error(f"读取图片失败: {path}, 错误: {e}")
        return None

def match_template(screen_image, template_path, threshold=0.8):
    """
    在屏幕截图中匹配模板图片
    :param screen_image: 屏幕截图 (numpy array, BGR or BGRA)
    :param template_path: 模板图片路径
    :param threshold: 匹配阈值 (0-1)
    :return: (center_x, center_y) or None
    """
    if screen_image is None:
        logger.error("屏幕截图为空")
        return None
    
    if not os.path.exists(template_path):
        logger.error(f"模板文件不存在: {template_path}")
        return None

    template = load_image(template_path)
    if template is None:
        return None

    # 转换颜色空间以匹配 (处理可能的 Alpha 通道)
    # 假设输入都是 BGR，如果 template 有 alpha 需要处理
    if len(template.shape) == 3 and template.shape[2] == 4:
        # 如果模板有透明通道，可以使用 mask 匹配 (cv2.TM_CCORR_NORMED) 或者简单的转 BGR
        # 这里简单起见，转 BGR
        template = cv2.cvtColor(template, cv2.COLOR_BGRA2BGR)
    
    if len(screen_image.shape) == 3 and screen_image.shape[2] == 4:
        screen_image = cv2.cvtColor(screen_image, cv2.COLOR_BGRA2BGR)

    # 确保是灰度图可能更稳，但彩色匹配信息更多。这里用 BGR 匹配
    # 方法使用 TM_CCOEFF_NORMED
    try:
        result = cv2.matchTemplate(screen_image, template, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
        
        if max_val >= threshold:
            # 计算中心坐标
            h, w = template.shape[:2]
            top_left = max_loc
            center_x = top_left[0] + w // 2
            center_y = top_left[1] + h // 2
            logger.info(f"匹配成功: {template_path}, 相似度: {max_val:.2f}, 坐标: ({center_x}, {center_y})")
            return (center_x, center_y)
        else:
            logger.debug(f"匹配失败: {template_path}, 最大相似度: {max_val:.2f} < {threshold}")
            return None
            
    except Exception as e:
        logger.error(f"模板匹配执行出错: {e}")
        return None

def find_button_on_screen(vision_agent, template_name):
    """
    辅助函数：通过 VisionAgent 获取截图并查找按钮
    """
    # 截图保存到临时文件或直接获取 bytes
    # DrissionPage 的 page.get_screenshot(as_bytes=True)
    if not vision_agent.page:
        return None
        
    try:
        # 获取截图二进制数据
        screenshot_bytes = vision_agent.page.get_screenshot(as_bytes='png')
        if not screenshot_bytes:
            return None
            
        # 转换为 numpy array
        nparr = np.frombuffer(screenshot_bytes, np.uint8)
        screen_img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        template_path = os.path.join("assets", "templates", template_name)
        return match_template(screen_img, template_path)
        
    except Exception as e:
        logger.error(f"查找按钮流程失败: {e}")
        return None

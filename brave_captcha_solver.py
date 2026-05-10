import asyncio
import logging
import os
import cv2
import numpy as np
import math
import random
from typing import Optional, Tuple
from datetime import datetime

USE_PRINT = True

def log_info(msg: str):
    if USE_PRINT:
        print(f"[INFO] {msg}")
    else:
        logging.getLogger(__name__).info(msg)

def log_debug(msg: str):
    if USE_PRINT:
        print(f"[DEBUG] {msg}")
    else:
        logging.getLogger(__name__).debug(msg)

def log_warning(msg: str):
    if USE_PRINT:
        print(f"[WARNING] {msg}")
    else:
        logging.getLogger(__name__).warning(msg)

def log_error(msg: str):
    if USE_PRINT:
        print(f"[ERROR] {msg}")
    else:
        logging.getLogger(__name__).error(msg)

logger = logging.getLogger(__name__)

CAPTCHA_DEBUG_DIR = "captcha_debug"
os.makedirs(CAPTCHA_DEBUG_DIR, exist_ok=True)


def calculate_gap_distance(captcha_image_path):
    """计算滑块验证码缺口距离（天才版算法）
    
    基于用户的两个天才观察：
    1. 直接找模糊的缺口圆（不需要滑块模板）
    2. 缺口圆心一定在水平中轴线上！
    
    Args:
        captcha_image_path: 验证码图片路径
    
    Returns:
        (gap_percent, pixel_offset): 缺口百分比, 像素偏移量
    """
    img = cv2.imread(captcha_image_path)
    if img is None:
        raise ValueError(f"无法读取图片: {captcha_image_path}")
    
    h, w = img.shape[:2]
    log_debug(f"[距离计算] 图片尺寸: {w}x{h}")
    
    # ==========================================
    # 第一步：计算水平中轴线！
    # ==========================================
    mid_y = h // 2
    log_debug(f"[距离计算] 水平中轴线: Y = {mid_y}")
    
    # ==========================================
    # 第二步：找滑块（用简单的方法）
    # ==========================================
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    lower_orange1 = np.array([0, 100, 100])
    upper_orange1 = np.array([10, 255, 255])
    lower_orange2 = np.array([170, 100, 100])
    upper_orange2 = np.array([180, 255, 255])
    
    mask1 = cv2.inRange(hsv, lower_orange1, upper_orange1)
    mask2 = cv2.inRange(hsv, lower_orange2, upper_orange2)
    mask = cv2.bitwise_or(mask1, mask2)
    
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise ValueError("无法找到滑块")
    
    # 找最左边的轮廓
    slider_cnt = min(contours, key=lambda c: cv2.boundingRect(c)[0])
    x_slider, y_slider, w_slider, h_slider = cv2.boundingRect(slider_cnt)
    center_slider_x = x_slider + w_slider // 2
    center_slider_y = mid_y  # 强制对齐到中轴线
    
    log_debug(f"[距离计算] 滑块位置: ({x_slider},{y_slider}) 尺寸: {w_slider}x{h_slider}")
    log_debug(f"[距离计算] 滑块中心: ({center_slider_x},{center_slider_y})")
    
    slider_radius = (w_slider + h_slider) // 4
    log_debug(f"[距离计算] 滑块等效半径: {slider_radius}")
    
    # ==========================================
    # 第三步：搜索区域 = 滑块右边缘+10px 到图片最右边
    # ==========================================
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    slider_right_edge = x_slider + w_slider
    right_region_start = slider_right_edge + 10
    
    search_margin = slider_radius
    search_y_start = max(0, mid_y - search_margin)
    search_y_end = min(h, mid_y + search_margin)
    
    gray_search = gray[search_y_start:search_y_end, right_region_start:]
    h_search, w_search = gray_search.shape
    
    log_debug(f"[距离计算] 搜索区域: X=[{right_region_start}, {w}], Y=[{search_y_start}, {search_y_end}]")
    log_debug(f"[距离计算] 搜索区域尺寸: {w - right_region_start}x{search_y_end - search_y_start}px")
    
    # ==========================================
    # 第四步：模糊处理！让模糊的圆更明显
    # ==========================================
    blur_scale = slider_radius / 50.0
    k1 = max(3, int(15 * blur_scale) | 1)
    k2 = max(3, int(11 * blur_scale) | 1)
    k3 = max(3, int(9 * blur_scale) | 1)
    
    blurred = gray_search.copy()
    blurred = cv2.GaussianBlur(blurred, (k1, k1), 0)
    blurred = cv2.medianBlur(blurred, k2)
    blurred = cv2.GaussianBlur(blurred, (k3, k3), 0)
    
    log_debug(f"[距离计算] 模糊核大小: ({k1},{k1}) + ({k2},{k2}) + ({k3},{k3}) (基于滑块半径 {slider_radius})")
    
    # ==========================================
    # 第五步：在中轴线上找圆形！
    # ==========================================
    best_circle = None
    best_score = 0
    
    param2_values = [25, 20, 15, 12, 10, 8]
    min_radius = max(10, int(slider_radius * 0.6))
    max_radius = min(int(w * 0.3), int(slider_radius * 1.4))
    dp_values = [1.1, 1.2, 1.3, 1.5]
    
    min_dist = int(slider_radius * 0.6)
    
    log_debug(f"[距离计算] 缺口半径搜索范围: [{min_radius}, {max_radius}]")
    
    all_candidates = []
    
    for dp in dp_values:
        for param2 in param2_values:
            circles = cv2.HoughCircles(
                blurred,
                cv2.HOUGH_GRADIENT,
                dp=dp,
                minDist=min_dist,
                param1=40,
                param2=param2,
                minRadius=min_radius,
                maxRadius=max_radius
            )
            
            if circles is not None:
                circles = np.uint16(np.around(circles))
                
                for circle in circles[0]:
                    cx_rel, cy_rel, r = int(circle[0]), int(circle[1]), int(circle[2])
                    abs_x = cx_rel + right_region_start
                    
                    mid_search = h_search // 2
                    dist_from_mid = abs(cy_rel - mid_search)
                    mid_score = max(0.0, 1.0 - (dist_from_mid / slider_radius))
                    
                    radius_diff = abs(r - slider_radius)
                    radius_score = max(0.0, 1.0 - (radius_diff / (slider_radius * 0.4)))
                    
                    mask = np.zeros(gray_search.shape, dtype=np.uint8)
                    cv2.circle(mask, (cx_rel, cy_rel), r, 255, -1)
                    circle_gray = gray_search[mask == 255]
                    
                    outside_mask = np.zeros(gray_search.shape, dtype=np.uint8)
                    cv2.circle(outside_mask, (cx_rel, cy_rel), r + int(slider_radius * 0.2), 255, -1)
                    cv2.circle(outside_mask, (cx_rel, cy_rel), r, 0, -1)
                    outside_gray = gray_search[outside_mask == 255]
                    
                    if len(circle_gray) > 0 and len(outside_gray) > 0:
                        contrast = abs(np.mean(circle_gray) - np.mean(outside_gray))
                        contrast_score = min(1.0, contrast / (slider_radius * 0.6))
                    else:
                        contrast_score = 0.0
                    
                    combined_score = mid_score * 0.4 + radius_score * 0.3 + contrast_score * 0.3
                    
                    key = (abs_x, r)
                    if key not in [(c[0], c[1]) for c in all_candidates]:
                        all_candidates.append((abs_x, r, mid_score, radius_score, contrast_score, combined_score))
    
    if not all_candidates:
        raise ValueError("在中轴线上找不到缺口圆！")
    
    all_candidates.sort(key=lambda x: -x[5])
    best = all_candidates[0]
    center_gap_x = best[0]
    
    log_debug(f"[距离计算] 检测到 {len(all_candidates)} 个候选圆")
    log_debug(f"[距离计算] 最佳候选: X={center_gap_x}, 半径={best[1]}")
    log_debug(f"[距离计算] 评分: 中轴={best[2]:.2f}, 半径={best[3]:.2f}, 对比度={best[4]:.2f}, 综合={best[5]:.2f}")
    
    # ==========================================
    # 第六步：计算圆心位置（绝对坐标）
    # ==========================================
    center_gap_y = mid_y
    gap_radius = slider_radius
    
    log_debug(f"[距离计算] 缺口圆心: ({center_gap_x},{center_gap_y})")
    log_debug(f"[距离计算] 缺口半径: {gap_radius} (基于滑块大小)")
    
    # ==========================================
    # 第七步：计算最终距离！
    # ==========================================
    pixel_offset = center_gap_x - center_slider_x
    gap_percent = (center_gap_x / w) * 100
    
    log_debug(f"[距离计算] 滑块中心到缺口圆心的距离: {pixel_offset}px")
    log_debug(f"[距离计算] 缺口百分比: {gap_percent:.2f}%")
    
    return gap_percent, pixel_offset


def generate_human_like_points(start_x, start_y, end_x, end_y, count=None):
    """生成类人鼠标移动轨迹点（根据距离动态调整参数）"""
    points = []
    base_timestamp = random.randint(1000, 5000)
    
    # 确保输入都是普通 Python int，不是 numpy 类型！
    start_x = int(start_x)
    start_y = int(start_y)
    end_x = int(end_x)
    end_y = int(end_y)
    
    distance = abs(end_x - start_x)
    
    # 根据距离动态调整轨迹点数量
    if count is None:
        if distance < 100:
            count = 60
        elif distance < 200:
            count = 90
        else:
            count = 120
    
    log_debug(f"[轨迹生成] 距离: {distance}px, 轨迹点数量: {count}")
    
    # 根据距离调整贝塞尔曲线控制点范围
    cp1_x = start_x + (end_x - start_x) * random.uniform(0.2, 0.35)
    cp1_y = start_y + random.uniform(-3, 3)
    cp2_x = start_x + (end_x - start_x) * random.uniform(0.65, 0.8)
    cp2_y = end_y + random.uniform(-2, 2)
    
    # 根据距离调整速度参数
    max_speed = min(8.0, 4.0 + distance / 100.0)
    log_debug(f"[轨迹生成] 最大速度因子: {max_speed:.2f}")
    
    # 生成平滑的竖直方向抖动曲线
    # 使用正弦波生成更自然的上下波动
    vertical_jitter_amplitude = random.uniform(1.5, 3.5)
    vertical_jitter_frequency = random.uniform(2, 5)
    log_debug(f"[轨迹生成] 竖直抖动幅度: {vertical_jitter_amplitude:.1f}, 频率: {vertical_jitter_frequency:.1f}")
    
    for i in range(count):
        t = i / count
        
        x = (1-t)**3 * start_x + 3*(1-t)**2*t * cp1_x + 3*(1-t)*t**2 * cp2_x + t**3 * end_x
        y = (1-t)**3 * start_y + 3*(1-t)**2*t * cp1_y + 3*(1-t)*t**2 * cp2_y + t**3 * end_y
        
        # 增加明显的竖直方向抖动
        # 1. 正弦波抖动（平滑）
        y += math.sin(t * math.pi * vertical_jitter_frequency) * vertical_jitter_amplitude
        
        # 2. 随机小抖动（更自然）
        y += random.gauss(0, 0.4)
        
        # 3. 在开头和结尾增加一些额外的小抖动
        if t < 0.15 or t > 0.85:
            y += random.gauss(0, 0.6)
        
        if t < 0.10:
            speed_factor = 0.05
        elif t < 0.90:
            progress = (t - 0.10) / 0.80
            speed_factor = 0.05 + (max_speed - 0.05) * (math.sin(progress * math.pi * 0.5))
        else:
            progress = (t - 0.90) / 0.10
            speed_factor = max_speed - (max_speed - 0.05) * (math.sin(progress * math.pi * 0.5))
        
        timestamp = base_timestamp + int(i * 10 / speed_factor)
        
        points.append({
            'x': int(x), 'y': int(y), 't': t,
            'timestamp': timestamp, 'speed': speed_factor
        })
    
    return points


def crop_captcha_region(full_image_path: str, output_path: str, crop_x: int = 0, crop_y: int = 0, crop_width: int = 620, crop_height: int = 310) -> Optional[str]:
    """裁剪截图到只包含验证码区域
    
    Args:
        full_image_path: 原始截图路径
        output_path: 裁剪后输出路径
        crop_x, crop_y: 裁剪起始坐标
        crop_width, crop_height: 裁剪尺寸
    
    Returns:
        裁剪后图片路径或 None
    """
    try:
        img = cv2.imread(full_image_path)
        if img is None:
            log_error(f"[裁剪] 无法读取图片: {full_image_path}")
            return None
        
        h, w = img.shape[:2]
        
        crop_x = max(0, min(crop_x, w - 1))
        crop_y = max(0, min(crop_y, h - 1))
        crop_width = min(crop_width, w - crop_x)
        crop_height = min(crop_height, h - crop_y)
        
        cropped_img = img[crop_y:crop_y+crop_height, crop_x:crop_x+crop_width]
        cv2.imwrite(output_path, cropped_img)
        log_debug(f"[裁剪] 已裁剪: {output_path} (尺寸: {crop_width}x{crop_height}, 起始: ({crop_x},{crop_y})")
        return output_path
    except Exception as e:
        log_error(f"[裁剪] 裁剪失败: {e}")
        return None


async def take_debug_screenshot(tab, description: str, attempt: int = 0, crop: bool = False, 
                               crop_x: int = 0, crop_y: int = 0, crop_width: int = 620, crop_height: int = 310) -> Optional[str]:
    """保存调试截图（使用固定名称方便查看）
    
    Args:
        tab: pydoll Tab 对象
        description: 截图描述
        attempt: 尝试次数
        crop: 是否裁剪
        crop_x, crop_y: 裁剪起始坐标
        crop_width, crop_height: 裁剪尺寸
    
    Returns:
        截图路径或 None
    """
    try:
        full_screenshot_path = os.path.join(CAPTCHA_DEBUG_DIR, "latest.png")
        await tab.take_screenshot(path=full_screenshot_path)
        log_debug(f"[调试截图] 已保存全屏: {full_screenshot_path} ({description})")
        
        if crop:
            cropped_screenshot_path = os.path.join(CAPTCHA_DEBUG_DIR, "latest_cropped.png")
            cropped_path = crop_captcha_region(full_screenshot_path, cropped_screenshot_path, crop_x, crop_y, crop_width, crop_height)
            if cropped_path:
                log_debug(f"[调试截图] 裁剪后的图片单独保存为: {cropped_path}")
                return cropped_path
            else:
                return full_screenshot_path
        
        return full_screenshot_path
    except Exception as e:
        log_error(f"[调试截图] 保存失败: {e}")
        return None


async def drag_slider_with_cdp(tab, slider_element, start_x, start_y, end_x, end_y, attempt: int = 0):
    """使用 CDP 原生 Input.dispatchMouseEvent 拖动滑块
    
    Args:
        tab: pydoll Tab 对象
        slider_element: 滑块元素
        start_x, start_y: 起始坐标
        end_x, end_y: 目标坐标
        attempt: 尝试次数
    """
    from pydoll.commands import InputCommands
    from pydoll.protocol.input.types import MouseButton, MouseEventType, PointerType
    import time
    
    # 确保所有坐标都是普通 Python int，不是 numpy 类型！
    start_x = int(start_x)
    start_y = int(start_y)
    end_x = int(end_x)
    end_y = int(end_y)
    
    points = generate_human_like_points(start_x, start_y, end_x, end_y)
    log_info(f"[CDP拖动] 生成 {len(points)} 个轨迹点")
    
    connection_handler = slider_element._connection_handler
    base_timestamp = int(time.time() * 1000)  # 毫秒时间戳
    
    try:
        log_info("[CDP拖动] [1/6] 按下鼠标...")
        press_command = InputCommands.dispatch_mouse_event(
            type=MouseEventType.MOUSE_PRESSED,
            x=int(start_x),
            y=int(start_y),
            button=MouseButton.LEFT,
            click_count=1,
            pointer_type=PointerType.MOUSE,
            timestamp=int(base_timestamp),
        )
        await connection_handler.execute_command(press_command)
        await asyncio.sleep(random.uniform(0.05, 0.15))
        
        log_info("[CDP拖动] [2/6] 移动鼠标...")
        for i, point in enumerate(points):
            move_timestamp = base_timestamp + i * 5
            move_command = InputCommands.dispatch_mouse_event(
                type=MouseEventType.MOUSE_MOVED,
                x=int(point['x']),
                y=int(point['y']),
                pointer_type=PointerType.MOUSE,
                timestamp=int(move_timestamp),
            )
            await connection_handler.execute_command(move_command)
            
            if point['speed'] < 0.5:
                delay = random.uniform(0.02, 0.04)
            elif point['speed'] < 2.0:
                delay = random.uniform(0.005, 0.01)
            elif point['speed'] < 5.0:
                delay = random.uniform(0.0015, 0.003)
            else:
                delay = random.uniform(0.0005, 0.0012)
            
            await asyncio.sleep(delay)
            
            if i % 10 == 0:
                log_debug(f"  已移动 {i}/{len(points)} 个点... (速度: {point['speed']:.2f})")
        
        log_info("[CDP拖动] [3/6] 移动到准确目标位置...")
        final_move_timestamp = base_timestamp + len(points) * 5 + 10
        final_move_command = InputCommands.dispatch_mouse_event(
            type=MouseEventType.MOUSE_MOVED,
            x=int(end_x),
            y=int(end_y),
            pointer_type=PointerType.MOUSE,
            timestamp=int(final_move_timestamp),
        )
        await connection_handler.execute_command(final_move_command)
        await asyncio.sleep(random.uniform(0.15, 0.3))
        
        log_info("[CDP拖动] [4/6] 轻微抖动后释放...")
        # 轻微抖动，更自然的人类行为
        jitter_timestamp = final_move_timestamp + 20
        jitter_x = end_x + random.randint(-2, 2)
        jitter_y = end_y + random.randint(-2, 2)
        jitter_move_command = InputCommands.dispatch_mouse_event(
            type=MouseEventType.MOUSE_MOVED,
            x=int(jitter_x),
            y=int(jitter_y),
            pointer_type=PointerType.MOUSE,
            timestamp=int(jitter_timestamp),
        )
        await connection_handler.execute_command(jitter_move_command)
        await asyncio.sleep(random.uniform(0.05, 0.15))
        
        # 回到准确位置
        final_adjust_timestamp = jitter_timestamp + 10
        final_adjust_command = InputCommands.dispatch_mouse_event(
            type=MouseEventType.MOUSE_MOVED,
            x=int(end_x),
            y=int(end_y),
            pointer_type=PointerType.MOUSE,
            timestamp=int(final_adjust_timestamp),
        )
        await connection_handler.execute_command(final_adjust_command)
        await asyncio.sleep(random.uniform(0.1, 0.2))
        
        log_info("[CDP拖动] [5/6] 释放鼠标...")
        release_timestamp = final_adjust_timestamp + 20
        release_command = InputCommands.dispatch_mouse_event(
            type=MouseEventType.MOUSE_RELEASED,
            x=int(end_x),
            y=int(end_y),
            button=MouseButton.LEFT,
            click_count=1,
            pointer_type=PointerType.MOUSE,
            timestamp=int(release_timestamp),
        )
        await connection_handler.execute_command(release_command)
        await asyncio.sleep(random.uniform(0.2, 0.4))
        
        log_info("[CDP拖动] [6/6] 使用pydoll点击滑块刷新状态...")
        try:
            # 先检查滑块是否还存在
            slider_still_exists = False
            try:
                # 尝试快速查询一下，看滑块是否还在
                test_selector = '.slider-button, [class*="slider"], [class*="captcha"] .handle, [class*="captcha"] [role="slider"], .slider-handle, div[role="slider"]'
                test_el = await tab.query(test_selector, timeout=0.5)
                slider_still_exists = test_el is not None
            except:
                slider_still_exists = False
            
            if slider_still_exists:
                await slider_element.click()
                await asyncio.sleep(random.uniform(0.3, 0.6))
                log_info("   ✅ 滑块点击完成")
            else:
                log_info("   ℹ️  滑块可能已消失，跳过点击")
        except Exception as e:
            log_debug(f"   点击滑块失败（可能不影响）: {e}")
        
        log_info(f"[CDP拖动] 拖动完成！从 ({start_x}, {start_y}) 到 ({end_x}, {end_y})")
        
    except Exception as e:
        log_error(f"[CDP拖动] 拖动失败: {e}")
        import traceback
        traceback.print_exc()


def check_slider_at_left_edge(image_path: str) -> bool:
    """检查滑块是否在最左边与左边相切（通过图片分析）
    
    Args:
        image_path: 裁剪后的验证码图片路径
        
    Returns:
        bool: 滑块是否在最左边
    """
    try:
        import cv2
        import numpy as np
        
        img = cv2.imread(image_path)
        if img is None:
            log_error(f"[滑块位置检查] 无法读取图片: {image_path}")
            return False
        
        h, w = img.shape[:2]
        
        # 转换为 HSV 颜色空间
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        
        # 橙色范围（HSV）
        lower_orange1 = np.array([0, 100, 100])
        upper_orange1 = np.array([10, 255, 255])
        lower_orange2 = np.array([170, 100, 100])
        upper_orange2 = np.array([180, 255, 255])
        
        mask1 = cv2.inRange(hsv, lower_orange1, upper_orange1)
        mask2 = cv2.inRange(hsv, lower_orange2, upper_orange2)
        mask = cv2.bitwise_or(mask1, mask2)
        
        # 查找橙色轮廓
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        if not contours:
            log_debug(f"[滑块位置检查] 未找到橙色滑块")
            return False
        
        # 找最左边的橙色轮廓
        slider_cnt = min(contours, key=lambda c: cv2.boundingRect(c)[0])
        x_slider, y_slider, w_slider, h_slider = cv2.boundingRect(slider_cnt)
        
        log_debug(f"[滑块位置检查] 滑块左边缘: X={x_slider}, 图片宽度: {w}")
        
        # 判断：滑块左边缘是否接近图片左边缘（阈值：10像素）
        if x_slider < 10:
            log_info(f"[滑块位置检查] ✅ 滑块在最左边 (X={x_slider})")
            return True
        else:
            log_info(f"[滑块位置检查] ⚠️  滑块不在最左边 (X={x_slider})")
            return False
            
    except Exception as e:
        log_error(f"[滑块位置检查] 检查失败: {e}")
        return False


async def check_captcha_success(tab) -> bool:
    """检查验证码是否成功（页面是否变化）
    
    Args:
        tab: pydoll Tab 对象
    
    Returns:
        bool: 是否成功
    """
    try:
        # 检查是否还有验证码元素
        captcha_selectors = [
            '.slider-button',
            '[class*="slider"]',
            '[class*="captcha"]',
            '#default-captcha-button',
        ]
        
        for selector in captcha_selectors:
            try:
                el = await tab.query(selector, timeout=1)
                if el:
                    log_debug(f"[验证检查] 仍找到验证码元素: {selector}")
                    return False
            except:
                continue
        
        log_info("[验证检查] 未找到验证码元素，可能已成功！")
        return True
        
    except Exception as e:
        log_debug(f"[验证检查] 检查失败: {e}")
        return False


class BraveSliderCaptchaSolver:
    """滑块验证码解决器"""

    def __init__(self, tab):
        self.tab = tab
        self.attempt = 0
        self.crop_x = 800
        self.crop_y = 420
        self.crop_width = 310
        self.crop_height = 153

    def set_crop_params(self, crop_x: int, crop_y: int, crop_width: int, crop_height: int):
        """设置裁剪参数
        
        Args:
            crop_x, crop_y: 裁剪起始坐标
            crop_width, crop_height: 裁剪尺寸
        """
        self.crop_x = crop_x
        self.crop_y = crop_y
        self.crop_width = crop_width
        self.crop_height = crop_height
        log_info(f"[滑块验证码] 裁剪参数已更新: ({crop_x},{crop_y}) {crop_width}x{crop_height}")

    async def solve(self, timeout: float = 60) -> bool:
        """尝试解决滑块验证码
        
        Args:
            timeout: 超时时间
            
        Returns:
            bool: 是否成功
        """
        self.attempt += 1
        log_info(f"[滑块验证码] 开始第 {self.attempt} 次尝试")
        log_info(f"[滑块验证码] 当前裁剪参数: ({self.crop_x},{self.crop_y}) {self.crop_width}x{self.crop_height}")
        
        try:
            await take_debug_screenshot(self.tab, "captcha_appeared", self.attempt)
            
            try:
                b = await self.tab.query('#default-captcha-button', timeout=2)
                if b:
                    await b.click()
                    log_info("[滑块验证码] 切换到传统验证码！")
                    await take_debug_screenshot(self.tab, "switched_to_traditional", self.attempt)
            except:
                pass
            
            await asyncio.sleep(4)
            
            log_info("[滑块验证码] 查找滑块元素...")
            slider_element = await self._find_slider_element()
            if not slider_element:
                log_error("[滑块验证码] 未找到滑块元素！")
                return False
            
            await take_debug_screenshot(self.tab, "slider_found", self.attempt)
            
            log_info("[滑块验证码] 截图验证码区域...")
            captcha_image_path = await take_debug_screenshot(
                self.tab, "captcha_area", self.attempt, 
                crop=True, crop_x=self.crop_x, crop_y=self.crop_y, 
                crop_width=self.crop_width, crop_height=self.crop_height
            )
            if not captcha_image_path:
                log_error("[滑块验证码] 验证码区域截图失败！")
                return False
            
            log_info("[滑块验证码] 计算缺口距离...")
            try:
                gap_percent, offset = calculate_gap_distance(captcha_image_path)
                log_info(f"[滑块验证码] 缺口百分比: {gap_percent:.1f}%")
                log_info(f"[滑块验证码] 像素距离: {offset}px (直接计算)")
            except Exception as e:
                log_error(f"[滑块验证码] 计算缺口距离失败: {e}")
                offset = 140
                log_info(f"[滑块验证码] 使用默认距离: {offset}px")
            
            await take_debug_screenshot(self.tab, "distance_calculated", self.attempt)
            
            log_info("[滑块验证码] 获取滑块位置...")
            start_x, start_y, found_selector = await self._get_slider_position(slider_element)
            if start_x is None:
                log_error("[滑块验证码] 获取滑块位置失败！")
                return False
            
            end_x = start_x + offset
            end_y = start_y
            
            log_info(f"[滑块验证码] 滑块位置: ({start_x}, {start_y})")
            log_info(f"[滑块验证码] 目标位置: ({end_x}, {start_y})")
            
            log_info("[滑块验证码] 开始拖动滑块...")
            await drag_slider_with_cdp(self.tab, slider_element, start_x, start_y, end_x, end_y, self.attempt)
            
            await take_debug_screenshot(self.tab, "drag_completed", self.attempt)
            
            log_info("[滑块验证码] 等待验证结果...")
            await asyncio.sleep(5)
            
            await take_debug_screenshot(self.tab, "final_check", self.attempt)
            
            log_info("[滑块验证码] 尝试完成，请观察验证结果")
            return True
            
        except Exception as exc:
            log_error(f"[滑块验证码] 解决验证码时出错: {exc}")
            import traceback
            traceback.print_exc()
            return False

    async def _find_slider_element(self):
        """查找滑块元素"""
        slider_selectors = [
            '.slider-button',
            '[class*="slider"]',
            '[class*="captcha"] .handle',
            '[class*="captcha"] [role="slider"]',
            '.slider-handle',
            'div[role="slider"]',
        ]
        
        for selector in slider_selectors:
            try:
                log_debug(f"[滑块查找] 尝试选择器: {selector}")
                el = await self.tab.query(selector, timeout=2)
                if el:
                    log_info(f"[滑块查找] 找到滑块: {selector}")
                    return el
            except Exception as e:
                log_debug(f"[滑块查找] 选择器 {selector} 失败: {e}")
                continue
        
        return None

    async def _get_slider_position(self, slider_element):
        """获取滑块位置"""
        def get_json_value(result_obj):
            import json
            try:
                if isinstance(result_obj, dict):
                    if 'result' in result_obj:
                        inner_result = result_obj['result']
                        if isinstance(inner_result, dict) and 'result' in inner_result:
                            value_obj = inner_result['result']
                            if isinstance(value_obj, dict) and 'value' in value_obj:
                                return json.loads(value_obj['value'])
                return None
            except Exception as e:
                log_error(f"解析返回值时出错: {e}")
                return None
        
        position_script = """
            const slider = document.querySelector('.slider-button, [class*="slider"], [class*="captcha"] .handle, [class*="captcha"] [role="slider"], .slider-handle, div[role="slider"]');
            if (!slider) {
                return JSON.stringify({ success: false, error: 'slider not found' });
            }
            const box = slider.getBoundingClientRect();
            return JSON.stringify({
                success: true,
                startX: box.left + box.width / 2,
                startY: box.top + box.height / 2,
                width: box.width,
                height: box.height
            });
        """
        
        pos_result = await self.tab.execute_script(position_script)
        pos_value = get_json_value(pos_result)
        
        if not pos_value or not pos_value.get('success'):
            log_error(f"获取滑块位置失败: {pos_value.get('error') if pos_value else 'unknown error'}")
            return None, None, None
        
        start_x = int(pos_value['startX'])
        start_y = int(pos_value['startY'])
        
        return start_x, start_y, None


class BraveCaptchaSolver:
    """专门用于解决 search.brave.com 验证码的类"""

    def __init__(self, tab):
        """
        初始化验证码解决器

        Args:
            tab: pydoll 的 Tab 对象
        """
        self.tab = tab

    async def solve(self, timeout: float = 10) -> bool:
        """
        尝试解决 Brave Search 验证码

        Args:
            timeout: 超时时间（秒）

        Returns:
            bool: 是否成功解决
        """
        try:
            log_info("开始尝试解决 Brave Search 验证码")

            # 方法 1: 直接查找包含 size--medium 的按钮（已注释，用于测试滑块验证码）
            # success = await self._try_find_and_click_button(timeout)
            # if success:
            #     log_info("验证码按钮点击成功！")
            #     return True

            # 方法 2: 查找 Shadow DOM（已注释，用于测试滑块验证码）
            # success = await self._try_shadow_dom_method(timeout)
            # if success:
            #     log_info("通过 Shadow DOM 解决验证码成功！")
            #     return True

            # 方法 3: 滑块验证码方法（仅保留此方法用于测试）
            success = await self._try_slider_captcha_method(timeout)
            if success:
                log_info("通过滑块验证码方法解决成功！")
                return True

            log_warning("未能找到验证码按钮")
            return False

        except Exception as exc:
            log_error(f"解决验证码时出错: {exc}")
            return False

    async def _try_find_and_click_button(self, timeout: float) -> bool:
        """
        方法 1: 直接在页面中查找包含 size--medium 的按钮

        Args:
            timeout: 超时时间

        Returns:
            bool: 是否成功
        """
        try:
            await asyncio.sleep(2)

            selectors = [
                'button[class*="size--medium"]',
                '.size--medium',
                'button[type="button"]',
                'input[type="checkbox"]',
                'div[role="button"]',
                '[class*="captcha"]',
                '[class*="challenge"]',
                '[class*="verify"]',
            ]

            for selector in selectors:
                try:
                    log_debug(f"尝试选择器: {selector}")
                    element = await self.tab.query(selector, timeout=2)
                    if element:
                        log_info(f"找到元素: {selector}")
                        await element.click()
                        await asyncio.sleep(3)
                        return True
                except Exception:
                    continue

            return False

        except Exception as exc:
            log_debug(f"方法 1 失败: {exc}")
            return False

    async def _try_shadow_dom_method(self, timeout: float) -> bool:
        """
        方法 2: 遍历 Shadow DOM 查找验证码

        Args:
            timeout: 超时时间

        Returns:
            bool: 是否成功
        """
        try:
            shadow_roots = await self.tab.find_shadow_roots(deep=True, timeout=timeout)

            if not shadow_roots:
                log_debug("未找到 Shadow Roots")
                return False

            log_info(f"找到 {len(shadow_roots)} 个 Shadow Roots")

            for sr in shadow_roots:
                try:
                    selectors = [
                        'button[class*="size--medium"]',
                        '.size--medium',
                        'button',
                        'input[type="checkbox"]',
                        'span.cb-i',
                        '[class*="checkbox"]',
                    ]

                    for selector in selectors:
                        try:
                            element = await sr.query(selector, timeout=1)
                            if element:
                                log_info(f"在 Shadow Root 中找到元素: {selector}")
                                await element.click()
                                await asyncio.sleep(3)
                                return True
                        except Exception:
                            continue

                    try:
                        iframe = await sr.query('iframe', timeout=1)
                        if iframe:
                            log_info("找到 iframe，尝试进入")
                            body = await iframe.find(tag_name='body', timeout=2)
                            if body:
                                inner_sr = await body.get_shadow_root(timeout=2)
                                if inner_sr:
                                    for selector in selectors:
                                        try:
                                            element = await inner_sr.query(selector, timeout=1)
                                            if element:
                                                log_info(f"在 iframe Shadow Root 中找到: {selector}")
                                                await element.click()
                                                await asyncio.sleep(3)
                                                return True
                                        except Exception:
                                            continue
                    except Exception:
                        continue

                except Exception as exc:
                    log_debug(f"处理 Shadow Root 时出错: {exc}")
                    continue

            return False

        except Exception as exc:
            log_debug(f"方法 2 失败: {exc}")
            return False

    async def _try_slider_captcha_method(self, timeout: float, 
                                       crop_x: Optional[int] = None, crop_y: Optional[int] = None,
                                       crop_width: Optional[int] = None, crop_height: Optional[int] = None) -> bool:
        """
        方法 3: 滑块验证码方法

        Args:
            timeout: 超时时间
            crop_x, crop_y: 裁剪起始坐标（可选）
            crop_width, crop_height: 裁剪尺寸（可选）

        Returns:
            bool: 是否成功
        """
        try:
            log_info("尝试滑块验证码方法...")
            slider_solver = BraveSliderCaptchaSolver(self.tab)
            
            if crop_x is not None and crop_y is not None and crop_width is not None and crop_height is not None:
                slider_solver.set_crop_params(crop_x, crop_y, crop_width, crop_height)
            
            return await slider_solver.solve(timeout=timeout)
        except Exception as exc:
            log_error(f"滑块验证码方法失败: {exc}")
            return False


async def solve_brave_captcha(tab, timeout: float = 10) -> bool:
    """
    便捷函数：解决 Brave Search 验证码

    Args:
        tab: pydoll Tab 对象
        timeout: 超时时间

    Returns:
        bool: 是否成功
    """
    solver = BraveCaptchaSolver(tab)
    return await solver.solve(timeout)

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class BraveCaptchaSolver:
    """
    专门用于解决 search.brave.com 验证码的类
    """

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
            logger.info("开始尝试解决 Brave Search 验证码")

            # 方法 1: 直接查找包含 size--medium 的按钮
            success = await self._try_find_and_click_button(timeout)
            if success:
                logger.info("验证码按钮点击成功！")
                return True

            # 方法 2: 查找 Shadow DOM
            success = await self._try_shadow_dom_method(timeout)
            if success:
                logger.info("通过 Shadow DOM 解决验证码成功！")
                return True

            logger.warning("未能找到验证码按钮")
            return False

        except Exception as exc:
            logger.error(f"解决验证码时出错: {exc}")
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
            # 等待几秒钟让验证码加载
            await asyncio.sleep(2)

            # 尝试多种选择器
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
                    logger.debug(f"尝试选择器: {selector}")
                    element = await self.tab.query(selector, timeout=2)
                    if element:
                        logger.info(f"找到元素: {selector}")
                        await element.click()
                        await asyncio.sleep(3)  # 等待验证完成
                        return True
                except Exception:
                    continue

            return False

        except Exception as exc:
            logger.debug(f"方法 1 失败: {exc}")
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
            # 查找所有 Shadow Roots
            shadow_roots = await self.tab.find_shadow_roots(deep=True, timeout=timeout)

            if not shadow_roots:
                logger.debug("未找到 Shadow Roots")
                return False

            logger.info(f"找到 {len(shadow_roots)} 个 Shadow Roots")

            for sr in shadow_roots:
                try:
                    # 尝试在 Shadow Root 中查找按钮
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
                                logger.info(f"在 Shadow Root 中找到元素: {selector}")
                                await element.click()
                                await asyncio.sleep(3)
                                return True
                        except Exception:
                            continue

                    # 也尝试查找 iframe
                    try:
                        iframe = await sr.query('iframe', timeout=1)
                        if iframe:
                            logger.info("找到 iframe，尝试进入")
                            body = await iframe.find(tag_name='body', timeout=2)
                            if body:
                                inner_sr = await body.get_shadow_root(timeout=2)
                                if inner_sr:
                                    for selector in selectors:
                                        try:
                                            element = await inner_sr.query(selector, timeout=1)
                                            if element:
                                                logger.info(f"在 iframe Shadow Root 中找到: {selector}")
                                                await element.click()
                                                await asyncio.sleep(3)
                                                return True
                                        except Exception:
                                            continue
                    except Exception:
                        continue

                except Exception as exc:
                    logger.debug(f"处理 Shadow Root 时出错: {exc}")
                    continue

            return False

        except Exception as exc:
            logger.debug(f"方法 2 失败: {exc}")
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

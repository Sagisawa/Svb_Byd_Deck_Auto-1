"""
手牌管理器
专门使用SIFT特征匹配识别手牌区域中的卡牌及其费用
"""

import logging
import time
from typing import Any, Dict, List, Optional, Protocol
from src.config.paths import get_card_cost_dir
from .sift_card_recognition import SiftCardRecognition

logger = logging.getLogger(__name__)

CardInfo = Dict[str, Any]


class _U2DeviceLike(Protocol):
    def click(self, *args: Any, **kwargs: Any) -> Any: ...


class _DeviceStateLike(Protocol):
    def take_screenshot(self) -> Any: ...

    def get_u2_device(self) -> Optional[_U2DeviceLike]: ...


class HandCardManager:
    """手牌管理器类"""
    
    def __init__(self, device_state: Optional[_DeviceStateLike] = None):
        """
        初始化手牌管理器
        
        参数：
            device_state: 设备状态对象
        """
        self.device_state: Optional[_DeviceStateLike] = device_state
        self.hand_area = (229, 539, 1130, 710)  # 手牌区域坐标

        # 每台设备、每个管理器持有独立识别器，模板数据由内部共享。
        self.sift_recognition = SiftCardRecognition(get_card_cost_dir(ensure=True))

    def recognize_hand_cards(self, screenshot, silent=False) -> List[CardInfo]:
        """
        使用SIFT识别手牌区域中的卡牌
        
        参数：
            screenshot: 游戏截图
            silent: 是否静默模式，不输出日志
            
        返回：
            List[Dict]: 识别到的卡牌列表，每个字典包含:
                - center: (x, y) 卡牌中心位置
                - cost: int 卡牌费用
                - name: str 卡牌名称
                - confidence: float 匹配置信度
        """
        # 优先通过内存桥接获取确切的手牌数据与计算坐标
        try:
            from src.bridge.helper import get_memory_adapter
            mem_adapter = get_memory_adapter()
            if mem_adapter and mem_adapter.is_available():
                mem_cards = mem_adapter.get_hand_cards()
                if mem_cards:
                    if not silent:
                        card_info = [f"{c['cost']}费_{c['name']}" for c in mem_cards]
                        logger.info(f"[内存] 手牌详情: {' | '.join(card_info)}")
                    return mem_cards
        except Exception:
            pass

        try:
            # 使用SIFT识别手牌
            recognized_cards = self.sift_recognition.recognize_hand_cards(
                screenshot, hand_area=self.hand_area
            )
            
            if recognized_cards and not silent:
                # 输出识别结果
                card_info = []
                for card in recognized_cards:
                    card_info.append(f"{card['cost']}费_{card['name']}")
                    #加上了置信度 card_info.append(f"{card['cost']}费_{card['name']}({card['confidence']:.2f})")
                logger.info(f"手牌详情: {' | '.join(card_info)}")
                
            elif not recognized_cards and not silent:
                logger.info("SIFT未识别到任何手牌")
            
            return recognized_cards
            
        except Exception as e:
            logger.error(f"手牌识别出错: {str(e)}")
            return []
    
    def get_hand_cards_with_retry(self, max_retries: int = 3, silent: bool = False) -> List[CardInfo]:
        """
        带重试机制的手牌识别
        
        参数：
            max_retries: 最大重试次数
            silent: 是否静默模式，不输出日志
            
        返回：
            List[Dict]: 识别到的卡牌列表
        """
        device_state = self.device_state
        if device_state is None:
            if not silent:
                logger.warning("device_state未初始化，无法识别手牌")
            return []
        assert device_state is not None

        for attempt in range(max_retries):
            try:
                # 获取截图
                screenshot = device_state.take_screenshot()
                if screenshot is None:
                    if not silent:
                        logger.warning(f"第{attempt + 1}次尝试获取截图失败")
                    continue
                
                # 识别手牌
                cards = self.recognize_hand_cards(screenshot, silent)
                
                if cards:
                    return cards
                else:
                    if not silent:
                        logger.warning(f"第{attempt + 1}次尝试未识别到手牌")
                    
                    # 未识别到手牌时，点击展牌按钮再重试
                    from src.config.game_constants import SHOW_CARDS_BUTTON, SHOW_CARDS_RANDOM_X, SHOW_CARDS_RANDOM_Y
                    import random
                    u2_device = device_state.get_u2_device()
                    if u2_device is None:
                        if not silent:
                            logger.warning("u2_device未连接，无法点击展牌按钮")
                        continue

                    u2_device.click(
                        SHOW_CARDS_BUTTON[0] + random.randint(SHOW_CARDS_RANDOM_X[0], SHOW_CARDS_RANDOM_X[1]),
                        SHOW_CARDS_BUTTON[1] + random.randint(SHOW_CARDS_RANDOM_Y[0], SHOW_CARDS_RANDOM_Y[1])
                    )
                    # 展牌动画/结算动画时，0.1s 可能仍在过渡帧，容易导致SIFT误识别或漏识别
                    time.sleep(0.25)
                    #移除手牌光标提高识别率
                    # from src.config.game_constants import DEFAULT_ATTACK_TARGET
                    # self.device_state.u2_device.click(DEFAULT_ATTACK_TARGET[0] + random.randint(-2,2), DEFAULT_ATTACK_TARGET[1] + random.randint(-2,2))
                    # time.sleep(0.3)
            
            except Exception as e:
                logger.error(f"第{attempt + 1}次手牌识别尝试出错: {str(e)}")
            
            # 等待一段时间后重试
            if attempt < max_retries - 1:
                time.sleep(1)
        
        if not silent:
            logger.warning(f"经过{max_retries}次尝试仍未识别到手牌")
        return []
    
    def get_card_cost_by_name(self, card_name: str) -> Optional[int]:
        """
        根据卡牌名称获取费用
        
        参数：
            card_name: 卡牌名称
            
        返回：
            Optional[int]: 卡牌费用，如果未找到返回None
        """
        return self.sift_recognition.get_card_cost_by_name(card_name)
    
    def get_all_card_names(self) -> List[str]:
        """
        获取所有卡牌名称
        
        返回：
            List[str]: 所有卡牌名称列表
        """
        return self.sift_recognition.get_all_card_names()
    
    def get_all_card_costs(self) -> Dict[str, int]:
        """
        获取所有卡牌的费用映射
        
        返回：
            Dict[str, int]: 卡牌名称到费用的映射
        """
        return self.sift_recognition.get_all_card_costs()
    
    def sort_cards_by_cost(self, cards: List[CardInfo]) -> List[CardInfo]:
        """
        按费用排序卡牌（从低到高）
        
        参数：
            cards: 卡牌列表
            
        返回：
            List[Dict]: 排序后的卡牌列表
        """
        return sorted(cards, key=lambda card: card['cost'])
    
    def sort_cards_by_position(self, cards: List[CardInfo]) -> List[CardInfo]:
        """
        按位置排序卡牌（从左到右）
        
        参数：
            cards: 卡牌列表
            
        返回：
            List[Dict]: 排序后的卡牌列表
        """
        return sorted(cards, key=lambda card: card['center'][0])
    
    def filter_cards_by_cost(self, cards: List[CardInfo], max_cost: int) -> List[CardInfo]:
        """
        按费用过滤卡牌
        
        参数：
            cards: 卡牌列表
            max_cost: 最大费用
            
        返回：
            List[Dict]: 过滤后的卡牌列表
        """
        return [card for card in cards if card['cost'] <= max_cost]
    
    def get_cards_summary(self, cards: List[CardInfo]) -> str:
        """
        获取卡牌摘要信息
        
        参数：
            cards: 卡牌列表
            
        返回：
            str: 卡牌摘要信息
        """
        if not cards:
            return "无手牌"
        
        # 按费用分组
        cost_groups = {}
        for card in cards:
            cost = card['cost']
            if cost not in cost_groups:
                cost_groups[cost] = []
            cost_groups[cost].append(card['name'])
        
        # 生成摘要
        summary_parts = []
        for cost in sorted(cost_groups.keys()):
            names = cost_groups[cost]
            summary_parts.append(f"{cost}费({len(names)}张): {', '.join(names)}")
        
        return " | ".join(summary_parts) 

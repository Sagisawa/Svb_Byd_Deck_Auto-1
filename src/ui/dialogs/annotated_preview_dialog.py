#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对局画面截图与坐标标定预览对话框。

为自动化脚本提供实时的视觉标定窗口：
1. 原生适配 WGC/设备截图，清晰还原游戏对局画面；
2. 结合内存适配器 (SnapshotAdapter) 实时叠加敌方随从、我方随从、手牌与守护目标的权威点击坐标点；
3. 支持随时“重新截图”、“坐标标定显示”切换以及“保存图片”功能。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

logger = logging.getLogger(__name__)


class AnnotatedPreviewDialog(QDialog):
    """带坐标标注的截图预览对话框。"""

    refresh_requested = pyqtSignal()

    def __init__(self, parent=None, initial_image: Optional[QImage] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("游戏画面截图与坐标标定预览")
        self.resize(1100, 720)
        self.setMinimumSize(720, 480)

        self._raw_pixmap: Optional[QPixmap] = None
        self._annotated_pixmap: Optional[QPixmap] = None

        self._build_ui()

        if initial_image is not None and not initial_image.isNull():
            self.update_image(initial_image)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # 顶部工具栏
        top_bar = QHBoxLayout()
        top_bar.setSpacing(10)

        self.refresh_btn = QPushButton("重新截图")
        self.refresh_btn.setObjectName("SecondaryButton")
        self.refresh_btn.setMinimumHeight(32)
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        top_bar.addWidget(self.refresh_btn)

        self.coords_check = QCheckBox("显示坐标标定")
        self.coords_check.setChecked(True)
        self.coords_check.stateChanged.connect(self._update_display)
        top_bar.addWidget(self.coords_check)

        self.save_btn = QPushButton("保存图片")
        self.save_btn.setObjectName("SecondaryButton")
        self.save_btn.setMinimumHeight(32)
        self.save_btn.clicked.connect(self._save_image)
        top_bar.addWidget(self.save_btn)

        top_bar.addStretch(1)

        self.status_label = QLabel("正在初始化...")
        self.status_label.setStyleSheet("color: #9399b2; font-size: 12px;")
        top_bar.addWidget(self.status_label)

        layout.addLayout(top_bar)

        # 图像展示区
        self.image_container = QLabel()
        self.image_container.setAlignment(Qt.AlignCenter)
        self.image_container.setStyleSheet(
            "background-color: #11111b; border: 1px solid #313244; border-radius: 8px;"
        )
        layout.addWidget(self.image_container, 1)

    def _on_refresh_clicked(self) -> None:
        self.status_label.setText("正在拉取最新游戏画面...")
        self.refresh_requested.emit()

    def update_image(self, qimage: QImage) -> None:
        """接收新截图，重新结合内存生成标定层并刷新显示。"""
        if qimage is None or qimage.isNull():
            self.status_label.setText("未获取到有效图像")
            return

        self._raw_pixmap = QPixmap.fromImage(qimage)
        self._annotated_pixmap = self._render_annotations(self._raw_pixmap)
        self._update_display()

    def _render_annotations(self, base_pixmap: QPixmap) -> QPixmap:
        """在基础截图上绘制坐标标定圆点与信息文本。"""
        result = QPixmap(base_pixmap)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        font_label = QFont("Microsoft YaHei", 9, QFont.Bold)
        font_small = QFont("Microsoft YaHei", 8)
        painter.setFont(font_label)

        # 获取内存适配器
        mem_adapter = None
        has_memory = False
        try:
            from src.bridge.helper import get_memory_adapter
            mem_adapter = get_memory_adapter()
            if mem_adapter and mem_adapter.is_available():
                has_memory = True
        except Exception as exc:
            logger.debug("AnnotatedPreviewDialog get_memory_adapter: %s", exc)

        width_px = base_pixmap.width()
        height_px = base_pixmap.height()

        # 标准设计视口为 1280x720，计算当前图像相对于标准坐标系的缩放比
        scale_x = width_px / 1280.0
        scale_y = height_px / 720.0

        enemy_followers = []
        ward_targets = []
        our_followers = []
        hand_cards = []
        is_mulligan = False
        turn = None

        if has_memory and mem_adapter:
            try:
                turn = mem_adapter.get_turn()
                is_mulligan = bool(turn == 0)
                enemy_followers = list(mem_adapter.get_enemy_followers(include_amulets=True) or [])
                ward_targets = list(mem_adapter.get_shield_targets() or [])
                our_followers = list(mem_adapter.get_our_followers() or [])
                if is_mulligan:
                    hand_cards = list(mem_adapter.get_mulligan_cards() or [])
                else:
                    hand_cards = list(mem_adapter.get_hand_cards() or [])
            except Exception as exc:
                logger.debug("Failed reading memory details for preview: %s", exc)

        # 构建状态栏提示
        status_parts = [f"分辨率: {width_px}×{height_px}"]
        if has_memory:
            turn_str = f"第{turn}回合" if turn is not None and turn > 0 else ("换牌阶段" if is_mulligan else "对局中")
            status_parts.append(f"[内存已连接 · {turn_str}]")

            our_followers_cnt = sum(1 for o in our_followers if str(o[2] if len(o) > 2 else "normal") != "amulet")
            our_amulets_cnt = sum(1 for o in our_followers if str(o[2] if len(o) > 2 else "") == "amulet")
            status_parts.append(f"我方随从: {our_followers_cnt}")
            if our_amulets_cnt > 0:
                status_parts.append(f"我方护符: {our_amulets_cnt}")

            enemy_followers_cnt = sum(
                1 for e in enemy_followers
                if not (bool(e[5]) if len(e) > 5 else (str(e[2] if len(e) > 2 else "") == "amulet" or str(e[3] if len(e) > 3 else "1") == "0"))
            )
            enemy_amulets_cnt = len(enemy_followers) - enemy_followers_cnt
            status_parts.append(f"敌方随从: {enemy_followers_cnt}")
            if enemy_amulets_cnt > 0:
                status_parts.append(f"敌方护符: {enemy_amulets_cnt}")

            status_parts.append(f"手牌: {len(hand_cards)}")
            if ward_targets:
                status_parts.append(f"守护: {len(ward_targets)}")
        else:
            status_parts.append("[纯视觉/非对局状态 · 未检测到活跃内存快照]")
        self.status_label.setText("  |  ".join(status_parts))

        def draw_marker(
            raw_x: int,
            raw_y: int,
            dot_color: QColor,
            label_text: str,
            text_color: QColor,
            is_ward: bool = False,
        ) -> None:
            # 缩放到当前分辨率下的绝对像素位置
            sx = int(round(raw_x * scale_x))
            sy = int(round(raw_y * scale_y))

            r = 9
            # 绘制守护特殊标识环
            if is_ward:
                painter.setPen(QPen(QColor(80, 200, 255, 230), 3))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(sx - r - 4, sy - r - 4, (r + 4) * 2, (r + 4) * 2)

            # 外描边与核心圆点
            painter.setPen(QPen(QColor(0, 0, 0, 220), 2))
            painter.setBrush(QBrush(dot_color))
            painter.drawEllipse(sx - r, sy - r, r * 2, r * 2)

            # 中心白色反光微点
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(255, 255, 255, 240)))
            painter.drawEllipse(sx - 2, sy - 2, 4, 4)

            # 绘制文本气泡背景
            painter.setFont(font_label)
            metrics = painter.fontMetrics()
            tw = metrics.horizontalAdvance(label_text)
            th = metrics.height()

            bx = sx - tw // 2 - 6
            # 上半部随从文本画在圆点下方，下半部手牌/我方随从文本画在圆点上方
            by = sy - r - th - 7 if sy > (height_px * 0.45) else sy + r + 5

            painter.setPen(QPen(dot_color, 1))
            painter.setBrush(QBrush(QColor(17, 17, 27, 225)))
            painter.drawRoundedRect(bx, by, tw + 12, th + 4, 4, 4)

            # 绘制文本
            painter.setPen(text_color)
            painter.drawText(bx + 6, by + th - 3, label_text)

        # 1. 敌方随从与护符与护盾标注 (随从红色 / 护符紫色)
        ward_set = {(int(w[0]), int(w[1])) for w in ward_targets if len(w) >= 2}
        for idx, enemy in enumerate(enemy_followers):
            try:
                ex, ey = int(enemy[0]), int(enemy[1])
                ftype = str(enemy[2] if len(enemy) > 2 else "normal")
                hp = str(enemy[3] if len(enemy) > 3 else "?")
                name = str(enemy[4] or "") if len(enemy) > 4 else ""
                is_amulet = bool(enemy[5]) if len(enemy) > 5 else (ftype == "amulet" or hp == "0")
                countdown = int(enemy[6]) if len(enemy) > 6 else 0

                if is_amulet:
                    cd_tag = f" 倒数={countdown}" if countdown > 0 else ""
                    name_str = f" {name}" if name else ""
                    label = f"敌{idx + 1} ({ex},{ey}) [护符]{name_str}{cd_tag}"
                    draw_marker(ex, ey, QColor("#cba6f7"), label, QColor("#ffffff"), is_ward=False)
                else:
                    # 模糊比对是否为守护
                    is_ward = any(abs(ex - wx) < 50 for wx, _ in ward_set)
                    tag = " 【盾】" if is_ward else ""
                    name_str = f" {name}" if name else ""
                    label = f"敌{idx + 1} ({ex},{ey}) [随从]{name_str} HP={hp}{tag}"
                    draw_marker(ex, ey, QColor("#f38ba8"), label, QColor("#ffffff"), is_ward=is_ward)
            except Exception:
                continue

        # 2. 我方随从与护符标注 (绿色:疾驰 / 黄色:突进 / 紫色:护符 / 青色:普通随从)
        type_color_map = {
            "green": (QColor("#a6e3a1"), "疾驰"),
            "yellow": (QColor("#f9e2af"), "突进"),
            "amulet": (QColor("#cba6f7"), "护符"),
            "normal": (QColor("#89dceb"), "随从"),
        }
        for idx, our in enumerate(our_followers):
            try:
                ox, oy = int(our[0]), int(our[1])
                ftype = str(our[2] if len(our) > 2 else "normal")
                name = str(our[3] or "") if len(our) > 3 else ""
                dot_col, type_desc = type_color_map.get(ftype, (QColor("#89dceb"), "随从"))
                label = f"我{idx + 1} ({ox},{oy}) [{type_desc}] {name}".strip()
                draw_marker(ox, oy, dot_col, label, dot_col)
            except Exception:
                continue

        # 3. 我方手牌标注 (橙黄色)
        for idx, card in enumerate(hand_cards):
            try:
                center = card.get("center", (0, 0))
                hx, hy = int(center[0]), int(center[1])
                cname = str(card.get("name") or "")
                cost = card.get("cost", 0)
                prefix = "换" if is_mulligan else "手"
                label = f"{prefix}{idx + 1} ({hx},{hy}) {cost}费_{cname}".strip()
                draw_marker(hx, hy, QColor("#fab387"), label, QColor("#fab387"))
            except Exception:
                continue

        # 4. 绘制左上角图例面板
        legend_w = 270
        legend_h = 120 if not is_mulligan else 80
        painter.setPen(QPen(QColor("#45475a"), 1))
        painter.setBrush(QBrush(QColor(17, 17, 27, 220)))
        painter.drawRoundedRect(16, 16, legend_w, legend_h, 6, 6)

        painter.setFont(font_small)
        if is_mulligan:
            painter.setPen(QColor("#fab387"))
            painter.drawText(26, 40, "● 换牌阶段 · 固定卡位 (橙黄色)")
            painter.setPen(QColor("#cdd6f4"))
            painter.drawText(26, 64, "● 权威坐标源: Svb SnapshotAdapter")
        else:
            painter.setPen(QColor("#f38ba8"))
            painter.drawText(26, 36, "● 敌方随从 (红)  环形: 【守护】")
            painter.setPen(QColor("#cba6f7"))
            painter.drawText(26, 56, "● 敌/我护符 (紫)")
            painter.setPen(QColor("#a6e3a1"))
            painter.drawText(26, 76, "● 我方随从: 疾驰(绿) 突进(黄) 普通(青)")
            painter.setPen(QColor("#fab387"))
            painter.drawText(26, 96, "● 我方展开手牌 (橙黄)")
            painter.setPen(QColor("#6c7086"))
            painter.drawText(26, 116, "权威坐标源: Svb SnapshotAdapter")

        painter.end()
        return result

    def _update_display(self) -> None:
        target = self._annotated_pixmap if self.coords_check.isChecked() else self._raw_pixmap
        if not target or target.isNull():
            return
        container_size = self.image_container.size()
        if container_size.width() <= 10 or container_size.height() <= 10:
            return
        scaled = target.scaled(
            container_size,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.image_container.setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_display()

    def _save_image(self) -> None:
        target = self._annotated_pixmap if self.coords_check.isChecked() else self._raw_pixmap
        if not target or target.isNull():
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存截图预览",
            "shadowverse_annotated.png",
            "PNG 图片 (*.png);;JPEG 图片 (*.jpg)",
        )
        if path:
            target.save(path)

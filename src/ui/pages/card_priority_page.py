#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡牌优先级与模式选项页面。"""

from __future__ import annotations

import json
import os

from typing import Any

from PyQt5.QtCore import Qt as _Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.config.paths import get_config_path
from src.config.paths import get_card_cost_dir
from src.config.config_repository import ConfigRepository
from src.config.effects_registry import get_triggers
from src.utils.card_filename import (
    is_evo_card_name,
    make_enhance_key,
    normalize_card_base_name,
    normalize_config_key,
    parse_card_filename,
)


# 不同环境的 PyQt5 类型桩存在差异，因此保持 Qt 属性访问方式兼容。
Qt: Any = _Qt


class CardPriorityPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_widget: Any = parent
        self.config_data = self.load_config()
        self.card_widgets = []
        # 爆能行与基础卡牌行共用进化优先级。
        self._base_evolve_priority_inputs = {}
        self._enhance_evolve_priority_views = {}
        self.init_ui()

    def _sync_enhance_evolve_priority_views(self, base_name: str, text: str) -> None:
        views = self._enhance_evolve_priority_views.get(str(base_name), [])
        for v in list(views):
            try:
                v.setText(text)
            except Exception:
                pass

    def init_ui(self):
        self.setObjectName("CardPriorityPage")
        self.setProperty("pageRoot", True)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(24, 22, 24, 24)
        main_layout.setSpacing(16)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(12)
        heading_layout = QVBoxLayout()
        heading_layout.setSpacing(3)
        title_label = QLabel("卡牌设置")
        title_label.setObjectName("PageTitle")
        title_label.setProperty("heading", "page")
        heading_layout.addWidget(title_label)
        desc_label = QLabel("当前卡组的出牌、进化、留牌与特殊效果配置")
        desc_label.setObjectName("SubtleText")
        desc_label.setProperty("muted", True)
        heading_layout.addWidget(desc_label)
        header_layout.addLayout(heading_layout)
        header_layout.addStretch()
        self.help_btn = QPushButton("帮助")
        self.help_btn.setObjectName("SecondaryButton")
        self.help_btn.clicked.connect(self.show_card_settings_help)
        header_layout.addWidget(self.help_btn)
        main_layout.addLayout(header_layout)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("PriorityScrollArea")
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("PriorityScrollContent")
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 6, 0)
        self.scroll_layout.setSpacing(10)
        self.scroll_area.setWidget(self.scroll_content)
        main_layout.addWidget(self.scroll_area)

        action_panel = QFrame()
        action_panel.setObjectName("SurfacePanel")
        action_panel.setProperty("card", True)
        btn_layout = QHBoxLayout(action_panel)
        btn_layout.setContentsMargins(14, 10, 14, 10)
        btn_layout.setSpacing(10)
        btn_layout.addStretch()
        self.save_btn = QPushButton("保存设置")
        self.save_btn.setObjectName("PrimaryButton")
        self.save_btn.setProperty("variant", "primary")
        self.save_btn.clicked.connect(self.save_config)
        self.back_btn = QPushButton("返回主界面")
        self.back_btn.setObjectName("SecondaryButton")
        self.back_btn.clicked.connect(self._go_back)
        btn_layout.addWidget(self.back_btn)
        btn_layout.addWidget(self.save_btn)
        main_layout.addWidget(action_panel)

        self.load_card_priority_settings()

    def _go_back(self) -> None:
        try:
            sw = getattr(self.parent_widget, "stacked_widget", None)
            if sw is not None and hasattr(sw, "setCurrentIndex"):
                sw.setCurrentIndex(0)
        except Exception:
            pass

    def _build_effects_tag(self, base_name: str, config_key: str, is_enhance: bool) -> str:
        try:
            from src.config.strategy_effects import get_card_effect_steps
        except Exception:
            return ""

        tags = []
        for t in get_triggers():
            tid = str(t.get("id") or "")
            short = str(t.get("short") or tid)
            if not tid:
                continue

            # ``on_play`` 与 ``on_play_bridge`` 按支持爆能的手牌配置键索引；随从触发器按基础随从名索引。
            if tid in ("on_play", "on_play_bridge"):
                key = str(config_key or base_name)
            else:
                if is_enhance:
                    continue
                key = str(base_name)

            steps = get_card_effect_steps(self.config_data, card_name=key, trigger=tid)
            if steps:
                tags.append(short)

        return "/".join(tags)

    def open_effects_editor(
        self, base_name: str, config_key: str, display_name: str, is_enhance: bool
    ) -> None:
        try:
            from src.ui.pages.card_effects_editor import CardEffectsDialog
        except Exception as e:
            QMessageBox.warning(self, "错误", f"无法打开特殊效果编辑器: {str(e)}")
            return

        dlg = CardEffectsDialog(
            self,
            base_name=str(base_name or ""),
            config_key=str(config_key or base_name or ""),
            display_name=str(display_name or base_name or ""),
            is_enhance=bool(is_enhance),
            deck_card_names=self._current_deck_card_names(),
        )
        res = dlg.exec_()
        if res == QDialog.Accepted:
            self.refresh_card_priority()

    def _current_deck_card_names(self) -> list:
        names = []
        seen = set()
        for card in getattr(self, "card_widgets", []) or []:
            if not isinstance(card, dict) or bool(card.get("is_enhance")):
                continue
            name = str(card.get("card_name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        if names:
            return names

        for name in (getattr(self, "config_data", {}) or {}).get("high_priority_cards", {}).keys():
            base = str(name or "").split("_enhance_", 1)[0].strip()
            if base and base not in seen:
                seen.add(base)
                names.append(base)
        return names

    def show_card_settings_help(self):
        """显示卡牌设置帮助"""
        help_text = """
卡牌设置详细说明

一、优先级设置

1. 出牌优先级（进化前/进化后）
   作用：控制出牌顺序，会根据进化是否解锁切换不同阶段的优先级
   数值含义：数字越小优先级越高
   默认值：999（最低优先级）
   示例：若“进化前=1、进化后=5”，则前期更倾向优先打出；进化解锁后优先级会降低

2. 进化优先级
   作用：控制进化/超进化时的选择顺序
   数值含义：数字越小优先级越高
   默认值：999（最低优先级）
   示例：进化时会优先选择进化优先级更高（数字更小）的随从

二、模式选项设置

二、留牌与特殊效果

1. 必留(force_keep)
   作用：换牌阶段强制保留该基础卡（爆能档位共用）。

2. 特殊效果...
   作用：进入二级编辑器，按触发时机配置操作（出牌/攻击/进化/超进化）。
   说明：爆能档位行也可配置攻击/进化等触发，作为该爆能档位专属效果。
"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("卡牌设置帮助")
        msg_box.setText(help_text)
        msg_box.setIcon(QMessageBox.Information)
        msg_box.addButton(QMessageBox.Ok)
        msg_box.exec_()

    def load_config(self):
        config_path = get_config_path()
        cfg, _, _ = ConfigRepository(config_path).load_existing(allow_default_on_error=True)
        return cfg if isinstance(cfg, dict) else {}

    def load_card_priority_settings(self):
        # 清空现有内容
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.card_widgets = []
        self._base_evolve_priority_inputs = {}
        self._enhance_evolve_priority_views = {}

        card_dir = get_card_cost_dir(ensure=True)
        if not os.path.exists(card_dir):
            no_card_label = QLabel("未找到卡组卡片，请先在卡组工作区选择并应用卡牌")
            no_card_label.setObjectName("EmptyState")
            no_card_label.setProperty("status", "warning")
            no_card_label.setAlignment(Qt.AlignCenter)
            self.scroll_layout.addWidget(no_card_label)
            self.scroll_layout.addStretch()
            return

        card_files = [
            f
            for f in os.listdir(card_dir)
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
            and not is_evo_card_name(f)
        ]
        if not card_files:
            no_card_label = QLabel("没有找到卡片，请先在卡组工作区选择并应用卡牌")
            no_card_label.setObjectName("EmptyState")
            no_card_label.setProperty("status", "warning")
            no_card_label.setAlignment(Qt.AlignCenter)
            self.scroll_layout.addWidget(no_card_label)
            self.scroll_layout.addStretch()
            return

        # 从文件名构建基础卡与爆能层级显示条目。
        entries = []
        for card_file in card_files:
            try:
                base_cost, enhance_costs, base_name = parse_card_filename(card_file)
            except Exception:
                base_cost, enhance_costs, base_name = 0, [], card_file.split("_", 1)[-1].rsplit(".", 1)[0]

            base_name = normalize_card_base_name(str(base_name or "").strip())
            if not base_name:
                continue
            enhance_costs = list(enhance_costs or [])

            entries.append(
                {
                    "file": card_file,
                    "base_name": base_name,
                    "config_key": normalize_config_key(base_name),
                    "base_cost": int(base_cost or 0),
                    "variant_cost": int(base_cost or 0),
                    "is_enhance": False,
                    "enhance_costs": enhance_costs,
                }
            )
            for c in enhance_costs:
                entries.append(
                    {
                        "file": card_file,
                        "base_name": base_name,
                        "config_key": normalize_config_key(make_enhance_key(base_name, c)),
                        "base_cost": int(base_cost or 0),
                        "variant_cost": int(c),
                        "is_enhance": True,
                        "enhance_costs": enhance_costs,
                    }
                )

        entries.sort(
            key=lambda e: (
                int(e.get("base_cost", 0)),
                str(e.get("base_name", "")),
                1 if bool(e.get("is_enhance")) else 0,
                int(e.get("variant_cost", 0)),
            )
        )

        for entry in entries:
            card_file = entry["file"]
            base_name = entry["base_name"]
            config_key = entry["config_key"]
            is_enhance = bool(entry.get("is_enhance"))
            variant_cost = int(entry.get("variant_cost", 0))

            card_row = QFrame()
            card_row.setObjectName("SurfacePanel")
            card_row.setProperty("card", True)
            card_row.setProperty("enhance", is_enhance)
            card_row.setMinimumHeight(126)
            card_row.setMaximumHeight(142)
            card_row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            row_layout = QHBoxLayout(card_row)
            row_layout.setContentsMargins(14, 12, 14, 12)
            row_layout.setSpacing(14)

            card_label = QLabel()
            card_label.setObjectName("CardArtwork")
            card_label.setFixedSize(72, 102)
            card_path = os.path.join(card_dir, card_file)
            pixmap = QPixmap(card_path)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(68, 98, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                card_label.setPixmap(pixmap)
            card_label.setAlignment(Qt.AlignCenter)
            row_layout.addWidget(card_label)

            if is_enhance:
                display_name = f"{base_name} (爆能{variant_cost})"
            else:
                display_name = f"{base_name}"

            identity_widget = QWidget()
            identity_widget.setMinimumWidth(170)
            identity_widget.setMaximumWidth(230)
            identity_layout = QVBoxLayout(identity_widget)
            identity_layout.setContentsMargins(0, 0, 0, 0)
            identity_layout.setSpacing(5)
            name_label = QLabel(display_name)
            name_label.setObjectName("CardName")
            name_label.setProperty("heading", "section")
            name_label.setWordWrap(True)
            name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            identity_layout.addWidget(name_label)
            variant_label = QLabel(
                f"爆能档位 · {variant_cost} 费"
                if is_enhance
                else f"基础卡 · {variant_cost} 费"
            )
            variant_label.setObjectName("SubtleText")
            variant_label.setProperty("muted", True)
            identity_layout.addWidget(variant_label)
            identity_layout.addStretch()
            row_layout.addWidget(identity_widget)

            # 出牌优先级按基础卡或爆能变体键分别保存。
            high_priority = self.config_data.get("high_priority_cards", {}).get(config_key, {})

            settings_layout = QGridLayout()
            settings_layout.setContentsMargins(0, 0, 0, 0)
            settings_layout.setHorizontalSpacing(12)
            settings_layout.setVerticalSpacing(7)

            pre_label = QLabel("出牌 · 进化前")
            pre_label.setObjectName("FieldLabel")
            pre_label.setProperty("muted", True)
            settings_layout.addWidget(pre_label, 0, 0)
            play_priority_pre_input = QLineEdit()
            play_priority_pre_input.setObjectName("PriorityInput")
            play_priority_pre_input.setFixedWidth(76)
            play_priority_pre_input.setAlignment(Qt.AlignCenter)
            play_priority_pre_input.setPlaceholderText("0-999")
            if isinstance(high_priority, dict):
                pre_priority = high_priority.get(
                    "priority_pre_evolution", high_priority.get("priority", "")
                )
                play_priority_pre_input.setText(str(pre_priority) if pre_priority != "" else "")
            settings_layout.addWidget(play_priority_pre_input, 1, 0)

            post_label = QLabel("出牌 · 进化后")
            post_label.setObjectName("FieldLabel")
            post_label.setProperty("muted", True)
            settings_layout.addWidget(post_label, 0, 1)
            play_priority_post_input = QLineEdit()
            play_priority_post_input.setObjectName("PriorityInput")
            play_priority_post_input.setFixedWidth(76)
            play_priority_post_input.setAlignment(Qt.AlignCenter)
            play_priority_post_input.setPlaceholderText("0-999")
            if isinstance(high_priority, dict):
                post_priority = high_priority.get(
                    "priority_post_evolution", high_priority.get("priority", "")
                )
                play_priority_post_input.setText(
                    str(post_priority) if post_priority != "" else ""
                )
            settings_layout.addWidget(play_priority_post_input, 1, 1)

            force_keep_checkbox = None
            evolve_priority_input = None
            evolve_priority_view = None

            if not is_enhance:
                # 强制留牌（仅基础卡）
                keep_label = QLabel("换牌")
                keep_label.setObjectName("FieldLabel")
                keep_label.setProperty("muted", True)
                settings_layout.addWidget(keep_label, 0, 3)
                force_keep_checkbox = QCheckBox("必留")
                base_cfg = self.config_data.get("high_priority_cards", {}).get(base_name, {})
                if isinstance(base_cfg, dict) and base_cfg.get("force_keep") is True:
                    force_keep_checkbox.setChecked(True)
                settings_layout.addWidget(force_keep_checkbox, 1, 3)

                # 进化优先级（仅基础卡）
                evolve_label = QLabel("进化优先级")
                evolve_label.setObjectName("FieldLabel")
                evolve_label.setProperty("muted", True)
                settings_layout.addWidget(evolve_label, 0, 2)
                evolve_priority_input = QLineEdit()
                evolve_priority_input.setObjectName("PriorityInput")
                evolve_priority_input.setFixedWidth(76)
                evolve_priority_input.setAlignment(Qt.AlignCenter)
                evolve_priority_input.setPlaceholderText("0-999")
                evolve_priority = self.config_data.get("evolve_priority_cards", {}).get(
                    base_name, {}
                )
                if isinstance(evolve_priority, dict):
                    evolve_priority_input.setText(str(evolve_priority.get("priority", "")))
                settings_layout.addWidget(evolve_priority_input, 1, 2)

        # 爆能行的进化优先级与该基础卡输入保持同步。
                self._base_evolve_priority_inputs[base_name] = evolve_priority_input
                evolve_priority_input.textChanged.connect(
                    lambda text, n=base_name: self._sync_enhance_evolve_priority_views(n, text)
                )

            else:
                # 爆能档位不支持独立进化优先级（进化按随从名判定）。这里显示共用值，避免误解。
                evolve_label = QLabel("进化优先级 · 共用")
                evolve_label.setObjectName("FieldLabel")
                evolve_label.setProperty("muted", True)
                settings_layout.addWidget(evolve_label, 0, 2)
                evolve_priority_view = QLineEdit()
                evolve_priority_view.setObjectName("SharedPriorityInput")
                evolve_priority_view.setFixedWidth(76)
                evolve_priority_view.setAlignment(Qt.AlignCenter)
                evolve_priority_view.setReadOnly(True)
                evolve_priority_view.setToolTip("进化优先级按随从名共用，请在基础卡行设置")
                evolve_priority = self.config_data.get("evolve_priority_cards", {}).get(
                    base_name, {}
                )
                if isinstance(evolve_priority, dict):
                    evolve_priority_view.setText(str(evolve_priority.get("priority", "")))
                settings_layout.addWidget(evolve_priority_view, 1, 2)

                self._enhance_evolve_priority_views.setdefault(base_name, []).append(
                    evolve_priority_view
                )
                try:
                    base_input = self._base_evolve_priority_inputs.get(base_name)
                    if base_input is not None:
                        evolve_priority_view.setText(base_input.text())
                except Exception:
                    pass

            settings_layout.setColumnStretch(4, 1)
            row_layout.addLayout(settings_layout, 1)

        # Step3A 特殊效果交由二级编辑器处理。
            effects_layout = QVBoxLayout()
            effects_layout.setContentsMargins(0, 0, 0, 0)
            effects_layout.setSpacing(7)
            effects_title = QLabel("特殊效果")
            effects_title.setObjectName("FieldLabel")
            effects_title.setProperty("muted", True)
            effects_layout.addWidget(effects_title)
            effects_tag = self._build_effects_tag(base_name, config_key, is_enhance)
            tag_label = QLabel(effects_tag if effects_tag else "未配置")
            tag_label.setObjectName("EffectSummary")
            tag_label.setProperty("configured", bool(effects_tag))
            tag_label.setProperty("muted", not bool(effects_tag))
            if effects_tag:
                tag_label.setProperty("status", "success")
            tag_label.setMinimumWidth(104)
            tag_label.setMaximumWidth(148)
            tag_label.setWordWrap(True)
            effects_layout.addWidget(tag_label)

            effects_btn = QPushButton("编辑特殊效果")
            effects_btn.setObjectName("SecondaryButton")
            effects_btn.setMinimumWidth(104)
            effects_btn.clicked.connect(
                lambda _=False,
                b=base_name,
                k=config_key,
                d=display_name,
                enh=is_enhance: self.open_effects_editor(b, k, d, enh)
            )
            effects_layout.addWidget(effects_btn)
            effects_layout.addStretch()
            row_layout.addLayout(effects_layout)

            self.card_widgets.append(
                {
                    "card_name": base_name,
                    "config_key": config_key,
                    "is_enhance": is_enhance,
                    "play_priority_pre": play_priority_pre_input,
                    "play_priority_post": play_priority_post_input,
                    "force_keep": force_keep_checkbox,
                    "evolve_priority": evolve_priority_input,
                    "evolve_priority_view": evolve_priority_view if is_enhance else None,
                }
            )

            self.scroll_layout.addWidget(card_row)

        self.scroll_layout.addStretch()

    def refresh_card_priority(self):
        # 重新加载配置文件
        self.config_data = self.load_config()
        # 重新加载卡牌优先级设置
        self.load_card_priority_settings()

    def save_config(self):
        try:
            if getattr(self.parent_widget, "is_script_running", lambda: False)():
                QMessageBox.warning(
                    self,
                    "运行中",
                    "脚本运行中，禁止修改卡牌设置/配置。请先停止脚本后再保存。",
                )
                return
        except Exception:
            pass

        # 仅保存卡牌优先级部分，合并磁盘上的其余配置
        high_priority_cards = {}
        evolve_priority_cards = {}
        for card in self.card_widgets:
            base_name = card.get("card_name", "")
            config_key = normalize_config_key(str(card.get("config_key") or base_name))
            is_enhance = bool(card.get("is_enhance"))

            name_for_msg = str(config_key or base_name)
            if is_enhance:
                try:
                    _b, _c = str(config_key).rsplit("@", 1)
                    if str(_b) == str(base_name):
                        name_for_msg = f"{base_name}(爆能{_c})"
                except Exception:
                    name_for_msg = str(config_key or base_name)

            play_pre_text = card["play_priority_pre"].text().strip()
            play_post_text = card["play_priority_post"].text().strip()
            if play_pre_text or play_post_text:
                pre_val = None
                post_val = None
                if play_pre_text:
                    try:
                        pre_val = int(play_pre_text)
                        if pre_val < 0 or pre_val > 999:
                            raise ValueError("优先级必须在0-999之间")
                    except Exception as e:
                        QMessageBox.warning(
                            self,
                            "输入错误",
                            f"卡片 '{name_for_msg}' 的出牌优先级(进化前)设置错误: {str(e)}",
                        )
                        return
                if play_post_text:
                    try:
                        post_val = int(play_post_text)
                        if post_val < 0 or post_val > 999:
                            raise ValueError("优先级必须在0-999之间")
                    except Exception as e:
                        QMessageBox.warning(
                            self,
                            "输入错误",
                            f"卡片 '{name_for_msg}' 的出牌优先级(进化后)设置错误: {str(e)}",
                        )
                        return

                # 只填了一个阶段时，默认另一阶段同值，避免出现999导致策略异常
                if pre_val is None and post_val is not None:
                    pre_val = post_val
                if post_val is None and pre_val is not None:
                    post_val = pre_val

                high_priority_cards[config_key] = {
                    "priority_pre_evolution": pre_val,
                    "priority_post_evolution": post_val,
                }

            # 强制留牌（仅基础卡）
            try:
                force_keep_widget = card.get("force_keep")
                force_keep_checked = bool(
                    force_keep_widget is not None and force_keep_widget.isChecked()
                )
            except Exception:
                force_keep_checked = False

            if force_keep_checked:
                base_cfg = high_priority_cards.get(base_name)
                if not isinstance(base_cfg, dict):
                    base_cfg = {}
                    high_priority_cards[base_name] = base_cfg
                base_cfg["force_keep"] = True

            # 进化优先级（仅基础卡；爆能档位不单独配置）
            evolve_widget = card.get("evolve_priority")
            if evolve_widget is not None:
                evolve_priority_text = evolve_widget.text().strip()
                if evolve_priority_text:
                    try:
                        priority = int(evolve_priority_text)
                        if priority < 0 or priority > 999:
                            raise ValueError("优先级必须在0-999之间")
                        evolve_priority_cards[base_name] = {"priority": priority}
                    except Exception as e:
                        QMessageBox.warning(
                            self,
                            "输入错误",
                            f"卡片 '{base_name}' 的进化优先级设置错误: {str(e)}",
                        )
                        return

        config_path = get_config_path()
        repo = ConfigRepository(config_path)
        existing, parse_ok, parse_err = repo.load_existing(allow_default_on_error=False)
        if existing is None:
            QMessageBox.warning(
                self,
                "保存失败",
                f"config.json解析失败，已拒绝覆盖写入: {str(parse_err or '')}",
            )
            return

        if high_priority_cards:
            existing["high_priority_cards"] = high_priority_cards
        elif "high_priority_cards" in existing:
            del existing["high_priority_cards"]

        if evolve_priority_cards:
            existing["evolve_priority_cards"] = evolve_priority_cards
        elif "evolve_priority_cards" in existing:
            del existing["evolve_priority_cards"]

        try:
            res = repo.replace_with_snapshot(existing, indent=4, ensure_ascii=False)
            if not res.ok:
                raise RuntimeError(res.error or "config write failed")
            QMessageBox.information(self, "成功", "卡牌设置已保存！")
            try:
                log_output = getattr(self.parent_widget, "log_output", None)
                if log_output is not None and hasattr(log_output, "append"):
                    log_output.append("[配置] 卡牌设置已更新")
            except Exception:
                pass

            # 保存到当前卡组文件
            self._save_to_current_deck(high_priority_cards, evolve_priority_cards)

        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"保存卡牌设置失败: {str(e)}")

    def _save_to_current_deck(
        self, high_priority_cards: dict, evolve_priority_cards: dict
    ) -> None:
        """将优先级设置保存到当前卡组文件"""
        if not high_priority_cards and not evolve_priority_cards:
            return

        try:
            # 获取主窗口
            parent = self.parent_widget

            while parent:
                if hasattr(parent, "card_select_page"):
                    card_select_page = parent.card_select_page
                    break
                parent = getattr(parent, "parent_widget", None) or getattr(
                    parent, "parent", None
                )
            else:
                return

        # 通过新的 ``current_deck_file`` 属性获取当前卡组文件。
            deck_file = getattr(card_select_page, "current_deck_file", None)
            if not deck_file:
                return

            # 读取卡组文件
            decks_dir = os.path.join(os.path.dirname(get_config_path()), "saved_decks")
            deck_path = os.path.join(decks_dir, deck_file)

            if not os.path.exists(deck_path):
                return

            with open(deck_path, "r", encoding="utf-8") as f:
                deck_data = json.load(f)

        # 更新 ``strategy_config``。
            sc = deck_data.get("strategy_config")
            if not isinstance(sc, dict):
                sc = {}
                deck_data["strategy_config"] = sc

            # 保存高优先级卡牌
            if high_priority_cards:
                sc["high_priority_cards"] = high_priority_cards
            elif "high_priority_cards" in sc:
                del sc["high_priority_cards"]

            # 保存进化优先级
            if evolve_priority_cards:
                sc["evolve_priority_cards"] = evolve_priority_cards
            elif "evolve_priority_cards" in sc:
                del sc["evolve_priority_cards"]

            # 写回卡组文件
            with open(deck_path, "w", encoding="utf-8") as f:
                json.dump(deck_data, f, ensure_ascii=False, indent=2)

        except Exception:
            pass

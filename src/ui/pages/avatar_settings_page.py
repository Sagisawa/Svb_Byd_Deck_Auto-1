#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面分身配置页面。

允许用户自定义在开启桌面分身（Windows Child Session）后自动启动的目标程序列表。
通过 Windows 任务计划程序 COM 接口直接跨会话注入，拥有最高管理员权限并绕过 UAC 拦截。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.device.child_session.manager import get_child_session_manager


class ProgramEditDialog(QDialog):
    """添加或编辑自启动程序的弹窗。"""

    def __init__(self, parent: Optional[QWidget] = None, data: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("编辑自启动程序" if data else "添加自启动程序")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(10)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("例如：影之诗 / MuMu 模拟器 / 自动化脚本")

        path_layout = QHBoxLayout()
        path_layout.setSpacing(8)
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("选择或输入可执行文件 (.exe, .bat, .cmd) 完整路径")
        self.browse_btn = QPushButton("浏览...")
        self.browse_btn.clicked.connect(self._browse_file)
        path_layout.addWidget(self.path_input, 1)
        path_layout.addWidget(self.browse_btn)

        self.args_input = QLineEdit()
        self.args_input.setPlaceholderText("可选启动参数，例如：-fullscreen")

        workdir_layout = QHBoxLayout()
        workdir_layout.setSpacing(8)
        self.workdir_input = QLineEdit()
        self.workdir_input.setPlaceholderText("留空时默认取程序所在目录")
        self.workdir_browse_btn = QPushButton("浏览...")
        self.workdir_browse_btn.clicked.connect(self._browse_workdir)
        workdir_layout.addWidget(self.workdir_input, 1)
        workdir_layout.addWidget(self.workdir_browse_btn)

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 300)
        self.delay_spin.setValue(0)
        self.delay_spin.setSuffix(" 秒")

        self.enabled_chk = QCheckBox("启用此程序")
        self.enabled_chk.setChecked(True)

        form.addRow("程序名称:", self.name_input)
        form.addRow("程序路径 (*):", path_layout)
        form.addRow("启动参数:", self.args_input)
        form.addRow("工作目录:", workdir_layout)
        form.addRow("启动延迟:", self.delay_spin)
        form.addRow("启用状态:", self.enabled_chk)

        layout.addLayout(form)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确定")
        ok_btn.setObjectName("PrimaryButton")
        ok_btn.clicked.connect(self._validate_and_accept)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        if data:
            self.name_input.setText(str(data.get("name", "")))
            self.path_input.setText(str(data.get("path", "")))
            self.args_input.setText(str(data.get("args", "")))
            self.workdir_input.setText(str(data.get("working_dir", "")))
            self.delay_spin.setValue(int(data.get("delay_seconds", 0)))
            self.enabled_chk.setChecked(bool(data.get("enabled", True)))

    def _browse_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择在分身中运行的可执行程序",
            self.path_input.text().strip() or os.getcwd(),
            "可执行程序 (*.exe *.bat *.cmd);;所有文件 (*.*)",
        )
        if file_path:
            norm = os.path.normpath(file_path)
            self.path_input.setText(norm)
            if not self.name_input.text().strip():
                base_name = os.path.splitext(os.path.basename(norm))[0]
                self.name_input.setText(base_name)
            if not self.workdir_input.text().strip():
                self.workdir_input.setText(os.path.dirname(norm))

    def _browse_workdir(self):
        dir_path = QFileDialog.getExistingDirectory(
            self,
            "选择程序工作目录",
            self.workdir_input.text().strip() or os.getcwd(),
        )
        if dir_path:
            self.workdir_input.setText(os.path.normpath(dir_path))

    def _validate_and_accept(self):
        path = self.path_input.text().strip()
        if not path:
            QMessageBox.warning(self, "输入错误", "请指定程序文件路径。")
            return
        if not os.path.isfile(path):
            reply = QMessageBox.question(
                self,
                "文件不存在",
                f"指定的文件当前不存在：\n{path}\n\n是否仍然保存？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        if not self.name_input.text().strip():
            self.name_input.setText(os.path.splitext(os.path.basename(path))[0])

        self.accept()

    def get_data(self) -> Dict[str, Any]:
        return {
            "name": self.name_input.text().strip(),
            "path": self.path_input.text().strip(),
            "args": self.args_input.text().strip(),
            "working_dir": self.workdir_input.text().strip(),
            "delay_seconds": self.delay_spin.value(),
            "enabled": self.enabled_chk.isChecked(),
        }


class AvatarSettingsPage(QWidget):
    """桌面分身自启动与行为设置主页面。"""

    config_saved = pyqtSignal(dict)
    avatar_requested = pyqtSignal()
    log_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("AvatarSettingsPage")
        self.setProperty("pageRoot", True)

        self._init_ui()
        self.load_config()

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(24, 22, 24, 20)
        main_layout.setSpacing(16)

        # 页面标题
        title_label = QLabel("桌面分身设置")
        title_label.setObjectName("PageTitle")
        title_label.setProperty("heading", "page")
        main_layout.addWidget(title_label)

        subtitle_label = QLabel(
            "管理桌面分身（Windows Child Session）的启动行为与自启动程序清单。\n"
            "所有程序均通过系统级任务计划程序直接穿透注入到分身桌面，赋予最高管理员权限并彻底绕过 UAC 弹窗拦截。"
        )
        subtitle_label.setObjectName("PageSubtitle")
        subtitle_label.setProperty("muted", True)
        subtitle_label.setWordWrap(True)
        main_layout.addWidget(subtitle_label)

        # 滚动容器
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)

        content = QWidget()
        content.setProperty("pageRoot", True)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 8, 0)
        content_layout.setSpacing(16)

        # 1. 全局配置卡片
        global_panel = QFrame()
        global_panel.setObjectName("SurfacePanel")
        global_layout = QVBoxLayout(global_panel)
        global_layout.setContentsMargins(18, 16, 18, 16)
        global_layout.setSpacing(12)

        global_title = QLabel("基础运行选项")
        global_title.setObjectName("SectionTitle")
        global_layout.addWidget(global_title)

        self.auto_launch_checkbox = QCheckBox("开启分身后自动启动下方已勾选的程序")
        self.auto_launch_checkbox.setObjectName("AutoLaunchCheckBox")
        global_layout.addWidget(self.auto_launch_checkbox)

        row_layout = QHBoxLayout()
        row_layout.setSpacing(20)

        # 初始等待延迟
        delay_layout = QHBoxLayout()
        delay_label = QLabel("分身就绪后初始等待延迟:")
        self.launch_delay_spin = QSpinBox()
        self.launch_delay_spin.setRange(0, 30)
        self.launch_delay_spin.setValue(2)
        self.launch_delay_spin.setSuffix(" 秒")
        self.launch_delay_spin.setToolTip("等待分身内部 Windows 桌面与基础服务完全就绪后再启动第一个程序")
        delay_layout.addWidget(delay_label)
        delay_layout.addWidget(self.launch_delay_spin)
        row_layout.addLayout(delay_layout)

        # 分辨率偏好
        res_layout = QHBoxLayout()
        res_label = QLabel("默认虚拟桌面分辨率:")
        self.res_combo = QComboBox()

        screen = QApplication.primaryScreen()
        sw = screen.size().width() if screen else 1920
        sh = screen.size().height() if screen else 1080

        self.res_presets = [
            ("1920 × 1080 (1080P 推荐 - 清晰)", "1920x1080"),
            ("2560 × 1440 (2K 超清 - 适合高分屏)", "2560x1440"),
            ("3840 × 2160 (4K 极清)", "3840x2160"),
            ("1600 × 900 (900P)", "1600x900"),
            ("1280 × 720 (720P - 低分辨率)", "1280x720"),
            (f"跟随屏幕原生 ({sw} × {sh})", f"{sw}x{sh}"),
        ]
        for label, val in self.res_presets:
            self.res_combo.addItem(label, val)

        res_layout.addWidget(res_label)
        res_layout.addWidget(self.res_combo, 1)
        row_layout.addLayout(res_layout)
        global_layout.addLayout(row_layout)
        content_layout.addWidget(global_panel)

        # 2. 程序管理表格卡片
        progs_panel = QFrame()
        progs_panel.setObjectName("SurfacePanel")
        progs_layout = QVBoxLayout(progs_panel)
        progs_layout.setContentsMargins(18, 16, 18, 16)
        progs_layout.setSpacing(12)

        header_row = QHBoxLayout()
        progs_title = QLabel("分身自启动程序列表")
        progs_title.setObjectName("SectionTitle")
        header_row.addWidget(progs_title)

        progs_hint = QLabel("（按表格顺序依次启动；已勾选的项将在每次打开桌面分身后自动执行）")
        progs_hint.setObjectName("SubtleText")
        header_row.addWidget(progs_hint)
        header_row.addStretch()
        progs_layout.addLayout(header_row)

        # 表格
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["启用", "程序名称", "可执行文件路径", "启动参数", "延迟"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(240)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.itemDoubleClicked.connect(self._on_item_double_clicked)
        progs_layout.addWidget(self.table)

        # 操作按钮工具栏
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setSpacing(8)

        self.add_btn = QPushButton("添加程序 (+)")
        self.add_btn.setObjectName("SecondaryButton")
        self.add_btn.clicked.connect(self._add_program)
        toolbar_layout.addWidget(self.add_btn)

        self.edit_btn = QPushButton("编辑")
        self.edit_btn.setObjectName("SecondaryButton")
        self.edit_btn.clicked.connect(self._edit_selected_program)
        toolbar_layout.addWidget(self.edit_btn)

        self.del_btn = QPushButton("删除 (-)")
        self.del_btn.setObjectName("SecondaryButton")
        self.del_btn.clicked.connect(self._delete_selected_program)
        toolbar_layout.addWidget(self.del_btn)

        self.up_btn = QPushButton("上移 (▲)")
        self.up_btn.setObjectName("SecondaryButton")
        self.up_btn.clicked.connect(self._move_up_selected)
        toolbar_layout.addWidget(self.up_btn)

        self.down_btn = QPushButton("下移 (▼)")
        self.down_btn.setObjectName("SecondaryButton")
        self.down_btn.clicked.connect(self._move_down_selected)
        toolbar_layout.addWidget(self.down_btn)

        toolbar_layout.addSpacing(16)

        self.test_run_btn = QPushButton("在分身中测试运行 (▶)")
        self.test_run_btn.setObjectName("SecondaryButton")
        self.test_run_btn.setToolTip("如果当前桌面分身正在运行，立即向分身内注入启动选中的程序（管理员权限）")
        self.test_run_btn.clicked.connect(self._test_run_selected)
        toolbar_layout.addWidget(self.test_run_btn)

        toolbar_layout.addStretch()
        progs_layout.addLayout(toolbar_layout)

        content_layout.addWidget(progs_panel)
        scroll.setWidget(content)
        main_layout.addWidget(scroll, 1)

        # 底部操作栏
        bottom_bar = QFrame()
        bottom_bar.setObjectName("SettingsBottomBar")
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(0, 8, 0, 0)
        bottom_layout.setSpacing(12)

        self.status_label = QLabel("● 配置就绪")
        self.status_label.setObjectName("SubtleText")
        bottom_layout.addWidget(self.status_label)
        bottom_layout.addStretch()

        self.open_avatar_btn = QPushButton("启动桌面分身")
        self.open_avatar_btn.setObjectName("AvatarButton")
        self.open_avatar_btn.clicked.connect(self.avatar_requested.emit)
        bottom_layout.addWidget(self.open_avatar_btn)

        self.save_btn = QPushButton("保存设置")
        self.save_btn.setObjectName("PrimaryButton")
        self.save_btn.clicked.connect(self.save_config)
        bottom_layout.addWidget(self.save_btn)

        main_layout.addWidget(bottom_bar)

    def load_config(self):
        """读取持久化设置并刷新 UI。"""
        mgr = get_child_session_manager()
        cfg = mgr.get_child_session_config()

        self.auto_launch_checkbox.setChecked(bool(cfg.get("auto_launch_enabled", True)))
        self.launch_delay_spin.setValue(int(cfg.get("launch_delay_seconds", 2)))

        saved_res = str(cfg.get("resolution", "1920x1080"))
        idx = 0
        for i in range(self.res_combo.count()):
            if self.res_combo.itemData(i) == saved_res:
                idx = i
                break
        self.res_combo.setCurrentIndex(idx)

        progs = cfg.get("auto_launch_programs", [])
        self._populate_table(progs)
        self.status_label.setText("● 配置已加载")

    def _populate_table(self, programs: List[Dict[str, Any]]):
        self.table.setRowCount(0)
        for row, prog in enumerate(programs):
            if not isinstance(prog, dict):
                continue
            self.table.insertRow(row)

            # 启用复选框
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk_item.setCheckState(Qt.Checked if prog.get("enabled", True) else Qt.Unchecked)
            chk_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 0, chk_item)

            name_item = QTableWidgetItem(str(prog.get("name", "")))
            name_item.setData(Qt.UserRole, prog)  # 保存完整字典
            self.table.setItem(row, 1, name_item)

            path_item = QTableWidgetItem(str(prog.get("path", "")))
            path_item.setToolTip(str(prog.get("path", "")))
            self.table.setItem(row, 2, path_item)

            args_item = QTableWidgetItem(str(prog.get("args", "")))
            self.table.setItem(row, 3, args_item)

            delay_sec = int(prog.get("delay_seconds", 0))
            delay_item = QTableWidgetItem(f"{delay_sec}s" if delay_sec > 0 else "-")
            delay_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 4, delay_item)

    def _get_programs_from_table(self) -> List[Dict[str, Any]]:
        progs = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 1)
            chk_item = self.table.item(row, 0)
            data = dict(name_item.data(Qt.UserRole) or {})
            data["enabled"] = chk_item.checkState() == Qt.Checked
            data["name"] = name_item.text().strip()
            data["path"] = self.table.item(row, 2).text().strip()
            data["args"] = self.table.item(row, 3).text().strip()
            progs.append(data)
        return progs

    def _add_program(self):
        dlg = ProgramEditDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            new_data = dlg.get_data()
            current_list = self._get_programs_from_table()
            current_list.append(new_data)
            self._populate_table(current_list)
            self.status_label.setText("● 已添加程序，请点击「保存设置」保存")

    def _edit_selected_program(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先在列表中选中要编辑的程序行。")
            return
        data = self._get_programs_from_table()[row]
        dlg = ProgramEditDialog(self, data)
        if dlg.exec_() == QDialog.Accepted:
            updated = dlg.get_data()
            current_list = self._get_programs_from_table()
            current_list[row] = updated
            self._populate_table(current_list)
            self.table.selectRow(row)
            self.status_label.setText("● 已修改程序，请点击「保存设置」保存")

    def _on_item_double_clicked(self, item: QTableWidgetItem):
        self._edit_selected_program()

    def _delete_selected_program(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先在列表中选中要删除的程序行。")
            return
        current_list = self._get_programs_from_table()
        del current_list[row]
        self._populate_table(current_list)
        self.status_label.setText("● 已删除程序，请点击「保存设置」保存")

    def _move_up_selected(self):
        row = self.table.currentRow()
        if row <= 0:
            return
        current_list = self._get_programs_from_table()
        current_list[row - 1], current_list[row] = current_list[row], current_list[row - 1]
        self._populate_table(current_list)
        self.table.selectRow(row - 1)
        self.status_label.setText("● 顺序已调整，请点击「保存设置」保存")

    def _move_down_selected(self):
        row = self.table.currentRow()
        current_list = self._get_programs_from_table()
        if row < 0 or row >= len(current_list) - 1:
            return
        current_list[row + 1], current_list[row] = current_list[row], current_list[row + 1]
        self._populate_table(current_list)
        self.table.selectRow(row + 1)
        self.status_label.setText("● 顺序已调整，请点击「保存设置」保存")

    def _test_run_selected(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先在列表中选择一个要测试运行的程序。")
            return

        mgr = get_child_session_manager()
        sid = mgr.get_active_child_session_id()
        if sid is None:
            QMessageBox.warning(
                self,
                "分身未运行",
                "当前系统未检测到正在运行的桌面分身 (Child Session)。\n\n"
                "请先点击右下方的「启动桌面分身」，在分身就绪后再进行测试注入运行。",
            )
            return

        item_data = self._get_programs_from_table()[row]
        exe_path = item_data.get("path", "").strip()
        args = item_data.get("args", "").strip()
        workdir = item_data.get("working_dir", "").strip()
        name = item_data.get("name", "") or os.path.basename(exe_path)

        if not os.path.isfile(exe_path):
            QMessageBox.warning(self, "文件不存在", f"程序文件未找到：\n{exe_path}")
            return

        self.status_label.setText(f"正在向分身注入启动 {name}...")
        QApplication.processEvents()

        ok = mgr.launch_program_in_child_session(exe_path, args, workdir)
        if ok:
            self.status_label.setText(f"● 已成功在分身中启动 {name}")
            QMessageBox.information(
                self,
                "启动成功",
                f"已通过任务计划程序以最高管理员权限在桌面分身中启动：\n{name}\n\n请切到桌面分身窗口查看运行效果。",
            )
        else:
            self.status_label.setText(f"● 在分身中启动 {name} 失败")
            QMessageBox.critical(
                self,
                "启动失败",
                f"向桌面分身注入启动 {name} 失败，请检查程序路径或查看日志。",
            )

    def save_config(self):
        """保存分身自启动与全局配置。"""
        mgr = get_child_session_manager()
        cfg = {
            "enabled": True,
            "resolution": self.res_combo.currentData(),
            "auto_launch_enabled": self.auto_launch_checkbox.isChecked(),
            "launch_delay_seconds": self.launch_delay_spin.value(),
            "auto_launch_programs": self._get_programs_from_table(),
        }

        ok = mgr.save_child_session_config(cfg)
        if ok:
            self.status_label.setText("● 设置已成功保存")
            self.config_saved.emit(cfg)
            QMessageBox.information(self, "保存成功", "桌面分身设置已成功保存到 config.json！")
        else:
            self.status_label.setText("● 保存设置失败")
            QMessageBox.critical(self, "保存失败", "保存配置时发生错误，请查看日志。")

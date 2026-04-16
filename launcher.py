import os
import json
from subprocess import call, Popen, PIPE, STDOUT
from sys import argv, exit

from PyQt5.QtCore import QThread, pyqtSignal, QSize, Qt
from PyQt5.QtGui import QPixmap, QIcon
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QComboBox, QProgressBar,
    QPushButton, QApplication, QMainWindow, QHBoxLayout,
    QInputDialog, QMessageBox, QDialog, QFormLayout,
    QLineEdit, QSpinBox, QCheckBox, QGroupBox, QTabWidget,
    QSystemTrayIcon, QMenu, QAction, QTextBrowser, QListWidget, QListWidgetItem
)
import requests
try:
    import markdown as md  # type: ignore
except Exception:
    md = None
import re
import html as htmllib

def markdown_to_html_simple(source: str) -> str:
    # Escape base HTML
    text = htmllib.escape(source)
    # Preserve newlines
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # Code blocks ```...```
    def repl_codeblock(match):
        code = match.group(1)
        return f"</p><pre><code>{code}</code></pre><p>"
    text = re.sub(r"```[\s\S]*?```", lambda m: repl_codeblock(re.match(r"```[\s\S]*?\n?([\s\S]*?)\n?```", m.group(0)) or m), text)

    # Headings
    text = re.sub(r"^######\s+(.*)$", r"<h6>\1</h6>", text, flags=re.MULTILINE)
    text = re.sub(r"^#####\s+(.*)$", r"<h5>\1</h5>", text, flags=re.MULTILINE)
    text = re.sub(r"^####\s+(.*)$", r"<h4>\1</h4>", text, flags=re.MULTILINE)
    text = re.sub(r"^###\s+(.*)$", r"<h3>\1</h3>", text, flags=re.MULTILINE)
    text = re.sub(r"^##\s+(.*)$", r"<h2>\1</h2>", text, flags=re.MULTILINE)
    text = re.sub(r"^#\s+(.*)$", r"<h1>\1</h1>", text, flags=re.MULTILINE)

    # Lists: group consecutive -/* lines into <ul>
    lines = text.split('\n')
    out = []
    in_ul = False
    for line in lines:
        if re.match(r"^\s*[-*+]\s+", line):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            item = re.sub(r"^\s*[-*+]\s+", "", line)
            out.append(f"<li>{item}</li>")
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(line)
    if in_ul:
        out.append("</ul>")
    text = "\n".join(out)

    # Bold/italic/inline code
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    # Links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", r"<a href='\2'>\1</a>", text)

    # Paragraphs: wrap remaining non-tag lines
    def wrap_paragraphs(s: str) -> str:
        result = []
        for block in s.split('\n\n'):
            if re.search(r"^\s*<", block):
                result.append(block)
            else:
                if block.strip():
                    result.append(f"<p>{block}</p>")
        return "\n".join(result)
    text = wrap_paragraphs(text)

    style = (
        '<style>body{font-family:Segoe UI,Arial; font-size:12px;}'
        'h1,h2,h3{margin:6px 0;}'
        'ul{margin:6px 0 6px 18px;}'
        'code, pre{background:#2a2a2a; color:#ddd;}'
        'code{padding:2px 4px;}'
        'pre{padding:8px; overflow:auto;}'
        'a{color:#4a9eff;}</style>'
    )
    return f"<html><head><meta charset='utf-8'>{style}</head><body>{text}</body></html>"

from minecraft_launcher_lib.utils import get_minecraft_directory, get_version_list
from minecraft_launcher_lib.install import install_minecraft_version
from minecraft_launcher_lib.command import get_minecraft_command
try:
    from minecraft_launcher_lib.fabric import install_fabric as mll_install_fabric  # type: ignore
except Exception:
    mll_install_fabric = None

try:
    from minecraft_launcher_lib.quilt import install_quilt as mll_install_quilt  # type: ignore
except Exception:
    mll_install_quilt = None
 

# Путь установки Minecraft для MjnLauncher
minecraft_directory = get_minecraft_directory().replace('minecraft', 'mjnlauncher')


class LaunchThread(QThread):
    launch_setup_signal = pyqtSignal(str, str)
    progress_update_signal = pyqtSignal(int, int, str)
    state_update_signal = pyqtSignal(bool)
    message_signal = pyqtSignal(str, str)
    console_output_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.launch_setup_signal.connect(self.launch_setup)
        self.version_id = ''
        self.username = ''
        self.progress = 0
        self.progress_max = 0
        self.progress_label = ''

    def launch_setup(self, version_id, username):
        self.version_id = version_id
        self.username = username

    def update_progress_label(self, value):
        self.progress_label = value
        self.progress_update_signal.emit(self.progress, self.progress_max, self.progress_label)

    def update_progress(self, value):
        self.progress = value
        self.progress_update_signal.emit(self.progress, self.progress_max, self.progress_label)

    def update_progress_max(self, value):
        self.progress_max = value
        self.progress_update_signal.emit(self.progress, self.progress_max, self.progress_label)

    def run(self):
        self.state_update_signal.emit(True)

        def is_version_installed(version_id: str) -> bool:
            version_dir = os.path.join(minecraft_directory, 'versions', version_id)
            version_json = os.path.join(version_dir, f"{version_id}.json")
            version_jar = os.path.join(version_dir, f"{version_id}.jar")
            return os.path.isdir(version_dir) and os.path.isfile(version_json)

        def find_installed_mod_version(base_version: str, loader: str) -> str:
            versions_dir = os.path.join(minecraft_directory, 'versions')
            try:
                candidates = []
                for entry in os.listdir(versions_dir):
                    if not os.path.isdir(os.path.join(versions_dir, entry)):
                        continue
                    name_lower = entry.lower()
                    if base_version in entry and loader in name_lower:
                        # проверяем наличие json
                        if os.path.isfile(os.path.join(versions_dir, entry, f"{entry}.json")):
                            candidates.append(entry)
                if candidates:
                    # Приоритет: fabric-loader-*, quilt-loader-*, затем алиасы
                    def candidate_key(e: str) -> tuple:
                        if e.startswith('fabric-loader-'):
                            return (0, e)
                        elif e.startswith('quilt-loader-'):
                            return (1, e)
                        else:
                            return (2, e)
                    candidates.sort(key=candidate_key)
                    return candidates[0]
            except Exception:
                pass
            return ''

        def install_modded_if_needed(base_version: str, loader: str) -> str:
            # Возвращает реальный id установленной модифицированной версии (или пусто при неуспехе)
            try:
                # Установка базовой версии перед модлоадером
                if not is_version_installed(base_version):
                    install_minecraft_version(
                        versionid=base_version,
                        minecraft_directory=minecraft_directory,
                        callback={
                            'setStatus': self.update_progress_label,
                            'setProgress': self.update_progress,
                            'setMax': self.update_progress_max
                        }
                    )
                if loader == 'fabric' and mll_install_fabric is not None:
                    mll_install_fabric(
                        minecraft_version=base_version,
                        minecraft_directory=minecraft_directory,
                        callback={
                            'setStatus': self.update_progress_label,
                            'setProgress': self.update_progress,
                            'setMax': self.update_progress_max
                        }
                    )
                elif loader == 'quilt' and mll_install_quilt is not None:
                    mll_install_quilt(
                        minecraft_version=base_version,
                        minecraft_directory=minecraft_directory,
                        callback={
                            'setStatus': self.update_progress_label,
                            'setProgress': self.update_progress,
                            'setMax': self.update_progress_max
                        }
                    )
                else:
                    pass
            except Exception:
                # Игнорируем, попробуем найти локально установленную
                pass
            return find_installed_mod_version(base_version, loader)

        # Поддержка синтаксиса отображаемых версий: "<base> <loader>" или просто "<base>"
        version_to_launch = self.version_id
        game_dir_override = None
        loader = ''
        if ' ' in self.version_id:
            parts = self.version_id.split()
            if len(parts) >= 2 and parts[-1].lower() in {'fabric', 'quilt'}:
                loader = parts[-1].lower()
                base_version = ' '.join(parts[:-1])
                # Пытаемся найти уже установленную мод-версию
                version_to_launch = find_installed_mod_version(base_version, loader)
                if not version_to_launch:
                    # Пытаемся установить мод-версию (если есть интернет/установщик)
                    version_to_launch = install_modded_if_needed(base_version, loader)
                if not version_to_launch:
                    self.message_signal.emit('Ошибка установки', f'Не удалось установить {loader} для {base_version}. Проверьте интернет или совместимость версии.')
                    self.state_update_signal.emit(False)
                    return
                # Создаём алиас-версию с читаемым названием, например "1.21.8 fabric" или "1.21.8 quilt"
                if 'fabric' in version_to_launch.lower() or 'quilt' in version_to_launch.lower():
                    alias_id = f"{base_version} {loader}"
                    try:
                        alias_dir = os.path.join(minecraft_directory, 'versions', alias_id)
                        alias_json_path = os.path.join(alias_dir, f"{alias_id}.json")
                        if not os.path.isfile(alias_json_path):
                            os.makedirs(alias_dir, exist_ok=True)
                            alias_data = {
                                'id': alias_id,
                                'inheritsFrom': version_to_launch,
                                'type': 'release'
                            }
                            with open(alias_json_path, 'w', encoding='utf-8') as f:
                                json.dump(alias_data, f, indent=4, ensure_ascii=False)
                        # Алиас нужен для отображения в списке; запуск оставим на реальный fabric id
                    except Exception:
                        pass
                # Для модовой версии используем отдельную папку профиля
                game_dir_override = os.path.join(minecraft_directory, 'profiles', f"{base_version}-{loader}")
                try:
                    os.makedirs(game_dir_override, exist_ok=True)
                    for sub in ['mods', 'config', 'resourcepacks', 'saves']:
                        os.makedirs(os.path.join(game_dir_override, sub), exist_ok=True)
                except Exception:
                    pass
        else:
            # Обычная ванильная версия
            try:
                if not is_version_installed(version_to_launch):
                    install_minecraft_version(
                        versionid=version_to_launch,
                        minecraft_directory=minecraft_directory,
                        callback={
                            'setStatus': self.update_progress_label,
                            'setProgress': self.update_progress,
                            'setMax': self.update_progress_max
                        }
                    )
            except Exception:
                self.message_signal.emit(
                    'Оффлайн режим',
                    'Эта версия не установлена локально и не может быть скачана без интернета.'
                )
                self.state_update_signal.emit(False)
                return

        # Запуск игры с ником (offline-режим)
        options = {
            'username': self.username,
            'uuid': '',
            'token': ''
        }
        if game_dir_override:
            options['gameDirectory'] = game_dir_override

        cmd = get_minecraft_command(
            version=version_to_launch,
            minecraft_directory=minecraft_directory,
            options=options
        )
        try:
            # Запускаем процесс с перенаправлением вывода в консоль вкладки
            process = Popen(cmd, stdout=PIPE, stderr=STDOUT, universal_newlines=True)
            if process.stdout is not None:
                for line in iter(process.stdout.readline, ''):
                    if line == '':
                        break
                    self.console_output_signal.emit(line.rstrip('\n'))
            process.wait()
        except Exception as e:
            self.console_output_signal.emit(f"[Launcher] Ошибка запуска: {e}")

        self.state_update_signal.emit(False)


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Настройки MJNL')
        self.setModal(True)
        self.resize(500, 400)
        
        # Основной лэйаут
        layout = QVBoxLayout(self)
        
        # Создаем табы
        tab_widget = QTabWidget()
        
        # Таб "Общие"
        general_tab = QWidget()
        general_layout = QFormLayout(general_tab)
        
        # Путь к Java
        self.java_path_edit = QLineEdit()
        self.java_path_edit.setPlaceholderText("Автоопределение")
        general_layout.addRow("Путь к Java:", self.java_path_edit)
        
        # Память для JVM
        self.memory_spinbox = QSpinBox()
        self.memory_spinbox.setRange(512, 8192)
        self.memory_spinbox.setValue(2048)
        self.memory_spinbox.setSuffix(" MB")
        general_layout.addRow("Память JVM:", self.memory_spinbox)
        
        # Автозапуск последней версии
        self.auto_launch_checkbox = QCheckBox()
        general_layout.addRow("Автозапуск последней версии:", self.auto_launch_checkbox)
        
        # Сворачивание в трей
        self.minimize_to_tray_checkbox = QCheckBox()
        general_layout.addRow("Сворачивать в трей:", self.minimize_to_tray_checkbox)
        
        # Таб "Интерфейс"
        interface_tab = QWidget()
        interface_layout = QFormLayout(interface_tab)
        
        # Темная тема
        self.dark_theme_checkbox = QCheckBox()
        interface_layout.addRow("Темная тема:", self.dark_theme_checkbox)
        
        # Размер окна
        self.window_size_combo = QComboBox()
        self.window_size_combo.addItems(["300x200", "400x300", "500x400", "600x500"])
        self.window_size_combo.setCurrentText("300x200")
        interface_layout.addRow("Размер окна:", self.window_size_combo)
        
        # Таб "Модлоадеры"
        modloaders_tab = QWidget()
        modloaders_layout = QFormLayout(modloaders_tab)
        
        # Поддержка Quilt
        self.quilt_support_checkbox = QCheckBox()
        modloaders_layout.addRow("Поддержка Quilt (бета):", self.quilt_support_checkbox)
        
        # Автоустановка модлоадеров
        self.auto_install_modloaders_checkbox = QCheckBox()
        modloaders_layout.addRow("Автоустановка модлоадеров:", self.auto_install_modloaders_checkbox)
        
        # Добавляем табы
        tab_widget.addTab(general_tab, "Общие")
        tab_widget.addTab(interface_tab, "Интерфейс")
        tab_widget.addTab(modloaders_tab, "Модлоадеры")
        
        layout.addWidget(tab_widget)
        
        # Кнопки
        button_layout = QHBoxLayout()
        self.save_button = QPushButton("Сохранить")
        self.cancel_button = QPushButton("Отмена")
        self.save_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)
        
        # Загружаем настройки
        self.load_settings()
        
        # Применяем тему родительского окна
        if parent and hasattr(parent, 'settings'):
            if parent.settings.get('dark_theme', False):
                self.apply_dark_theme()
    
    def load_settings(self):
        """Загружает настройки из конфига"""
        try:
            config_path = os.path.join(
                os.getenv('APPDATA'), '.MjnLauncher', 'client', 'settings.json'
            )
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    
                self.java_path_edit.setText(settings.get('java_path', ''))
                self.memory_spinbox.setValue(settings.get('memory', 2048))
                self.auto_launch_checkbox.setChecked(settings.get('auto_launch', False))
                self.minimize_to_tray_checkbox.setChecked(settings.get('minimize_to_tray', False))
                self.dark_theme_checkbox.setChecked(settings.get('dark_theme', False))
                self.window_size_combo.setCurrentText(settings.get('window_size', '300x200'))
                self.quilt_support_checkbox.setChecked(settings.get('quilt_support', False))
                self.auto_install_modloaders_checkbox.setChecked(settings.get('auto_install_modloaders', True))
        except Exception:
            pass
    
    def save_settings(self):
        """Сохраняет настройки в конфиг"""
        try:
            config_path = os.path.join(
                os.getenv('APPDATA'), '.MjnLauncher', 'client', 'settings.json'
            )
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            
            settings = {
                'java_path': self.java_path_edit.text(),
                'memory': self.memory_spinbox.value(),
                'auto_launch': self.auto_launch_checkbox.isChecked(),
                'minimize_to_tray': self.minimize_to_tray_checkbox.isChecked(),
                'dark_theme': self.dark_theme_checkbox.isChecked(),
                'window_size': self.window_size_combo.currentText(),
                'quilt_support': self.quilt_support_checkbox.isChecked(),
                'auto_install_modloaders': self.auto_install_modloaders_checkbox.isChecked()
            }
            
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4, ensure_ascii=False)
                
            return True
        except Exception:
            return False
    
    def apply_dark_theme(self):
        """Применяет темную тему к окну настроек"""
        dark_style = """
        QDialog {
            background-color: #2b2b2b;
            color: #ffffff;
        }
        QWidget {
            background-color: #2b2b2b;
            color: #ffffff;
        }
        QPushButton {
            background-color: #404040;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 6px;
            color: #ffffff;
        }
        QPushButton:hover {
            background-color: #505050;
        }
        QPushButton:pressed {
            background-color: #353535;
        }
        QComboBox {
            background-color: #404040;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 4px;
            color: #ffffff;
        }
        QComboBox:hover {
            background-color: #505050;
        }
        QComboBox::drop-down {
            border: none;
        }
        QComboBox::down-arrow {
            image: none;
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            border-top: 5px solid #ffffff;
            margin-right: 5px;
        }
        QComboBox QAbstractItemView {
            background-color: #404040;
            border: 1px solid #555555;
            selection-background-color: #505050;
            color: #ffffff;
        }
        QLabel {
            color: #ffffff;
        }
        QLineEdit {
            background-color: #404040;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 4px;
            color: #ffffff;
        }
        QLineEdit:focus {
            border: 1px solid #4a9eff;
        }
        QSpinBox {
            background-color: #404040;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 4px;
            color: #ffffff;
        }
        QSpinBox:focus {
            border: 1px solid #4a9eff;
        }
        QCheckBox {
            color: #ffffff;
        }
        QCheckBox::indicator {
            width: 16px;
            height: 16px;
        }
        QCheckBox::indicator:unchecked {
            background-color: #404040;
            border: 1px solid #555555;
            border-radius: 3px;
        }
        QCheckBox::indicator:checked {
            background-color: #4a9eff;
            border: 1px solid #4a9eff;
            border-radius: 3px;
        }
        QTabWidget::pane {
            border: 1px solid #555555;
            background-color: #2b2b2b;
        }
        QTabBar::tab {
            background-color: #404040;
            border: 1px solid #555555;
            padding: 8px 16px;
            color: #ffffff;
        }
        QTabBar::tab:selected {
            background-color: #505050;
        }
        QTabBar::tab:hover {
            background-color: #4a4a4a;
        }
        QGroupBox {
            color: #ffffff;
            border: 1px solid #555555;
            border-radius: 4px;
            margin-top: 10px;
            padding-top: 10px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px 0 5px;
        }
        """
        self.setStyleSheet(dark_style)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle('MJNL v1.3')
        self.resize(300, 200)
        self.centralwidget = QWidget(self)

        # Путь к файлу с аккаунтами
        self.users_path = os.path.join(
            os.getenv('APPDATA'), '.MjnLauncher', 'client', 'users.json'
        )
        os.makedirs(os.path.dirname(self.users_path), exist_ok=True)

        # Путь к конфигу лаунчера
        self.config_path = os.path.join(
            os.getenv('APPDATA'), '.MjnLauncher', 'client', 'config.json'
        )

        # Верхний баннер
        self.logo = QLabel(self.centralwidget)
        self.logo.setMaximumSize(QSize(256, 37))
        self.logo.setPixmap(QPixmap('assets/title.png'))
        self.logo.setScaledContents(True)

        # ПРАВАЯ БОКОВАЯ ПАНЕЛЬ (аккаунт/настройки)
        self.account_type = QComboBox(self.centralwidget)
        self.add_account_button = QPushButton("+", self.centralwidget)
        self.add_account_button.setFixedWidth(30)
        self.add_account_button.clicked.connect(self.add_account)

        right_panel = QVBoxLayout()
        right_panel.addWidget(QLabel('Аккаунт:', self.centralwidget))
        account_row = QHBoxLayout()
        account_row.addWidget(self.account_type, 4)
        account_row.addWidget(self.add_account_button, 1)
        right_panel.addLayout(account_row)

        self.settings_button = QPushButton('⚙️', self.centralwidget)
        self.settings_button.setFixedWidth(40)
        self.settings_button.setToolTip('Настройки')
        self.settings_button.clicked.connect(self.open_settings)
        right_panel.addWidget(self.settings_button, 0, Qt.AlignRight)

        # Превью скина выбранного аккаунта
        self.skin_preview = QLabel(self.centralwidget)
        self.skin_preview.setMinimumSize(QSize(120, 180))
        self.skin_preview.setMaximumSize(QSize(160, 240))
        self.skin_preview.setScaledContents(True)
        right_panel.addWidget(self.skin_preview, 0, Qt.AlignRight)
        refresh_skin_btn = QPushButton('Обновить скин', self.centralwidget)
        refresh_skin_btn.clicked.connect(self.refresh_skin_preview)
        right_panel.addWidget(refresh_skin_btn, 0, Qt.AlignRight)
        right_panel.addStretch(1)

        # ЦЕНТРАЛЬНЫЕ ВКЛАДКИ (Legacy-стиль)
        self.center_tabs = QTabWidget(self.centralwidget)
        news_tab = QWidget()
        news_layout = QVBoxLayout(news_tab)
        self.news_view = QTextBrowser(news_tab)
        self.news_view.setOpenExternalLinks(True)
        news_controls = QHBoxLayout()
        self.news_refresh_btn = QPushButton('Обновить новости', news_tab)
        self.news_refresh_btn.clicked.connect(self.load_news)
        news_controls.addWidget(self.news_refresh_btn)
        news_layout.addLayout(news_controls)
        news_layout.addWidget(self.news_view)
        self.center_tabs.addTab(news_tab, 'Новости')

        mods_tab = QWidget()
        mods_layout = QVBoxLayout(mods_tab)
        # Поиск модов
        mods_search_row = QHBoxLayout()
        self.mods_search_edit = QLineEdit(mods_tab)
        self.mods_search_edit.setPlaceholderText('Поиск модов Modrinth...')
        self.mods_search_btn = QPushButton('Искать', mods_tab)
        self.mods_search_btn.clicked.connect(self.on_mods_search)
        self.mods_search_edit.returnPressed.connect(self.on_mods_search)
        mods_search_row.addWidget(self.mods_search_edit, 4)
        mods_search_row.addWidget(self.mods_search_btn, 1)
        mods_layout.addLayout(mods_search_row)

        # Фильтры: Loader и MC версия из выбранной версии лаунчера
        mods_filter_row = QHBoxLayout()
        self.mods_loader_combo = QComboBox(mods_tab)
        self.mods_loader_combo.addItems(['auto', 'fabric', 'quilt', 'forge'])
        self.mods_gamever_combo = QComboBox(mods_tab)
        self.mods_gamever_combo.setEditable(True)
        self.mods_gamever_combo.setPlaceholderText('auto')
        mods_filter_row.addWidget(QLabel('Загрузчик:', mods_tab))
        mods_filter_row.addWidget(self.mods_loader_combo)
        mods_filter_row.addWidget(QLabel('MC версия:', mods_tab))
        mods_filter_row.addWidget(self.mods_gamever_combo)
        mods_layout.addLayout(mods_filter_row)

        # Список результатов
        self.mods_results = QListWidget(mods_tab)
        mods_layout.addWidget(self.mods_results)

        # Действия
        mods_actions = QHBoxLayout()
        self.mods_download_btn = QPushButton('Скачать выбранный мод', mods_tab)
        self.mods_download_btn.clicked.connect(self.on_mod_download)
        mods_actions.addStretch(1)
        mods_actions.addWidget(self.mods_download_btn)
        mods_layout.addLayout(mods_actions)

        self.center_tabs.addTab(mods_tab, 'Моды')

        console_tab = QWidget()
        console_layout = QVBoxLayout(console_tab)
        console_controls = QHBoxLayout()
        self.console_clear_btn = QPushButton('Очистить', console_tab)
        self.console_autoscroll = QCheckBox('Автопрокрутка', console_tab)
        self.console_autoscroll.setChecked(True)
        self.console_clear_btn.clicked.connect(lambda: self.console_view.clear())
        console_controls.addWidget(self.console_clear_btn)
        console_controls.addStretch(1)
        console_controls.addWidget(self.console_autoscroll)
        self.console_view = QTextBrowser(console_tab)
        self.console_view.setOpenExternalLinks(True)
        console_layout.addLayout(console_controls)
        console_layout.addWidget(self.console_view)
        self.center_tabs.addTab(console_tab, 'Консоль')

        # Список версий и фильтр (нижняя панель)
        self.version_filter = QComboBox(self.centralwidget)
        self.version_filter.addItems(['Релизы', 'Снапшоты', 'Все'])
        self.version_filter.currentIndexChanged.connect(self.on_version_filter_changed)

        self.refresh_versions_button = QPushButton("🔄 Обновить", self.centralwidget)
        self.refresh_versions_button.clicked.connect(self.refresh_versions)

        bottom_filter_row = QHBoxLayout()
        bottom_filter_row.addWidget(self.version_filter, 2)
        bottom_filter_row.addWidget(self.refresh_versions_button, 1)

        self.version_select = QComboBox(self.centralwidget)
        # Синхронизация выбора версии с фильтрами во вкладке модов
        self.version_select.currentIndexChanged.connect(self.set_mods_filters_from_selected)
        self.all_versions = []
        self.offline_mode = False

        # Прогресс и статус (нижняя панель)
        self.start_progress_label = QLabel(self.centralwidget)
        self.start_progress_label.setVisible(False)
        self.start_progress = QProgressBar(self.centralwidget)
        self.start_progress.setVisible(False)
        self.time_label = QLabel(self.centralwidget)
        self.time_label.setVisible(False)

        # Нижняя панель управления
        self.start_button = QPushButton('Play', self.centralwidget)
        self.start_button.clicked.connect(self.launch_game)

        bottom_controls = QHBoxLayout()
        bottom_controls.addWidget(self.version_select, 5)
        bottom_controls.addWidget(self.start_button, 1)

        # Центральная область: вкладки слева и правая панель
        center_row = QHBoxLayout()
        center_row.addWidget(self.center_tabs, 4)
        right_container = QWidget(self.centralwidget)
        right_container.setLayout(right_panel)
        center_row.addWidget(right_container, 1)

        # Главный лэйаут
        layout = QVBoxLayout(self.centralwidget)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.addWidget(self.logo, alignment=Qt.AlignHCenter)
        layout.addLayout(center_row)
        layout.addLayout(bottom_filter_row)
        layout.addWidget(self.start_progress_label)
        layout.addWidget(self.start_progress)
        layout.addWidget(self.time_label)
        layout.addLayout(bottom_controls)

        self.setCentralWidget(self.centralwidget)

        self.load_accounts()
        self.load_config()
        self.load_settings()
        self.load_versions()
        # Загрузка новостей при старте (без падения при ошибке)
        try:
            self.load_news()
        except Exception:
            pass
        # Первичная загрузка скина
        try:
            self.refresh_skin_preview()
        except Exception:
            pass
        # Инициализируем список версий для фильтра модов
        try:
            self.populate_mods_game_versions()
        except Exception:
            pass

        # Инициализация системного трея
        self.init_system_tray()
        
        # Поток для запуска игры
        self.launch_thread = LaunchThread()
        self.launch_thread.state_update_signal.connect(self.state_update)
        self.launch_thread.progress_update_signal.connect(self.update_progress)
        self.launch_thread.message_signal.connect(self.show_message)
        self.launch_thread.console_output_signal.connect(self.append_console)

        # Обновление превью скина при смене аккаунта
        self.account_type.currentIndexChanged.connect(self.refresh_skin_preview)

    def load_news(self):
        url = 'https://raw.githubusercontent.com/Maybeoff/MojNovyLauncher/main/README.md'
        try:
            headers = {
                'User-Agent': 'MojNovyLauncher/1.2 (news-fetch)'
            }
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            content = resp.text
            if md is not None:
                html = md.markdown(content, extensions=['extra', 'sane_lists'])
                html_wrapped = (
                    '<html><head><meta charset="utf-8">'
                    '<style>body{font-family:Segoe UI,Arial; font-size:12px;}'
                    'h1,h2,h3{margin:6px 0;}'
                    'ul{margin:6px 0 6px 18px;}'
                    'code, pre{background:#2a2a2a; color:#ddd;}'
                    'code{padding:2px 4px;}'
                    'pre{padding:8px; overflow:auto;}'
                    'a{color:#4a9eff;}</style></head><body>'
                    + html + '</body></html>'
                )
                self.news_view.setHtml(html_wrapped)
            else:
                self.news_view.setHtml(markdown_to_html_simple(content))
        except Exception as e:
            self.news_view.setPlainText(f"Не удалось загрузить новости.\n{str(e)}")

    # ===== Modrinth helpers =====
    def get_user_agent(self) -> str:
        return 'MojNovyLauncher/1.2 (mods)'

    def infer_selected_mc_and_loader(self) -> tuple:
        # Возвращает (mc_version, loader) из выбранной версии в лаунчере
        display_text = self.version_select.currentText()
        clean = display_text.replace('✅ ', '').replace('⬇️ ', '').strip()
        parts = clean.split()
        if len(parts) >= 2 and parts[-1].lower() in {'fabric', 'quilt', 'forge'}:
            return (' '.join(parts[:-1]), parts[-1].lower())
        return (clean, 'auto')

    def set_mods_filters_from_selected(self):
        try:
            mc, loader = self.infer_selected_mc_and_loader()
            # Версия MC
            if hasattr(self, 'mods_gamever_combo'):
                if mc and self.mods_gamever_combo.findText(mc) >= 0:
                    self.mods_gamever_combo.setCurrentText(mc)
                elif mc:
                    # Если версии нет в списке – просто установить текст
                    self.mods_gamever_combo.setCurrentText(mc)
            # Лоадер
            if hasattr(self, 'mods_loader_combo'):
                if loader in {'fabric', 'quilt', 'forge'}:
                    idx = self.mods_loader_combo.findText(loader)
                    if idx >= 0:
                        self.mods_loader_combo.setCurrentIndex(idx)
                else:
                    idx = self.mods_loader_combo.findText('auto')
                    if idx >= 0:
                        self.mods_loader_combo.setCurrentIndex(idx)
        except Exception:
            pass

    def populate_mods_game_versions(self):
        # Заполняем выпадающий список версий с учетом фильтра (Релизы/Снапшоты/Все)
        seen = set()
        self.mods_gamever_combo.clear()
        self.mods_gamever_combo.addItem('auto')
        versions = self.all_versions
        try:
            if not getattr(self, 'offline_mode', False):
                mode = self.version_filter.currentText()
                if mode == 'Релизы':
                    allowed_types = {'release'}
                elif mode == 'Снапшоты':
                    allowed_types = {'snapshot'}
                else:
                    allowed_types = None
                if allowed_types is not None:
                    versions = [v for v in versions if v.get('type') in allowed_types]
        except Exception:
            pass
        for v in versions:
            vid = v.get('id') if isinstance(v, dict) else None
            if vid and vid not in seen:
                self.mods_gamever_combo.addItem(vid)
                seen.add(vid)
        # Предзаполним из выбранной версии
        mc, _ = self.infer_selected_mc_and_loader()
        if mc and self.mods_gamever_combo.findText(mc) >= 0:
            self.mods_gamever_combo.setCurrentText(mc)

    def on_mods_search(self):
        query = self.mods_search_edit.text().strip()
        self.mods_results.clear()
        try:
            headers = {
                'User-Agent': self.get_user_agent()
            }
            # Фасеты: project_type=mod, loaders/game_versions если заданы
            facets = [["project_type:mod"]]
            loader = self.mods_loader_combo.currentText()
            if loader and loader != 'auto':
                # Поиск по Modrinth использует loader как категорию
                facets.append([f"categories:{loader}"])
            game_ver = self.mods_gamever_combo.currentText().strip()
            if game_ver and game_ver != 'auto':
                facets.append([f"versions:{game_ver}"])
            params = {
                'query': query if query else '',
                'limit': 20,
                'facets': json.dumps(facets, ensure_ascii=False)
            }
            resp = requests.get('https://api.modrinth.com/v2/search', params=params, headers=headers, timeout=12)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get('hits', [])
            for hit in hits:
                title = hit.get('title') or hit.get('project_id') or 'Untitled'
                desc = hit.get('description') or ''
                pid = hit.get('project_id')
                item = QListWidgetItem(f"{title} — {desc[:80]}")
                item.setData(Qt.UserRole, {'project_id': pid})
                self.mods_results.addItem(item)
            if not hits:
                self.mods_results.addItem(QListWidgetItem('Ничего не найдено'))
        except Exception as e:
            QMessageBox.warning(self, 'Ошибка поиска', f"{type(e).__name__}: {e}")

    def on_mod_download(self):
        item = self.mods_results.currentItem()
        if not item:
            QMessageBox.warning(self, 'Моды', 'Выберите мод из списка.')
            return
        payload = item.data(Qt.UserRole)
        if not isinstance(payload, dict) or 'project_id' not in payload:
            QMessageBox.warning(self, 'Моды', 'Элемент не содержит данных проекта.')
            return
        project_id = payload['project_id']
        # Определяем таргет: выбранная версия/лоадер
        mc_ver, loader = self.infer_selected_mc_and_loader()
        if loader == 'auto':
            loader = self.mods_loader_combo.currentText() or 'fabric'
            if loader == 'auto':
                loader = 'fabric'
        try:
            headers = {'User-Agent': self.get_user_agent()}
            # Получаем список версий проекта и выбираем подходящую по game_versions+loaders
            url = f'https://api.modrinth.com/v2/project/{project_id}/version'
            resp = requests.get(url, headers=headers, timeout=12)
            resp.raise_for_status()
            versions = resp.json() or []
            selected_version = None
            for v in versions:
                gv = v.get('game_versions') or []
                loaders = v.get('loaders') or []
                if (not mc_ver or mc_ver in gv) and (loader in loaders):
                    selected_version = v
                    break
            if not selected_version and versions:
                selected_version = versions[0]
            if not selected_version:
                QMessageBox.warning(self, 'Моды', 'Не удалось подобрать версию мода для этой версии Minecraft.')
                return
            # Скачиваем первый файл из версии
            files = selected_version.get('files') or []
            primary = None
            for f in files:
                if f.get('primary'):
                    primary = f
                    break
            if not primary and files:
                primary = files[0]
            if not primary:
                QMessageBox.warning(self, 'Моды', 'В выбранной версии мода нет файлов для скачивания.')
                return
            url = primary.get('url') or primary.get('downloads', [None])[0]
            if not url:
                QMessageBox.warning(self, 'Моды', 'Не найден URL файла мода.')
                return
            # Путь mods: всегда используем папку профиля выбранной версии
            _, sel_loader = self.infer_selected_mc_and_loader()
            if sel_loader in {'fabric', 'quilt', 'forge'}:
                profile_name = f"{mc_ver}-{sel_loader}"
            else:
                # Для ванильной версии создаем отдельный профиль по названию версии
                profile_name = mc_ver
            game_dir = os.path.join(minecraft_directory, 'profiles', profile_name)
            try:
                os.makedirs(game_dir, exist_ok=True)
                for sub in ['mods', 'config', 'resourcepacks']:
                    os.makedirs(os.path.join(game_dir, sub), exist_ok=True)
            except Exception:
                pass
            mods_dir = os.path.join(game_dir, 'mods')
            os.makedirs(mods_dir, exist_ok=True)
            filename = primary.get('filename') or os.path.basename(url)
            target = os.path.join(mods_dir, filename)
            with requests.get(url, headers=headers, timeout=30, stream=True) as r:
                r.raise_for_status()
                with open(target, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            QMessageBox.information(self, 'Моды', f'Мод скачан: {target}')
        except Exception as e:
            QMessageBox.warning(self, 'Ошибка скачивания', str(e))

    def load_accounts(self):
        self.account_type.clear()
        try:
            with open(self.users_path, 'r', encoding='utf-8') as f:
                users = json.load(f)
                for user in users:
                    self.account_type.addItem(user.get('nickname', 'Unknown'))
        except Exception:
            self.account_type.addItem('Player')

    def load_config(self):
        self._config = {}
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = json.load(f)
        except Exception:
            self._config = {}

        # Восстанавливаем фильтр версий, если сохранён
        saved_filter = self._config.get('version_filter')
        if isinstance(saved_filter, str):
            self.version_filter.blockSignals(True)
            idx = self.version_filter.findText(saved_filter)
            if idx >= 0:
                self.version_filter.setCurrentIndex(idx)
            self.version_filter.blockSignals(False)

        # Сохраняем желаемую версию для установки после загрузки списка
        self._desired_version = self._config.get('selected_version')

    def load_versions(self):
        self.refresh_versions()

    def refresh_versions(self):
        """Обновляет список доступных версий из интернета"""
        # Показываем прогресс загрузки
        self.refresh_versions_button.setEnabled(False)
        self.refresh_versions_button.setText("🔄 Загрузка...")
        
        # Пытаемся получить список доступных версий из сети
        try:
            self.all_versions = get_version_list()
            self.offline_mode = False
            self.refresh_versions_button.setText("🔄 Обновить")
            self.refresh_versions_button.setEnabled(True)
        except Exception as e:
            # Оффлайн-режим: берём только локально установленные версии
            self.all_versions = self.get_installed_versions()
            self.offline_mode = True
            self.refresh_versions_button.setText("🔄 Обновить (офлайн)")
            self.refresh_versions_button.setEnabled(True)
            # Показываем сообщение об ошибке только если это не первая загрузка
            if hasattr(self, '_first_load_done'):
                QMessageBox.warning(self, "Ошибка сети", 
                    f"Не удалось загрузить список версий из интернета.\n"
                    f"Показываются только установленные версии.\n"
                    f"Ошибка: {str(e)}")

        # В оффлайне отключаем фильтр типов, в онлайне включаем
        self.version_filter.setDisabled(self.offline_mode)

        self.apply_version_filter()

        # Восстанавливаем выбранную версию из конфигурации, если она есть в списке
        if getattr(self, '_desired_version', None):
            # Ищем версию по чистому имени (без иконки)
            for i in range(self.version_select.count()):
                item_text = self.version_select.itemText(i)
                clean_text = item_text.replace("✅ ", "").replace("⬇️ ", "")
                if clean_text == self._desired_version:
                    self.version_select.setCurrentIndex(i)
                    break
            self._desired_version = None
        
        # Отмечаем, что первая загрузка завершена
        self._first_load_done = True
        # Обновляем фильтры модов под текущий выбор и перечень версий
        try:
            self.populate_mods_game_versions()
            self.set_mods_filters_from_selected()
        except Exception:
            pass

    def apply_version_filter(self):
        # Сохраняем текущий выбор, чтобы попытаться восстановить после фильтрации
        previous_selection = self.version_select.currentText() if self.version_select.count() > 0 else ''

        # Определяем режим фильтра
        if getattr(self, 'offline_mode', False):
            # В офлайне типы неизвестны, показываем все установленное
            allowed_types = None
        else:
            mode = self.version_filter.currentText()
            if mode == 'Релизы':
                allowed_types = {'release'}
            elif mode == 'Снапшоты':
                allowed_types = {'snapshot'}
            else:
                allowed_types = None  # Все

        # Перезаполняем список версий
        self.version_select.blockSignals(True)
        self.version_select.clear()

        versions = self.all_versions
        if allowed_types is not None:
            versions = [v for v in versions if v.get('type') in allowed_types]

        for version in versions:
            vid = version.get('id')
            if not vid:
                continue
            
            # Проверяем, установлена ли версия
            is_installed = self.is_version_installed(vid)
            status_icon = "✅" if is_installed else "⬇️"
            
            # Добавляем базовую версию с индикатором статуса
            display_name = f"{status_icon} {vid}"
            self.version_select.addItem(display_name)
            
            # В онлайне добавляем «виртуальные» записи: fabric и quilt (если поддерживается)
            if not getattr(self, 'offline_mode', False):
                if self.is_modloader_supported_for(vid, 'fabric'):
                    fabric_display = f"{status_icon} {vid} fabric"
                    self.version_select.addItem(fabric_display)
                
                # Добавляем Quilt только если включена поддержка в настройках
                if (hasattr(self, 'settings') and 
                    self.settings.get('quilt_support', False) and
                    self.is_modloader_supported_for(vid, 'quilt')):
                    quilt_display = f"{status_icon} {vid} quilt"
                    self.version_select.addItem(quilt_display)

        # Восстанавливаем выбор, если возможно
        if previous_selection:
            # Ищем по чистому имени (без иконки)
            clean_previous = previous_selection.replace("✅ ", "").replace("⬇️ ", "")
            for i in range(self.version_select.count()):
                item_text = self.version_select.itemText(i)
                clean_text = item_text.replace("✅ ", "").replace("⬇️ ", "")
                if clean_text == clean_previous:
                    self.version_select.setCurrentIndex(i)
                    break
        self.version_select.blockSignals(False)
        # После изменения списка – синхронизируем фильтры модов
        try:
            self.populate_mods_game_versions()
            self.set_mods_filters_from_selected()
        except Exception:
            pass

    def is_version_installed(self, version_id: str) -> bool:
        """Проверяет, установлена ли версия локально"""
        version_dir = os.path.join(minecraft_directory, 'versions', version_id)
        version_json = os.path.join(version_dir, f"{version_id}.json")
        return os.path.isdir(version_dir) and os.path.isfile(version_json)

    def is_modloader_supported_for(self, mc_version: str, loader: str) -> bool:
        # Грубая эвристика: Fabric и Quilt официально поддерживают 1.14+; более точно можно опросить meta
        try:
            parts = mc_version.split('.')
            # Ожидаем формат X.Y[.Z]
            major = int(parts[0]) if len(parts) > 0 else 0
            minor = int(parts[1]) if len(parts) > 1 else 0
            return (major > 1) or (major == 1 and minor >= 14)
        except Exception:
            return False
    
    def is_fabric_supported_for(self, mc_version: str) -> bool:
        """Обратная совместимость"""
        return self.is_modloader_supported_for(mc_version, 'fabric')

    def get_installed_versions(self):
        versions_dir = os.path.join(minecraft_directory, 'versions')
        result = []
        try:
            if not os.path.isdir(versions_dir):
                return result
            for entry in os.listdir(versions_dir):
                entry_path = os.path.join(versions_dir, entry)
                if not os.path.isdir(entry_path):
                    continue
                # Установленная версия обычно имеет файл <version>/<version>.json
                json_manifest = os.path.join(entry_path, f"{entry}.json")
                if os.path.isfile(json_manifest):
                    result.append({'id': entry})
        except Exception:
            pass
        return result

    def on_version_filter_changed(self):
        self.apply_version_filter()
        self.save_config()
        try:
            self.populate_mods_game_versions()
            self.set_mods_filters_from_selected()
        except Exception:
            pass

    def save_config(self):
        cfg = dict(self._config) if hasattr(self, '_config') else {}
        cfg['version_filter'] = self.version_filter.currentText()
        # Текущая выбранная версия (без иконки)
        if self.version_select.count() > 0:
            display_text = self.version_select.currentText()
            clean_version = display_text.replace("✅ ", "").replace("⬇️ ", "")
            cfg['selected_version'] = clean_version
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            self._config = cfg
        except Exception:
            pass

    def add_account(self):
        nick, ok = QInputDialog.getText(self, "Добавить аккаунт", "Введите никнейм:")
        if ok and nick.strip():
            nick = nick.strip()
            try:
                with open(self.users_path, 'r', encoding='utf-8') as f:
                    users = json.load(f)
            except Exception:
                users = []

            if any(u.get('nickname') == nick for u in users):
                QMessageBox.warning(self, "Ошибка", "Такой аккаунт уже существует.")
                return

            users.append({'nickname': nick})

            with open(self.users_path, 'w', encoding='utf-8') as f:
                json.dump(users, f, indent=4, ensure_ascii=False)

            self.load_accounts()
            index = self.account_type.findText(nick)
            if index >= 0:
                self.account_type.setCurrentIndex(index)

    def state_update(self, value: bool):
        self.start_button.setDisabled(value)
        self.start_progress.setVisible(value)
        self.start_progress_label.setVisible(value)
        self.time_label.setVisible(False)

    def update_progress(self, progress: int, max_progress: int, label: str):
        self.start_progress.setMaximum(max_progress)
        self.start_progress.setValue(progress)
        self.start_progress_label.setText(label)

    def launch_game(self):
        nick = self.account_type.currentText()
        if not nick:
            nick = 'Player'
        
        # Извлекаем чистую версию из отображаемого имени (убираем иконку)
        display_text = self.version_select.currentText()
        version_id = display_text.replace("✅ ", "").replace("⬇️ ", "")
        
        self.launch_thread.launch_setup_signal.emit(version_id, nick)
        self.launch_thread.start()
        # Сохраняем выбранную версию на момент запуска
        try:
            self.save_config()
        except Exception:
            pass

    def show_message(self, title: str, text: str):
        QMessageBox.information(self, title, text)
    
    def append_console(self, line: str):
        try:
            if not hasattr(self, 'console_view'):
                return
            self.console_view.append(line)
            if self.console_autoscroll.isChecked():
                self.console_view.moveCursor(self.console_view.textCursor().End)
        except Exception:
            pass

    def refresh_skin_preview(self):
        try:
            nick = self.account_type.currentText().strip() or 'Steve'
            url = f'https://minotar.net/armor/body/{nick}/160.png'
            headers = {'User-Agent': 'MojNovyLauncher/1.2 (skin-preview)'}
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                pix = QPixmap()
                if pix.loadFromData(r.content):
                    self.skin_preview.setPixmap(pix)
                    return
        except Exception:
            pass
        # Фолбэк: пустое превью
        self.skin_preview.setPixmap(QPixmap())
    
    def load_settings(self):
        """Загружает настройки приложения"""
        self.settings = {}
        try:
            settings_path = os.path.join(
                os.getenv('APPDATA'), '.MjnLauncher', 'client', 'settings.json'
            )
            if os.path.exists(settings_path):
                with open(settings_path, 'r', encoding='utf-8') as f:
                    self.settings = json.load(f)
        except Exception:
            self.settings = {}
        
        # Применяем настройки
        self.apply_settings()
    
    def apply_settings(self):
        """Применяет загруженные настройки"""
        # Размер окна
        window_size = self.settings.get('window_size', '300x200')
        if 'x' in window_size:
            width, height = map(int, window_size.split('x'))
            self.resize(width, height)
        
        # Темная тема
        if self.settings.get('dark_theme', False):
            self.apply_dark_theme()
        else:
            self.apply_light_theme()
        
        # Обновляем меню трея при изменении настроек
        if hasattr(self, 'tray_icon') and self.tray_icon.isVisible():
            self.create_tray_menu()
    
    def apply_dark_theme(self):
        """Применяет темную тему"""
        dark_style = """
        * {
            background-color: #2b2b2b;
            color: #ffffff;
        }
        QMainWindow {
            background-color: #2b2b2b;
            color: #ffffff;
        }
        QWidget {
            background-color: #2b2b2b;
            color: #ffffff;
        }
        QPushButton {
            background-color: #404040;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 6px;
            color: #ffffff;
        }
        QPushButton:hover {
            background-color: #505050;
        }
        QPushButton:pressed {
            background-color: #353535;
        }
        QPushButton:disabled {
            background-color: #2a2a2a;
            color: #666666;
        }
        QComboBox {
            background-color: #404040;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 4px;
            color: #ffffff;
        }
        QComboBox:hover {
            background-color: #505050;
        }
        QComboBox::drop-down {
            border: none;
        }
        QComboBox::down-arrow {
            image: none;
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            border-top: 5px solid #ffffff;
            margin-right: 5px;
        }
        QComboBox QAbstractItemView {
            background-color: #404040;
            border: 1px solid #555555;
            selection-background-color: #505050;
            color: #ffffff;
        }
        QProgressBar {
            border: 1px solid #555555;
            border-radius: 4px;
            text-align: center;
            background-color: #2a2a2a;
            color: #ffffff;
        }
        QProgressBar::chunk {
            background-color: #4a9eff;
            border-radius: 3px;
        }
        QLabel {
            color: #ffffff;
        }
        QLineEdit {
            background-color: #404040;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 4px;
            color: #ffffff;
        }
        QLineEdit:focus {
            border: 1px solid #4a9eff;
        }
        QSpinBox {
            background-color: #404040;
            border: 1px solid #555555;
            border-radius: 4px;
            padding: 4px;
            color: #ffffff;
        }
        QSpinBox:focus {
            border: 1px solid #4a9eff;
        }
        QCheckBox {
            color: #ffffff;
        }
        QCheckBox::indicator {
            width: 16px;
            height: 16px;
        }
        QCheckBox::indicator:unchecked {
            background-color: #404040;
            border: 1px solid #555555;
            border-radius: 3px;
        }
        QCheckBox::indicator:checked {
            background-color: #4a9eff;
            border: 1px solid #4a9eff;
            border-radius: 3px;
        }
        QTabWidget::pane {
            border: 1px solid #555555;
            background-color: #2b2b2b;
        }
        QTabBar::tab {
            background-color: #404040;
            border: 1px solid #555555;
            padding: 8px 16px;
            color: #ffffff;
        }
        QTabBar::tab:selected {
            background-color: #505050;
        }
        QTabBar::tab:hover {
            background-color: #4a4a4a;
        }
        QGroupBox {
            color: #ffffff;
            border: 1px solid #555555;
            border-radius: 4px;
            margin-top: 10px;
            padding-top: 10px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px 0 5px;
        }
        """
        self.setStyleSheet(dark_style)
        # Применяем стили ко всем дочерним элементам
        self.centralwidget.setStyleSheet(dark_style)
    
    def apply_light_theme(self):
        """Применяет светлую тему (сброс стилей)"""
        self.setStyleSheet("")
        self.centralwidget.setStyleSheet("")
    
    def open_settings(self):
        """Открывает окно настроек"""
        dialog = SettingsDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            if dialog.save_settings():
                # Перезагружаем настройки
                self.load_settings()
                QMessageBox.information(self, "Настройки", "Настройки сохранены успешно!")
            else:
                QMessageBox.warning(self, "Ошибка", "Не удалось сохранить настройки.")
    
    def init_system_tray(self):
        """Инициализирует системный трей"""
        # Проверяем, поддерживается ли системный трей
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        
        # Создаем иконку для трея
        self.tray_icon = QSystemTrayIcon(self)
        
        # Загружаем иконку (используем первую доступную)
        icon_path = "assets/2.ico"
        if os.path.exists(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            # Если иконка не найдена, используем стандартную
            self.tray_icon.setIcon(self.style().standardIcon(self.style().SP_ComputerIcon))
        
        # Создаем контекстное меню для трея
        self.create_tray_menu()
        
        # Подключаем сигналы
        self.tray_icon.activated.connect(self.tray_icon_activated)
        
        # Показываем трей
        self.tray_icon.show()
        
        # Устанавливаем подсказку
        self.tray_icon.setToolTip("MJNL - MojNovyLauncher")
    
    def create_tray_menu(self):
        """Создает контекстное меню для системного трея"""
        tray_menu = QMenu()
        
        # Показать/скрыть
        self.show_action = QAction("Показать", self)
        self.show_action.triggered.connect(self.show_window)
        tray_menu.addAction(self.show_action)
        
        # Запустить игру
        self.launch_action = QAction("Запустить игру", self)
        self.launch_action.triggered.connect(self.launch_game)
        tray_menu.addAction(self.launch_action)
        
        tray_menu.addSeparator()
        
        # Настройки
        settings_action = QAction("Настройки", self)
        settings_action.triggered.connect(self.open_settings)
        tray_menu.addAction(settings_action)
        
        tray_menu.addSeparator()
        
        # Выход
        quit_action = QAction("Выход", self)
        quit_action.triggered.connect(self.quit_application)
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
    
    def tray_icon_activated(self, reason):
        """Обработчик активации иконки в трее"""
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_window()
    
    def show_window(self):
        """Показывает главное окно"""
        self.show()
        self.raise_()
        self.activateWindow()
    
    def hide_to_tray(self):
        """Сворачивает окно в трей"""
        if hasattr(self, 'tray_icon') and self.tray_icon.isVisible():
            self.hide()
            self.tray_icon.showMessage(
                "MJNL",
                "Лаунчер свернут в системный трей",
                QSystemTrayIcon.Information,
                2000
            )
    
    def quit_application(self):
        """Выход из приложения"""
        if hasattr(self, 'tray_icon'):
            self.tray_icon.hide()
        QApplication.quit()
    
    def closeEvent(self, event):
        """Обработчик закрытия окна"""
        # Проверяем настройку "сворачивать в трей"
        if (hasattr(self, 'settings') and 
            self.settings.get('minimize_to_tray', False) and
            hasattr(self, 'tray_icon') and 
            self.tray_icon.isVisible()):
            
            self.hide_to_tray()
            event.ignore()
        else:
            # Обычное закрытие
            if hasattr(self, 'tray_icon'):
                self.tray_icon.hide()
            event.accept()


if __name__ == '__main__':
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(argv)
    window = MainWindow()
    window.show()
    exit(app.exec_())



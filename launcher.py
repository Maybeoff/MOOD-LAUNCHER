import os
import sys
import json
import requests
import zipfile
import uuid
from subprocess import call
from typing import List, Dict

from PyQt6.QtCore import QThread, pyqtSignal, QSize, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QComboBox, QProgressBar,
    QPushButton, QApplication, QMainWindow, QHBoxLayout,
    QInputDialog, QMessageBox, QCheckBox
)

from minecraft_launcher_lib.utils import get_minecraft_directory, get_version_list
from minecraft_launcher_lib.install import install_minecraft_version
from minecraft_launcher_lib.command import get_minecraft_command
import minecraft_launcher_lib.forge

# Определение базового пути для ресурсов (работает и в .exe и в .py)
def resource_path(relative_path):
    """Получить абсолютный путь к ресурсу, работает для dev и для PyInstaller"""
    try:
        # PyInstaller создает временную папку и сохраняет путь в _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# Базовая директория для игр
BASE_GAME_DIR = os.path.join(os.getenv('APPDATA'), '.MjnLauncher', 'game')

# Настройки Java
JRE_DIR = "jre"
JAVA_ZIP_URL = "https://github.com/adoptium/temurin8-binaries/releases/download/jdk8u482-b08/OpenJDK8U-jdk_x64_windows_hotspot_8u482b08.zip"

# URL со сборками модов
MODPACKS_URL = "https://raw.githubusercontent.com/Maybeoff/mc-sborka/refs/heads/main/version.json"


def load_modpacks() -> List[Dict]:
    """Загрузка списка сборок модов из GitHub"""
    try:
        response = requests.get(MODPACKS_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        # Если это один объект, оборачиваем в список
        if isinstance(data, dict):
            return [data]
        return data
    except Exception as e:
        print(f"Ошибка загрузки сборок: {e}")
        return []


def ensure_java():
    """Проверка и установка Java 8 для Forge"""
    if not os.path.exists(JRE_DIR):
        print("Скачивание Java 8...")
        zip_path = "java8.zip"
        response = requests.get(JAVA_ZIP_URL, stream=True)
        total_size = int(response.headers.get('content-length', 0))
        
        with open(zip_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print("Распаковка Java...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(JRE_DIR)
        os.remove(zip_path)
    
    # Поиск java.exe
    for root, _, files in os.walk(JRE_DIR):
        if "java.exe" in files:
            return os.path.join(root, "java.exe")
    return None


class LaunchThread(QThread):
    launch_setup_signal = pyqtSignal(str, str, bool, str, str, str)
    progress_update_signal = pyqtSignal(int, int, str)
    state_update_signal = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.launch_setup_signal.connect(self.launch_setup)
        self.version_id = ''
        self.username = ''
        self.user_uuid = ''
        self.use_forge = False
        self.mod_list_url = ''
        self.options_url = ''
        self.modpack_name = ''
        self.minecraft_directory = ''
        self.progress = 0
        self.progress_max = 0
        self.progress_label = ''

    def launch_setup(self, version_id, username, use_forge, mod_list_url='', options_url='', modpack_name=''):
        self.version_id = version_id
        self.username = username
        self.use_forge = use_forge
        self.mod_list_url = mod_list_url
        self.options_url = options_url
        self.modpack_name = modpack_name
        
        # Определяем директорию для установки
        if modpack_name:
            # Для сборок - отдельная папка
            safe_name = "".join(c for c in modpack_name if c.isalnum() or c in (' ', '-', '_')).strip()
            safe_name = safe_name.replace(' ', '_')
            self.minecraft_directory = os.path.join(BASE_GAME_DIR, safe_name)
        else:
            # Для обычных версий - общая папка
            self.minecraft_directory = os.path.join(BASE_GAME_DIR, "default")

    def update_progress_label(self, value):
        self.progress_label = value
        self.progress_update_signal.emit(self.progress, self.progress_max, self.progress_label)

    def update_progress(self, value):
        self.progress = value
        self.progress_update_signal.emit(self.progress, self.progress_max, self.progress_label)

    def update_progress_max(self, value):
        self.progress_max = value
        self.progress_update_signal.emit(self.progress, self.progress_max, self.progress_label)

    def download_mods(self):
        """Скачивание модов из mod_list_url"""
        if not self.mod_list_url:
            return
        
        try:
            self.update_progress_label("Загрузка списка модов...")
            response = requests.get(self.mod_list_url, timeout=10)
            response.raise_for_status()
            mods_dict = response.json()
            
            # Создаём папку mods
            mods_dir = os.path.join(self.minecraft_directory, "mods")
            os.makedirs(mods_dir, exist_ok=True)
            
            total_mods = len(mods_dict)
            self.update_progress_max(total_mods)
            
            for idx, (mod_url, mod_filename) in enumerate(mods_dict.items(), 1):
                mod_path = os.path.join(mods_dir, mod_filename)
                
                # Проверяем размер файла - если меньше 1KB, перекачиваем
                if os.path.exists(mod_path):
                    file_size = os.path.getsize(mod_path)
                    if file_size > 1024:  # Больше 1KB - нормальный файл
                        self.update_progress_label(f"Мод {mod_filename} уже установлен ({idx}/{total_mods})")
                        self.update_progress(idx)
                        continue
                    else:
                        # Удаляем битый файл
                        os.remove(mod_path)
                
                self.update_progress_label(f"Скачивание {mod_filename} ({idx}/{total_mods})")
                
                try:
                    # Конвертируем raw.githubusercontent.com в media.githubusercontent.com для LFS
                    download_url = mod_url.replace(
                        'raw.githubusercontent.com',
                        'media.githubusercontent.com/media'
                    )
                    
                    # Скачиваем с stream=True для бинарных файлов
                    mod_response = requests.get(download_url, timeout=60, stream=True)
                    mod_response.raise_for_status()
                    
                    with open(mod_path, 'wb') as f:
                        for chunk in mod_response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                    
                    self.update_progress(idx)
                except Exception as e:
                    self.update_progress_label(f"Ошибка скачивания {mod_filename}: {e}")
                    if os.path.exists(mod_path):
                        os.remove(mod_path)
                    
        except Exception as e:
            self.update_progress_label(f"Ошибка загрузки модов: {e}")

    def download_options(self):
        """Скачивание настроек игры (options.txt)"""
        if not self.options_url:
            return
        
        try:
            options_path = os.path.join(self.minecraft_directory, "options.txt")
            
            # Скачиваем только если файла нет
            if os.path.exists(options_path):
                self.update_progress_label("Настройки уже установлены")
                return
            
            self.update_progress_label("Скачивание настроек игры...")
            response = requests.get(self.options_url, timeout=30)
            response.raise_for_status()
            
            with open(options_path, 'w', encoding='utf-8') as f:
                f.write(response.text)
            
            self.update_progress_label("Настройки установлены")
        except Exception as e:
            self.update_progress_label(f"Ошибка загрузки настроек: {e}")

    def is_version_installed(self, version_id):
        """Проверка установлена ли версия"""
        version_json = os.path.join(self.minecraft_directory, "versions", version_id, f"{version_id}.json")
        return os.path.exists(version_json)

    def run(self):
        self.state_update_signal.emit(True)

        callback = {
            'setStatus': self.update_progress_label,
            'setProgress': self.update_progress,
            'setMax': self.update_progress_max
        }

        actual_version_id = self.version_id
        java_path = None

        if self.use_forge:
            # Установка Java для Forge
            self.update_progress_label("Подготовка Java...")
            java_path = ensure_java()
            if not java_path:
                self.update_progress_label("Ошибка: Java не найдена!")
                self.state_update_signal.emit(False)
                return

            # Проверка и установка базовой версии
            if not self.is_version_installed(self.version_id):
                self.update_progress_label(f"Установка базовой версии {self.version_id}...")
                install_minecraft_version(
                    version=self.version_id,
                    minecraft_directory=self.minecraft_directory,
                    callback=callback
                )
            else:
                self.update_progress_label(f"Версия {self.version_id} уже установлена")

            # Поиск и установка Forge
            self.update_progress_label(f"Поиск Forge для {self.version_id}...")
            all_forge = minecraft_launcher_lib.forge.list_forge_versions()
            forge_list = [v for v in all_forge if v.startswith(self.version_id)]
            
            if not forge_list:
                self.update_progress_label("Ошибка: Forge не найден!")
                self.state_update_signal.emit(False)
                return
            
            latest_forge = forge_list[0]
            
            # Определение ID версии Forge
            versions_path = os.path.join(self.minecraft_directory, "versions")
            forge_installed = False
            if os.path.exists(versions_path):
                for folder in os.listdir(versions_path):
                    if self.version_id in folder and "forge" in folder.lower():
                        actual_version_id = folder
                        forge_installed = True
                        break
            if not forge_installed:
                actual_version_id = latest_forge
            
            # Установка Forge если не установлен
            if not forge_installed:
                self.update_progress_label(f"Установка {latest_forge}...")
                minecraft_launcher_lib.forge.install_forge_version(
                    latest_forge,
                    self.minecraft_directory,
                    callback=callback
                )
            else:
                self.update_progress_label(f"Forge уже установлен")
            
            # Скачивание модов если указан mod_list_url
            if self.mod_list_url:
                self.download_mods()
            
            # Скачивание настроек если указан options_url
            if self.options_url:
                self.download_options()
        else:
            # Обычная установка без Forge
            if not self.is_version_installed(self.version_id):
                self.update_progress_label(f"Установка версии {self.version_id}...")
                install_minecraft_version(
                    version=self.version_id,
                    minecraft_directory=self.minecraft_directory,
                    callback=callback
                )
            else:
                self.update_progress_label(f"Версия {self.version_id} уже установлена")

        # Запуск игры
        self.update_progress_label("Запуск игры...")
        options = {
            'username': self.username,
            'uuid': self.user_uuid,
            'token': ''
        }

        if java_path:
            options['executablePath'] = java_path
            options['jvmArguments'] = ["-Xmx2G", "-Xms2G"]

        call(get_minecraft_command(
            version=actual_version_id,
            minecraft_directory=self.minecraft_directory,
            options=options
        ))

        self.state_update_signal.emit(False)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle('MJNL')
        self.resize(300, 200)
        self.centralwidget = QWidget(self)

        # Путь к файлу с аккаунтами
        self.users_path = os.path.join(
            os.getenv('APPDATA'), '.MjnLauncher', 'client', 'users.json'
        )
        os.makedirs(os.path.dirname(self.users_path), exist_ok=True)

        # Логотип
        self.logo = QLabel(self.centralwidget)
        self.logo.setMaximumSize(QSize(256, 37))
        self.logo.setPixmap(QPixmap(resource_path('assets/title.png')))
        self.logo.setScaledContents(True)

        # Список аккаунтов и кнопка добавления
        self.account_type = QComboBox(self.centralwidget)
        self.add_account_button = QPushButton("+", self.centralwidget)
        self.add_account_button.setFixedWidth(30)
        self.add_account_button.clicked.connect(self.add_account)

        self.account_layout = QHBoxLayout()
        self.account_layout.addWidget(self.account_type, 4)
        self.account_layout.addWidget(self.add_account_button, 1)

        # Список сборок модов
        self.version_select = QComboBox(self.centralwidget)
        self.modpacks = load_modpacks()
        
        # Чекбокс для Forge (только для стандартного режима)
        self.forge_checkbox = QCheckBox("Использовать Forge", self.centralwidget)
        
        if self.modpacks:
            for modpack in self.modpacks:
                self.version_select.addItem(modpack.get('name', 'Unknown'))
            # Скрываем чекбокс Forge для сборок (они всегда с Forge)
            self.forge_checkbox.setVisible(False)
        else:
            # Фолбэк на стандартные версии если сборки не загрузились
            for version in get_version_list():
                if version['type'] == 'release':
                    self.version_select.addItem(version['id'])
            self.modpacks = None
            self.forge_checkbox.setVisible(True)

        # Прогресс-бар и метка
        self.start_progress_label = QLabel(self.centralwidget)
        self.start_progress_label.setVisible(False)
        self.start_progress = QProgressBar(self.centralwidget)
        self.start_progress.setVisible(False)

        self.time_label = QLabel(self.centralwidget)
        self.time_label.setVisible(False)

        # Кнопка запуска игры
        self.start_button = QPushButton('Play', self.centralwidget)
        self.start_button.clicked.connect(self.launch_game)

        # Основной вертикальный лэйаут
        layout = QVBoxLayout(self.centralwidget)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.addWidget(self.logo, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addLayout(self.account_layout)
        layout.addWidget(self.version_select)
        layout.addWidget(self.forge_checkbox)
        layout.addWidget(self.start_progress_label)
        layout.addWidget(self.start_progress)
        layout.addWidget(self.time_label)
        layout.addWidget(self.start_button)

        self.setCentralWidget(self.centralwidget)

        self.load_accounts()

        # Поток для запуска игры
        self.launch_thread = LaunchThread()
        self.launch_thread.state_update_signal.connect(self.state_update)
        self.launch_thread.progress_update_signal.connect(self.update_progress)

    def load_accounts(self):
        self.account_type.clear()
        try:
            with open(self.users_path, 'r', encoding='utf-8') as f:
                users = json.load(f)
                for user in users:
                    self.account_type.addItem(user.get('nickname', 'Unknown'))
        except Exception:
            self.account_type.addItem('Player')

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

            # Генерируем UUID на основе никнейма (как в других пиратских лаунчерах)
            user_uuid = str(uuid.uuid3(uuid.NAMESPACE_DNS, f"OfflinePlayer:{nick}"))
            users.append({'nickname': nick, 'uuid': user_uuid})

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
        
        # Получаем UUID для выбранного аккаунта
        user_uuid = ''
        try:
            with open(self.users_path, 'r', encoding='utf-8') as f:
                users = json.load(f)
                for user in users:
                    if user.get('nickname') == nick:
                        user_uuid = user.get('uuid', '')
                        break
        except Exception:
            pass
        
        # Если UUID не найден, генерируем на основе никнейма (как в других пиратских лаунчерах)
        if not user_uuid:
            user_uuid = str(uuid.uuid3(uuid.NAMESPACE_DNS, f"OfflinePlayer:{nick}"))
        
        # Сохраняем UUID в поток запуска
        self.launch_thread.user_uuid = user_uuid
        
        # Если используются сборки модов
        if self.modpacks:
            selected_index = self.version_select.currentIndex()
            if 0 <= selected_index < len(self.modpacks):
                modpack = self.modpacks[selected_index]
                version_id = modpack.get('minecraft_version', '1.12.2')
                mod_list_url = modpack.get('Mod_list_url', '')
                options_url = modpack.get('options', '')
                modpack_name = modpack.get('name', 'modpack')
                # Сборки всегда используют Forge
                self.launch_thread.launch_setup_signal.emit(version_id, nick, True, mod_list_url, options_url, modpack_name)
            else:
                return
        else:
            # Стандартный режим
            use_forge = self.forge_checkbox.isChecked()
            self.launch_thread.launch_setup_signal.emit(self.version_select.currentText(), nick, use_forge, '', '', '')
        
        self.launch_thread.start()


if __name__ == '__main__':
    app = QApplication(argv)
    window = MainWindow()
    window.show()
    exit(app.exec())

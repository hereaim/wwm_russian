import csv
import os
import re
import sys
from typing import List, Tuple, Dict
from collections import defaultdict

from PyQt5 import QtWidgets, QtCore
from PyQt5.QtGui import QTextCursor


# --- TSV helpers ---
def load_tsv(path: str) -> Tuple[List[str], List[List[str]]]:
    """Загрузка TSV: возвращает (header, rows)."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration:
            return [], []
        rows = [row for row in reader]
    return header, rows


def save_tsv(path: str, header: List[str], rows: List[List[str]]) -> None:
    """Сохранение TSV."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        if header:
            writer.writerow(header)
        writer.writerows(rows)


def find_column_index(header: List[str], name: str, default_index: int = 0) -> int:
    """Индекс колонки по имени или default_index."""
    try:
        return header.index(name)
    except ValueError:
        return default_index if len(header) > default_index else 0


# --- Сортировочные правила ---
class SortRule:
    def __init__(self, word: str, mode: str):
        self.word = word
        self.mode = mode  # "text" — подстрока с окончаниями; "own" — целое слово; "exception" — исключить при совпадении

    def matches(self, text: str) -> bool:
        if not text:
            return False
        word = re.escape(self.word)
        if self.mode == "own":
            # Точное слово без суффиксов
            pattern = rf"\b{word}\b"
        elif self.mode == "exception":
            # Подстрока, помеченная как исключение
            pattern = rf"\b{word}\w*"
        else:
            # Подстрока с возможными продолжениями (склонения/окончания), регистр игнорируем
            pattern = rf"\b{word}\w*"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None


def load_sort_rules(path: str) -> List[SortRule]:
    """Читает sort.txt формата word:mode. mode in {'text','own','exception'}; по умолчанию text."""
    rules: List[SortRule] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                word, mode = line.split(":", 1)
                mode = mode.strip() or "text"
            else:
                word, mode = line, "text"
            rules.append(SortRule(word.strip(), mode))
    return rules


# --- Основная сортировка ---
def build_score(text: str, rules: List[SortRule]) -> Tuple[int, bool]:
    """Строит битовую маску; exception помечает строку к исключению."""
    score = 0
    excluded = False
    total = len(rules)
    for idx, rule in enumerate(rules):
        if rule.matches(text):
            if rule.mode == "exception":
                excluded = True
                continue
            bit_pos = total - idx - 1  # старшие биты — первые правила
            score |= 1 << bit_pos
    return score, excluded


def sort_rows(
    rows: List[List[str]],
    text_idx: int,
    id_idx: int,
    rules: List[SortRule],
    filter_only: bool,
) -> Tuple[List[List[str]], int, int]:
    """
    Возвращает отсортированные/отфильтрованные строки.
    filter_only=True — оставляем только строки с матчем по правилам.
    Ключ сортировки: (-score, text_lower, id) — даёт группировку по одинаковому тексту.
    """
    prepared = []
    matched = 0
    for row in rows:
        text = row[text_idx] if len(row) > text_idx else ""
        rid = row[id_idx] if len(row) > id_idx else ""
        score, excluded = build_score(text, rules)
        if excluded:
            continue
        if filter_only and score == 0:
            continue
        if score > 0:
            matched += 1
        key = (-score, text.lower(), rid)
        prepared.append((key, row))

    prepared.sort(key=lambda x: x[0])
    return [row for _, row in prepared], len(prepared), matched


def build_source_index(
    rows: List[List[str]], text_idx: int, id_idx: int, rules: List[SortRule]
) -> Tuple[dict, int]:
    """
    Строит индекс по ID из исходного файла:
    id -> (rank, score, text_lower)
    rank — позиция после сортировки ключом (-score, text_lower, id)
    Возвращает (index, matched_count)
    """
    prepared = []
    matched = 0
    for row in rows:
        text = row[text_idx] if len(row) > text_idx else ""
        rid = row[id_idx] if len(row) > id_idx else ""
        score, excluded = build_score(text, rules)
        if excluded:
            continue
        if score > 0:
            matched += 1
        key = (-score, text.lower(), rid)
        prepared.append((key, rid, text))

    prepared.sort(key=lambda x: x[0])
    index = {}
    for rank, (key, rid, text) in enumerate(prepared):
        if rid:
            score = -key[0]  # так как ключ хранит -score
            index[rid] = (rank, score, text.lower())
    return index, matched


# --- Функции для сортировки по переводам ---
def detect_language(text: str) -> int:
    """
    Определяет язык текста и возвращает приоритет для сортировки:
    0 - кириллица (русский)
    1 - английский
    2 - китайский
    3 - другой/неопределенный
    """
    if not text or not text.strip():
        return 3
    
    # Проверка на кириллицу
    if re.search(r'[А-Яа-яЁё]', text):
        return 0
    
    # Проверка на китайские иероглифы (CJK)
    if re.search(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]', text):
        return 2
    
    # Проверка на английский (латиница)
    if re.search(r'[A-Za-z]', text):
        return 1
    
    return 3


def sort_by_translations(
    rows: List[List[str]],
    text_idx: int,
    id_idx: int,
) -> List[List[str]]:
    """
    Сортирует строки по переводам:
    1. Сначала кириллица, потом English, потом китайский
    2. Группирует дубли на одном языке рядом (одинаковый текст на одном языке будет рядом)
    3. НЕ удаляет дубли, сохраняет все строки
    
    Возвращает отсортированные строки (все дубли сохранены, но сгруппированы).
    """
    # Создаем список с ключами сортировки для каждой строки
    # Ключ: (приоритет языка, текст в нижнем регистре, ID)
    # Это автоматически сгруппирует дубли рядом (одинаковый язык и текст)
    prepared = []
    
    for row in rows:
        text = row[text_idx] if len(row) > text_idx else ""
        rid = row[id_idx] if len(row) > id_idx else ""
        language_priority = detect_language(text)
        text_lower = text.lower().strip()
        
        # Ключ сортировки: сначала язык, потом текст, потом ID для стабильности
        key = (language_priority, text_lower, rid)
        prepared.append((key, row))
    
    # Сортируем по ключу
    prepared.sort(key=lambda x: x[0])
    
    # Возвращаем отсортированные строки (все дубли сохранены)
    return [row for _, row in prepared]


# --- GUI ---
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TSV сортировка по словам и дублям")
        self.resize(800, 400)

        self.sort_path = ""  # путь к sort.txt

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # Выбор файлов A и B
        file_layout = QtWidgets.QGridLayout()
        self.edit_a = QtWidgets.QLineEdit()
        self.btn_a = QtWidgets.QPushButton("Обзор A...")
        self.btn_a.clicked.connect(self.browse_a)

        self.edit_b = QtWidgets.QLineEdit()
        self.btn_b = QtWidgets.QPushButton("Обзор B...")
        self.btn_b.clicked.connect(self.browse_b)

        file_layout.addWidget(QtWidgets.QLabel("Файл A (обычно перевод):"), 0, 0)
        file_layout.addWidget(self.edit_a, 0, 1)
        file_layout.addWidget(self.btn_a, 0, 2)

        file_layout.addWidget(QtWidgets.QLabel("Файл B (обычно EN):"), 1, 0)
        file_layout.addWidget(self.edit_b, 1, 1)
        file_layout.addWidget(self.btn_b, 1, 2)

        layout.addLayout(file_layout)

        # Селектор источника сортировки
        source_layout = QtWidgets.QHBoxLayout()
        source_layout.addWidget(QtWidgets.QLabel("Сортировать из:"))
        self.combo_source = QtWidgets.QComboBox()
        self.combo_source.addItem("Файл A", "A")
        self.combo_source.addItem("Файл B", "B")
        source_layout.addWidget(self.combo_source)

        source_layout.addWidget(QtWidgets.QLabel("Сортировать в:"))
        self.combo_target = QtWidgets.QComboBox()
        self.combo_target.addItem("Файл A", "A")
        self.combo_target.addItem("Файл B", "B")
        source_layout.addWidget(self.combo_target)
        source_layout.addStretch(1)
        layout.addLayout(source_layout)

        # sort.txt
        sort_layout = QtWidgets.QHBoxLayout()
        sort_layout.addWidget(QtWidgets.QLabel("sort.txt (слова):"))
        self.edit_sort = QtWidgets.QLineEdit()
        self.btn_sort = QtWidgets.QPushButton("Обзор sort.txt...")
        self.btn_sort.clicked.connect(self.browse_sort)
        sort_layout.addWidget(self.edit_sort)
        sort_layout.addWidget(self.btn_sort)
        layout.addLayout(sort_layout)

        # Кнопки действий
        btn_layout = QtWidgets.QHBoxLayout()
        self.btn_full_sort = QtWidgets.QPushButton("1. Полная сортировка (все строки)")
        self.btn_full_sort.clicked.connect(self.handle_full_sort)
        self.btn_filter_sort = QtWidgets.QPushButton("2. Вырезать только по словам из sort.txt")
        self.btn_filter_sort.clicked.connect(self.handle_filter_sort)
        self.btn_sort_translations = QtWidgets.QPushButton("3. Отсортировать по переводам")
        self.btn_sort_translations.clicked.connect(self.handle_sort_translations)
        btn_layout.addWidget(self.btn_full_sort)
        btn_layout.addWidget(self.btn_filter_sort)
        btn_layout.addWidget(self.btn_sort_translations)
        layout.addLayout(btn_layout)

        # Лог
        self.log = QtWidgets.QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)

    # --- browse helpers ---
    def browse_a(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Выберите TSV-файл A", "", "TSV files (*.tsv);;All files (*.*)"
        )
        if path:
            self.edit_a.setText(path)
            self.try_auto_sort_path(path)

    def browse_b(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Выберите TSV-файл B", "", "TSV files (*.tsv);;All files (*.*)"
        )
        if path:
            self.edit_b.setText(path)
            self.try_auto_sort_path(path)

    def browse_sort(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Выберите sort.txt", "", "Text files (*.txt);;All files (*.*)"
        )
        if path:
            self.sort_path = path
            self.edit_sort.setText(path)

    def try_auto_sort_path(self, tsv_path: str):
        """Если sort.txt в той же папке — подставляем, но не затираем уже выбранный вручную."""
        if self.sort_path:
            return
        candidate = os.path.join(os.path.dirname(tsv_path), "sort.txt")
        if os.path.isfile(candidate):
            self.sort_path = candidate
            self.edit_sort.setText(candidate)

    # --- log helper ---
    def append_log(self, text: str):
        self.log.append(text)
        self.log.moveCursor(QTextCursor.End)

    # --- core actions ---
    def get_path_by_key(self, key: str) -> str:
        if key == "A":
            return self.edit_a.text().strip()
        return self.edit_b.text().strip()

    def ensure_paths(self) -> Tuple[str, str, str]:
        src = self.get_path_by_key(self.combo_source.currentData())
        dst = self.get_path_by_key(self.combo_target.currentData())

        if not src:
            raise ValueError("Не выбран исходный TSV (A или B).")
        if not dst:
            raise ValueError("Не выбран целевой TSV (A или B).")
        if not os.path.isfile(src):
            raise FileNotFoundError(f"Исходный TSV не найден: {src}")
        if not os.path.isfile(dst):
            raise FileNotFoundError(f"Целевой TSV не найден: {dst}")

        sort_path = self.sort_path or self.edit_sort.text().strip()
        if not sort_path:
            raise ValueError("Не указан sort.txt.")
        if not os.path.isfile(sort_path):
            # предложим выбрать
            reply = QtWidgets.QMessageBox.question(
                self,
                "sort.txt",
                f"sort.txt не найден по пути:\n{sort_path}\nВыбрать другой файл?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes,
            )
            if reply == QtWidgets.QMessageBox.Yes:
                self.browse_sort()
                sort_path = self.sort_path or self.edit_sort.text().strip()
            if not sort_path or not os.path.isfile(sort_path):
                raise FileNotFoundError("sort.txt не найден.")
        self.sort_path = sort_path
        self.edit_sort.setText(sort_path)
        return src, dst, sort_path

    def make_output_path(self, src_path: str) -> str:
        folder, name = os.path.split(src_path)
        base, ext = os.path.splitext(name)
        return os.path.join(folder, f"{base}_sort{ext or '.tsv'}")

    def handle_full_sort(self):
        self.run_sort(filter_only=False)

    def handle_filter_sort(self):
        self.run_sort(filter_only=True)

    def handle_sort_translations(self):
        """Обработчик сортировки по переводам."""
        try:
            # Используем целевой файл для сортировки
            dst = self.get_path_by_key(self.combo_target.currentData())
            
            if not dst:
                raise ValueError("Не выбран целевой TSV (A или B).")
            if not os.path.isfile(dst):
                raise FileNotFoundError(f"Целевой TSV не найден: {dst}")
            
            # Загружаем файл
            header, rows = load_tsv(dst)
            if not rows:
                QtWidgets.QMessageBox.information(self, "Сортировка", "Файл пуст или без данных.")
                return
            
            id_idx = find_column_index(header, "ID", 0)
            text_idx = find_column_index(header, "OriginalText", 1 if len(header) > 1 else 0)
            
            # Сортируем по переводам
            sorted_rows = sort_by_translations(rows, text_idx, id_idx)
            
            # Сохраняем результат
            out_path = self.make_output_path(dst)
            save_tsv(out_path, header, sorted_rows)
            
            msg = (
                f"Готово. Файл: {os.path.basename(dst)}. "
                f"Всего строк: {len(rows)}, отсортировано: {len(sorted_rows)}. "
                f"Дубли сгруппированы по языкам (кириллица → английский → китайский). "
                f"Итоговый файл: {out_path}"
            )
            self.append_log(msg)
            QtWidgets.QMessageBox.information(self, "Сортировка по переводам", msg)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Ошибка", str(e))
            self.append_log(f"Ошибка: {e}")

    def run_sort(self, filter_only: bool):
        try:
            src, dst, sort_path = self.ensure_paths()

            # Загружаем исходный файл для построения индекса
            header_src, rows_src = load_tsv(src)
            if not rows_src:
                QtWidgets.QMessageBox.information(self, "Сортировка", "Исходный файл пуст или без данных.")
                return

            id_idx_src = find_column_index(header_src, "ID", 0)
            text_idx_src = find_column_index(header_src, "OriginalText", 1 if len(header_src) > 1 else 0)

            rules = load_sort_rules(sort_path)
            if not rules:
                QtWidgets.QMessageBox.warning(self, "Сортировка", "В sort.txt нет правил.")
                return

            src_index, matched_src = build_source_index(rows_src, text_idx_src, id_idx_src, rules)

            # Загружаем целевой файл, который будем сортировать/фильтровать
            header_dst, rows_dst = load_tsv(dst)
            if not rows_dst:
                QtWidgets.QMessageBox.information(self, "Сортировка", "Целевой файл пуст или без данных.")
                return

            id_idx_dst = find_column_index(header_dst, "ID", 0)
            text_idx_dst = find_column_index(header_dst, "OriginalText", 1 if len(header_dst) > 1 else 0)

            prepared = []
            kept = 0
            matched_dst = 0
            big_rank = len(rows_dst) + len(src_index) + 10  # для не найденных ID

            for row in rows_dst:
                rid = row[id_idx_dst] if len(row) > id_idx_dst else ""
                text = row[text_idx_dst] if len(row) > text_idx_dst else ""
                if rid in src_index:
                    rank, score, text_low_src = src_index[rid]
                    matched_dst += 1 if score > 0 else 0
                else:
                    rank, score, text_low_src = big_rank, 0, ""

                if filter_only and score == 0:
                    continue

                # сортируем по рангу исходника, затем по тексту целевого для стабильности
                key = (rank, text.lower(), rid)
                prepared.append((key, row))
                kept += 1

            if filter_only and kept == 0:
                QtWidgets.QMessageBox.information(
                    self, "Результат", "Совпадений по словам из sort.txt не найдено."
                )
                return

            prepared.sort(key=lambda x: x[0])
            sorted_rows = [row for _, row in prepared]

            out_path = self.make_output_path(dst)
            save_tsv(out_path, header_dst, sorted_rows)

            msg = (
                f"Готово. Исходный файл: {os.path.basename(src)}. "
                f"Целевой файл: {os.path.basename(dst)}. "
                f"Всего строк в целевом: {len(rows_dst)}, сохранено: {len(sorted_rows)}. "
                f"Совпало по словам в исходнике: {matched_src}, совпало ID в целевом: {matched_dst}. "
                f"Итоговый файл: {out_path}"
            )
            self.append_log(msg)
            QtWidgets.QMessageBox.information(self, "Сортировка", msg)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Ошибка", str(e))
            self.append_log(f"Ошибка: {e}")


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()


import os
import sys

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('SimpleSound')
    app.setWindowIcon(QIcon())
    app.setFont(QFont('Segoe UI', 10))

    style_path = os.path.join(os.path.dirname(__file__), 'styles', 'theme.qss')
    if os.path.exists(style_path):
        with open(style_path, 'r', encoding='utf-8') as f:
            app.setStyleSheet(f.read())
        print('Design loaded successfully!')
    else:
        print(f'Warning: Style file not found at {style_path}')

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

import os
from PyQt6.QtGui import QFont, QPalette, QColor

_ASSETS = os.path.dirname(os.path.abspath(__file__))

def _asset_url(name: str) -> str:
    return os.path.join(_ASSETS, "assets", name).replace("\\", "/")


# ── Color tokens ──────────────────────────────────────────────────────────────
# Page / surface
PAGE_BG    = "#F3F4F6"
CARD_BG    = "#FFFFFF"

# Borders
BORDER     = "#E5E7EB"
BORDER_HVR = "#9CA3AF"

# Primary action
PRIMARY    = "#2563EB"
PRIMARY_BG = "#EFF6FF"
PRIMARY_BD = "#BFDBFE"

# Text
TEXT_MAIN  = "#111827"
TEXT_SUB   = "#6B7280"
TEXT_OFF   = "#D1D5DB"

# Semantic
SUCCESS    = "#16A34A"
WARNING    = "#D97706"
ERROR      = "#DC2626"


# ── Main stylesheet ────────────────────────────────────────────────────────────
STYLESHEET = f"""

/* === BASE ===
   QWidget's transparent background must be declared BEFORE the
   QMainWindow/QDialog rule below: Qt stylesheets break same-specificity
   ties by source order (last wins), so QMainWindow/QDialog needs to come
   second to keep its solid background from being overridden by the
   generic QWidget rule (both are single type-selectors, same specificity).
   Getting this backwards left QMainWindow/QDialog "transparent", which
   Windows 11 dark mode then composited as solid black. */
QWidget {{
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    font-size: 13px;
    color: {TEXT_MAIN};
    background-color: transparent;
}}
QMainWindow, QDialog {{
    background-color: {PAGE_BG};
}}

/* === MENU BAR === */
QMenuBar {{
    background-color: {CARD_BG};
    border-bottom: 1px solid {BORDER};
    padding: 2px 4px;
    spacing: 2px;
}}
QMenuBar::item {{
    padding: 6px 12px;
    border-radius: 4px;
    background-color: transparent;
}}
QMenuBar::item:selected {{
    background-color: {PRIMARY_BG};
    color: {PRIMARY};
}}
QMenu {{
    background-color: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 8px 32px 8px 12px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {PRIMARY_BG};
    color: {PRIMARY};
}}
QMenu::separator {{
    height: 1px;
    background-color: {BORDER};
    margin: 4px 8px;
}}

/* === GROUP BOX  →  white card === */
QGroupBox {{
    background-color: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: 10px;
    margin-top: 14px;
    padding-top: 4px;
    font-size: 11px;
    font-weight: 700;
    color: {TEXT_SUB};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    top: -1px;
    padding: 0 4px;
    background-color: {CARD_BG};
}}

/* === PUSH BUTTON === */
QPushButton {{
    background-color: {CARD_BG};
    color: {TEXT_MAIN};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 10px 16px;
    font-size: 13px;
    font-weight: 500;
    text-align: center;
    margin: 2px 0;
}}
QPushButton:hover {{
    background-color: #F9FAFB;
    border-color: {BORDER_HVR};
}}
QPushButton:pressed {{
    background-color: #F3F4F6;
}}
QPushButton:disabled {{
    color: {TEXT_OFF};
    background-color: #F9FAFB;
    border-color: #F3F4F6;
}}

/* === LINE EDIT === */
QLineEdit {{
    background-color: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
    color: {TEXT_MAIN};
    selection-background-color: {PRIMARY};
    selection-color: {CARD_BG};
    min-height: 28px;
}}
QLineEdit:focus {{
    border: 2px solid {PRIMARY};
}}
QLineEdit:disabled {{
    background-color: #F9FAFB;
    color: {TEXT_OFF};
}}

/* === COMBO BOX === */
QComboBox {{
    background-color: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
    color: {TEXT_MAIN};
    min-height: 28px;
}}
QComboBox:hover  {{ border-color: {BORDER_HVR}; }}
QComboBox:focus  {{ border: 2px solid {PRIMARY}; }}
QComboBox::drop-down {{
    border: none;
    padding-right: 8px;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    selection-background-color: {PRIMARY_BG};
    selection-color: {PRIMARY};
    outline: none;
}}

/* === CHECK BOX === */
QCheckBox {{
    spacing: 8px;
    color: {TEXT_MAIN};
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 2px solid {BORDER};
    border-radius: 4px;
    background-color: {CARD_BG};
}}
QCheckBox::indicator:hover   {{ border-color: {BORDER_HVR}; }}
QCheckBox::indicator:checked {{
    background-color: {PRIMARY};
    border-color: {PRIMARY};
    image: url({_asset_url("checkmark.svg")});
}}
QCheckBox::indicator:checked:disabled {{
    background-color: {TEXT_OFF};
    border-color: {TEXT_OFF};
}}
QCheckBox::indicator:unchecked:disabled {{
    background-color: #F3F4F6;
    border-color: #E5E7EB;
}}

/* === RADIO BUTTON === */
QRadioButton {{
    spacing: 8px;
    color: {TEXT_MAIN};
}}
QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 2px solid {BORDER};
    border-radius: 8px;
    background-color: {CARD_BG};
}}
QRadioButton::indicator:hover {{ border-color: {BORDER_HVR}; }}
QRadioButton::indicator:checked {{
    border: 2px solid {BORDER};
    background-color: qradialgradient(
        cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 {PRIMARY}, stop:0.5 {PRIMARY}, stop:0.55 transparent, stop:1 transparent
    );
}}
QRadioButton::indicator:checked:disabled {{
    border-color: {TEXT_OFF};
    background-color: qradialgradient(
        cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 {TEXT_OFF}, stop:0.5 {TEXT_OFF}, stop:0.55 transparent, stop:1 transparent
    );
}}
QRadioButton::indicator:unchecked:disabled {{
    background-color: #F3F4F6;
    border-color: #E5E7EB;
}}

/* === LABEL === */
QLabel {{
    background-color: transparent;
    color: {TEXT_MAIN};
}}

/* === SCROLL BAR === */
QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 3px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: {BORDER_HVR}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

QScrollBar:horizontal {{
    background: transparent;
    height: 6px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: 3px;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{ background: {BORDER_HVR}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* === MESSAGE BOX === */
QMessageBox {{ background-color: {CARD_BG}; }}
QMessageBox QLabel {{ color: {TEXT_MAIN}; }}
QMessageBox QPushButton {{
    min-width: 80px;
    text-align: center;
    padding-left: 0;
}}

/* === SPIN BOX === */
QSpinBox, QDoubleSpinBox {{
    background-color: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 13px;
    min-height: 28px;
    selection-background-color: {PRIMARY};
    selection-color: {CARD_BG};
}}
QSpinBox:focus, QDoubleSpinBox:focus {{ border: 2px solid {PRIMARY}; }}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    border: none;
    background: transparent;
    width: 18px;
}}

/* === TAB WIDGET === */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    background-color: {CARD_BG};
}}
QTabBar::tab {{
    background-color: transparent;
    border: none;
    padding: 8px 16px;
    font-size: 13px;
    color: {TEXT_SUB};
}}
QTabBar::tab:selected {{
    color: {PRIMARY};
    border-bottom: 2px solid {PRIMARY};
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{ color: {TEXT_MAIN}; }}

/* === TOOL TIP === */
QToolTip {{
    background-color: {TEXT_MAIN};
    color: {CARD_BG};
    border: none;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}}

/* === PROGRESS BAR === */
QProgressBar {{
    background-color: #F3F4F6;
    border: none;
    border-radius: 4px;
    height: 6px;
    text-align: center;
    font-size: 11px;
    color: transparent;
}}
QProgressBar::chunk {{
    background-color: {PRIMARY};
    border-radius: 4px;
}}

/* === STATUS BAR (QMainWindow) === */
QStatusBar {{
    background-color: {CARD_BG};
    border-top: 1px solid {BORDER};
    color: {TEXT_SUB};
    font-size: 12px;
}}

/* === LAUNCHER BUTTONS (main window section list items) ===
   Set QPushButton property "launcher" = True to apply.
   Set property "active" = True when the associated window is open. */
QPushButton[launcher=true] {{
    background-color: transparent;
    border: none;
    border-bottom: 1px solid {BORDER};
    border-radius: 0;
    margin: 4px;
    padding: 4px 12px;
    font-size: 13px;
    font-weight: 500;
    color: {TEXT_MAIN};
    text-align: left;
}}
QPushButton[list_first=true] {{
    border-top: 1px solid {BORDER};
}}
QPushButton[list_last=true] {{}}
QPushButton[launcher=true]:hover {{
    background-color: #F9FAFB;
}}
QPushButton[launcher=true]:pressed {{
    background-color: {PAGE_BG};
}}
QPushButton[launcher=true]:disabled {{
    color: {TEXT_OFF};
    background-color: transparent;
    border-bottom-color: {BORDER};
}}
QPushButton[launcher=true][active=true] {{
    background-color: {PRIMARY_BG};
    color: {PRIMARY};
    font-weight: 600;
    border-bottom-color: {PRIMARY_BD};
}}
QPushButton[list_last=true][active=true] {{
    border-bottom: none;
}}
QPushButton[launcher=true][active=true]:hover {{
    background-color: #DBEAFE;
}}

"""


def apply(app) -> None:
    app.setStyleSheet(STYLESHEET)
    font = QFont("Segoe UI")
    font.setPointSize(10)
    app.setFont(font)

    # Explicit light palette so transparent QSS backgrounds render on
    # PAGE_BG rather than the dark system Window colour.
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,          QColor(PAGE_BG))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor(TEXT_MAIN))
    palette.setColor(QPalette.ColorRole.Base,            QColor(CARD_BG))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(PAGE_BG))
    palette.setColor(QPalette.ColorRole.Text,            QColor(TEXT_MAIN))
    palette.setColor(QPalette.ColorRole.BrightText,      QColor(CARD_BG))
    palette.setColor(QPalette.ColorRole.Button,          QColor(CARD_BG))
    palette.setColor(QPalette.ColorRole.ButtonText,      QColor(TEXT_MAIN))
    palette.setColor(QPalette.ColorRole.Link,            QColor(PRIMARY))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor(PRIMARY))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(CARD_BG))
    palette.setColor(QPalette.ColorRole.ToolTipBase,     QColor(TEXT_MAIN))
    palette.setColor(QPalette.ColorRole.ToolTipText,     QColor(CARD_BG))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_OFF))
    app.setPalette(palette)

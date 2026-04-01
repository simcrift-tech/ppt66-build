import sys
import cv2
import numpy as np
import os
import json
import fitz
import re
import copy
import tempfile
import urllib.request
import random
import hashlib

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, 
                             QVBoxLayout, QLabel, QPushButton, QFileDialog, 
                             QFrame, QLineEdit, QDateEdit, QScrollArea, 
                             QTableWidget, QTableWidgetItem, QComboBox, QGroupBox, 
                             QCheckBox, QTimeEdit, QInputDialog, QSpinBox, QHeaderView, QDialog, QMessageBox)
from PyQt6.QtGui import QImage, QPixmap, QColor, QPainter, QPen, QCursor, QFont, QLinearGradient
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QTimer, QTime, QDate, QDateTime, QRect, QRectF, QUrl

# [필수] 웹 엔진 및 파워포인트 라이브러리
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineSettings
except ImportError:
    print("PyQt6-WebEngine이 필요합니다. 터미널에 'python -m pip install PyQt6-WebEngine'을 실행하세요.")
    sys.exit(1)

try:
    import comtypes.client
except ImportError:
    print("PowerPoint 지원을 위해 'python -m pip install comtypes'가 필요합니다.")

os.environ["OPENCV_VIDEOIO_LOG_LEVEL"] = "0"


# --- [사용자 계정 관리 유틸리티] ---
USERS_FILE = "users.json"

def hash_password(password):
    """비밀번호를 SHA-256 해시로 변환합니다."""
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def load_users():
    """users.json 파일에서 사용자 정보를 불러옵니다."""
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}

def save_users(users):
    """사용자 정보를 users.json 파일에 저장합니다."""
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, ensure_ascii=False, indent=4)
    except IOError:
        print("오류: 사용자 정보를 파일에 저장할 수 없습니다.")



# --- [날씨 정보 파싱 스레드] ---
class WeatherThread(QThread):
    weather_signal = pyqtSignal(str)
    def run(self):
        try:
            req = urllib.request.Request("https://wttr.in/Seoul?format=%c+%t", headers={'User-Agent': 'Mozilla/5.0'})
            w = urllib.request.urlopen(req, timeout=10).read().decode('utf-8').strip() # 타임아웃 10초로 늘림
            self.weather_signal.emit(w)
        except:
            self.weather_signal.emit("날씨 정보 없음")

# --- [PowerPoint 변환 유틸리티] ---
def convert_pptx_to_images(pptx_path):
    temp_dir = tempfile.mkdtemp()
    abs_pptx = os.path.abspath(pptx_path)
    try:
        powerpoint = comtypes.client.CreateObject("Powerpoint.Application")
        deck = powerpoint.Presentations.Open(abs_pptx, WithWindow=False)
        for i, slide in enumerate(deck.Slides):
            image_path = os.path.join(temp_dir, f"slide_{i+1:03d}.png")
            slide.Export(image_path, "PNG")
        deck.Close()
        powerpoint.Quit()
        return [os.path.join(temp_dir, f) for f in sorted(os.listdir(temp_dir))]
    except Exception as e:
        print(f"PPT 변환 실패: {e}")
        return []

# --- [영상 재생 엔진] ---
class VideoThread(QThread):
    change_pixmap_signal = pyqtSignal(QImage)
    finished_signal = pyqtSignal()
    def __init__(self):
        super().__init__()
        self.path = None; self._run = False; self.rep = True
    def set_path(self, p, r=True):
        if self.path == p and self._run: return
        self.stop(); self.path = p; self.rep = r; self._run = True; self.start()
    def run(self):
        if not self.path or not os.path.exists(self.path): return
        cap = cv2.VideoCapture(self.path)
        while self._run:
            ret, frame = cap.read()
            if ret:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb.shape
                img = QImage(rgb.data, w, h, w*ch, QImage.Format.Format_RGB888)
                self.change_pixmap_signal.emit(img.copy()); self.msleep(30)
            else:
                if self.rep: cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                else: self.finished_signal.emit(); break
        cap.release()
    def stop(self): self._run = False; self.wait()

# --- [미디어 섹션 패널] ---
class DynamicSection(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background-color: black;")
        self.layout = QVBoxLayout(self); self.layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(); self.label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.layout.addWidget(self.label)
        self.webview = QWebEngineView(); self.webview.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        self.layout.addWidget(self.webview); self.webview.hide()
        self.vt = VideoThread(); self.vt.change_pixmap_signal.connect(self.update_frame); self.vt.finished_signal.connect(self.play_next)
        self.playlist = []; self.current_idx = 0; self.is_repeat = True; self.duration = 0
        self.duration_timer = QTimer(self); self.duration_timer.timeout.connect(self.play_next)

    def update_frame(self, img):
        self.label.setPixmap(QPixmap.fromImage(img).scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio))

    def play(self, paths, r=True, duration=0):
        if not paths: return
        if isinstance(paths, str): paths = [paths]
        final_playlist = []
        for p in paths:
            if p.lower().endswith(('.pptx', '.ppt')):
                slides = convert_pptx_to_images(p)
                final_playlist.extend(slides)
            else:
                final_playlist.append(p)
        self.playlist = final_playlist; self.is_repeat = r; self.duration = duration; self.current_idx = 0; self._play_current()

    def _play_current(self):
        if not self.playlist: return
        p = self.playlist[self.current_idx]
        self.vt.stop(); self.duration_timer.stop()
        is_video = False
        
        if p.startswith("http"):
            self.label.hide(); self.webview.show()
            if "youtube.com" in p or "youtu.be" in p:
                vid = self.extract_yt_id(p)
                embed_url = f"https://www.youtube.com/embed/{vid}?autoplay=1&mute=1&loop=1&playlist={vid}&controls=0"
                html = f'<!DOCTYPE html><html style="width:100%; height:100%; margin:0;"><body style="width:100%; height:100%; margin:0; background:black; overflow:hidden;"><iframe width="100%" height="100%" src="{embed_url}" frameborder="0" allow="autoplay; encrypted-media" allowfullscreen></iframe></body></html>'
                self.webview.setHtml(html, QUrl("https://www.youtube.com"))
            else: self.webview.setUrl(QUrl(p))
        else:
            self.webview.hide(); self.webview.setUrl(QUrl("about:blank")); self.label.show()
            ext = p.lower().split('.')[-1]
            if ext in ['mp4', 'avi', 'mov']:
                is_video = True; rep_vid = (len(self.playlist) == 1) and self.is_repeat; self.vt.set_path(p, rep_vid)
            elif ext == 'pdf': self.load_pdf(p)
            else: self.load_img(p)
            
        if self.duration > 0: self.duration_timer.start(self.duration * 1000)
        elif len(self.playlist) > 1 and not is_video: self.duration_timer.start(5000)

    def play_next(self):
        if not self.playlist: return
        self.current_idx += 1
        if self.current_idx >= len(self.playlist):
            if self.is_repeat: self.current_idx = 0
            else: self.stop(); return
        self._play_current()

    def extract_yt_id(self, url):
        match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", url)
        return match.group(1) if match else ""
    def load_pdf(self, p):
        try:
            doc = fitz.open(p); page = doc[0]; pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
            self.update_frame(img); doc.close()
        except: pass
    def load_img(self, p):
        try:
            img_arr = np.fromfile(p, np.uint8); img = cv2.imdecode(img_arr, cv2.IMREAD_COLOR); img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            qimg = QImage(img.data, img.shape[1], img.shape[0], img.shape[1]*3, QImage.Format.Format_RGB888)
            self.update_frame(qimg)
        except: pass
    def stop(self): 
        self.vt.stop(); self.duration_timer.stop(); self.label.clear(); self.webview.setUrl(QUrl("about:blank")); self.webview.hide(); self.playlist = []

# --- [로그인 다이얼로그] ---
class LoginDialog(QDialog):
    def __init__(self, users_data, parent=None):
        super().__init__(parent)
        self.users = users_data
        self.user_info = None
        self.setWindowTitle("로그인")
        self.setModal(True)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; }
            QLabel { color: #E0E0E0; font-family: 'Malgun Gothic'; font-size: 14px; }
            QLineEdit { background: #2A2A2A; border: 1px solid #444; color: white; border-radius: 4px; padding: 8px; font-size: 14px; }
            QPushButton { background: #333; border: 1px solid #555; border-radius: 4px; padding: 8px; color: #AAA; font-weight: bold; }
            QPushButton:hover { background: #444; }
            QPushButton#LoginBtn { background: #2196F3; color: white; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("사용자 이름")
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("비밀번호")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.message_label = QLabel("")
        self.message_label.setStyleSheet("color: #CF6679;")

        button_layout = QHBoxLayout()
        self.login_button = QPushButton("로그인")
        self.login_button.setObjectName("LoginBtn")
        self.cancel_button = QPushButton("취소")

        button_layout.addWidget(self.cancel_button)
        button_layout.addStretch()
        button_layout.addWidget(self.login_button)

        layout.addWidget(QLabel("사용자 이름:"))
        layout.addWidget(self.username_input)
        layout.addWidget(QLabel("비밀번호:"))
        layout.addWidget(self.password_input)
        layout.addWidget(self.message_label)
        layout.addLayout(button_layout)

        self.login_button.clicked.connect(self.attempt_login)
        self.cancel_button.clicked.connect(self.reject)
        self.username_input.returnPressed.connect(self.password_input.setFocus)
        self.password_input.returnPressed.connect(self.attempt_login)

    def attempt_login(self):
        username = self.username_input.text().strip()
        password = self.password_input.text().strip()
        
        if not username or not password:
            self.message_label.setText("사용자 이름과 비밀번호를 모두 입력하세요.")
            return

        user_data = self.users.get(username)
        if user_data and user_data['password'] == hash_password(password):
            self.user_info = {'username': username, 'role': user_data['role']}
            self.accept()
        else:
            self.message_label.setText("사용자 이름 또는 비밀번호가 잘못되었습니다.")
            self.password_input.clear()

# --- [사용자 관리 다이얼로그] ---
class UserManagementDialog(QDialog):
    def __init__(self, users_data, parent=None):
        super().__init__(parent)
        self.users = copy.deepcopy(users_data) # 원본을 수정하지 않도록 깊은 복사
        self.setWindowTitle("사용자 계정 관리")
        self.setMinimumSize(500, 400)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; color: #E0E0E0; }
            QTableWidget { background-color: #2A2A2A; color: white; gridline-color: #444; }
            QHeaderView::section { background-color: #333; color: white; padding: 4px; border: 1px solid #444; }
            QPushButton { background: #333; border: 1px solid #555; padding: 8px; color: #AAA; border-radius: 4px; }
            QPushButton:hover { background: #444; }
            QPushButton#SaveBtn { background: #2196F3; color: white; font-weight: bold; }
        """)

        layout = QVBoxLayout(self)
        
        self.user_table = QTableWidget()
        self.user_table.setColumnCount(2)
        self.user_table.setHorizontalHeaderLabels(["사용자 이름", "역할"])
        self.user_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.user_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.user_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        
        self.populate_table()
        layout.addWidget(self.user_table)
        
        button_layout = QHBoxLayout()
        self.add_button = QPushButton("➕ 사용자 추가")
        self.delete_button = QPushButton("🗑️ 선택 사용자 삭제")
        self.change_pass_button = QPushButton("🔑 비밀번호 변경")
        self.save_button = QPushButton("저장 후 닫기")
        self.save_button.setObjectName("SaveBtn")

        button_layout.addWidget(self.add_button)
        button_layout.addWidget(self.delete_button)
        button_layout.addWidget(self.change_pass_button)
        button_layout.addStretch()
        button_layout.addWidget(self.save_button)
        layout.addLayout(button_layout)
        
        self.add_button.clicked.connect(self.add_user)
        self.delete_button.clicked.connect(self.delete_user)
        self.change_pass_button.clicked.connect(self.change_password)
        self.save_button.clicked.connect(self.accept)

    def populate_table(self):
        self.user_table.setRowCount(0)
        # 사용자 이름을 기준으로 정렬하여 보여줍니다.
        for username, data in sorted(self.users.items()):
            row_position = self.user_table.rowCount()
            self.user_table.insertRow(row_position)
            self.user_table.setItem(row_position, 0, QTableWidgetItem(username))
            self.user_table.setItem(row_position, 1, QTableWidgetItem(data['role']))

    def add_user(self):
        """'사용자 추가' 다이얼로그를 열고 새 사용자를 추가합니다."""
        dialog = AddUserDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            details = dialog.get_user_details()
            if details:
                username = details['username']
                if not username:
                    QMessageBox.warning(self, "입력 오류", "사용자 이름은 비워둘 수 없습니다.")
                    return

                if username in self.users:
                    QMessageBox.warning(self, "오류", "이미 존재하는 사용자 이름입니다.")
                    return
                
                self.users[username] = {
                    'password': hash_password(details['password']),
                    'role': details['role']
                }
                self.populate_table()

    def delete_user(self):
        """선택된 사용자를 삭제합니다."""
        selected_items = self.user_table.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "알림", "삭제할 사용자를 목록에서 선택하세요.")
            return
            
        username_to_delete = selected_items[0].text()
        
        if username_to_delete == 'admin':
            QMessageBox.warning(self, "삭제 불가", "'admin' 사용자는 시스템의 기본 관리자이므로 삭제할 수 없습니다.")
            return

        reply = QMessageBox.question(self, '삭제 확인', f"'{username_to_delete}' 사용자를 정말로 삭제하시겠습니까?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)

        if reply == QMessageBox.StandardButton.Yes:
            if username_to_delete in self.users:
                del self.users[username_to_delete]
                self.populate_table()

    def change_password(self):
        """선택된 사용자의 비밀번호를 변경합니다."""
        selected_items = self.user_table.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "알림", "비밀번호를 변경할 사용자를 목록에서 선택하세요.")
            return
        
        username = selected_items[0].text()
        dialog = ChangePasswordDialog(username, self)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_password = dialog.get_new_password()
            if new_password:
                self.users[username]['password'] = hash_password(new_password)
                QMessageBox.information(self, "성공", f"'{username}' 사용자의 비밀번호가 변경되었습니다.")
            else:
                QMessageBox.warning(self, "입력 오류", "비밀번호가 비어있거나 일치하지 않습니다.")


class AddUserDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("새 사용자 추가")
        self.setModal(True)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; color: #E0E0E0; }
            QLineEdit, QComboBox { background: #2A2A2A; border: 1px solid #444; color: white; padding: 5px; }
            QPushButton { background: #333; border: 1px solid #555; padding: 8px; color: #AAA; }
        """)

        layout = QVBoxLayout(self)
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("새 사용자 이름")
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("새 비밀번호")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.role_combo = QComboBox()
        self.role_combo.addItems(["User", "Admin"])

        layout.addWidget(QLabel("사용자 이름:"))
        layout.addWidget(self.username_input)
        layout.addWidget(QLabel("비밀번호:"))
        layout.addWidget(self.password_input)
        layout.addWidget(QLabel("역할:"))
        layout.addWidget(self.role_combo)

        button_box = QHBoxLayout()
        self.ok_button = QPushButton("추가")
        self.cancel_button = QPushButton("취소")
        button_box.addStretch()
        button_box.addWidget(self.cancel_button)
        button_box.addWidget(self.ok_button)
        layout.addLayout(button_box)

        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

    def get_user_details(self):
        username = self.username_input.text().strip()
        password = self.password_input.text().strip()
        role = self.role_combo.currentText()
        if username and password:
            return {'username': username, 'password': password, 'role': role}
        return None

class ChangePasswordDialog(QDialog):
    def __init__(self, username, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"'{username}' 비밀번호 변경")
        self.setModal(True)
        self.setStyleSheet("""
            QDialog { background-color: #1E1E1E; color: #E0E0E0; }
            QLineEdit { background: #2A2A2A; border: 1px solid #444; color: white; padding: 5px; }
            QPushButton { background: #333; border: 1px solid #555; padding: 8px; color: #AAA; }
        """)

        layout = QVBoxLayout(self)
        self.new_pass_input = QLineEdit()
        self.new_pass_input.setPlaceholderText("새 비밀번호")
        self.new_pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        
        self.confirm_pass_input = QLineEdit()
        self.confirm_pass_input.setPlaceholderText("새 비밀번호 확인")
        self.confirm_pass_input.setEchoMode(QLineEdit.EchoMode.Password)

        layout.addWidget(QLabel("새 비밀번호:"))
        layout.addWidget(self.new_pass_input)
        layout.addWidget(QLabel("비밀번호 확인:"))
        layout.addWidget(self.confirm_pass_input)

        button_box = QHBoxLayout()
        self.ok_button = QPushButton("변경")
        self.cancel_button = QPushButton("취소")
        button_box.addStretch()
        button_box.addWidget(self.cancel_button)
        button_box.addWidget(self.ok_button)
        layout.addLayout(button_box)

        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

    def get_new_password(self):
        if self.new_pass_input.text() and self.new_pass_input.text() == self.confirm_pass_input.text():
            return self.new_pass_input.text()
        return None

# --- [재생 창 (로고 글자 흰색 변환 기능 추가)] ---
class PlaybackWindow(QMainWindow):
    esc_signal = pyqtSignal()
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Signage Display"); self.setStyleSheet("background-color: #000;"); self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.secs = []; self.cont = QWidget(); self.setCentralWidget(self.cont); self.cur_layout_id = ""
        
        self.bg_label = QLabel(self.cont)
        self.bg_label.hide()
        self.current_weather_type = 'default'

        self.logo_label = QLabel(self.cont); self.logo_label.setStyleSheet("background: transparent;")
        
        # [핵심 수정] 로고 이미지를 로드하고 검은색 글자를 흰색으로 변환
        logo_path = "lg_logo.png"
        if os.path.exists(logo_path):
            try:
                # 1. OpenCV로 이미지를 투명도 채널(IMREAD_UNCHANGED)을 포함해 불러옵니다. (BGRA 형태)
                img_bgra = cv2.imread(logo_path, cv2.IMREAD_UNCHANGED)
                
                if img_bgra is not None:
                    # 2. 채널 분리
                    b, g, r, a = cv2.split(img_bgra)
                    
                    # 3. '검은색에 가까운' 영역 찾아내기 (글자 부분)
                    # 글자가 완전 검은색(0)이 아니더라도 어두우면(예: 30 미만) 잡히도록 임계값을 줍니다.
                    # AND 연산을 위해 투명하지 않은 영역(a > 200)만 대상으로 삼아야 배경이 안 바뀝니다.
                    color_threshold = 30 # 이 숫자보다 작으면 검은색으로 간주
                    black_mask = (b < color_threshold) & (g < color_threshold) & (r < color_threshold) & (a > 200)
                    
                    # 4. 검은색 마스크 영역의 B, G, R 채널을 모두 255(흰색)로 바꿉니다.
                    b[black_mask] = 255
                    g[black_mask] = 255
                    r[black_mask] = 255
                    
                    # 5. 채널 다시 병합 (변환된 흰색 글자 + 원본 투명도)
                    modified_bgra = cv2.merge((b, g, r, a))
                    
                    # 6. OpenCV (BGRA) -> PyQt (ARGB32) 형식으로 변환하여 QImage 생성
                    height, width, channel = modified_bgra.shape
                    bytes_per_line = 4 * width
                    qimg = QImage(modified_bgra.data, width, height, bytes_per_line, QImage.Format.Format_ARGB32)
                    
                    # 7. QPixmap으로 변환 후 스케일링
                    pix = QPixmap.fromImage(qimg).scaled(180, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                    self.logo_label.setPixmap(pix); self.logo_label.resize(pix.width(), pix.height())
                else: raise Exception("imread fail")
            except Exception as e:
                # 이미지 가공 실패 시 원본 로드 시도 (폴백)
                print(f"로고 색상 변환 실패, 원본 사용: {e}")
                pix = QPixmap(logo_path).scaled(180, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self.logo_label.setPixmap(pix); self.logo_label.resize(pix.width(), pix.height())
        else:
            # 로고 파일이 없으면 텍스트로 표시
            self.logo_label.setText("LG"); self.logo_label.setStyleSheet("color: #E0E0E0; font-size: 100px; font-weight: bold;")
            self.logo_label.resize(180, 120); self.logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo_label.hide()

        self.overlay_time = QLabel(self.cont)
        self.overlay_time.setStyleSheet("color: #E0E0E0; font-size: 35px; font-family: 'Malgun Gothic'; font-weight: bold; background-color: rgba(0,0,0,100); padding: 15px; border-radius: 10px;")
        self.overlay_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay_time.hide()
        
        self.overlay_weather = QLabel(self.cont)
        self.overlay_weather.setStyleSheet("color: #E0E0E0; font-size: 30px; font-family: 'Malgun Gothic'; font-weight: bold; background-color: rgba(0,0,0,100); padding: 15px; border-radius: 10px;")
        self.overlay_weather.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay_weather.hide()

        # [수정] 통합 머무름 타이머 (180,000ms = 3분) - 로고, 시간, 날씨 통합
        self.dwell_timer = QTimer(self)
        self.dwell_timer.timeout.connect(self.update_all_overlay_positions)

        self.clock_timer = QTimer(self); self.clock_timer.timeout.connect(self.update_time); self.clock_timer.start(1000)
        self.weather_timer = QTimer(self); self.weather_timer.timeout.connect(self.fetch_weather); self.weather_timer.start(1800000) 
        self.fetch_weather() 

    def fetch_weather(self):
        self.wt = WeatherThread()
        self.wt.weather_signal.connect(self.on_weather_fetched)
        self.wt.start()

    def parse_weather_type(self, w_str):
        if any(e in w_str for e in ['☀️', '🌤️', '⛅', '맑음']): return 'sunny'
        if any(e in w_str for e in ['☁️', '🌥️', '🌫', '흐림', '구름']): return 'cloudy'
        if any(e in w_str for e in ['🌧️', '🌦️', '💧', '☔', '비']): return 'rainy'
        if any(e in w_str for e in ['❄️', '🌨️', '⛄', '눈']): return 'snowy'
        return 'default'

    def on_weather_fetched(self, w_data):
        if w_data != "날씨 정보 없음":
            self.overlay_weather.setText(f"서울: {w_data}")
            self.current_weather_type = self.parse_weather_type(w_data)
        else:
            self.overlay_weather.setText(w_data)
        
        self.overlay_weather.adjustSize()
        if self.bg_label.isVisible():
            self.apply_weather_background()

    def apply_weather_background(self):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0: return
        
        self.bg_label.resize(w, h)
        pix = QPixmap(w, h)
        pix.fill(QColor("#121212"))
        
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, 0, h)
        
        if self.current_weather_type == 'sunny':
            grad.setColorAt(0.0, QColor("#4A0E1B")); grad.setColorAt(1.0, QColor("#0D111A")); painter.fillRect(0, 0, w, h, grad)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(255, 50, 80, 20)); painter.drawEllipse(int(w*0.85), int(h*0.15), 400, 400)
            painter.setBrush(QColor(255, 50, 80, 30)); painter.drawEllipse(int(w*0.85)+50, int(h*0.15)+50, 300, 300)
            painter.setBrush(QColor(255, 100, 120, 40)); painter.drawEllipse(int(w*0.85)+100, int(h*0.15)+100, 200, 200)
        elif self.current_weather_type == 'cloudy':
            grad.setColorAt(0.0, QColor("#292533")); grad.setColorAt(1.0, QColor("#100F14")); painter.fillRect(0, 0, w, h, grad)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(50, 45, 60, 150))
            painter.drawEllipse(int(w*0.1), int(h*0.6), 600, 300); painter.drawEllipse(int(w*0.25), int(h*0.5), 500, 400)
            painter.drawEllipse(int(w*0.6), int(h*0.7), 800, 350); painter.drawEllipse(int(w*0.75), int(h*0.55), 600, 300)
        elif self.current_weather_type == 'rainy':
            grad.setColorAt(0.0, QColor("#141E2B")); grad.setColorAt(1.0, QColor("#05070A")); painter.fillRect(0, 0, w, h, grad)
            painter.setPen(QPen(QColor(80, 120, 200, 100), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for i in range(150):
                rx = random.randint(0, w); ry = random.randint(0, h); length = random.randint(20, 50)
                painter.drawLine(rx, ry, rx - int(length*0.3), ry + length)
        elif self.current_weather_type == 'snowy':
            grad.setColorAt(0.0, QColor("#1E2024")); grad.setColorAt(1.0, QColor("#0A0B0C")); painter.fillRect(0, 0, w, h, grad)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(200, 210, 220, 80))
            for i in range(200):
                rx = random.randint(0, w); ry = random.randint(0, h); r = random.randint(4, 10); painter.drawEllipse(rx, ry, r, r)
        else: grad.setColorAt(0.0, QColor("#1A1A1A")); grad.setColorAt(1.0, QColor("#000000")); painter.fillRect(0, 0, w, h, grad)
            
        painter.end()
        self.bg_label.setPixmap(pix)

    def update_time(self):
        now = QDateTime.currentDateTime()
        self.overlay_time.setText(now.toString("yyyy년 MM월 dd일\nHH:mm:ss"))
        self.overlay_time.adjustSize()

    # [핵심 수정] 로고, 시간, 날씨가 서로 겹치지 않게(충돌 안나게) 8개 칸에 무작위 배정하는 통합 함수
    def update_all_overlay_positions(self):
        # 대기 화면 요소가 하나도 안 보이면 실행 안 함
        widgets_to_move = []
        if self.logo_label.isVisible(): widgets_to_move.append(self.logo_label)
        if self.overlay_time.isVisible(): widgets_to_move.append(self.overlay_time)
        if self.overlay_weather.isVisible(): widgets_to_move.append(self.overlay_weather)
        
        if not widgets_to_move: return

        # 정의된 8개 칸의 인덱스 풀 (0~7)
        available_grid_indices = [0, 1, 2, 3, 4, 5, 6, 7]
        
        # 1. 로고 먼저 배정 (제일 중요하니까)
        if self.logo_label.isVisible():
            idx_logo = random.choice(available_grid_indices)
            available_grid_indices.remove(idx_logo) # 배정된 칸은 풀에서 제거
            self.move_widget_to_grid(self.logo_label, idx_logo)

        # 2. 시간 배정
        if self.overlay_time.isVisible():
            if not available_grid_indices: available_grid_indices = [idx_logo] # 혹시 칸이 모자라면 로고칸 재사용 (안겹침 보장 안됨, 엣지케이스)
            idx_time = random.choice(available_grid_indices)
            available_grid_indices.remove(idx_time)
            self.move_widget_to_grid(self.overlay_time, idx_time)

        # 3. 날씨 배정
        if self.overlay_weather.isVisible():
            # 남은 칸이 없으면 로고칸이라도 재사용 시도
            if not available_grid_indices: available_grid_indices = [idx_logo] 
            idx_weather = random.choice(available_grid_indices)
            self.move_widget_to_grid(self.overlay_weather, idx_weather)

    # 특정 위젯을 지정된 그리드 칸 인덱스(0~7)의 좌표로 계산해 이동시키는 유틸리티
    def move_widget_to_grid(self, widget, idx):
        w, h = self.width(), self.height()
        ww, wh = widget.width(), widget.height()
        m = 25 # 화면 끝 여백

        # X 좌표 후보 (좌, 중, 우)
        x_left = m
        x_center = int((w - ww) / 2)
        x_right = w - ww - m
        
        # Y 좌표 후보 (상, 중, 하)
        y_top = m
        y_mid = int((h - wh) / 2)
        y_bottom = h - wh - m

        # 그리드 인덱스 매핑 (wxh, wwxwh 고려해 중앙 비움)
        if idx == 0: widget.move(x_left, y_top)      # TL (상좌)
        elif idx == 1: widget.move(x_center, y_top)   # TC (상중)
        elif idx == 2: widget.move(x_right, y_top)    # TR (상우)
        elif idx == 3: widget.move(x_left, y_bottom)   # BL (하좌)
        elif idx == 4: widget.move(x_center, y_bottom)# BC (하중)
        elif idx == 5: widget.move(x_right, y_bottom) # BR (하우)
        elif idx == 6: widget.move(x_left, y_mid)     # ML (중좌 - 3280 와이드니까 유용)
        elif idx == 7: widget.move(x_right, y_mid)    # MR (중우)

    def update_playback(self, w, h, section_data, standby_opts=None):
        opts = standby_opts or {'logo': False, 'time': False, 'weather': False}
        layout_id = f"{w}x{h}_{str(section_data)}_{str(opts)}"
        
        if self.cur_layout_id == layout_id: return
        self.cur_layout_id = layout_id; self.setFixedSize(w, h)
        
        while self.secs: s = self.secs.pop(); s.stop(); s.hide(); s.deleteLater()
        
        # 재생 중엔 무조건 모든 대기 타이머 끔
        self.dwell_timer.stop() 

        if not section_data: 
            # 대기 화면 진입
            self.bg_label.raise_()
            self.bg_label.show()
            self.apply_weather_background()

            # 레이아웃 결정
            logo_vis = opts.get('logo', False)
            time_vis = opts.get('time', False)
            weather_vis = opts.get('weather', False)

            if logo_vis or time_vis or weather_vis:
                # 대기 요소가 하나라도 보이면
                if logo_vis: self.logo_label.raise_(); self.logo_label.show()
                else: self.logo_label.hide()
                
                if time_vis: self.overlay_time.raise_(); self.overlay_time.show()
                else: self.overlay_time.hide()
                
                if weather_vis: self.overlay_weather.raise_(); self.overlay_weather.show()
                else: self.overlay_weather.hide()

                # 대기 진입 즉시 3곳 위치 배정하고 3분 타이머 시작
                QTimer.singleShot(100, self.update_all_overlay_positions) 
                if not self.dwell_timer.isActive():
                    self.dwell_timer.start(180000) # 3분 머무름
            else: 
                # 대기 화면이지만 아무것도 안 보임
                self.logo_label.hide()
                self.overlay_time.hide()
                self.overlay_weather.hide()
            return
            
        # 미디어 재생 중 (대기 요소 모두 숨김)
        self.bg_label.hide()
        self.logo_label.hide(); self.overlay_time.hide(); self.overlay_weather.hide()
        
        for info in section_data:
            ds = DynamicSection(self.cont); ds.setGeometry(QRect(info['x'], info['y'], info['w'], info['h']))
            ds.show(); paths = info.get('paths', [info.get('path')])
            ds.play(paths, info.get('repeat', True), info.get('duration', 0)); self.secs.append(ds)
        self.cont.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_H: self.setWindowFlags(self.windowFlags() ^ Qt.WindowType.FramelessWindowHint); self.show()
        elif event.key() == Qt.Key.Key_F11: self.showFullScreen() if not self.isFullScreen() else self.showNormal()
        elif event.key() == Qt.Key.Key_Escape: self.esc_signal.emit()

# --- [마우스 캔버스 에디터] ---
class CanvasWidget(QWidget):
    def __init__(self, dashboard):
        super().__init__()
        self.db = dashboard; self.setFixedSize(400, 225); self.start_p = None; self.cur_p = None
        self.action = 'draw'; self.sel_idx = -1; self.off_x = 0; self.off_y = 0; self.pending_rect = None; self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _get_rect(self, sec):
        rw, rh = self.db.get_current_resolution()
        sx, sy = self.width() / rw, self.height() / rh
        return QRect(int(sec['x']*sx), int(sec['y']*sy), int(sec['w']*sx), int(sec['h']*sy))

    def paintEvent(self, event):
        p = QPainter(self); p.fillRect(self.rect(), QColor("#000")); p.setPen(QPen(QColor("#333"), 1))
        for x in range(0, self.width(), 20): p.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), 20): p.drawLine(0, y, self.width(), y)
        for i, sec in enumerate(self.db.temp_sections):
            rect = self._get_rect(sec)
            color = QColor(76, 175, 80, 100) if i == self.sel_idx else QColor(33, 150, 243, 100)
            p.setBrush(color); p.setPen(QPen(color.darker(), 2)); p.drawRect(rect)
            p.setBrush(QColor("#FFC107")); p.setPen(Qt.PenStyle.NoPen); p.drawRect(rect.right()-8, rect.bottom()-8, 8, 8)
        if self.action == 'draw' and self.start_p and self.cur_p:
            p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(QColor("#FF9800"), 2, Qt.PenStyle.DashLine)); p.drawRect(QRect(self.start_p, self.cur_p).normalized())
        elif self.pending_rect:
            p.setBrush(QColor(255,152,0,100)); p.setPen(QPen(QColor("#FF9800"), 2)); p.drawRect(self.pending_rect)

    def mouseMoveEvent(self, event):
        pos = event.pos()
        if event.buttons() == Qt.MouseButton.NoButton:
            cursor = Qt.CursorShape.CrossCursor
            for i in range(len(self.db.temp_sections)-1,-1,-1):
                rect = self._get_rect(self.db.temp_sections[i]); handle = QRect(rect.right()-10, rect.bottom()-10, 20, 20)
                if handle.contains(pos): cursor = Qt.CursorShape.SizeFDiagCursor; break
                elif rect.contains(pos): cursor = Qt.CursorShape.OpenHandCursor; break
            self.setCursor(cursor); return
        if self.action == 'draw': self.cur_p = pos
        elif self.sel_idx != -1:
            sec = self.db.temp_sections[self.sel_idx]; rw, rh = self.db.get_current_resolution(); sx, sy = rw/self.width(), rh/self.height()
            if self.action == 'move':
                self.setCursor(Qt.CursorShape.ClosedHandCursor); nx, ny = (pos.x()-self.off_x)*sx, (pos.y()-self.off_y)*sy
                sec['x'], sec['y'] = int(max(0,min(nx,rw-sec['w']))), int(max(0,min(ny,rh-sec['h'])))
            elif self.action == 'resize':
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
                nw, nh = (pos.x()-self.off_x)*sx - sec['x'], (pos.y()-self.off_y)*sy - sec['y']
                sec['w'], sec['h'] = int(max(50,min(nw,rw-sec['x']))), int(max(50,min(nh,rh-sec['y'])))
            self.db.load_sec_to_ui(sec)
        self.update()

    def mousePressEvent(self, event):
        self.setFocus(); pos = event.pos(); self.action = 'draw'; self.sel_idx = -1; self.pending_rect = None
        for i in range(len(self.db.temp_sections)-1,-1,-1):
            rect = self._get_rect(self.db.temp_sections[i]); handle = QRect(rect.right()-10, rect.bottom()-10, 20, 20)
            if handle.contains(pos): 
                self.action = 'resize'; self.sel_idx = i; self.off_x, self.off_y = pos.x()-rect.right(), pos.y()-rect.bottom()
                self.db.load_sec_to_ui(self.db.temp_sections[i]); break
            elif rect.contains(pos): 
                self.action = 'move'; self.sel_idx = i; self.off_x, self.off_y = pos.x()-rect.x(), pos.y()-rect.y()
                self.db.load_sec_to_ui(self.db.temp_sections[i]); break
        if self.action == 'draw': self.start_p = pos; self.cur_p = pos
        self.update()

    def mouseReleaseEvent(self, event):
        if self.action == 'draw' and self.start_p and self.cur_p:
            self.pending_rect = QRect(self.start_p, self.cur_p).normalized(); self.db.update_coords_from_canvas(self.start_p, self.cur_p)
        self.start_p = None; self.cur_p = None; self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.sel_idx != -1:
                self.db.temp_sections.pop(self.sel_idx)
                self.sel_idx = -1
                self.db.btn_add_sec.setText(f"섹션 추가됨 ({len(self.db.temp_sections)}개)")
                self.update()

# --- [시각적 타임라인 위젯] ---
class TimelineWidget(QWidget):
    def __init__(self, dashboard, scroll_area):
        super().__init__()
        self.db = dashboard; self.scroll_area = scroll_area; self.setFixedHeight(65); self.schedules = []
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True); self.drag_idx = -1; self.drag_mode = None
        self.click_x = 0; self.click_y = 0; self.orig_start = 0; self.orig_end = 0
        
        self.zoom_level = 12.0 
        QTimer.singleShot(100, self.apply_default_zoom)

    def apply_default_zoom(self):
        vp_w = self.scroll_area.viewport().width()
        if vp_w > 50:
            self.setMinimumWidth(int(vp_w * self.zoom_level))
        self.update()

    def keyPressEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_C: self.db.copy_selected_items()
            elif event.key() == Qt.Key.Key_V: self.db.paste_items_to_end() 
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.db.delete_selected_items()

    def str_to_min(self, t_str):
        h, m = map(int, t_str.split(':')); return h * 60 + m

    def min_to_str(self, m):
        m = int(m) % 1440; return f"{max(0, m)//60:02d}:{max(0, m)%60:02d}"

    def update_timeline(self, scheds):
        self.schedules = scheds; self.update()

    def _get_rects_for_sched(self, sm, em, w):
        rects = []
        min_px = 2.0 
        visual_em = em if sm != em else sm + 1
            
        if sm <= visual_em: 
            rx = 20 + (w - 40) * (sm / 1440.0)
            rw = max((w - 40) * ((visual_em - sm) / 1440.0), min_px)
            rects.append(QRectF(rx, 25, rw, 20))
        else: 
            rx1 = 20 + (w - 40) * (sm / 1440.0)
            rw1 = max((w - 40) * ((1440 - sm) / 1440.0), min_px)
            rects.append(QRectF(rx1, 25, rw1, 20))
            rw2 = max((w - 40) * (visual_em / 1440.0), min_px)
            rects.append(QRectF(20, 25, rw2, 20))
        return rects

    def wheelEvent(self, event):
        if event.angleDelta().y() > 0: self.zoom_level = min(self.zoom_level * 1.25, 24.0)
        else: self.zoom_level = max(self.zoom_level / 1.25, 1.0)
        vp_w = self.scroll_area.viewport().width()
        if self.zoom_level <= 1.05:
            self.zoom_level = 1.0; self.setMinimumWidth(0)
        else:
            self.setMinimumWidth(int(vp_w * self.zoom_level))
        event.accept(); self.update()

    def mousePressEvent(self, event):
        self.setFocus() 
        x, y = event.pos().x(), event.pos().y()
        self.click_x = x; self.click_y = y
        
        if not (25 <= y <= 45): return 
        w = self.width()
        for i, sched in enumerate(self.schedules):
            sm, em = self.str_to_min(sched['start_time']), self.str_to_min(sched['end_time'])
            for r in self._get_rects_for_sched(sm, em, w):
                if r.left() - 5 <= x <= r.right() + 5:
                    self.drag_idx = i; self.orig_start = sm; self.orig_end = em
                    margin = min(8, r.width() / 3) 
                    if x <= r.left() + margin: self.drag_mode = 'left'
                    elif x >= r.right() - margin: self.drag_mode = 'right'
                    else: self.drag_mode = 'move'
                    return

    def mouseMoveEvent(self, event):
        x, y = event.pos().x(), event.pos().y(); w = self.width()
        if self.drag_idx == -1:
            cursor = Qt.CursorShape.ArrowCursor
            if 25 <= y <= 45:
                for sched in self.schedules:
                    sm, em = self.str_to_min(sched['start_time']), self.str_to_min(sched['end_time'])
                    for r in self._get_rects_for_sched(sm, em, w):
                        if r.left() - 5 <= x <= r.right() + 5:
                            margin = min(8, r.width() / 3)
                            if x <= r.left() + margin or x >= r.right() - margin: cursor = Qt.CursorShape.SizeHorCursor
                            else: cursor = Qt.CursorShape.OpenHandCursor
                            break
            self.setCursor(cursor); return

        delta_min = int(round(((x - self.click_x) / (w - 40) * 1440) / 5) * 5)
        if self.drag_mode == 'move':
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.schedules[self.drag_idx]['start_time'] = self.min_to_str(self.orig_start + delta_min)
            self.schedules[self.drag_idx]['end_time'] = self.min_to_str(self.orig_end + delta_min)
        elif self.drag_mode == 'left':
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.schedules[self.drag_idx]['start_time'] = self.min_to_str(self.orig_start + delta_min)
        elif self.drag_mode == 'right':
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.schedules[self.drag_idx]['end_time'] = self.min_to_str(self.orig_end + delta_min)
        self.update()

    def mouseReleaseEvent(self, event):
        if self.drag_idx != -1:
            x, y = event.pos().x(), event.pos().y()
            if abs(x - self.click_x) < 3 and abs(y - self.click_y) < 3:
                is_sel = self.schedules[self.drag_idx].get('selected', False)
                self.schedules[self.drag_idx]['selected'] = not is_sel
                
            self.drag_idx = -1; self.drag_mode = None
            self.db.update_list_ui() 
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        bg_rect = QRect(20, 25, w - 40, 20)
        p.setBrush(QColor("#2A2A2A")); p.setPen(Qt.PenStyle.NoPen); p.drawRoundedRect(bg_rect, 5, 5)

        p.setPen(QPen(QColor("#888888"), 1)); p.setFont(QFont("Malgun Gothic", 8))
        draw_interval = 3
        if self.zoom_level >= 2.0: draw_interval = 1
        for i in range(25):
            x = 20 + (w - 40) * (i / 24.0)
            p.drawLine(int(x), 45, int(x), 50)
            if i % draw_interval == 0: 
                p.drawText(int(x) - 10, 62, f"{i}시")

        if self.zoom_level > 6.0:
            p.setPen(QPen(QColor("#555555"), 1, Qt.PenStyle.DashLine))
            for i in range(24 * 6): 
                if i % 6 == 0: continue 
                x = 20 + (w - 40) * (i / 144.0)
                p.drawLine(int(x), 47, int(x), 50)

        for i, sched in enumerate(self.schedules):
            sm, em = self.str_to_min(sched['start_time']), self.str_to_min(sched['end_time'])
            rects = self._get_rects_for_sched(sm, em, w)
            
            is_selected = sched.get('selected', False)
            if self.drag_idx == i: color = QColor(76, 175, 80, 255)
            elif is_selected: color = QColor(76, 175, 80, 180)
            else: color = QColor(33, 150, 243, 220)
                
            for r in rects:
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(color); p.drawRoundedRect(r, 4, 4)
                p.setPen(QPen(Qt.GlobalColor.white)); p.setFont(QFont("Malgun Gothic", 10, QFont.Weight.Bold))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(i + 1))


# --- [통합 관리 대시보드] ---
class UnifiedDashboard(QWidget):
    def __init__(self, pb_win):
        super().__init__()
        self.pb = pb_win
        self.current_user = None
        self.setup_user_system()
        self.pb.esc_signal.connect(self.force_close_playback)
        self.master_schedules = []
        self.current_camp_idx = -1
        self.temp_sections = []
        self.playlist_data = []
        self.active_sched = []
        self.is_pub = False
        self.last_paths = []
        self.clipboard_campaign = None
        self.clipboard_schedules = []

        self.setWindowTitle("Signage All-in-One Manager (날씨 그라데이션 자동 연동 완료)")
        self.resize(1500, 950)
        self.setStyleSheet("""
            QWidget { background-color: #121212; color: #E0E0E0; font-family: 'Malgun Gothic'; }
            QGroupBox { border: 1px solid #333; border-radius: 8px; margin-top: 10px; padding-top: 15px; background-color: #1E1E1E; color: #2196F3; font-weight: bold; }
            QLineEdit, QComboBox, QDateEdit, QTimeEdit, QSpinBox { background: #2A2A2A; border: 1px solid #444; color: white; border-radius: 4px; padding: 4px; }
            QPushButton { background: #333; border: 1px solid #555; border-radius: 4px; padding: 8px; color: #AAA; }
            QPushButton:hover { background: #444; }
            QPushButton:checked { background: #2196F3; color: white; }
            QPushButton#ActionBtn { background: #2196F3; color: white; font-weight: bold; }
            QPushButton#StopBtn { background: #CF6679; color: white; font-weight: bold; }
            QTableWidget { background-color: #1E1E1E; color: white; gridline-color: #444; border: 1px solid #333; }
            QTableWidget::item:selected { background-color: #2196F3; color: white; }
            QHeaderView::section { background-color: #333; color: white; font-weight: bold; padding: 4px; border: 1px solid #444;}
        """)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(15)

        # 0단계
        left_panel = QWidget(); left_panel.setFixedWidth(400); left_v = QVBoxLayout(left_panel); left_v.setContentsMargins(0, 0, 0, 0)
        master_grp = QGroupBox("0단계: 전체 스케줄 캠페인"); master_v = QVBoxLayout(master_grp)
        self.camp_table = QTableWidget(0, 2); self.camp_table.setHorizontalHeaderLabels(["스케줄 이름", "운영 기간"])
        self.camp_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch); self.camp_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.camp_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows); self.camp_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.camp_table.cellClicked.connect(self.load_campaign_to_editor); master_v.addWidget(self.camp_table)
        camp_btn_h1 = QHBoxLayout(); self.btn_new_camp = QPushButton("➕ 새 스케줄"); self.btn_new_camp.setStyleSheet("color: #4CAF50; font-weight: bold;")
        self.btn_del_camp = QPushButton("🗑️ 삭제"); self.btn_del_camp.setStyleSheet("color: #CF6679;")
        camp_btn_h1.addWidget(self.btn_new_camp); camp_btn_h1.addWidget(self.btn_del_camp); master_v.addLayout(camp_btn_h1) 
        
        camp_btn_h2 = QHBoxLayout()
        self.btn_copy_camp = QPushButton("📄 스케줄 복사")
        self.btn_paste_camp = QPushButton("📋 붙여넣기")
        self.btn_copy_camp.setStyleSheet("color: #FF9800; font-weight: bold;")
        self.btn_paste_camp.setStyleSheet("color: #2196F3; font-weight: bold;")
        camp_btn_h2.addWidget(self.btn_copy_camp); camp_btn_h2.addWidget(self.btn_paste_camp); master_v.addLayout(camp_btn_h2)

        left_v.addWidget(master_grp, 1) 
        
        act_grp = QGroupBox("시스템 통합 제어"); act_v_left = QVBoxLayout(act_grp)
        self.btn_user_mgmt = QPushButton("👤 사용자 계정 관리")
        self.btn_save_all = QPushButton("💾 전체 스케줄 저장 (JSON)"); self.btn_load_all = QPushButton("📂 스케줄 파일 불러오기")
        self.btn_stop_all = QPushButton("🛑 전체 중단 (대기화면 모드)"); self.btn_stop_all.setObjectName("StopBtn")
        self.btn_pub_all = QPushButton("📢 스케줄 자동 순환 발행"); self.btn_pub_all.setObjectName("ActionBtn")
        for b in [self.btn_user_mgmt, self.btn_save_all, self.btn_load_all, self.btn_stop_all, self.btn_pub_all]: b.setFixedHeight(45); act_v_left.addWidget(b)
        left_v.addWidget(act_grp); main_layout.addWidget(left_panel)

        # 1~3단계
        right_panel = QWidget(); right_v = QVBoxLayout(right_panel); right_v.setContentsMargins(0, 0, 0, 0); right_v.setSpacing(10)
        
        top_h = QHBoxLayout(); base_grp = QGroupBox("1단계: 선택된 스케줄 기간 (자동 동기화)"); base_v = QVBoxLayout(base_grp)
        name_h = QHBoxLayout(); name_h.addWidget(QLabel("스케줄 이름:")); self.in_name = QLineEdit(); name_h.addWidget(self.in_name); base_v.addLayout(name_h)
        date_h = QHBoxLayout(); self.in_start = QDateEdit(QDate.currentDate()); self.in_end = QDateEdit(QDate.currentDate().addMonths(1))
        self.in_start.setDisplayFormat("yyyy-MM-dd"); self.in_end.setDisplayFormat("yyyy-MM-dd"); self.in_start.setCalendarPopup(True); self.in_end.setCalendarPopup(True)
        date_h.addWidget(QLabel("기간:")); date_h.addWidget(self.in_start); date_h.addWidget(QLabel("~")); date_h.addWidget(self.in_end); base_v.addLayout(date_h); top_h.addWidget(base_grp, 1)

        time_grp = QGroupBox("운영 시간 및 대기화면(Standby) 옵션"); time_v = QVBoxLayout(time_grp)
        t_h = QHBoxLayout(); self.in_on = QTimeEdit(QTime(9,0)); self.in_off = QTimeEdit(QTime(18,0))
        self.in_on.setStyleSheet("background: #2A2A2A; border: 1px solid #444; color: white;"); self.in_off.setStyleSheet("background: #2A2A2A; border: 1px solid #444; color: white;")
        t_h.addWidget(QLabel("시간:")); t_h.addWidget(self.in_on); t_h.addWidget(QLabel("~")); t_h.addWidget(self.in_off); time_v.addLayout(t_h)
        day_h = QHBoxLayout(); self.day_btns = []
        for d in ["월","화","수","목","금","토","일"]:
            btn = QPushButton(d); btn.setCheckable(True); btn.setChecked(True); btn.setFixedWidth(35); self.day_btns.append(btn); day_h.addWidget(btn)
        time_v.addLayout(day_h)
        
        opt_h = QHBoxLayout()
        self.check_logo = QCheckBox("직접 등록한 로고 표시 (lg_logo.png)")
        self.check_time = QCheckBox("날짜/시간 표시")
        self.check_weather = QCheckBox("날씨 연동 다크 배경 및 기온")
        self.check_logo.setChecked(True); self.check_time.setChecked(True); self.check_weather.setChecked(True)
        opt_h.addWidget(self.check_logo); opt_h.addWidget(self.check_time); opt_h.addWidget(self.check_weather); opt_h.addStretch(); time_v.addLayout(opt_h)
        top_h.addWidget(time_grp, 1); right_v.addLayout(top_h)

        editor_grp = QGroupBox("2단계: 레이아웃 캔버스 에디터 (박스 클릭 후 Del 키로 삭제)"); editor_h = QHBoxLayout(editor_grp)
        self.canvas = CanvasWidget(self); editor_h.addWidget(self.canvas)
        detail_v = QVBoxLayout(); self.lbl_edit_status = QLabel(""); self.lbl_edit_status.hide(); detail_v.addWidget(self.lbl_edit_status)
        res_h = QHBoxLayout(); self.in_res = QComboBox(); self.in_res.addItems(["1920x1080", "1280x800", "1080x1920", "3840x1080", "3248x1080", "사용자 지정"])
        self.custom_w = QLineEdit("1920"); self.custom_h = QLineEdit("1080"); self.custom_w.setEnabled(False); self.custom_h.setEnabled(False)
        res_h.addWidget(self.in_res); res_h.addWidget(QLabel("W:")); res_h.addWidget(self.custom_w); res_h.addWidget(QLabel("H:")); res_h.addWidget(self.custom_h); detail_v.addLayout(res_h)
        pos_h = QHBoxLayout(); self.in_x, self.in_y, self.in_w, self.in_h = QLineEdit("0"), QLineEdit("0"), QLineEdit("1920"), QLineEdit("1080")
        for lbl, w_input in [("X:", self.in_x), ("Y:", self.in_y), ("W:", self.in_w), ("H:", self.in_h)]: pos_h.addWidget(QLabel(lbl)); pos_h.addWidget(w_input)
        detail_v.addLayout(pos_h)
        media_h = QHBoxLayout(); self.btn_file = QPushButton("📁 미디어 선택 (PPT 지원)"); self.btn_yt = QPushButton("🔗 유튜브/URL"); self.lbl_file = QLabel("선택된 파일 없음"); self.btn_yt.setStyleSheet("color:#FF4444; font-weight:bold;")
        media_h.addWidget(self.btn_file); media_h.addWidget(self.btn_yt); detail_v.addLayout(media_h); detail_v.addWidget(self.lbl_file)
        opt_h = QHBoxLayout(); self.check_rep = QCheckBox("무한 반복"); self.check_rep.setChecked(True); self.in_duration = QSpinBox(); self.in_duration.setRange(0, 3600); self.in_duration.setSuffix(" 초")
        opt_h.addWidget(self.check_rep); opt_h.addWidget(QLabel("자동 넘김:")); opt_h.addWidget(self.in_duration); opt_h.addStretch(); detail_v.addLayout(opt_h)
        btn_h = QHBoxLayout(); self.btn_add_sec = QPushButton("➕ 화면 구역 추가"); self.btn_clear_sec = QPushButton("🧹 캔버스 초기화"); self.btn_add_sec.setStyleSheet("background:#2E7D32; color:white; font-size: 14px; font-weight:bold; height: 35px;")
        btn_h.addWidget(self.btn_add_sec); btn_h.addWidget(self.btn_clear_sec); detail_v.addLayout(btn_h); editor_h.addLayout(detail_v); right_v.addWidget(editor_grp)

        list_grp = QGroupBox("3단계: 상세 시간대별 화면 배정 (타임라인 선택/클릭 후 Ctrl+C, Ctrl+V)")
        list_v = QVBoxLayout(list_grp)
        
        self.timeline_scroll = QScrollArea()
        self.timeline = TimelineWidget(self, self.timeline_scroll)
        self.timeline_scroll.setWidgetResizable(True); self.timeline_scroll.setFixedHeight(90); self.timeline_scroll.setWidget(self.timeline); self.timeline_scroll.setStyleSheet("border: 1px solid #444; border-radius: 5px;")
        list_v.addWidget(self.timeline_scroll)

        assign_h = QHBoxLayout()
        self.sched_start = QTimeEdit(QTime(9, 0)); self.sched_end = QTimeEdit(QTime(10, 0))
        self.sched_start.setDisplayFormat("HH:mm"); self.sched_end.setDisplayFormat("HH:mm")
        self.sched_start.setStyleSheet("background: #2A2A2A; border: 1px solid #444; color: white; font-size: 14px;")
        self.sched_end.setStyleSheet("background: #2A2A2A; border: 1px solid #444; color: white; font-size: 14px;")
        self.btn_add_sched = QPushButton("🔽 현재 2단계 화면을 이 시간대에 등록")
        self.btn_add_sched.setStyleSheet("background: #FF9800; color: black; font-weight: bold; height: 30px;")
        assign_h.addWidget(QLabel("시작:")); assign_h.addWidget(self.sched_start); assign_h.addWidget(QLabel("~ 종료:")); assign_h.addWidget(self.sched_end); assign_h.addSpacing(20); assign_h.addWidget(self.btn_add_sched); assign_h.addStretch(); list_v.addLayout(assign_h)
        
        bulk_h = QHBoxLayout()
        self.btn_sel_all = QPushButton("☑️ 전체 선택"); self.btn_sel_none = QPushButton("🔲 선택 해제")
        self.btn_copy_sel = QPushButton("📄 선택 복사"); self.btn_paste = QPushButton("📋 끝에 붙여넣기 (0개)"); self.btn_del_sel = QPushButton("🗑️ 선택 삭제")
        self.btn_snap_all = QPushButton("🧲 간격 자동 밀착")
        self.btn_snap_all.setStyleSheet("background: #00BCD4; color: white; font-weight: bold; padding: 5px;")
        self.btn_sel_all.setStyleSheet("background: #333; color: white; font-weight: bold; padding: 5px;"); self.btn_sel_none.setStyleSheet("background: #333; color: white; font-weight: bold; padding: 5px;")
        self.btn_copy_sel.setStyleSheet("background: #FF9800; color: black; font-weight: bold; padding: 5px;"); self.btn_paste.setStyleSheet("background: #4CAF50; color: white; font-weight: bold; padding: 5px;"); self.btn_del_sel.setStyleSheet("background: #CF6679; color: white; font-weight: bold; padding: 5px;")
        bulk_h.addWidget(self.btn_sel_all); bulk_h.addWidget(self.btn_sel_none); bulk_h.addWidget(self.btn_copy_sel); bulk_h.addWidget(self.btn_paste); bulk_h.addWidget(self.btn_del_sel); bulk_h.addSpacing(15); bulk_h.addWidget(self.btn_snap_all); bulk_h.addStretch(); list_v.addLayout(bulk_h)
        
        self.list_v = QVBoxLayout(); self.list_v.setAlignment(Qt.AlignmentFlag.AlignTop); self.list_v.setSpacing(5)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setStyleSheet("border: none;")
        scroll_w = QWidget(); scroll_w.setLayout(self.list_v); scroll.setWidget(scroll_w); list_v.addWidget(scroll); right_v.addWidget(list_grp, 3)
        main_layout.addWidget(right_panel)

        # 이벤트 연결
        self.in_name.textChanged.connect(self.sync_campaign_meta); self.in_start.dateChanged.connect(self.sync_campaign_meta); self.in_end.dateChanged.connect(self.sync_campaign_meta); self.in_on.timeChanged.connect(self.sync_campaign_meta); self.in_off.timeChanged.connect(self.sync_campaign_meta)
        for b in self.day_btns: b.toggled.connect(self.sync_campaign_meta)
        self.check_logo.toggled.connect(self.sync_campaign_meta); self.check_time.toggled.connect(self.sync_campaign_meta); self.check_weather.toggled.connect(self.sync_campaign_meta)
        
        self.btn_new_camp.clicked.connect(self.create_new_campaign); self.btn_del_camp.clicked.connect(self.delete_campaign)
        self.btn_copy_camp.clicked.connect(self.copy_campaign); self.btn_paste_camp.clicked.connect(self.paste_campaign)
        
        self.in_x.textChanged.connect(self.update_canvas_from_coords); self.in_y.textChanged.connect(self.update_canvas_from_coords); self.in_w.textChanged.connect(self.update_canvas_from_coords); self.in_h.textChanged.connect(self.update_canvas_from_coords); self.in_duration.valueChanged.connect(self.update_canvas_from_coords); self.in_res.currentIndexChanged.connect(self.toggle_custom_res)
        self.btn_file.clicked.connect(self.get_file); self.btn_yt.clicked.connect(self.get_youtube); self.btn_add_sec.clicked.connect(self.add_sec); self.btn_clear_sec.clicked.connect(self.clear_sec); self.btn_add_sched.clicked.connect(self.add_time_schedule)
        self.btn_sel_all.clicked.connect(self.select_all_items); self.btn_sel_none.clicked.connect(self.deselect_all_items); self.btn_copy_sel.clicked.connect(self.copy_selected_items); self.btn_paste.clicked.connect(self.paste_items_to_end); self.btn_del_sel.clicked.connect(self.delete_selected_items)
        self.btn_snap_all.clicked.connect(self.snap_all_items)
        
        self.btn_pub_all.clicked.connect(self.publish_all); self.btn_stop_all.clicked.connect(self.stop_all); self.btn_save_all.clicked.connect(self.save_json); self.btn_load_all.clicked.connect(self.load_json)
        self.btn_user_mgmt.clicked.connect(self.open_user_management)
        
        self.timer = QTimer(self); self.timer.timeout.connect(self.global_loop); self.timer.start(1000); self.create_new_campaign()

    def open_user_management(self):
        """사용자 관리 다이얼로그를 엽니다."""
        self.users = load_users()
        dialog = UserManagementDialog(self.users, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            save_users(dialog.users)
            self.users = dialog.users
            print("사용자 정보가 업데이트되었습니다.")

    def closeEvent(self, event):
        """메인 창이 닫힐 때 재생 창도 함께 닫습니다."""
        self.pb.close()
        event.accept()

    def time_to_min(self, t_str):
        h, m = map(int, t_str.split(':')); return h * 60 + m
    def min_to_time(self, m):
        m = int(m) % 1440; return f"{max(0, m)//60:02d}:{max(0, m)%60:02d}"

    def copy_campaign(self):
        if self.current_camp_idx >= 0:
            self.clipboard_campaign = copy.deepcopy(self.master_schedules[self.current_camp_idx])
            self.btn_paste_camp.setText("📋 붙여넣기 (준비됨)")

    def paste_campaign(self):
        if self.clipboard_campaign:
            new_camp = copy.deepcopy(self.clipboard_campaign)
            new_camp['id'] = QDateTime.currentMSecsSinceEpoch()
            new_camp['name'] = new_camp['name'] + " (복사본)"
            for s in new_camp['playlist_data']: s['selected'] = False
            self.master_schedules.append(new_camp)
            self.refresh_campaign_list()
            idx = len(self.master_schedules) - 1
            self.camp_table.selectRow(idx)
            self.load_campaign_to_editor(idx, 0)

    def select_all_items(self):
        for it in self.playlist_data: it['selected'] = True
        self.update_list_ui()
    def deselect_all_items(self):
        for it in self.playlist_data: it['selected'] = False
        self.update_list_ui()
    def copy_selected_items(self):
        self.clipboard_schedules = [copy.deepcopy(it) for it in self.playlist_data if it.get('selected', False)]
        self.btn_paste.setText(f"📋 끝에 붙여넣기 ({len(self.clipboard_schedules)}개)")
    def paste_items_to_end(self):
        if not self.clipboard_schedules: return
        clipboard_sorted = sorted(self.clipboard_schedules, key=lambda x: self.time_to_min(x['start_time']))
        if not self.playlist_data: paste_start_min = self.time_to_min("09:00")
        else:
            self.playlist_data.sort(key=lambda x: self.time_to_min(x['start_time']))
            paste_start_min = self.time_to_min(self.playlist_data[-1]['end_time'])
        first_clip_start = self.time_to_min(clipboard_sorted[0]['start_time'])
        offset = paste_start_min - first_clip_start
        for it in clipboard_sorted:
            orig_s = self.time_to_min(it['start_time']); orig_e = self.time_to_min(it['end_time'])
            if orig_e < orig_s: orig_e += 1440
            new_s = orig_s + offset; new_e = orig_e + offset
            new_it = copy.deepcopy(it); new_it['selected'] = False; new_it['start_time'] = self.min_to_time(new_s); new_it['end_time'] = self.min_to_time(new_e)
            self.playlist_data.append(new_it)
        self.update_list_ui()
    def snap_all_items(self):
        if not self.playlist_data: return
        self.playlist_data.sort(key=lambda x: self.time_to_min(x['start_time']))
        curr_min = self.time_to_min(self.playlist_data[0]['start_time'])
        for it in self.playlist_data:
            dur = self.get_duration_min(it['start_time'], it['end_time'])
            it['start_time'] = self.min_to_time(curr_min); curr_min += dur; it['end_time'] = self.min_to_time(curr_min)
        self.update_list_ui()
    def delete_selected_items(self):
        self.playlist_data = [it for it in self.playlist_data if not it.get('selected', False)]
        self.update_list_ui()

    def create_new_campaign(self):
        new_camp = {'id': QDateTime.currentMSecsSinceEpoch(), 'name': f"새 스케줄 {len(self.master_schedules)+1}", 'start_date': QDate.currentDate().toString("yyyy-MM-dd"), 'end_date': QDate.currentDate().addMonths(1).toString("yyyy-MM-dd"), 'on_time': "09:00", 'off_time': "18:00", 'days': ["월","화","수","목","금","토","일"], 'show_logo': True, 'show_time': True, 'show_weather': True, 'playlist_data': []}
        self.master_schedules.append(new_camp); self.refresh_campaign_list(); self.camp_table.selectRow(len(self.master_schedules)-1); self.load_campaign_to_editor(len(self.master_schedules)-1, 0)
    def delete_campaign(self):
        if self.current_camp_idx < 0: return
        self.master_schedules.pop(self.current_camp_idx); self.current_camp_idx = -1; self.refresh_campaign_list()
        if self.master_schedules: self.camp_table.selectRow(0); self.load_campaign_to_editor(0, 0)
        else: self.playlist_data = []; self.update_list_ui()
    def refresh_campaign_list(self):
        self.camp_table.setRowCount(len(self.master_schedules))
        for i, camp in enumerate(self.master_schedules): self.camp_table.setItem(i, 0, QTableWidgetItem(camp['name'])); self.camp_table.setItem(i, 1, QTableWidgetItem(f"{camp['start_date'][5:]} ~ {camp['end_date'][5:]}"))
    def load_campaign_to_editor(self, r, c):
        self.current_camp_idx = r; camp = self.master_schedules[r]
        self.in_name.blockSignals(True); self.in_name.setText(camp['name']); self.in_name.blockSignals(False)
        self.in_start.blockSignals(True); self.in_start.setDate(QDate.fromString(camp['start_date'], "yyyy-MM-dd")); self.in_start.blockSignals(False)
        self.in_end.blockSignals(True); self.in_end.setDate(QDate.fromString(camp['end_date'], "yyyy-MM-dd")); self.in_end.blockSignals(False)
        self.in_on.blockSignals(True); self.in_on.setTime(QTime.fromString(camp['on_time'], "HH:mm")); self.in_on.blockSignals(False)
        self.in_off.blockSignals(True); self.in_off.setTime(QTime.fromString(camp['off_time'], "HH:mm")); self.in_off.blockSignals(False)
        for b in self.day_btns: b.blockSignals(True); b.setChecked(b.text() in camp['days']); b.blockSignals(False)
        self.check_logo.blockSignals(True); self.check_logo.setChecked(camp.get('show_logo', True)); self.check_logo.blockSignals(False)
        self.check_time.blockSignals(True); self.check_time.setChecked(camp.get('show_time', True)); self.check_time.blockSignals(False)
        self.check_weather.blockSignals(True); self.check_weather.setChecked(camp.get('show_weather', True)); self.check_weather.blockSignals(False)
        self.playlist_data = camp['playlist_data']; self.clear_sec(); self.update_list_ui()
    
    def sync_campaign_meta(self):
        if self.current_camp_idx < 0: return
        camp = self.master_schedules[self.current_camp_idx]; camp['name'] = self.in_name.text(); camp['start_date'] = self.in_start.date().toString("yyyy-MM-dd"); camp['end_date'] = self.in_end.date().toString("yyyy-MM-dd"); camp['on_time'] = self.in_on.time().toString("HH:mm"); camp['off_time'] = self.in_off.time().toString("HH:mm"); camp['days'] = [b.text() for b in self.day_btns if b.isChecked()]
        camp['show_logo'] = self.check_logo.isChecked(); camp['show_time'] = self.check_time.isChecked(); camp['show_weather'] = self.check_weather.isChecked()
        self.camp_table.setItem(self.current_camp_idx, 0, QTableWidgetItem(camp['name'])); self.camp_table.setItem(self.current_camp_idx, 1, QTableWidgetItem(f"{camp['start_date'][5:]} ~ {camp['end_date'][5:]}"))

    def get_duration_min(self, start_str, end_str):
        s = QTime.fromString(start_str, "HH:mm"); e = QTime.fromString(end_str, "HH:mm"); secs = s.secsTo(e)
        if secs < 0: secs += 86400 
        return secs // 60

    def on_list_time_chg(self, t, idx, s_edit, e_edit, dur_spin):
        self.playlist_data[idx]['start_time'] = s_edit.time().toString("HH:mm"); self.playlist_data[idx]['end_time'] = e_edit.time().toString("HH:mm")
        new_dur = self.get_duration_min(self.playlist_data[idx]['start_time'], self.playlist_data[idx]['end_time'])
        dur_spin.blockSignals(True); dur_spin.setValue(new_dur); dur_spin.blockSignals(False); self.timeline.update_timeline(self.playlist_data)

    def on_list_dur_chg(self, val, idx, s_edit, e_edit):
        s_time = s_edit.time(); e_time = s_time.addSecs(val * 60)
        e_edit.blockSignals(True); e_edit.setTime(e_time); e_edit.blockSignals(False)
        self.playlist_data[idx]['end_time'] = e_time.toString("HH:mm"); self.timeline.update_timeline(self.playlist_data)

    def update_list_ui(self):
        while self.list_v.count():
            item = self.list_v.takeAt(0); w = item.widget()
            if w: w.deleteLater()
            
        self.playlist_data.sort(key=lambda x: self.time_to_min(x['start_time']))
        
        for i, it in enumerate(self.playlist_data):
            f = QFrame(); f.setMinimumHeight(65)
            is_selected = it.get('selected', False)
            bg_color = "#3A4A3A" if is_selected else "#252525"
            f.setStyleSheet(f"background: {bg_color}; border: 1px solid #555; border-radius: 8px; margin-bottom: 2px;")
            h = QHBoxLayout(f)
            
            chk = QCheckBox(); chk.setChecked(is_selected); chk.setStyleSheet("spacing: 0px;")
            def on_chk(state, idx=i):
                self.playlist_data[idx]['selected'] = bool(state); self.update_list_ui() 
            chk.stateChanged.connect(on_chk); h.addWidget(chk)
            
            idx_lbl = QLabel(str(i + 1)); idx_lbl.setFixedSize(24, 24); idx_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            idx_lbl.setStyleSheet("background-color: #FF9800; color: black; border-radius: 12px; font-weight: bold; font-size: 13px; border: none;"); h.addWidget(idx_lbl)
            
            s_edit = QTimeEdit(QTime.fromString(it['start_time'], "HH:mm")); s_edit.setDisplayFormat("HH:mm"); s_edit.setFixedWidth(110)
            s_edit.setStyleSheet("font-size: 15px; font-weight: bold; color: #2196F3; background: #333; padding: 2px; border: none;")
            e_edit = QTimeEdit(QTime.fromString(it['end_time'], "HH:mm")); e_edit.setDisplayFormat("HH:mm"); e_edit.setFixedWidth(110)
            e_edit.setStyleSheet("font-size: 15px; font-weight: bold; color: #FF9800; background: #333; padding: 2px; border: none;")
            
            dur_min = self.get_duration_min(it['start_time'], it['end_time'])
            dur_spin = QSpinBox(); dur_spin.setRange(0, 1440); dur_spin.setValue(dur_min); dur_spin.setFixedWidth(110)
            dur_spin.setStyleSheet("background: #333; color: #4CAF50; font-size: 15px; font-weight: bold; padding: 2px; border: none;")
            dur_lbl = QLabel("분 유지"); dur_lbl.setStyleSheet("color: #4CAF50; font-size: 13px; font-weight: bold; border: none;")

            s_edit.timeChanged.connect(lambda t, idx=i, s=s_edit, e=e_edit, d=dur_spin: self.on_list_time_chg(t, idx, s, e, d))
            e_edit.timeChanged.connect(lambda t, idx=i, s=s_edit, e=e_edit, d=dur_spin: self.on_list_time_chg(t, idx, s, e, d))
            dur_spin.valueChanged.connect(lambda val, idx=i, s=s_edit, e=e_edit: self.on_list_dur_chg(val, idx, s, e))
            
            h.addWidget(s_edit); h.addWidget(QLabel("~")); h.addWidget(e_edit); h.addSpacing(10); h.addWidget(dur_spin); h.addWidget(dur_lbl)
            h.addWidget(QLabel(f" | {it['res_w']}x{it['res_h']} | 섹션 {len(it['secs'])}개")); h.addStretch()
            
            btn_copy = QPushButton("복사"); btn_copy.setFixedWidth(60); btn_copy.setStyleSheet("color: #FF9800; font-weight: bold; border: 1px solid #FF9800; padding: 3px;")
            btn_copy.clicked.connect(lambda _, idx=i: self.duplicate_item(idx))
            btn_edit = QPushButton("상세편집"); btn_edit.setFixedWidth(70); btn_edit.setStyleSheet("color: #2196F3; font-weight: bold; border: 1px solid #2196F3; padding: 3px;")
            btn_edit.clicked.connect(lambda _, idx=i: self.edit_schedule_item(idx))
            btn_del = QPushButton("삭제"); btn_del.setFixedWidth(50); btn_del.setStyleSheet("color: #CF6679; font-weight: bold; border: 1px solid #CF6679; padding: 3px;")
            btn_del.clicked.connect(lambda _, idx=i: self.delete_schedule_item(idx))
            h.addWidget(btn_copy); h.addWidget(btn_edit); h.addWidget(btn_del); self.list_v.addWidget(f)
            
        self.timeline.update_timeline(self.playlist_data)

    def duplicate_item(self, idx):
        self.playlist_data.insert(idx + 1, copy.deepcopy(self.playlist_data[idx])); self.update_list_ui()

    def update_canvas_from_coords(self):
        try: x, y, w, h = int(self.in_x.text() or 0), int(self.in_y.text() or 0), int(self.in_w.text() or 50), int(self.in_h.text() or 50)
        except: return
        if hasattr(self.canvas, 'sel_idx') and self.canvas.sel_idx != -1: self.temp_sections[self.canvas.sel_idx].update({'x':x,'y':y,'w':w,'h':h,'duration':self.in_duration.value()})
        else: rw, rh = self.get_current_resolution(); sx, sy = self.canvas.width()/rw, self.canvas.height()/rh; self.canvas.pending_rect = QRect(int(x*sx), int(y*sy), int(w*sx), int(h*sy))
        self.canvas.update()

    def get_current_resolution(self):
        if self.in_res.currentText() == "사용자 지정": return int(self.custom_w.text() or 1920), int(self.custom_h.text() or 1080)
        return map(int, self.in_res.currentText().split('x'))

    def update_coords_from_canvas(self, p1, p2):
        rw, rh = self.get_current_resolution(); cw, ch = self.canvas.width(), self.canvas.height(); sx, sy = rw/cw, rh/ch; rect = QRect(p1,p2).normalized(); x, y, w, h = int(rect.x()*sx), int(rect.y()*sy), int(rect.width()*sx), int(rect.height()*sy)
        for b,v in [(self.in_x,x),(self.in_y,y),(self.in_w,w),(self.in_h,h)]: b.blockSignals(True); b.setText(str(v)); b.blockSignals(False)

    def load_sec_to_ui(self, sec):
        for b,v in [(self.in_x,sec['x']),(self.in_y,sec['y']),(self.in_w,sec['w']),(self.in_h,sec['h'])]: b.blockSignals(True); b.setText(str(v)); b.blockSignals(False)
        self.in_duration.blockSignals(True); self.in_duration.setValue(sec.get('duration', 0)); self.in_duration.blockSignals(False)
        self.last_paths = sec.get('paths',[]); self.lbl_file.setText(f"{len(self.last_paths)}개 파일" if len(self.last_paths) > 1 else os.path.basename(self.last_paths[0])) if self.last_paths else self.lbl_file.setText("파일 없음")
        self.check_rep.setChecked(sec.get('repeat', True))

    def toggle_custom_res(self):
        is_c = self.in_res.currentText() == "사용자 지정"; self.custom_w.setEnabled(is_c); self.custom_h.setEnabled(is_c); self.canvas.update()

    def get_file(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "미디어/문서 선택", "", "All Files (*.*);;Videos (*.mp4 *.avi);;Images (*.jpg *.png);;Documents (*.pdf *.pptx *.ppt)")
        if paths: self.last_paths = paths; self.lbl_file.setText(f"{len(paths)}개 선택" if len(paths)>1 else os.path.basename(paths[0]))
        if hasattr(self.canvas, 'sel_idx') and self.canvas.sel_idx != -1: self.temp_sections[self.canvas.sel_idx]['paths'] = paths

    def get_youtube(self):
        text, ok = QInputDialog.getText(self, "유튜브/URL", "URL 주소 입력:")
        if ok and text: self.last_paths = [text]; self.lbl_file.setText(text[:20]+"...")
        if hasattr(self.canvas, 'sel_idx') and self.canvas.sel_idx != -1: self.temp_sections[self.canvas.sel_idx]['paths'] = [text]

    def add_sec(self):
        if not hasattr(self, 'last_paths') or not self.last_paths: return
        self.temp_sections.append({'x':int(self.in_x.text()), 'y':int(self.in_y.text()), 'w':int(self.in_w.text()), 'h':int(self.in_h.text()), 'paths':self.last_paths.copy(), 'repeat':self.check_rep.isChecked(), 'duration':self.in_duration.value()})
        self.btn_add_sec.setText(f"섹션 추가됨 ({len(self.temp_sections)}개)"); self.canvas.pending_rect = None; self.canvas.sel_idx = len(self.temp_sections)-1; self.canvas.update()

    def clear_sec(self):
        self.temp_sections.clear(); self.btn_add_sec.setText("➕ 화면 구역 추가"); self.canvas.pending_rect = None; self.lbl_edit_status.hide(); self.canvas.update()

    def add_time_schedule(self):
        if not self.temp_sections: return 
        rw, rh = self.get_current_resolution()
        self.playlist_data.append({'start_time': self.sched_start.time().toString("HH:mm"), 'end_time': self.sched_end.time().toString("HH:mm"), 'res_w': rw, 'res_h': rh, 'secs': self.temp_sections.copy(), 'selected': False})
        self.lbl_edit_status.hide(); self.update_list_ui()

    def edit_schedule_item(self, idx):
        item = self.playlist_data[idx]
        self.lbl_edit_status.setText(f"✏️ 수정 중: [{item['start_time']}~{item['end_time']}] (수정 완료 후 목록에 다시 등록하세요)"); self.lbl_edit_status.setStyleSheet("color: #FFC107; background: #332900; padding: 5px; border-radius: 4px;"); self.lbl_edit_status.show()
        res_str = f"{item['res_w']}x{item['res_h']}"
        if self.in_res.findText(res_str) != -1: self.in_res.setCurrentText(res_str)
        else: self.in_res.setCurrentText("사용자 지정"); self.custom_w.setText(str(item['res_w'])); self.custom_h.setText(str(item['res_h']))
        self.sched_start.setTime(QTime.fromString(item['start_time'], "HH:mm")); self.sched_end.setTime(QTime.fromString(item['end_time'], "HH:mm"))
        self.temp_sections = copy.deepcopy(item['secs'])
        if self.temp_sections: self.load_sec_to_ui(self.temp_sections[-1]); self.canvas.sel_idx = len(self.temp_sections)-1
        self.btn_add_sec.setText(f"섹션 롤백됨 ({len(self.temp_sections)}개)"); self.canvas.update(); self.delete_schedule_item(idx)

    def delete_schedule_item(self, idx):
        self.playlist_data.pop(idx); self.update_list_ui()

    def force_close_playback(self):
        self.is_pub = False; self.pb.cur_layout_id = "FORCE_STOP"; self.pb.hide(); self.pb.update_playback(1920, 1080, None, None); self.btn_pub_all.setText("📢 스케줄 자동 발행 시작"); self.btn_pub_all.setStyleSheet("background: #2196F3; font-weight: bold;")

    def save_json(self):
        p, _ = QFileDialog.getSaveFileName(self, "모든 캠페인 저장", "", "JSON (*.json)")
        if p:
            with open(p, 'w', encoding='utf-8') as f: json.dump(self.master_schedules, f, ensure_ascii=False, indent=4)

    def load_json(self):
        p, _ = QFileDialog.getOpenFileName(self, "캠페인 불러오기", "", "JSON (*.json)")
        if p:
            with open(p, 'r', encoding='utf-8') as f: data = json.load(f)
            if isinstance(data, dict):
                if 'schedules' in data:
                    for s in data['schedules']:
                        if 'hour' in s and 'start_time' not in s: h = int(s['hour']); s['start_time'] = f"{h:02d}:00"; s['end_time'] = f"{(h+1)%24:02d}:00" 
                        s['selected'] = False
                new_camp = {'id': QDateTime.currentMSecsSinceEpoch(), 'name': data.get('name', '불러온 스케줄'), 'start_date': QDate.currentDate().toString("yyyy-MM-dd"), 'end_date': QDate.currentDate().addMonths(1).toString("yyyy-MM-dd"), 'on_time': data.get('on', "09:00"), 'off_time': data.get('off', "18:00"), 'days': data.get('days', ["월","화","수","목","금","토","일"]), 'show_logo': True, 'show_time': True, 'show_weather': True, 'playlist_data': data.get('schedules', [])}
                self.master_schedules = [new_camp]
            elif isinstance(data, list): 
                for c in data:
                    for s in c.get('playlist_data', []):
                        if 'selected' not in s: s['selected'] = False
                self.master_schedules = data
            self.refresh_campaign_list()
            if self.master_schedules: self.camp_table.selectRow(0); self.load_campaign_to_editor(0, 0)

    def publish_all(self): 
        self.is_pub = True; self.pb.cur_layout_id = "FORCE_REFRESH"; self.pb.show(); self.pb.raise_(); self.pb.activateWindow(); self.btn_pub_all.setText("✅ 모든 스케줄 발행 중"); self.btn_pub_all.setStyleSheet("background: #4CAF50; font-weight: bold;"); self.global_loop()

    def stop_all(self): 
        self.is_pub = False; self.pb.cur_layout_id = "FORCE_STOP"; self.pb.show()
        opts = {'logo': True, 'time': True, 'weather': True}
        if self.current_camp_idx >= 0: 
            opts['logo'] = self.master_schedules[self.current_camp_idx].get('show_logo', True)
            opts['time'] = self.master_schedules[self.current_camp_idx].get('show_time', True)
            opts['weather'] = self.master_schedules[self.current_camp_idx].get('show_weather', True)
        self.pb.update_playback(1920, 1080, None, opts); self.btn_pub_all.setText("📢 스케줄 자동 발행 시작"); self.btn_pub_all.setStyleSheet("background: #2196F3; font-weight: bold;")

    def global_loop(self):
        if not self.is_pub: return
        now = QDateTime.currentDateTime(); active_camp = None
        for camp in self.master_schedules:
            if QDate.fromString(camp['start_date'], "yyyy-MM-dd") <= now.date() <= QDate.fromString(camp['end_date'], "yyyy-MM-dd"): active_camp = camp; break
            
        opts = {'logo': False, 'time': False, 'weather': False}
        if active_camp:
            opts = {'logo': active_camp.get('show_logo', True), 'time': active_camp.get('show_time', True), 'weather': active_camp.get('show_weather', True)}
            
        if not active_camp: self.pb.update_playback(1920, 1080, None, opts); return
        on_t, off_t, days = QTime.fromString(active_camp['on_time'], "HH:mm"), QTime.fromString(active_camp['off_time'], "HH:mm"), active_camp['days']
        valid_day = any(d == ["월","화","수","목","금","토","일"][now.date().dayOfWeek()-1] for d in days)
        
        if not valid_day or not (on_t <= now.time() <= off_t): self.pb.update_playback(1920, 1080, None, opts); return
        curr_t_str = now.time().toString("HH:mm"); has_s = False
        for s in active_camp['playlist_data']:
            start, end = s['start_time'], s['end_time']
            if (start < end and start <= curr_t_str < end) or (start > end and (curr_t_str >= start or curr_t_str < end)):
                self.pb.update_playback(s['res_w'], s['res_h'], s['secs'], None); has_s = True; break
        if not has_s: self.pb.update_playback(1920, 1080, None, opts)

    def setup_user_system(self):
        """사용자 시스템을 초기화하고, 기본 관리자 계정이 없으면 생성합니다."""
        self.users = load_users()
        if not self.users:
            print("기본 관리자 계정을 생성합니다. (admin/admin)")
            admin_pass = hash_password('admin')
            self.users = {
                'admin': {
                    'password': admin_pass,
                    'role': 'Admin'
                }
            }
            save_users(self.users)
            
    def apply_role_permissions(self):
        """로그인한 사용자의 역할에 따라 UI 접근 권한을 설정합니다."""
        is_admin = self.current_user and self.current_user['role'] == 'Admin'

        # 사용자 관리 버튼은 관리자에게만 보임
        self.btn_user_mgmt.setVisible(is_admin)
        
        # 캠페인 관리 기능은 관리자만 활성화
        self.btn_new_camp.setEnabled(is_admin)
        self.btn_del_camp.setEnabled(is_admin)
        self.btn_copy_camp.setEnabled(is_admin)
        self.btn_paste_camp.setEnabled(is_admin)

        # 시스템 제어 기능도 관리자만 활성화
        self.btn_save_all.setEnabled(is_admin)
        self.btn_load_all.setEnabled(is_admin)
        
        # 일반 사용자인 경우, 색상이 지정된 버튼들의 스타일을 비활성화 스타일로 덮어씀
        if not is_admin:
            disabled_style = "background-color: #2A2A2A; color: #555; border: 1px solid #444;"
            self.btn_new_camp.setStyleSheet(disabled_style)
            self.btn_del_camp.setStyleSheet(disabled_style)
            self.btn_copy_camp.setStyleSheet(disabled_style)
            self.btn_paste_camp.setStyleSheet(disabled_style)
        else:
            # 관리자인 경우, 원래 스타일로 복원
            self.btn_new_camp.setStyleSheet("color: #4CAF50; font-weight: bold;")
            self.btn_del_camp.setStyleSheet("color: #CF6679;")
            self.btn_copy_camp.setStyleSheet("color: #FF9800; font-weight: bold;")
            self.btn_paste_camp.setStyleSheet("color: #2196F3; font-weight: bold;")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # 사용자 데이터 로드 및 초기 관리자 계정 확인
    users = load_users()
    if not users:
        # 이 로직은 UnifiedDashboard의 setup_user_system에서도 호출되지만,
        # 로그인 다이얼로그가 대시보드보다 먼저 뜨므로 여기서도 한 번 더 확인하여 생성해줍니다.
        print("최초 실행: 기본 관리자 계정을 생성합니다. (ID: admin, PW: admin)")
        admin_pass = hash_password('admin')
        users = {'admin': {'password': admin_pass, 'role': 'Admin'}}
        save_users(users)

    # 로그인 다이얼로그 실행
    login_dialog = LoginDialog(users)
    
    if login_dialog.exec() == QDialog.DialogCode.Accepted:
        # 로그인 성공
        user_info = login_dialog.user_info
        print(f"로그인 성공: {user_info['username']} (권한: {user_info['role']})")
        
        playback = PlaybackWindow()
        dashboard = UnifiedDashboard(playback)
        dashboard.current_user = user_info # 로그인한 사용자 정보 대시보드에 전달
        dashboard.apply_role_permissions() # 역할 기반 권한 적용
        
        playback.show()
        dashboard.show()
        sys.exit(app.exec())
    else:
        # 로그인 취소
        print("로그인이 취소되었습니다. 프로그램을 종료합니다.")
        sys.exit(0)
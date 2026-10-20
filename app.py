import os
import requests
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, 
    url_for, flash, session, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Exif解析用ライブラリ (Pillow)
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key_here'

# データベース設定 (SQLite)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'oshikatsu.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 画像アップロード設定
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db = SQLAlchemy(app)


# --- データベースモデル ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    memos = db.relationship('Memo', backref='user', lazy=True, cascade="all, delete-orphan")


class Memo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    oshikatsu_date = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD 形式
    activity_type = db.Column(db.String(50), nullable=True)    # 推し活の種類 (例: ライブ, グッズ購入)
    oshi_names_str = db.Column(db.String(200), nullable=True)  # カンマ区切りの推し名
    content = db.Column(db.Text, nullable=False)
    color = db.Column(db.String(20), default='#4b7bec')
    image_files_str = db.Column(db.Text, nullable=True)        # カンマ区切りの画像ファイル名
    
    # 位置情報用カラム
    location_name = db.Column(db.String(100), nullable=True)   # 場所名
    lat = db.Column(db.Float, nullable=True)                    # 緯度
    lng = db.Column(db.Float, nullable=True)                    # 経度

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def oshi_names(self):
        if self.oshi_names_str:
            raw = self.oshi_names_str.replace('、', ',').replace(' ', ',')
            return [name.strip() for name in raw.split(',') if name.strip()]
        return []

    @property
    def image_files(self):
        if self.image_files_str:
            return [img.strip() for img in self.image_files_str.split(',') if img.strip()]
        return []


# --- ヘルパー関数 ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('ログインが必要です。')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# ★ 画像ファイルから Exif GPS 情報を抽出する関数
def extract_gps_from_image(image_file):
    try:
        image = Image.open(image_file)
        exif_data = image._getexif()
        
        if not exif_data:
            return None, None

        gps_info = {}
        for tag, value in exif_data.items():
            decoded = TAGS.get(tag, tag)
            if decoded == "GPSInfo":
                for key in value:
                    sub_tag = GPSTAGS.get(key, key)
                    gps_info[sub_tag] = value[key]

        if not gps_info or 'GPSLatitude' not in gps_info or 'GPSLongitude' not in gps_info:
            return None, None

        def convert_to_degrees(value):
            d = float(value[0])
            m = float(value[1])
            s = float(value[2])
            return d + (m / 60.0) + (s / 360.0)

        lat = convert_to_degrees(gps_info['GPSLatitude'])
        if gps_info.get('GPSLatitudeRef') != 'N':
            lat = -lat

        lng = convert_to_degrees(gps_info['GPSLongitude'])
        if gps_info.get('GPSLongitudeRef') != 'E':
            lng = -lng

        return lat, lng

    except Exception as e:
        print(f"Exif GPS抽出エラー: {e}")
        return None, None


# ★ 場所名から自動で緯度・経度を取得する関数 (Nominatim API)
def geocode_location(location_name):
    if not location_name or not location_name.strip():
        return None, None
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': location_name,
            'format': 'json',
            'limit': 1
        }
        headers = {'User-Agent': 'OshikatsuApp/1.0'}
        response = requests.get(url, params=params, headers=headers, timeout=5)
        data = response.json()
        if data:
            return float(data[0]['lat']), float(data[0]['lon'])
    except Exception as e:
        print(f"ジオコーディングエラー: {e}")
    return None, None


# --- ルーティング ---

# トップページ
@app.route('/')
def welcome():
    return render_template('welcome.html')


# 一覧画面（推し名・種類での絞り込み機能を追加）
@app.route('/list')
@login_required
def list_memos():
    selected_oshi = request.args.get('oshi', '').strip()
    selected_type = request.args.get('type', '').strip()

    query = Memo.query.filter_by(user_id=session['user_id'])

    if selected_type:
        query = query.filter(Memo.activity_type == selected_type)

    if selected_oshi:
        query = query.filter(Memo.oshi_names_str.like(f"%{selected_oshi}%"))

    memos = query.order_by(Memo.oshikatsu_date.desc()).all()

    all_user_memos = Memo.query.filter_by(user_id=session['user_id']).all()
    
    all_oshi_set = set()
    for memo in all_user_memos:
        for name in memo.oshi_names:
            all_oshi_set.add(name)
    all_oshi_list = sorted(list(all_oshi_set))

    all_types_set = {m.activity_type for m in all_user_memos if m.activity_type}
    all_types_list = sorted(list(all_types_set))

    return render_template(
        'list.html', 
        memos=memos,
        all_oshi_list=all_oshi_list,
        all_types_list=all_types_list,
        selected_oshi=selected_oshi,
        selected_type=selected_type
    )


# ★ カレンダー & 年表（タイムライン）切替画面
@app.route('/calendar')
@login_required
def calendar_page():
    # 年表用に日付の古い順 (asc) で推し活メモを取得
    timeline_memos = Memo.query.filter_by(user_id=session['user_id'])\
                               .order_by(Memo.oshikatsu_date.asc()).all()
    return render_template('calendar.html', timeline_memos=timeline_memos)


# マップ画面
@app.route('/map')
@login_required
def map_page():
    return render_template('map.html')


# FullCalendar 用 API
@app.route('/api/memos')
@login_required
def api_memos():
    memos = Memo.query.filter_by(user_id=session['user_id']).all()
    events = []
    
    for memo in memos:
        events.append({
            'id': memo.id,
            'title': memo.activity_type or '推し活',
            'start': memo.oshikatsu_date,
            'backgroundColor': memo.color or '#4b7bec',
            'borderColor': memo.color or '#4b7bec',
            'extendedProps': {
                'activity_type': memo.activity_type,
                'oshi_names': memo.oshi_names,
                'content': memo.content,
                'image_files': memo.image_files,
                'color': memo.color,
                'location_name': memo.location_name,
                'lat': memo.lat,
                'lng': memo.lng
            }
        })
        
    return jsonify(events)


# ★ 年表（タイムライン）用 API
@app.route('/api/timeline')
@login_required
def api_timeline():
    memos = Memo.query.filter_by(user_id=session['user_id'])\
                       .order_by(Memo.oshikatsu_date.asc()).all()
    timeline_data = []
    for memo in memos:
        timeline_data.append({
            'id': memo.id,
            'date': memo.oshikatsu_date,
            'activity_type': memo.activity_type or '推し活',
            'oshi_names': memo.oshi_names,
            'content': memo.content,
            'color': memo.color or '#4b7bec',
            'image_files': memo.image_files,
            'location_name': memo.location_name
        })
    return jsonify(timeline_data)


# マップ用 API (位置情報付きメモのみ取得)
@app.route('/api/memos/locations')
@login_required
def api_memo_locations():
    memos = Memo.query.filter(
        Memo.user_id == session['user_id'],
        Memo.lat.isnot(None),
        Memo.lng.isnot(None)
    ).all()

    locations = []
    for memo in memos:
        first_image_path = None
        if memo.image_files:
            first_image_path = f"/static/uploads/{memo.image_files[0]}"

        locations.append({
            'id': memo.id,
            'title': memo.activity_type or '推し活',
            'oshi_name': memo.oshi_names[0] if memo.oshi_names else '推し活',
            'activity_type': memo.activity_type,
            'date': memo.oshikatsu_date,
            'location_name': memo.location_name,
            'lat': memo.lat,
            'lng': memo.lng,
            'color': memo.color or '#ff4757',
            'content': memo.content,
            'image_files': memo.image_files,
            'image_path': first_image_path
        })
    return jsonify(locations)


@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_page():
    if request.method == 'POST':
        oshikatsu_date = request.form.get('oshikatsu_date')
        activity_type = request.form.get('activity_type')
        
        oshi_names_list = request.form.getlist('oshi_names')
        combined_names = ",".join(oshi_names_list)
        
        content = request.form.get('content')
        color = request.form.get('color', '#4b7bec')

        # 位置情報の取得
        location_name = request.form.get('location_name')
        lat_raw = request.form.get('lat')
        lng_raw = request.form.get('lng')

        lat = float(lat_raw) if lat_raw else None
        lng = float(lng_raw) if lng_raw else None

        # 複数画像のアップロード処理 ＆ Exif GPS情報抽出
        uploaded_files = request.files.getlist('image_files')
        saved_filenames = []
        exif_lat, exif_lng = None, None

        for file in uploaded_files:
            if file and allowed_file(file.filename):
                if exif_lat is None and exif_lng is None:
                    exif_lat, exif_lng = extract_gps_from_image(file)
                    file.seek(0)

                filename = secure_filename(file.filename)
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
                unique_filename = f"{timestamp}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                saved_filenames.append(unique_filename)

        if exif_lat is not None and exif_lng is not None:
            lat, lng = exif_lat, exif_lng
        elif location_name and (lat is None or lng is None):
            auto_lat, auto_lng = geocode_location(location_name)
            lat = lat or auto_lat
            lng = lng or auto_lng

        new_memo = Memo(
            user_id=session['user_id'],
            oshikatsu_date=oshikatsu_date,
            activity_type=activity_type,
            oshi_names_str=combined_names,
            content=content,
            color=color,
            image_files_str=','.join(saved_filenames) if saved_filenames else None,
            location_name=location_name,
            lat=lat,
            lng=lng
        )

        db.session.add(new_memo)
        db.session.commit()
        flash('新しい推し活記録を追加しました！')
        return redirect(url_for('list_memos'))

    return render_template('add.html')


@app.route('/edit/<int:memo_id>', methods=['GET', 'POST'])
@login_required
def edit_memo(memo_id):
    memo = Memo.query.filter_by(id=memo_id, user_id=session['user_id']).first_or_404()

    if request.method == 'POST':
        memo.oshikatsu_date = request.form.get('oshikatsu_date')
        memo.activity_type = request.form.get('activity_type')
        
        oshi_names_list = request.form.getlist('oshi_names')
        memo.oshi_names_str = ",".join(oshi_names_list)

        memo.content = request.form.get('content')
        memo.color = request.form.get('color', '#4b7bec')

        location_name = request.form.get('location_name')
        lat_raw = request.form.get('lat')
        lng_raw = request.form.get('lng')

        lat = float(lat_raw) if lat_raw else None
        lng = float(lng_raw) if lng_raw else None

        delete_images = request.form.getlist('delete_images')
        current_images = memo.image_files
        for img_to_delete in delete_images:
            if img_to_delete in current_images:
                current_images.remove(img_to_delete)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], img_to_delete)
                if os.path.exists(filepath):
                    os.remove(filepath)

        uploaded_files = request.files.getlist('image_files')
        exif_lat, exif_lng = None, None

        for file in uploaded_files:
            if file and allowed_file(file.filename):
                if exif_lat is None and exif_lng is None:
                    exif_lat, exif_lng = extract_gps_from_image(file)
                    file.seek(0)

                filename = secure_filename(file.filename)
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
                unique_filename = f"{timestamp}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                current_images.append(unique_filename)

        if exif_lat is not None and exif_lng is not None:
            lat, lng = exif_lat, exif_lng
        elif location_name and (lat is None or lng is None or location_name != memo.location_name):
            auto_lat, auto_lng = geocode_location(location_name)
            lat = lat if lat is not None else auto_lat
            lng = lng if lng is not None else auto_lng

        memo.image_files_str = ','.join(current_images) if current_images else None
        memo.location_name = location_name
        memo.lat = lat
        memo.lng = lng

        db.session.commit()
        flash('記録を更新しました。')
        return redirect(url_for('list_memos'))

    return render_template('edit.html', memo=memo)


@app.route('/delete/<int:memo_id>', methods=['POST'])
@login_required
def delete_memo(memo_id):
    memo = Memo.query.filter_by(id=memo_id, user_id=session['user_id']).first_or_404()

    for img in memo.image_files:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], img)
        if os.path.exists(filepath):
            os.remove(filepath)

    db.session.delete(memo)
    db.session.commit()
    flash('記録を削除しました。')
    return redirect(url_for('list_memos'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
            flash('ログインしました。')
            return redirect(url_for('list_memos'))
        
        flash('ユーザー名またはパスワードが正しくありません。')

    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if User.query.filter_by(username=username).first():
            flash('そのユーザー名は既に登録されています。')
            return redirect(url_for('register'))

        new_user = User(
            username=username,
            password_hash=generate_password_hash(password)
        )
        db.session.add(new_user)
        db.session.commit()
        flash('会員登録が完了しました。ログインしてください。')
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('ログアウトしました。')
    return redirect(url_for('login'))


# アプリ起動時にテーブルを作成
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
import os
import math
from datetime import datetime, timedelta
import cv2
import io
import zipfile
import numpy as np
import pickle
from flask import Flask, render_template, request, redirect, session, url_for, jsonify, send_file
import sqlite3
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'replace-this-with-something-random-later'

UPLOAD_FOLDER = os.path.join('static', 'uploads')
FACE_FOLDER = os.path.join('static', 'faces')

OFFICE_LATITUDE = 9.9396938
OFFICE_LONGITUDE = 78.1326017
ALLOWED_RADIUS_METERS = 500
# ---------- ATTENDANCE ANOMALY SETTINGS ----------
LATE_THRESHOLD_TIME = "10:00:00"        # check-ins after this time count as late
LATE_FREQUENCY_THRESHOLD = 5            # flag if late this many times in a single calendar month
CONSECUTIVE_ABSENCE_THRESHOLD = 3       # flag at this many consecutive missed working days
ABSENCE_LOOKBACK_DAYS = 30              # how far back to check for consecutive absences
LEAVE_FREQUENCY_THRESHOLD = 5           # flag if approved leave covers this many days in a single calendar month

# ---------- TAMIL NADU GOVERNMENT HOLIDAYS ----------
# Official list per G.O. (Ms.) No.708, Public (Misc.) Department, dated 11-Nov-2025.
# Add the next year's list here once it's officially notified.
TN_GOVT_HOLIDAYS = {
    2026: {
        "2026-01-01",  # New Year's Day
        "2026-01-15",  # Pongal
        "2026-01-16",  # Thiruvalluvar Day
        "2026-01-17",  # Uzhavar Thirunal
        "2026-01-26",  # Republic Day
        "2026-02-01",  # Thai Poosam
        "2026-03-19",  # Telugu New Year's Day
        "2026-03-21",  # Ramzan (Id-ul-Fitr)
        "2026-03-31",  # Mahaveer Jayanthi
        "2026-04-03",  # Good Friday
        "2026-04-14",  # Tamil New Year / Dr. B.R. Ambedkar Jayanthi
        "2026-05-01",  # May Day
        "2026-05-28",  # Bakrid (Id-ul-Zuha)
        "2026-06-26",  # Muharram
        "2026-08-15",  # Independence Day
        "2026-08-26",  # Milad-un-Nabi
        "2026-09-04",  # Krishna Jayanthi
        "2026-09-14",  # Vinayakar Chathurthi
        "2026-10-02",  # Gandhi Jayanthi
        "2026-10-19",  # Ayutha Pooja
        "2026-10-20",  # Vijaya Dashami
        "2026-11-08",  # Deepavali
        "2026-12-25",  # Christmas
    },
}


def calculate_distance(lat1, lon1, lat2, lon2):
    """
    Returns the distance in meters between two lat/lon points
    using the Haversine formula.
    """
    R = 6371000  # Earth's radius in meters

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) *
         math.sin(delta_lambda / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c

def is_tn_govt_holiday(date_str):
    year = int(date_str[:4])
    return date_str in TN_GOVT_HOLIDAYS.get(year, set())


def get_approved_leave_dates(cursor):
    """Returns {username: set_of_date_strings} covering every day inside an
    approved leave request (inclusive of start_date and end_date)."""
    cursor.execute(
        "SELECT username, start_date, end_date FROM leave_requests WHERE status = 'Approved'"
    )
    leave_dates_by_user = {}
    for username, start_date, end_date in cursor.fetchall():
        start = datetime.strptime(start_date, '%Y-%m-%d').date()
        end = datetime.strptime(end_date, '%Y-%m-%d').date()
        dates = leave_dates_by_user.setdefault(username, set())
        d = start
        while d <= end:
            dates.add(d.strftime('%Y-%m-%d'))
            d += timedelta(days=1)
    return leave_dates_by_user


def get_attendance_anomalies():
    """
    Scans the attendance table and flags staff who are either:
      - Late (earliest check-in after LATE_THRESHOLD_TIME) on
        LATE_FREQUENCY_THRESHOLD or more days within any single
        calendar month
      - Absent on CONSECUTIVE_ABSENCE_THRESHOLD or more consecutive
        working days, counted backwards from yesterday
      - On approved leave for LEAVE_FREQUENCY_THRESHOLD or more days
        within any single calendar month

    Sundays, approved leave days, and Tamil Nadu government holidays
    are excluded from the working-day count (they neither count as an
    absence nor break a streak).

    Staff who have never checked in at all are excluded from the
    consecutive-absence check (no baseline to measure against yet).
    """
    today = datetime.now().date()
    absence_window_start = today - timedelta(days=ABSENCE_LOOKBACK_DAYS)

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()

    cursor.execute("SELECT username FROM staff WHERE role = 'staff'")
    staff_usernames = [row[0] for row in cursor.fetchall()]

    cursor.execute("SELECT username, MIN(date) FROM attendance GROUP BY username")
    first_seen_by_user = dict(cursor.fetchall())
    ever_checked_in = set(first_seen_by_user.keys())

    # Recent window only - used for the consecutive-absence check
    cursor.execute(
        "SELECT username, date, MIN(time) FROM attendance WHERE date >= ? GROUP BY username, date",
        (absence_window_start.strftime('%Y-%m-%d'),)
    )
    recent_rows = cursor.fetchall()

    # Full history, grouped per day - used for the per-month lateness check
    cursor.execute("SELECT username, date, MIN(time) FROM attendance GROUP BY username, date")
    all_rows = cursor.fetchall()

    leave_dates_by_user = get_approved_leave_dates(cursor)

    conn.close()

    recent_records_by_user = {}
    for username, date_str, earliest_time in recent_rows:
        recent_records_by_user.setdefault(username, {})[date_str] = earliest_time

    # Count late days per user, bucketed by calendar month ('YYYY-MM')
    late_days_by_user_month = {}
    for username, date_str, earliest_time in all_rows:
        if earliest_time and earliest_time > LATE_THRESHOLD_TIME:
            month_key = date_str[:7]
            month_counts = late_days_by_user_month.setdefault(username, {})
            month_counts[month_key] = month_counts.get(month_key, 0) + 1

    # Count approved-leave days per user, bucketed by calendar month
    leave_days_by_user_month = {}
    for username, dates in leave_dates_by_user.items():
        month_counts = leave_days_by_user_month.setdefault(username, {})
        for date_str in dates:
            month_key = date_str[:7]
            month_counts[month_key] = month_counts.get(month_key, 0) + 1

    anomalies = []

    for username in staff_usernames:
        flags = []

        for month_key, count in sorted(late_days_by_user_month.get(username, {}).items()):
            if count >= LATE_FREQUENCY_THRESHOLD:
                month_label = datetime.strptime(month_key, '%Y-%m').strftime('%B %Y')
                flags.append(f"Late {count}x in {month_label}")

        for month_key, count in sorted(leave_days_by_user_month.get(username, {}).items()):
            if count >= LEAVE_FREQUENCY_THRESHOLD:
                month_label = datetime.strptime(month_key, '%Y-%m').strftime('%B %Y')
                flags.append(f"{count} days of approved leave in {month_label}")

        user_records = recent_records_by_user.get(username, {})
        user_leave_dates = leave_dates_by_user.get(username, set())
        consecutive_absences = 0
        if username in ever_checked_in:
            first_seen = first_seen_by_user.get(username)
            check_date = today - timedelta(days=1)
            while (today - check_date).days <= ABSENCE_LOOKBACK_DAYS:
                date_str = check_date.strftime('%Y-%m-%d')
                if first_seen and date_str < first_seen:
                    break  # don't count days before their first ever check-in

                is_sunday = check_date.weekday() == 6
                is_holiday = is_tn_govt_holiday(date_str)
                is_on_leave = date_str in user_leave_dates

                if not is_sunday and not is_holiday and not is_on_leave:
                    if date_str in user_records:
                        break
                    consecutive_absences += 1
                # Sundays / holidays / leave days are skipped entirely -
                # they don't break the streak or add to it.

                check_date -= timedelta(days=1)

        if consecutive_absences >= CONSECUTIVE_ABSENCE_THRESHOLD:
            flags.append(f"{consecutive_absences} consecutive working days absent")

        if flags:
            anomalies.append({
                'username': username,
                'flags': flags
            })

    return anomalies

# ---------- LOGIN / LOGOUT ----------

@app.route('/', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = sqlite3.connect('ngo.db')
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM staff WHERE username = ?", (username,))
        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user[2], password):
            session['username'] = user[1]
            session['role'] = user[3]
            return redirect('/dashboard')
        else:
            error = "Invalid username or password"

    return render_template('login.html', error=error)


@app.route('/dashboard')
def dashboard():
    if 'username' not in session:
        return redirect('/')

    if session['role'] == 'admin':
        return render_template('admin_dashboard.html',
                                session_username=session['username'],
                                session_role=session['role'])
    else:
        return render_template('staff_dashboard.html',
                                session_username=session['username'],
                                session_role=session['role'])


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')


# ---------- PHOTOS ----------

# ---------- PHOTOS (with 1-level subfolder support) ----------

@app.route('/photos')
def photos():
    if 'username' not in session:
        return redirect('/')

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    # Only TOP-LEVEL folders here (no parent_folder set)
    cursor.execute(
        "SELECT programme, COUNT(*) FROM photos "
        "WHERE filename != '' AND (parent_folder IS NULL OR parent_folder = '') "
        "GROUP BY programme"
    )
    folders = cursor.fetchall()
    conn.close()

    return render_template('photos.html', folders=folders)


@app.route('/photos/create_folder', methods=['POST'])
def create_folder():
    if 'username' not in session:
        return redirect('/')

    programme = request.form['programme']
    parent = request.form.get('parent', '').strip() or None

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()

    if parent:
        cursor.execute(
            "SELECT id FROM photos WHERE programme = ? AND parent_folder = ?",
            (programme, parent)
        )
    else:
        cursor.execute(
            "SELECT id FROM photos WHERE programme = ? AND (parent_folder IS NULL OR parent_folder = '')",
            (programme,)
        )
    existing = cursor.fetchone()

    if not existing:
        cursor.execute(
            "INSERT INTO photos (filename, programme, uploaded_by, parent_folder) VALUES (?, ?, ?, ?)",
            ('', programme, session['username'], parent)
        )
        conn.commit()
    conn.close()

    if parent:
        return redirect(url_for('photo_subfolder', programme=parent, subfolder=programme))
    return redirect(url_for('photo_folder', programme=programme))


@app.route('/photos/folder/<programme>', methods=['GET', 'POST'])
def photo_folder(programme):
    if 'username' not in session:
        return redirect('/')

    if request.method == 'POST':
        file = request.files['photo']
        if file:
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

            conn = sqlite3.connect('ngo.db')
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO photos (filename, programme, uploaded_by, parent_folder) VALUES (?, ?, ?, NULL)",
                (filename, programme, session['username'])
            )
            conn.commit()
            conn.close()

        return redirect(url_for('photo_folder', programme=programme))

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()

    # Photos directly inside this top-level folder
    cursor.execute(
        "SELECT * FROM photos WHERE programme = ? AND filename != '' "
        "AND (parent_folder IS NULL OR parent_folder = '') ORDER BY id DESC",
        (programme,)
    )
    folder_photos = cursor.fetchall()

    # Subfolders that live inside this folder
    # Subfolders that live inside this folder (count only real photos,
    # but still show folders that have zero photos yet)
    cursor.execute(
        "SELECT programme, COUNT(CASE WHEN filename != '' THEN 1 END) "
        "FROM photos WHERE parent_folder = ? GROUP BY programme",
        (programme,)
    )
    subfolders = cursor.fetchall()
    conn.close()

    return render_template(
        'photo_folder.html',
        programme=programme,
        photos=folder_photos,
        subfolders=subfolders
    )


@app.route('/photos/folder/<programme>/<subfolder>', methods=['GET', 'POST'])
def photo_subfolder(programme, subfolder):
    if 'username' not in session:
        return redirect('/')

    if request.method == 'POST':
        file = request.files['photo']
        if file:
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

            conn = sqlite3.connect('ngo.db')
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO photos (filename, programme, uploaded_by, parent_folder) VALUES (?, ?, ?, ?)",
                (filename, subfolder, session['username'], programme)
            )
            conn.commit()
            conn.close()

        return redirect(url_for('photo_subfolder', programme=programme, subfolder=subfolder))

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM photos WHERE programme = ? AND parent_folder = ? AND filename != '' ORDER BY id DESC",
        (subfolder, programme)
    )
    subfolder_photos = cursor.fetchall()
    conn.close()

    return render_template(
        'photo_subfolder.html',
        programme=programme,
        subfolder=subfolder,
        photos=subfolder_photos
    )
@app.route('/photos/delete_photo/<int:photo_id>', methods=['POST'])
def delete_photo(photo_id):
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Not logged in.'})

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute("SELECT filename FROM photos WHERE id = ?", (photo_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return jsonify({'success': False, 'message': 'Photo not found.'})

    filename = row[0]

    cursor.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    conn.commit()
    conn.close()

    if filename:
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass  # DB row is already gone; ignore file cleanup failures

    return jsonify({'success': True})


@app.route('/photos/delete_folder/<programme>', methods=['POST'])
def delete_folder(programme):
    """Deletes a TOP-LEVEL folder, all photos directly inside it,
    all its subfolders, and all photos inside those subfolders."""
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Not logged in.'})

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()

    cursor.execute(
        "SELECT filename FROM photos WHERE "
        "(programme = ? AND (parent_folder IS NULL OR parent_folder = '')) "
        "OR parent_folder = ?",
        (programme, programme)
    )
    filenames = [row[0] for row in cursor.fetchall() if row[0]]

    cursor.execute(
        "DELETE FROM photos WHERE "
        "(programme = ? AND (parent_folder IS NULL OR parent_folder = '')) "
        "OR parent_folder = ?",
        (programme, programme)
    )
    conn.commit()
    conn.close()

    for filename in filenames:
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass

    return jsonify({'success': True})


@app.route('/photos/delete_subfolder/<programme>/<subfolder>', methods=['POST'])
def delete_subfolder(programme, subfolder):
    """Deletes one subfolder and all photos inside it."""
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Not logged in.'})

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()

    cursor.execute(
        "SELECT filename FROM photos WHERE programme = ? AND parent_folder = ?",
        (subfolder, programme)
    )
    filenames = [row[0] for row in cursor.fetchall() if row[0]]

    cursor.execute(
        "DELETE FROM photos WHERE programme = ? AND parent_folder = ?",
        (subfolder, programme)
    )
    conn.commit()
    conn.close()

    for filename in filenames:
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass

    return jsonify({'success': True})


# ---------- ZIP DOWNLOADS ----------

def _build_zip_response(filenames, zip_name):
    """Builds an in-memory zip of the given filenames (from UPLOAD_FOLDER)
    and returns a Flask send_file response."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename in filenames:
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            if os.path.exists(filepath):
                zf.write(filepath, arcname=filename)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=zip_name
    )


@app.route('/photos/download_folder/<programme>')
def download_folder(programme):
    """Zips every photo directly inside this top-level folder (not subfolders)."""
    if 'username' not in session:
        return redirect('/')

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT filename FROM photos WHERE programme = ? AND filename != '' "
        "AND (parent_folder IS NULL OR parent_folder = '')",
        (programme,)
    )
    filenames = [row[0] for row in cursor.fetchall()]
    conn.close()

    if not filenames:
        return "No photos to download in this folder.", 404

    safe_name = secure_filename(programme) or 'folder'
    return _build_zip_response(filenames, f"{safe_name}.zip")


@app.route('/photos/download_subfolder/<programme>/<subfolder>')
def download_subfolder(programme, subfolder):
    """Zips every photo inside this subfolder."""
    if 'username' not in session:
        return redirect('/')

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT filename FROM photos WHERE programme = ? AND parent_folder = ? AND filename != ''",
        (subfolder, programme)
    )
    filenames = [row[0] for row in cursor.fetchall()]
    conn.close()

    if not filenames:
        return "No photos to download in this subfolder.", 404

    safe_name = secure_filename(f"{programme}_{subfolder}") or 'subfolder'
    return _build_zip_response(filenames, f"{safe_name}.zip")


@app.route('/photos/download_selected', methods=['POST'])
def download_selected():
    """Zips a specific set of photo IDs the user checked via checkboxes."""
    if 'username' not in session:
        return redirect('/')

    photo_ids = request.form.getlist('photo_ids')
    if not photo_ids:
        return "No photos selected.", 400

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    placeholders = ','.join('?' for _ in photo_ids)
    cursor.execute(
        f"SELECT filename FROM photos WHERE id IN ({placeholders}) AND filename != ''",
        photo_ids
    )
    filenames = [row[0] for row in cursor.fetchall()]
    conn.close()

    if not filenames:
        return "No matching photos found.", 404

    return _build_zip_response(filenames, "selected_photos.zip")

# ---------- ATTENDANCE ----------

@app.route('/attendance')
def attendance():
    if 'username' not in session:
        return redirect('/')

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM attendance WHERE username = ? ORDER BY id DESC",
        (session['username'],)
    )
    records = cursor.fetchall()
    conn.close()

    return render_template('attendance.html', records=records)


@app.route('/attendance/checkin', methods=['POST'])
def attendance_checkin():
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Not logged in.'})

    data = request.get_json()
    lat = data['latitude']
    lon = data['longitude']

    distance = calculate_distance(OFFICE_LATITUDE, OFFICE_LONGITUDE, lat, lon)

    if distance <= ALLOWED_RADIUS_METERS:
        now = datetime.now()
        today_str = now.strftime('%Y-%m-%d')

        conn = sqlite3.connect('ngo.db')
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id FROM attendance WHERE username = ? AND date = ?",
            (session['username'], today_str)
        )
        already_marked = cursor.fetchone()

        if already_marked:
            conn.close()
            return jsonify({'success': False, 'message': 'Attendance already marked for today.'})

        cursor.execute(
            "INSERT INTO attendance (username, date, time, latitude, longitude, status) VALUES (?, ?, ?, ?, ?, ?)",
            (session['username'], today_str, now.strftime('%H:%M:%S'), lat, lon, 'Present')
        )
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': f'Attendance marked! You were {int(distance)}m from the office.'})
    else:
        return jsonify({'success': False, 'message': f'Too far from office ({int(distance)}m away). You must be within {ALLOWED_RADIUS_METERS}m.'})


# ---------- FACE ENROLLMENT ----------

@app.route('/face/enroll-page')
def enroll_page():
    if 'username' not in session:
        return redirect('/')
    return render_template('enroll_face.html')


@app.route('/face/enroll', methods=['POST'])
def face_enroll():
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Not logged in.'})

    file = request.files['photo']
    file_bytes = np.frombuffer(file.read(), np.uint8)
    image = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    faces = face_cascade.detectMultiScale(gray, 1.3, 5)

    if len(faces) == 0:
        return jsonify({'success': False, 'message': 'No face detected. Please face the camera directly.'})

    (x, y, w, h) = faces[0]
    face_crop = gray[y:y+h, x:x+w]
    face_crop = cv2.resize(face_crop, (200, 200))

    username = session['username']
    user_folder = os.path.join(FACE_FOLDER, username)
    os.makedirs(user_folder, exist_ok=True)

    existing_count = len([f for f in os.listdir(user_folder) if f.endswith('.jpg')])
    filename = f"{existing_count + 1}.jpg"
    filepath = os.path.join(user_folder, filename)
    cv2.imwrite(filepath, face_crop)

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO face_data (username, image_path) VALUES (?, ?)",
        (username, filepath)
    )
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': f'Photo {existing_count + 1} captured successfully.'})


@app.route('/face/checkin-page')
def checkin_page():
    if 'username' not in session:
        return redirect('/')
    return render_template('face_checkin.html')


@app.route('/face/checkin', methods=['POST'])
def face_checkin():
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Not logged in.'})

    today_str = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM attendance WHERE username = ? AND date = ?",
        (session['username'], today_str)
    )
    already_marked = cursor.fetchone()
    conn.close()

    if already_marked:
        return jsonify({'success': False, 'message': 'Attendance already marked for today.'})

    if not os.path.exists('face_model.yml'):
        return jsonify({'success': False, 'message': 'No trained model found. Ask admin to enroll and train faces first.'})

    recognizer = cv2.face.LBPHFaceRecognizer_create()
    recognizer.read('face_model.yml')

    with open('label_map.pkl', 'rb') as f:
        label_to_username = pickle.load(f)

    file = request.files['photo']
    file_bytes = np.frombuffer(file.read(), np.uint8)
    image = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    faces = face_cascade.detectMultiScale(gray, 1.3, 5)

    if len(faces) == 0:
        return jsonify({'success': False, 'message': 'No face detected. Please face the camera directly.'})

    (x, y, w, h) = faces[0]
    face_crop = gray[y:y+h, x:x+w]
    face_crop = cv2.resize(face_crop, (200, 200))

    label, confidence = recognizer.predict(face_crop)
    recognized_username = label_to_username.get(label, None)

    CONFIDENCE_THRESHOLD = 70

    if confidence > CONFIDENCE_THRESHOLD or recognized_username != session['username']:
        return jsonify({'success': False, 'message': f'Face not recognized confidently. Try again with better lighting.'})

    now = datetime.now()
    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO attendance (username, date, time, latitude, longitude, status) VALUES (?, ?, ?, ?, ?, ?)",
        (session['username'], today_str, now.strftime('%H:%M:%S'), None, None, 'Present (Face)')
    )
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': f'Welcome, {session["username"]}! Attendance marked successfully.'})


# ---------- MANAGE STAFF (admin only) ----------
@app.route('/manage-staff', methods=['GET', 'POST'])
def manage_staff():
    if 'username' not in session or session['role'] != 'admin':
        return redirect('/dashboard')

    error = request.args.get('error')

    if request.method == 'POST':
        new_username = request.form['username']
        new_password = request.form['password']

        conn = sqlite3.connect('ngo.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM staff WHERE username = ?", (new_username,))
        existing = cursor.fetchone()

        if existing:
            error = "That username already exists."
        else:
            hashed = generate_password_hash(new_password)
            cursor.execute(
                "INSERT INTO staff (username, password, role) VALUES (?, ?, ?)",
                (new_username, hashed, 'staff')
            )
            conn.commit()
        conn.close()

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM staff")
    staff_list = cursor.fetchall()
    conn.close()

    return render_template('manage_staff.html', staff_list=staff_list, error=error)


@app.route('/manage-staff/edit/<int:staff_id>', methods=['POST'])
def edit_staff(staff_id):
    if 'username' not in session or session['role'] != 'admin':
        return redirect('/dashboard')

    new_username = request.form.get('username', '').strip()
    new_password = request.form.get('password', '').strip()

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()

    cursor.execute("SELECT username, role FROM staff WHERE id = ?", (staff_id,))
    target = cursor.fetchone()

    if not target:
        conn.close()
        return redirect(url_for('manage_staff', error="Staff member not found."))

    if target[1] == 'admin':
        conn.close()
        return redirect(url_for('manage_staff', error="Admin accounts can't be edited here."))

    if new_username:
        cursor.execute("SELECT id FROM staff WHERE username = ? AND id != ?", (new_username, staff_id))
        clash = cursor.fetchone()
        if clash:
            conn.close()
            return redirect(url_for('manage_staff', error="That username is already taken."))
        cursor.execute("UPDATE staff SET username = ? WHERE id = ?", (new_username, staff_id))

    if new_password:
        hashed = generate_password_hash(new_password)
        cursor.execute("UPDATE staff SET password = ? WHERE id = ?", (hashed, staff_id))

    conn.commit()
    conn.close()

    return redirect(url_for('manage_staff'))


@app.route('/manage-staff/delete/<int:staff_id>', methods=['POST'])
def delete_staff(staff_id):
    if 'username' not in session or session['role'] != 'admin':
        return redirect('/dashboard')

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()

    cursor.execute("SELECT role FROM staff WHERE id = ?", (staff_id,))
    target = cursor.fetchone()

    if target and target[0] != 'admin':
        cursor.execute("DELETE FROM staff WHERE id = ?", (staff_id,))
        conn.commit()
    elif target and target[0] == 'admin':
        conn.close()
        return redirect(url_for('manage_staff', error="Admin accounts can't be deleted."))

    conn.close()
    return redirect(url_for('manage_staff'))

@app.route('/admin/attendance')
def admin_attendance():
    if 'username' not in session or session['role'] != 'admin':
        return redirect('/dashboard')

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM attendance ORDER BY date DESC, time DESC")
    records = cursor.fetchall()
    conn.close()

    anomalies = get_attendance_anomalies()

    return render_template('admin_attendance.html', records=records, anomalies=anomalies)


@app.route('/leave', methods=['GET', 'POST'])
def leave():
    if 'username' not in session:
        return redirect('/')

    if request.method == 'POST':
        start_date = request.form['start_date']
        end_date = request.form['end_date']
        reason = request.form['reason']

        conn = sqlite3.connect('ngo.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO leave_requests (username, start_date, end_date, reason, status) VALUES (?, ?, ?, ?, ?)",
            (session['username'], start_date, end_date, reason, 'Pending')
        )
        conn.commit()
        conn.close()

        return redirect('/leave')

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM leave_requests WHERE username = ? ORDER BY id DESC",
        (session['username'],)
    )
    requests = cursor.fetchall()
    conn.close()

    return render_template('leave.html', requests=requests)


@app.route('/admin/leave')
def admin_leave():
    if 'username' not in session or session['role'] != 'admin':
        return redirect('/dashboard')

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM leave_requests ORDER BY id DESC")
    requests = cursor.fetchall()

    leave_dates_by_user = get_approved_leave_dates(cursor)
    conn.close()

    # Bucket each user's *approved* leave days by calendar month ('YYYY-MM')
    leave_days_by_user_month = {}
    for username, dates in leave_dates_by_user.items():
        month_counts = leave_days_by_user_month.setdefault(username, {})
        for date_str in dates:
            month_key = date_str[:7]
            month_counts[month_key] = month_counts.get(month_key, 0) + 1

    # For every request row, check if that staff member already has
    # LEAVE_FREQUENCY_THRESHOLD+ approved days in any month the request
    # touches, so the admin sees a warning before approving more.
    request_warnings = {}
    for row in requests:
        leave_id = row[0]
        username = row[1]
        start_date = row[2]
        end_date = row[3]

        start = datetime.strptime(start_date, '%Y-%m-%d').date()
        end = datetime.strptime(end_date, '%Y-%m-%d').date()

        months_touched = set()
        d = start
        while d <= end:
            months_touched.add(d.strftime('%Y-%m'))
            d += timedelta(days=1)

        user_month_counts = leave_days_by_user_month.get(username, {})
        flagged_months = []
        for month_key in sorted(months_touched):
            count = user_month_counts.get(month_key, 0)
            if count >= LEAVE_FREQUENCY_THRESHOLD:
                month_label = datetime.strptime(month_key, '%Y-%m').strftime('%B %Y')
                flagged_months.append(f"{count} approved days in {month_label}")

        if flagged_months:
            request_warnings[leave_id] = flagged_months

    return render_template('admin_leave.html', requests=requests, request_warnings=request_warnings)


@app.route('/admin/leave/<int:leave_id>/<action>', methods=['POST'])
def leave_action(leave_id, action):
    if 'username' not in session or session['role'] != 'admin':
        return redirect('/dashboard')

    conn = sqlite3.connect('ngo.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE leave_requests SET status = ? WHERE id = ?", (action, leave_id))
    conn.commit()
    conn.close()

    return redirect('/admin/leave')


if __name__ == '__main__':
    app.run(debug=True)
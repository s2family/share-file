#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File Storage & Sharing Service
Admin controlled storage duration, performance optimized
"""

import sqlite3
from datetime import datetime, timedelta, timezone

# Fix SQLite datetime warning
sqlite3.register_adapter(datetime, lambda dt: dt.isoformat())
sqlite3.register_converter("DATETIME", lambda dt: datetime.fromisoformat(dt.decode()))

from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, session, abort, Response
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import os, uuid, hashlib, time, threading, secrets, mimetypes, qrcode, io, base64
from functools import wraps
import logging
import shutil
import mmap
import concurrent.futures

# ===== LOGGING SETUP =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# ===== CONFIGURATION =====
UPLOAD_FOLDER = os.path.join('static', 'uploads', 'banners')
STORAGE_FOLDER = os.path.join('storage', 'files')
TEMP_FOLDER = os.path.join(STORAGE_FOLDER, 'temp')

# Create directories
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(STORAGE_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

BANNER_MAX_SIZE = 16 * 1024 * 1024  # 16MB for banners
ALLOWED_BANNER_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# Unlimited timeout cho file lớn
from werkzeug.serving import WSGIRequestHandler
WSGIRequestHandler.timeout = 0  # Unlimited

# ===== DATABASE SETUP =====
def init_db():
    conn = sqlite3.connect('file_storage.db')
    cursor = conn.cursor()
    
    # Files table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            file_type TEXT,
            file_size INTEGER DEFAULT 0,
            mime_type TEXT,
            share_code TEXT UNIQUE NOT NULL,
            password TEXT,
            download_limit INTEGER DEFAULT 100,
            download_count INTEGER DEFAULT 0,
            uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME,
            last_accessed DATETIME,
            uploader_ip TEXT,
            description TEXT,
            is_public BOOLEAN DEFAULT 1
        )
    ''')
    
    # Visitors table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS visitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE,
            ip_address TEXT,
            user_agent TEXT,
            first_visit DATETIME,
            last_activity DATETIME,
            page_views INTEGER DEFAULT 1,
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    
    # Banners table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS banners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            image_path TEXT,
            link_url TEXT,
            position TEXT CHECK(position IN ('left', 'right')),
            clicks INTEGER DEFAULT 0,
            status BOOLEAN DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Settings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            description TEXT
        )
    ''')
    
    # Download stats table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS download_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT,
            file_name TEXT,
            download_ip TEXT,
            download_time DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_agent TEXT,
            FOREIGN KEY (file_id) REFERENCES files (id)
        )
    ''')
    
    # Insert default settings
    default_settings = [
        ('admin_username', 'admin', 'Admin username'),
        ('admin_password_hash', generate_password_hash('admin123'), 'Admin password hash'),
        ('site_title', 'File Storage & Sharing', 'Site title'),
        ('maintenance_mode', 'false', 'Maintenance mode'),
        ('auto_cleanup_enabled', 'false', 'Auto cleanup enabled'),
        ('cleanup_interval_minutes', '60', 'Cleanup interval in minutes'),
        ('default_expire_days', '30', 'Default file expiration days'),
        ('max_file_size_gb', '25', 'Maximum file size in GB'),
        ('max_download_limit', '100', 'Default max downloads per file'),
        
        # Performance settings
        ('chunk_size_mb', '32', 'Upload chunk size in MB'),
        ('max_concurrent_chunks', '8', 'Maximum concurrent chunks'),
        ('max_workers', '8', 'Maximum worker threads'),
        ('enable_chunked_upload', 'true', 'Enable chunked upload for large files'),
        ('enable_compression', 'true', 'Enable gzip compression'),
        ('enable_caching', 'true', 'Enable file caching'),
        ('buffer_size_kb', '2048', 'I/O buffer size in KB'),
        ('connection_timeout', '300', 'Connection timeout in seconds')
    ]
    
    for key, value, desc in default_settings:
        cursor.execute('INSERT OR IGNORE INTO settings (key, value, description) VALUES (?, ?, ?)', 
                      (key, value, desc))
    
    conn.commit()
    conn.close()

# Initialize database
init_db()

# ===== PERFORMANCE FUNCTIONS =====
def get_performance_settings():
    """Get performance-related settings"""
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT key, value FROM settings 
            WHERE key IN (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            'chunk_size_mb', 'max_concurrent_chunks', 'max_workers',
            'enable_chunked_upload', 'enable_compression', 'enable_caching',
            'buffer_size_kb', 'connection_timeout'
        ))
        
        settings = dict(cursor.fetchall())
        conn.close()
        
        return {
            'chunk_size_mb': int(settings.get('chunk_size_mb', 32)),
            'max_concurrent_chunks': int(settings.get('max_concurrent_chunks', 8)),
            'max_workers': int(settings.get('max_workers', 8)),
            'enable_chunked_upload': settings.get('enable_chunked_upload', 'true') == 'true',
            'enable_compression': settings.get('enable_compression', 'true') == 'true',
            'enable_caching': settings.get('enable_caching', 'true') == 'true',
            'buffer_size_kb': int(settings.get('buffer_size_kb', 2048)),
            'connection_timeout': int(settings.get('connection_timeout', 300))
        }
    except Exception as e:
        logger.error(f"Error getting performance settings: {e}")
        return {
            'chunk_size_mb': 32,
            'max_concurrent_chunks': 8, 
            'max_workers': 8,
            'enable_chunked_upload': True,
            'enable_compression': True,
            'enable_caching': True,
            'buffer_size_kb': 2048,
            'connection_timeout': 300
        }

def get_max_content_length():
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM settings WHERE key = ?', ('max_file_size_gb',))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            max_gb = int(result[0])
            return max_gb * 1024 * 1024 * 1024
        else:
            return 25 * 1024 * 1024 * 1024
    except:
        return 25 * 1024 * 1024 * 1024

# Set initial max content length
app.config['MAX_CONTENT_LENGTH'] = get_max_content_length()

# ===== VISITOR TRACKING =====
class VisitorTracker:
    def __init__(self):
        self.active_visitors = {}
        self.cleanup_thread = threading.Thread(target=self.cleanup_inactive_visitors, daemon=True)
        self.cleanup_thread.start()
    
    def track_visitor(self, request):
        session_id = session.get('session_id')
        if not session_id:
            session_id = str(uuid.uuid4())
            session['session_id'] = session_id
        
        ip_address = request.remote_addr
        user_agent = request.headers.get('User-Agent', '')
        current_time = datetime.now(timezone.utc)
        
        self.active_visitors[session_id] = current_time
        
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT id, page_views FROM visitors WHERE session_id = ?', (session_id,))
        visitor = cursor.fetchone()
        
        if visitor:
            cursor.execute('''
                UPDATE visitors 
                SET last_activity = ?, page_views = page_views + 1, is_active = 1
                WHERE session_id = ?
            ''', (current_time, session_id))
        else:
            cursor.execute('''
                INSERT INTO visitors (session_id, ip_address, user_agent, first_visit, last_activity)
                VALUES (?, ?, ?, ?, ?)
            ''', (session_id, ip_address, user_agent, current_time, current_time))
        
        conn.commit()
        conn.close()
        
        return session_id
    
    def get_active_count(self):
        current_time = datetime.now(timezone.utc)
        cutoff_time = current_time - timedelta(minutes=5)
        
        self.active_visitors = {
            sid: last_activity for sid, last_activity in self.active_visitors.items()
            if last_activity > cutoff_time
        }
        
        return len(self.active_visitors)
    
    def cleanup_inactive_visitors(self):
        while True:
            try:
                time.sleep(300)
                current_time = datetime.now(timezone.utc)
                cutoff_time = current_time - timedelta(minutes=10)
                
                conn = sqlite3.connect('file_storage.db')
                cursor = conn.cursor()
                cursor.execute('UPDATE visitors SET is_active = 0 WHERE last_activity < ?', (cutoff_time,))
                conn.commit()
                conn.close()
                
            except Exception as e:
                logger.error(f"Visitor cleanup error: {e}")

visitor_tracker = VisitorTracker()

# ===== CACHE SCHEDULER =====
class CacheScheduler:
    def __init__(self):
        self.scheduler_thread = threading.Thread(target=self.run_scheduler, daemon=True)
        self.scheduler_thread.start()
        
    def run_scheduler(self):
        while True:
            try:
                time.sleep(60)
                
                conn = sqlite3.connect('file_storage.db')
                cursor = conn.cursor()
                
                cursor.execute('SELECT value FROM settings WHERE key = ?', ('auto_cleanup_enabled',))
                enabled = cursor.fetchone()
                enabled = enabled and enabled[0] == 'true'
                
                if not enabled:
                    conn.close()
                    continue
                
                cursor.execute('SELECT value FROM settings WHERE key = ?', ('cleanup_interval_minutes',))
                interval = cursor.fetchone()
                interval = int(interval[0]) if interval else 60
                
                conn.close()
                
                if hasattr(self, 'last_cleanup'):
                    time_since_last = (datetime.now(timezone.utc) - self.last_cleanup).total_seconds() / 60
                    if time_since_last < interval:
                        continue
                
                logger.info("Running scheduled cache cleanup...")
                deleted_count = self.cleanup_expired_files()
                logger.info(f"Scheduled cleanup completed: {deleted_count} files deleted")
                self.last_cleanup = datetime.now(timezone.utc)
                
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
    
    def cleanup_expired_files(self):
        try:
            current_time = datetime.now(timezone.utc)
            deleted_count = 0
            
            conn = sqlite3.connect('file_storage.db')
            cursor = conn.cursor()
            
            cursor.execute('SELECT id, stored_name FROM files WHERE expires_at < ?', (current_time.isoformat(),))
            expired_files = cursor.fetchall()
            
            for file_id, stored_name in expired_files:
                try:
                    file_path = os.path.join(STORAGE_FOLDER, stored_name)
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    
                    cursor.execute('DELETE FROM files WHERE id = ?', (file_id,))
                    cursor.execute('DELETE FROM download_stats WHERE file_id = ?', (file_id,))
                    deleted_count += 1
                    
                except Exception as e:
                    logger.error(f"Error deleting file {file_id}: {e}")
            
            conn.commit()
            conn.close()
            
            return deleted_count
            
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            return 0

cache_scheduler = CacheScheduler()

# ===== ADMIN AUTHENTICATION =====
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# ===== UTILITY FUNCTIONS =====
def get_admin_settings():
    """Get admin-controlled settings"""
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT key, value FROM settings WHERE key IN (?, ?, ?)', 
                      ('default_expire_days', 'max_file_size_gb', 'max_download_limit'))
        settings = dict(cursor.fetchall())
        conn.close()
        
        return {
            'expire_days': int(settings.get('default_expire_days', 30)),
            'max_size_gb': int(settings.get('max_file_size_gb', 25)),
            'download_limit': int(settings.get('max_download_limit', 100))
        }
    except:
        return {'expire_days': 30, 'max_size_gb': 25, 'download_limit': 100}

def get_banners(position=None):
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        if position:
            cursor.execute('SELECT * FROM banners WHERE position = ? AND status = 1 ORDER BY id DESC', (position,))
        else:
            cursor.execute('SELECT * FROM banners WHERE status = 1 ORDER BY id DESC')
        
        banners = cursor.fetchall()
        conn.close()
        
        banner_list = []
        for banner in banners:
            banner_list.append({
                'id': banner[0],
                'title': banner[1],
                'description': banner[2],
                'image_path': banner[3],
                'link_url': banner[4],
                'position': banner[5],
                'clicks': banner[6],
                'status': banner[7],
                'created_at': banner[8]
            })
        
        return banner_list
        
    except Exception as e:
        logger.error(f"Error getting banners: {e}")
        return []

def generate_share_code():
    return hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[:12]

def get_file_type(filename):
    if not filename or '.' not in filename:
        return 'other'
    
    ext = filename.rsplit('.', 1)[1].lower()
    
    file_types = {
        'image': ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg'],
        'video': ['mp4', 'avi', 'mov', 'mkv', 'flv', 'webm', 'wmv'],
        'audio': ['mp3', 'wav', 'flac', 'aac', 'ogg', 'wma'],
        'document': ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt'],
        'archive': ['zip', 'rar', '7z', 'tar', 'gz']
    }
    
    for file_type, extensions in file_types.items():
        if ext in extensions:
            return file_type
    
    return 'other'

def format_file_size(size_bytes):
    if size_bytes == 0:
        return "0B"
    
    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while size_bytes >= 1024 and i < len(size_names) - 1:
        size_bytes /= 1024.0
        i += 1
    
    return f"{size_bytes:.1f}{size_names[i]}"

def generate_qr_code(url):
    try:
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        img_buffer = io.BytesIO()
        img.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        
        return base64.b64encode(img_buffer.getvalue()).decode()
    except:
        return None

def read_chunk_file(temp_dir, chunk_index):
    """Read chunk file efficiently"""
    chunk_path = os.path.join(temp_dir, f'chunk_{chunk_index:06d}')
    with open(chunk_path, 'rb') as f:
        return f.read()

def merge_chunks_high_speed(temp_dir, final_path, total_chunks):
    """High-speed parallel chunk merging"""
    perf_settings = get_performance_settings()
    
    with open(final_path, 'wb') as final_file:
        with concurrent.futures.ThreadPoolExecutor(max_workers=perf_settings['max_workers']) as executor:
            chunk_data = {}
            
            # Submit all read operations
            future_to_chunk = {
                executor.submit(read_chunk_file, temp_dir, i): i 
                for i in range(total_chunks)
            }
            
            # Collect results
            for future in concurrent.futures.as_completed(future_to_chunk):
                chunk_index = future_to_chunk[future]
                chunk_data[chunk_index] = future.result()
            
            # Write in order with large buffer
            for i in range(total_chunks):
                final_file.write(chunk_data[i])
                del chunk_data[i]

# ===== MIDDLEWARE =====
@app.before_request
def track_visitors():
    if request.endpoint and not request.endpoint.startswith('static'):
        visitor_tracker.track_visitor(request)

@app.before_request
def update_max_content_length():
    """Update file size limit before upload requests"""
    if request.endpoint == 'upload_file' or request.endpoint == 'upload_chunked':
        app.config['MAX_CONTENT_LENGTH'] = get_max_content_length()

# ===== MAIN ROUTES =====
@app.route('/')
def index():
    try:
        left_banners = get_banners('left')
        right_banners = get_banners('right')
        admin_settings = get_admin_settings()
        
        return render_template('index.html', 
                             left_banners=left_banners, 
                             right_banners=right_banners,
                             recent_files=[],
                             stats=None,
                             admin_settings=admin_settings)
                             
    except Exception as e:
        logger.error(f"Homepage error: {e}")
        admin_settings = {'expire_days': 30, 'max_size_gb': 25, 'download_limit': 100}
        return render_template('index.html', 
                             left_banners=[], 
                             right_banners=[],
                             recent_files=[],
                             stats=None,
                             admin_settings=admin_settings)

@app.route('/api/performance-config')
def get_performance_config():
    """API to get performance settings for JavaScript"""
    try:
        settings = get_performance_settings()
        return jsonify({
            'success': True,
            'config': {
                'chunkSizeMB': settings['chunk_size_mb'],
                'maxConcurrentChunks': settings['max_concurrent_chunks'],
                'enableChunkedUpload': settings['enable_chunked_upload'],
                'bufferSizeKB': settings['buffer_size_kb'],
                'connectionTimeout': settings['connection_timeout']
            }
        })
    except Exception as e:
        logger.error(f"Performance config error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        logger.info("Upload request received")
        
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Không có file được chọn'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'Không có file được chọn'}), 400
        
        admin_settings = get_admin_settings()
        perf_settings = get_performance_settings()
        
        # Check file size
        if hasattr(file, 'content_length') and file.content_length:
            max_size = admin_settings['max_size_gb'] * 1024 * 1024 * 1024
            if file.content_length > max_size:
                return jsonify({'success': False, 'error': f'File quá lớn. Tối đa {admin_settings["max_size_gb"]}GB'}), 413
        
        description = request.form.get('description', '')
        expire_days = admin_settings['expire_days']
        download_limit = admin_settings['download_limit'] 
        is_public = True
        
        # Generate file info
        file_id = str(uuid.uuid4())
        share_code = generate_share_code()
        original_name = secure_filename(file.filename)
        file_ext = original_name.rsplit('.', 1)[1].lower() if '.' in original_name else ''
        stored_name = f"{file_id}.{file_ext}" if file_ext else file_id
        file_path = os.path.join(STORAGE_FOLDER, stored_name)
        
        file_type = get_file_type(original_name)
        expires_at = datetime.now(timezone.utc) + timedelta(days=expire_days)
        
        # Create database record first
        try:
            conn = sqlite3.connect('file_storage.db')
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO files (
                    id, original_name, stored_name, file_type, file_size, 
                    mime_type, share_code, password, download_limit, expires_at, 
                    uploader_ip, description, is_public
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                file_id, original_name, stored_name, file_type, 0,
                file.mimetype or 'application/octet-stream', share_code, None, 
                download_limit, expires_at, request.remote_addr, description, is_public
            ))
            
            conn.commit()
            conn.close()
            
        except Exception as db_error:
            logger.error(f"Database error: {db_error}")
            return jsonify({'success': False, 'error': f'Lỗi cơ sở dữ liệu: {str(db_error)}'}), 500
        
        # Save file with optimized I/O
        try:
            buffer_size = perf_settings['buffer_size_kb'] * 1024
            total_size = 0
            
            with open(file_path, 'wb') as f:
                while True:
                    chunk = file.stream.read(buffer_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    total_size += len(chunk)
            
            # Update file size
            conn = sqlite3.connect('file_storage.db')
            cursor = conn.cursor()
            cursor.execute('UPDATE files SET file_size = ? WHERE id = ?', (total_size, file_id))
            conn.commit()
            conn.close()
            
        except Exception as save_error:
            logger.error(f"Error saving file: {save_error}")
            # Cleanup
            try:
                conn = sqlite3.connect('file_storage.db')
                cursor = conn.cursor()
                cursor.execute('DELETE FROM files WHERE id = ?', (file_id,))
                conn.commit()
                conn.close()
            except:
                pass
            return jsonify({'success': False, 'error': f'Lỗi lưu file: {str(save_error)}'}), 500
        
        # Generate response
        share_url = request.url_root + f"f/{share_code}"
        qr_code = generate_qr_code(share_url)
        
        return jsonify({
            'success': True,
            'file_id': file_id,
            'share_code': share_code,
            'share_url': share_url,
            'qr_code': qr_code,
            'expires_at': expires_at.isoformat(),
            'expire_days': expire_days,
            'file_size': format_file_size(total_size),
            'file_type': file_type
        })
        
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return jsonify({'success': False, 'error': f'Lỗi tải lên: {str(e)}'}), 500

@app.route('/upload/chunked', methods=['POST'])
def upload_chunked():
    try:
        chunk_number = int(request.form.get('chunkNumber', 0))
        total_chunks = int(request.form.get('totalChunks', 1))
        file_id = request.form.get('fileId')
        original_filename = request.form.get('filename')
        
        if not file_id:
            file_id = str(uuid.uuid4())
        
        chunk_data = request.files['chunk']
        perf_settings = get_performance_settings()
        
        # Create temp directory
        temp_dir = os.path.join(TEMP_FOLDER, file_id)
        os.makedirs(temp_dir, exist_ok=True)
        
        # Save chunk with optimized I/O
        chunk_path = os.path.join(temp_dir, f'chunk_{chunk_number:06d}')
        buffer_size = perf_settings['buffer_size_kb'] * 1024
        
        with open(chunk_path, 'wb') as f:
            while True:
                data = chunk_data.stream.read(buffer_size)
                if not data:
                    break
                f.write(data)
        
        # Check completion
        uploaded_chunks = len([f for f in os.listdir(temp_dir) if f.startswith('chunk_')])
        
        if uploaded_chunks == total_chunks:
            # Merge chunks
            admin_settings = get_admin_settings()
            final_filename = secure_filename(original_filename)
            file_ext = final_filename.rsplit('.', 1)[1].lower() if '.' in final_filename else ''
            stored_name = f"{file_id}.{file_ext}" if file_ext else file_id
            final_path = os.path.join(STORAGE_FOLDER, stored_name)
            
            merge_chunks_high_speed(temp_dir, final_path, total_chunks)
            
            # Cleanup temp
            shutil.rmtree(temp_dir)
            
            # Create database record
            file_size = os.path.getsize(final_path)
            share_code = generate_share_code()
            file_type = get_file_type(original_filename)
            expires_at = datetime.now(timezone.utc) + timedelta(days=admin_settings['expire_days'])
            
            conn = sqlite3.connect('file_storage.db')
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO files (
                    id, original_name, stored_name, file_type, file_size, 
                    mime_type, share_code, password, download_limit, expires_at, 
                    uploader_ip, description, is_public
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                file_id, original_filename, stored_name, file_type, file_size,
                'application/octet-stream', share_code, None, 
                admin_settings['download_limit'], expires_at, request.remote_addr, '', True
            ))
            
            conn.commit()
            conn.close()
            
            share_url = request.url_root + f"f/{share_code}"
            qr_code = generate_qr_code(share_url)
            
            return jsonify({
                'success': True,
                'completed': True,
                'file_id': file_id,
                'share_code': share_code,
                'share_url': share_url,
                'qr_code': qr_code,
                'file_size': format_file_size(file_size)
            })
        
        return jsonify({
            'success': True,
            'completed': False,
            'uploaded_chunks': uploaded_chunks,
            'total_chunks': total_chunks,
            'progress': (uploaded_chunks / total_chunks) * 100
        })
        
    except Exception as e:
        logger.error(f"Chunked upload error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/f/<share_code>')
def share_page(share_code):
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM files WHERE share_code = ?', (share_code,))
        result = cursor.fetchone()
        
        if not result:
            conn.close()
            abort(404)
        
        file_data = {
            'id': result[0],
            'original_name': result[1],
            'file_type': result[3],
            'file_size': format_file_size(result[4]),
            'share_code': result[6],
            'has_password': False,
            'download_limit': result[8],
            'download_count': result[9],
            'expires_at': result[11],
            'description': result[14]
        }
        
        # Check if expired
        if file_data['expires_at']:
            expires_at = datetime.fromisoformat(file_data['expires_at'].replace('Z', '+00:00'))
            if datetime.now(timezone.utc) > expires_at:
                conn.close()
                return render_template('index.html', error='File đã hết hạn')
        
        # Check download limit
        if file_data['download_count'] >= file_data['download_limit']:
            conn.close()
            return render_template('index.html', error='File đã đạt giới hạn tải xuống')
        
        # Update last accessed
        cursor.execute('UPDATE files SET last_accessed = ? WHERE share_code = ?', 
                      (datetime.now(timezone.utc), share_code))
        conn.commit()
        conn.close()
        
        return render_template('index.html', shared_file=file_data, show_download=True)
        
    except Exception as e:
        logger.error(f"Share page error: {e}")
        abort(500)

@app.route('/download/<share_code>')
def download_file(share_code):
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM files WHERE share_code = ?', (share_code,))
        result = cursor.fetchone()
        
        if not result:
            conn.close()
            abort(404)
        
        file_data = {
            'id': result[0],
            'original_name': result[1],
            'stored_name': result[2],
            'download_limit': result[8],
            'download_count': result[9],
            'expires_at': result[11]
        }
        
        # Check if expired
        if file_data['expires_at']:
            expires_at = datetime.fromisoformat(file_data['expires_at'].replace('Z', '+00:00'))
            if datetime.now(timezone.utc) > expires_at:
                conn.close()
                return jsonify({'error': 'File đã hết hạn'}), 410
        
        # Check download limit
        if file_data['download_count'] >= file_data['download_limit']:
            conn.close()
            return jsonify({'error': 'File đã đạt giới hạn tải xuống'}), 403
        
        # Check if file exists
        file_path = os.path.join(STORAGE_FOLDER, file_data['stored_name'])
        if not os.path.exists(file_path):
            conn.close()
            return jsonify({'error': 'File không tồn tại'}), 404
        
        file_size = os.path.getsize(file_path)
        perf_settings = get_performance_settings()
        
        # Update download count and log
        cursor.execute('UPDATE files SET download_count = download_count + 1, last_accessed = ? WHERE share_code = ?', 
                      (datetime.now(timezone.utc), share_code))
        
        cursor.execute('''
            INSERT INTO download_stats (file_id, file_name, download_ip, user_agent)
            VALUES (?, ?, ?, ?)
        ''', (file_data['id'], file_data['original_name'], 
              request.remote_addr, request.headers.get('User-Agent', '')))
        
        conn.commit()
        conn.close()
        
        # High-speed streaming download
        def generate_file_stream():
            with open(file_path, 'rb') as f:
                if file_size > 100 * 1024 * 1024:  # Files > 100MB use memory mapping
                    with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped_file:
                        chunk_size = perf_settings['buffer_size_kb'] * 1024
                        offset = 0
                        while offset < len(mmapped_file):
                            chunk = mmapped_file[offset:offset + chunk_size]
                            yield chunk
                            offset += chunk_size
                else:
                    # Smaller files use regular read
                    chunk_size = perf_settings['buffer_size_kb'] * 1024
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        yield chunk
        
        response = Response(
            generate_file_stream(),
            mimetype='application/octet-stream',
            headers={
                'Content-Disposition': f'attachment; filename="{file_data["original_name"]}"',
                'Content-Length': str(file_size),
                'Accept-Ranges': 'bytes',
                'Cache-Control': 'no-cache'
            }
        )
        
        return response
        
    except Exception as e:
        logger.error(f"Download error: {e}")
        return jsonify({'error': str(e)}), 500

# ===== BANNER ROUTES =====
@app.route('/banner/click/<int:banner_id>')
def banner_click(banner_id):
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        cursor.execute('UPDATE banners SET clicks = clicks + 1 WHERE id = ?', (banner_id,))
        cursor.execute('SELECT link_url FROM banners WHERE id = ?', (banner_id,))
        result = cursor.fetchone()
        
        conn.commit()
        conn.close()
        
        if result and result[0]:
            return redirect(result[0])
        else:
            return redirect(url_for('index'))
            
    except Exception as e:
        logger.error(f"Banner click error: {e}")
        return redirect(url_for('index'))

# ===== ADMIN ROUTES =====
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM settings WHERE key = ?', ('admin_username',))
        db_username = cursor.fetchone()[0]
        cursor.execute('SELECT value FROM settings WHERE key = ?', ('admin_password_hash',))
        db_password_hash = cursor.fetchone()[0]
        conn.close()
        
        if username == db_username and check_password_hash(db_password_hash, password):
            session['admin_logged_in'] = True
            session['admin_username'] = username
            return redirect(url_for('admin_dashboard'))
        else:
            return render_template('admin_login.html', error='Sai tên đăng nhập hoặc mật khẩu')
    
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    session.pop('admin_username', None)
    return redirect(url_for('admin_login'))

@app.route('/admin')
@admin_required
def admin_dashboard():
    return render_template('admin.html')

# ===== ADMIN API ROUTES =====
@app.route('/admin/api/stats')
@admin_required
def admin_stats():
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        # Active visitors
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        cursor.execute('SELECT COUNT(*) FROM visitors WHERE is_active = 1 AND last_activity > ?', (cutoff_time,))
        active_visitors = cursor.fetchone()[0]
        
        # Total files
        cursor.execute('SELECT COUNT(*) FROM files WHERE expires_at > ?', (datetime.now(timezone.utc),))
        total_files = cursor.fetchone()[0]
        
        # Total downloads
        cursor.execute('SELECT SUM(download_count) FROM files')
        total_downloads = cursor.fetchone()[0] or 0
        
        # Active banners
        cursor.execute('SELECT COUNT(*) FROM banners WHERE status = 1')
        active_banners = cursor.fetchone()[0]
        
        conn.close()
        
        return jsonify({
            'success': True,
            'stats': {
                'visitors': {'active_now': active_visitors},
                'files': {'total_files': total_files, 'total_downloads': total_downloads},
                'banners': {'active_banners': active_banners}
            }
        })
        
    except Exception as e:
        logger.error(f"Stats API error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/api/performance', methods=['GET', 'POST'])
@admin_required
def admin_performance():
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        if request.method == 'GET':
            cursor.execute('''
                SELECT key, value, description FROM settings 
                WHERE key IN (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                'chunk_size_mb', 'max_concurrent_chunks', 'max_workers',
                'enable_chunked_upload', 'enable_compression', 'enable_caching', 
                'buffer_size_kb', 'connection_timeout'
            ))
            
            settings = []
            for row in cursor.fetchall():
                settings.append({
                    'key': row[0],
                    'value': row[1],
                    'description': row[2]
                })
            
            conn.close()
            return jsonify({'success': True, 'settings': settings})
            
        elif request.method == 'POST':
            data = request.get_json()
            settings_to_update = data.get('settings', {})
            
            # Validate performance settings
            validation_errors = []
            
            for key, value in settings_to_update.items():
                if key == 'chunk_size_mb':
                    if not (1 <= int(value) <= 100):
                        validation_errors.append('Chunk size phải từ 1-100 MB')
                elif key == 'max_concurrent_chunks':
                    if not (1 <= int(value) <= 20):
                        validation_errors.append('Max concurrent chunks phải từ 1-20')
                elif key == 'max_workers':
                    if not (1 <= int(value) <= 20):
                        validation_errors.append('Max workers phải từ 1-20')
                elif key == 'buffer_size_kb':
                    if not (64 <= int(value) <= 8192):
                        validation_errors.append('Buffer size phải từ 64-8192 KB')
                elif key == 'connection_timeout':
                    if not (30 <= int(value) <= 3600):
                        validation_errors.append('Connection timeout phải từ 30-3600 giây')
            
            if validation_errors:
                conn.close()
                return jsonify({'success': False, 'error': '; '.join(validation_errors)})
            
            # Update settings
            for key, value in settings_to_update.items():
                cursor.execute('UPDATE settings SET value = ? WHERE key = ?', (value, key))
            
            conn.commit()
            conn.close()
            
            logger.info("Performance settings updated")
            return jsonify({'success': True, 'message': 'Cài đặt hiệu suất đã được cập nhật'})
            
    except Exception as e:
        logger.error(f"Performance API error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/api/cache', methods=['GET', 'POST'])
@admin_required
def admin_cache():
    try:
        if request.method == 'GET':
            total_files = 0
            total_size = 0
            
            for filename in os.listdir(STORAGE_FOLDER):
                file_path = os.path.join(STORAGE_FOLDER, filename)
                if os.path.isfile(file_path):
                    total_files += 1
                    total_size += os.path.getsize(file_path)
            
            cache_info = {
                'total_files': total_files,
                'total_size_mb': round(total_size / (1024 * 1024), 2)
            }
            
            return jsonify({'success': True, 'cache_info': cache_info})
            
        elif request.method == 'POST':
            data = request.get_json()
            action = data.get('action')
            
            if action == 'cleanup_old':
                deleted_count = cache_scheduler.cleanup_expired_files()
                message = f'Đã xóa {deleted_count} file hết hạn'
                
            elif action == 'clear_all':
                deleted_count = 0
                try:
                    conn = sqlite3.connect('file_storage.db')
                    cursor = conn.cursor()
                    
                    cursor.execute('SELECT stored_name FROM files')
                    all_files = cursor.fetchall()
                    
                    for (stored_name,) in all_files:
                        file_path = os.path.join(STORAGE_FOLDER, stored_name)
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            deleted_count += 1
                    
                    cursor.execute('DELETE FROM files')
                    cursor.execute('DELETE FROM download_stats')
                    
                    conn.commit()
                    conn.close()
                    
                    message = f'Đã xóa tất cả {deleted_count} file'
                    
                except Exception as e:
                    logger.error(f"Clear all cache error: {e}")
                    return jsonify({'success': False, 'error': str(e)})
                
            else:
                return jsonify({'success': False, 'error': 'Invalid action'})
            
            return jsonify({
                'success': True,
                'message': message,
                'deleted_count': deleted_count
            })
            
    except Exception as e:
        logger.error(f"Cache API error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/api/banners', methods=['GET', 'POST', 'PUT', 'DELETE'])
@admin_required
def admin_banners():
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        if request.method == 'GET':
            cursor.execute('SELECT * FROM banners ORDER BY id DESC')
            
            banners = []
            for row in cursor.fetchall():
                banners.append({
                    'id': row[0],
                    'title': row[1],
                    'description': row[2],
                    'image_path': row[3],
                    'link_url': row[4],
                    'position': row[5],
                    'clicks': row[6],
                    'status': bool(row[7]),
                    'created_at': row[8]
                })
            
            conn.close()
            return jsonify({'success': True, 'banners': banners})
            
        elif request.method == 'POST':
            data = request.get_json()
            if not data:
                return jsonify({'success': False, 'error': 'No data received'})
            
            cursor.execute('''
                INSERT INTO banners (title, description, image_path, link_url, position, status)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                data.get('title', ''),
                data.get('description', ''),
                data.get('image_path', ''),
                data.get('link_url', ''),
                data.get('position', 'left'),
                1 if data.get('status', True) else 0
            ))
            
            banner_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            return jsonify({
                'success': True, 
                'banner_id': banner_id, 
                'message': 'Banner đã được tạo thành công'
            })
            
        elif request.method == 'PUT':
            data = request.get_json()
            if not data or not data.get('id'):
                return jsonify({'success': False, 'error': 'Missing banner ID'})
            
            banner_id = data.get('id')
            
            cursor.execute('''
                UPDATE banners 
                SET title = ?, description = ?, image_path = ?, link_url = ?, 
                    position = ?, status = ?
                WHERE id = ?
            ''', (
                data.get('title', ''),
                data.get('description', ''),
                data.get('image_path', ''),
                data.get('link_url', ''),
                data.get('position', 'left'),
                1 if data.get('status', True) else 0,
                banner_id
            ))
            
            conn.commit()
            conn.close()
            
            return jsonify({
                'success': True, 
                'message': 'Banner đã được cập nhật thành công'
            })
            
        elif request.method == 'DELETE':
            data = request.get_json()
            if not data or not data.get('id'):
                return jsonify({'success': False, 'error': 'Missing banner ID'})
            
            banner_id = data.get('id')
            
            cursor.execute('SELECT image_path FROM banners WHERE id = ?', (banner_id,))
            result = cursor.fetchone()
            
            if result and result[0]:
                image_path = result[0]
                full_path = os.path.join(app.root_path, image_path.lstrip('/'))
                if os.path.exists(full_path):
                    try:
                        os.remove(full_path)
                    except:
                        pass
            
            cursor.execute('DELETE FROM banners WHERE id = ?', (banner_id,))
            conn.commit()
            conn.close()
            
            return jsonify({
                'success': True, 
                'message': 'Banner đã được xóa thành công'
            })
            
    except Exception as e:
        logger.error(f"Banner API error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/upload', methods=['POST'])
@admin_required  
def admin_upload():
    try:
        if 'image' not in request.files:
            return jsonify({'success': False, 'error': 'Không có file hình ảnh'})
        
        file = request.files['image']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'Chưa chọn file'})
        
        file_extension = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
        
        if file_extension not in ALLOWED_BANNER_EXTENSIONS:
            return jsonify({'success': False, 'error': 'Định dạng file không được hỗ trợ'})
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{secure_filename(file.filename)}"
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        
        file.save(file_path)
        
        web_path = f"/static/uploads/banners/{filename}"
        
        return jsonify({
            'success': True,
            'image_path': web_path,
            'message': 'Hình ảnh đã được upload thành công'
        })
        
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/api/visitors')
@admin_required
def admin_visitors():
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT session_id, ip_address, user_agent, first_visit, last_activity, 
                   page_views, is_active
            FROM visitors 
            ORDER BY last_activity DESC 
            LIMIT 50
        ''')
        
        visitors = []
        for row in cursor.fetchall():
            visitors.append({
                'session_id': row[0][:8] + '...',
                'ip_address': row[1],
                'user_agent': row[2][:50] + '...' if len(row[2]) > 50 else row[2],
                'first_visit': row[3],
                'last_activity': row[4],
                'page_views': row[5],
                'is_active': bool(row[6])
            })
        
        conn.close()
        
        return jsonify({
            'success': True,
            'visitors': visitors,
            'active_count': visitor_tracker.get_active_count()
        })
        
    except Exception as e:
        logger.error(f"Visitors API error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/api/files')
@admin_required
def admin_files():
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, original_name, file_type, file_size, 
                   download_count, uploaded_at, expires_at, uploader_ip
            FROM files 
            ORDER BY uploaded_at DESC 
            LIMIT 50
        ''')
        
        files = []
        for row in cursor.fetchall():
            files.append({
                'id': row[0][:8] + '...',
                'name': row[1],
                'type': row[2],
                'size': format_file_size(row[3]),
                'downloads': row[4],
                'uploaded_at': row[5],
                'expires_at': row[6],
                'uploader_ip': row[7]
            })
        
        conn.close()
        
        return jsonify({'success': True, 'files': files})
        
    except Exception as e:
        logger.error(f"Files API error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/api/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    try:
        conn = sqlite3.connect('file_storage.db')
        cursor = conn.cursor()
        
        if request.method == 'GET':
            cursor.execute('SELECT key, value, description FROM settings')
            settings = []
            for row in cursor.fetchall():
                if row[0] == 'admin_password_hash':
                    continue
                settings.append({
                    'key': row[0],
                    'value': row[1],
                    'description': row[2]
                })
            
            conn.close()
            return jsonify({'success': True, 'settings': settings})
            
        elif request.method == 'POST':
            data = request.get_json()
            settings_to_update = data.get('settings', {})
            
            for key, value in settings_to_update.items():
                if key == 'admin_password':
                    if len(value) < 6:
                        conn.close()
                        return jsonify({'success': False, 'error': 'Mật khẩu phải có ít nhất 6 ký tự'})
                    
                    password_hash = generate_password_hash(value)
                    cursor.execute('UPDATE settings SET value = ? WHERE key = ?', 
                                 (password_hash, 'admin_password_hash'))
                else:
                    cursor.execute('UPDATE settings SET value = ? WHERE key = ?', (value, key))
            
            conn.commit()
            conn.close()
            
            # Update MAX_CONTENT_LENGTH if file size changed
            app.config['MAX_CONTENT_LENGTH'] = get_max_content_length()
            
            return jsonify({'success': True, 'message': 'Cài đặt đã được cập nhật'})
            
    except Exception as e:
        logger.error(f"Settings API error: {e}")
        return jsonify({'success': False, 'error': str(e)})

# ===== ERROR HANDLERS =====
@app.errorhandler(413)
def file_too_large(error):
    admin_settings = get_admin_settings()
    return jsonify({
        'success': False, 
        'error': f'File quá lớn. Tối đa {admin_settings["max_size_gb"]}GB theo cài đặt admin'
    }), 413

@app.errorhandler(404)
def not_found(error):
    return render_template('index.html', error='Trang không tồn tại'), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('index.html', error='Lỗi server'), 500

# ===== RUN SERVER =====
if __name__ == '__main__':
    print("=" * 70)
    print("🗂️  FILE STORAGE & SHARING SERVICE")
    print("=" * 70)
    print(f"🌐 URL: http://localhost:5000")
    print(f"👤 Admin: http://localhost:5000/admin")
    print(f"🔑 Login: admin / admin123")
    print(f"📁 Storage: {STORAGE_FOLDER}")
    print(f"📊 Max size: Admin controlled (default 25GB)")
    print(f"⏰ Expiration: Admin controlled (default 30 days)")
    print("=" * 70)
    print("📢 Features:")
    print("  - High-speed chunked upload")
    print("  - Performance-optimized downloads")
    print("  - Banner management system")
    print("  - Auto cache cleanup scheduler")
    print("  - Visitor tracking & analytics")
    print("  - QR code generation")
    print("  - Admin performance controls")
    print("=" * 70)
    print("⚡ PERFORMANCE SETTINGS:")
    print("  - Configurable chunk sizes")
    print("  - Concurrent upload controls")
    print("  - Buffer size optimization")
    print("  - Memory-mapped downloads")
    print("=" * 70)
    app.run(host='0.0.0.0', port=5000, debug=True)

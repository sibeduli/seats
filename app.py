from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import wraps
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import os
import secrets
import hashlib
import queue
import json
import logging

# Load environment variables
load_dotenv()

# Event queue for SSE notifications (thread-safe)
import threading
event_queues = []
event_queues_lock = threading.Lock()

# Simple rate limiting (in-memory, resets on restart)
from collections import defaultdict
import time
rate_limit_store = defaultdict(list)
rate_limit_lock = threading.Lock()

def is_rate_limited(ip, max_requests=10, window_seconds=60):
    """Check if IP has exceeded rate limit"""
    now = time.time()
    with rate_limit_lock:
        # Clean old entries
        rate_limit_store[ip] = [t for t in rate_limit_store[ip] if now - t < window_seconds]
        if len(rate_limit_store[ip]) >= max_requests:
            return True
        rate_limit_store[ip].append(now)
        return False


import re
import unicodedata

def sanitize_text(text):
    """Remove invisible/problematic unicode characters and normalize whitespace"""
    if not text:
        return ''
    # Normalize unicode (NFKC converts weird chars to normal equivalents)
    text = unicodedata.normalize('NFKC', text)
    # Remove zero-width and invisible characters
    text = re.sub(r'[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]', '', text)
    # Normalize whitespace (multiple spaces, tabs, etc. to single space)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

# Configuration from environment
PENDING_TIMEOUT_MINUTES = int(os.getenv('PENDING_TIMEOUT_MINUTES', 30))
ADMIN_PHONE = os.getenv('ADMIN_PHONE', '6281234567890')
ADMIN_PHONE_DISPLAY = os.getenv('ADMIN_PHONE_DISPLAY', '0812-3456-7890')

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Configuration
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'yarsi-hippocratic-oath-2026')

# Database configuration - PostgreSQL or SQLite fallback
DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'seats.db')

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Admin password from environment
ADMIN_PASSWORD_HASH = hashlib.sha256(os.getenv('ADMIN_PASSWORD', 'test').encode()).hexdigest()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(basedir, 'app.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

db = SQLAlchemy(app)


# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return render_template('errors/404.html'), 404


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return render_template('errors/500.html'), 500


# Auth decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def api_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function


# Models
class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticket_hash = db.Column(db.String(32), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='pending')  # pending / active / revoked
    booked_by_admin = db.Column(db.Boolean, default=False)  # True if booked by admin
    seats = db.relationship('Seat', backref='transaction', lazy=True)

    def __repr__(self):
        return f'<Transaction {self.ticket_hash}>'


class Seat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    region = db.Column(db.String(10), nullable=False)
    seat_number = db.Column(db.Integer, nullable=False)
    transaction_id = db.Column(db.Integer, db.ForeignKey('transaction.id'), nullable=True)

    __table_args__ = (db.UniqueConstraint('region', 'seat_number', name='unique_seat'),)

    def __repr__(self):
        return f'<Seat {self.region}-{self.seat_number}>'


def expire_pending_tickets():
    """Auto-expire pending tickets older than PENDING_TIMEOUT_MINUTES"""
    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=PENDING_TIMEOUT_MINUTES)
        # Lock rows to prevent race conditions during expiration
        expired = Transaction.query.filter(
            Transaction.status == 'pending',
            Transaction.timestamp < cutoff
        ).with_for_update().all()
        
        for transaction in expired:
            transaction.status = 'expired'
            # Lock seats before freeing
            for seat in Seat.query.filter_by(transaction_id=transaction.id).with_for_update().all():
                seat.transaction_id = None
        
        if expired:
            db.session.commit()
        
        return len(expired)
    except Exception as e:
        db.session.rollback()
        logger.error(f"Expire tickets error: {str(e)}")
        return 0


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect('/admin')
    
    error = None
    if request.method == 'POST':
        # Rate limit login attempts (5 per minute per IP)
        client_ip = request.remote_addr
        if is_rate_limited(client_ip + '_login', max_requests=5, window_seconds=60):
            error = 'Terlalu banyak percobaan, coba lagi nanti'
        else:
            password = request.form.get('password', '')
            if hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASSWORD_HASH:
                session['logged_in'] = True
                return redirect('/admin')
            else:
                error = 'Password salah'
    
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static', 'assets'), 'favicon.png', mimetype='image/png')


@app.route('/')
def welcome():
    """Public landing page"""
    return render_template('welcome.html')


@app.route('/admin')
@login_required
def index():
    return render_template('index.html')


@app.route('/booked')
@login_required
def booked_list():
    expire_pending_tickets()  # Clean up expired pending tickets
    
    # Pagination and filtering
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    status_filter = request.args.get('status', 'all')
    search = request.args.get('search', '').strip()
    
    # Sort: pending first, then by timestamp descending
    from sqlalchemy import case
    status_order = case(
        (Transaction.status == 'pending', 0),
        (Transaction.status == 'active', 1),
        (Transaction.status == 'expired', 2),
        (Transaction.status == 'revoked', 3),
        else_=4
    )
    
    # Build query with filters
    query = Transaction.query
    
    if status_filter != 'all':
        query = query.filter(Transaction.status == status_filter)
    
    if search:
        # Search by name, phone, or seat (e.g., "WLA-5" or "WLA" or "5")
        # Use EXISTS subquery for efficient DB-side filtering
        from sqlalchemy import exists
        seat_subquery = exists().where(
            db.and_(
                Seat.transaction_id == Transaction.id,
                db.or_(
                    Seat.region.ilike(f'%{search}%'),
                    db.cast(Seat.seat_number, db.String).ilike(f'%{search}%'),
                    (Seat.region + '-' + db.cast(Seat.seat_number, db.String)).ilike(f'%{search}%')
                )
            )
        )
        
        query = query.filter(
            db.or_(
                Transaction.name.ilike(f'%{search}%'),
                Transaction.phone.ilike(f'%{search}%'),
                seat_subquery
            )
        )
    
    pagination = query.order_by(
        status_order,
        Transaction.timestamp.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)
    
    return render_template('booked.html', 
                           transactions=pagination.items,
                           pagination=pagination,
                           status_filter=status_filter,
                           search=search)


@app.route('/qr')
@login_required
def qr_page():
    site_url = request.url_root.rstrip('/') + '/book'
    return render_template('qr.html', site_url=site_url)


@app.route('/book')
def guest_booking():
    """Public guest booking page"""
    return render_template('guest_booking.html')


@app.route('/ticket/<ticket_hash>')
def ticket_page(ticket_hash):
    transaction = Transaction.query.filter_by(ticket_hash=ticket_hash).first_or_404()
    # Calculate expiry time for pending tickets (timestamp + 30 min)
    expiry_timestamp = None
    if transaction.status == 'pending':
        expiry_utc = transaction.timestamp + timedelta(minutes=PENDING_TIMEOUT_MINUTES)
        # timestamp is stored as UTC but naive, need to calculate UTC timestamp properly
        # Use calendar.timegm to treat naive datetime as UTC
        import calendar
        expiry_timestamp = int(calendar.timegm(expiry_utc.timetuple()) * 1000)
    return render_template('ticket.html', transaction=transaction, expiry_timestamp=expiry_timestamp)


@app.route('/api/seats')
def get_seats():
    """Get all booked seats (pending + active)"""
    expire_pending_tickets()  # Clean up expired pending tickets
    booked_seats = Seat.query.filter(Seat.transaction_id.isnot(None)).all()
    result = []
    for seat in booked_seats:
        if seat.transaction and seat.transaction.status in ('active', 'pending'):
            result.append({'region': seat.region, 'number': seat.seat_number, 'status': seat.transaction.status})
    return jsonify(result)


@app.route('/api/check-seats', methods=['POST'])
def check_seats():
    """Check if seats are still available (race condition check)"""
    data = request.get_json()
    seats = data.get('seats', [])
    
    unavailable = []
    for seat_data in seats:
        existing = Seat.query.filter_by(
            region=seat_data['region'],
            seat_number=seat_data['number']
        ).first()
        if existing and existing.transaction_id:
            if existing.transaction.status in ('active', 'pending'):
                unavailable.append(f"{seat_data['region']}-{seat_data['number']}")
    
    if unavailable:
        return jsonify({
            'available': False,
            'error': f"Kursi {', '.join(unavailable)} sudah dipesan oleh orang lain"
        })
    
    return jsonify({'available': True})


@app.route('/api/book', methods=['POST'])
def book_seats():
    """Book seats and create transaction"""
    # Rate limiting (10 requests per minute per IP)
    client_ip = request.remote_addr
    if is_rate_limited(client_ip, max_requests=10, window_seconds=60):
        return jsonify({'error': 'Terlalu banyak permintaan, coba lagi nanti'}), 429
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'}), 400
    
    name = sanitize_text(data.get('name', ''))
    phone = sanitize_text(data.get('phone', ''))
    seats = data.get('seats', [])  # List of {region, number}
    is_admin = session.get('logged_in', False)
    
    # Input validation
    if not name or not phone or not seats:
        return jsonify({'error': 'Name, phone and seats required'}), 400
    
    # Length limits to prevent abuse
    if len(name) > 100:
        return jsonify({'error': 'Nama terlalu panjang (max 100 karakter)'}), 400
    if len(phone) > 20:
        return jsonify({'error': 'Nomor telepon tidak valid'}), 400
    if len(seats) > 10:
        return jsonify({'error': 'Maksimal 10 kursi per pemesanan'}), 400
    
    # Validate seat data structure
    for seat_data in seats:
        if not isinstance(seat_data, dict) or 'region' not in seat_data or 'number' not in seat_data:
            return jsonify({'error': 'Format kursi tidak valid'}), 400
        if not isinstance(seat_data.get('region'), str) or len(seat_data['region']) > 10:
            return jsonify({'error': 'Region tidak valid'}), 400
    
    try:
        # Check and book seats atomically with row-level locking
        for seat_data in seats:
            # Use FOR UPDATE to lock the row (PostgreSQL) or just check (SQLite)
            existing = Seat.query.filter_by(
                region=seat_data['region'],
                seat_number=seat_data['number']
            ).with_for_update().first()
            
            if existing and existing.transaction_id:
                if existing.transaction.status in ('active', 'pending'):
                    db.session.rollback()
                    return jsonify({'error': f"Kursi {seat_data['region']}-{seat_data['number']} sudah dipesan"}), 400
        
        # Create transaction - active if admin, pending if guest
        ticket_hash = secrets.token_hex(16)
        status = 'active' if is_admin else 'pending'
        transaction = Transaction(ticket_hash=ticket_hash, name=name, phone=phone, status=status, booked_by_admin=is_admin)
        db.session.add(transaction)
        db.session.flush()  # Get transaction ID
        
        # Book seats
        for seat_data in seats:
            seat = Seat.query.filter_by(
                region=seat_data['region'],
                seat_number=seat_data['number']
            ).first()
            if not seat:
                seat = Seat(region=seat_data['region'], seat_number=seat_data['number'])
                db.session.add(seat)
            seat.transaction_id = transaction.id
        
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Booking error: {str(e)}")
        return jsonify({'error': 'Terjadi kesalahan, silakan coba lagi'}), 500
    
    # Log the booking
    seat_list = [f"{s['region']}-{s['number']}" for s in seats]
    logger.info(f"BOOKING: name={name}, phone={phone}, seats={seat_list}, status={status}, admin={is_admin}, hash={ticket_hash}")
    
    # Broadcast SSE event for new pending booking (not for admin bookings)
    if not is_admin:
        broadcast_event('new_booking', {
            'name': name,
            'phone': phone,
            'seats': seat_list,
            'status': status
        })
    
    return jsonify({
        'success': True,
        'ticket_hash': ticket_hash,
        'ticket_url': f'/ticket/{ticket_hash}'
    })


@app.route('/api/approve/<int:transaction_id>', methods=['POST'])
@api_login_required
def approve_transaction(transaction_id):
    """Approve a pending transaction"""
    try:
        transaction = Transaction.query.with_for_update().get(transaction_id)
        if not transaction:
            return jsonify({'error': 'Transaction not found'}), 404
        if transaction.status != 'pending':
            db.session.rollback()
            return jsonify({'error': 'Transaction is not pending'}), 400
        transaction.status = 'active'
        db.session.commit()
        
        seats = [f"{s.region}-{s.seat_number}" for s in transaction.seats]
        logger.info(f"APPROVE: id={transaction_id}, name={transaction.name}, seats={seats}, hash={transaction.ticket_hash}")
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Approve error: {str(e)}")
        return jsonify({'error': 'Terjadi kesalahan'}), 500


@app.route('/api/reject/<int:transaction_id>', methods=['POST'])
@api_login_required
def reject_transaction(transaction_id):
    """Reject a pending transaction and free seats"""
    try:
        transaction = Transaction.query.with_for_update().get(transaction_id)
        if not transaction:
            return jsonify({'error': 'Transaction not found'}), 404
        seats = [f"{s.region}-{s.seat_number}" for s in transaction.seats]
        transaction.status = 'revoked'
        
        # Free the seats with locking
        for seat in Seat.query.filter_by(transaction_id=transaction_id).with_for_update().all():
            seat.transaction_id = None
        
        db.session.commit()
        logger.info(f"REJECT: id={transaction_id}, name={transaction.name}, seats={seats}, hash={transaction.ticket_hash}")
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Reject error: {str(e)}")
        return jsonify({'error': 'Terjadi kesalahan'}), 500


@app.route('/api/revoke/<int:transaction_id>', methods=['POST'])
@api_login_required
def revoke_transaction(transaction_id):
    """Revoke an active transaction and free seats"""
    try:
        transaction = Transaction.query.with_for_update().get(transaction_id)
        if not transaction:
            return jsonify({'error': 'Transaction not found'}), 404
        seats = [f"{s.region}-{s.seat_number}" for s in transaction.seats]
        transaction.status = 'revoked'
        
        # Free the seats with locking
        for seat in Seat.query.filter_by(transaction_id=transaction_id).with_for_update().all():
            seat.transaction_id = None
        
        db.session.commit()
        logger.info(f"REVOKE: id={transaction_id}, name={transaction.name}, seats={seats}, hash={transaction.ticket_hash}")
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Revoke error: {str(e)}")
        return jsonify({'error': 'Terjadi kesalahan'}), 500


def broadcast_event(event_type, data):
    """Broadcast event to all connected SSE clients (thread-safe)"""
    message = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with event_queues_lock:
        dead_queues = []
        for q in event_queues:
            try:
                q.put_nowait(message)
            except:
                dead_queues.append(q)
        for q in dead_queues:
            event_queues.remove(q)


@app.route('/api/events')
@api_login_required
def sse_events():
    """SSE endpoint for real-time admin notifications"""
    def event_stream():
        q = queue.Queue()
        with event_queues_lock:
            event_queues.append(q)
        try:
            while True:
                try:
                    # Non-blocking check with short timeout
                    message = q.get(timeout=15)
                    yield message
                except queue.Empty:
                    # Send keep-alive ping
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            with event_queues_lock:
                if q in event_queues:
                    event_queues.remove(q)
    
    return Response(event_stream(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no'
    })


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, threaded=True)

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response
from flask_sqlalchemy import SQLAlchemy
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

# Event queue for SSE notifications
event_queues = []

# Configuration from environment
PENDING_TIMEOUT_MINUTES = int(os.getenv('PENDING_TIMEOUT_MINUTES', 30))
ADMIN_PHONE = os.getenv('ADMIN_PHONE', '6281234567890')
ADMIN_PHONE_DISPLAY = os.getenv('ADMIN_PHONE_DISPLAY', '0812-3456-7890')

app = Flask(__name__)

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
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=PENDING_TIMEOUT_MINUTES)
    expired = Transaction.query.filter(
        Transaction.status == 'pending',
        Transaction.timestamp < cutoff
    ).all()
    
    for transaction in expired:
        transaction.status = 'expired'
        for seat in transaction.seats:
            seat.transaction_id = None
    
    if expired:
        db.session.commit()
    
    return len(expired)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('index'))
    
    error = None
    if request.method == 'POST':
        password = request.form.get('password', '')
        if hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASSWORD_HASH:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            error = 'Password salah'
    
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


@app.route('/')
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
        query = query.join(Seat).filter(
            (Transaction.name.ilike(f'%{search}%')) | 
            (Transaction.phone.ilike(f'%{search}%')) |
            (Seat.region.ilike(f'%{search}%')) |
            (db.cast(Seat.seat_number, db.String).like(f'%{search}%')) |
            ((Seat.region + '-' + db.cast(Seat.seat_number, db.String)).ilike(f'%{search}%'))
        ).distinct()
    
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
    data = request.get_json()
    name = data.get('name')
    phone = data.get('phone')
    seats = data.get('seats', [])  # List of {region, number}
    is_admin = session.get('logged_in', False)
    
    if not name or not phone or not seats:
        return jsonify({'error': 'Name, phone and seats required'}), 400
    
    # Check if seats are available
    for seat_data in seats:
        existing = Seat.query.filter_by(
            region=seat_data['region'],
            seat_number=seat_data['number']
        ).first()
        if existing and existing.transaction_id:
            if existing.transaction.status in ('active', 'pending'):
                return jsonify({'error': f"Seat {seat_data['region']}-{seat_data['number']} already booked"}), 400
    
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
    transaction = Transaction.query.get_or_404(transaction_id)
    if transaction.status != 'pending':
        return jsonify({'error': 'Transaction is not pending'}), 400
    transaction.status = 'active'
    db.session.commit()
    
    seats = [f"{s.region}-{s.seat_number}" for s in transaction.seats]
    logger.info(f"APPROVE: id={transaction_id}, name={transaction.name}, seats={seats}, hash={transaction.ticket_hash}")
    return jsonify({'success': True})


@app.route('/api/reject/<int:transaction_id>', methods=['POST'])
@api_login_required
def reject_transaction(transaction_id):
    """Reject a pending transaction and free seats"""
    transaction = Transaction.query.get_or_404(transaction_id)
    seats = [f"{s.region}-{s.seat_number}" for s in transaction.seats]
    transaction.status = 'revoked'
    
    # Free the seats
    for seat in transaction.seats:
        seat.transaction_id = None
    
    db.session.commit()
    logger.info(f"REJECT: id={transaction_id}, name={transaction.name}, seats={seats}, hash={transaction.ticket_hash}")
    return jsonify({'success': True})


@app.route('/api/revoke/<int:transaction_id>', methods=['POST'])
@api_login_required
def revoke_transaction(transaction_id):
    """Revoke an active transaction and free seats"""
    transaction = Transaction.query.get_or_404(transaction_id)
    seats = [f"{s.region}-{s.seat_number}" for s in transaction.seats]
    transaction.status = 'revoked'
    
    # Free the seats
    for seat in transaction.seats:
        seat.transaction_id = None
    
    db.session.commit()
    logger.info(f"REVOKE: id={transaction_id}, name={transaction.name}, seats={seats}, hash={transaction.ticket_hash}")
    return jsonify({'success': True})


def broadcast_event(event_type, data):
    """Broadcast event to all connected SSE clients"""
    message = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
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

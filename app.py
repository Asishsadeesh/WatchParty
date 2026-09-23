import os
import time
import threading
import re
import requests as req
# pyrefly: ignore [missing-import]
from datetime import datetime, timedelta
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, send_file, abort, Response
from flask_socketio import SocketIO
from models import db, User, Room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-watch-party-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
CHUNK_SIZE = 1024 * 1024  # 1MB chunk size

db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Range Proxy state storage
range_states = {} # { room_id: { 'chunks': { chunk_id: bytes }, 'waiters': { chunk_id: threading.Event() }, 'requested': set(), 'size': int, 'content_type': str } }

# Import sockets after initializing socketio to avoid circular imports
import sockets  # noqa: E402

@app.before_request
def check_user():
    if 'user_id' not in session:
        # API endpoints must return JSON, not an HTML redirect
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Not authenticated'}), 401
        if request.endpoint not in ['home', 'static']:
            return redirect(url_for('home'))

@app.route('/', methods=['GET', 'POST'])
def home():
    
    # Cleanup empty rooms older than 1 hour
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    empty_rooms = Room.query.filter(Room.last_empty_at < one_hour_ago).all()
    for room in empty_rooms:
        db.session.delete(room)
        db.session.commit()
        if str(room.id) in sockets.room_states:
            del sockets.room_states[str(room.id)]
    if empty_rooms:
        db.session.commit()

    if request.method == 'POST':
        username = request.form.get('username')
        if username:
            user = User.query.filter_by(username=username).first()
            if not user:
                user = User(username=username)
                db.session.add(user)
                db.session.commit()
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect(url_for('home'))
            
    rooms = Room.query.all()
    user_id = session.get('user_id')
    user = db.session.get(User, user_id) if user_id else None
    
    return render_template('home.html', rooms=rooms, user=user)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/create_room', methods=['POST'])
def create_room():
    if 'user_id' not in session:
        return redirect(url_for('home'))
        
    name = request.form.get('name')
    if name:
        room = Room(name=name, host_id=session['user_id'])
        db.session.add(room)
        db.session.commit()
        return redirect(url_for('room', room_id=room.id))
    return redirect(url_for('home'))

@app.route('/room/<int:room_id>')
def room(room_id):
    room = Room.query.get_or_404(room_id)
    user = db.session.get(User, session['user_id'])
    is_host = (room.host_id == user.id)
    return render_template('room.html', room=room, user=user, is_host=is_host)

@app.route('/delete_room/<int:room_id>', methods=['POST'])
def delete_room(room_id):
    if 'user_id' not in session:
        return redirect(url_for('home'))
        
    room = Room.query.get_or_404(room_id)
    if room.host_id == session['user_id']:
        db.session.delete(room)
        db.session.commit()
        
        if str(room_id) in sockets.room_states:
            del sockets.room_states[str(room_id)]
        socketio.emit('room_deleted', to=str(room_id))
            
    return redirect(url_for('home'))

@app.route('/api/search')
def youtube_search():
    """Search YouTube via the internal youtubei/v1/search endpoint (no API key needed)."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'results': [], 'error': 'No query provided'})
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Content-Type': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        body = {
            'context': {
                'client': {
                    'clientName': 'WEB',
                    'clientVersion': '2.20231121.09.00',
                    'hl': 'en',
                    'gl': 'US',
                }
            },
            'query': query
        }
        resp = req.post(
            'https://www.youtube.com/youtubei/v1/search?prettyPrint=false',
            headers=headers, json=body, timeout=12
        )
        resp.raise_for_status()
        data = resp.json()

        # Navigate the nested response to find videoRenderer items
        results = []
        sections = (data.get('contents', {})
                        .get('twoColumnSearchResultsRenderer', {})
                        .get('primaryContents', {})
                        .get('sectionListRenderer', {})
                        .get('contents', []))

        for section in sections:
            items = section.get('itemSectionRenderer', {}).get('contents', [])
            for item in items:
                vr = item.get('videoRenderer')
                if not vr:
                    continue
                video_id = vr.get('videoId', '')
                if not video_id:
                    continue
                
                # Extract title
                title_runs = vr.get('title', {}).get('runs', [])
                title = title_runs[0].get('text', '') if title_runs else 'Unknown Title'
                
                # Extract channel
                owner_runs = vr.get('ownerText', {}).get('runs', [])
                channel = owner_runs[0].get('text', '') if owner_runs else ''
                
                # Extract length
                length = vr.get('lengthText', {}).get('simpleText', '')
                
                # Extract thumbnail
                thumbnails = vr.get('thumbnail', {}).get('thumbnails', [])
                thumbnail_url = thumbnails[0].get('url', '') if thumbnails else ''
                
                results.append({
                    'id': video_id,
                    'title': title,
                    'channel': channel,
                    'length': length,
                    'thumbnail': thumbnail_url
                })
                
                if len(results) >= 15:
                    break
            if len(results) >= 15:
                break
                
        return jsonify({'results': results})
        
    except Exception as e:
        import traceback
        print(f"[Search] Error: {e}")
        traceback.print_exc()
        return jsonify({'results': [], 'error': str(e)}), 500


# The room uses same-origin HTTP polling instead of requiring a browser-side
# Socket.IO/WebSocket client.  This also works on networks that block CDNs or
# WebSockets.
def _get_room_state(room_id):
    room = Room.query.get_or_404(room_id)
    user_id = str(session['user_id'])
    username = session.get('username', '')
    key = str(room_id)
    state = sockets.room_states.setdefault(key, {
        'participants': {},
        'state': {'status': 'paused', 'time': 0, 'updated_at': time.time()},
        'media': {'type': 'youtube', 'url': ''},
        'revision': 0
    })
    state.setdefault('revision', 0)
    
    if 'upload_cv' not in state:
        state['upload_cv'] = threading.Condition()
        state['chunk_map'] = set() # Now represents uploaded segment indices
        state['requested_chunks'] = {} # Now represents requested segment indices
        state['has_init'] = False
        state['video_duration'] = 0
        state['segment_duration'] = 20

    now = time.time()
    # Keep the displayed presence list accurate without a disconnect event.
    state['participants'] = {
        participant_id: participant
        for participant_id, participant in state['participants'].items()
        # A pagehide leave signal handles normal exits immediately. This is the
        # fallback for a closed tab, crashed browser, or lost connection.
        if now - participant.get('last_seen', now) < 8
    }

    participant = state['participants'].setdefault(user_id, {
        'username': username,
        'has_seek_access': str(room.host_id) == user_id
    })
    participant['username'] = username
    participant['last_seen'] = now
    if str(room.host_id) == user_id:
        participant['has_seek_access'] = True

    _elect_active_host(room, state)
    if state.get('controller_id') not in state['participants']:
        state['controller_id'] = str(room.host_id) if str(room.host_id) in state['participants'] else None
    return room, state, user_id


def _elect_active_host(room, state):
    """Make the longest-waiting active participant host when necessary."""
    participants = state['participants']
    current_host_id = str(room.host_id)
    if not participants or current_host_id in participants:
        return

    new_host_id = min(
        participants,
        key=lambda participant_id: participants[participant_id].get('last_seen', 0)
    )
    room.host_id = int(new_host_id)
    participants[new_host_id]['has_seek_access'] = True
    state['controller_id'] = new_host_id
    db.session.commit()


def _room_payload(room, state, user_id):
    participants = {
        participant_id: {
            'username': participant.get('username', ''),
            'has_seek_access': participant.get('has_seek_access', False)
        }
        for participant_id, participant in state['participants'].items()
    }
    now = time.time()
    # Clean up stale chunk requests (older than 10 seconds)
    if 'requested_chunks' in state:
        state['requested_chunks'] = {c: t for c, t in state['requested_chunks'].items() if now - t < 10}
        
    # Sort chunks by timestamp descending (newest requests get highest priority)
    sorted_chunks = sorted(state.get('requested_chunks', {}).items(), key=lambda x: x[1], reverse=True)
    requested_chunks_list = [c for c, _ in sorted_chunks]

    
    room_range = range_states.get(str(room.id), {'requested': set()})
    
    return {
        'participants': participants,
        'media': state['media'],
        'state': state['state'],
        'host_id': str(room.host_id),
        'is_host': str(room.host_id) == user_id,
        'controller_id': state.get('controller_id'),
        'revision': state['revision'],
        'requested_chunks': list(range_states.get(str(room.id), {'requested': set()})['requested'])
    }


@app.route('/api/rooms/<int:room_id>/state')
def room_state(room_id):
    room, state, user_id = _get_room_state(room_id)
    return jsonify(_room_payload(room, state, user_id))


@app.route('/api/rooms/<int:room_id>/leave', methods=['POST'])
def leave_room_presence(room_id):
    room, state, user_id = _get_room_state(room_id)
    state['participants'].pop(user_id, None)
    _elect_active_host(room, state)
    state['revision'] += 1
    return jsonify({'ok': True})


@app.route('/api/rooms/<int:room_id>/media', methods=['POST'])
def update_room_media(room_id):
    room, state, user_id = _get_room_state(room_id)
    participant = state['participants'][user_id]
    media = (request.get_json(silent=True) or {}).get('media', {})

    if not participant.get('has_seek_access'):
        return jsonify({'error': 'Only the host or users with control access can change media.'}), 403
    if media.get('type') not in ('youtube', 'movie', 'video', 'hls', 'range') or not isinstance(media.get('url'), str):
        error_details = {
            'error': 'invalid media',
            'reason': 'media type or url failed validation',
            'received_type': media.get('type'),
            'received_url': media.get('url'),
            'allowed_types': ['youtube', 'movie', 'video', 'hls', 'range']
        }
        print(f"[DEBUG] /media validation failed: {error_details}")
        return jsonify(error_details), 400

    state['media'] = {'type': media['type'], 'url': media['url'].strip()}
    state['state'] = {'status': 'paused', 'time': 0, 'updated_at': time.time()}
    state['controller_id'] = user_id
    state['revision'] += 1
    return jsonify(_room_payload(room, state, user_id))


@app.route('/api/rooms/<int:room_id>/media/range-init', methods=['POST'])
def range_init(room_id):
    room, state, user_id = _get_room_state(room_id)
    if not state['participants'][user_id].get('has_seek_access'):
        return jsonify({'error': 'Unauthorized'}), 403
        
    data = request.get_json(silent=True) or {}
    size = data.get('size', 0)
    content_type = data.get('contentType', 'video/mp4')
    
    range_states[str(room_id)] = {
        'chunks': {},
        'waiters': {},
        'requested': set(),
        'size': size,
        'content_type': content_type
    }
    
    state['media'] = {'type': 'range', 'url': 'range', 'size': size, 'contentType': content_type}
    state['state'] = {'status': 'paused', 'time': 0, 'updated_at': time.time()}
    state['revision'] += 1
    return jsonify({'ok': True})

@app.route('/api/rooms/<int:room_id>/media/range-chunk/<chunk_id>', methods=['POST'])
def range_chunk(room_id, chunk_id):
    room, state, user_id = _get_room_state(room_id)
    if not state['participants'][user_id].get('has_seek_access'):
        return jsonify({'error': 'Unauthorized'}), 403
        
    rs = range_states.get(str(room_id))
    if not rs:
        return jsonify({'error': 'No range session'}), 400
        
    rs['chunks'][chunk_id] = request.get_data()
    
    if chunk_id in rs['requested']:
        rs['requested'].remove(chunk_id)
        
    if chunk_id in rs['waiters']:
        rs['waiters'][chunk_id].set()
        
    return jsonify({'ok': True})

@app.route('/api/rooms/<int:room_id>/media/range-stream', methods=['GET'])
def range_stream(room_id):
    rs = range_states.get(str(room_id))
    if not rs or rs['size'] == 0:
        return abort(404, "Stream not initialized")
        
    range_header = request.headers.get('Range')
    if not range_header:
        resp = Response(status=200)
        resp.headers['Accept-Ranges'] = 'bytes'
        resp.headers['Content-Length'] = str(rs['size'])
        resp.headers['Content-Type'] = rs['content_type']
        return resp
        
    try:
        range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        start_byte = int(range_match.group(1))
        end_byte = range_match.group(2)
        if end_byte:
            end_byte = int(end_byte)
        else:
            end_byte = min(start_byte + 1024 * 1024 - 1, rs['size'] - 1)
    except:
        return abort(400, "Invalid Range header")
        
    chunk_size = 1024 * 1024
    chunk_start = (start_byte // chunk_size) * chunk_size
    chunk_end = min(chunk_start + chunk_size - 1, rs['size'] - 1)
    
    chunk_id = f"{chunk_start}-{chunk_end}"
    
    if chunk_id not in rs['chunks']:
        if chunk_id not in rs['waiters']:
            rs['waiters'][chunk_id] = threading.Event()
        rs['requested'].add(chunk_id)
        
        rs['waiters'][chunk_id].wait(timeout=15)
        
    chunk_data = rs['chunks'].get(chunk_id)
    if not chunk_data:
        return abort(408, "Chunk timeout")
        
    offset_in_chunk = start_byte - chunk_start
    requested_length = end_byte - start_byte + 1
    
    if offset_in_chunk + requested_length > len(chunk_data):
        requested_length = len(chunk_data) - offset_in_chunk
        end_byte = start_byte + requested_length - 1
        
    slice_data = chunk_data[offset_in_chunk:offset_in_chunk + requested_length]
    
    resp = Response(slice_data, status=206)
    resp.headers['Accept-Ranges'] = 'bytes'
    resp.headers['Content-Range'] = f'bytes {start_byte}-{end_byte}/{rs["size"]}'
    resp.headers['Content-Length'] = str(len(slice_data))
    resp.headers['Content-Type'] = rs['content_type']
    return resp


@app.route('/api/rooms/<int:room_id>/sync', methods=['POST'])
def update_room_sync(room_id):
    room, state, user_id = _get_room_state(room_id)
    participant = state['participants'][user_id]
    data = request.get_json(silent=True) or {}
    playback = data.get('state', {})
    is_heartbeat = bool(data.get('heartbeat'))

    if not participant.get('has_seek_access'):
        return jsonify({'error': 'Playback control is not permitted.'}), 403
    if playback.get('status') not in ('playing', 'paused'):
        return jsonify({'error': 'Invalid playback status.'}), 400
    try:
        playback_time = max(0, float(playback.get('time', 0)))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid playback time.'}), 400

    # A periodic clock update is passive. It must not steal control back from
    # someone who has just paused or sought the shared player.
    if is_heartbeat and state.get('controller_id') not in (None, user_id):
        return jsonify({
            'ok': True,
            'ignored': True,
            'revision': state['revision'],
        'requested_chunks': list(range_states.get(str(room.id), {'requested': set()})['requested'])
    })

    state['state'] = {'status': playback['status'], 'time': playback_time, 'updated_at': time.time()}
    state['controller_id'] = user_id
    state['revision'] += 1
    return jsonify({'ok': True, 'revision': state['revision'], 'controller_id': user_id})


@app.route('/api/rooms/<int:room_id>/access', methods=['POST'])
def update_room_access(room_id):
    room, state, user_id = _get_room_state(room_id)
    if str(room.host_id) != user_id:
        return jsonify({'error': 'Only the host can change participant access.'}), 403

    data = request.get_json(silent=True) or {}
    target_id = str(data.get('target_user_id', ''))
    if target_id not in state['participants'] or target_id == user_id:
        return jsonify({'error': 'Participant not found.'}), 404

    state['participants'][target_id]['has_seek_access'] = bool(data.get('granted'))
    if not state['participants'][target_id]['has_seek_access'] and state.get('controller_id') == target_id:
        state['controller_id'] = user_id
    state['revision'] += 1
    return jsonify(_room_payload(room, state, user_id))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(app, debug=True, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)

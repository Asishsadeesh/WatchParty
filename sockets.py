from flask import request
from flask_socketio import emit, join_room, leave_room
from app import socketio
from models import db, Room
from datetime import datetime

# Store room state in memory for faster sync than DB
room_states = {}
sid_to_user = {}
active_sockets = {}

def handle_user_leave(sid):
    if sid not in sid_to_user:
        return
        
    info = sid_to_user[sid]
    room_id = info['room_id']
    user_id = info['user_id']  # already a string
    username = info['username']
    
    del sid_to_user[sid]
    
    # If this sid is NOT the active socket for this user,
    # it means the user reconnected (page refresh) and a newer
    # socket already replaced this one. Skip removal.
    if active_sockets.get(user_id) != sid:
        return
        
    del active_sockets[user_id]
        
    if room_id not in room_states:
        return
    if user_id not in room_states[room_id]['participants']:
        return
        
    was_host = room_states[room_id]['participants'][user_id].get('has_seek_access', False)
    del room_states[room_id]['participants'][user_id]
    
    participants = room_states[room_id]['participants']
    emit('user_left', {'username': username, 'participants': participants}, to=room_id)
    
    if not participants:
        # Room is empty — mark it for cleanup
        try:
            room = db.session.get(Room, int(room_id))
            if room:
                room.last_empty_at = datetime.utcnow()
                db.session.commit()
        except Exception as e:
            print(f"Error marking room empty: {e}")
    elif was_host:
        # Promote the first remaining participant to host
        new_host_id = list(participants.keys())[0]
        participants[new_host_id]['has_seek_access'] = True
        
        try:
            room = db.session.get(Room, int(room_id))
            if room:
                room.host_id = int(new_host_id)
                db.session.commit()
        except Exception as e:
            print(f"Error updating host: {e}")
            
        emit('new_host', {'host_id': new_host_id, 'participants': participants}, to=room_id)

@socketio.on('disconnect')
def on_disconnect():
    handle_user_leave(request.sid)

@socketio.on('join')
def on_join(data):
    try:
        print(f"[SERVER] join received: {data}")
        room_id = str(data['room'])
        username = str(data['username'])
        user_id = str(data['user_id'])  # ALWAYS string
        is_host = bool(data.get('is_host', False))
        
        join_room(room_id)
        
        if room_id not in room_states:
            room_states[room_id] = {
                'participants': {},
                'state': {'status': 'paused', 'time': 0, 'updated_at': 0},
                'media': {'type': 'youtube', 'url': ''}
            }
            
        participants = room_states[room_id]['participants']
        
        # If this room has no participants, the joining user becomes host
        if not participants:
            is_host = True
            try:
                room = db.session.get(Room, int(room_id))
                if room:
                    room.host_id = int(user_id)
                    room.last_empty_at = None
                    db.session.commit()
            except Exception as e:
                print(f"Error setting host: {e}")
                
        participants[user_id] = {
            'username': username,
            'has_seek_access': is_host 
        }
        
        sid_to_user[request.sid] = {
            'room_id': room_id,
            'user_id': user_id,
            'username': username
        }
        
        active_sockets[user_id] = request.sid
        
        # Determine current host_id for this room
        current_host_id = user_id if is_host else None
        if not current_host_id:
            try:
                room = db.session.get(Room, int(room_id))
                if room:
                    current_host_id = str(room.host_id)
            except Exception as e:
                print(f"Error getting host_id: {e}")
        
        # Notify EVERYONE in the room (including new joiner) about the new participant
        emit('user_joined', {'username': username, 'participants': participants}, to=room_id)
        
        # Send the new joiner their personal snapshot — MUST use request.sid to target only them
        emit('joined_room', {
            'user_id':      user_id,
            'host_id':      current_host_id,
            'participants': participants,
            'is_host':      is_host,
        }, to=request.sid)
        
        # Tell this user who the host is
        if current_host_id:
            emit('new_host', {'host_id': current_host_id, 'participants': participants}, to=request.sid)
        
        # Send current playback state to newly joined user only
        emit('sync_state', room_states[room_id]['state'], to=request.sid)
        if room_states[room_id]['media']['url']:
            emit('media_change', room_states[room_id]['media'], to=request.sid)

    except Exception as e:
        import traceback
        print(f"[SERVER] FATAL ERROR IN on_join: {e}")
        traceback.print_exc()

@socketio.on('leave')
def on_leave(data):
    room_id = str(data['room'])
    leave_room(room_id)
    handle_user_leave(request.sid)

@socketio.on('sync')
def on_sync(data):
    room_id = str(data['room'])
    user_id = str(data['user_id'])
    
    if room_id in room_states:
        participant = room_states[room_id]['participants'].get(user_id)
        if participant and participant['has_seek_access']:
            room_states[room_id]['state'] = data['state']
            emit('sync_state', data['state'], to=room_id, include_self=False)

@socketio.on('grant_access')
def on_grant_access(data):
    room_id = str(data['room'])
    target_user_id = str(data['target_user_id'])
    
    if room_id in room_states and target_user_id in room_states[room_id]['participants']:
        room_states[room_id]['participants'][target_user_id]['has_seek_access'] = data['granted']
        emit('access_updated', {'participants': room_states[room_id]['participants']}, to=room_id)

@socketio.on('change_media')
def on_change_media(data):
    room_id = str(data['room'])
    user_id = str(data['user_id'])
    
    print(f"[change_media] room={room_id}, user={user_id}")
    
    if room_id not in room_states:
        print(f"[change_media] Room {room_id} not found in room_states")
        return
        
    participant = room_states[room_id]['participants'].get(user_id)
    print(f"[change_media] participant={participant}, all_keys={list(room_states[room_id]['participants'].keys())}")
    
    if participant and participant['has_seek_access']:
        room_states[room_id]['media'] = data['media']
        print(f"[change_media] Broadcasting media_change: {data['media']}")
        emit('media_change', data['media'], to=room_id)
    else:
        print(f"[change_media] DENIED - participant not found or no seek access")

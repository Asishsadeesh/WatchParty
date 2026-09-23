// Same-origin HTTP polling replaces the browser Socket.IO/WebSocket dependency.
const html5Player = document.getElementById('html5-player');
const ytWrapper = document.getElementById('yt-container');
const playerStatus = document.getElementById('player-status');
const roomApi = `/api/rooms/${encodeURIComponent(ROOM_ID)}`;
let player = null, currentMediaType = 'youtube', currentMediaUrl = '';
let hasSeekAccess = IS_HOST, ytApiReady = false, isSyncing = false, pollInFlight = false;
let lastRoomRevision = -1, pendingPlayback = null, activeControllerId = null;
let queuedSync = null, syncInFlight = false;
// After the user seeks/pauses, ignore poll-driven playback corrections for
// 1.2 s so stale poll responses can't snap the player back to the old position.
let syncCooldownUntil = 0;
let wtClient = null;

// Range proxy state
let activeFile = null;
let rangeChunkCache = new Map();
let isUploadingChunk = false;
let memberChoice = null; // null = not chosen, 'stream' = host stream, 'local' = local file
let memberLocalFile = null;


function setPlayerStatus(message) {
    if (!playerStatus) return;
    playerStatus.textContent = message || '';
    playerStatus.style.display = message ? 'flex' : 'none';
}

async function api(path = '/state', options = {}) {
    const response = await fetch(`${roomApi}${path}`, {
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
}

function updateParticipantsList(participants) {
    const list = document.getElementById('participant-list');
    const count = document.getElementById('participant-count');
    if (!list || !count) return;
    const ids = Object.keys(participants || {});
    count.textContent = ids.length;
    list.replaceChildren();
    ids.forEach(id => {
        const participant = participants[id];
        const item = document.createElement('li');
        item.className = 'participant-item';
        const info = document.createElement('div');
        info.className = 'user-info';
        info.textContent = `${participant.username}${String(id) === String(USER_ID) ? ' (you)' : ''}${participant.has_seek_access ? ' ★' : ''}`;
        item.appendChild(info);
        if (IS_HOST && String(id) !== String(USER_ID)) {
            const button = document.createElement('button');
            button.className = 'access-btn';
            button.textContent = participant.has_seek_access ? 'Revoke' : 'Allow control';
            button.addEventListener('click', () => updateAccess(id, !participant.has_seek_access));
            item.appendChild(button);
        }
        list.appendChild(item);
    });
}

function applyPlayback(playback) {
    if (!playback || isSyncing) return;
    if (Date.now() < syncCooldownUntil) return;
    pendingPlayback = playback;
    let changedPlayer = false;

    const now = Date.now() / 1000;
    const latency = playback.updated_at ? Math.max(0, now - playback.updated_at) : 0;
    const expectedTime = playback.status === 'playing' ? playback.time + latency : playback.time;

    try {
        if (currentMediaType === 'youtube' && player?.getCurrentTime) {
            const ps = player.getPlayerState();
            if (ps === YT.PlayerState.BUFFERING) return;

            if (Math.abs(player.getCurrentTime() - expectedTime) > 1.5) {
                isSyncing = true;
                changedPlayer = true;
                player.seekTo(expectedTime, true);
            }
            if (playback.status === 'playing' && ps !== YT.PlayerState.PLAYING) {
                const isInitialLoad = ps === YT.PlayerState.UNSTARTED || ps === YT.PlayerState.CUED || ps === -1;
                if (!IS_HOST && isInitialLoad) player.mute();
                isSyncing = true;
                changedPlayer = true;
                player.playVideo();
            }
            if (playback.status === 'paused' && ps !== YT.PlayerState.PAUSED) {
                isSyncing = true;
                changedPlayer = true;
                player.pauseVideo();
            }
        } else if ((currentMediaType === 'video' || currentMediaType === 'range') && html5Player) {
            // Members who chose local file should not be synced
            if (!IS_HOST && memberChoice === 'local') return;
            if (html5Player.readyState >= 1) {
                const diff = html5Player.currentTime - expectedTime;
                
                if (Math.abs(diff) > 2.0) {
                    isSyncing = true;
                    changedPlayer = true;
                    html5Player.currentTime = expectedTime;
                } else if (Math.abs(diff) > 0.5 && playback.status === 'playing') {
                    html5Player.playbackRate = diff < 0 ? 1.1 : 0.9;
                } else if (html5Player.playbackRate !== 1.0) {
                    html5Player.playbackRate = 1.0;
                }
            }

            if (playback.status === 'playing' && html5Player.paused) {
                isSyncing = true;
                changedPlayer = true;
                html5Player.play().catch(() => {});
            }
            if (playback.status === 'paused' && !html5Player.paused) {
                isSyncing = true;
                changedPlayer = true;
                html5Player.pause();
                if (Math.abs(html5Player.currentTime - expectedTime) > 0.1) {
                    html5Player.currentTime = expectedTime;
                }
            }
        }
    } catch (error) { console.warn('Playback sync failed:', error); }
    if (changedPlayer) window.setTimeout(() => { isSyncing = false; }, 400);
}


function applyRoomState(data) {
    const revision = Number(data.revision || 0);
    if (revision < lastRoomRevision) return;
    lastRoomRevision = revision;
    IS_HOST = Boolean(data.is_host);
    hasSeekAccess = Boolean(data.participants?.[String(USER_ID)]?.has_seek_access);
    activeControllerId = data.controller_id == null ? null : String(data.controller_id);
    updateParticipantsList(data.participants || {});
    document.getElementById('host-media-controls').style.display = IS_HOST ? 'block' : 'none';
    const deleteForm = document.getElementById('delete-room-form');
    if (deleteForm) deleteForm.style.display = IS_HOST ? 'inline-block' : 'none';
    const host = data.participants?.[String(data.host_id)];
    if (host) document.getElementById('host-name').textContent = host.username;
    const media = data.media || { type: 'youtube', url: '' };
    if (media.type !== currentMediaType || media.url !== currentMediaUrl) {
        currentMediaType = media.type;
        currentMediaUrl = media.url;
        document.getElementById('waiting-prompt').style.display = 'none';
        const memberChoiceEl = document.getElementById('member-media-choice');

        if (media.type === 'youtube' && media.url) {
            if (memberChoiceEl) memberChoiceEl.style.display = 'none';
            loadYouTubeVideo(media.url);
        } else if (media.type === 'video' && media.url) {
            if (memberChoiceEl) memberChoiceEl.style.display = 'none';
            loadDirectVideo(media.url);
        } else if (media.type === 'range') {
            if (IS_HOST && activeFile) {
                // Host: already playing from blob URL, do nothing special
                console.log('[MEDIA HOST] range media registered, host playing locally');
                switchPlayer('video');
            } else if (!IS_HOST) {
                // Member: show the choice prompt if they haven't chosen yet
                if (!memberChoice) {
                    console.log('[MEDIA MEMBER] media state received, showing choice prompt');
                    if (memberChoiceEl) memberChoiceEl.style.display = 'flex';
                } else if (memberChoice === 'stream') {
                    // Already chose stream — make sure we're pointed at range-stream
                    console.log('[MEDIA MEMBER] media state received, already streaming');
                }
            }
        } else if (!media.url && !IS_HOST) {
            // No media yet, show waiting prompt
            document.getElementById('waiting-prompt').style.display = 'flex';
        }
    }

    // Host: fulfill range chunk requests from members
    if (IS_HOST && activeFile && data.requested_chunks && data.requested_chunks.length > 0 && !isUploadingChunk) {
        processRangeQueue(data.requested_chunks);
    }

    applyPlayback(data.state);
}

async function refreshRoom() {
    if (pollInFlight) return;
    pollInFlight = true;
    try { applyRoomState(await api()); }
    catch (error) { console.error('Room update failed:', error); setPlayerStatus('Room connection failed. Refresh to reconnect.'); }
    finally { pollInFlight = false; }
}

async function changeMedia(media) {
    try {
        const state = await api('/media', { method: 'POST', body: JSON.stringify({ media }) });
        applyRoomState(state);
        // Force-load the video the user just picked, even if the URL didn't change
        const url = state.media?.url || media.url;
        if (media.type === 'youtube' && url) loadYouTubeVideo(url);
        else if (media.type === 'video' && url) loadDirectVideo(url);
        else if (media.type === 'hls' && url) {
            if (IS_HOST && activeFile) loadLocalFile(activeFile);
            else loadHlsVideo(url);
        }
    } catch (error) { setPlayerStatus(error.message); }
}

function emitSync(status, time, heartbeat = false) {
    if (!hasSeekAccess || isSyncing) return;
    if (heartbeat && queuedSync && !queuedSync.heartbeat) return;
    // For user-initiated actions (not heartbeats), set a cooldown so that the
    // next background poll doesn't undo the seek before the server has caught up.
    if (!heartbeat) syncCooldownUntil = Date.now() + 1200;
    queuedSync = { status, time, heartbeat };
    flushSyncQueue();
}

async function flushSyncQueue() {
    if (syncInFlight || !queuedSync) return;
    syncInFlight = true;
    const state = queuedSync;
    queuedSync = null;
    try {
        const response = await api('/sync', {
            method: 'POST',
            body: JSON.stringify({
                state: { status: state.status, time: state.time },
                heartbeat: state.heartbeat
            })
        });
        activeControllerId = response.controller_id == null ? null : String(response.controller_id);
        lastRoomRevision = Math.max(lastRoomRevision, Number(response.revision || lastRoomRevision));
    } catch (error) {
        console.warn('Playback update failed:', error);
    } finally {
        syncInFlight = false;
        flushSyncQueue();
    }
}

async function updateAccess(targetUserId, granted) {
    try { applyRoomState(await api('/access', { method: 'POST', body: JSON.stringify({ target_user_id: targetUserId, granted }) })); }
    catch (error) { setPlayerStatus(error.message); }
}

const searchInput = document.getElementById('yt-search-input');
const searchButton = document.getElementById('yt-search-btn');
async function doSearch() {
    const query = searchInput?.value.trim();
    if (!query) return;
    const status = document.getElementById('search-status');
    const results = document.getElementById('search-results');
    status.style.display = 'flex'; results.replaceChildren();
    try {
        const data = await fetch(`/api/search?q=${encodeURIComponent(query)}`, { credentials: 'same-origin' }).then(response => response.json());
        if (!data.results?.length) throw new Error('No videos found.');
        data.results.forEach(video => {
            const card = document.getElementById('search-result-tpl').content.cloneNode(true);
            card.querySelector('img').src = video.thumbnail;
            card.querySelector('img').alt = video.title;
            card.querySelector('.thumb-duration').textContent = video.duration || '';
            card.querySelector('.search-title').textContent = video.title || '';
            card.querySelector('.search-channel').textContent = video.channel || '';
            card.querySelector('.search-views').textContent = video.views || '';
            card.querySelector('.btn-play-result').addEventListener('click', () => changeMedia({ type: 'youtube', url: `https://www.youtube.com/watch?v=${video.id}` }));
            results.appendChild(card);
        });
    } catch (error) { results.textContent = error.message || 'Search failed.'; }
    finally { status.style.display = 'none'; }
}
searchButton?.addEventListener('click', doSearch);
searchInput?.addEventListener('keydown', event => { if (event.key === 'Enter') doSearch(); });

function extractYTId(value) {
    const url = (value || '').trim();
    if (/^[\w-]{11}$/.test(url)) return url;
    try {
        const parsed = new URL(url), host = parsed.hostname.replace(/^www\./, '');
        const parts = parsed.pathname.split('/').filter(Boolean);
        if (host === 'youtu.be') return parts[0] || '';
        if (['youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtube-nocookie.com'].includes(host)) {
            return ['embed', 'v', 'shorts', 'live'].includes(parts[0]) ? (parts[1] || '') : (parsed.searchParams.get('v') || '');
        }
    } catch (_) {}
    return '';
}

window.onYouTubeIframeAPIReady = function () {
    ytApiReady = true;
    if (currentMediaType === 'youtube' && currentMediaUrl) loadYouTubeVideo(currentMediaUrl);
};
function onPlayerStateChange(event) {
    if (event.data === YT.PlayerState.CUED || event.data === YT.PlayerState.PLAYING) setPlayerStatus('');
    // Any user with seek access: publish position immediately on seek (BUFFERING)
    // so polls can't overwrite with the pre-seek timestamp.
    if (event.data === YT.PlayerState.BUFFERING && hasSeekAccess && !isSyncing) {
        window.setTimeout(() => emitSync('playing', player.getCurrentTime()), 250);
    }
    if (isSyncing) return;
    if (event.data === YT.PlayerState.PLAYING) emitSync('playing', player.getCurrentTime());
    if (event.data === YT.PlayerState.PAUSED) emitSync('paused', player.getCurrentTime());
}
function onPlayerError(event) {
    const errors = {
        2: 'This YouTube link is invalid.',
        5: 'This video cannot be embedded.',
        100: 'This video is unavailable.',
        101: 'YouTube does not permit this video to play in an embedded room.',
        150: 'YouTube does not permit this video to play in an embedded room.',
        153: 'YouTube could not verify this player. Refresh and try again.'
    };
    setPlayerStatus(errors[event.data] || 'YouTube could not play this video.');
}
function loadYouTubeVideo(url) {
    const videoId = extractYTId(url);
    if (!videoId) return setPlayerStatus('Enter a valid YouTube URL or video ID.');
    if (html5Player) { html5Player.pause(); html5Player.style.display = 'none'; }
    ytWrapper.style.display = 'block';
    if (!ytApiReady) { setPlayerStatus('Loading video...'); return; }
    // Reuse existing player — just swap the video, no status flicker
    if (player?.loadVideoById) { player.loadVideoById(videoId); return; }
    // First load — create a new player
    setPlayerStatus('Loading video...');
    player = new YT.Player('yt-player', {
        height: '100%', width: '100%', videoId,
        playerVars: { playsinline: 1, rel: 0, origin: window.location.origin },
        events: {
            onReady: () => { setPlayerStatus(''); isSyncing = false; applyPlayback(pendingPlayback); },
            onStateChange: onPlayerStateChange,
            onError: onPlayerError
        }
    });
}
let hlsInstance = null;
function loadHlsVideo(url) {
    if (!html5Player) return;
    if (player?.pauseVideo) player.pauseVideo();
    ytWrapper.style.display = 'none';
    document.getElementById('waiting-prompt').style.display = 'none';
    
    if (Hls.isSupported()) {
        if (hlsInstance) hlsInstance.destroy();
        hlsInstance = new Hls();
        hlsInstance.loadSource(url);
        hlsInstance.attachMedia(html5Player);
    } else if (html5Player.canPlayType('application/vnd.apple.mpegurl')) {
        // Native Safari support
        html5Player.src = url;
    }
    html5Player.style.display = 'block';
}
function loadDirectVideo(url) {
    if (!html5Player) return;
    switchPlayer('video');
    html5Player.src = url;
    html5Player.load();
}
function loadLocalFile(file) {
    if (!html5Player) return;
    if (player?.pauseVideo) player.pauseVideo();
    ytWrapper.style.display = 'none';
    document.getElementById('waiting-prompt').style.display = 'none';
    html5Player.src = URL.createObjectURL(file);
    html5Player.style.display = 'block';
}
function switchPlayer(mode) {
    if (mode === 'video') {
        if (player?.pauseVideo) player.pauseVideo();
        ytWrapper.style.display = 'none';
        document.getElementById('waiting-prompt').style.display = 'none';
        const memberChoiceEl = document.getElementById('member-media-choice');
        if (memberChoiceEl) memberChoiceEl.style.display = 'none';
        html5Player.style.display = 'block';
    } else {
        html5Player.pause();
        html5Player.style.display = 'none';
        ytWrapper.style.display = 'block';
    }
}
function loadRangeStream() {
    if (!html5Player) return;
    const streamUrl = `${roomApi}/media/range-stream`;
    console.log(`[MEDIA MEMBER] requesting URL=${streamUrl}`);
    switchPlayer('video');
    html5Player.src = streamUrl;
    html5Player.load();
    console.log('[MEDIA MEMBER] player initialized with range-stream URL');
}
html5Player?.addEventListener('play', () => emitSync('playing', html5Player.currentTime));
html5Player?.addEventListener('pause', () => emitSync('paused', html5Player.currentTime));
html5Player?.addEventListener('seeked', () => emitSync(html5Player.paused ? 'paused' : 'playing', html5Player.currentTime));

document.querySelectorAll('.tab-btn').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(item => item.classList.remove('active'));
    document.querySelectorAll('.input-section').forEach(item => item.style.display = 'none');
    button.classList.add('active'); document.getElementById(button.dataset.target).style.display = 'flex';
}));
// Smart URL paste handler — auto-detects YouTube vs direct video URL
function loadPasteUrl() {
    const input = document.getElementById('paste-url-input');
    const raw = (input?.value || '').trim();
    if (!raw) return setPlayerStatus('Paste a YouTube URL or direct video link.');
    // Detect direct video links
    if (/\.(mp4|webm|ogg|mov|mkv)(\?|$)/i.test(raw)) {
        changeMedia({ type: 'video', url: raw });
    } else {
        // Treat everything else as YouTube (extractYTId will validate)
        const id = extractYTId(raw);
        if (!id) return setPlayerStatus('Not a valid YouTube URL or direct video link.');
        changeMedia({ type: 'youtube', url: `https://www.youtube.com/watch?v=${id}` });
    }
}
document.getElementById('load-paste-url-btn')?.addEventListener('click', loadPasteUrl);
document.getElementById('paste-url-input')?.addEventListener('keydown', e => { if (e.key === 'Enter') loadPasteUrl(); });


document.getElementById('load-local-btn')?.addEventListener('click', async (e) => {
    if (e) e.preventDefault();
    activeFile = document.getElementById('local-file').files[0];
    if (!activeFile) {
        setPlayerStatus('Choose a video first.');
        return;
    }
    
    window.fileTiming = { selected: performance.now() };
    console.log(`[TIMING] file selected at ${window.fileTiming.selected}`);
    console.log(`[MEDIA HOST] file selected`);
    console.log(`[MEDIA HOST] filename=${activeFile.name}`);
    console.log(`[MEDIA HOST] size=${activeFile.size}`);
    console.log(`[MEDIA HOST] type=${activeFile.type}`);
    
    window.fileTiming.parsingStarted = performance.now();
    console.log(`[TIMING] metadata parsing started at ${window.fileTiming.parsingStarted} (delta: ${window.fileTiming.parsingStarted - window.fileTiming.selected}ms)`);
    
    window.fileTiming.parsingCompleted = performance.now();
    console.log(`[TIMING] metadata parsing completed at ${window.fileTiming.parsingCompleted} (delta: ${window.fileTiming.parsingCompleted - window.fileTiming.parsingStarted}ms)`);
    
    const sizeMb = (activeFile.size / (1024 * 1024)).toFixed(1);
    setPlayerStatus(`${activeFile.name} — ${sizeMb} MB\nPreparing stream...`);
    
    try {
        // Register with range proxy backend
        const initResp = await fetch(`${roomApi}/media/range-init`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ size: activeFile.size, contentType: activeFile.type || 'video/mp4' })
        });
        if (!initResp.ok) {
            const err = await initResp.json().catch(() => ({}));
            setPlayerStatus(`Failed to register stream: ${err.error || initResp.status}`);
            return;
        }
        console.log('[MEDIA HOST] media registered with range-init');
        
        // Clear chunk cache for new file
        rangeChunkCache.clear();
        isUploadingChunk = false;
        
        // Broadcast to room so members know media type is 'range'
        await changeMedia({ type: 'range', url: `${roomApi}/media/range-stream` });
        console.log('[MEDIA HOST] media broadcast to room as type=range');
        
        // Play locally from blob URL (fast, no network)
        if (html5Player) {
            html5Player.src = URL.createObjectURL(activeFile);
            html5Player.load();
            switchPlayer('video');
        }
        setPlayerStatus('');
    } catch (err) {
        console.error('[MEDIA HOST] initialization error:', err);
        setPlayerStatus(`Failed to initialize stream: ${err.message}`);
    }
});

// Member choice button handlers
document.getElementById('btn-watch-host')?.addEventListener('click', () => {
    memberChoice = 'stream';
    console.log('[MEDIA MEMBER] chose: Watch Host Stream');
    document.getElementById('member-media-choice').style.display = 'none';
    loadRangeStream();
});

document.getElementById('btn-choose-local')?.addEventListener('click', () => {
    memberChoice = 'local';
    console.log('[MEDIA MEMBER] chose: Choose File From Device');
    document.getElementById('member-media-choice').style.display = 'none';
    const filePicker = document.getElementById('member-local-file');
    filePicker.click();
});

document.getElementById('member-local-file')?.addEventListener('change', (e) => {
    memberLocalFile = e.target.files[0];
    if (!memberLocalFile) return;
    console.log(`[MEDIA MEMBER] local file selected: ${memberLocalFile.name}`);
    loadLocalFile(memberLocalFile);
});

window.addEventListener('youtube-api-load-error', () => setPlayerStatus('The YouTube player script could not load. Check your connection or content blocker.'));
let leaveSent = false;
function notifyRoomLeave() {
    if (leaveSent) return;
    leaveSent = true;
    const body = new Blob(['{}'], { type: 'application/json' });
    if (navigator.sendBeacon?.(`${roomApi}/leave`, body)) return;
    fetch(`${roomApi}/leave`, { method: 'POST', credentials: 'same-origin', keepalive: true }).catch(() => {});
}
window.addEventListener('pagehide', notifyRoomLeave);
window.addEventListener('beforeunload', notifyRoomLeave);

// Host publishes the advancing clock every 500ms for tighter guest sync.
// Guests poll at 500ms; host at 1000ms (they already push via heartbeat).
window.setInterval(() => {
    if (!hasSeekAccess || String(USER_ID) !== activeControllerId) return;
    try {
        if (currentMediaType === 'youtube' && player?.getPlayerState && player.getPlayerState() === YT.PlayerState.PLAYING) {
            emitSync('playing', player.getCurrentTime(), true);
        }
        if ((currentMediaType === 'video' || currentMediaType === 'range') && html5Player && !html5Player.paused) {
            emitSync('playing', html5Player.currentTime, true);
        }
    } catch (_) {}
}, 500);

refreshRoom();
const POLL_MS = IS_HOST ? 1000 : 500;
window.setInterval(refreshRoom, POLL_MS);


async function processRangeQueue(requestedChunks) {
    if (isUploadingChunk || !activeFile) return;
    
    for (const chunkId of requestedChunks) {
        if (rangeChunkCache.has(chunkId)) continue;
        
        isUploadingChunk = true;
        try {
            const [startStr, endStr] = chunkId.split('-');
            const startByte = parseInt(startStr, 10);
            const endByte = parseInt(endStr, 10);
            
            // Read slice
            const slice = activeFile.slice(startByte, endByte + 1);
            const buffer = await slice.arrayBuffer();
            
            // Upload chunk
            await fetch(`${roomApi}/media/range-chunk/${chunkId}`, {
                method: 'POST',
                body: buffer,
                headers: { 'Content-Type': 'application/octet-stream' }
            });
            
            rangeChunkCache.set(chunkId, true);
        } catch (err) {
            console.error("Failed to upload chunk", chunkId, err);
        } finally {
            isUploadingChunk = false;
        }
    }
}

let myStream;
let janusConnection;
let janusPlugin;
let remoteFeed       = null;
let myid             = null;
let mypvtid          = null;
let doorbellID       = null;
let isAudioMuted     = false;
let isSpeaking       = false;
let isRemoteAudioMuted = true;
let pollTimer        = null;
let reconnectTimer   = null;
let creatingSubscription = false;
let subscribeQueue   = Promise.resolve();

// Audio gain variables
let audioContext = null;
let mediaStream = null;
let sourceNode = null;
let gainNode = null;
let destinationStream = null;
let currentGain = 3.0;  // raised from 5.0

const janusServer    = "********";
const janusProtocol  = "wss";
const janusPort      = "443";
const myroom         = ********;

let feedStreams       = {};
let subscriptions    = {};
let remoteTracks     = {};

const url  = new URL(window.location.href);
const user = url.searchParams.get("user") || "webuser";

const connectionStatus        = document.getElementById('connection_status');
const loadingDoorbell         = document.getElementById('loadingDoorbell');
const errorConnectionDoorbell = document.getElementById('errorConnectionDoorbell');
const remoteVideo             = document.getElementById('remoteVideo');
const muteButton              = document.getElementById('muteButton');
const talkButton              = document.getElementById('talkButton');
const openDoorButton          = document.getElementById('openDoorButton');
const hangupButton            = document.getElementById('hangupButton');
const speakerButton           = document.getElementById('speakerButton');


remoteVideo.muted = true;
remoteVideo.setAttribute('playsinline', '');


let audioCtx = null;

function getAudioContext() {
    if (!audioCtx || audioCtx.state === 'closed') {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (audioCtx.state === 'suspended') {
        audioCtx.resume();
    }
    return audioCtx;
}

function playChime() {
    const ctx = getAudioContext();
    if (ctx.state !== 'running') {
        console.warn("AudioContext not running yet, chime skipped");
        return;
    }
    const notes = [783.99, 659.25, 523.25, 659.25];
    let time = ctx.currentTime;
    notes.forEach((freq) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.frequency.value = freq;
        osc.type = 'sine';
        gain.gain.setValueAtTime(0.5, time);
        gain.gain.exponentialRampToValueAtTime(0.001, time + 0.6);
        osc.start(time);
        osc.stop(time + 0.6);
        time += 0.45;
    });
}
function getTokenExpiry() {
    const token = url.searchParams.get('token') || '';
    const parts = token.split('.');
    if (parts.length !== 2) return null;
    const expiry = parseInt(parts[1]);
    return isNaN(expiry) ? null : expiry;
}

function isTokenExpired() {
    const expiry = getTokenExpiry();
    if (!expiry) return true;
    return Math.floor(Date.now() / 1000) > expiry;
}

function updateDoorButtonState() {
    const btn = document.getElementById('openDoorButton');
    if (!btn) return;
    if (isTokenExpired()) {
        btn.disabled = true;
        btn.style.opacity = '0.35';
        btn.title = 'Token expired';
    } else {
        btn.disabled = false;
        btn.style.opacity = '';
        btn.title = 'Open door';
    }
}

function updateMicButton() {
    if (!muteButton) return;
    muteButton.innerHTML = isAudioMuted
        ? `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="2" y1="2" x2="22" y2="22"/><path d="M18.89 13.23A7.12 7.12 0 0 0 19 12v-2"/><path d="M5 10v2a7 7 0 0 0 12 5"/><path d="M15 9.34V5a3 3 0 0 0-5.94-.6"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12"/><line x1="12" y1="19" x2="12" y2="22"/></svg>`
        : `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg>`;
    muteButton.classList.toggle('danger-active', isAudioMuted);
    muteButton.title = isAudioMuted ? 'Unmute your microphone' : 'Mute your microphone';
}

function updateSpeakerButton() {
    if (!speakerButton) return;
    speakerButton.innerHTML = isRemoteAudioMuted
        ? `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>`
        : `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>`;
    speakerButton.classList.toggle('danger-active', isRemoteAudioMuted);
    speakerButton.title = isRemoteAudioMuted ? 'Unmute doorbell audio' : 'Mute doorbell audio';
}

function toggleMute() {
    if (!janusPlugin) return;
    isAudioMuted = !isAudioMuted;
    if (isAudioMuted) janusPlugin.muteAudio();
    else janusPlugin.unmuteAudio();
    updateMicButton();
}

function toggleRemoteAudio() {
    if (!remoteVideo) return;
    isRemoteAudioMuted = !isRemoteAudioMuted;
    remoteVideo.muted = isRemoteAudioMuted;
    updateSpeakerButton();
    console.log("Remote audio muted:", isRemoteAudioMuted);
}

$(document).ready(function() {
    initializeJanus();
    window.debugDoorbell = debug;
    // Auto-unmute remote audio on first page click (bypass autoplay)
    document.body.addEventListener('click', function unmuteOnce() {
		if (remoteVideo && isRemoteAudioMuted) toggleRemoteAudio();
		getAudioContext();  // ← add this line
		document.body.removeEventListener('click', unmuteOnce);
	}, { once: true });
});

function initializeJanus() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    Janus.init({
        debug: false,
        dependencies: Janus.useDefaultDependencies({ adapter: adapter }),
        callback: function() {
            janusConnection = new Janus({
                server: `${janusProtocol}://${janusServer}:${janusPort}/janus`,
                iceServers: [
                    { urls: "stun:stun.l.google.com:19302" },
                    { urls: "stun:stun.relay.metered.ca:80" },
                    { urls: "turn:eu.relay.metered.ca:80", username: "********", credential: "********" },
                    { urls: "turn:eu.relay.metered.ca:80?transport=tcp", username: "********", credential: "********" },
                    { urls: "turn:eu.relay.metered.ca:443", username: "********", credential: "********" },
                    { urls: "turn:eu.relay.metered.ca:443?transport=tcp", username: "********", credential: "********" },
                    { urls: "turns:eu.relay.metered.ca:443", username: "********", credential: "********" },
                    { urls: "turns:eu.relay.metered.ca:443?transport=tcp", username: "********", credential: "********" },
                ],
                success: onJanusConnected,
                error: function(error) {
                    console.error("Janus connection error:", error);
                    updateConnectionStatus('red', 'Connection failed');
                    showDoorbellError("Cannot reach Janus server");
                    scheduleReconnect();
                },
                destroyed: function() {
                    updateConnectionStatus('gray', 'Session destroyed');
                    scheduleReconnect();
                }
            });
        }
    });
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
        console.log("Reconnecting...");
        initializeJanus();
        reconnectTimer = null;
    }, 3000);
}

function onJanusConnected() {
    updateConnectionStatus('orange', 'Joining room...');
    janusConnection.attach({
        plugin: "janus.plugin.videoroom",
        success: function(pluginHandle) {
            janusPlugin = pluginHandle;
            console.log("Publisher handle attached:", janusPlugin.getId());
            janusPlugin.send({ message: { request: "join", room: myroom, ptype: "publisher", display: user } });
        },
        error: function(error) {
            console.error("Plugin attach error:", error);
            updateConnectionStatus('red', 'Plugin error');
            scheduleReconnect();
        },
        iceState: function(state) { console.log("Publisher ICE state:", state); },
        webrtcState: function(on) { console.log("Publisher WebRTC:", on ? "up" : "down"); },
        onmessage: function(msg, jsep) { handlePublisherMessage(msg, jsep); },
        onlocalstream: function(stream) {
            console.log("Local stream obtained:", stream.getTracks().map(t => t.kind));
        },
        oncleanup: function() {
            if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());
            if (audioContext) audioContext.close();
            mediaStream = null;
            audioContext = null;
        }
    });
}

function handlePublisherMessage(msg, jsep) {
    const event = msg["videoroom"];
    console.log(`[participants] handlePublisherMessage event="${event}"`, Object.keys(msg));
    if (event === "joined") {
        myid = msg["id"];
        mypvtid = msg["private_id"];
        console.log(`Joined room ${myroom}, my ID: ${myid}`);
        updateConnectionStatus('green', 'In room');
        const publishers = msg["publishers"] || [];
        console.log(`[participants] joined event — publishers in room: ${publishers.length}`, publishers.map(p => `${p.display}(${p.id})`));
        if (publishers.length > 0) handlePublishers(publishers);
        else { showDoorbellError("Doorbell not connected"); startPolling(); }
        // attendees = non-publishing participants already in the room
        const attendees = msg["attendees"] || [];
        console.log(`[participants] joined event — attendees in room: ${attendees.length}`, attendees.map(p => `${p.display}(${p.id})`));
        attendees.filter(p => p.id !== myid).forEach(p => addParticipant(p.id, p.display || String(p.id)));
        console.log(`[participants] requesting listparticipants`);
        janusPlugin.send({ message: { request: "listparticipants", room: myroom } });
    }
    else if (event === "event") {
        if (msg["publishers"] && msg["publishers"].length > 0) {
            stopPolling();
            console.log(`[participants] publishers event:`, msg["publishers"].map(p => `${p.display}(${p.id})`));
            handlePublishers(msg["publishers"]);
            // Also ensure they appear in the avatar row (handlePublishers skips doorbell)
            msg["publishers"].filter(p => p.display !== "doorbell" && p.id !== myid)
                .forEach(p => addParticipant(p.id, p.display || String(p.id)));
        }
        if (msg["leaving"] || msg["unpublished"]) {
            const gone = msg["leaving"] || msg["unpublished"];
            console.log(`[participants] leaving/unpublished: ${gone}`);
            if (gone === doorbellID) {
                doorbellID = null;
                if (remoteFeed) remoteFeed.detach();
                remoteFeed = null;
                if (remoteVideo) remoteVideo.srcObject = null;
                showDoorbellError("Doorbell disconnected");
                startPolling();
            }
            removeParticipant(gone);
        }
        if (msg["participants"]) {
            const all = msg["participants"];
            console.log(`[participants] listparticipants response — ${all.length} total:`, all.map(p => `${p.display}(${p.id}) pub=${p.publisher}`));
            const active = all.filter(p => p.publisher === true);
            if (active.length > 0) { stopPolling(); handlePublishers(active); }
            // Add everyone (publishers and listeners) except ourselves
            all.filter(p => p.id !== myid).forEach(p => {
                console.log(`[participants] adding from listparticipants: ${p.display}(${p.id})`);
                addParticipant(p.id, p.display || String(p.id));
            });
        }
        // New participant joined (non-publishing)
        if (msg["joining"]) {
            const p = msg["joining"];
            console.log(`[participants] joining event: ${p.display}(${p.id})`);
            if (p.id !== myid) addParticipant(p.id, p.display || String(p.id));
        }
        // Talking events
        if (msg["talking"] !== undefined) {
            setParticipantTalking(msg["id"], msg["talking"]);
        }
    }
    else if (event === "participants") {
        const all = msg["participants"] || [];
        console.log(`[participants] listparticipants response — ${all.length} total:`, all.map(p => `${p.display}(${p.id}) pub=${p.publisher}`));
        const active = all.filter(p => p.publisher === true);
        if (active.length > 0) { stopPolling(); handlePublishers(active); }
        all.filter(p => p.id !== myid).forEach(p => {
            console.log(`[participants] adding from listparticipants: ${p.display}(${p.id})`);
            addParticipant(p.id, p.display || String(p.id));
        });
    }
    else if (event === "destroyed") scheduleReconnect();
    if (jsep) janusPlugin.handleRemoteJsep({ jsep });
}

function handlePublishers(publishers) {
    console.log(`[participants] handlePublishers called with ${publishers.length}:`, publishers.map(p => `${p.display}(${p.id})`));
    for (const pub of publishers) {
        if (pub.display === "doorbell") {
            if (doorbellID !== pub.id || !remoteFeed) {
                doorbellID = pub.id;
                loadingDoorbell.style.display = "none";
                errorConnectionDoorbell.style.display = "none";
                updateConnectionStatus('orange', 'Subscribing...');
                subscribeQueue = subscribeQueue.then(() => subscribeToDoorbell(pub.streams || [], pub.id, pub.display));
            }
            continue;
        }
        console.log(`[participants] adding from handlePublishers: ${pub.display}(${pub.id})`);
        addParticipant(pub.id, pub.display || String(pub.id));
    }
    if (!doorbellID) showDoorbellError("Doorbell not in room — waiting...");
}

function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => {
        if (doorbellID) { stopPolling(); return; }
        if (janusPlugin) janusPlugin.send({ message: { request: "listparticipants", room: myroom } });
    }, 3000);
}

function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

async function subscribeToDoorbell(streams, feedId, displayName) {
    if (remoteFeed) {
        const subs = streams.length > 0 ? streams.map(s => ({ feed: feedId, mid: s.mid })) : [{ feed: feedId }];
        remoteFeed.send({ message: { request: "update", subscribe: subs } });
        return;
    }
    const joinBody = streams.length > 0
		? { request: "join", room: myroom, ptype: "subscriber", 
			streams: streams.map(s => ({ feed: feedId, mid: s.mid })),
			data: true }        // ← add this
		: { request: "join", room: myroom, ptype: "subscriber", 
        feed: feedId, data: true };   // ← and this
    return new Promise((resolve, reject) => {
        if (creatingSubscription) { reject(new Error("Already creating subscription")); return; }
        creatingSubscription = true;
        janusConnection.attach({
            plugin: "janus.plugin.videoroom",
            success: function(pluginHandle) {
                remoteFeed = pluginHandle;
                console.log("Subscriber handle attached:", remoteFeed.getId());
                feedStreams[feedId] = { id: feedId, display: displayName, streams };
                remoteFeed.send({ message: joinBody });
                creatingSubscription = false;
                resolve();
            },
            error: function(error) {
                console.error("Subscriber attach error:", error);
                creatingSubscription = false;
                reject(error);
                scheduleReconnect();
            },
            iceState: function(state) {
                console.log("Subscriber ICE state:", state);
                if (state === 'disconnected' || state === 'failed') {
                    console.warn("Subscriber ICE failed/disconnected — scheduling reconnect");
                    scheduleReconnect();
                }
            },
            webrtcState: function(on) {
                console.log("Subscriber WebRTC:", on ? "up" : "down");
                if (!on) scheduleReconnect();
            },
            slowLink: function(uplink, lost) {
                console.warn("Subscriber slow link — uplink:", uplink, "lost:", lost);
            },
            onmessage: function(msg, jsep) {
                if (jsep) {
                    if (!remoteFeed) return;
                    remoteFeed.createAnswer({
                        jsep,
                        tracks: [{ type: 'audio', capture: false, recv: true }, { type: 'video', capture: false, recv: true }, { type: 'data' }],
                        success: function(answerJsep) {
                            if (remoteFeed) remoteFeed.send({ message: { request: "start", room: myroom }, jsep: answerJsep });
                        },
                        error: function(err) { console.error("createAnswer error:", err); scheduleReconnect(); }
                    });
                }
            },
            onremotetrack: function(track, mid, on) {
                if (on) {
                    if (!remoteVideo.srcObject) remoteVideo.srcObject = new MediaStream();
                    remoteVideo.srcObject.addTrack(track);
                    remoteTracks[mid] = track;
                    if (track.kind === 'video') {
                        remoteVideo.muted = isRemoteAudioMuted;
                        remoteVideo.play().catch(e => console.warn("Play error:", e));
                        remoteVideo.onloadeddata = () => {
                            loadingDoorbell.style.display = "none";
                            errorConnectionDoorbell.style.display = "none";
                            updateConnectionStatus('green', 'Doorbell connected');
                        };
                    } else if (track.kind === 'audio') {
                        track.enabled = true;
                    }
                } else {
                    const t = remoteTracks[mid];
                    if (t && remoteVideo.srcObject) remoteVideo.srcObject.removeTrack(t);
                    delete remoteTracks[mid];
                }
            },
            ondata: function(data) {
				console.log("Datachannel message received:", data);
				try {
					const msg = JSON.parse(data);
					console.log("Parsed datachannel message:", msg);
					if (msg.type === "door_control" && msg.action === "open") {
						if (typeof window.showDoorToast === 'function') window.showDoorToast();
					} else if (msg.type === "doorbell" && msg.action === "chime") {
						console.log("Playing chime!");
						playChime();
					} else {
						console.warn("Unknown datachannel message type:", msg.type, msg.action);
					}
				} catch(e) {
					console.error("Datachannel parse error:", e, "raw data:", data);
				}
			},
            oncleanup: function() { remoteFeed = null; remoteVideo.srcObject = null; }
        });
    });
}

async function startAmplifiedMic() {
    if (audioContext && audioContext.state !== 'closed') await audioContext.close();
    audioContext = new (window.AudioContext || window.webkitAudioContext)();

    // Re-enable browser processing — helps boost quiet mics automatically
    const originalStream = await navigator.mediaDevices.getUserMedia({ audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
    } });

    mediaStream = originalStream;
    sourceNode = audioContext.createMediaStreamSource(originalStream);
    gainNode = audioContext.createGain();
    gainNode.gain.value = currentGain;
    const dest = audioContext.createMediaStreamDestination();
    sourceNode.connect(gainNode);
    gainNode.connect(dest);
    destinationStream = dest.stream;
	window._destStream = destinationStream;  // ← add this line

    // Tap raw level BEFORE gain for diagnostics
    const rawAnalyser = audioContext.createAnalyser();
    sourceNode.connect(rawAnalyser);
    rawAnalyser.fftSize = 256;
    const rawData = new Uint8Array(rawAnalyser.frequencyBinCount);

    // Tap amplified level AFTER gain
    const ampAnalyser = audioContext.createAnalyser();
    gainNode.connect(ampAnalyser);
    ampAnalyser.fftSize = 256;
    const ampData = new Uint8Array(ampAnalyser.frequencyBinCount);

    setInterval(() => {
        if (!isSpeaking) return;

        rawAnalyser.getByteTimeDomainData(rawData);
        let rawMax = 0;
        for (let i = 0; i < rawData.length; i++) {
            rawMax = Math.max(rawMax, Math.abs((rawData[i] - 128) / 128));
        }

        ampAnalyser.getByteTimeDomainData(ampData);
        let ampMax = 0;
        for (let i = 0; i < ampData.length; i++) {
            ampMax = Math.max(ampMax, Math.abs((ampData[i] - 128) / 128));
        }

        console.log(`🎙️ RAW: ${rawMax.toFixed(3)} | Amplified: ${ampMax.toFixed(3)} (gain=${currentGain}x)`);
    }, 1000);

    return destinationStream;
}

function publishOwnFeed(useAudio) {
    if (!useAudio) {
        janusPlugin.createOffer({
            tracks: [{ type: 'data' }],
            success: function(jsep) {
                janusPlugin.send({ message: { request: "configure", audio: false, video: false }, jsep });
                isSpeaking = false;
                updateTalkButton(false);
            },
            error: function(err) { console.error("createOffer error:", err); }
        });
        return;
    }
    startAmplifiedMic().then(amplifiedStream => {
        janusPlugin.createOffer({
            tracks: [
                { type: 'audio', capture: true, recv: false, stream: amplifiedStream },
                { type: 'data' }
            ],
            success: function(jsep) {
                janusPlugin.send({ message: { request: "configure", audio: true, video: false, audiocodec: "opus" }, jsep });
                isSpeaking = true;
                updateTalkButton(true);
            },
            error: function(err) { console.error("createOffer error:", err); }
        });
    }).catch(err => {
        console.error("Microphone access failed:", err);
        alert("Cannot access microphone: " + err.message);
    });
}

function stopPublishingAudio() {
    if (janusPlugin) janusPlugin.send({ message: { request: "configure", audio: false, video: false } });
    if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());
    if (audioContext) audioContext.close();
    mediaStream = null;
    audioContext = null;
    isSpeaking = false;
    updateTalkButton(false);
}

function toggleTalk() {
    if (!janusPlugin) return;
    if (isSpeaking) stopPublishingAudio();
    else publishOwnFeed(true);
}

const doorToken = url.searchParams.get("token") || "";


function openDoor() {
    if (!janusPlugin) return;
    if (!remoteFeed) {
        console.error("No remote feed available");
        return;
    }
    if (isTokenExpired()) {
        console.warn('Token expired, door open blocked');
        return;
    }
    console.log("🚪 Sending door open, token:", doorToken);
    janusPlugin.data({
        text: JSON.stringify({ type: "door_control", action: "open", token: doorToken }),
        success: () => console.log("✅ Door open message sent successfully"),
        error: (err) => console.error("❌ Door open failed:", err)
    });
}

function hangUp() {
    stopPolling();
    if (isSpeaking) stopPublishingAudio();
    if (janusPlugin) janusPlugin.send({ message: { request: "leave" } });
    remoteVideo.srcObject = null;
    doorbellID = null;
    remoteFeed = null;
    updateConnectionStatus('gray', 'Disconnected');
}

function showDoorbellError(msg) {
    loadingDoorbell.style.display = "none";
    errorConnectionDoorbell.style.display = "block";
    errorConnectionDoorbell.textContent = "❌ " + msg;
}

function updateConnectionStatus(color, text) {
    if (connectionStatus) { connectionStatus.style.backgroundColor = color; connectionStatus.title = text; }
    console.log("Status:", text);
}

function updateTalkButton(active) {
    if (!talkButton) return;
    talkButton.innerHTML = active
        ? `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="1" y1="1" x2="23" y2="23"/><path d="M16.5 16.5l3.77 3.77a2 2 0 0 0 2.73-2.73L19.73 14a2 2 0 0 0-2.11-.45l-1.27 1.27A16 16 0 0 1 9.91 8.09l1.27-1.27A2 2 0 0 0 11.63 4.8L8.27 1.46A2 2 0 0 0 5.54 4.19L9.31 8"/>`
        : `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/></svg>`;
    talkButton.classList.toggle('speaking', active);
}

function debug() {
    console.log("=== Debug ===");
    console.log("My ID:", myid, "| Doorbell ID:", doorbellID);
    console.log("Speaking:", isSpeaking, "| Local mic muted:", isAudioMuted);
    console.log("Remote audio muted:", isRemoteAudioMuted);
    console.log("Remote feed:", remoteFeed ? remoteFeed.getId() : "none");
    console.log("Remote tracks:", Object.keys(remoteTracks));
    if (janusPlugin) janusPlugin.send({ message: { request: "listparticipants", room: myroom } });
}

if (muteButton) { muteButton.addEventListener('click', toggleMute); updateMicButton(); }
if (speakerButton) { speakerButton.addEventListener('click', toggleRemoteAudio); updateSpeakerButton(); }
if (talkButton) talkButton.addEventListener('click', toggleTalk);
if (openDoorButton) openDoorButton.addEventListener('click', openDoor);
if (hangupButton) hangupButton.addEventListener('click', hangUp);

function addParticipant(id, display) {
    console.log(`[participants] addParticipant called: ${display}(${id}), handler exists: ${typeof window.addParticipantAvatar === 'function'}`);
    if (typeof window.addParticipantAvatar === 'function') window.addParticipantAvatar(id, display);
}

function removeParticipant(id) {
    if (typeof window.removeParticipantAvatar === 'function') window.removeParticipantAvatar(id);
}

function setParticipantTalking(id, talking) {
    if (typeof window.setParticipantTalking === 'function') window.setParticipantTalking(id, talking);
}

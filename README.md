This is a works in progress.

So far, it worked up to 90% of features, in Debian Bookworm on Dietpi, but everything broke down when system, was upgraded to Trixie. AV on Bookwrom is too old, and there were inconsistencies between newer version or aiortc and AV.


I will be keeping this here for reference, but moving on, I have converted to Node.js, hoping to solve most problems and make the system more manageable in the future.


WebRTC Doorbell System — Project Summary
Overview
A Raspberry Pi-based video doorbell with two-way audio and door control, using WebRTC via a Janus media server. The Pi runs a Node.js bridge that connects a local RTSP camera (via MediaMTX) to a Janus videoroom, allowing browser clients to see/hear the door and talk back.

Infrastructure
ComponentLocationPurposeRaspberry Pi192.168.1.12Runs Node.js app, camera, mic, speakerMediaMTX192.168.1.12:8889RTSP→WHEP camera bridgeJanus192.168.1.133:8188 (ws)WebRTC media server / videoroomCaddystefe.duckdns.orgHTTPS reverse proxy, WebSocket proxyHome Assistant192.168.1.10:8123Receives doorbell webhookTURNeu.relay.metered.caICE relay for external browser clients

Node.js App Files (doorbell.js, chime.js, janus.js, config.js)
Runs on the Pi. Entry point is doorbell.js.
doorbell.js — main bridge:

Subscribes to camera via WHEP (MediaMTX) using werift
Publishes video + mic audio to Janus videoroom (room 1610)
Subscribes to audio from all other room participants → forwards to FFmpeg speaker via UDP RTP
Video gate: video track disabled when no viewers; activates on doorbell ring or external viewer joining; 10s idle timeout
FFmpeg mic: ALSA plughw:1,0 → Opus RTP → port 5006 → werift track → Janus. Filters: aecho, highpass, loudnorm
FFmpeg speaker: Janus audio → UDP RTP → port 5008 → FFmpeg SDP → ALSA plughw:2,0. Low latency: reorder_queue_size 0, max_delay 0, buffer_size 960, fflags nobuffer, flags low_delay
SSE chime server: port 8765, /chime endpoint, broadcasts to intercom on button press
Auto-detects Pi LAN IP via os.networkInterfaces()
TURN error handling: iceServers:[] for MediaMTX (loopback); full ICE servers for Janus connections; global unhandledRejection swallows ENETUNREACH
Audio subscription upgrade: handles clients that publish data-only first then add audio later via updateSubscriptionAudio() + Janus update request

chime.js — button/token/chime/webhook:

Generates token: {24 alphanumeric}.{unix_expiry_seconds} (10 min validity)
On button press: plays WAV chime via aplay, sends HA webhook with token, broadcasts via datachannel and SSE
Handles incoming datachannel messages: door_control (open door) and video_gate
Door open auth: token === 'intercom' (always valid) or token match + expiry check; single use

janus.js — Janus WebSocket client:

Session + handle management, keepalive, transaction tracking
joinPublisher(), publishStream(), subscribeToFeed(), startSubscriber(), updateSubscriber(), detachHandle()
onPublishers() / onLeaving() callbacks

config.js — loads doorbell.yml via js-yaml
doorbell.yml — all config: IPs, ports, room ID (1610), ALSA device indices, ICE servers, HA webhook IDs

Browser Clients
doorbell-client.js — visitor-facing client:

Joins Janus room as publisher (display name from URL ?user=)
Subscribes to doorbell feed (video + audio)
Talk button: publishes mic audio via amplified AudioContext pipeline (gain 3x)
Door open: sends { type: 'door_control', action: 'open', token } via datachannel; token from URL param, expiry-checked client-side
Chime: plays 4-note browser tone on datachannel doorbell/chime message
Echo fix: suppressLocalAudioPlayback: true, latencyHint: 'interactive'

intercom-client.js — internal intercom client:

Same structure as doorbell-client but user = 'intercom' (hardcoded)
Door open: always authorized, sends token: 'intercom'
SSE: connects to /pi/chime for chime events (after user clicks Start)
Shows showChimeAlert() overlay on chime
Status check: polls /pi/status every 30s
Restart: can POST to /pi/restart
Echo fix: same as doorbell-client



Key Constants
javascriptROOM_ID = 
DISPLAY = 'doorbell'          // Pi's Janus display name
INTERNAL_DISPLAYS = ['doorbell', 'intercom']  // excluded from video gate count
MIC_RTP_PORT = 5006
SPEAKER_RTP_PORT = 5008
VIDEO_IDLE_TIMEOUT_MS = 10000  // 10s after last viewer leaves
ALSA_MIC = 'plughw:1,0'       // webcam mic
ALSA_SPEAKER = 'plughw:2,0'   // USB sound card

Outstanding / To Do

Port 8766 status/restart server (separate script, not yet ported to Node)
NFC door open (separate Python script, direct GPIO recommended)
Token format review (deferred)
Audio latency and echo — partially improved, may need PulseAudio module-echo-cancel for true AEC on Pi
Audio from clients to Pi — fixed but needs real-world testing

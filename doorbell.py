import yaml
import logging
import asyncio
import json
import random
import string
import fractions
import time
import wave
import threading
import av
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.mediastreams import VideoStreamTrack, AudioStreamTrack
import websockets as ws_lib
import pyaudio
import queue
from scipy import signal
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("doorbell")

janus_instance = None
main_loop = None

with open('doorbell.yml', 'r') as file:
    config = yaml.safe_load(file)


LOGIN = config['DOORBELL']['login']
ICESERVERSLIST = config['NETWORK']['iceservers']
BUTTON_GPIO = config['HARDWARE']['button']

WEBHOOK_URL = config['HA']['url'] + ":" + config['HA']['port'] + "/api/webhook/"
WEBHOOK_DOORBELL_IS_RINGING = WEBHOOK_URL + config['WEBHOOK']['doorbell-is-ringing']
WEBHOOK_DOORBELL_ISSUES     = WEBHOOK_URL + config['WEBHOOK']['doorbell-issues']

# Audio settings
AUDIO_SAMPLE_RATE = 32000
AUDIO_CHANNELS = 1
DTYPE = 'int16'
AUDIO_BLOCK_SIZE = int(AUDIO_SAMPLE_RATE * 0.02)

SERVER_URL     = config['SERVER']['IP']
JANUS_SERVER   = config['JANUS']['IP']
JANUS_PROTOCOL = config['JANUS']['protocol']
JANUS_PORT     = config['JANUS']['port']
JANUS_URL      = f"{JANUS_PROTOCOL}://{JANUS_SERVER}:{JANUS_PORT}/"

ROOM                 = config['DOORBELL']['room']
RTSP_URL             = config['CAMERA']['feed']
MIC_DEVICE_INDEX     = config['HARDWARE']['mic']
SPEAKER_DEVICE_INDEX = config['HARDWARE']['speaker']
WEBRTC_SAMPLE_RATE   = 48000
MIC_CHUNK_MS         = 20
SPEAKER_GAIN         = 1

TURN_SERVERS = [
    RTCIceServer(
        urls=server['urls'],
        username=server.get('username'),
        credential=server.get('credential')
    )
    for server in ICESERVERSLIST
]

RTC_CONFIG = RTCConfiguration(iceServers=TURN_SERVERS)

# ── Resampler ──────────────────────────────────────────────────────────────
class Resampler:
    def __init__(self):
        self._cache = {}

    def resample(self, audio_data, original_rate, target_rate):
        if original_rate == target_rate:
            return audio_data
        key = f"{original_rate}_{target_rate}"
        if key not in self._cache:
            self._cache[key] = True
            logger.info(f"Resampler: {original_rate}Hz -> {target_rate}Hz")
        gcd = np.gcd(original_rate, target_rate)
        up = target_rate // gcd
        down = original_rate // gcd
        return signal.resample_poly(audio_data.astype(float), up, down).astype(np.int16)


# ── MicAudioTrack ──────────────────────────────────────────────────────────
class MicAudioTrack(AudioStreamTrack):
    def __init__(self, device_index=0, target_rate=48000):
        super().__init__()
        self.p = pyaudio.PyAudio()
        self.audio_queue = queue.Queue(maxsize=10)
        self.stream = None
        self._running = True
        self._timestamp = 0
        self.device_index = device_index
        self.target_rate = target_rate
        self.resampler = Resampler()
        self._time_base = fractions.Fraction(1, target_rate)
        try:
            info = self.p.get_device_info_by_index(device_index)
            self.device_rate = int(info['defaultSampleRate'])
            self.CHUNK = int(self.device_rate * MIC_CHUNK_MS / 1000)
            logger.info(f"🎤 Mic: {info['name']} ({self.device_rate}Hz -> {target_rate}Hz, chunk={self.CHUNK})")
            self.stream = self.p.open(
                format=pyaudio.paInt16, channels=1, rate=self.device_rate,
                input=True, input_device_index=device_index,
                frames_per_buffer=self.CHUNK, stream_callback=self._cb
            )
            self.stream.start_stream()
        except Exception as e:
            logger.error(f"Mic init failed: {e}")
            self._running = False
            self.device_rate = target_rate

    def _cb(self, in_data, frame_count, time_info, status):
        try:
            self.audio_queue.put_nowait(in_data)
        except queue.Full:
            pass
        return (in_data, pyaudio.paContinue)

    async def recv(self):
        while self._running:
            try:
                data = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.audio_queue.get(timeout=0.05)
                )
                arr = np.frombuffer(data, dtype=np.int16).copy()
                if self.device_rate != self.target_rate:
                    arr = self.resampler.resample(arr, self.device_rate, self.target_rate)
                break
            except queue.Empty:
                await asyncio.sleep(0.01)
        else:
            arr = np.zeros(int(self.target_rate * MIC_CHUNK_MS / 1000), dtype=np.int16)
        frame = av.AudioFrame.from_ndarray(arr.reshape(1, -1), format='s16', layout='mono')
        frame.sample_rate = self.target_rate
        frame.time_base = self._time_base
        frame.pts = int(self._timestamp * self.target_rate)
        self._timestamp += frame.samples / self.target_rate
        return frame

    def stop(self):
        self._running = False
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.p.terminate()

# ── SpeakerOutput — dedicated playback thread ──────────────────────────────
class SpeakerOutput:
    def __init__(self, device_index=0):
        self.p = pyaudio.PyAudio()
        self.stream = None
        self.device_index = device_index
        self.resampler = Resampler()
        self.channels = 2
        self._queue = queue.Queue(maxsize=15)
        self._running = True

        try:
            info = self.p.get_device_info_by_index(device_index)
            self.device_name = info['name']
            self.device_rate = 48000
            logger.info(f"🔊 Speaker: {self.device_name} ({self.device_rate}Hz, {self.channels}ch)")
        except Exception as e:
            logger.error(f"Speaker device {device_index} not available: {e}")
            default_info = self.p.get_default_output_device_info()
            self.device_index = default_info['index']
            self.device_name = default_info['name']
            self.device_rate = 48000
            logger.info(f"🔊 Using default: {self.device_name} ({self.device_rate}Hz)")

        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.device_rate,
            output=True,
            output_device_index=self.device_index,
            frames_per_buffer=int(self.device_rate * MIC_CHUNK_MS / 1000),
            stream_callback=None
        )
        logger.info("✅ Speaker stream opened")

        # Test beep
        test_n = int(self.device_rate * 0.1)
        t = np.linspace(0, 0.1, test_n)
        beep = (np.sin(2 * np.pi * 880 * t) * 8000).astype(np.int16)
        beep_stereo = np.column_stack([beep, beep]).flatten()
        self.stream.write(beep_stereo.tobytes())
        logger.info("🔊 Test beep played")

        self._thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._thread.start()

    def _playback_loop(self):
        while self._running:
            try:
                data = self._queue.get(timeout=0.1)
                try:
                    self.stream.write(data)
                except Exception as e:
                    logger.error(f"Speaker write failed: {e}")
            except queue.Empty:
                continue

    async def play_audio(self, audio_data, source_rate):
        arr = np.frombuffer(audio_data, dtype=np.int16).copy()
        if source_rate != self.device_rate:
            arr = self.resampler.resample(arr, source_rate, self.device_rate)
        stereo = np.column_stack([arr, arr]).flatten()
        if SPEAKER_GAIN != 1:
            stereo = np.clip(stereo * SPEAKER_GAIN, -32768, 32767).astype(np.int16)
        try:
            self._queue.put_nowait(stereo.tobytes())
        except queue.Full:
            try:
                self._queue.get_nowait()  # discard oldest
                self._queue.put_nowait(stereo.tobytes())  # add newest
            except:
                pass

    def play_chime(self):
        rate = 48000
        notes = [(880, 0.3), (660, 0.4)]
        silence = np.zeros(int(rate * 0.05), dtype=np.int16)
        chunks = []
        for freq, duration in notes:
            n = int(rate * duration)
            t = np.linspace(0, duration, n)
            tone = (np.sin(2 * np.pi * freq * t) * 16000).astype(np.int16)
            fade = np.linspace(1.0, 0.0, n)
            tone = (tone * fade).astype(np.int16)
            chunks.append(tone)
            chunks.append(silence)
        audio = np.concatenate(chunks)
        stereo = np.column_stack([audio, audio]).flatten()
        while not self._queue.empty():
            try: self._queue.get_nowait()
            except: break
        self.stream.write(stereo.tobytes())

    def close(self):
        self._running = False
        self._thread.join(timeout=2)
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.p.terminate()

# ── Resilient RTSP video track ─────────────────────────────────────────────
class ResilientRTSPVideoTrack(VideoStreamTrack):
    def __init__(self, rtsp_url, retry_delay=5, frame_timeout=10):
        super().__init__()
        self.rtsp_url = rtsp_url
        self.retry_delay = retry_delay
        self.frame_timeout = frame_timeout
        self._running = True
        self._timestamp = 0
        self._frame_queue = asyncio.Queue(maxsize=2)
        self._worker_task = None
        self._last_frame_time = None

    async def start(self):
        self._worker_task = asyncio.create_task(self._worker())
        asyncio.create_task(self._monitor())

    async def _worker(self):
        while self._running:
            try:
                logger.info(f"Connecting to RTSP: {self.rtsp_url}")
                container = av.open(self.rtsp_url, options={
                    'rtsp_transport': 'tcp',
                    'stimeout': '5000000',
                    'buffer_size': '65535',
                    'fflags': 'nobuffer',
                    'flags': 'low_delay',
                    'max_delay': '0',
                })
                video_stream = next(s for s in container.streams if s.type == 'video')
                for packet in container.demux(video_stream):
                    if not self._running: break
                    for frame in packet.decode():
                        if not self._running: break
                        self._last_frame_time = time.time()
                        await self._frame_queue.put(frame)
                container.close()
            except Exception as e:
                logger.error(f"RTSP error: {e}")
            if self._running:
                await asyncio.sleep(self.retry_delay)

    async def _monitor(self):
        while self._running:
            await asyncio.sleep(5)
            if self._last_frame_time and (time.time() - self._last_frame_time) > self.frame_timeout:
                logger.warning("No video frames, restarting RTSP worker")
                if self._worker_task:
                    self._worker_task.cancel()
                    try: await self._worker_task
                    except: pass
                self._worker_task = asyncio.create_task(self._worker())
                self._last_frame_time = None

    async def recv(self):
        while self._running:
            try:
                frame = await asyncio.wait_for(self._frame_queue.get(), timeout=1.0)
                frame.pts = int(self._timestamp * 90000)
                self._timestamp += 1 / 30
                return frame
            except asyncio.TimeoutError:
                continue
        raise Exception("Video track stopped")

    def stop(self):
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()

# ── JanusClient ────────────────────────────────────────────────────────────
class JanusClient:
    def __init__(self, url, mic_device=0, speaker_device=0):
        self.url = url
        self.ws = None
        self.session_id = None
        self.pub_handle_id = None
        self.pub_pc = None
        self.speaker = SpeakerOutput(speaker_device)
        self.mic_track = None
        self.video_track = None
        self.mic_device = mic_device
        self._my_feed_id = None
        self._pending = {}
        self._async_events = asyncio.Queue()
        self._dispatcher_task = None
        self._keepalive_task = None
        self._health_task = None
        self._running = True
        self._fatal_error = None
        self._data_channel = None
        self._door_token = None
        self._door_token_expiry = None

        # Multi-subscriber state — keyed by feed_id
        self._sub_handles = {}
        self._sub_pcs = {}
        self._sub_tasks = {}

    def _gen_id(self):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=12))

    async def connect(self):
        self.ws = await ws_lib.connect(
            self.url, subprotocols=['janus-protocol'], ping_interval=20, ping_timeout=10
        )
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop())
        resp = await self._request({"janus": "create", "transaction": self._gen_id()})
        if resp['janus'] == 'success':
            self.session_id = resp['data']['id']
            logger.info(f"✅ Janus session: {self.session_id}")
            self._keepalive_task = asyncio.create_task(self._keepalive())
            self._health_task = asyncio.create_task(self._health_check())
            return True
        return False

    async def _dispatch_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                janus = msg.get('janus')
                tx = msg.get('transaction')
                if tx and tx in self._pending:
                    if janus == 'ack': continue
                    fut = self._pending.pop(tx)
                    if not fut.done(): fut.set_result(msg)
                else:
                    if janus == 'timeout' or (janus == 'event' and msg.get('error')):
                        logger.warning(f"Janus session timeout/error: {msg}")
                        self._fatal_error = Exception("Session destroyed")
                    await self._async_events.put(msg)
        except Exception as e:
            logger.warning(f"Dispatcher ended: {e}")
            self._fatal_error = e

    async def _request(self, message):
        if not self.ws or self.ws.closed:
            raise Exception("WebSocket closed")
        if self.session_id and 'session_id' not in message:
            message['session_id'] = self.session_id
        tx_id = message['transaction']
        fut = asyncio.get_event_loop().create_future()
        self._pending[tx_id] = fut
        await self.ws.send(json.dumps(message))
        resp = await asyncio.wait_for(fut, timeout=15)
        if resp.get('janus') == 'error':
            error_code = resp.get('error', {}).get('code')
            error_text = resp.get('error', {}).get('reason', 'Unknown error')
            logger.error(f"Janus error: {error_code} - {error_text}")
            if error_code == 412:
                self._fatal_error = Exception("Session missing")
            raise Exception(f"Janus error: {error_text}")
        return resp

    async def _keepalive(self):
        while self._running and self.session_id and not self._fatal_error:
            await asyncio.sleep(30)
            if not self.pub_handle_id: continue
            try:
                await self._request({
                    "janus": "message", "handle_id": self.pub_handle_id,
                    "transaction": self._gen_id(),
                    "body": {"request": "listparticipants", "room": ROOM}
                })
                logger.debug("Keepalive OK")
            except Exception as e:
                logger.warning(f"Keepalive failed: {e}")
                self._fatal_error = e
                break

    async def _health_check(self):
        while self._running:
            await asyncio.sleep(30)
            if self.pub_pc and self.pub_pc.iceConnectionState in ["failed", "disconnected", "closed"]:
                self._fatal_error = Exception("Publisher ICE failed")
                break
            for feed_id in list(self._sub_pcs.keys()):
                pc = self._sub_pcs.get(feed_id)
                if pc and pc.iceConnectionState in ["failed", "disconnected", "closed"]:
                    logger.warning(f"Subscriber ICE failed for feed {feed_id}, removing")
                    await self._remove_subscriber(feed_id)
            if self.ws and self.ws.closed:
                self._fatal_error = Exception("WebSocket closed")
                break

    async def _attach_handle(self):
        resp = await self._request({
            "janus": "attach", "plugin": "janus.plugin.videoroom", "transaction": self._gen_id()
        })
        return resp['data']['id']

    async def join_and_publish(self, room, display_name):
        self.pub_handle_id = await self._attach_handle()
        logger.info(f"Publisher handle: {self.pub_handle_id}")
        join_resp = await self._request({
            "janus": "message", "handle_id": self.pub_handle_id,
            "transaction": self._gen_id(),
            "body": {"request": "join", "room": room, "ptype": "publisher",
                     "display": display_name, "audio": True, "video": True, "data": True}
        })
        vr_data = join_resp.get('plugindata', {}).get('data', {})
        if vr_data.get('videoroom') != 'joined':
            raise Exception(f"Join failed: {join_resp}")
        self._my_feed_id = vr_data.get('id')
        logger.info(f"Joined room {room}, feed ID: {self._my_feed_id}")

        self.video_track = ResilientRTSPVideoTrack(RTSP_URL)
        await self.video_track.start()

        try:
            self.mic_track = MicAudioTrack(self.mic_device, WEBRTC_SAMPLE_RATE)
            has_audio = True
        except Exception as e:
            logger.error(f"Mic init failed: {e}")
            self.mic_track = None
            has_audio = False

        self.pub_pc = RTCPeerConnection(configuration=RTC_CONFIG)

        # ── Datachannel ────────────────────────────────────────────────────
        self._data_channel = self.pub_pc.createDataChannel("doorbell")

        @self._data_channel.on("open")
        def on_dc_open():
            logger.info("✅ Datachannel open")

        @self._data_channel.on("close")
        def on_dc_close():
            logger.warning("Datachannel closed")

        @self._data_channel.on("message")
        def on_dc_message(message):
            logger.info(f"📨 Raw datachannel message: {message}")
            try:
                msg = json.loads(message)
                logger.info(f"Datachannel message received: {msg}")
                if msg.get("type") == "door_control" and msg.get("action") == "open":
                    incoming_token = msg.get("token", "")
                    if not self._door_token:
                        logger.warning("❌ Door open rejected — no active token")
                    elif time.time() > self._door_token_expiry:
                        logger.warning("❌ Door open rejected — token expired")
                        self._door_token = None
                        self._door_token_expiry = None
                    elif incoming_token == self._door_token:
                        logger.info("✅ Door open authorised — opening door!")
                        self._door_token = None
                        self._door_token_expiry = None
                        asyncio.ensure_future(self.send_datachannel({
                            "type": "door_control",
                            "action": "open"
                        }))
                    else:
                        logger.warning("❌ Door open rejected — token mismatch")
            except Exception as e:
                logger.error(f"Datachannel message error: {e}")
        # ──────────────────────────────────────────────────────────────────

        self.pub_pc.addTrack(self.video_track)
        if has_audio and self.mic_track:
            self.pub_pc.addTrack(self.mic_track)

        @self.pub_pc.on("iceconnectionstatechange")
        async def on_ice():
            state = self.pub_pc.iceConnectionState
            logger.info(f"Publisher ICE: {state}")
            if state in ["failed", "disconnected", "closed"]:
                self._fatal_error = Exception(f"Publisher ICE {state}")

        offer = await self.pub_pc.createOffer()
        await self.pub_pc.setLocalDescription(offer)
        cfg_resp = await self._request({
            "janus": "message", "handle_id": self.pub_handle_id,
            "transaction": self._gen_id(),
            "body": {"request": "configure", "audio": has_audio, "video": True, "data": True},
            "jsep": {"type": self.pub_pc.localDescription.type, "sdp": self.pub_pc.localDescription.sdp}
        })
        if 'jsep' not in cfg_resp:
            raise Exception("No JSEP in configure response")
        await self.pub_pc.setRemoteDescription(
            RTCSessionDescription(sdp=cfg_resp['jsep']['sdp'], type=cfg_resp['jsep']['type'])
        )
        logger.info("Publisher negotiation complete")
        asyncio.create_task(self._wait_and_subscribe(room))

    async def send_datachannel(self, message):
        logger.info(f"Datachannel state: {self._data_channel.readyState if self._data_channel else 'None'}")
        if self._data_channel and self._data_channel.readyState == "open":
            self._data_channel.send(json.dumps(message))
            logger.info(f"✅ Datachannel sent: {message}")
        else:
            state = self._data_channel.readyState if self._data_channel else "None"
            logger.warning(f"Datachannel not ready, state: {state}")

    async def _wait_and_subscribe(self, room):
        logger.info("Monitoring for browser publishers...")

        while self._running and not self._fatal_error:
            try:
                event = await asyncio.wait_for(self._async_events.get(), timeout=30)
            except asyncio.TimeoutError:
                continue

            janus_type = event.get('janus')
            data = event.get('plugindata', {}).get('data', {})
            publishers = data.get('publishers', [])

            # ── New publishers joined ──────────────────────────────────────
            if janus_type == 'event' and publishers:
                others = [p for p in publishers if p.get('id') != self._my_feed_id]
                for feed in others:
                    feed_id = feed['id']
                    streams = feed.get('streams', [])
                    audio_streams = [s for s in streams if s.get('type') == 'audio']

                    if feed_id in self._sub_handles:
                        if audio_streams:
                            handle_id = self._sub_handles[feed_id]
                            subs = [{"feed": feed_id, "mid": s["mid"]} for s in audio_streams]
                            try:
                                await self._request({
                                    "janus": "message", "handle_id": handle_id,
                                    "transaction": self._gen_id(),
                                    "body": {"request": "update", "subscribe": subs}
                                })
                                logger.info(f"Updated subscription for feed {feed_id}")
                            except Exception as e:
                                logger.error(f"Update failed for feed {feed_id}: {e}")
                    else:
                        logger.info(f"New publisher {feed_id} — subscribing for datachannel")
                        asyncio.create_task(self._subscribe_to_feed(room, feed_id))

            # ── Publisher left or unpublished ──────────────────────────────
            elif janus_type == 'event':
                leaving = data.get('leaving') or data.get('unpublished')
                if leaving and leaving in self._sub_handles:
                    logger.info(f"Publisher {leaving} left — removing subscriber")
                    await self._remove_subscriber(leaving)

                feed_id = data.get('feed_id')
                if feed_id and feed_id in self._sub_handles:
                    streams = data.get('streams', [])
                    audio_streams = [s for s in streams if s.get('type') == 'audio']
                    if audio_streams:
                        handle_id = self._sub_handles[feed_id]
                        subs = [{"feed": feed_id, "mid": s["mid"]} for s in audio_streams]
                        try:
                            await self._request({
                                "janus": "message", "handle_id": handle_id,
                                "transaction": self._gen_id(),
                                "body": {"request": "update", "subscribe": subs}
                            })
                            logger.info(f"Stream update applied for feed {feed_id}")
                        except Exception as e:
                            logger.error(f"Stream update failed for feed {feed_id}: {e}")

    async def _subscribe_to_feed(self, room, feed_id):
        try:
            handle_id = await self._attach_handle()
            self._sub_handles[feed_id] = handle_id
            logger.info(f"Subscriber handle {handle_id} for feed {feed_id}")

            sub_resp = await self._request({
                "janus": "message", "handle_id": handle_id,
                "transaction": self._gen_id(),
                "body": {
                    "request": "join",
                    "room": room,
                    "ptype": "subscriber",
                    "feed": feed_id,
                    "data": True
                }
            })
            if 'jsep' not in sub_resp:
                logger.error(f"No JSEP for feed {feed_id}")
                return

            pc = RTCPeerConnection(configuration=RTC_CONFIG)
            self._sub_pcs[feed_id] = pc
            audio_task_started = False

            logger.info(f"Creating subscriber datachannel for feed {feed_id}")
            sub_channel = pc.createDataChannel(f"door-{feed_id}")
            logger.info(f"Subscriber datachannel created: {sub_channel.label}, state: {sub_channel.readyState}")

            @sub_channel.on("open")
            def on_sub_dc_open():
                logger.info(f"📨 Subscriber datachannel OPEN for feed {feed_id}")

            @sub_channel.on("close")
            def on_sub_dc_close():
                logger.warning(f"📨 Subscriber datachannel CLOSED for feed {feed_id}")

            @sub_channel.on("message")
            def on_sub_dc_message(message):
                logger.info(f"📨 Message from feed {feed_id}: {message}")

            @pc.on("iceconnectionstatechange")
            async def on_ice():
                state = pc.iceConnectionState
                logger.info(f"Subscriber ICE [{feed_id}]: {state}")
                if state in ["failed", "disconnected", "closed"]:
                    await self._remove_subscriber(feed_id)

            @pc.on("track")
            async def on_track(track):
                nonlocal audio_task_started
                if track.kind == 'audio' and not audio_task_started:
                    audio_task_started = True
                    logger.info(f"Audio track received from feed {feed_id}")
                    task = asyncio.create_task(self._handle_remote_track(track, feed_id))
                    self._sub_tasks[feed_id] = task

            @pc.on("datachannel")
            def on_datachannel(channel):
                logger.info(f"📨 Datachannel received from feed {feed_id}: {channel.label}")

                @channel.on("message")
                def on_message(message):
                    logger.info(f"📨 Message from feed {feed_id}: {message}")
                    try:
                        msg = json.loads(message)
                        if msg.get("type") == "door_control" and msg.get("action") == "open":
                            incoming_token = msg.get("token", "")
                            if not self._door_token:
                                logger.warning("❌ Door open rejected — no active token")
                            elif time.time() > self._door_token_expiry:
                                logger.warning("❌ Door open rejected — token expired")
                                self._door_token = None
                                self._door_token_expiry = None
                            elif incoming_token == self._door_token:
                                logger.info("✅ Door open authorised — opening door!")
                                self._door_token = None
                                self._door_token_expiry = None
                                asyncio.ensure_future(self.send_datachannel({
                                    "type": "door_control",
                                    "action": "open"
                                }))
                            else:
                                logger.warning("❌ Door open rejected — token mismatch")
                    except Exception as e:
                        logger.error(f"Datachannel message error: {e}")

            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=sub_resp['jsep']['sdp'], type=sub_resp['jsep']['type'])
            )
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await self._request({
                "janus": "message", "handle_id": handle_id,
                "transaction": self._gen_id(),
                "body": {"request": "start", "room": room},
                "jsep": {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp}
            })
            logger.info(f"Subscribed to feed {feed_id}")
        except Exception as e:
            logger.error(f"Subscribe to feed {feed_id} failed: {e}")
            await self._remove_subscriber(feed_id)

    async def _remove_subscriber(self, feed_id):
        logger.info(f"Removing subscriber for feed {feed_id}")
        task = self._sub_tasks.pop(feed_id, None)
        if task:
            task.cancel()
            try: await task
            except: pass
        pc = self._sub_pcs.pop(feed_id, None)
        if pc:
            try: await pc.close()
            except: pass
        handle_id = self._sub_handles.pop(feed_id, None)
        if handle_id:
            try:
                await self.ws.send(json.dumps({
                    "janus": "detach", "session_id": self.session_id,
                    "handle_id": handle_id, "transaction": self._gen_id()
                }))
            except: pass

    async def _handle_remote_track(self, track, feed_id):
        frame_count = 0
        last_log = time.time()

        while self._running and not self._fatal_error:
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=5.0)
                frame_count += 1
                now = time.time()

                raw_bytes = bytes(frame.planes[0])
                arr = np.frombuffer(raw_bytes, dtype=np.int16).copy()

                n_channels = len(frame.layout.channels)
                if n_channels > 1:
                    arr = arr.reshape(-1, n_channels)
                    arr = ((arr[:, 0].astype(np.int32) + arr[:, 1].astype(np.int32)) // 2).astype(np.int16)

                if frame_count % 50 == 0 or (now - last_log > 5):
                    peak = np.max(np.abs(arr))
                    logger.info(f"Feed {feed_id} | frames={frame_count}, "
                                f"rate={frame.sample_rate}, peak={peak}/32767")
                    last_log = now

                await self.speaker.play_audio(arr.tobytes(), frame.sample_rate)

            except asyncio.TimeoutError:
                logger.warning(f"No audio from feed {feed_id} for 5s")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Remote track error [{feed_id}]: {e}")
                break

    async def close(self):
        self._running = False
        if self._keepalive_task: self._keepalive_task.cancel()
        if self._health_task: self._health_task.cancel()

        for feed_id in list(self._sub_handles.keys()):
            await self._remove_subscriber(feed_id)

        if self.speaker: self.speaker.close()
        if self.mic_track: self.mic_track.stop()
        if self.video_track: self.video_track.stop()
        if self.pub_pc: await self.pub_pc.close()
        if self._dispatcher_task: self._dispatcher_task.cancel()

        if self.pub_handle_id:
            try:
                await self.ws.send(json.dumps({
                    "janus": "detach", "session_id": self.session_id,
                    "handle_id": self.pub_handle_id, "transaction": self._gen_id()
                }))
            except: pass
        if self.session_id:
            try:
                await self.ws.send(json.dumps({
                    "janus": "destroy", "session_id": self.session_id,
                    "transaction": self._gen_id()
                }))
            except: pass
        if self.ws: await self.ws.close()

    def is_fatal(self): return self._fatal_error is not None

# ── HA webhook helpers ─────────────────────────────────────────────────────
def notify_janus_down():
    """Alert HA that the Janus server is unreachable."""
    try:
        x = requests.post(WEBHOOK_DOORBELL_ISSUES, json={
            'login': LOGIN,
            'issue': 'Janus server unreachable'
        }, headers={'Content-type': 'application/json', 'Accept': 'text/plain'}, timeout=5)
        logger.info(f"Janus-down webhook response: {x.status_code}")
    except Exception as e:
        logger.error(f"Janus-down webhook failed: {e}")

# ── Button / keyboard callback ─────────────────────────────────────────────
def button_pressed_callback(channel):
    expiry = int(time.time()) + 600  # 10 minutes
    raw = ''.join(random.choices(string.ascii_letters + string.digits, k=24))
    token = f"{raw}.{expiry}"
    janus_instance._door_token = token
    janus_instance._door_token_expiry = expiry
    logger.info(f"Button pressed! Token: {token}")

    # Play chime on Pi speaker
    if janus_instance and janus_instance.speaker:
        threading.Thread(target=janus_instance.speaker.play_chime, daemon=True).start()

    try:
        x = requests.post(WEBHOOK_DOORBELL_IS_RINGING, json={
            'login': LOGIN,
            'url': SERVER_URL,
            'room': ROOM,
            'token': token
        }, headers={'Content-type': 'application/json', 'Accept': 'text/plain'})
        logger.info(f"Webhook response: {x.status_code}")
    except Exception as e:
        logger.error(f"Webhook failed: {e}")

    # Send chime + token to browser via datachannel
    asyncio.run_coroutine_threadsafe(
        janus_instance.send_datachannel({
            "type": "doorbell",
            "action": "chime",
            "token": token
        }),
        main_loop
    )

# ── Keyboard listener (simulates GPIO button) ──────────────────────────────
def start_keyboard_listener():
    def listen():
        logger.info("⌨️  Press ENTER to simulate doorbell button press")
        while True:
            input()
            for _ in range(10):
                if janus_instance is None:
                    logger.warning("Janus is None, waiting 1s...")
                elif janus_instance.is_fatal():
                    logger.warning(f"Janus is fatal: {janus_instance._fatal_error}, waiting 1s...")
                else:
                    button_pressed_callback(None)
                    break
                time.sleep(1)
            else:
                logger.error("Janus unavailable after 10s, button press dropped")
    thread = threading.Thread(target=listen, daemon=True)
    thread.start()

# ── Main loop with reconnection ────────────────────────────────────────────
async def run_forever():
    global janus_instance

    def suppress_invalid_state(loop, context):
        if "invalid state" in str(context.get("message", "")).lower():
            return
        loop.default_exception_handler(context)

    asyncio.get_event_loop().set_exception_handler(suppress_invalid_state)

    start_keyboard_listener()

    # Track consecutive connection failures to throttle retries and notify HA
    consecutive_failures = 0
    RETRY_DELAYS = [5, 10, 30, 60]  # seconds — backs off as failures accumulate

    while True:
        janus_instance = JanusClient(JANUS_URL, mic_device=MIC_DEVICE_INDEX, speaker_device=SPEAKER_DEVICE_INDEX)
        try:
            if not await janus_instance.connect():
                raise Exception("Connect returned False")

            # Successful connection — reset failure counter
            consecutive_failures = 0

            await janus_instance.join_and_publish(ROOM, "doorbell")
            logger.info("=" * 60)
            logger.info("🎥 Doorbell running — Ctrl+C to stop")
            logger.info("=" * 60)
            while not janus_instance.is_fatal():
                await asyncio.sleep(1)
            logger.error(f"Fatal error: {janus_instance._fatal_error}")

        except Exception as e:
            consecutive_failures += 1
            is_connection_error = "Connect call failed" in str(e) or "Connection refused" in str(e)

            if is_connection_error:
                logger.warning(f"Janus unreachable (attempt {consecutive_failures}): {e}")
                # Notify HA on first failure and then every 5 failures
                if consecutive_failures == 1 or consecutive_failures % 5 == 0:
                    threading.Thread(target=notify_janus_down, daemon=True).start()
            else:
                logger.error(f"Exception: {e}")
                import traceback; traceback.print_exc()

        finally:
            try:
                await janus_instance.close()
            except: pass
            janus_instance = None

            # Back off retry delay based on consecutive failures
            delay = RETRY_DELAYS[min(consecutive_failures - 1, len(RETRY_DELAYS) - 1)]
            logger.info(f"Reconnecting in {delay}s... (failure #{consecutive_failures})")
            await asyncio.sleep(delay)

if __name__ == '__main__':
    try:
        main_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(main_loop)
        main_loop.run_until_complete(run_forever())
    except KeyboardInterrupt:
        logger.info("Exited by user")

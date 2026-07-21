#!/usr/bin/env python3
"""
Denon AVR-2113/1913 Remote Control Server
==========================================
TCP port 23 (Telnet) protocol controller with WebSocket frontend and SinRic Pro integration.
"""

import asyncio
import signal
import sys
import os
import time
import datetime
import aiohttp
import aiohttp.web
from sinricpro import SinricPro, SinricProConfig, SinricProTV

# =============================================================================
# CONFIGURATION - Modify these values as needed
# =============================================================================

# Connection Settings
AVR_IP = 'XXX.XXX.XXX.XXX'
AVR_PORT = 23
SERVER_HOST = '0.0.0.0'
SERVER_PORT = 8000

# SinricPro Configuration (for Alexa/Google Home integration)
SINRICPRO_DEVICE_ID = ""
SINRICPRO_APP_KEY = ""
SINRICPRO_APP_SECRET = ""

# Timing Configuration (seconds) - Backend only
COMMAND_DELAY_AFTER_PWON = 1.0    # Block commands for 1s after PWON
AVR_COMMAND_DELAY = 0.1          # Delay between commands (protects half-duplex)
AVR_RECONNECT_DELAY = 2.0        # Wait before retrying AVR connection
AVR_IDLE_TIMEOUT = 15.0          # Heartbeat interval

SINRIC_QUEUE_INTERVAL = 1.1      # Seconds between outbound Sinric events
SINRIC_KEEPALIVE_INTERVAL = 25   # Seconds between keepalive pings
NSE_POLL_INTERVAL = 2.0          # How often to query metadata
ALBUM_ART_FETCH_DELAY = 1.5      # Delay after track change before fetching art
SCHEDULED_SHUTDOWN_TIME = (3, 0) # 24h (hour, minute) for auto power-off

# =============================================================================
# INTERNAL STATE
# =============================================================================

connected_clients: set[aiohttp.web.WebSocketResponse] = set()
avr_reader: asyncio.StreamReader | None = None
avr_writer: asyncio.StreamWriter | None = None
avr_state_cache: dict[str, str] = {}
album_art_cache: bytes | None = None
http_session: aiohttp.ClientSession | None = None
metadata_update_event = asyncio.Event()
# Sources that support NSE metadata display and Album Art
NSE_SUPPORTED_SOURCES = {'NET', 'PANDORA', 'SIRIUSXM', 'SPOTIFY', 'LASTFM', 'FLICKR', 'FAVORITES', 'IRADIO', 'SERVER', 'USB/IPOD', 'USB', 'IPD', 'IRP', 'FVP', 'AIRPLAY', 'NETWORK'}

# Command queue for serialized writes with rate limiting
avr_write_lock = asyncio.Lock()
command_queue: asyncio.Queue[str] = asyncio.Queue()

# Global background task tracking for cleaner shutdown
background_tasks: set[asyncio.Task] = set()

# Track AVR connection state for new clients
avr_is_connected = False

# PWON gate: block commands after power-on
power_on_block_until: float = 0.0

# Graceful shutdown event
shutdown_event = asyncio.Event()

async def index(request):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.join(base_dir, 'frontend', 'index.html')
    if not os.path.exists(index_path):
        return aiohttp.web.Response(text="[FATAL ERROR] index.html not found in the 'frontend' folder.", status=404)
    return aiohttp.web.FileResponse(index_path)


async def album_art_handler(request):
    """Serve cached album art as JPEG."""
    if album_art_cache:
        return aiohttp.web.Response(body=album_art_cache, content_type='image/jpeg')
    else:
        return aiohttp.web.Response(status=404)


async def _process_command_queue():
    """Background task that processes commands from the queue with rate limiting."""
    global avr_writer, power_on_block_until

    while not shutdown_event.is_set():
        try:
            command = await asyncio.wait_for(command_queue.get(), timeout=1.0)

            # Check PWON gate
            now = asyncio.get_running_loop().time()
            if power_on_block_until > now:
                await asyncio.sleep(power_on_block_until - now)
            if command.strip() == 'PWON':
                power_on_block_until = asyncio.get_running_loop().time() + COMMAND_DELAY_AFTER_PWON

            payload = (command.strip() + '\r').encode('ascii', errors='strict')
            
            # Proactive Polling: If we send a media control command, schedule a metadata refresh
            if any(cmd in command for cmd in ['NS9D', 'NS9E', 'NS9A', 'NS9B', 'NS9C']):
                 asyncio.create_task(asyncio.sleep(0.5)).add_done_callback(lambda _: command_queue.put_nowait('NSE'))

            if len(payload) > 135:
                print(f"Command exceeds AVR protocol max length (135 bytes): {command}")
                continue

            async with avr_write_lock:
                if avr_writer and not avr_writer.is_closing():
                    try:
                        avr_writer.write(payload)
                        await avr_writer.drain()
                        # Inter-command delay to prevent half-duplex collisions
                        await asyncio.sleep(AVR_COMMAND_DELAY)  # Respect half-duplex protocol
                    except Exception as e:
                        print(f"Error writing to AVR: {e}")
                        try:
                            avr_writer.close()
                            await avr_writer.wait_closed()
                        except Exception:
                            pass

        except asyncio.TimeoutError:
            continue
        except Exception as e:
            print(f"Command queue error: {e}")
            await asyncio.sleep(0.1)


async def _send_to_avr(command: str) -> bool:
    """Send a command to the AVR via the queue. Returns True if queued successfully."""
    command = command.strip()
    if not command:
        return False

    if len(command) > 135:
        print(f"Command exceeds AVR protocol max length: {command}")
        return False

    await command_queue.put(command)
    return True


async def websocket_handler(request):
    global avr_is_connected
    # Enable a 30s heartbeat to keep connection alive and detect zombie clients
    ws = aiohttp.web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    connected_clients.add(ws)
    print(f"[WebSocket] Client connected. Total clients: {len(connected_clients)}")

    # Push all cached state to newly connecting client - use gather for concurrent send
    cached_messages = list(avr_state_cache.values())
    if cached_messages:
        # Send all cached messages concurrently
        async def send_cached(msg):
            try:
                if not ws.closed:
                    await ws.send_str(msg)
            except Exception:
                pass
        await asyncio.gather(*[send_cached(msg) for msg in cached_messages], return_exceptions=True)

    # Send current AVR connection status to new client
    try:
        if not ws.closed:
            status_msg = "__AVR_CONNECTED__" if avr_is_connected else "__AVR_DISCONNECTED__"
            await ws.send_str(status_msg)

            # Send current album art status if available
            if album_art_cache:
                await ws.send_str("__ALBUM_ART_UPDATED__")
    except Exception:
        pass

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await _send_to_avr(msg.data.strip())
    finally:
        connected_clients.discard(ws)
        print(f"[WebSocket] Client disconnected. Total clients: {len(connected_clients)}")
    return ws

def _clean_nse_text(raw: str, line_idx: int) -> str:
    """Clean NSE display text - strip attribute byte and all padding whitespace."""
    # Lines 1-8 have attribute byte (control char), strip it
    if line_idx > 0 and raw and ord(raw[0]) < 0x20:
        raw = raw[1:]
    # Strip null terminator
    null_pos = raw.find('\x00')
    if null_pos >= 0:
        raw = raw[:null_pos]
    # Strip all whitespace (handles spaces, tabs, non-breaking spaces, etc.)
    return raw.strip()


_CACHE_KEY_PREFIXES = (
    'MU', 'MS', 'SV', 'SD', 'DC', 'SLP', 'VSASP', 
    'VSSCH', 'VSSC', 'VSAUDIO', 'VSVPM',
    'TFAN', 'TPAN', 'TMAN',
    'PSTONE', 'PSMULTEQ', 'PSBAS', 'PSTRE',
    'PSDYNEQ', 'PSDYNVOL', 'PSREFLEV', 'PSDRC',
    'PSSWR', 'PSSB', 'PSCINEMA', 'PSFRONT', 'PSRSTR', 'PSRSZ', 'PSLOM',
    'SYREMOTE', 'SYPANEL'
)

def _cache_key(line):
    """Determine the cache key for an incoming AVR message."""
    if line == 'PWON' or line == 'PWSTANDBY': return 'PW'
    if line.startswith('SI'): return 'SI'
    if line.startswith('MVMAX'): return 'MVMAX'
    if line.startswith('MV') and line[2:3].isdigit(): return 'MV'
    if line.startswith('NSE'): return line[:4] if len(line) >= 4 else None
    if line.startswith('CV') and not line.endswith(('UP', 'DOWN')):
        idx = sum(1 for c in line[:6] if c.isalpha())
        if idx > 2 and any(c.isdigit() for c in line[idx:]):
            return line[:idx]
        return None

    for prefix in _CACHE_KEY_PREFIXES:
        if line.startswith(prefix):
            return prefix
    return None


async def _broadcast_to_clients(message: str):
    """Broadcast a message to all connected clients, handling broken connections."""
    dead_clients = []

    for client in list(connected_clients):
        if client.closed:
            dead_clients.append(client)
            continue

        try:
            await client.send_str(message)
        except Exception:
            dead_clients.append(client)

    # Clean up dead clients
    for client in dead_clients:
        connected_clients.discard(client)


async def avr_listener():
    global avr_reader, avr_writer, power_on_block_until, avr_is_connected

    while not shutdown_event.is_set():
        try:
            avr_reader, avr_writer = await asyncio.open_connection(AVR_IP, AVR_PORT)
            print(f"Connected to AVR at {AVR_IP}:{AVR_PORT}")

            # Update connection state
            avr_is_connected = True

            # Inform clients that AVR is connected
            await _broadcast_to_clients("__AVR_CONNECTED__")

            # Perform initial state sync - query all states once
            init_commands = [
                'PW?', 'MV?', 'MU?', 'SI?', 'MS?', 'SV?', 'SD?', 'DC?',
                'VSASP ?', 'VSSC ?', 'VSSCH ?', 'VSAUDIO ?', 'VSVPM ?',
                'PSTONE CTRL ?', 'SLP?', 'SYREMOTE LOCK ?',
                'PSDYNEQ ?', 'PSDYNVOL ?', 'PSREFLEV ?', 'PSMULTEQ: ?',
                'PSBAS ?', 'PSTRE ?', 'PSDRC ?', 'PSSWR ?',
                'PSSB: ?', 'PSCINEMA EQ. ?', 'PSFRONT?', 'PSRSTR ?', 'PSRSZ ?',
                'PSLOM ?', 'TFAN?', 'TPAN?', 'TMAN?', 'CV?', 'NSE'
            ]
            for cmd in init_commands:
                await _send_to_avr(cmd)

            try:
                while not shutdown_event.is_set():
                    try:
                        line = await asyncio.wait_for(avr_reader.readuntil(b'\r'), timeout=AVR_IDLE_TIMEOUT)
                        # Fix #1: Decode as UTF-8 with replacement for NSE (network display)
                        decoded_line = line.rstrip(b'\r').decode('utf-8', errors='replace')
                        if not decoded_line:
                            continue

                        # Cache and prepare message for broadcast (clean NSE lines to remove padding)
                        key = _cache_key(decoded_line)
                        broadcast_msg = decoded_line  # Default: broadcast original

                        if key is not None:
                            if decoded_line.startswith('NSE') and len(decoded_line) >= 4:
                                # Extract line index and clean the text
                                try:
                                    idx = int(decoded_line[3:4])
                                    cleaned = _clean_nse_text(decoded_line[4:], idx)
                                    # Cache with prefix for consistency, but ensure it's clean
                                    cleaned_msg = decoded_line[:4] + cleaned
                                    avr_state_cache[key] = broadcast_msg = cleaned_msg
                                except (ValueError, IndexError):
                                    avr_state_cache[key] = broadcast_msg = decoded_line
                            else:
                                avr_state_cache[key] = decoded_line
                                
                        # On Input change: Clear NSE cache if not a supported source
                        if decoded_line.startswith('SI'):
                            new_input = decoded_line[2:].strip()
                            if new_input not in NSE_SUPPORTED_SOURCES:
                                for i in range(9):
                                    avr_state_cache.pop(f'NSE{i}', None)
                                    await _broadcast_to_clients(f'NSE{i}')
                                global album_art_cache, album_art_track
                                album_art_cache = None
                                album_art_track = None
                                await _broadcast_to_clients('__ALBUM_ART_CLEARED__')

                        # Signal metadata refresh task that new data arrived
                        if decoded_line.startswith('NSE'):
                            metadata_update_event.set()

                        # Sync power state to all clients and SinricPro
                        if decoded_line in ('PWON', 'PWSTANDBY'):
                            await _sync_power_state(decoded_line)

                        # Sync Mute to SinricPro
                        if decoded_line in ('MUON', 'MUOFF'):
                            await _sync_mute_state(decoded_line == 'MUON')

                        # Sync volume to SinricPro (with echo prevention)
                        if decoded_line.startswith('MV') and not decoded_line.startswith('MVMAX'):
                            try:
                                vol = _parse_avr_volume_str(decoded_line[2:])
                                await _sync_volume_to_sinric(vol)
                            except Exception:
                                pass

                        # When MVMAX is received, re-sync current volume with new max
                        if decoded_line.startswith('MVMAX'):
                            try:
                                cached_mv = avr_state_cache.get('MV')
                                if cached_mv:
                                    vol = _parse_avr_volume_str(cached_mv[2:])
                                    await _sync_volume_to_sinric(vol)
                            except Exception:
                                pass

                        await _broadcast_to_clients(broadcast_msg)
                    except asyncio.TimeoutError:
                        if avr_writer and not avr_writer.is_closing():
                            await _send_to_avr('PW?')
            finally:
                # Properly close connection
                if avr_writer:
                    try:
                        avr_writer.close()
                        await avr_writer.wait_closed()
                    except Exception:
                        pass
                avr_reader = None
                avr_writer = None
                avr_is_connected = False
                # Connection lost - sync unknown state to all
                await _sync_power_state(None)
                await _broadcast_to_clients("__AVR_DISCONNECTED__")
                power_on_block_until = 0.0

        except Exception as e:
            print(f"AVR connection error: {e}")
            avr_is_connected = False
            # Force close any stale connections
            if avr_writer and not avr_writer.is_closing():
                try:
                    avr_writer.close()
                    await avr_writer.wait_closed()
                except Exception:
                    pass
            avr_reader = None
            avr_writer = None
            # Clear pending commands
            while not command_queue.empty():
                try:
                    command_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            # Connection lost - sync unknown state to all
            await _sync_power_state(None)
            await _broadcast_to_clients("__AVR_DISCONNECTED__")
            power_on_block_until = 0.0

            # Wait before retry (loop will automatically retry)
            for _ in range(int(AVR_RECONNECT_DELAY * 10)):
                await asyncio.sleep(0.1)
                if shutdown_event.is_set():
                    break


# =============================================================================
# SINRICPRO INTEGRATION (Alexa/Google Home)
# =============================================================================

sinric_pro: SinricPro | None = None
sinric_tv: SinricProTV | None = None
last_synced_power_state: str | None = 'INITIAL'  # Track state to prevent echo loop
last_synced_volume: float | None = None  # Track volume to prevent echo loop
sinric_latest_volume: float | None = None  # Track latest volume for throttle
sinric_volume_throttle_until: float = 0  # Timestamp for 1s debounce
sinric_throttle_task: asyncio.Task | None = None  # Task for delayed send

# SinricPro outbound queue - prevents rate limiting
sinric_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()  # (event_type, payload)
sinric_queue_task: asyncio.Task | None = None
last_sent_volume_percent: int | None = None  # Track last sent volume to skip duplicates


def _get_avr_max_volume() -> int:
    """Get AVR's max volume from cache, default to 98."""
    try:
        max_str = avr_state_cache.get('MVMAX')
        if max_str:
            return int(max_str[6:]) if len(max_str) > 6 else 98
    except Exception:
        pass
    return 98


def _parse_avr_volume_str(vol_str: str) -> float:
    """Parse AVR volume string - handles both 2-digit ('32') and 3-digit ('315' = 31.5)."""
    vol_str = vol_str.strip()
    if not vol_str:
        return 0
    if len(vol_str) == 3:
        # 3-digit format: first 2 digits + 0.5 (e.g., '315' = 31.5)
        return float(vol_str[:2]) + 0.5
    try:
        return float(vol_str)
    except ValueError:
        return 0


def _format_avr_volume(vol: float) -> str:
    """Format volume to Denon spec: 32.0 -> '32', 31.5 -> '315'."""
    if vol == int(vol):
        return f"{int(vol):02d}"
    # Half-step: 31.5 -> "315"
    return f"{int(vol):02d}5"


def _sinric_to_avr_volume(percent: int) -> float:
    """Convert Sinric volume (0-100) to AVR volume (0-MVMAX).
    Returns exact float to handle 0.5 steps."""
    max_vol = _get_avr_max_volume()
    result = percent * max_vol / 100
    return min(max_vol, max(0, result))


def _avr_to_sinric_volume(avr_vol: int | float) -> int:
    """Convert AVR volume to Sinric percentage (0-100).
    Rounds the final percentage."""
    max_vol = _get_avr_max_volume()
    if max_vol == 0:
        return 0
    # Round the final percentage
    return min(100, max(0, round(avr_vol * 100 / max_vol)))


async def sinric_on_power_state(state: bool) -> bool:
    global last_synced_power_state
    print(f"[SinricPro] Power: {'ON' if state else 'OFF'}")
    target = 'PWON' if state else 'PWSTANDBY'
    # Track state we're sending so when AVR echoes back, we don't echo to Sinric
    last_synced_power_state = target
    await _send_to_avr(target)
    return True


async def sinric_on_volume(volume: int) -> bool:
    global last_synced_volume
    print(f"[SinricPro] Volume set to: {volume}%")
    # Convert Sinric percentage (0-100) to AVR volume (0-MVMAX)
    avr_vol = _sinric_to_avr_volume(volume)
    # Track volume we're sending so when AVR echoes back, we skip
    last_synced_volume = avr_vol
    await _send_to_avr(f'MV{_format_avr_volume(avr_vol)}')
    return True


async def sinric_on_adjust_volume(volume_delta: int) -> bool:
    global last_synced_volume
    print(f"[SinricPro] Volume adjust: {'+' if volume_delta > 0 else ''}{volume_delta}")
    # Get current AVR volume from cache
    try:
        raw_vol_str = avr_state_cache.get('MV', 'MV00')[2:]
        current = _parse_avr_volume_str(raw_vol_str)
    except Exception:
        current = 50.0
    max_vol = _get_avr_max_volume()
    # Convert delta from Sinric percent points to AVR units (round to nearest 0.5)
    delta_avr = round((volume_delta * max_vol / 100.0) * 2) / 2
    new_vol = max(0.0, min(float(max_vol), current + delta_avr))
    # Track what we're sending
    last_synced_volume = new_vol
    await _send_to_avr(f'MV{_format_avr_volume(new_vol)}')
    return True


async def sinric_on_mute(mute: bool) -> bool:
    print(f"[SinricPro] Mute: {'ON' if mute else 'OFF'}")
    await _send_to_avr('MUON' if mute else 'MUOFF')
    return True


async def _sync_power_state(power_state: str | None):
    """Sync power state to all clients and SinricPro.

    power_state: 'PWON', 'PWSTANDBY', or None (disconnected/unknown)
    """
    global last_synced_power_state

    # Skip if we're shutting down
    if shutdown_event.is_set():
        return

    # Prevent echo: if AVR's response matches what we just sent to Sinric, skip it
    if power_state == last_synced_power_state:
        return

    # Update tracker to new actual state
    last_synced_power_state = power_state

    # Sync to SinricPro via queue (rate limited)
    if sinric_tv:
        try:
            if power_state == 'PWON':
                await _queue_sinric_event('power', True)
                print("[SinricPro] Power: ON (queued)")
            elif power_state == 'PWSTANDBY':
                await _queue_sinric_event('power', False)
                print("[SinricPro] Power: OFF (queued)")
            else:
                await _queue_sinric_event('power', False)
                print("[SinricPro] Power: unknown, reported as OFF (queued)")
        except Exception as e:
            print(f"[SinricPro] Power sync error: {e}")

async def _sync_mute_state(is_muted: bool):
    """Sync mute state to SinricPro."""
    if sinric_tv and not shutdown_event.is_set():
        try:
            # Queue it to respect the 1.1s rate limit
            await _queue_sinric_event('mute', is_muted)
            print(f"[SinricPro] Mute: {'ON' if is_muted else 'OFF'} (queued)")
        except Exception as e:
            print(f"[SinricPro] Mute sync error: {e}")


# Volume throttling uses sinric_queue (global 1.1s rate limit)
# plus local 1s debounce to coalesce rapid volume changes into single event


async def _sync_volume_to_sinric(avr_vol: float):
    """Sync volume to SinricPro with 1-second throttling."""
    global last_synced_volume, sinric_latest_volume, sinric_volume_throttle_until, sinric_throttle_task

    # Skip if we're shutting down
    if shutdown_event.is_set():
        return

    # Update the latest known volume so the flush task has the most recent data
    sinric_latest_volume = avr_vol
    now = asyncio.get_event_loop().time()

    # Echo prevention: if this update matches what we just sent to the AVR via Sinric, ignore it
    if last_synced_volume is not None and abs(avr_vol - last_synced_volume) < 0.01:
        last_synced_volume = None # Reset so manual changes immediately after are caught
        return

    async def delayed_flush():
        """Wait for the throttle window to expire, then send the final state."""
        global sinric_throttle_task
        try:
            while True:
                wait = sinric_volume_throttle_until - asyncio.get_event_loop().time()
                if wait <= 0:
                    break
                await asyncio.sleep(wait)
            
            if sinric_latest_volume is not None and not shutdown_event.is_set():
                percent = _avr_to_sinric_volume(sinric_latest_volume)
                await _queue_sinric_event('volume', percent)
        except asyncio.CancelledError:
            pass
        finally:
            sinric_throttle_task = None

    # If within throttle window, extend the window and return
    if now < sinric_volume_throttle_until:
        sinric_volume_throttle_until = now + 1.2  # Extend window (1.2s to be safe)
        if not sinric_throttle_task or sinric_throttle_task.done():
            sinric_throttle_task = asyncio.create_task(delayed_flush())
        return

    # Not in window: send the first update immediately and start the window
    sinric_volume_throttle_until = now + 1.0
    percent = _avr_to_sinric_volume(avr_vol)
    await _queue_sinric_event('volume', percent)


# =============================================================================
# SINRICPRO OUTBOUND QUEUE - Rate limiting to 1 event/second
# =============================================================================

async def _process_sinric_queue():
    """Background task that processes the SinricPro outbound queue with rate limiting."""
    last_send_time = 0.0

    while not shutdown_event.is_set():
        try:
            wait_time = 0.0
            # Wait for items in queue with timeout to check shutdown flag
            try:
                event_type, payload = await asyncio.wait_for(sinric_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            # Skip if we're shutting down
            if shutdown_event.is_set():
                break

            now = asyncio.get_running_loop().time()
            wait_time = SINRIC_QUEUE_INTERVAL - (now - last_send_time)

            if wait_time > 0:
                await asyncio.sleep(wait_time)

            # Skip if we're shutting down
            if shutdown_event.is_set():
                break

            # Actually send to SinricPro
            global last_sent_volume_percent
            if sinric_pro and sinric_tv:
                try:
                    if event_type == 'power':
                        await sinric_tv.send_power_state_event(payload)
                        print(f"[SinricPro] Queued power: {payload}")
                    elif event_type == 'mute':
                        await sinric_tv.send_mute_event(payload)
                    elif event_type == 'volume':
                        # Update tracking BEFORE skipping to handle the echo case
                        # (we want to track what SHOULD have been sent)
                        if last_sent_volume_percent is None:
                            will_send = True
                        else:
                            will_send = (payload != last_sent_volume_percent)

                        if will_send:
                            await sinric_tv.send_volume_event(payload)
                            last_sent_volume_percent = payload
                            print(f"[SinricPro] Queued volume: {payload}")
                        else:
                            print(f"[SinricPro] Volume skipped (same as last): {payload}%")
                except Exception as e:
                    print(f"[SinricPro] Queue send error: {e}")

            last_send_time = asyncio.get_running_loop().time()

        except Exception as e:
            print(f"[SinricPro] Queue error: {e}")
            await asyncio.sleep(0.1)


async def _queue_sinric_event(event_type: str, payload: bool | int):
    """Queue an event to be sent to SinricPro with rate limiting."""
    await sinric_queue.put((event_type, payload))


async def sinric_on_media_control(control: str) -> bool:
    """Handle media control from SinricPro."""
    global last_synced_power_state
    
    # GOOGLE VOICE BUG FIX: Ignore macro media commands if we are shutting down
    if last_synced_power_state == 'PWSTANDBY':
        print(f"[SinricPro] Ignoring '{control}' command because AVR is shutting down")
        return True

    print(f"[SinricPro] Media: {control}")
    commands = {
        "Previous": "NS9E",
        "Next": "NS9D",
        "Play": "NS9A",
        "Pause": "NS9B",
    }
    command = commands.get(control)
    if command:
        print(f"[SinricPro] Sending AVR command: {command}")
        result = await _send_to_avr(command)
        print(f"[SinricPro] Command queued: {result}")
        return result
    return False


async def sinric_on_select_input(input_name: str) -> bool:
    """Handle input selection from SinricPro - maps 'VIDEO' etc. to DVD input."""
    print(f"[SinricPro] Input select: {input_name}")
    if input_name.upper() in {"VIDEO", "TV", "BEAMER", "PROJEKTOR", "PROJECTOR"}:
        await _send_to_avr("SIDVD")
        return True
    # Ignore other input selections
    return False


# =============================================================================
# NSE DISPLAY AUTO-REFRESH & ALBUM ART
# =============================================================================

album_art_track: str | None = None  # Track title for cache invalidation
_album_art_fetching: bool = False  # Lock to prevent concurrent fetches

def _get_current_track_fingerprint() -> str | None:
    """Build a unique identifier for the current track based on metadata lines."""
    # Include SI (Source Input) to ensure switches between sources are caught
    # even if metadata strings are temporarily identical or empty.
    si = avr_state_cache.get('SI', '').replace('SI', '')
    parts = [si] if si else []
    
    # Use lines 1, 2, and 4 (Title, Artist, Album)
    for i in [1, 2, 4]:
        val = avr_state_cache.get(f'NSE{i}', '')
        if len(val) > 4:
            parts.append(val[4:].strip())
    
    # If we only have the source name and no metadata, it's not a valid track fingerprint
    if len(parts) <= 1: return None

    fingerprint = " - ".join(parts).strip()
    return fingerprint if fingerprint and "loading" not in fingerprint.lower() else None

async def _fetch_album_art() -> bytes | None:
    """Fetch album art from AVR. Returns JPEG data or None."""
    global http_session
    url = f"http://{AVR_IP}/NetAudio/art.asp-jpg?t={int(time.time() * 1000)}"
    try:
        async with http_session.get(url, timeout=5.0) as response:
            if response.status == 200:
                data = await response.read()
                if len(data) > 100: return data
        return None
    except Exception as e:
        print(f"[Album Art] Fetch failed: {e}")
        return None

async def _run_album_art_fetch(track_fingerprint: str):
    """Background task to fetch art with retries without blocking metadata polling."""
    global album_art_cache, album_art_track, _album_art_fetching
    _album_art_fetching = True
    try:
        # Wait for AVR to finalize the art file after track/source change
        await asyncio.sleep(ALBUM_ART_FETCH_DELAY)

        for attempt in range(1, 11): # 10 attempts (approx 20s total)
            if shutdown_event.is_set(): break
            
            # Check if track changed again while we were waiting/retrying
            if _get_current_track_fingerprint() != track_fingerprint:
                return
            
            data = await _fetch_album_art()
            if data:
                album_art_cache = data
                await _broadcast_to_clients('__ALBUM_ART_UPDATED__')
                return
            
            await asyncio.sleep(2.0)

        # Definitive failure: Clear the cache so UI doesn't show stale art
        if album_art_cache is not None:
            album_art_cache = None
            await _broadcast_to_clients('__ALBUM_ART_CLEARED__')
        
    finally:
        _album_art_fetching = False

async def _nse_auto_refresh():
    """Task focused purely on metadata polling and change detection."""
    global album_art_track
    last_poll_time = 0
    track_none_count = 0  # Counter for missing metadata cycles
    
    while not shutdown_event.is_set():
        try:
            current_input = avr_state_cache.get('SI', '').replace('SI', '')
            is_on = avr_state_cache.get('PW') == 'PWON'

            if avr_is_connected and is_on and current_input in NSE_SUPPORTED_SOURCES:
                now = time.time()
                if now - last_poll_time >= NSE_POLL_INTERVAL:
                    await _send_to_avr('NSE')
                    last_poll_time = now
                
                nse0 = avr_state_cache.get('NSE0', '')
                # Be more inclusive with keywords to catch various sources
                is_playing_state = any(kw in nse0.lower() for kw in ["now playing", "playing", "streaming", "spotify", "airplay", "net", "usb"])
                current_track = _get_current_track_fingerprint() if is_playing_state else None

                if current_track:
                    track_none_count = 0 # Reset stability counter
                    if current_track != album_art_track and not _album_art_fetching:
                        # Lock the fingerprint immediately and trigger fetcher
                        album_art_track = current_track
                        fetch_task = asyncio.create_task(_run_album_art_fetch(current_track))
                        background_tasks.add(fetch_task)
                        fetch_task.add_done_callback(background_tasks.discard)
                elif not current_track and album_art_track:
                    # Metadata jitter protection: require 3 consecutive failed polls before clearing
                    track_none_count += 1
                    if track_none_count >= 3:
                        album_art_track = None
                        global album_art_cache
                        album_art_cache = None
                        await _broadcast_to_clients('__ALBUM_ART_CLEARED__')
            
            # Wait for either a metadata update or a periodic timeout
            try:
                await asyncio.wait_for(metadata_update_event.wait(), timeout=1.0)
                metadata_update_event.clear()
            except asyncio.TimeoutError:
                pass
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[NSE Refresh] Error: {e}")
            await asyncio.sleep(2.0)


# =============================================================================
# AUTOMATED SHUTDOWN SCHEDULER
# =============================================================================

async def _scheduled_power_off():
    """Background task that automatically powers off the AVR at 3:00 AM."""
    hour, minute = SCHEDULED_SHUTDOWN_TIME

    while not shutdown_event.is_set():
        try:
            now = datetime.datetime.now()
            target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            # If we've passed 3am today, schedule for tomorrow
            if now >= target_time:
                target_time = target_time + datetime.timedelta(days=1)

            # Calculate seconds until target
            wait_seconds = (target_time - now).total_seconds()

            print(f"[Scheduler] Next auto power-off scheduled for {target_time.strftime('%Y-%m-%d %H:%M')}")

            # Sleep in chunks to allow for shutdown event checking
            while wait_seconds > 0 and not shutdown_event.is_set():
                sleep_chunk = min(wait_seconds, 3600) # Sleep max 1 hour at a time
                await asyncio.sleep(sleep_chunk)
                wait_seconds -= sleep_chunk

            if shutdown_event.is_set():
                return

            # Check if AVR is on before sending power off
            power_state = avr_state_cache.get('PW', '')
            if power_state == 'PWON':
                print("[Scheduler] 3:00 AM - Auto powering off AVR")
                await _send_to_avr('PWSTANDBY')
            else:
                print("[Scheduler] 3:00 AM - AVR already in standby, skipping")

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Scheduler] Error in power-off task: {e}")
            await asyncio.sleep(60)


async def _start_sinric_pro():
    global sinric_pro, sinric_tv
    sinric_pro = SinricPro.get_instance()
    sinric_tv = SinricProTV(SINRICPRO_DEVICE_ID)
    sinric_tv.on_power_state(sinric_on_power_state)
    sinric_tv.on_volume(sinric_on_volume)
    sinric_tv.on_adjust_volume(sinric_on_adjust_volume)
    sinric_tv.on_mute(sinric_on_mute)
    sinric_tv.on_media_control(sinric_on_media_control)
    sinric_tv.on_select_input(sinric_on_select_input)
    sinric_pro.add(sinric_tv)
    try:
        await sinric_pro.begin(SinricProConfig(app_key=SINRICPRO_APP_KEY, app_secret=SINRICPRO_APP_SECRET))
        print("[SinricPro] Connected!")

        # Start background task to keep connection alive with periodic events
        async def keepalive_task():
            while sinric_pro and sinric_tv and not shutdown_event.is_set():
                await asyncio.sleep(SINRIC_KEEPALIVE_INTERVAL)
                # Check shutdown before sending
                if shutdown_event.is_set():
                    break
                try:
                    # Get current power state from cache and send it back - no actual effect
                    # Queue it instead of direct send to prevent rate limiting
                    power_str = avr_state_cache.get('PW', 'PWSTANDBY')
                    is_on = power_str == 'PWON'
                    await _queue_sinric_event('power', is_on)
                    print("[SinricPro] Keepalive ping sent")
                except Exception as e:
                    # Connection likely dead, exit gracefully
                    print(f"[SinricPro] Keepalive error: {e}")
                    break

        ka_task = asyncio.create_task(keepalive_task())
        background_tasks.add(ka_task)
        ka_task.add_done_callback(background_tasks.discard)

        # Sync initial power state from cache (uses None/OFF if unknown)
        await _sync_power_state(avr_state_cache.get('PW'))
        # Query MVMAX to get actual max volume for Sinric conversions
        await _send_to_avr('MVMAX?')
    except Exception as e:
        print(f"[SinricPro] Error: {e}")


async def start_app():
    @aiohttp.web.middleware
    async def cache_and_header_middleware(request, handler):
        response = await handler(request)

        path = request.path

        # 1. Static Assets (JS, CSS, Images in /frontend/): Cache aggressively
        if path.startswith('/frontend/') and not path.endswith('.html'):
            response.headers['Cache-Control'] = 'public, max-age=86400, immutable'

        # 2. HTML Entry Point (index.html or root '/'): Always revalidate
        elif path in ('/', '/index.html') or path.endswith('.html'):
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'
            response.headers['Pragma'] = 'no-cache'

        # 3. Dynamic Album Art: Revalidate so art updates instantly on track change
        elif path == '/album-art':
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'

        return response

    app = aiohttp.web.Application(
        middlewares=[cache_and_header_middleware]
    )
    
    global http_session
    http_session = aiohttp.ClientSession()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    frontend_dir = os.path.join(base_dir, 'frontend')

    # Routes
    app.router.add_get('/', index)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/album-art', album_art_handler)
    
    # Static files handler (Handles 304 Not Modified automatically)
    if os.path.isdir(frontend_dir):
        app.router.add_static('/frontend/', path=frontend_dir, name='frontend')
    else:
        print(
            "\n[FATAL ERROR] 'frontend' directory not found!\n"
            "[FATAL ERROR] Please create the 'frontend' folder and add your HTML/JS files.\n"
            "[FATAL ERROR] Exiting script...\n"
        )
        sys.exit(1)

    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, SERVER_HOST, SERVER_PORT)

    def handle_signal(sig_num, frame):
        print(f"Received signal {sig_num}, initiating shutdown...")
        shutdown_event.set()
        # Batch cancel all tracked background tasks
        if http_session:
            asyncio.create_task(http_session.close())
        for task in background_tasks:
            if not task.done():
                task.cancel()

        # Try to stop SinricPro properly
        global sinric_pro
        if sinric_pro:
            try:
                # Schedule stop in the event loop
                loop = asyncio.get_event_loop()
                if not loop.is_closed():
                    loop.create_task(sinric_pro.stop())
            except Exception:
                pass

    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except NotImplementedError:
        pass  # Windows doesn't support SIGTERM in Python

    # Track background tasks for unified management
    background_tasks.add(asyncio.create_task(_process_command_queue()))
    background_tasks.add(asyncio.create_task(_start_sinric_pro()))
    background_tasks.add(asyncio.create_task(_scheduled_power_off()))
    background_tasks.add(asyncio.create_task(_process_sinric_queue()))
    background_tasks.add(asyncio.create_task(_nse_auto_refresh()))

    await asyncio.gather(
        site.start(),
        avr_listener()
    )


if __name__ == '__main__':
    try:
        asyncio.run(start_app())
    finally:
        print("Server stopped.")

const { createApp, ref, onMounted, computed, watch, nextTick } = Vue;

// Frontend configuration - used for volume debouncing
const CONFIG = {
    VOLUME_DEBOUNCE_DELAY: 200,
    SLIDER_DEBOUNCE_DELAY: 100,
    AVR_UPDATE_SUPPRESS_DELAY: 300,
    RECONCILE_DELAY: 1000,
    BOOT_GATE_DURATION: 2000,
    CHANNEL_VOL_MIN: 38,
    CHANNEL_VOL_MAX: 62,
    TONE_CONTROL_MIN: 44,
    TONE_CONTROL_MAX: 56,
    MAX_LOG_ENTRIES: 200
};

const INPUTS = ['CD', 'TUNER', 'DVD', 'BD', 'TV', 'SAT/CBL', 'MPLAY', 'GAME', 'AUX1', 'NET', 'PANDORA', 'SIRIUSXM', 'SPOTIFY', 'LASTFM', 'FLICKR', 'FAVORITES', 'IRADIO', 'SERVER', 'USB/IPOD', 'USB', 'IPD', 'IRP', 'FVP'];
const MODES = ['MOVIE', 'MUSIC', 'GAME', 'DIRECT', 'PURE DIRECT', 'STEREO', 'STANDARD', 'DOLBY DIGITAL', 'DTS SURROUND', 'MCH STEREO', 'ROCK ARENA', 'JAZZ CLUB', 'MONO MOVIE', 'MATRIX', 'VIDEO GAME', 'VIRTUAL', 'MULTI CH IN'];
const CHANNELS = ['FL', 'FR', 'C', 'SW', 'SL', 'SR', 'SBL', 'SBR', 'FHL', 'FHR', 'SB'];
const CHANNEL_NAMES = {
    FL: 'Front Left',
    FR: 'Front Right',
    C: 'Center',
    SW: 'Subwoofer',
    SL: 'Surround Left',
    SR: 'Surround Right',
    SBL: 'Surround Back L',
    SBR: 'Surround Back R',
    SB: 'Surround Back',
    FHL: 'Front Height L',
    FHR: 'Front Height R'
};
const VIDEO_SELECTS = ['DVD', 'BD', 'TV', 'SAT/CBL', 'MPLAY', 'GAME', 'AUX1', 'CD', 'SOURCE'];
const SIGNAL_DECODES = ['AUTO', 'HDMI', 'DIGITAL', 'ANALOG'];
const DIGITAL_DECODES = ['AUTO', 'PCM', 'DTS'];
const RESOLUTIONS = ['48P', '10I', '72P', '10P', '10P24', '4K', 'AUTO'];
const MULTEQ_MODES = ['AUDYSSEY', 'BYP.LR', 'FLAT', 'MANUAL', 'OFF'];
const DRC_MODES = ['AUTO', 'LOW', 'MID', 'HI', 'OFF'];
const SB_MODES = ['MTRX ON', 'PL2X CINEMA', 'PL2X MUSIC', 'ON', 'OFF'];
const FRONT_SP_MODES = ['SPA', 'SPB', 'A+B'];
const AUDIO_RESTORER_MODES = ['OFF', 'MODE1', 'MODE2', 'MODE3'];
const ROOM_SIZES = ['S', 'MS', 'M', 'ML', 'L'];

const parseVal = (str) => {
    str = str.trim();
    if (str.length === 3) return parseInt(str.substring(0, 2), 10) + 0.5;
    return parseInt(str, 10);
};

const formatVal = (num) => {
    if (Number.isInteger(num)) return num.toString().padStart(2, '0');
    return Math.floor(num).toString().padStart(2, '0') + '5';
};

const formatTime = () => {
    return new Date().toLocaleTimeString('en-GB', { hour12: false }) + '.' +
        new Date().getMilliseconds().toString().padStart(3, '0');
};

createApp({
    setup() {
        const ws = ref(null);
        const connectionStatus = ref('CONNECTING');
        const avrConnected = ref(false);
        const rawCommand = ref('');
        const manualFreq = ref('');
        const freqError = ref('');
        const presetRecall = ref('');
        const presetSave = ref('');
        const nsdQuery = ref('');

        const savedTerminalState = localStorage.getItem('avr_terminal_enabled');
        const terminalEnabled = ref(savedTerminalState === 'true');

        const isScrolledToBottom = ref(true);
        const debugLog = ref([]);
        const showAllChannels = ref(false);
        const albumArtUrl = ref(null);
        const isBooting = ref(false);
        let bootGateTimeout = null;
        let reconnectTimer = null;

        const tabs = ['Main Control', 'Audio & Channels', 'Tuner & Network', 'Video & Signal', 'Advanced'];
        const savedTab = localStorage.getItem('avr_active_tab');
        const activeTab = ref(tabs.includes(savedTab) ? savedTab : 'Main Control');

        const scrollActiveTabIntoView = (behavior = 'auto') => {
            nextTick(() => {
                const container = document.getElementById('tab-container');
                if (!container) return;
                // Find the button that matches the current activeTab value
                const buttons = Array.from(container.querySelectorAll('button'));
                const activeBtn = buttons.find(btn => btn.textContent.trim() === activeTab.value);

                if (activeBtn) {
                    activeBtn.scrollIntoView({ behavior, block: 'nearest', inline: 'center' });
                }
            });
        };

        watch(activeTab, (newTab) => {
            localStorage.setItem('avr_active_tab', newTab);
            scrollActiveTabIntoView('smooth');
        });

        const resetState = () => {
            state.value.nseLines = Array(9).fill('');
            state.value.input = state.value.input; // Keep input but clear metadata
            albumArtUrl.value = null;
        };

        const state = ref({
            power: '',
            volume: 0, maxVolume: 98, mute: '', sleep: 'OFF',
            input: '', surround: '',
            videoSelect: '', aspect: '', resolution: '', hdmiResolution: '',
            vsAudio: '', vsVpm: '',
            signalDecode: '', digitalDecode: '',
            toneCtrl: '', bass: 50, treble: 50, multeq: '',
            dyneq: '', dynvol: '', reflev: '',
            drc: '', swr: '', sbMode: '', cinemaEq: '', lom: '',
            frontSp: '', audioRestorer: '', roomSize: '',
            channelVols: { FL: null, FR: null, C: null, SW: null, SL: null, SR: null, SBL: null, SBR: null, FHL: null, FHR: null, SB: null },
            remoteLock: 'OFF', panelLock: 'OFF',
            tunerFreq: '', tunerPreset: '', tunerMode: '',
            nseLines: Array(9).fill('')
        });

        // Reset metadata/art immediately on source or power changes
        watch(() => state.value.input, (newVal) => {
            if (!INPUTS.slice(9).includes(newVal) && newVal !== 'NETWORK') {
                state.value.nseLines = Array(9).fill('');
                albumArtUrl.value = null;
            }
        });
        watch(() => state.value.power, (newVal) => {
            if (newVal !== 'ON') resetState();
        });

        // Unified Volume Manager Factory (handles Master Volume & Channel Volumes)
        const createVolumeManager = (getUiVal, setUiVal, getMin, getMax, getStepCmds, getAbsCmd) => {
            let trueVal = null;
            let startVol = null;
            let pendingDelta = 0;
            let sendTimer = null;
            let reconcileTimer = null;
            let isSuppressed = false;

            const handleIncoming = (newVal) => {
                trueVal = newVal;
                if (!isSuppressed) {
                    setUiVal(newVal);
                }
            };

            const resetReconcile = () => {
                clearTimeout(reconcileTimer);
                reconcileTimer = setTimeout(() => {
                    isSuppressed = false;
                    if (trueVal !== null && getUiVal() !== trueVal) {
                        setUiVal(trueVal);
                    }
                }, CONFIG.RECONCILE_DELAY);
            };

            const adjust = (delta) => {
                const wasSuppressed = isSuppressed;
                isSuppressed = true;
                if (pendingDelta === 0) {
                    const currentUi = getUiVal();
                    // Anchor to the current UI value if we are already interacting (suppressed).
                    // This prevents "jump backs" when starting a new batch of clicks before 
                    // the AVR has confirmed the previous batch.
                    startVol = (wasSuppressed && currentUi !== null) ? currentUi : (trueVal !== null ? trueVal : (currentUi || getMin()));
                }
                pendingDelta += delta;

                const minVal = getMin();
                const maxVal = getMax();

                // Optimistic UI update using the starting volume as the anchor
                setUiVal(Math.max(minVal, Math.min(maxVal, startVol + pendingDelta)));

                clearTimeout(sendTimer);
                sendTimer = setTimeout(() => {
                    const pressCount = Math.abs(pendingDelta) / 0.5;
                    const stepCmds = getStepCmds();

                    // Use step commands ONLY for a single tap (0.5 change)
                    if (stepCmds && pressCount === 1) {
                        send(pendingDelta > 0 ? stepCmds.up : stepCmds.down);
                    } else {
                        const targetVol = Math.max(minVal, Math.min(maxVal, startVol + pendingDelta));
                        send(getAbsCmd(targetVol));
                    }
                    pendingDelta = 0;
                    startVol = null;
                }, CONFIG.VOLUME_DEBOUNCE_DELAY);

                resetReconcile();
            };

            const drag = () => {
                isSuppressed = true;
                clearTimeout(sendTimer);
                sendTimer = setTimeout(() => {
                    send(getAbsCmd(getUiVal()));
                }, CONFIG.SLIDER_DEBOUNCE_DELAY);

                resetReconcile();
            };

            return { handleIncoming, adjust, drag };
        };

        const mvManager = createVolumeManager(
            () => state.value.volume,
            (v) => { state.value.volume = v; },
            () => 0,
            () => state.value.maxVolume,
            () => ({ up: 'MVUP', down: 'MVDOWN' }),
            (v) => 'MV' + formatVal(v)
        );

        const cvManagers = {};
        CHANNELS.forEach(ch => {
            cvManagers[ch] = createVolumeManager(
                () => state.value.channelVols[ch],
                (v) => { state.value.channelVols[ch] = v; },
                () => CONFIG.CHANNEL_VOL_MIN,
                () => CONFIG.CHANNEL_VOL_MAX,
                () => ({ up: `CV${ch} UP`, down: `CV${ch} DOWN` }),
                (v) => `CV${ch} ${formatVal(v)}`
            );
        });

        const formattedTunerFreq = computed(() => {
            let fStr = state.value.tunerFreq;
            if (!fStr) return '---.-- MHz';
            let match = fStr.match(/\d+/);
            if (match) {
                let f = parseInt(match[0], 10) / 100;
                return f.toFixed(2) + ' MHz';
            }
            return '---.-- MHz';
        });

        const setTunerFreq = () => {
            const f = parseFloat(manualFreq.value);
            if (isNaN(f)) { freqError.value = 'Enter a number'; return; }
            if (f < 87.5 || f > 108.0) { freqError.value = 'FM range: 87.5 – 108.0 MHz'; return; }
            const rounded = Math.round(f * 10) / 10;
            const freqStr = Math.round(rounded * 100).toString().padStart(6, '0');
            send('TFAN' + freqStr);
            manualFreq.value = '';
            freqError.value = '';
        };

        const recallPreset = () => {
            const n = parseInt(presetRecall.value, 10);
            if (isNaN(n) || n < 1 || n > 56) return;
            send('TPAN' + n.toString().padStart(2, '0'));
            presetRecall.value = '';
        };

        const savePreset = () => {
            const n = parseInt(presetSave.value, 10);
            if (isNaN(n) || n < 1 || n > 56) return;
            send('TPANMEM' + n.toString().padStart(2, '0'));
            presetSave.value = '';
        };

        const scrollToBottom = () => {
            nextTick(() => {
                const el = document.getElementById('terminal-logs');
                if (el) el.scrollTop = el.scrollHeight;
            });
        };

        const onTerminalScroll = (e) => {
            const el = e.target;
            // Use a tighter 2px threshold.
            // If distance from bottom is less than 2px, we consider it "at bottom".
            const distFromBottom = el.scrollHeight - el.clientHeight - el.scrollTop;
            isScrolledToBottom.value = distFromBottom < 2;
        };

        watch(terminalEnabled, (newVal) => {
            localStorage.setItem('avr_terminal_enabled', newVal);
            if (newVal) {
                scrollToBottom();
            }
        });

        // Computed property for UI enabled state - checks all conditions
        const uiEnabled = computed(() => {
            // Must be connected to WebSocket server
            if (connectionStatus.value !== 'CONNECTED') return false;
            // Must be connected to AVR
            if (!avrConnected.value) return false;
            // Must not be in standby
            if (state.value.power === 'STANDBY') return false;
            // Must not be in PWON boot window (give AVR time to boot)
            if (isBooting.value) return false;
            return true;
        });

        // Computed property for disable reason (for overlay message)
        const disableReason = computed(() => {
            if (connectionStatus.value !== 'CONNECTED') {
                return { icon: 'disconnect', text: 'Connecting to Server...' };
            }
            if (!avrConnected.value) {
                return { icon: 'disconnect', text: 'Connecting to AVR...' };
            }
            if (state.value.power === 'STANDBY') {
                return { icon: 'standby', text: 'Standby' };
            }
            if (isBooting.value) {
                return { icon: 'disconnect', text: 'Booting...' };
            }
            return null;
        });

        // Watch power state for PWON gate - trigger on POWER ON from STANDBY (not from initial empty state)
        watch(() => state.value.power, (newVal, oldVal) => {
            // Trigger boot gate ONLY when transitioning from explicit STANDBY to ON
            // Don't trigger when oldVal is '' (initial state on page load)
            if (newVal === 'ON' && oldVal === 'STANDBY') {
                isBooting.value = true;
                // Auto-clear after 2 seconds
                clearTimeout(bootGateTimeout);
                bootGateTimeout = setTimeout(() => {
                    isBooting.value = false;
                }, CONFIG.BOOT_GATE_DURATION);
            }
        });

        const connectWs = () => {
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }

            if (ws.value) {
                ws.value.onopen = ws.value.onmessage = ws.value.onclose = ws.value.onerror = null;
                if (ws.value.readyState !== WebSocket.CLOSED) ws.value.close();
            }

            connectionStatus.value = (connectionStatus.value === 'CONNECTED' || connectionStatus.value === 'RECONNECTING') ? 'RECONNECTING' : 'CONNECTING';

            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws.value = new WebSocket(`${protocol}//${window.location.host}/ws`);

            ws.value.onopen = () => {
                console.log('[WebSocket] Connected');
                connectionStatus.value = 'CONNECTED';
            };

            ws.value.onmessage = (event) => {
                const msg = event.data.trim();
                if (!msg) return;

                // Handle backend status messages
                if (msg === '__AVR_CONNECTED__') {
                    avrConnected.value = true;
                    return;
                }
                if (msg === '__AVR_DISCONNECTED__') {
                    avrConnected.value = false;
                    return;
                }
                if (msg === '__ALBUM_ART_UPDATED__') {
                    // Album art changed, reload with cache-busting timestamp
                    albumArtUrl.value = `/album-art?t=${Date.now()}`;
                    return;
                }
                if (msg === '__ALBUM_ART_CLEARED__') {
                    albumArtUrl.value = null;
                    return;
                }

                let isHb = false;
                // Heartbeat: AVR responds to PW? with PWON or PWSTANDBY
                if (msg === 'PWON' || msg === 'PWSTANDBY') {
                    isHb = true;
                }

                const timeStr = formatTime();

                debugLog.value.push({ time: timeStr, msg: msg, isTx: false, isHb: isHb });
                if (debugLog.value.length > CONFIG.MAX_LOG_ENTRIES) debugLog.value.shift();

                if (terminalEnabled.value && isScrolledToBottom.value) {
                    scrollToBottom();
                }

                if (msg === 'PWON') state.value.power = 'ON';
                else if (msg === 'PWSTANDBY') state.value.power = 'STANDBY';
                else if (msg === 'MUON') state.value.mute = 'ON';
                else if (msg === 'MUOFF') state.value.mute = 'OFF';
                else if (msg === 'SYREMOTE LOCK ON') state.value.remoteLock = 'ON';
                else if (msg === 'SYREMOTE LOCK OFF') state.value.remoteLock = 'OFF';
                else if (msg === 'SYPANEL LOCK ON') state.value.panelLock = 'PANEL';
                else if (msg === 'SYPANEL+V LOCK ON') state.value.panelLock = 'PANEL+V';
                else if (msg === 'SYPANEL LOCK OFF') state.value.panelLock = 'OFF';
                else if (msg.startsWith('MVMAX')) {
                    state.value.maxVolume = parseVal(msg.substring(6));
                }
                else if (msg.startsWith('MV') && msg.length <= 5) {
                    const newVol = parseVal(msg.substring(2));
                    mvManager.handleIncoming(newVol);
                }
                else if (msg.startsWith('SI')) state.value.input = msg.substring(2);
                else if (msg.startsWith('MS')) { state.value.surround = msg.substring(2); send('CV?', true); }
                else if (msg.startsWith('SV')) state.value.videoSelect = msg.substring(2);
                else if (msg.startsWith('SD')) state.value.signalDecode = msg.substring(2);
                else if (msg.startsWith('DC')) state.value.digitalDecode = msg.substring(2);
                else if (msg.startsWith('TFAN')) state.value.tunerFreq = msg.substring(4);
                else if (msg.startsWith('TPAN')) {
                    const tpVal = msg.substring(4);
                    if (tpVal === 'OFF') state.value.tunerPreset = '';
                    else if (/^\d+$/.test(tpVal)) state.value.tunerPreset = tpVal;
                }
                else if (msg.startsWith('TMAN')) state.value.tunerMode = msg.substring(4);
                else if (msg.startsWith('SLP')) {
                    let slp = msg.substring(3).trim();
                    if (slp === 'OFF' || !isNaN(parseInt(slp))) state.value.sleep = slp;
                }
                else if (msg.startsWith('VS')) {
                    if (msg.startsWith('VSASP')) state.value.aspect = msg.substring(5);
                    else if (msg.startsWith('VSSCH')) state.value.hdmiResolution = msg.substring(5);
                    else if (msg.startsWith('VSSC')) state.value.resolution = msg.substring(4);
                    else if (msg.startsWith('VSAUDIO ')) state.value.vsAudio = msg.substring(8).trim();
                    else if (msg.startsWith('VSVPM')) state.value.vsVpm = msg.substring(5);
                }
                else if (msg.startsWith('PS')) {
                    if (msg.startsWith('PSTONE CTRL')) state.value.toneCtrl = msg.substring(11).trim();
                    else if (msg.startsWith('PSMULTEQ:')) state.value.multeq = msg.substring(9).trim();
                    else if (msg.startsWith('PSBAS')) state.value.bass = parseVal(msg.substring(5)) || 50;
                    else if (msg.startsWith('PSTRE')) state.value.treble = parseVal(msg.substring(5)) || 50;
                    else if (msg.startsWith('PSDYNEQ ')) state.value.dyneq = msg.replace('PSDYNEQ ', '').trim();
                    else if (msg.startsWith('PSDYNVOL ')) state.value.dynvol = msg.replace('PSDYNVOL ', '').trim();
                    else if (msg.startsWith('PSREFLEV ')) state.value.reflev = msg.replace('PSREFLEV ', '').trim();
                    else if (msg.startsWith('PSDRC ')) state.value.drc = msg.substring(6).trim();
                    else if (msg.startsWith('PSSWR ')) state.value.swr = msg.substring(6).trim();
                    else if (msg.startsWith('PSSB:')) state.value.sbMode = msg.substring(5).trim();
                    else if (msg.startsWith('PSCINEMA EQ.')) state.value.cinemaEq = msg.substring(12).trim();
                    else if (msg.startsWith('PSFRONT ')) state.value.frontSp = msg.substring(8).trim();
                    else if (msg.startsWith('PSRSTR ')) state.value.audioRestorer = msg.substring(7).trim();
                    else if (msg.startsWith('PSRSZ ')) state.value.roomSize = msg.substring(6).trim();
                    else if (msg.startsWith('PSLOM ')) state.value.lom = msg.substring(6).trim();
                }
                else if (msg.startsWith('CV')) {
                    const match = msg.match(/^CV([A-Z]+)\s*(\d{2,3})/);
                    if (match && match[1] in state.value.channelVols) {
                        const raw = match[2].trim();
                        const ch = match[1];
                        const v = (raw === '00') ? null : parseVal(raw);
                        if (v === null || (v >= CONFIG.CHANNEL_VOL_MIN && v <= CONFIG.CHANNEL_VOL_MAX)) cvManagers[ch].handleIncoming(v);
                    }
                }
                else if (msg.startsWith('NSE')) {
                    const idx = parseInt(msg.substring(3, 4), 10);
                    if (!isNaN(idx) && idx <= 8) {
                        // Extract everything after the 4-char prefix (e.g. 'NSE0')
                        const text = msg.slice(4).trim();
                        if (state.value.nseLines[idx] !== text) {
                            state.value.nseLines[idx] = text;
                            state.value.nseLines = [...state.value.nseLines];
                        }
                    }
                }
            };

            ws.value.onclose = () => {
                if (connectionStatus.value !== 'RECONNECTING') {
                    connectionStatus.value = 'DISCONNECTED';
                    avrConnected.value = false;
                    resetState();
                }
                console.log('[WebSocket] Connection lost, retrying in 2s...');
                if (!reconnectTimer) reconnectTimer = setTimeout(connectWs, 2000);
            };
            ws.value.onerror = (error) => {
                console.error('[WebSocket] Error detected, triggering close');
                if (ws.value) ws.value.close();
            };
        };

        const send = (cmd, silent = false) => {
            if (connectionStatus.value === 'CONNECTED' && ws.value && ws.value.readyState === WebSocket.OPEN) {
                if (isBooting.value) return;
                ws.value.send(cmd);

                if (!silent) {
                    const timeStr = formatTime();

                    debugLog.value.push({ time: timeStr, msg: 'TX: ' + cmd, isTx: true, isHb: false });
                    if (debugLog.value.length > CONFIG.MAX_LOG_ENTRIES) debugLog.value.shift();
                    if (terminalEnabled.value && isScrolledToBottom.value) {
                        scrollToBottom();
                    }
                }
            }
        };

        const sendRaw = () => {
            if (rawCommand.value) {
                send(rawCommand.value.toUpperCase());
                rawCommand.value = '';
            }
        };

        const sendNsd = () => {
            const ch = nsdQuery.value.trim().toUpperCase().charAt(0);
            if (ch) {
                send('NSD' + ch);
                nsdQuery.value = '';
            }
        };

        const onAlbumArtError = () => {
            albumArtUrl.value = null;
        };

        const togglePower = () => {
            // Optimistic update - immediate UI feedback
            const newPower = state.value.power === 'ON' ? 'STANDBY' : 'ON';
            state.value.power = newPower;

            send(newPower === 'STANDBY' ? 'PWSTANDBY' : 'PWON');
        };

        const toggleMute = () => {
            // Optimistic update
            state.value.mute = state.value.mute === 'ON' ? 'OFF' : 'ON';
            send(state.value.mute === 'ON' ? 'MUON' : 'MUOFF');
        };

        const toggleLock = () => {
            // Optimistic update
            state.value.remoteLock = state.value.remoteLock === 'ON' ? 'OFF' : 'ON';
            send(state.value.remoteLock === 'ON' ? 'SYREMOTE LOCK ON' : 'SYREMOTE LOCK OFF');
        };

        let toneTimeout;
        const adjustTone = (type, delta) => {
            // Optimistic update
            if (type === 'BAS') state.value.bass = Math.max(CONFIG.TONE_CONTROL_MIN, Math.min(CONFIG.TONE_CONTROL_MAX, state.value.bass + delta));
            else state.value.treble = Math.max(CONFIG.TONE_CONTROL_MIN, Math.min(CONFIG.TONE_CONTROL_MAX, state.value.treble + delta));
            send('PS' + type + ' ' + formatVal(type === 'BAS' ? state.value.bass : state.value.treble));
        };

        const onToneDrag = (type) => {
            clearTimeout(toneTimeout);
            toneTimeout = setTimeout(() => {
                const val = type === 'BAS' ? state.value.bass : state.value.treble;
                send('PS' + type + ' ' + formatVal(val));
            }, CONFIG.SLIDER_DEBOUNCE_DELAY);
        };

        const adjustVolume = (delta) => mvManager.adjust(delta);
        const onVolumeDrag = () => mvManager.drag();
        const adjustChannel = (ch, delta) => cvManagers[ch].adjust(delta);
        const onChannelDrag = (ch) => cvManagers[ch].drag();

        const resetChannels = () => {
            // Send the neutral trim (50 = 0 dB) only for active channels not already at 0 dB
            activeChannels.value.forEach(ch => {
                if (state.value.channelVols[ch] !== 50) {
                    send(`CV${ch} ${formatVal(50)}`);
                }
            });
        };

        onMounted(() => {
            console.log('[App] Mounted, initiating WebSocket connection...');
            connectWs();

            // Ensure the remembered tab is scrolled into view on initial load.
            // Using a small delay to allow the browser to calculate layout geometry on mobile.
            setTimeout(() => scrollActiveTabIntoView('auto'), 100);

            // Check connection shortly after mount - if WebSocket is dead from tab restore, reconnect
            // Only reload if actually closed - not if still connecting
            setTimeout(() => {
                if (ws.value && (ws.value.readyState === WebSocket.CLOSED || ws.value.readyState === WebSocket.CLOSING)) {
                    console.log('[App] WebSocket is closed after mount, reconnecting...');
                    connectWs();
                }
            }, 500);

            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState === 'visible') {
                    console.log('[App] Tab visible, checking connection status...');
                    if (!ws.value || ws.value.readyState !== WebSocket.OPEN) {
                        console.log('[App] Socket not OPEN, forcing reconnect...');
                        connectWs();
                    }
                }
            });
        });

        const isSurroundMode = (mode) => {
            const s = state.value.surround;
            if (!s) return false;
            if (s === mode) return true;
            if (mode === 'MOVIE') return s.startsWith('DOLBY') || s.startsWith('DTS') || s === 'MONO MOVIE' || s === 'VIRTUAL';
            if (mode === 'MUSIC') return s === 'ROCK ARENA' || s === 'JAZZ CLUB' || s === 'MATRIX';
            if (mode === 'GAME') return s === 'VIDEO GAME';
            if (mode === 'MULTI CH IN') return s.startsWith('MULTI CH IN');
            return false;
        };

        const isDirectMode = computed(() =>
            state.value.surround === 'DIRECT' || state.value.surround === 'PURE DIRECT'
        );

        const isTrueHDMode = computed(() =>
            state.value.surround.startsWith('DOLBY HD') || state.value.surround.startsWith('DOLBY TRUEHD')
        );

        const isOriginalListeningMode = computed(() => {
            const s = state.value.surround;
            return s === 'ROCK ARENA' || s === 'JAZZ CLUB' || s === 'MATRIX' || s === 'VIDEO GAME';
        });

        const isTunerMode = computed(() => state.value.input === 'TUNER');

        const isStereoMode = computed(() => {
            const s = state.value.surround;
            return s === 'STEREO' || s === 'DIRECT' || s === 'PURE DIRECT';
        });

        const activeChannels = computed(() =>
            CHANNELS.filter(ch => state.value.channelVols[ch] !== null)
        );

        return {
            wsConnected: computed(() => connectionStatus.value === 'CONNECTED'),
            avrConnected,
            connectionStatus, state, tabs, activeTab,
            inputs: INPUTS, modes: MODES, videoSelects: VIDEO_SELECTS, signalDecodes: SIGNAL_DECODES,
            channelNames: CHANNEL_NAMES,
            digitalDecodes: DIGITAL_DECODES, resolutions: RESOLUTIONS,
            multeqModes: MULTEQ_MODES, channels: CHANNELS, drcModes: DRC_MODES, sbModes: SB_MODES,
            frontSpModes: FRONT_SP_MODES, audioRestorerModes: AUDIO_RESTORER_MODES, roomSizes: ROOM_SIZES,
            send, sendRaw, rawCommand, togglePower, toggleMute, toggleLock,
            adjustVolume, onVolumeDrag, onToneDrag, onChannelDrag, adjustTone, adjustChannel, resetChannels,
            terminalEnabled, debugLog, onTerminalScroll,
            showAllChannels, manualFreq, freqError, formattedTunerFreq, setTunerFreq,
            presetRecall, presetSave, recallPreset, savePreset,
            nsdQuery, sendNsd, albumArtUrl,
            onAlbumArtError, isSurroundMode, isDirectMode, isTrueHDMode, isOriginalListeningMode, isTunerMode,
            activeChannels,
            isStereoMode, stereoChannels: ['FL', 'FR', 'SW'], surroundChannels: ['FL', 'FR', 'C', 'SW', 'SL', 'SR'],
            uiEnabled, disableReason
        };
    }
}).mount('#app');

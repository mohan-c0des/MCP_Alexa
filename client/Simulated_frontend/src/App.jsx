import React, { useState, useEffect, useRef, useMemo } from 'react';
import {
  Mic,
  MicOff,
  Volume2,
  VolumeX,
  Send,
  Sparkles,
  FileText,
  FolderSearch,
  ShieldCheck,
  Play,
  XCircle,
  Eye,
  BarChart3,
  Globe,
  Terminal,
  RotateCcw,
  CheckCircle2,
  AlertTriangle,
  Cpu,
  ExternalLink,
  Code2,
  Layers,
  Radio,
  MessageSquare
} from 'lucide-react';

const API_BASE = 'http://127.0.0.1:8001';

const QUICK_PROMPTS = [
  { icon: Eye, label: 'Screen Vision', prompt: 'What is on my screen right now? Summarize the key details.', accent: '#38bdf8' },
  { icon: Cpu, label: 'Live GUI Action', prompt: 'Open YouTube and play the latest video from MrBeast.', accent: '#f43f5e' },
  { icon: BarChart3, label: 'Analyze CSV', prompt: 'Analyze sales.csv in my workspace and highlight the main trends.', accent: '#a855f7' },
  { icon: Globe, label: 'Web Research', prompt: 'Research current Indian EV market leaders and compare key numbers.', accent: '#2dd4bf' },
  { icon: Terminal, label: 'File Automation', prompt: 'Organize the files in my workspace into clean folders by file type.', accent: '#f59e0b' }
];

// Plays a subtle Echo-style two-tone chime using Web Audio API so you hear Alexa wake up even when minimized
function playAlexaChime(type = 'wake') {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    const now = ctx.currentTime;
    if (type === 'wake') {
      osc.frequency.setValueAtTime(520, now);
      osc.frequency.exponentialRampToValueAtTime(780, now + 0.14);
    } else {
      osc.frequency.setValueAtTime(640, now);
      osc.frequency.exponentialRampToValueAtTime(440, now + 0.12);
    }
    gain.gain.setValueAtTime(0.08, now);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.18);
    osc.start(now);
    osc.stop(now + 0.19);
  } catch (_) {}
}

function extractFollowUpPrompts(markdown = '') {
  const match = markdown.match(/## Follow-up questions\s+([\s\S]*?)(?:\n## |\s*$)/i);
  if (!match) return [];
  return match[1]
    .split('\n')
    .map((l) => l.replace(/^[-*]\s*/, '').trim())
    .filter((l) => l.length > 5 && l.length < 140)
    .slice(0, 3);
}

function FormattedMarkdown({ markdown }) {
  if (!markdown) return null;
  return (
    <div className="space-y-2 text-slate-200 text-sm leading-relaxed">
      {markdown.split('\n').map((raw, idx) => {
        const line = raw.trim();
        if (!line) return <div key={idx} className="h-1" />;
        if (line.startsWith('# ')) return <h2 key={idx} className="text-base font-bold text-white pt-1">{line.slice(2)}</h2>;
        if (line.startsWith('## ')) {
          return <h3 key={idx} className="text-xs font-bold uppercase tracking-wider text-cyan-300 pt-2 pb-1 border-b border-slate-600/80">{line.slice(3)}</h3>;
        }
        if (line.startsWith('- ') || line.startsWith('* ')) {
          const parts = line.slice(2).split(/(\*\*.*?\*\*)/g);
          return (
            <div key={idx} className="flex items-start gap-2 bg-slate-900/90 border border-slate-600/70 rounded-lg px-3 py-2">
              <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 mt-2 shrink-0" />
              <div className="text-xs text-slate-200">
                {parts.map((p, i) =>
                  p.startsWith('**') && p.endsWith('**') ? <strong key={i} className="text-white">{p.slice(2, -2)}</strong> : <span key={i}>{p}</span>
                )}
              </div>
            </div>
          );
        }
        return <p key={idx} className="text-xs text-slate-300">{line.replace(/\*\*/g, '')}</p>;
      })}
    </div>
  );
}

export default function App() {
  const [input, setInput] = useState('');
  const [chatHistory, setChatHistory] = useState([]);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState({
    connected: false,
    tools: ['ask_about_screen', 'interact_with_screen', 'research_topic', 'analyze_data', 'write_script', 'run_script'],
    workspace: '~/AlexaWorkspace'
  });

  const [artifactList, setArtifactList] = useState([]);
  const [selectedArtifactIdx, setSelectedArtifactIdx] = useState(0);
  const [showRawMarkdown, setShowRawMarkdown] = useState(false);

  // Live Voice Mode & Wake-Word States
  const [isLiveMode, setIsLiveMode] = useState(false);
  const [requireWakeWord, setRequireWakeWord] = useState(false);
  const [wakeArmed, setWakeArmed] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [liveTranscript, setLiveTranscript] = useState('');
  const [lastSpokenText, setLastSpokenText] = useState('Say "Alexa, what can you see on my screen right now?" from any app.');
  const [ttsEnabled, setTtsEnabled] = useState(true);

  const chatScrollRef = useRef(null);
  const recognitionRef = useRef(null);
  const isLiveRef = useRef(false);
  const isSpeakingRef = useRef(false);
  const loadingRef = useRef(false);
  const wakeArmedRef = useRef(false);
  const requireWakeRef = useRef(false);

  useEffect(() => { isLiveRef.current = isLiveMode; }, [isLiveMode]);
  useEffect(() => { isSpeakingRef.current = isSpeaking; }, [isSpeaking]);
  useEffect(() => { loadingRef.current = loading; }, [loading]);
  useEffect(() => { requireWakeRef.current = requireWakeWord; }, [requireWakeWord]);

  useEffect(() => {
    if ('speechSynthesis' in window) {
      window.speechSynthesis.getVoices();
      window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
    }
  }, []);

  useEffect(() => {
    fetch(`${API_BASE}/status`)
      .then((r) => r.json())
      .then((d) => d?.tools && setStatus(d))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTo({ top: chatScrollRef.current.scrollHeight, behavior: 'smooth' });
    }
  }, [chatHistory, loading]);

  const startRecognitionSafely = () => {
    if (!isLiveRef.current || isSpeakingRef.current || loadingRef.current) return;
    try {
      recognitionRef.current?.start();
    } catch (_) {}
  };

  const stopSpeaking = () => {
    window.speechSynthesis?.cancel();
    setIsSpeaking(false);
    isSpeakingRef.current = false;
    setTimeout(startRecognitionSafely, 250);
  };

  const formatPathForSpeech = (rawPath, precededByPrep = false) => {
    const parts = rawPath.replace(/[`"']/g, '').replace(/\\/g, '/').split('/').filter(Boolean);
    if (parts.length === 0) return rawPath;

    const itemName = parts[parts.length - 1];
    const cleanItem = itemName.replace(/\.[a-z0-9]{2,5}$/i, '');

    // Friendly phrasing for auto-generated timestamped PDF reports
    if (/^(analysis|research)_\d+/i.test(itemName)) {
      return precededByPrep ? 'your Alexa Workspace reports folder' : 'the PDF report in your Alexa Workspace';
    }

    // Strip drive letter (C:) and Users/<username> so only meaningful folders remain
    let folders = parts.slice(0, -1);
    const usersIdx = folders.findIndex((p) => /^users$/i.test(p));
    if (usersIdx !== -1) {
      folders = folders.slice(usersIdx + 2);
    } else if (/^[a-z]:$/i.test(folders[0] || '')) {
      folders = folders.slice(1);
    }

    const parent = folders[folders.length - 1];
    const grandParent = folders[folders.length - 2];

    let locationPhrase = 'your folder';
    if (parent && grandParent) {
      locationPhrase =
        parent.toLowerCase() === grandParent.toLowerCase()
          ? `the ${parent} subfolder inside ${grandParent}`
          : `your ${grandParent} ${parent} folder`;
    } else if (parent) {
      locationPhrase = `your ${parent} folder`;
    }

    return precededByPrep ? locationPhrase : `${cleanItem} in ${locationPhrase}`;
  };

  const speakText = (text) => {
    if (!text) return;
    setLastSpokenText(text);
    if (!ttsEnabled || !('speechSynthesis' in window)) return;

    // Pause mic while Alexa speaks so she doesn't hear herself
    try { recognitionRef.current?.stop(); } catch (_) {}
    window.speechSynthesis.cancel();

    const pathPattern = /((?:in|at|from|to)\s+)?(?:`([A-Za-z]:\\[^`]+|\/[^`]+)`|"([A-Za-z]:\\[^"]+|\/[^"]+)"|([A-Za-z]:\\(?:[^\\/:*?"<>|\r\n]+\\)+[^\\/:*?"<>|\r\n]+\.[A-Za-z0-9]{2,5}\b|[A-Za-z]:\\[^\s"`',;!?)]+))/gi;

    const cleanSpeech = text
      .replace(pathPattern, (_, prep = '', p1, p2, p3) => {
        const matchedPath = p1 || p2 || p3 || '';
        return `${prep}${formatPathForSpeech(matchedPath, Boolean(prep.trim()))}`;
      })
      .replace(/\.pptx\b/gi, ' presentation')
      .replace(/[*#`_~]/g, '');

    const utterance = new SpeechSynthesisUtterance(cleanSpeech);
    const voices = window.speechSynthesis.getVoices();
    const preferredFemale =
      voices.find((v) => /aria|jenny|zira|samantha|google us english|sonia|natasha|hazel|susan|female/i.test(v.name)) ||
      voices.find((v) => v.lang?.startsWith('en'));
    if (preferredFemale) {
      utterance.voice = preferredFemale;
    }
    utterance.rate = 1.18;
    utterance.pitch = 1.05;
    utterance.onstart = () => {
      setIsSpeaking(true);
      isSpeakingRef.current = true;
    };
    utterance.onend = () => {
      setIsSpeaking(false);
      isSpeakingRef.current = false;
      setTimeout(startRecognitionSafely, 300);
    };
    utterance.onerror = () => {
      setIsSpeaking(false);
      isSpeakingRef.current = false;
      setTimeout(startRecognitionSafely, 300);
    };
    window.speechSynthesis.speak(utterance);
  };

  const toggleLiveMode = () => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) return;

    if (isLiveMode) {
      isLiveRef.current = false;
      setIsLiveMode(false);
      setWakeArmed(false);
      wakeArmedRef.current = false;
      try { recognitionRef.current?.stop(); } catch (_) {}
      stopSpeaking();
      return;
    }

    stopSpeaking();
    const recognition = new SpeechRecognition();
    recognitionRef.current = recognition;
    recognition.lang = 'en-US';
    recognition.continuous = true;
    recognition.interimResults = true;

    recognition.onresult = (event) => {
      if (isSpeakingRef.current || loadingRef.current) return;
      let interim = '';
      let finalChunk = '';

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const piece = event.results[i][0].transcript;
        if (event.results[i].isFinal) finalChunk += piece;
        else interim += piece;
      }

      const currentText = (finalChunk || interim).trim();
      setLiveTranscript(currentText);

      // Check if user said "Alexa" in interim to show visual wake state immediately
      if (/(\b|^)(alexa|alex)(\b|$)/i.test(currentText) && !wakeArmedRef.current) {
        wakeArmedRef.current = true;
        setWakeArmed(true);
      }

      if (finalChunk.trim()) {
        const raw = finalChunk.trim();
        const wakeMatch = raw.match(/(?:^|\b)(?:hey\s+|ok\s+)?alexa[\s,.:;!?-]*(.*)/i);

        if (!requireWakeRef.current) {
          const directCommand = raw.replace(/^(?:hey\s+|ok\s+)?alexa[\s,.:;!?-]*/i, '').trim() || raw;
          playAlexaChime('wake');
          sendMessage(null, directCommand);
        } else if (wakeMatch) {
          const commandAfterAlexa = wakeMatch[1].trim();
          if (commandAfterAlexa.length > 2) {
            // User said "Alexa, what can you see on my screen right now?" in one go
            playAlexaChime('wake');
            wakeArmedRef.current = false;
            setWakeArmed(false);
            sendMessage(null, commandAfterAlexa);
          } else {
            // User just said "Alexa" — arm for the next sentence!
            playAlexaChime('wake');
            wakeArmedRef.current = true;
            setWakeArmed(true);
            setLiveTranscript('Listening for your command...');
          }
        } else if (wakeArmedRef.current) {
          // Follow-up sentence right after saying "Alexa"
          wakeArmedRef.current = false;
          setWakeArmed(false);
          sendMessage(null, raw);
        }
      }
    };

    // Keep restarting in background whenever Live Mode is active
    recognition.onend = () => {
      if (isLiveRef.current && !isSpeakingRef.current && !loadingRef.current) {
        setTimeout(startRecognitionSafely, 200);
      }
    };

    isLiveRef.current = true;
    setIsLiveMode(true);
    setLiveTranscript('');
    playAlexaChime('wake');
    try { recognition.start(); } catch (_) {}
  };

  const sendMessage = async (e, overrideText = null) => {
    if (e) e.preventDefault();
    const textToSend = (overrideText ?? input).trim();
    if (!textToSend || loadingRef.current) return;

    try { recognitionRef.current?.stop(); } catch (_) {}
    window.speechSynthesis?.cancel();
    setIsSpeaking(false);
    isSpeakingRef.current = false;

    setInput('');
    setLiveTranscript(textToSend);
    setLoading(true);
    loadingRef.current = true;

    const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    setChatHistory((prev) => [...prev, { sender: 'user', text: textToSend, timestamp: time }]);

    try {
      const res = await fetch(`${API_BASE}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: textToSend })
      });
      const data = await res.json();
      const replyText = data.response || 'Done.';
      const steps = data.steps || [];
      const incomingArtifacts = data.artifacts || [];

      let suggestions = [];
      if (incomingArtifacts.length > 0) {
        const lastArt = incomingArtifacts[incomingArtifacts.length - 1];
        if (lastArt.tool === 'write_script' && lastArt.data?.script_id) {
          suggestions = [`Yes, run ${lastArt.data.script_id}`, 'No, do not run the script'];
        } else if (lastArt.data?.report_markdown) {
          suggestions = extractFollowUpPrompts(lastArt.data.report_markdown);
        }
      }

      setChatHistory((prev) => [
        ...prev,
        { sender: 'alexa', text: replyText, steps, suggestions, timestamp: time }
      ]);

      if (incomingArtifacts.length > 0) {
        setArtifactList((prev) => {
          const next = [...prev, ...incomingArtifacts];
          setSelectedArtifactIdx(next.length - 1);
          return next;
        });
      }

      setLoading(false);
      loadingRef.current = false;
      speakText(replyText);
    } catch (_) {
      const errMsg = 'Could not reach Backend on port 8001. Ensure server.py (8000) and main.py (8001) are running.';
      setChatHistory((prev) => [...prev, { sender: 'alexa', text: errMsg, isError: true, timestamp: time }]);
      setLoading(false);
      loadingRef.current = false;
      speakText(errMsg);
    }
  };

  const handleReset = async () => {
    stopSpeaking();
    try { await fetch(`${API_BASE}/reset`, { method: 'POST' }); } catch (_) {}
    setChatHistory([]);
    setArtifactList([]);
    setSelectedArtifactIdx(0);
    setLastSpokenText('Conversation cleared. Ready for your next voice command.');
  };

  const openPdf = (pdfPath) => {
    if (pdfPath) window.open(`${API_BASE}/pdf?path=${encodeURIComponent(pdfPath)}`, '_blank');
  };

  const activeArtifact = artifactList[selectedArtifactIdx] || null;
  const latestSuggestions = useMemo(() => {
    for (let i = chatHistory.length - 1; i >= 0; i--) {
      if (chatHistory[i].sender === 'alexa' && chatHistory[i].suggestions?.length) return chatHistory[i].suggestions;
    }
    return [];
  }, [chatHistory]);

  const orbState = isSpeaking ? 'speaking' : loading ? 'thinking' : wakeArmed ? 'armed' : 'listening';

  return (
    <div className="fixed inset-0 w-screen h-screen overflow-hidden bg-[#050811] text-slate-100 flex flex-col select-none font-sans">
      <style>{`
        html, body, #root { height: 100% !important; width: 100% !important; margin: 0 !important; padding: 0 !important; overflow: hidden !important; background: #050811 !important; }
        .custom-scroll::-webkit-scrollbar { width: 6px; height: 6px; }
        .custom-scroll::-webkit-scrollbar-thumb { background: rgba(56, 189, 248, 0.4); border-radius: 9999px; }
        @keyframes orbMorph {
          0%, 100% { border-radius: 50%; transform: scale(1) rotate(0deg); }
          33% { border-radius: 44% 56% 52% 48% / 48% 44% 56% 52%; transform: scale(1.08) rotate(60deg); }
          66% { border-radius: 54% 46% 45% 55% / 53% 55% 45% 47%; transform: scale(0.95) rotate(120deg); }
        }
        @keyframes rippleOut {
          0% { transform: scale(0.9); opacity: 0.75; }
          100% { transform: scale(1.75); opacity: 0; }
        }
        .orb-core { animation: orbMorph 5s ease-in-out infinite; }
        .orb-fast { animation: orbMorph 1.6s ease-in-out infinite; }
        .orb-ripple-1 { animation: rippleOut 2.2s cubic-bezier(0.16, 1, 0.3, 1) infinite; }
        .orb-ripple-2 { animation: rippleOut 2.2s cubic-bezier(0.16, 1, 0.3, 1) 0.7s infinite; }
      `}</style>

      {/* Top Header with crisp visible border */}
      <header className="h-16 px-6 border-b-2 border-cyan-500/40 bg-[#091022] flex items-center justify-between shrink-0 shadow-[0_4px_20px_rgba(0,168,225,0.12)]">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-cyan-500/20 border-2 border-cyan-400 flex items-center justify-center shadow-[0_0_15px_rgba(56,189,248,0.4)]">
            <Sparkles className="w-4 h-4 text-cyan-300" />
          </div>
          <div>
            <div className="flex items-center gap-2.5">
              <h1 className="text-base font-bold text-white tracking-tight">Alexa+ Laptop Productivity Co-Pilot</h1>
              <span className="text-[10px] font-bold uppercase tracking-widest px-2.5 py-0.5 rounded-full bg-cyan-500/20 text-cyan-200 border border-cyan-400/60">
                Streamable HTTP MCP
              </span>
              {isLiveMode && (
                <span className="text-[10px] font-bold uppercase tracking-widest px-2.5 py-0.5 rounded-full bg-rose-500/25 text-rose-200 border border-rose-400 flex items-center gap-1.5 animate-pulse">
                  <span className="w-2 h-2 rounded-full bg-rose-400" /> Background Wake-Word Active
                </span>
              )}
            </div>
            <p className="text-[11px] text-slate-300">
              {status.connected ? `Connected • ${status.tools.length} MCP Tools Ready` : 'MCP Client Ready (Ports 8000 & 8001)'}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={toggleLiveMode}
            className={`flex items-center gap-2 px-3.5 py-1.5 rounded-xl text-xs font-bold border-2 transition cursor-pointer ${
              isLiveMode
                ? 'bg-rose-600 text-white border-rose-300 shadow-[0_0_20px_rgba(244,63,94,0.6)]'
                : 'bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-200 border-cyan-400/70'
            }`}
          >
            <Radio className="w-4 h-4" />
            {isLiveMode ? 'LIVE MODE ON (Listening for "Alexa")' : 'Go Live (Voice Mode)'}
          </button>

          <button
            type="button"
            onClick={() => { if (ttsEnabled) stopSpeaking(); setTtsEnabled(!ttsEnabled); }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold bg-slate-900 text-slate-200 border-2 border-slate-600 hover:border-cyan-400 transition cursor-pointer"
          >
            {ttsEnabled ? <Volume2 className="w-3.5 h-3.5 text-cyan-400" /> : <VolumeX className="w-3.5 h-3.5 text-slate-400" />}
            {ttsEnabled ? 'Voice ON' : 'Muted'}
          </button>

          <button
            type="button"
            onClick={handleReset}
            className="p-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-slate-200 border-2 border-slate-600 hover:border-slate-400 transition cursor-pointer"
            title="Reset Session"
          >
            <RotateCcw className="w-4 h-4" />
          </button>
        </div>
      </header>

      {}
      <main className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-12 gap-4 p-4 overflow-hidden">
        {/* LEFT PANEL: Switches between Full Gemini/ChatGPT Live Voice Orb UI and Chat History */}
        <section className="lg:col-span-7 flex flex-col min-h-0 h-full rounded-2xl border-2 border-cyan-500/50 bg-[#0a1020] shadow-[0_0_30px_rgba(0,168,225,0.1)] overflow-hidden p-4">
          {isLiveMode ? (
            /* FULL-PANEL CHATGPT / GEMINI LIVE VOICE INTERFACE */
            <div className="flex-1 min-h-0 flex flex-col items-center justify-between py-4 px-6 relative overflow-hidden">
              {/* Top Live Controls Bar */}
              <div className="w-full flex items-center justify-between gap-2 z-10">
                <div className="flex items-center gap-2">
                  <span className="px-3 py-1 rounded-full text-xs font-bold bg-cyan-500/20 text-cyan-200 border border-cyan-400/60">
                    {orbState === 'speaking'
                      ? '🔊 Alexa+ Speaking'
                      : orbState === 'thinking'
                      ? '⚡ Running Laptop MCP Tool...'
                      : orbState === 'armed'
                      ? '🎙️ Heard "Alexa" — Speak Command!'
                      : '👂 Background Listener Ready'}
                  </span>
                  <button
                    type="button"
                    onClick={() => setRequireWakeWord(!requireWakeWord)}
                    className={`px-2.5 py-1 rounded-full text-[11px] font-semibold border transition cursor-pointer ${
                      requireWakeWord
                        ? 'bg-emerald-500/20 text-emerald-200 border-emerald-400/60'
                        : 'bg-slate-800 text-slate-300 border-slate-600'
                    }`}
                  >
                    Wake Word "Alexa": {requireWakeWord ? 'ON' : 'OFF (Send All Speech)'}
                  </button>
                </div>

                <button
                  type="button"
                  onClick={() => setIsLiveMode(false)}
                  className="flex items-center gap-1.5 px-3 py-1 rounded-xl bg-slate-900 hover:bg-slate-800 text-xs font-semibold text-slate-200 border border-slate-500 cursor-pointer"
                >
                  <MessageSquare className="w-3.5 h-3.5 text-cyan-400" />
                  View Chat History ({chatHistory.length})
                </button>
              </div>

              {/* Center Fluid Gemini/ChatGPT Speaking Circle */}
              <div className="relative flex items-center justify-center my-auto w-56 h-56">
                <div
                  className="orb-ripple-1 absolute inset-0 rounded-full border-2"
                  style={{
                    borderColor:
                      orbState === 'speaking' ? '#38bdf8' : orbState === 'thinking' ? '#c084fc' : orbState === 'armed' ? '#fb7185' : '#0284c7'
                  }}
                />
                <div
                  className="orb-ripple-2 absolute inset-4 rounded-full border-2"
                  style={{
                    borderColor:
                      orbState === 'speaking' ? '#00A8E1' : orbState === 'thinking' ? '#a855f7' : orbState === 'armed' ? '#f43f5e' : '#38bdf8'
                  }}
                />
                <div
                  className={`w-36 h-36 flex flex-col items-center justify-center text-center p-4 shadow-2xl transition-all duration-500 ${
                    orbState === 'speaking' || orbState === 'thinking' || orbState === 'armed' ? 'orb-fast' : 'orb-core'
                  }`}
                  style={{
                    background:
                      orbState === 'speaking'
                        ? 'radial-gradient(circle at 30% 30%, #67e8f9, #00A8E1, #1d4ed8)'
                        : orbState === 'thinking'
                        ? 'radial-gradient(circle at 30% 30%, #e879f9, #9333ea, #3b0764)'
                        : orbState === 'armed'
                        ? 'radial-gradient(circle at 30% 30%, #fda4af, #f43f5e, #9f1239)'
                        : 'radial-gradient(circle at 30% 30%, #38bdf8, #0369a1, #0f172a)',
                    boxShadow:
                      orbState === 'speaking'
                        ? '0 0 60px rgba(56, 189, 248, 0.85)'
                        : orbState === 'thinking'
                        ? '0 0 55px rgba(168, 85, 247, 0.8)'
                        : orbState === 'armed'
                        ? '0 0 55px rgba(244, 63, 94, 0.8)'
                        : '0 0 35px rgba(2, 132, 199, 0.45)'
                  }}
                >
                  <span className="text-xs font-bold uppercase tracking-widest text-white drop-shadow">
                    {orbState === 'speaking'
                      ? 'Speaking'
                      : orbState === 'thinking'
                      ? 'Working'
                      : orbState === 'armed'
                      ? 'Listening'
                      : 'Say "Alexa"'}
                  </span>
                </div>
              </div>

              {/* Live Subtitles / Captions under the Circle */}
              <div className="w-full max-w-xl text-center space-y-3 z-10">
                {liveTranscript && (
                  <div className="px-4 py-2 rounded-xl bg-slate-900/90 border border-cyan-400/50 text-cyan-200 text-sm font-medium">
                    🎙️ "{liveTranscript}"
                  </div>
                )}
                <div className="p-4 rounded-2xl bg-slate-950/90 border-2 border-slate-600/80 text-slate-100 text-sm leading-relaxed max-h-32 overflow-y-auto custom-scroll">
                  {lastSpokenText}
                </div>
                <p className="text-xs text-cyan-300/90 font-medium">
                  💡 Tip: Switch to any other window (VS Code, browser tab, Excel) and say:{' '}
                  <strong className="text-white">"Alexa, what can you see on my screen right now?"</strong>
                </p>
              </div>
            </div>
          ) : (
            /* STANDARD SCROLLABLE CHAT VIEW */
            <div ref={chatScrollRef} className="flex-1 min-h-0 overflow-y-auto custom-scroll pr-1 space-y-4 select-text">
              {chatHistory.length === 0 ? (
                <div className="h-full flex flex-col items-center justify-center text-center px-6">
                  <h2 className="text-lg font-bold text-white mb-1">Say "Alexa" in Live Mode or pick a command</h2>
                  <p className="text-xs text-slate-300 max-w-md mb-5">
                    Click <strong className="text-cyan-300">Go Live</strong> to keep Alexa listening in the background while you switch windows, or use text below.
                  </p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 w-full max-w-xl">
                    {QUICK_PROMPTS.map((item) => {
                      const Icon = item.icon;
                      return (
                        <button
                          key={item.label}
                          type="button"
                          onClick={() => sendMessage(null, item.prompt)}
                          className="text-left p-3.5 rounded-xl bg-slate-900 hover:bg-slate-800 border-2 border-slate-600/80 hover:border-cyan-400 transition cursor-pointer flex items-start gap-3"
                        >
                          <Icon className="w-5 h-5 shrink-0 mt-0.5" style={{ color: item.accent }} />
                          <div>
                            <div className="text-xs font-bold text-white">{item.label}</div>
                            <p className="text-[11px] text-slate-300 mt-0.5">"{item.prompt}"</p>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                </div>
              ) : (
                chatHistory.map((msg, i) => (
                  <div key={i} className="space-y-2">
                    {msg.steps?.length > 0 && (
                      <div className="rounded-xl bg-slate-950 border-2 border-slate-600/80 px-3.5 py-2 text-xs font-mono space-y-1">
                        <div className="text-[10px] uppercase tracking-wider text-cyan-300 font-bold flex items-center gap-1.5">
                          <Cpu className="w-3.5 h-3.5" /> MCP Tool Execution Trace
                        </div>
                        {msg.steps.map((s, idx) => (
                          <div key={idx} className="flex items-center gap-2 text-[11px] truncate">
                            <span className={s.type === 'call' ? 'text-cyan-300 font-bold' : 'text-emerald-300 font-bold'}>
                              {s.type === 'call' ? `→ ${s.tool}()` : `← ${s.tool}`}
                            </span>
                            <span className="text-slate-300 truncate">{s.summary}</span>
                          </div>
                        ))}
                      </div>
                    )}
                    <div className={`flex flex-col ${msg.sender === 'user' ? 'items-end' : 'items-start'}`}>
                      <div
                        className={`max-w-[88%] rounded-2xl px-4 py-3 text-sm leading-relaxed border-2 ${
                          msg.sender === 'user'
                            ? 'bg-[#0284c7] text-white border-cyan-300/60'
                            : msg.isError
                            ? 'bg-rose-950/80 border-rose-400 text-rose-100'
                            : 'bg-slate-900 border-slate-500/80 text-slate-100'
                        }`}
                      >
                        {msg.text}
                      </div>
                    </div>
                  </div>
                ))
              )}
              {loading && (
                <div className="px-4 py-3 rounded-xl bg-slate-900 border-2 border-cyan-400/60 text-xs text-cyan-200 font-semibold animate-pulse">
                  ⚡ Alexa+ is chaining MCP tools...
                </div>
              )}
            </div>
          )}

          {/* Persistent Quick-Prompts Ribbon + Input Bar */}
          <div className="pt-2.5 mt-2 border-t-2 border-slate-700/90 space-y-2 shrink-0">
            <div className="flex items-center gap-1.5 overflow-x-auto custom-scroll pb-1">
              <span className="text-[10px] font-bold uppercase text-cyan-300 shrink-0 mr-1">Prompts:</span>
              {(latestSuggestions.length > 0
                ? latestSuggestions.map((s) => ({ label: s, prompt: s }))
                : QUICK_PROMPTS
              ).map((item, i) => (
                <button
                  key={i}
                  type="button"
                  disabled={loading}
                  onClick={() => sendMessage(null, item.prompt)}
                  className="text-[11px] px-3 py-1 rounded-full bg-slate-900 hover:bg-slate-800 text-slate-200 hover:text-cyan-200 border border-slate-500 shrink-0 transition cursor-pointer"
                >
                  {item.label}
                </button>
              ))}
            </div>

            <form onSubmit={sendMessage} className="flex items-center gap-2">
              <button
                type="button"
                onClick={toggleLiveMode}
                className={`px-4 py-3 rounded-xl border-2 font-bold text-xs flex items-center gap-2 cursor-pointer shrink-0 transition ${
                  isLiveMode
                    ? 'bg-rose-600 text-white border-rose-300 shadow-[0_0_15px_rgba(244,63,94,0.6)]'
                    : 'bg-slate-900 hover:bg-slate-800 text-cyan-300 border-cyan-400/70'
                }`}
              >
                {isLiveMode ? <MicOff className="w-4 h-4" /> : <Mic className="w-4 h-4" />}
                {isLiveMode ? 'Exit Live' : 'Live Voice'}
              </button>

              <input
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="Type a command or click 'Live Voice' and say 'Alexa...' from any app"
                className="flex-1 bg-slate-900 border-2 border-slate-600 focus:border-cyan-400 focus:outline-none rounded-xl px-4 py-2.5 text-sm text-white placeholder-slate-400"
              />

              <button
                type="submit"
                disabled={loading || !input.trim()}
                className="px-5 py-2.5 rounded-xl bg-[#00A8E1] hover:bg-[#0284c7] disabled:opacity-40 text-white font-bold text-sm border-2 border-cyan-300/60 flex items-center gap-1.5 cursor-pointer shrink-0"
              >
                <Send className="w-4 h-4" /> Send
              </button>
            </form>
          </div>
        </section>

        {}
        <section className="lg:col-span-5 flex flex-col min-h-0 h-full rounded-2xl border-2 border-slate-500/80 bg-[#0a1020] shadow-2xl overflow-hidden">
          <div className="px-4 py-3 border-b-2 border-slate-600/90 bg-[#070c18] flex items-center justify-between gap-2 shrink-0">
            <div className="flex items-center gap-2">
              <Layers className="w-4 h-4 text-cyan-400" />
              <span className="text-xs font-bold uppercase tracking-wider text-white">Echo Show Multimodal Canvas</span>
            </div>
            {artifactList.length > 1 && (
              <div className="flex items-center gap-1 overflow-x-auto custom-scroll">
                {artifactList.map((art, idx) => (
                  <button
                    key={idx}
                    type="button"
                    onClick={() => setSelectedArtifactIdx(idx)}
                    className={`text-[11px] px-2.5 py-1 rounded-md font-mono border cursor-pointer ${
                      idx === selectedArtifactIdx
                        ? 'bg-cyan-500/25 text-cyan-200 border-cyan-400'
                        : 'bg-slate-900 text-slate-300 border-slate-600'
                    }`}
                  >
                    #{idx + 1} {art.tool}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="flex-1 min-h-0 overflow-y-auto custom-scroll p-5 select-text">
            {!activeArtifact ? (
              <div className="h-full flex flex-col items-center justify-center text-center px-6 text-slate-300">
                <div className="w-16 h-16 rounded-2xl bg-slate-900 border-2 border-slate-600 flex items-center justify-center mb-4">
                  <FileText className="w-7 h-7 text-cyan-400" />
                </div>
                <p className="text-sm font-bold text-white mb-1">Multimodal Display Ready</p>
                <p className="text-xs text-slate-300 max-w-xs leading-relaxed">
                  PDF reports, extracted metrics, screen captures, and sandbox script approvals appear here automatically.
                </p>
              </div>
            ) : (
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-mono font-bold px-3 py-1 rounded-md bg-cyan-500/20 text-cyan-200 border border-cyan-400/70">
                    Tool: {activeArtifact.tool}
                  </span>
                  {activeArtifact.data?.confidence && (
                    <span className="text-xs font-bold uppercase px-3 py-1 rounded-full bg-emerald-500/20 text-emerald-200 border border-emerald-400/70">
                      Confidence: {activeArtifact.data.confidence}
                    </span>
                  )}
                </div>

                {(activeArtifact.tool === 'analyze_data' || activeArtifact.tool === 'research_topic') && (
                  <div className="space-y-4">
                    <div className="p-4 rounded-xl bg-slate-900 border-2 border-slate-600">
                      <h2 className="text-base font-bold text-white">{activeArtifact.data.headline || 'Analysis Report'}</h2>
                      {activeArtifact.data.spoken_summary && (
                        <p className="text-xs text-slate-200 mt-2 leading-relaxed">{activeArtifact.data.spoken_summary}</p>
                      )}
                      <div className="flex flex-wrap gap-2.5 mt-4">
                        {activeArtifact.data.report_pdf_path && (
                          <button
                            type="button"
                            onClick={() => openPdf(activeArtifact.data.report_pdf_path)}
                            className="flex items-center gap-2 px-3.5 py-2 rounded-lg bg-[#00A8E1] hover:bg-[#0284c7] text-white text-xs font-bold border border-cyan-300 cursor-pointer"
                          >
                            <FileText className="w-3.5 h-3.5" /> Open Analysis PDF <ExternalLink className="w-3 h-3" />
                          </button>
                        )}
                        {activeArtifact.data.dossier_pdf_path && (
                          <button
                            type="button"
                            onClick={() => openPdf(activeArtifact.data.dossier_pdf_path)}
                            className="flex items-center gap-2 px-3.5 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-cyan-200 border border-cyan-400 text-xs font-bold cursor-pointer"
                          >
                            <FolderSearch className="w-3.5 h-3.5" /> Open Dossier PDF <ExternalLink className="w-3 h-3" />
                          </button>
                        )}
                      </div>
                    </div>

                    {activeArtifact.data.report_markdown && (
                      <div className="space-y-2">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-bold uppercase text-slate-300">Evidence Breakdown</span>
                          <button
                            type="button"
                            onClick={() => setShowRawMarkdown(!showRawMarkdown)}
                            className="text-xs text-cyan-300 hover:underline flex items-center gap-1 cursor-pointer"
                          >
                            <Code2 className="w-3.5 h-3.5" /> {showRawMarkdown ? 'Formatted' : 'Raw Markdown'}
                          </button>
                        </div>
                        <div className="p-4 rounded-xl bg-slate-950 border-2 border-slate-600/80">
                          {showRawMarkdown ? (
                            <pre className="text-xs text-slate-200 font-mono whitespace-pre-wrap">{activeArtifact.data.report_markdown}</pre>
                          ) : (
                            <FormattedMarkdown markdown={activeArtifact.data.report_markdown} />
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {activeArtifact.tool === 'write_script' && (
                  <div className="space-y-4">
                    <div className="p-4 rounded-xl bg-amber-500/15 border-2 border-amber-400/80 space-y-2">
                      <div className="flex items-center gap-2 text-amber-200 font-bold text-sm">
                        <ShieldCheck className="w-4 h-4" /> Human-in-the-Loop Script Approval
                      </div>
                      <p className="text-xs text-white">{activeArtifact.data.summary_for_user}</p>
                      <div className="text-[11px] font-mono text-emerald-200 bg-emerald-950/80 border border-emerald-400/60 px-2.5 py-1.5 rounded-lg">
                        ✓ {activeArtifact.data.safety_check} • ID: {activeArtifact.data.script_id}
                      </div>
                    </div>
                    <pre className="p-3.5 rounded-xl bg-slate-950 border-2 border-slate-600 text-xs text-cyan-200 font-mono whitespace-pre-wrap max-h-60 overflow-y-auto custom-scroll">
                      {activeArtifact.data.code}
                    </pre>
                    <div className="flex items-center gap-3">
                      <button
                        type="button"
                        onClick={() => sendMessage(null, `Yes, run ${activeArtifact.data.script_id}`)}
                        className="flex-1 py-2.5 px-4 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs border border-emerald-300 flex items-center justify-center gap-2 cursor-pointer"
                      >
                        <Play className="w-3.5 h-3.5 fill-current" /> Approve & Run in Sandbox
                      </button>
                      <button
                        type="button"
                        onClick={() => sendMessage(null, 'No, do not run the script.')}
                        className="py-2.5 px-4 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 font-bold text-xs border border-slate-500 flex items-center gap-1.5 cursor-pointer"
                      >
                        <XCircle className="w-3.5 h-3.5" /> Cancel
                      </button>
                    </div>
                  </div>
                )}

                {activeArtifact.tool === 'run_script' && (
                  <div className="space-y-3">
                    <div className="p-4 rounded-xl border-2 border-emerald-400/80 bg-emerald-500/15 text-emerald-200 font-bold text-sm flex items-center gap-2">
                      {activeArtifact.data.succeeded ? <CheckCircle2 className="w-5 h-5" /> : <AlertTriangle className="w-5 h-5" />}
                      {activeArtifact.data.succeeded ? 'Sandbox Execution Succeeded' : 'Sandbox Execution Stopped'}
                    </div>
                    {activeArtifact.data.output && (
                      <pre className="p-3.5 rounded-xl bg-slate-950 border-2 border-slate-600 text-xs text-emerald-300 font-mono whitespace-pre-wrap">
                        {activeArtifact.data.output}
                      </pre>
                    )}
                  </div>
                )}

                {activeArtifact.tool === 'ask_about_screen' && (
                  <div className="p-4 rounded-xl bg-slate-950 border-2 border-cyan-400/70 text-sm text-slate-100 leading-relaxed whitespace-pre-wrap">
                    {activeArtifact.data.text || activeArtifact.data.spoken_summary}
                  </div>
                )}

                {}
                {activeArtifact.tool === 'interact_with_screen' && (
                  <div className="space-y-3">
                    <div className="p-4 rounded-xl bg-slate-900 border-2 border-cyan-400/80 space-y-1.5">
                      <div className="text-xs font-bold uppercase tracking-wider text-cyan-300 flex items-center gap-1.5">
                        <Cpu className="w-4 h-4" /> Live GUI Computer-Use Execution
                      </div>
                      {activeArtifact.data.screen_observation && (
                        <p className="text-xs text-slate-300">
                          <strong className="text-white">Observed Screen:</strong> {activeArtifact.data.screen_observation}
                        </p>
                      )}
                      {activeArtifact.data.spoken_summary && (
                        <p className="text-xs text-cyan-100 pt-1">{activeArtifact.data.spoken_summary}</p>
                      )}
                    </div>

                    <div className="space-y-2">
                      <div className="text-xs font-bold uppercase text-slate-300">Actions Performed on Host OS</div>
                      {(activeArtifact.data.actions_executed || []).map((item, idx) => (
                        <div
                          key={idx}
                          className={`p-3 rounded-xl border-2 text-xs font-mono flex items-start gap-2.5 ${
                            item.status === 'ok'
                              ? 'bg-emerald-950/50 border-emerald-400/70 text-emerald-200'
                              : 'bg-rose-950/50 border-rose-400/70 text-rose-200'
                          }`}
                        >
                          {item.status === 'ok' ? (
                            <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" />
                          ) : (
                            <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0 mt-0.5" />
                          )}
                          <span>{item.detail}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
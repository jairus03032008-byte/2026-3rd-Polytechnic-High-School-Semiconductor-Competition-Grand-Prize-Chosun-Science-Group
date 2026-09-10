import { useState, useEffect, useRef, useCallback } from "react";
import { initializeApp, getApps } from "firebase/app";
import { getDatabase, ref, onValue, set, off } from "firebase/database";

// ════════════════════════════════════════════════════════════
//  Firebase 설정 — 본인 프로젝트 값으로 교체
// ════════════════════════════════════════════════════════════
const FIREBASE_CONFIG = {
  apiKey:            "YOUR_API_KEY",
  authDomain:        "YOUR_PROJECT.firebaseapp.com",
  databaseURL:       "https://YOUR_PROJECT-default-rtdb.firebaseio.com",
  projectId:         "YOUR_PROJECT",
  storageBucket:     "YOUR_PROJECT.appspot.com",
  messagingSenderId: "YOUR_SENDER_ID",
  appId:             "YOUR_APP_ID",
};

// ── Firebase 훅 ─────────────────────────────────────────────
function useFirebase() {
  const api = useRef({
    db: getApps().length === 0 ? getDatabase(initializeApp(FIREBASE_CONFIG)) : getDatabase(),
    ref, onValue, set, off,
  });
  return { ready: true, api };
}

// ── 색상 맵 (Magma) ─────────────────────────────────────────
function magmaColor(v) {
  const stops = [
    [0,    [0,   0,   4  ]],
    [0.25, [79,  18,  123]],
    [0.5,  [181, 54,  122]],
    [0.75, [251, 139, 74 ]],
    [1.0,  [252, 253, 191]],
  ];
  let lo = stops[0], hi = stops[stops.length - 1];
  for (let i = 0; i < stops.length - 1; i++) {
    if (v >= stops[i][0] && v <= stops[i + 1][0]) {
      lo = stops[i]; hi = stops[i + 1]; break;
    }
  }
  const t = (v - lo[0]) / (hi[0] - lo[0] + 1e-8);
  return lo[1].map((c, i) => Math.round(c + (hi[1][i] - c) * t));
}

// ── 스펙트로그램 캔버스 ─────────────────────────────────────
function SpectrogramCanvas({ data }) {
  const ref = useRef();
  const W = 40, H = 24;   // Python의 image_to_firebase와 일치
  useEffect(() => {
    if (!data?.length || !ref.current) return;
    const ctx = ref.current.getContext("2d");
    const cw = ref.current.width, ch = ref.current.height;
    const pw = cw / W, ph = ch / H;
    for (let r = 0; r < H; r++) {
      for (let c = 0; c < W; c++) {
        // 0~100 int를 0~1 float로 복원
        const raw = data[(H - 1 - r) * W + c] ?? 0;
        const v = raw / 100;
        const [R, G, B] = magmaColor(v);
        ctx.fillStyle = `rgb(${R},${G},${B})`;
        ctx.fillRect(c * pw, r * ph, pw + 0.5, ph + 0.5);
      }
    }
  }, [data]);
  return (
    <canvas ref={ref} width={360} height={150}
      style={{ width: "100%", height: 150, borderRadius: 6, display: "block" }} />
  );
}

// ── 소리 레벨 세그먼트 게이지 ───────────────────────────────
function SoundGauge({ level, isTrain, label = "SOUND LEVEL", color }) {
  const pct = Math.round((level ?? 0) * 100);
  const segs = 12;
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
        <span style={{ fontSize: 10, color: "#64748B", letterSpacing: "0.1em" }}>{label}</span>
        <span style={{ fontSize: 13, fontWeight: 700, color: isTrain ? "#A78BFA" : pct > 60 ? "#FCD34D" : "#34D399", fontFamily: "monospace" }}>
          {pct}%
        </span>
      </div>
      <div style={{ display: "flex", gap: 3, height: 24, alignItems: "flex-end" }}>
        {Array.from({ length: segs }).map((_, i) => {
          const active = level >= (i + 1) / segs;
          const baseCol = color ?? (i < 7 ? "#34D399" : i < 10 ? "#FCD34D" : "#F87171");
          const col = isTrain ? "#A78BFA" : baseCol;
          return (
            <div key={i} style={{
              flex: 1,
              height: `${55 + (i / segs) * 45}%`,
              background: active ? col : "#1E293B",
              borderRadius: "2px 2px 0 0",
              boxShadow: active ? `0 0 5px ${col}66` : "none",
              transition: "background 0.12s",
            }} />
          );
        })}
      </div>
    </div>
  );
}

// ── 점수 히스토리 바 ─────────────────────────────────────────
function ScoreHistory({ history }) {
  const items = [...history].slice(-80);
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 1, height: 60 }}>
      {items.length === 0
        ? <div style={{ color: "#334155", fontSize: 11, margin: "auto" }}>탐지 데이터 대기 중</div>
        : items.map((h, i) => (
          <div key={i} style={{
            flex: 1, minWidth: 2,
            height: `${Math.max(4, (h.score ?? 0) * 100)}%`,
            background: h.is_abnormal ? "#F87171" : "#34D399",
            borderRadius: "2px 2px 0 0",
            opacity: 0.6 + 0.4 * (i / items.length),
            transition: "height 0.3s",
          }} />
        ))
      }
    </div>
  );
}

// ── 실시간 라인 그래프 (이상 점수 + 소리 레벨, 시간축) ───────
function LiveLineChart({ history, threshold = 0.5 }) {
  const W = 640, H = 200;
  const padL = 36, padR = 12, padT = 14, padB = 24;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const items = [...history].slice(-120);
  const n = items.length;

  if (n < 2) {
    return (
      <div style={{
        height: H, display: "flex", alignItems: "center",
        justifyContent: "center", color: "#334155", fontSize: 11,
        border: "1px dashed #1E293B", borderRadius: 6,
      }}>
        데이터가 쌓이면 실시간 그래프가 표시됩니다
      </div>
    );
  }

  const xAt = (i) => padL + (plotW * i) / (n - 1);
  const yAt = (v) => padT + plotH * (1 - Math.max(0, Math.min(1, v)));

  const scoreMicPath = items
    .map((h, i) => `${i === 0 ? "M" : "L"} ${xAt(i).toFixed(1)} ${yAt(h.score_mic ?? 0).toFixed(1)}`)
    .join(" ");

  const scoreVibPath = items
    .map((h, i) => `${i === 0 ? "M" : "L"} ${xAt(i).toFixed(1)} ${yAt(h.score_vib ?? 0).toFixed(1)}`)
    .join(" ");

  // AND 이상(둘 다 이상)인 지점 강조
  const abnormalDots = items
    .map((h, i) => (h.is_abnormal
      ? { x: xAt(i), y: yAt((h.score_mic + h.score_vib) / 2) }
      : null))
    .filter(Boolean);

  const gridLines = [0, 0.25, 0.5, 0.75, 1.0];

  // 최근 시각 라벨 (가장 오래된 것 / 가장 최근 것)
  const fmtTime = (ts) => {
    if (!ts) return "";
    const d = new Date(ts);
    return d.toLocaleTimeString("ko-KR", { hour12: false, minute: "2-digit", second: "2-digit" });
  };
  const firstTs = items[0]?.timestamp;
  const lastTs  = items[n - 1]?.timestamp;

  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block" }}>
      {/* 격자선 */}
      {gridLines.map((g) => (
        <g key={g}>
          <line
            x1={padL} x2={W - padR}
            y1={yAt(g)} y2={yAt(g)}
            stroke="#1A2540" strokeWidth="1"
          />
          <text x={padL - 6} y={yAt(g)} textAnchor="end" dominantBaseline="middle"
            fontSize="9" fill="#475569" fontFamily="monospace">
            {g.toFixed(2)}
          </text>
        </g>
      ))}

      {/* 임계값 점선 */}
      <line
        x1={padL} x2={W - padR}
        y1={yAt(threshold)} y2={yAt(threshold)}
        stroke="#F59E0B" strokeWidth="1" strokeDasharray="4,3" opacity="0.7"
      />
      <text x={W - padR} y={yAt(threshold) - 5} textAnchor="end"
        fontSize="9" fill="#F59E0B" fontFamily="monospace">
        임계 {threshold}
      </text>

      {/* 마이크 점수 라인 (파랑) */}
      <path d={scoreMicPath} fill="none" stroke="#60A5FA" strokeWidth="2" />

      {/* 진동 점수 라인 (노랑) */}
      <path d={scoreVibPath} fill="none" stroke="#FCD34D" strokeWidth="2" />

      {/* AND 이상 감지된 지점 강조 점 (빨강) */}
      {abnormalDots.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r="4" fill="#F87171" stroke="#080C14" strokeWidth="1.5" />
      ))}

      {/* 시간 라벨 */}
      <text x={padL} y={H - 6} fontSize="9" fill="#334155" fontFamily="monospace">
        {fmtTime(firstTs)}
      </text>
      <text x={W - padR} y={H - 6} textAnchor="end" fontSize="9" fill="#334155" fontFamily="monospace">
        {fmtTime(lastTs)}
      </text>
    </svg>
  );
}

// ── 학습 진행 바 ─────────────────────────────────────────────
function TrainProgress({ status }) {
  if (!status) return null;
  const pct = Math.min(100, Math.round((status.count / status.required) * 100));
  return (
    <div style={{
      background: "#0F1623", border: "1px solid #2D1F5E",
      borderRadius: 10, padding: "14px 18px", marginBottom: 14,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}>
        <span style={{ fontSize: 11, color: "#A78BFA", fontWeight: 700, letterSpacing: "0.08em" }}>
          ◉ 학습 모드 — 정상 데이터 수집 중
        </span>
        <span style={{ fontSize: 11, color: "#7C3AED", fontFamily: "monospace" }}>
          {status.count} / {status.required}
        </span>
      </div>
      <div style={{ background: "#1E293B", borderRadius: 4, height: 8, overflow: "hidden" }}>
        <div style={{
          height: "100%", borderRadius: 4,
          width: `${pct}%`,
          background: status.done
            ? "linear-gradient(90deg, #34D399, #6EE7B7)"
            : "linear-gradient(90deg, #7C3AED, #A78BFA)",
          transition: "width 0.4s",
        }} />
      </div>
      <div style={{ fontSize: 10, color: "#6D28D9", marginTop: 8 }}>
        {status.message}
      </div>
    </div>
  );
}

// ── 모드 전환 버튼 ───────────────────────────────────────────
function ModeSwitch({ mode, onSwitch, hasModel }) {
  const isTrain = mode === "TRAIN";
  return (
    <div style={{
      display: "flex", gap: 0,
      background: "#0A0F1A", border: "1px solid #1E293B",
      borderRadius: 10, padding: 4,
    }}>
      {["TRAIN", "DETECT"].map(m => {
        const active = mode === m;
        const disabled = m === "DETECT" && !hasModel && mode !== "DETECT";
        return (
          <button key={m}
            onClick={() => !disabled && onSwitch(m)}
            style={{
              flex: 1, padding: "10px 20px",
              background: active
                ? m === "TRAIN" ? "#2D1F5E" : "#0C2A1E"
                : "transparent",
              border: active
                ? `1px solid ${m === "TRAIN" ? "#7C3AED" : "#059669"}`
                : "1px solid transparent",
              borderRadius: 8,
              color: active
                ? m === "TRAIN" ? "#A78BFA" : "#34D399"
                : disabled ? "#1E293B" : "#475569",
              fontSize: 11, fontWeight: 700,
              fontFamily: "inherit",
              cursor: disabled ? "not-allowed" : "pointer",
              letterSpacing: "0.1em",
              transition: "all 0.2s",
            }}
          >
            {m === "TRAIN" ? "◈ TRAIN" : "◉ DETECT"}
            {m === "DETECT" && !hasModel && (
              <div style={{ fontSize: 9, fontWeight: 400, marginTop: 2, color: "#334155" }}>
                모델 필요
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}

// ── 연결 배지 ────────────────────────────────────────────────
function ConnBadge({ connected, age }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 6,
      background: "#111827", border: "1px solid #1E293B",
      borderRadius: 20, padding: "4px 12px",
    }}>
      <div style={{
        width: 7, height: 7, borderRadius: "50%",
        background: connected ? "#34D399" : "#475569",
        boxShadow: connected ? "0 0 6px #34D39988" : "none",
      }} />
      <span style={{ fontSize: 10, color: "#475569", fontFamily: "monospace" }}>
        {connected ? `LIVE · ${age}s ago` : "대기중"}
      </span>
    </div>
  );
}

// ════════════════════════════════════════════════════════════
//  메인 대시보드
// ════════════════════════════════════════════════════════════
export default function SonicExpertDashboard() {
  const { ready, api } = useFirebase();

  const [mode,        setMode]        = useState("DETECT");
  const [latest,      setLatest]      = useState(null);
  const [trainStatus, setTrainStatus] = useState(null);
  const [history,     setHistory]     = useState([]);
  const [alerts,      setAlerts]      = useState([]);
  const [hasModel,    setHasModel]    = useState(false);
  const [connected,   setConnected]   = useState(false);
  const [lastUpdate,  setLastUpdate]  = useState(null);
  const [cmdMsg,      setCmdMsg]      = useState("");

  // ── Firebase 구독 ─────────────────────────────────────────
  useEffect(() => {
    if (!ready) return;
    const { db, ref, onValue, off } = api.current;

    const refs = {
      latest:  ref(db, "sonic_expert/latest"),
      mode:    ref(db, "sonic_expert/mode"),
      train:   ref(db, "sonic_expert/train_status"),
      history: ref(db, "sonic_expert/history"),
      alerts:  ref(db, "sonic_expert/alerts"),
    };

    onValue(refs.latest, snap => {
      const v = snap.val();
      if (v) { setLatest(v); setConnected(true); setLastUpdate(Date.now()); }
    });

    onValue(refs.mode, snap => {
      const v = snap.val();
      if (v) setMode(v);
    });

    onValue(refs.train, snap => {
      const v = snap.val();
      setTrainStatus(v);
      if (v?.done) setHasModel(true);
    });

    onValue(refs.history, snap => {
      const v = snap.val();
      if (v) {
        const arr = Object.values(v)
          .sort((a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0))
          .slice(-80);
        setHistory(arr);
        setHasModel(true);  // 탐지 기록이 있으면 모델 있음
      }
    });

    onValue(refs.alerts, snap => {
      const v = snap.val();
      if (v) {
        setAlerts(Object.values(v).sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0)).slice(0, 50));
      }
    });

    const tick = setInterval(() => {
      setLastUpdate(prev => {
        if (prev && Date.now() - prev > 30000) setConnected(false);
        return prev;
      });
    }, 5000);

    return () => {
      Object.values(refs).forEach(r => off(r));
      clearInterval(tick);
    };
  }, [ready]);

  // ── 모드 전환 ─────────────────────────────────────────────
  const switchMode = useCallback(async (newMode) => {
    if (!ready) return;
    const { db, ref, set } = api.current;
    try {
      await set(ref(db, "sonic_expert/mode"), newMode);
      setCmdMsg(`${newMode} 모드로 전환 요청`);
      setTimeout(() => setCmdMsg(""), 2000);
    } catch (e) {
      setCmdMsg("전환 실패");
    }
  }, [ready]);

  // ── 릴레이 수동 제어 (자동 차단 없음 — 사용자가 직접 ON/OFF) ─
  const sendRelayCommand = useCallback(async (cmd) => {
    if (!ready) return;
    const { db, ref, set } = api.current;
    try {
      // 타임스탬프를 포함해 매번 값이 바뀌게 함
      // (같은 명령을 반복해도 Firebase가 변화로 감지 → Python이 항상 반응)
      await set(ref(db, "sonic_expert/relay_command"), { cmd, ts: Date.now() });
      setCmdMsg(cmd === "OFF" ? "릴레이 차단 요청" : "릴레이 재가동 요청");
      setTimeout(() => setCmdMsg(""), 2000);
    } catch (e) {
      setCmdMsg("명령 전송 실패");
    }
  }, [ready]);

  // 파생 상태
  const isTrain  = mode === "TRAIN";
  const isAb     = latest?.is_abnormal      ?? false;
  const isAbMic  = latest?.is_abnormal_mic  ?? false;
  const isAbVib  = latest?.is_abnormal_vib  ?? false;
  const scoreMic = latest?.score_mic        ?? 0;
  const scoreVib = latest?.score_vib        ?? 0;
  const score    = latest?.score            ?? 0;
  const micLevel = latest?.mic_level        ?? 0;
  const vibLevel = latest?.vib_level        ?? 0;
  const label    = latest?.label            ?? "대기중";
  const specMic  = latest?.spectrogram_mic  ?? [];
  const specVib  = latest?.spectrogram_vib  ?? [];
  const relayOn  = latest?.relay_on         ?? true;

  const abRate = history.length
    ? ((history.filter(h => h.is_abnormal).length / history.length) * 100).toFixed(1)
    : "0.0";

  const ageS = lastUpdate ? Math.round((Date.now() - lastUpdate) / 1000) : 0;

  return (
    <div style={{
      minHeight: "100vh", background: "#080C14",
      color: "#CBD5E1", padding: "18px 16px",
      fontFamily: "'DM Mono','Fira Code','Courier New',monospace",
      boxSizing: "border-box",
    }}>

      {/* ══ 헤더 ══ */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 16 }}>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, letterSpacing: "0.1em", color: "#F1F5F9" }}>
            SONIC<span style={{ color: "#38BDF8" }}>·</span>EXPERT
          </div>
          <div style={{ fontSize: 9, color: "#334155", letterSpacing: "0.2em", marginTop: 2 }}>
            ANOMALY DETECTION · FIREBASE REALTIME
          </div>
        </div>
        <ConnBadge connected={connected} age={ageS} />
      </div>

      {/* ══ 릴레이 차단 배너 (수동 차단 시에만 표시) ══ */}
      {!relayOn && (
        <div style={{
          background: "#2D0A0A", border: "1px solid #DC2626",
          borderRadius: 10, padding: "12px 16px", marginBottom: 14,
          display: "flex", alignItems: "center", justifyContent: "space-between",
        }}>
          <div>
            <div style={{ fontSize: 13, fontWeight: 700, color: "#FCA5A5", letterSpacing: "0.05em" }}>
              ⚡ 릴레이 차단 — 모터 전원 OFF
            </div>
            <div style={{ fontSize: 10, color: "#EF4444", marginTop: 2 }}>
              수동으로 차단됨 (자동 차단 아님)
            </div>
          </div>
          <button
            onClick={() => sendRelayCommand("ON")}
            style={{
              background: "#1D4ED8", color: "#fff", border: "none",
              borderRadius: 8, padding: "8px 14px", fontSize: 11,
              fontFamily: "inherit", cursor: "pointer", fontWeight: 600,
              letterSpacing: "0.04em",
            }}
          >
            ▶ 재가동
          </button>
        </div>
      )}

      {/* ══ 모드 전환 + 릴레이 수동 제어 (차단·동작 항상 표시) ══ */}
      <div style={{ display: "flex", gap: 8, marginBottom: 14, alignItems: "stretch" }}>
        <div style={{ flex: 1 }}>
          <ModeSwitch mode={mode} onSwitch={switchMode} hasModel={hasModel} />
        </div>

        {/* 릴레이 동작 (ON) */}
        <button
          onClick={() => sendRelayCommand("ON")}
          disabled={relayOn}
          style={{
            padding: "0 16px",
            background: relayOn ? "#052E16" : "#0C2A1E",
            border: `1px solid ${relayOn ? "#14532D" : "#059669"}`,
            borderRadius: 10,
            color: relayOn ? "#166534" : "#34D399",
            fontSize: 11, fontWeight: 700, letterSpacing: "0.06em",
            fontFamily: "inherit",
            cursor: relayOn ? "not-allowed" : "pointer",
            whiteSpace: "nowrap",
            opacity: relayOn ? 0.45 : 1,
            transition: "all 0.2s",
          }}
        >
          ▶ 릴레이 동작
        </button>

        {/* 릴레이 차단 (OFF) */}
        <button
          onClick={() => sendRelayCommand("OFF")}
          disabled={!relayOn}
          style={{
            padding: "0 16px",
            background: !relayOn ? "#2D0A0A" : "#1A0A0A",
            border: `1px solid ${!relayOn ? "#DC2626" : "#7F1D1D"}`,
            borderRadius: 10,
            color: !relayOn ? "#F87171" : "#EF4444",
            fontSize: 11, fontWeight: 700, letterSpacing: "0.06em",
            fontFamily: "inherit",
            cursor: !relayOn ? "not-allowed" : "pointer",
            whiteSpace: "nowrap",
            opacity: !relayOn ? 0.45 : 1,
            transition: "all 0.2s",
          }}
        >
          ■ 릴레이 차단
        </button>
      </div>
      {cmdMsg && (
        <div style={{ fontSize: 10, color: "#34D399", marginTop: -8, marginBottom: 14, textAlign: "center" }}>
          {cmdMsg}
        </div>
      )}

      {/* ══ 학습 진행 바 (TRAIN 모드일 때만) ══ */}
      {isTrain && <TrainProgress status={trainStatus} />}

      {/* ══ 상태 배너 (DETECT 모드) ══ */}
      {!isTrain && (
        <div style={{
          background: !relayOn ? "#1A0A0A" : isAb ? "#2D0A0A" : "#041A0E",
          border: `1px solid ${!relayOn ? "#7F1D1D" : isAb ? "#DC2626" : "#059669"}`,
          borderRadius: 10, padding: "14px 18px",
          textAlign: "center", marginBottom: 14,
          transition: "all 0.4s",
        }}>
          <div style={{
            fontSize: 16, fontWeight: 700, letterSpacing: "0.12em",
            color: !relayOn ? "#FCA5A5" : isAb ? "#F87171" : "#6EE7B7",
          }}>
            {!relayOn ? "■ MOTOR STOPPED (수동)"
              : isAb  ? "⚠ ANOMALY DETECTED"
              :         "● NORMAL OPERATION"}
          </div>
          {latest && (
            <div style={{ fontSize: 10, color: "#475569", marginTop: 4 }}>
              {label}
              {isAb && relayOn && " · 경고만 표시 — 모터는 계속 작동 중"}
            </div>
          )}
        </div>
      )}

      {/* ══ 지표 카드 (DETECT 모드) — 마이크/진동 분리 표시 ══ */}
      {!isTrain && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 8, marginBottom: 14 }}>
          {[
            { label: "MIC SCORE",   value: scoreMic.toFixed(3), color: isAbMic ? "#F87171" : "#34D399", sub: isAbMic ? "소리 이상" : "정상" },
            { label: "VIB SCORE",   value: scoreVib.toFixed(3), color: isAbVib ? "#F87171" : "#34D399", sub: isAbVib ? "진동 이상" : "정상" },
            { label: "AND VERDICT", value: isAb ? "⚠ ABNORMAL" : "● NORMAL", color: isAb ? "#F87171" : "#34D399", sub: "둘 다 이상일 때 알람" },
            { label: "TOTAL ALERTS",value: alerts.length, color: "#FCD34D", sub: alerts[0]?.time ?? "없음" },
          ].map(m => (
            <div key={m.label} style={{
              background: "#0F1623", border: "1px solid #1A2540",
              borderRadius: 10, padding: "12px 14px",
            }}>
              <div style={{ fontSize: 9, color: "#475569", letterSpacing: "0.1em", marginBottom: 5 }}>{m.label}</div>
              <div style={{ fontSize: m.label === "AND VERDICT" ? 14 : 22, fontWeight: 700, color: m.color }}>{m.value}</div>
              <div style={{ fontSize: 9, color: "#334155", marginTop: 3 }}>{m.sub}</div>
            </div>
          ))}
        </div>
      )}

      {/* ══ 스펙트로그램 두 개 나란히 ══ */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 14 }}>

        {/* 마이크 스펙트로그램 */}
        <div style={{ background: "#0F1623", border: `1px solid ${isAbMic ? "#7F1D1D" : "#1A2540"}`, borderRadius: 10, padding: 14 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 }}>
            <span style={{ fontSize: 9, color: "#475569", letterSpacing: "0.1em" }}>
              🎤 MIC SPECTROGRAM
              {isTrain && <span style={{ color: "#7C3AED", marginLeft: 8 }}>◈ COLLECTING</span>}
            </span>
            {!isTrain && (
              <span style={{ fontSize: 9, color: isAbMic ? "#F87171" : "#34D399" }}>
                {isAbMic ? "● 이상" : "● 정상"}
              </span>
            )}
          </div>
          {specMic.length > 0
            ? <SpectrogramCanvas data={specMic} />
            : (
              <div style={{ height: 150, display: "flex", alignItems: "center", justifyContent: "center", color: "#334155", fontSize: 11, border: "1px dashed #1E293B", borderRadius: 6 }}>
                {connected ? "신호 처리 중..." : "Firebase 연결 대기 중"}
              </div>
            )
          }
          <SoundGauge level={micLevel} isTrain={isTrain} label="SOUND LEVEL" color="#60A5FA" />
        </div>

        {/* 진동 스펙트로그램 */}
        <div style={{ background: "#0F1623", border: `1px solid ${isAbVib ? "#7F1D1D" : "#1A2540"}`, borderRadius: 10, padding: 14 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 }}>
            <span style={{ fontSize: 9, color: "#475569", letterSpacing: "0.1em" }}>
              📳 VIBRATION SPECTROGRAM
              {isTrain && <span style={{ color: "#7C3AED", marginLeft: 8 }}>◈ COLLECTING</span>}
            </span>
            {!isTrain && (
              <span style={{ fontSize: 9, color: isAbVib ? "#F87171" : "#34D399" }}>
                {isAbVib ? "● 이상" : "● 정상"}
              </span>
            )}
          </div>
          {specVib.length > 0
            ? <SpectrogramCanvas data={specVib} />
            : (
              <div style={{ height: 150, display: "flex", alignItems: "center", justifyContent: "center", color: "#334155", fontSize: 11, border: "1px dashed #1E293B", borderRadius: 6 }}>
                {connected ? "신호 처리 중..." : "Firebase 연결 대기 중"}
              </div>
            )
          }
          <SoundGauge level={vibLevel} isTrain={isTrain} label="VIB LEVEL" color="#FCD34D" />
        </div>
      </div>

      {/* ══ 학습 진행 상황 (TRAIN 모드일 때만) ══ */}
      {isTrain && trainStatus && (
        <div style={{ background: "#0F1623", border: "1px solid #1A2540", borderRadius: 10, padding: 14, marginBottom: 14, textAlign: "center" }}>
          <div style={{ fontSize: 32, fontWeight: 700, color: "#A78BFA" }}>
            {trainStatus.count} / {trainStatus.required}
          </div>
          <div style={{ fontSize: 10, color: "#6D28D9", marginTop: 4 }}>
            수집된 정상 샘플 (마이크 + 진동 동시 학습)
          </div>
        </div>
      )}

      {/* 그래프 영역 시작 */}

      {/* ══ 실시간 그래프 (이상 점수 + 소리 레벨) ══ */}
      {!isTrain && (
        <div style={{
          background: "#0F1623", border: "1px solid #1A2540",
          borderRadius: 10, padding: 14, marginBottom: 14,
        }}>
          <div style={{
            display: "flex", justifyContent: "space-between",
            alignItems: "baseline", marginBottom: 10,
          }}>
            <span style={{ fontSize: 9, color: "#475569", letterSpacing: "0.1em" }}>
              LIVE GRAPH — MIC & VIBRATION SCORES
            </span>
            <div style={{ display: "flex", gap: 12, fontSize: 9 }}>
              <span style={{ color: "#60A5FA" }}>━ 소리 점수</span>
              <span style={{ color: "#FCD34D" }}>━ 진동 점수</span>
              <span style={{ color: "#F87171" }}>● 이상 (AND)</span>
            </div>
          </div>
          <LiveLineChart history={history} threshold={0.5} />
        </div>
      )}

      {/* ══ 알람 로그 (DETECT 모드) ══ */}
      {!isTrain && (
        <div style={{ background: "#0F1623", border: "1px solid #1A2540", borderRadius: 10, padding: 14 }}>
          <div style={{ fontSize: 9, color: "#475569", letterSpacing: "0.1em", marginBottom: 10 }}>
            ALERT LOG ({alerts.length})
          </div>
          <div style={{ maxHeight: 160, overflowY: "auto" }}>
            {alerts.length === 0
              ? <div style={{ color: "#334155", fontSize: 11, textAlign: "center", padding: 16 }}>이상 감지 기록 없음</div>
              : alerts.map((a, i) => (
                <div key={i} style={{
                  display: "flex", justifyContent: "space-between",
                  padding: "6px 10px", borderRadius: 6, marginBottom: 3,
                  background: "#0D0A1A", borderLeft: "2px solid #F87171",
                  fontSize: 10,
                }}>
                  <span style={{ color: "#64748B" }}>{a.time}</span>
                  <span style={{ color: "#F87171", fontWeight: 700 }}>
                    score {typeof a.score === "number" ? a.score.toFixed(3) : a.score}
                  </span>
                </div>
              ))
            }
          </div>
        </div>
      )}

      <style>{`
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: #080C14; }
        ::-webkit-scrollbar-thumb { background: #1E293B; border-radius: 4px; }
      `}</style>
    </div>
  );
}

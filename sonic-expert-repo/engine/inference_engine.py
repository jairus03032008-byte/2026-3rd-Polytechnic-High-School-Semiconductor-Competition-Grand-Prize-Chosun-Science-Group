# ============================================================
#  Sonic-Expert | inference_engine.py
#
#  아키텍처:
#    Arduino ──시리얼──▶ Python ──Firebase──▶ 웹앱
#
#  모드 (웹앱에서 Firebase 경유로 전환):
#    TRAIN  : 정상 데이터 수집 → Isolation Forest 학습 → 모델 저장
#    DETECT : 학습된 모델로 실시간 이상 탐지 → Firebase 업로드
#
#  Firebase DB 구조:
#    /sonic_expert/
#      mode          ← "TRAIN" | "DETECT"  (웹앱이 씀, Python이 폴링)
#      latest/       ← 최신 탐지 결과 (웹앱 실시간 구독)
#      train_status/ ← 학습 진행 상황 (샘플 수, 완료 여부)
#      history/      ← 탐지 결과 히스토리
#      alerts/       ← 이상 감지 로그
#
#  실행:
#    python inference_engine.py --port COM3
#    python inference_engine.py --demo     # 아두이노 없이 테스트
#
#  필요 패키지:
#    pip install firebase-admin pyserial numpy scipy librosa joblib pillow scikit-learn
# ============================================================

import argparse
import os
import time
import threading
import warnings
import numpy as np
from collections import deque
from scipy.signal import butter, filtfilt

# librosa 등에서 나오는 UserWarning 억제 (콘솔 출력 폭주로 인한 지연 방지)
warnings.filterwarnings("ignore")

import firebase_admin
from firebase_admin import credentials, db as firebase_db

# ── Firebase 설정 ────────────────────────────────────────────
FIREBASE_DATABASE_URL   = "https://YOUR_PROJECT-default-rtdb.firebaseio.com"
FIREBASE_CREDENTIAL_PATH = "serviceAccountKey.json"

# ── 신호 처리 설정 ───────────────────────────────────────────
SAMPLE_RATE   = 100
WINDOW_SEC    = 0.4                        # 0.4초 윈도우 (속도-정확도 균형)
WINDOW_SAMPS  = int(SAMPLE_RATE * WINDOW_SEC)   # 40 샘플
LOWCUT        = 1.0
HIGHCUT       = 45.0
IMG_SIZE      = 128
THRESHOLD_IF  = -0.6      # 이 값보다 낮으면 이상 (낮출수록 덜 예민)

# 학습에 필요한 최소 정상 샘플 수
TRAIN_MIN_SAMPLES = 1000

# 레벨 기반 정상 범위 산출 시 표준편차 배수 (μ ± LEVEL_SIGMA·σ)
# 값이 클수록 정상 범위가 넓어져서 덜 예민함
LEVEL_SIGMA = 4.0
# 레벨 하한을 "정상 평균의 몇 배"로도 제한 (진동처럼 절대값이 작을 때 대비)
# 예: 0.4 → 정상 평균의 40% 밑으로 떨어지면 이상으로 판정
LEVEL_DROP_RATIO = 0.4

# Firebase 업로드 주기 (초)
# latest: 웹앱 반응 속도 직결. 멜 변환이 1.5ms로 빨라져 여유가 생겼으므로
#         0.12초(초당 ~8회)까지 당김. 그 이상은 Firebase 적체 위험.
LATEST_INTERVAL   = 0.12
HISTORY_INTERVAL  = 1.5     # history 묶음 전송 간격 (그래프용, 느려도 무방)
HISTORY_LIMIT     = 300

# Firebase 자동 청소 설정
HISTORY_MAX_KEEP         = 500   # /history 노드에 유지할 최대 항목 수
ALERTS_MAX_KEEP          = 200   # /alerts 노드에 유지할 최대 항목 수
HISTORY_CLEANUP_INTERVAL = 50    # 50개 업로드마다 청소 실행
ALERTS_CLEANUP_EVERY     = 20    # 20개 알람마다 청소 실행

# 모델 저장 경로 (마이크/진동 분리)
MODEL_DIR       = "models"
MODEL_PATH_MIC  = os.path.join(MODEL_DIR, "isolation_forest_mic.pkl")
MODEL_PATH_VIB  = os.path.join(MODEL_DIR, "isolation_forest_vib.pkl")


# ── 밴드패스 필터 ────────────────────────────────────────────
def bandpass_filter(data, lowcut=LOWCUT, highcut=HIGHCUT,
                    fs=SAMPLE_RATE, order=4):
    nyq  = fs / 2.0
    b, a = butter(order, [lowcut / nyq, highcut / nyq], btype="band")
    return filtfilt(b, a, np.array(data, dtype=float))


# 멜 필터뱅크 캐시 (window_to_images에서 재사용)
_MEL_BASIS_CACHE = {"key": None, "basis": None}


# ── 윈도우 → 소리·진동 스펙트로그램 두 개 반환 ──────────────
def window_to_images(window: np.ndarray):
    """소리와 진동 각각의 멜 스펙트로그램 두 개를 따로 반환.
    반환: (mic_img 128×128 float32, vib_img 128×128 float32)"""
    import librosa
    from PIL import Image as PILImage

    # 마이크 채널
    mic = window[:, 0].astype(float)
    mic = (mic - mic.mean()) / (mic.std() + 1e-8)
    mic = bandpass_filter(mic).astype(np.float32)

    # 진동 채널 (XYZ 가속도의 벡터 크기)
    accel = window[:, 1:4].astype(float) / 16384.0
    vib   = np.linalg.norm(accel, axis=1)
    vib   = (vib - vib.mean()) / (vib.std() + 1e-8)
    vib   = bandpass_filter(vib).astype(np.float32)

    sig_len    = len(mic)
    n_fft      = min(512, 2 ** int(np.floor(np.log2(sig_len))))
    hop_length = max(1, n_fft // 4)
    n_mels     = min(IMG_SIZE, n_fft // 2)

    # 멜 필터뱅크는 파라미터가 고정이므로 한 번만 만들어 캐싱 (매번 생성 시 6배 느림)
    global _MEL_BASIS_CACHE
    cache_key = (n_fft, n_mels)
    if _MEL_BASIS_CACHE.get("key") != cache_key:
        _MEL_BASIS_CACHE["key"]   = cache_key
        _MEL_BASIS_CACHE["basis"] = librosa.filters.mel(
            sr=SAMPLE_RATE, n_fft=n_fft, n_mels=n_mels, fmax=HIGHCUT)
    mel_basis = _MEL_BASIS_CACHE["basis"]

    def _to_image(signal):
        # STFT 파워 → 캐시된 멜 필터 적용 (librosa.melspectrogram보다 빠름)
        S      = np.abs(librosa.stft(signal, n_fft=n_fft, hop_length=hop_length)) ** 2
        mel    = mel_basis @ S
        mel_db = librosa.power_to_db(mel, ref=np.max)
        # 고정 dB 범위(-80~0)로 정규화 → 매 프레임 대비가 일정하게 유지됨
        raw    = np.clip((mel_db + 80.0) / 80.0, 0.0, 1.0)
        pil    = PILImage.fromarray((raw * 255).astype(np.uint8))
        pil    = pil.resize((IMG_SIZE, IMG_SIZE), PILImage.BILINEAR)
        return np.array(pil).astype(np.float32) / 255.0

    return _to_image(mic), _to_image(vib)


# ── 스펙트로그램 → Firebase 직렬화 (40×24 축소, 정수 0~100) ─
def image_to_firebase(img: np.ndarray) -> list:
    """스펙트로그램을 Firebase에 올릴 작은 리스트로 변환.
    트래픽 절약을 위해 40×24 해상도 + 0~100 정수로 인코딩."""
    from PIL import Image as PILImage
    pil = PILImage.fromarray((img * 255).astype(np.uint8))
    pil = pil.resize((40, 24), PILImage.BILINEAR)
    # 0~1 float 대신 0~100 int로 → 문자열 길이 절반 이하
    return (np.array(pil).astype(int) * 100 // 255).flatten().tolist()


# ── 마이크 RMS 레벨 (0~1) ────────────────────────────────────
def compute_mic_level(window: np.ndarray) -> float:
    """마이크 신호의 피크 진폭을 0~1로 정규화.
    DC 오프셋 제거 후 절댓값 상위 20%의 평균을 사용 (RMS보다 피크에 반응).
    분모를 크게 잡아 일반 동작 시 70% 이하로 머물도록 조정."""
    mic = window[:, 0].astype(float)
    mic = mic - mic.mean()                       # DC 오프셋 제거
    amp = np.abs(mic)
    # 상위 20% 샘플의 평균 = 피크 진폭의 안정적 추정치
    k = max(1, int(len(amp) * 0.2))
    peak = float(np.mean(np.sort(amp)[-k:]))
    # 분모를 충분히 크게 — 일반 동작 시 ~40%, 시끄러울 때 80% 정도가 되도록
    return float(np.clip(peak / 450.0, 0.0, 1.0))


# ── 진동 RMS 레벨 (0~1) ──────────────────────────────────────
def compute_vib_level(window: np.ndarray) -> float:
    """가속도 XYZ 벡터 크기의 피크 변동을 0~1로 정규화."""
    accel = window[:, 1:4].astype(float) / 16384.0
    mag   = np.linalg.norm(accel, axis=1)
    mag   = mag - mag.mean()
    amp   = np.abs(mag)
    k     = max(1, int(len(amp) * 0.2))
    peak  = float(np.mean(np.sort(amp)[-k:]))
    return float(np.clip(peak / 0.8, 0.0, 1.0))


# ════════════════════════════════════════════════════════════
#  메인 엔진
# ════════════════════════════════════════════════════════════
class SonicEngine:

    def __init__(self, port="COM3", baud=115200):
        self.port = port
        self.baud = baud

        # 현재 모드: "TRAIN" | "DETECT"
        self.mode  = "DETECT"

        # 마이크/진동 각각의 모델과 정규화 파라미터
        self.model_mic = None
        self.model_vib = None
        self._score_min_mic = -0.5
        self._score_max_mic =  0.0
        self._score_min_vib = -0.5
        self._score_max_vib =  0.0

        # 릴레이 상태 (Arduino가 source-of-truth, Python은 미러)
        # 자동 차단 없음 — 웹앱의 수동 명령으로만 변경됨
        self.relay_on = True

        # 학습 데이터 버퍼 (마이크/진동 분리)
        self._train_features_mic = []
        self._train_features_vib = []
        # 레벨 누적 버퍼 (학습 분포 기반 정상 범위 산출용)
        self._train_levels_mic   = []
        self._train_levels_vib   = []

        # 레벨 기반 정상 범위 (학습 후 채워짐, 미학습 시 None)
        self._level_min_mic = None
        self._level_max_mic = None
        self._level_min_vib = None
        self._level_max_vib = None

        # 최신 결과 (두 채널 점수 + AND 판정)
        self.latest_result = {
            "mode":            "DETECT",
            "label":           "대기중",
            "score_mic":       0.0,
            "score_vib":       0.0,
            "score":           0.0,    # 두 점수의 평균 (참고용)
            "mic_level":       0.0,
            "vib_level":       0.0,
            "is_abnormal_mic": False,
            "is_abnormal_vib": False,
            "is_abnormal":     False,  # AND: 둘 다 이상일 때만
            "relay_on":        True,
            "timestamp":       int(time.time() * 1000),
            "spectrogram_mic": [],
            "spectrogram_vib": [],
        }

        self.result_history = deque(maxlen=HISTORY_LIMIT)
        self.alert_log      = deque(maxlen=200)

        self._ser     = None
        self._running = False
        self._thread  = None

        # Firebase
        self._fb_db          = None
        self._last_fb_upload = 0.0
        self._mode_ref       = None   # Firebase mode 노드 리스너
        self._history_count  = 0      # 히스토리 청소 카운터
        self._alerts_count   = 0      # 알람 청소 카운터

        # 비동기 업로드 (네트워크 지연이 추론을 막지 않게)
        self._pending_latest  = None  # 가장 최신 latest 페이로드 (덮어쓰기)
        self._history_queue   = deque(maxlen=20)  # 히스토리 업로드 대기열
        self._upload_lock     = threading.Lock()
        self._latest_thread   = None
        self._history_thread  = None
        self._init_firebase()
        # _init_firebase에서 자동 TRAIN 모드로 진입함
        # (기존 모델 파일은 무시하고 항상 재학습)

    # ── Firebase 초기화 + 모드 리스너 ───────────────────────
    def _init_firebase(self):
        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(FIREBASE_CREDENTIAL_PATH)
                firebase_admin.initialize_app(cred, {
                    "databaseURL": FIREBASE_DATABASE_URL
                })
            self._fb_db = firebase_db
            print("[Firebase] 연결 성공")

            # Python 시작 시 항상 TRAIN 모드로 강제 진입 (재학습)
            self.mode = "TRAIN"
            self._train_features_mic = []
            self._train_features_vib = []
            self._train_levels_mic   = []
            self._train_levels_vib   = []
            self._fb_db.reference("sonic_expert/mode").set("TRAIN")
            self._upload_train_status(0, False,
                f"학습 시작 — 정상 데이터 수집 중... (0/{TRAIN_MIN_SAMPLES})")
            print(f"[모드] 자동 TRAIN 진입 — {TRAIN_MIN_SAMPLES}개 샘플 수집 필요")

            # 웹앱이 모드를 바꾸면 Python이 즉시 반응
            self._mode_ref = self._fb_db.reference("sonic_expert/mode")
            self._mode_ref.listen(self._on_mode_change)

            # 웹앱이 릴레이 수동 명령을 보내면 Python이 즉시 반응
            self._relay_cmd_ref = self._fb_db.reference("sonic_expert/relay_command")
            self._relay_cmd_ref.listen(self._on_relay_command)

        except Exception as e:
            print(f"[Firebase] 연결 실패: {e}")
            self._fb_db = None

    def _on_relay_command(self, event):
        """웹앱이 /relay_command 에 명령을 쓰면 호출됨.
        {cmd, ts} 형태(권장) 또는 'ON'/'OFF' 문자열 모두 처리.
        타임스탬프가 포함되면 같은 명령을 반복해도 항상 반응함."""
        data = event.data
        if data is None:
            return
        if isinstance(data, dict):
            cmd = data.get("cmd")
        else:
            cmd = data

        if cmd == "OFF":
            self._send_serial_cmd("RELAY:OFF")
            self.relay_on = False
            print("[릴레이] 수동 차단 명령 수신")
        elif cmd == "ON":
            self._send_serial_cmd("RELAY:ON")
            self.relay_on = True
            print("[릴레이] 수동 재가동 명령 수신")

    def _send_serial_cmd(self, cmd: str):
        try:
            if self._ser and self._ser.is_open:
                self._ser.write(f"{cmd}\n".encode())
        except Exception as e:
            print(f"[명령 전송 오류] {cmd}: {e}")

    def _on_mode_change(self, event):
        """Firebase /mode 값이 바뀌면 호출됨"""
        new_mode = event.data
        if new_mode in ("TRAIN", "DETECT") and new_mode != self.mode:
            print(f"[모드 전환] {self.mode} → {new_mode}")
            self.mode = new_mode
            self._send_serial_cmd(f"MODE:{new_mode}")  # Arduino LED 표시 갱신

            if new_mode == "TRAIN":
                self._train_features_mic = []
                self._train_features_vib = []
                self._train_levels_mic   = []
                self._train_levels_vib   = []
                self._upload_train_status(0, False, "학습 데이터 수집 중...")
            elif new_mode == "DETECT":
                self._load_models_if_exist()

    # ── 모델 로드 (마이크/진동 두 개) ────────────────────────
    def _load_models_if_exist(self):
        import joblib
        loaded = []
        for path, attr_model, attr_smin, attr_smax, attr_lmin, attr_lmax in [
            (MODEL_PATH_MIC, "model_mic", "_score_min_mic", "_score_max_mic",
                "_level_min_mic", "_level_max_mic"),
            (MODEL_PATH_VIB, "model_vib", "_score_min_vib", "_score_max_vib",
                "_level_min_vib", "_level_max_vib"),
        ]:
            if not os.path.exists(path):
                continue
            try:
                payload = joblib.load(path)
                setattr(self, attr_model, payload["model"])
                setattr(self, attr_smin, payload.get("score_min", -0.5))
                setattr(self, attr_smax, payload.get("score_max",  0.0))
                setattr(self, attr_lmin, payload.get("level_min", None))
                setattr(self, attr_lmax, payload.get("level_max", None))
                loaded.append(path)
            except Exception as e:
                print(f"[모델 로드 실패] {path}: {e}")

        if loaded:
            for p in loaded:
                print(f"[모델 로드] {p}")
            if self._level_min_mic is not None:
                print(f"[MIC 레벨 정상범위] {self._level_min_mic:.3f} ~ {self._level_max_mic:.3f}")
            if self._level_min_vib is not None:
                print(f"[VIB 레벨 정상범위] {self._level_min_vib:.3f} ~ {self._level_max_vib:.3f}")
        else:
            print("[모델] 저장된 모델 없음 — 먼저 TRAIN 모드로 학습하세요")

    # ── 학습 (마이크/진동 동시) ──────────────────────────────
    def _train(self, mic_img: np.ndarray, vib_img: np.ndarray,
               mic_level: float, vib_level: float):
        """정상 샘플 1개씩 추가, 충분하면 두 모델 학습 실행"""
        self._train_features_mic.append(mic_img.flatten())
        self._train_features_vib.append(vib_img.flatten())
        # 레벨도 같이 누적 (학습 분포 기반 정상 범위 산출용)
        self._train_levels_mic.append(mic_level)
        self._train_levels_vib.append(vib_level)
        n = len(self._train_features_mic)

        # 진행 상황 Firebase 업로드 (25샘플마다)
        if n % 25 == 0 or n == TRAIN_MIN_SAMPLES:
            self._upload_train_status(n, False,
                f"정상 샘플 수집 중... ({n}/{TRAIN_MIN_SAMPLES})")

        if n < TRAIN_MIN_SAMPLES:
            return

        # 학습 실행 (두 모델 따로)
        print(f"\n[학습 시작] 정상 샘플 {n}개로 마이크·진동 모델 각각 학습")
        from sklearn.ensemble import IsolationForest
        import joblib
        os.makedirs(MODEL_DIR, exist_ok=True)

        # 레벨 기반 정상 범위 산출
        levels_mic_arr = np.array(self._train_levels_mic)
        levels_vib_arr = np.array(self._train_levels_vib)
        mic_mean, mic_std = float(levels_mic_arr.mean()), float(levels_mic_arr.std())
        vib_mean, vib_std = float(levels_vib_arr.mean()), float(levels_vib_arr.std())

        # 상한: μ + LEVEL_SIGMA·σ (너무 커지면 이상)
        self._level_max_mic = mic_mean + LEVEL_SIGMA * mic_std
        self._level_max_vib = vib_mean + LEVEL_SIGMA * vib_std

        # 하한: μ - LEVEL_SIGMA·σ 와 "평균의 40%" 중 더 큰 값을 사용.
        # 진동처럼 절대값이 작아 μ-4σ가 음수가 되는 경우에도
        # 평균의 40% 밑으로 떨어지면 이상으로 잡을 수 있게 함.
        self._level_min_mic = max(mic_mean - LEVEL_SIGMA * mic_std,
                                  mic_mean * LEVEL_DROP_RATIO)
        self._level_min_vib = max(vib_mean - LEVEL_SIGMA * vib_std,
                                  vib_mean * LEVEL_DROP_RATIO)

        print(f"[MIC 레벨 정상범위] {self._level_min_mic:.4f} ~ {self._level_max_mic:.4f} "
              f"(평균 {mic_mean:.4f}, σ {mic_std:.4f})")
        print(f"[VIB 레벨 정상범위] {self._level_min_vib:.4f} ~ {self._level_max_vib:.4f} "
              f"(평균 {vib_mean:.4f}, σ {vib_std:.4f})")

        for tag, feats, path, lvl_min, lvl_max in [
            ("MIC", self._train_features_mic, MODEL_PATH_MIC,
                self._level_min_mic, self._level_max_mic),
            ("VIB", self._train_features_vib, MODEL_PATH_VIB,
                self._level_min_vib, self._level_max_vib),
        ]:
            X   = np.array(feats)
            clf = IsolationForest(n_estimators=300, contamination=0.005,
                                  random_state=42, n_jobs=-1)
            clf.fit(X)

            scores      = clf.score_samples(X)
            score_mean  = float(scores.mean())
            score_std   = float(scores.std())
            score_min   = float(scores.min()) - max(0.3, score_std * 2.0)
            score_max   = float(scores.max())
            print(f"[{tag} 정규화] mean={score_mean:.3f}, std={score_std:.3f}, "
                  f"min={score_min:.3f}, max={score_max:.3f}")

            joblib.dump({
                "model":     clf,
                "score_min": score_min,
                "score_max": score_max,
                "level_min": lvl_min,
                "level_max": lvl_max,
            }, path)

            if tag == "MIC":
                self.model_mic       = clf
                self._score_min_mic  = score_min
                self._score_max_mic  = score_max
            else:
                self.model_vib       = clf
                self._score_min_vib  = score_min
                self._score_max_vib  = score_max

        msg = f"학습 완료! 정상 샘플 {n}개 — DETECT 모드로 자동 전환"
        print(f"[학습 완료] {msg}")
        self._upload_train_status(n, True, msg)

        # Firebase 모드를 DETECT로 자동 전환
        if self._fb_db:
            self._fb_db.reference("sonic_expert/mode").set("DETECT")

    # ── 추론 (개별 모델) ─────────────────────────────────────
    def _predict_single(self, model, img: np.ndarray,
                        score_min: float, score_max: float):
        if model is None:
            return False, 0.0
        flat      = img.flatten().reshape(1, -1)
        raw_score = model.score_samples(flat)[0]
        is_ab     = raw_score < THRESHOLD_IF
        span      = score_max - score_min + 1e-8
        score     = float(np.clip(
            1.0 - (raw_score - score_min) / span, 0.0, 1.0))
        return is_ab, score

    # ── 레벨 기반 이상 판정 ──────────────────────────────────
    def _is_level_abnormal(self, level: float,
                           lvl_min, lvl_max) -> bool:
        """학습 시 기록된 정상 범위(lvl_min ~ lvl_max)를 벗어나면 True.
        범위가 학습 안 됐으면(None) 판정하지 않음."""
        if lvl_min is None or lvl_max is None:
            return False
        return level < lvl_min or level > lvl_max

    # ── 윈도우 처리 (TRAIN / DETECT 분기) ───────────────────
    def _process_window(self, window: np.ndarray):
        mic_img, vib_img = window_to_images(window)
        mic_level = compute_mic_level(window)
        vib_level = compute_vib_level(window)

        if self.mode == "TRAIN":
            self._train(mic_img, vib_img, mic_level, vib_level)
            # 학습 중 Firebase latest 업데이트 (레벨만 보여줌)
            self._upload_latest({
                "mode":            "TRAIN",
                "label":           f"수집중 ({len(self._train_features_mic)}/{TRAIN_MIN_SAMPLES})",
                "score_mic":       0.0,
                "score_vib":       0.0,
                "score":           0.0,
                "mic_level":       round(mic_level, 4),
                "vib_level":       round(vib_level, 4),
                "is_abnormal_mic": False,
                "is_abnormal_vib": False,
                "is_abnormal":     False,
                "relay_on":        self.relay_on,
                "timestamp":       int(time.time() * 1000),
                "spectrogram_mic": image_to_firebase(mic_img),
                "spectrogram_vib": image_to_firebase(vib_img),
            })

        else:  # DETECT
            # 1) IF 모델 기반 이상 판정 (스펙트로그램 패턴)
            is_if_ab_mic, score_mic = self._predict_single(
                self.model_mic, mic_img,
                self._score_min_mic, self._score_max_mic)
            is_if_ab_vib, score_vib = self._predict_single(
                self.model_vib, vib_img,
                self._score_min_vib, self._score_max_vib)

            # 2) 레벨 기반 이상 판정 (학습 분포 범위 벗어남)
            is_lvl_ab_mic = self._is_level_abnormal(
                mic_level, self._level_min_mic, self._level_max_mic)
            is_lvl_ab_vib = self._is_level_abnormal(
                vib_level, self._level_min_vib, self._level_max_vib)

            # 3) 각 채널 = IF OR 레벨 이상 (둘 중 하나만 이상이어도 채널 이상)
            is_ab_mic = is_if_ab_mic or is_lvl_ab_mic
            is_ab_vib = is_if_ab_vib or is_lvl_ab_vib

            # 4) 최종 이상 = 두 채널 모두 이상 (AND 조건 유지)
            is_ab     = is_ab_mic and is_ab_vib
            score_avg = (score_mic + score_vib) / 2.0

            if is_ab:
                label = "이상 감지! (소리+진동)"
            elif is_ab_mic:
                label = "소리만 이상 (경고)"
            elif is_ab_vib:
                label = "진동만 이상 (경고)"
            else:
                label = "정상"

            result = {
                "mode":            "DETECT",
                "label":           label,
                "score_mic":       round(score_mic, 4),
                "score_vib":       round(score_vib, 4),
                "score":           round(score_avg, 4),
                "mic_level":       round(mic_level, 4),
                "vib_level":       round(vib_level, 4),
                "is_abnormal_mic": is_ab_mic,
                "is_abnormal_vib": is_ab_vib,
                "is_abnormal":     is_ab,
                "relay_on":        self.relay_on,
                "timestamp":       int(time.time() * 1000),
                "spectrogram_mic": image_to_firebase(mic_img),
                "spectrogram_vib": image_to_firebase(vib_img),
            }
            self.latest_result = result
            self.result_history.append(result)

            if is_ab:
                # AND 조건 충족 시에만 알람 기록 (경고만, 자동 차단 없음)
                self._handle_alert(score_avg, score_mic, score_vib)

            self._upload_latest(result)
            self._upload_history(result)

    def _handle_alert(self, score_avg: float,
                      score_mic: float, score_vib: float):
        now   = time.strftime("%H:%M:%S")
        entry = {
            "time":      now,
            "score":     round(score_avg, 4),
            "score_mic": round(score_mic, 4),
            "score_vib": round(score_vib, 4),
            "ts":        int(time.time() * 1000),
        }
        self.alert_log.appendleft(entry)
        print(f"[{now}] ⚠ 이상 감지! mic={score_mic:.3f}, vib={score_vib:.3f}")
        if self._fb_db:
            try:
                ts_key = str(int(time.time() * 1000))
                self._fb_db.reference(f"sonic_expert/alerts/{ts_key}").set(
                    self._to_firebase(entry))
                # 20개마다 오래된 알람 청소
                self._alerts_count += 1
                if self._alerts_count >= ALERTS_CLEANUP_EVERY:
                    self._alerts_count = 0
                    self._cleanup_alerts()
            except Exception as e:
                print(f"[알람 업로드 오류] {e}")

    # ── Firebase 직렬화 헬퍼 ─────────────────────────────────
    @staticmethod
    def _to_firebase(d: dict) -> dict:
        """Python bool → int 변환 (Firebase Admin SDK JSON 직렬화 오류 방지)"""
        out = {}
        for k, v in d.items():
            if isinstance(v, bool):
                out[k] = int(v)
            elif isinstance(v, np.bool_):
                out[k] = int(v)
            elif isinstance(v, np.integer):
                out[k] = int(v)
            elif isinstance(v, np.floating):
                out[k] = float(v)
            elif isinstance(v, list):
                out[k] = [int(x) if isinstance(x, (bool, np.bool_)) else
                           float(x) if isinstance(x, np.floating) else x for x in v]
            else:
                out[k] = v
        return out

    # ── Firebase 업로드 (비동기 큐 적재) ────────────────────
    def _upload_latest(self, payload: dict):
        """추론 루프를 막지 않도록 최신 페이로드만 저장 (덮어쓰기).
        실제 네트워크 전송은 백그라운드 워커가 담당."""
        if not self._fb_db:
            return
        with self._upload_lock:
            self._pending_latest = self._to_firebase(payload)

    def _upload_history(self, result: dict):
        """히스토리는 큐에 적재만. 실제 전송은 백그라운드 워커가 담당."""
        if not self._fb_db:
            return
        no_spec = {k: v for k, v in result.items()
                   if not k.startswith("spectrogram")}
        with self._upload_lock:
            self._history_queue.append(self._to_firebase(no_spec))

    # ── 백그라운드 업로드 워커 (latest 전용, 최우선) ─────────
    def _latest_worker(self):
        """latest만 전담하는 스레드. history/청소에 방해받지 않고
        항상 최신 결과를 최소 지연으로 전송 → 웹앱 반응 속도 최우선."""
        while self._running:
            try:
                payload = None
                with self._upload_lock:
                    if self._pending_latest is not None:
                        payload = self._pending_latest
                        self._pending_latest = None

                if payload is not None:
                    try:
                        self._fb_db.reference("sonic_expert/latest").set(payload)
                    except Exception as e:
                        print(f"[latest 업로드 오류] {e}")
                    time.sleep(LATEST_INTERVAL)
                else:
                    # 보낼 게 없으면 아주 짧게 쉬며 대기
                    time.sleep(0.02)
            except Exception as e:
                print(f"[latest 워커 오류] {e}")
                time.sleep(0.3)

    # ── 백그라운드 업로드 워커 (history/청소 전용) ──────────
    def _history_worker(self):
        """history 적재와 청소를 전담. 느려도 latest에는 영향 없음."""
        while self._running:
            try:
                batch = {}
                with self._upload_lock:
                    while self._history_queue:
                        item = self._history_queue.popleft()
                        batch[str(item["timestamp"])] = item

                if batch:
                    try:
                        self._fb_db.reference("sonic_expert/history").update(batch)
                        self._history_count += len(batch)
                        if self._history_count >= HISTORY_CLEANUP_INTERVAL:
                            self._history_count = 0
                            self._cleanup_history()
                    except Exception as e:
                        print(f"[history 업로드 오류] {e}")

                time.sleep(HISTORY_INTERVAL)
            except Exception as e:
                print(f"[history 워커 오류] {e}")
                time.sleep(0.5)

    def _cleanup_history(self):
        """history 노드의 항목 수가 HISTORY_MAX_KEEP 초과 시 오래된 것부터 삭제"""
        if not self._fb_db:
            return
        try:
            ref = self._fb_db.reference("sonic_expert/history")
            # 키만 가져오기 (값은 빼서 트래픽 절약)
            all_data = ref.get(shallow=True) or {}
            keys = sorted(all_data.keys())  # 타임스탬프 키 정렬

            if len(keys) <= HISTORY_MAX_KEEP:
                return

            to_delete = keys[:len(keys) - HISTORY_MAX_KEEP]
            # 일괄 삭제 (update with None)
            updates = {k: None for k in to_delete}
            ref.update(updates)
            print(f"[히스토리 청소] {len(to_delete)}개 오래된 항목 삭제 "
                  f"(현재 {len(keys) - len(to_delete)}개 유지)")
        except Exception as e:
            print(f"[히스토리 청소 오류] {e}")

    def _cleanup_alerts(self):
        """alerts 노드도 같은 방식으로 청소"""
        if not self._fb_db:
            return
        try:
            ref = self._fb_db.reference("sonic_expert/alerts")
            all_data = ref.get(shallow=True) or {}
            keys = sorted(all_data.keys())
            if len(keys) <= ALERTS_MAX_KEEP:
                return
            to_delete = keys[:len(keys) - ALERTS_MAX_KEEP]
            updates = {k: None for k in to_delete}
            ref.update(updates)
            print(f"[알람 청소] {len(to_delete)}개 오래된 알람 삭제")
        except Exception as e:
            print(f"[알람 청소 오류] {e}")

    def _upload_train_status(self, count: int, done: bool, msg: str):
        if not self._fb_db:
            return
        try:
            self._fb_db.reference("sonic_expert/train_status").set({
                "count":    count,
                "required": TRAIN_MIN_SAMPLES,
                "done":     int(done),
                "message":  msg,
                "ts":       int(time.time() * 1000),
            })
        except Exception as e:
            print(f"[학습 상태 업로드 오류] {e}")

    # ── 시리얼 수신 루프 ─────────────────────────────────────
    def _serial_loop(self):
        import serial
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=1)
            time.sleep(2)
            self._ser.reset_input_buffer()
            self._serial_carry = ""
            print(f"[시리얼 연결] {self.port}")
        except Exception as e:
            print(f"[시리얼 오류] {e}")
            self._running = False
            return

        buf = deque(maxlen=WINDOW_SAMPS)
        STRIDE = max(1, int(WINDOW_SAMPS * 0.3))   # 약 0.15초마다 추론
        since_last_infer = 0
        drop_total = 0            # 폐기 누적 (가끔만 요약 출력)
        last_drop_log = time.time()

        while self._running:
            try:
                n_wait = self._ser.in_waiting
                if n_wait <= 0:
                    time.sleep(0.003)
                    continue

                # OS 버퍼에 쌓인 바이트를 통째로 읽음 (readline 반복보다 훨씬 빠름)
                chunk = self._ser.read(n_wait).decode("utf-8", errors="ignore")
                # 이전에 잘린 줄과 이어붙임
                chunk = getattr(self, "_serial_carry", "") + chunk
                raw_lines = chunk.split("\n")
                # 마지막 조각은 아직 안 끝난 줄일 수 있으니 보관
                self._serial_carry = raw_lines[-1]
                lines = raw_lines[:-1]

                # 실질적으로 필요한 건 최신 WINDOW_SAMPS 샘플뿐.
                # 그보다 많이 쌓였으면 최신 부분만 남기고 조용히 버림.
                keep = WINDOW_SAMPS + STRIDE
                if len(lines) > keep:
                    drop_total += len(lines) - keep
                    lines = lines[-keep:]

                # 폐기가 쌓이면 5초에 한 번만 요약 출력 (콘솔 폭주 방지)
                if drop_total > 0 and time.time() - last_drop_log > 5.0:
                    # 자주 뜨면 STRIDE를 늘리라는 힌트만 남김
                    drop_total = 0
                    last_drop_log = time.time()

                for raw in lines:
                    raw = raw.strip()
                    if not raw:
                        continue
                    if raw.startswith("STATUS:"):
                        continue
                    if raw.startswith("timestamp"):
                        continue

                    parts = raw.split(",")
                    if len(parts) >= 6:
                        try:
                            row = [float(p) for p in parts[:5]]
                            self.relay_on = (parts[5].strip() == "1")
                        except ValueError:
                            continue
                    elif len(parts) == 5:
                        try:
                            row = [float(p) for p in parts]
                        except ValueError:
                            continue
                    else:
                        continue

                    buf.append(row[1:])  # mic, ax, ay, az
                    since_last_infer += 1

                    # 윈도우가 다 차고 STRIDE 만큼 새 데이터 들어왔을 때만 추론
                    if len(buf) == WINDOW_SAMPS and since_last_infer >= STRIDE:
                        since_last_infer = 0
                        self._process_window(np.array(buf))

            except Exception as e:
                if self._running:
                    print(f"[수신 오류] {e}")
                    time.sleep(0.1)

        if self._ser and self._ser.is_open:
            self._ser.close()

    # ── 시작 / 정지 ──────────────────────────────────────────
    def start(self):
        self._running = True
        # latest 전용 + history 전용 두 워커 스레드 시작
        self._latest_thread  = threading.Thread(target=self._latest_worker, daemon=True)
        self._history_thread = threading.Thread(target=self._history_worker, daemon=True)
        self._latest_thread.start()
        self._history_thread.start()
        self._thread = threading.Thread(target=self._serial_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        if self._latest_thread:
            self._latest_thread.join(timeout=3)
        if self._history_thread:
            self._history_thread.join(timeout=3)

    # ── 데모 모드 ────────────────────────────────────────────
    def start_demo(self):
        self._running = True
        # latest 전용 + history 전용 두 워커 스레드 시작
        self._latest_thread  = threading.Thread(target=self._latest_worker, daemon=True)
        self._history_thread = threading.Thread(target=self._history_worker, daemon=True)
        self._latest_thread.start()
        self._history_thread.start()

        def _loop():
            buf = deque(maxlen=WINDOW_SAMPS)
            STRIDE = max(1, int(WINDOW_SAMPS * 0.3))
            since_last_infer = 0
            while self._running:
                # 가짜 센서 데이터 생성
                mic = np.random.randint(400, 624)
                ax  = int(np.random.normal(0,   500))
                ay  = int(np.random.normal(0,   500))
                az  = int(np.random.normal(16384, 300))

                if self.mode == "DETECT":
                    # 15% 확률로 이상 신호 주입
                    if np.random.random() < 0.15:
                        mic = np.random.randint(700, 1000)
                        ax  = int(np.random.normal(0, 3000))

                buf.append([mic, ax, ay, az])
                since_last_infer += 1
                if len(buf) == WINDOW_SAMPS and since_last_infer >= STRIDE:
                    since_last_infer = 0
                    self._process_window(np.array(buf))

                time.sleep(WINDOW_SEC / WINDOW_SAMPS)

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        print("[데모 모드] 시작")


# ── CLI ──────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sonic-Expert 엔진")
    parser.add_argument("--demo", action="store_true", help="아두이노 없이 데모")
    parser.add_argument("--port", type=str, default="COM3")
    parser.add_argument("--baud", type=int, default=115200)
    args = parser.parse_args()

    engine = SonicEngine(port=args.port, baud=args.baud)

    if args.demo:
        engine.start_demo()
    else:
        engine.start()

    print("엔진 실행 중. Ctrl+C로 종료.")
    try:
        while True:
            r = engine.latest_result
            print(
                f"\r[{time.strftime('%H:%M:%S')}] "
                f"모드: {engine.mode:<7} | "
                f"상태: {r['label']:<18} | "
                f"소리점수: {r.get('score_mic', 0):.3f} | "
                f"진동점수: {r.get('score_vib', 0):.3f} | "
                f"소리레벨: {r.get('mic_level', 0):.3f} | "
                f"진동레벨: {r.get('vib_level', 0):.3f} | "
                f"릴레이: {'ON' if r.get('relay_on', True) else 'OFF'}",
                end="", flush=True
            )
            time.sleep(0.5)
    except KeyboardInterrupt:
        engine.stop()
        print("\n종료.")

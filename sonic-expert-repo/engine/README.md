# Sonic-Expert 엔진

Arduino에서 받은 센서 데이터를 AI로 분석하고 Firebase에 업로드하는 Python 추론 엔진과 Arduino 펌웨어입니다.

## 구성

| 파일 | 설명 |
|------|------|
| `inference_engine.py` | 메인 엔진. 시리얼 수신 → 전처리 → 멜 스펙트로그램 → Isolation Forest 추론 → Firebase 업로드 |
| `cleanup_firebase.py` | Firebase의 누적 데이터(history/alerts)를 정리하는 유틸리티 |
| `arduino/sonic_sensor.ino` | Arduino 펌웨어. 센서 수집·릴레이 제어·LED 표시 |

## 설치

```bash
pip install -r requirements.txt
```

## 설정

1. `serviceAccountKey.example.json`을 참고하여 Firebase 서비스 계정 키를
   `serviceAccountKey.json` 이름으로 이 폴더에 저장
2. `inference_engine.py` 상단의 `FIREBASE_DATABASE_URL`을 본인 프로젝트 URL로 수정

## 실행

```bash
python inference_engine.py --port COM3     # 실제 아두이노 연결
python inference_engine.py --demo          # 아두이노 없이 데모 (가짜 데이터)
```

## Firebase 데이터 정리

```bash
python cleanup_firebase.py             # 대화식 메뉴
python cleanup_firebase.py --status    # 현재 데이터 개수 확인
python cleanup_firebase.py --all       # history + alerts 전체 삭제
```

## 주요 파라미터 (inference_engine.py 상단)

| 상수 | 기본값 | 설명 |
|------|--------|------|
| `WINDOW_SEC` | 0.4 | 분석 윈도우 길이(초). 짧을수록 반응 빠름 |
| `THRESHOLD_IF` | -0.6 | 이상 판정 임계값. 낮출수록 덜 예민 |
| `TRAIN_MIN_SAMPLES` | 1000 | 학습에 필요한 정상 샘플 수 |
| `LEVEL_SIGMA` | 4.0 | 레벨 정상 범위(μ ± Nσ) |
| `LATEST_INTERVAL` | 0.12 | Firebase latest 업로드 주기(초) |

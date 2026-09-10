# Sonic-Expert 웹 대시보드

Firebase Realtime Database를 실시간 구독하여 설비 상태를 표시하는 React 대시보드입니다.

## 설정 방법

이 폴더의 `src/App.jsx`는 대시보드의 전체 소스입니다. 아래 순서로 실행 환경을 구성하세요.

### 1. Vite + React 프로젝트 생성

```bash
npm create vite@latest sonic-expert-web -- --template react
cd sonic-expert-web
npm install
npm install firebase
```

### 2. App.jsx 교체

이 폴더의 `src/App.jsx` 내용을 새로 만든 프로젝트의 `src/App.jsx`에 그대로 복사합니다.

### 3. Firebase 설정 입력

`App.jsx` 상단의 `FIREBASE_CONFIG`를 본인 Firebase 웹 앱 설정값으로 채웁니다.

```js
const FIREBASE_CONFIG = {
  apiKey:            "...",
  authDomain:        "...",
  databaseURL:       "https://YOUR_PROJECT-default-rtdb.asia-southeast1.firebasedatabase.app",
  projectId:         "...",
  storageBucket:     "...",
  messagingSenderId: "...",
  appId:             "...",
};
```

> Firebase 콘솔 → 프로젝트 설정 → 내 앱 → 웹 앱(`</>`) → SDK 설정 및 구성에서 복사

### 4. 실행

```bash
npm run dev
```

브라우저에서 `http://localhost:5173` 접속.

## 주의

- `databaseURL`은 Python 엔진(`inference_engine.py`)의 `FIREBASE_DATABASE_URL`과 **반드시 동일**해야 합니다.
- Python 엔진이 실행 중이어야 데이터가 표시됩니다. 테스트 시 `python inference_engine.py --demo`로 먼저 확인하세요.

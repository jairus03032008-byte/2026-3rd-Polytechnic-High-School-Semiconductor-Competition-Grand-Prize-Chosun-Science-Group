"""
Sonic-Expert | cleanup_firebase.py
─────────────────────────────────────────────
Firebase Realtime Database의 누적 데이터를 즉시 청소하는 일회성 스크립트.

사용법:
    python cleanup_firebase.py              # 대화식 선택 메뉴
    python cleanup_firebase.py --all        # 전체 청소 (확인 없이)
    python cleanup_firebase.py --history    # /history만
    python cleanup_firebase.py --alerts     # /alerts만
    python cleanup_firebase.py --status     # 현재 항목 수만 확인

필요한 파일:
    - serviceAccountKey.json (inference_engine.py와 같은 위치)
"""

import argparse
import sys
import firebase_admin
from firebase_admin import credentials, db as firebase_db

# inference_engine.py와 동일한 설정
FIREBASE_DATABASE_URL    = "https://YOUR_PROJECT-default-rtdb.firebaseio.com"
FIREBASE_CREDENTIAL_PATH = "serviceAccountKey.json"


def init_firebase():
    """Firebase 연결 초기화"""
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIAL_PATH)
            firebase_admin.initialize_app(cred, {
                "databaseURL": FIREBASE_DATABASE_URL
            })
        print("[Firebase] 연결 성공")
        return firebase_db
    except Exception as e:
        print(f"[Firebase] 연결 실패: {e}")
        sys.exit(1)


def count_keys(db, path: str) -> int:
    """해당 경로의 키 개수만 반환 (shallow=True로 트래픽 절약)"""
    try:
        data = db.reference(path).get(shallow=True)
        return len(data) if data else 0
    except Exception as e:
        print(f"[조회 오류] {path}: {e}")
        return 0


def show_status(db):
    """현재 Firebase의 각 노드 항목 수를 표시"""
    print("\n=== 현재 Firebase 데이터 현황 ===")
    paths = [
        ("sonic_expert/history",   "히스토리"),
        ("sonic_expert/alerts",    "알람"),
    ]
    for path, label in paths:
        n = count_keys(db, path)
        print(f"  {label:8s} ({path}): {n:5d}개")

    # latest, mode, train_status, status는 단일 노드라 개수 의미 없음
    print("\n  (latest, mode, train_status는 단일 노드이므로 청소 대상 아님)")
    print()


def clear_path(db, path: str, label: str) -> int:
    """해당 경로를 통째로 비움. Firebase가 단일 요청 크기를 제한하므로
    배치(기본 500개)로 나눠서 삭제."""
    BATCH_SIZE = 500

    ref      = db.reference(path)
    n_before = count_keys(db, path)
    if n_before == 0:
        print(f"[{label}] 이미 비어있음 — 건너뜀")
        return 0

    print(f"[{label}] {n_before}개 삭제 시작 (배치 크기 {BATCH_SIZE})...")

    # 먼저 통째로 delete() 시도 (작은 노드는 빨라서)
    try:
        ref.delete()
        print(f"[{label}] {n_before}개 삭제 완료")
        return n_before
    except Exception:
        pass  # 너무 크면 아래 배치 삭제로 폴백

    # 배치 삭제
    total_deleted = 0
    try:
        while True:
            keys_data = ref.get(shallow=True)
            if not keys_data:
                break
            keys  = list(keys_data.keys())[:BATCH_SIZE]
            if not keys:
                break

            updates = {k: None for k in keys}
            ref.update(updates)
            total_deleted += len(keys)
            print(f"  ... {total_deleted}/{n_before}개 삭제됨", end="\r", flush=True)

            if len(keys_data) <= BATCH_SIZE:
                break

        print(f"\n[{label}] 총 {total_deleted}개 삭제 완료")
        return total_deleted
    except Exception as e:
        print(f"\n[{label}] 배치 삭제 중 오류: {e}")
        print(f"  (지금까지 {total_deleted}개 삭제됨, 남은 데이터는 다시 실행 시 정리됩니다)")
        return total_deleted


def confirm(prompt: str) -> bool:
    """yes/no 확인"""
    while True:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("", "n", "no"):
            return False


def interactive_menu(db):
    """대화식 메뉴"""
    show_status(db)

    print("청소 옵션을 선택하세요:")
    print("  1) /history 만 청소")
    print("  2) /alerts 만 청소")
    print("  3) /history + /alerts 모두 청소")
    print("  4) 종료")

    choice = input("\n선택 (1~4): ").strip()

    if choice == "1":
        if confirm("정말 /history 를 전부 삭제할까요?"):
            clear_path(db, "sonic_expert/history", "히스토리")
    elif choice == "2":
        if confirm("정말 /alerts 를 전부 삭제할까요?"):
            clear_path(db, "sonic_expert/alerts", "알람")
    elif choice == "3":
        if confirm("정말 /history 와 /alerts 를 전부 삭제할까요?"):
            clear_path(db, "sonic_expert/history", "히스토리")
            clear_path(db, "sonic_expert/alerts",  "알람")
    elif choice == "4":
        print("종료")
        return
    else:
        print("잘못된 선택")
        return

    print()
    show_status(db)


# ── 메인 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sonic-Expert Firebase 청소 도구")
    parser.add_argument("--all",     action="store_true",
                        help="확인 없이 history+alerts 전부 삭제")
    parser.add_argument("--history", action="store_true",
                        help="확인 없이 history만 삭제")
    parser.add_argument("--alerts",  action="store_true",
                        help="확인 없이 alerts만 삭제")
    parser.add_argument("--status",  action="store_true",
                        help="현재 데이터 개수만 표시하고 종료")
    args = parser.parse_args()

    db = init_firebase()

    if args.status:
        show_status(db)
    elif args.all:
        show_status(db)
        clear_path(db, "sonic_expert/history", "히스토리")
        clear_path(db, "sonic_expert/alerts",  "알람")
        print()
        show_status(db)
    elif args.history:
        clear_path(db, "sonic_expert/history", "히스토리")
    elif args.alerts:
        clear_path(db, "sonic_expert/alerts",  "알람")
    else:
        interactive_menu(db)

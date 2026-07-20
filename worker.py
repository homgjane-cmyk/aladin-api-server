from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import firebase_admin
from firebase_admin import credentials, firestore, messaging

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"message": "푸시 알림 감시 서버가 정상 작동 중입니다!"}

@app.get("/ping")
def keep_awake():
    return {"status": "ok"}

# Firebase Admin 초기화
try:
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("🔥 Firebase Admin 연결 완료! 푸시 알림 감시 시작...")
except ValueError:
    db = firestore.client()

# 실시간 데이터 감시 콜백
def on_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name == 'ADDED':
            notif = change.document.to_dict()
            if notif.get('isRead'):
                continue
            try:
                path_segments = change.document.reference.path.split('/')
                target_uid = path_segments[3]

                user_ref = db.document(f"artifacts/typerecord-app-v1/public/data/users/{target_uid}")
                user_snap = user_ref.get()

                if user_snap.exists:
                    user_data = user_snap.to_dict()
                    if user_data.get('pushEnabled') and user_data.get('fcmToken'):
                        title = f"{notif.get('fromName')}님의 알림" if notif.get('fromName') else 'Shelfy'
                        message = messaging.Message(
                            notification=messaging.Notification(title=title, body=notif.get('message')),
                            token=user_data.get('fcmToken')
                        )
                        messaging.send(message)
                        print(f"✅ 푸시 전송 성공: {user_data.get('displayName')}님에게 발송됨")
            except Exception as e:
                print(f"❌ 푸시 전송 실패: {e}")

# FastAPI 구동 시 감시 시작
@app.on_event("startup")
def start_firestore_listener():
    col_query = db.collection_group('notifications')
    col_query.on_snapshot(on_snapshot)

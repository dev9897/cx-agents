import json
import redis
from schemas.session import SessionState

r = redis.Redis(host="localhost", port=6379, decode_responses=True)

SESSION_TTL = 3600


def save_session(session: SessionState):
    key = f"session:{session['session_id']}"
    r.set(key, json.dumps(session), ex=SESSION_TTL)
 

def load_session(session_id: str) -> SessionState | None:
    key = f"session:{session_id}"
    data = r.get(key)
    if data:
        return json.loads(data)
    return None
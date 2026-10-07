"""카카오톡 '나에게 보내기' 최소 클라이언트 (urllib만 사용). 텔레그램 즉시 알림의 사본 채널.

토큰 보관:
  - 로컬: ~/.config/whisky-watch/kakao_tokens.json (+ kakao_rest_key.txt) — kakao_setup.py가 만든다.
  - 클라우드(GitHub Actions): data/kakao_token.enc — 위 토큰 JSON을 openssl AES-256으로 암호화한 파일.
    복호화 키는 GitHub Secret KAKAO_TOKEN_KEY, 앱 키는 KAKAO_REST_KEY(+선택 KAKAO_CLIENT_SECRET).
    리프레시 토큰이 갱신되면 다시 암호화해 같은 파일에 쓴다(워크플로의 상태 저장 단계가 커밋).
액세스 토큰은 6시간, 리프레시 토큰은 2개월 — 만료 1개월 전부터 갱신 요청 시 새 리프레시 토큰이 온다.
폴링이 하루 수십 번 돌기 때문에 사실상 끊기지 않는다(두 달 넘게 한 번도 안 돌면 kakao_setup.py 재실행).

주의: '나와의 채팅'은 내가 보낸 메시지로 처리돼 휴대폰 푸시가 오지 않는다는 보고가 있다 — 즉시 알림의
주 채널은 텔레그램이고, 카카오는 사본(기록·확인용)이다. 메시지 링크는 카카오 앱 설정
[플랫폼 > Web > 사이트 도메인]에 등록된 도메인만 열린다.
"""
import json, os, subprocess, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

CONF = Path.home() / ".config/whisky-watch"
LOCAL_TOKENS = CONF / "kakao_tokens.json"
LOCAL_KEY = CONF / "kakao_rest_key.txt"
LOCAL_SECRET = CONF / "kakao_client_secret.txt"
DATA = Path(os.environ.get("WHISKY_DATA_DIR", Path.home() / "Claude/Projects/위스키/모니터링"))
ENC = DATA / "kakao_token.enc"


def _read(p):
    try:
        return p.read_text().strip()
    except FileNotFoundError:
        return ""


REST_KEY = os.environ.get("KAKAO_REST_KEY") or _read(LOCAL_KEY)
CLIENT_SECRET = os.environ.get("KAKAO_CLIENT_SECRET") or _read(LOCAL_SECRET)
ENC_KEY = os.environ.get("KAKAO_TOKEN_KEY", "")


def _openssl(args, data, key):
    env = dict(os.environ, KAKAO_TOKEN_KEY=key)
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                        "-pass", "env:KAKAO_TOKEN_KEY", "-base64", "-A"] + args,
                       input=data, capture_output=True, env=env, check=True)
    return r.stdout


def encrypt_to_file(tok, key, path=ENC):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_openssl([], json.dumps(tok).encode(), key))


def _load():
    if ENC_KEY and ENC.exists():
        return json.loads(_openssl(["-d"], ENC.read_bytes(), ENC_KEY)), "enc"
    if LOCAL_TOKENS.exists():
        return json.loads(LOCAL_TOKENS.read_text()), "local"
    return None, None


def _save(tok, where):
    if where == "enc":
        encrypt_to_file(tok, ENC_KEY)
    else:
        LOCAL_TOKENS.write_text(json.dumps(tok))
        os.chmod(LOCAL_TOKENS, 0o600)


def _post(url, form, token=None):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def apply_token_response(tok, d):
    """oauth/token 응답을 저장 형식에 반영(첫 발급·갱신 공용). 리프레시 토큰은 새로 온 경우만 바꾼다."""
    now = int(time.time())
    tok["access_token"] = d["access_token"]
    tok["access_exp"] = now + int(d.get("expires_in", 21599)) - 300
    if d.get("refresh_token"):
        tok["refresh_token"] = d["refresh_token"]
        tok["refresh_exp"] = now + int(d.get("refresh_token_expires_in", 5184000))
    return tok


def _access_token():
    tok, where = _load()
    if not tok or not REST_KEY:
        return None
    if time.time() >= tok.get("access_exp", 0):
        form = {"grant_type": "refresh_token", "client_id": REST_KEY, "refresh_token": tok["refresh_token"]}
        if CLIENT_SECRET:
            form["client_secret"] = CLIENT_SECRET
        tok = apply_token_response(tok, _post("https://kauth.kakao.com/oauth/token", form))
        _save(tok, where)
    return tok["access_token"]


def enabled():
    return bool(REST_KEY) and (bool(ENC_KEY and ENC.exists()) or LOCAL_TOKENS.exists())


def send(text, url, button="바로 보기"):
    """나에게 텍스트 메시지 1건. 본문은 200자까지만 보인다. 실패하면 예외."""
    token = _access_token()
    if not token:
        raise RuntimeError("카카오 토큰 없음 — kakao_setup.py 먼저 실행")
    tmpl = {"object_type": "text", "text": text[:200],
            "link": {"web_url": url, "mobile_web_url": url}, "button_title": button}
    try:
        return _post("https://kapi.kakao.com/v2/api/talk/memo/default/send",
                     {"template_object": json.dumps(tmpl, ensure_ascii=False)}, token)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"카카오 전송 실패 {e.code}: {e.read().decode(errors='replace')[:200]}")

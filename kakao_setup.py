#!/usr/bin/env python3
"""카카오톡 '나에게 보내기' 최초 연결 — 사용자가 자기 터미널에서 한 번 실행한다.

사전 준비(Kakao Developers 콘솔, 사용자가 직접):
  1. 내 애플리케이션 만들기 → [앱 키]의 REST API 키 확인
  2. [카카오 로그인] 활성화 ON, Redirect URI에 http://localhost:8765/callback 등록
  3. [동의항목] '카카오톡 메시지 전송(talk_message)' 선택 동의로 설정
  4. [플랫폼 > Web] 사이트 도메인에 https://dailyshot.co, https://www.lotteon.com 등록(메시지 링크용)

이 스크립트가 하는 일:
  - REST API 키(와 클라이언트 시크릿, 쓰는 경우)를 화면에 보이지 않게 입력받아 ~/.config/whisky-watch/에 저장
  - 브라우저로 카카오 동의 화면을 열고, localhost로 돌아온 인가 코드를 받아 토큰 발급
  - 토큰을 ~/.config/whisky-watch/kakao_tokens.json(권한 600)에 저장하고 테스트 메시지 1건 발송
  - 클라우드용: 무작위 암호 키로 토큰을 암호화해 data/kakao_token.enc에 쓰고, gh CLI로
    GitHub Secret(KAKAO_REST_KEY·KAKAO_TOKEN_KEY·KAKAO_CLIENT_SECRET) 등록 + state 브랜치에 업로드
키·토큰은 화면에 출력하지 않는다. ~/Claude 아래에는 암호화된 파일만 남는다(저장소가 공개라 클라우드 쪽 파일도 암호화본뿐).
"""
import getpass, http.server, json, os, secrets, subprocess, sys, threading, urllib.parse, webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("WHISKY_DATA_DIR", str(REPO / "data"))
CONF = Path.home() / ".config/whisky-watch"
REDIRECT = "http://localhost:8765/callback"


def main():
    CONF.mkdir(parents=True, exist_ok=True)
    key = getpass.getpass("Kakao REST API 키 (입력 내용은 보이지 않음): ").strip()
    sec = getpass.getpass("클라이언트 시크릿 (안 쓰면 그냥 Enter): ").strip()
    if not key:
        sys.exit("REST API 키가 비었습니다.")
    for name, val in (("kakao_rest_key.txt", key), ("kakao_client_secret.txt", sec)):
        p = CONF / name
        if val:
            p.write_text(val)
            os.chmod(p, 0o600)
        elif p.exists():
            p.unlink()

    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "연결 완료 — 이 창은 닫아도 됩니다." if "code" in got else f"실패: {got.get('error_description') or got}"
            self.wfile.write(msg.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("localhost", 8765), H)
    t = threading.Thread(target=srv.handle_request)
    t.start()
    url = ("https://kauth.kakao.com/oauth/authorize?" + urllib.parse.urlencode(
        {"client_id": key, "redirect_uri": REDIRECT, "response_type": "code", "scope": "talk_message"}))
    print("브라우저에서 카카오 로그인·동의를 진행하세요. 안 열리면 아래 주소를 직접 여세요.\n" + url)
    webbrowser.open(url)
    t.join(timeout=300)
    srv.server_close()
    if "code" not in got:
        sys.exit(f"인가 코드를 받지 못했습니다: {got or '시간 초과'}")

    import kakao
    kakao.REST_KEY, kakao.CLIENT_SECRET = key, sec
    form = {"grant_type": "authorization_code", "client_id": key, "redirect_uri": REDIRECT, "code": got["code"]}
    if sec:
        form["client_secret"] = sec
    tok = kakao.apply_token_response({}, kakao._post("https://kauth.kakao.com/oauth/token", form))
    kakao.LOCAL_TOKENS.write_text(json.dumps(tok))
    os.chmod(kakao.LOCAL_TOKENS, 0o600)
    kakao.ENC_KEY = ""                                       # 테스트 발송은 로컬 토큰으로
    kakao.send("🥃 위스키 워치 카카오 알림 연결 완료 (테스트 메시지)", "https://dailyshot.co")
    print("테스트 메시지를 '나와의 채팅'으로 보냈습니다.")

    enc_key = secrets.token_urlsafe(32)
    kakao.encrypt_to_file(tok, enc_key)
    secrets_to_set = {"KAKAO_REST_KEY": key, "KAKAO_TOKEN_KEY": enc_key}
    if sec:
        secrets_to_set["KAKAO_CLIENT_SECRET"] = sec
    for name, val in secrets_to_set.items():
        subprocess.run(["gh", "secret", "set", name, "--repo", "tobagi-git/whisky-watch"],
                       input=val.encode(), check=True, cwd=REPO, capture_output=True)
    print("GitHub Secret 등록 완료:", ", ".join(secrets_to_set))
    push_state_file(kakao.ENC)
    print("이제 Claude에게 '카카오 설정 끝났어'라고 알려 주세요 — 클라우드 시험 발송을 진행합니다.")


def push_state_file(path, repo="tobagi-git/whisky-watch"):
    """암호화 토큰을 state 브랜치에 올린다(2026-10-08부터 data/는 main이 아니라 state 브랜치에 산다 —
    Actions가 시작할 때 state 브랜치를 data/로 복원하므로 거기 있어야 클라우드가 읽는다).
    실행 중인 워크플로가 끝에 state를 통째로 덮어쓰므로, 도는 중이면 끝나길 기다린 뒤 올린다."""
    import base64, time
    for _ in range(40):
        r = subprocess.run(["gh", "run", "list", "--repo", repo, "--json", "status", "-q",
                            '[.[]|select(.status=="in_progress" or .status=="queued")]|length', "--limit", "10"],
                           capture_output=True, text=True)
        if r.stdout.strip() in ("", "0"):
            break
        print("클라우드 폴링이 실행 중 — 끝나길 기다립니다…")
        time.sleep(15)
    api = f"repos/{repo}/contents/kakao_token.enc"
    sha = subprocess.run(["gh", "api", f"{api}?ref=state", "--jq", ".sha"], capture_output=True, text=True).stdout.strip()
    cmd = ["gh", "api", "-X", "PUT", api, "-f", "branch=state", "-f", "message=kakao token (encrypted)",
           "-f", "content=" + base64.b64encode(Path(path).read_bytes()).decode()]
    if sha:
        cmd += ["-f", f"sha={sha}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit("state 브랜치 업로드 실패: " + r.stderr[:300])
    print("암호화 토큰을 state 브랜치에 올렸습니다.")


if __name__ == "__main__":
    main()

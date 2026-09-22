// whisky-watch-trigger — GitHub Actions를 정확한 시각에, 그리고 텔레그램 메시지가 오면 바로 실행시키는 알람시계.
// 수집·판정은 전부 GitHub Actions 쪽 파이썬이 한다. 여기서는 "언제 돌릴지"만 정한다.

const KST_OFFSET_MIN = 9 * 60;

// 폴링 시각(KST): 16:40~18:30은 10분 간격(무카와 17시 업데이트 대비), 그 외엔 짝수시 10분.
function isScheduledSlot(d) {
  const h = d.getUTCHours();
  const m = d.getUTCMinutes();
  const hm = h * 60 + m;
  if (hm >= 16 * 60 + 40 && hm <= 18 * 60 + 30 && m % 10 === 0) return true;
  return m === 10 && h % 2 === 0;
}

async function dispatch(env, reason) {
  const r = await fetch(
    `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "whisky-watch-trigger",
      },
      body: JSON.stringify({ ref: "main" }),
    }
  );
  const ok = r.status === 204;
  console.log(`dispatch(${reason}) → ${r.status}${ok ? "" : " " + (await r.text()).slice(0, 200)}`);
  return ok;
}

// 텔레그램에 아직 처리 안 된 메시지가 있는지 엿본다. offset 없이 부르면 확인(consume)하지 않으므로
// 실제 처리는 GitHub 쪽 파이썬이 offset을 넘겨 가져가면서 한다. 음수 offset은 과거 업데이트를 지우니 쓰지 말 것.
async function pendingUpdateId(env) {
  const r = await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/getUpdates?timeout=0&limit=100`);
  if (!r.ok) {
    console.log(`telegram getUpdates → ${r.status}`);
    return null;
  }
  const j = await r.json();
  const ids = (j.result || []).map((u) => u.update_id);
  return ids.length ? Math.max(...ids) : null;
}

async function tick(env, when) {
  const kst = new Date(when + KST_OFFSET_MIN * 60 * 1000);
  const now = Date.now();
  let dispatched = false;

  // 1) 텔레그램 새 메시지 → 바로 실행. 같은 메시지로는 10분에 한 번만 재시도.
  const maxId = await pendingUpdateId(env);
  if (maxId !== null) {
    const last = JSON.parse((await env.STATE.get("tg")) || "{}");
    const isNew = !last.id || maxId > last.id;
    const stale = last.id === maxId && now - (last.at || 0) > 10 * 60 * 1000;
    if (isNew || stale) {
      dispatched = await dispatch(env, isNew ? `telegram ${maxId}` : `telegram retry ${maxId}`);
      if (dispatched) await env.STATE.put("tg", JSON.stringify({ id: maxId, at: now }));
    }
  }

  // 2) 정해진 시각 → 폴링 실행 (방금 텔레그램으로 실행했으면 그걸로 갈음)
  if (!dispatched && isScheduledSlot(kst)) {
    dispatched = await dispatch(env, `schedule ${kst.toISOString().slice(11, 16)} KST`);
  }
  if (dispatched) await env.STATE.put("last_dispatch", new Date(now).toISOString());
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env, event.scheduledTime));
  },
};

/**
 * MeetLite 反馈接收端（Cloudflare Worker）
 *
 * 作用：把客户端 POST 上来的反馈，用服务端自己的 GitHub token 建成 issue，
 * 这样用户不必有 GitHub 账号也能提交。
 *
 * 部署（约 5 分钟）：
 *   npm i -g wrangler && wrangler login
 *   wrangler init meetlite-feedback     # 或直接在本目录 wrangler deploy
 *   wrangler secret put GH_TOKEN        # PAT，只需 issues:write（fine-grained）或 repo
 *   wrangler secret put FEEDBACK_TOKEN  # 自定义口令，防止被陌生人刷
 *   wrangler deploy
 *
 * 然后把 https://<worker>.workers.dev/f?k=<FEEDBACK_TOKEN> 填进客户端的
 * config.json → feedback_webhook（或打包时写进 config.DEFAULTS）。
 *
 * 可选变量（wrangler.toml 的 [vars]）：
 *   REPO = "L1nKy0713/MeetLite"
 */

const DEFAULT_REPO = 'L1nKy0713/MeetLite';
const MAX_BODY = 8000;
const KIND_LABEL = { bug: 'bug', idea: 'enhancement', other: 'question' };
const KIND_TITLE = { bug: '问题', idea: '建议', other: '反馈' };

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  });

const clip = (s, n) => {
  const t = String(s ?? '');
  return t.length > n ? t.slice(0, n) + '…' : t;
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === 'GET' && url.pathname === '/health') {
      return json({ ok: true, service: 'meetlite-feedback' });
    }
    if (request.method !== 'POST') {
      return json({ ok: false, error: '只接受 POST' }, 405);
    }
    if (!env.FEEDBACK_TOKEN || url.searchParams.get('k') !== env.FEEDBACK_TOKEN) {
      return json({ ok: false, error: '口令不对' }, 403);
    }
    if (!env.GH_TOKEN) {
      return json({ ok: false, error: '服务端没有配置 GH_TOKEN' }, 500);
    }

    let data;
    try {
      data = await request.json();
    } catch {
      return json({ ok: false, error: '正文不是合法 JSON' }, 400);
    }
    if (!data || typeof data !== 'object') return json({ ok: false, error: '正文为空' }, 400);

    const kind = ['bug', 'idea', 'other'].includes(data.kind) ? data.kind : 'other';
    const title = clip(data.title || '(无标题)', 160).replace(/\s+/g, ' ').trim();
    if (!title) return json({ ok: false, error: '缺少标题' }, 400);
    const detail = clip(data.body || '', MAX_BODY);
    const contact = clip(data.contact || '', 200);
    const diag = clip(data.diagnostics || '', 4000);

    const lines = [
      `**类型**：${KIND_TITLE[kind]}`,
      `**客户端时间**：${new Date().toISOString()}`,
      contact ? `**联系方式**：${contact}` : '**联系方式**：未填',
      '',
      '### 描述',
      detail || '（空）',
    ];
    if (diag) {
      lines.push('', '<details><summary>环境信息（自动附带，不含会议内容）</summary>', '', '```', diag, '```', '', '</details>');
    }
    lines.push('', '<!-- via meetlite-feedback worker -->');

    const repo = env.REPO || DEFAULT_REPO;
    const res = await fetch(`https://api.github.com/repos/${repo}/issues`, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${env.GH_TOKEN}`,
        accept: 'application/vnd.github+json',
        'content-type': 'application/json',
        'user-agent': 'meetlite-feedback-worker',
      },
      body: JSON.stringify({
        title: `[${kind}] ${title}`,
        body: lines.join('\n'),
        labels: ['feedback', KIND_LABEL[kind]],
      }),
    });

    const out = await res.json().catch(() => ({}));
    if (!res.ok) {
      return json({ ok: false, error: `GitHub ${res.status}: ${out.message || '创建 issue 失败'}` }, 502);
    }
    return json({ ok: true, issue: out.number, url: out.html_url });
  },
};
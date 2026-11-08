#!/usr/bin/env node
/**
 * 把 GitHub Issues（label=feedback）折成一份周报 Markdown。
 * 用法：node server/feedback-digest.mjs issues.json > feedback.md
 * 环境：DIGEST_SINCE（可选，ISO 时间），只保留该时间之后有更新的 issue。
 */
import { readFileSync } from 'node:fs';

const file = process.argv[2];
if (!file) {
  console.error('usage: node server/feedback-digest.mjs issues.json');
  process.exit(2);
}
const since = process.env.DIGEST_SINCE ? new Date(process.env.DIGEST_SINCE) : null;
const issues = JSON.parse(readFileSync(file, 'utf8'));

const KIND = { bug: '🐞 问题', idea: '💡 建议', other: '📝 其他' };
const rows = issues
  .filter((i) => !since || new Date(i.updated_at) >= since)
  .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

const esc = (s) => String(s || '').replace(/\|/g, '\\|').replace(/\n+/g, ' ');
const label = (i) => {
  const m = /^\[(bug|idea|other)\]/i.exec(i.title || '');
  return KIND[(m && m[1].toLowerCase()) || 'other'];
};
const title = (i) => String(i.title || '').replace(/^\[(bug|idea|other)\]\s*/i, '');

const out = [];
out.push(`# 反馈周报 · ${new Date().toISOString().slice(0, 10)}`);
out.push('');
out.push(`共 **${rows.length}** 条（label=feedback${since ? `，${since.toISOString().slice(0, 10)} 之后有更新` : ''}）。`);
out.push('');
if (!rows.length) {
  out.push('这周没有新反馈。');
} else {
  out.push('| 类型 | 标题 | 状态 | 创建 | 链接 |');
  out.push('| --- | --- | --- | --- | --- |');
  for (const i of rows) {
    out.push(`| ${label(i)} | ${esc(title(i))} | ${i.state} | ${i.created_at.slice(0, 10)} | [#${i.number}](${i.html_url}) |`);
  }
  out.push('');
  out.push('## 详情');
  for (const i of rows) {
    out.push('');
    out.push(`### #${i.number} ${esc(title(i))}`);
    out.push('');
    out.push(`- 类型：${label(i)}　状态：${i.state}　创建：${i.created_at.slice(0, 10)}`);
    out.push(`- 链接：${i.html_url}`);
    const body = String(i.body || '').trim();
    if (body) {
      out.push('');
      out.push(body.length > 1500 ? body.slice(0, 1500) + '\n\n…（截断）' : body);
    }
  }
}
process.stdout.write(out.join('\n') + '\n');

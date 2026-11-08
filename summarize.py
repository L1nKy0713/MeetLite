"""提示词与摘要流水线：map-reduce 分块摘要。

模板按语言分目录存放（templates/zh、templates/en），模板语言决定「纪要输出语言」，
与会议本身说的是什么语言无关——中文会议可以用英文模板出一份英文纪要。
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_ROOT = os.path.join(HERE, "templates")
DEFAULT_LANG = "zh"
LANGS = ("zh", "en")

# ---------------------------------------------------------------------------
# 每种语言的提示词
# ---------------------------------------------------------------------------

PROMPTS = {
    "zh": {
        "strict": "严禁输出 Thinking Process、分析步骤、英文推理或任何过程说明；直接从 Markdown 内容开始输出。\n\n",
        "system_base": (
            "你是一名专业的会议记录助手。你只能依据提供的会议转写内容作答，"
            "不得编造未出现的信息，不得输出思考过程、解释或与任务无关的说明。"
            "如果某个部分在内容中确实没有信息，就写“本节无相关内容”。"
            "输出必须是 Markdown，不要用代码块包裹。"
        ),
        "chunk_system": (
            "你是一名会议记录助手。请把这段会议转写压缩成要点笔记，"
            "保留：发言主题、结论/决定、提出的任务（含负责人和时间，如果提到）、"
            "关键数字与专有名词。只输出要点列表，不要评论，不要总结方法。"
        ),
        "combine_system": (
            "你是一名会议记录助手。下面是同一场会议的若干分段笔记，"
            "请合并为一份不重复、按主题组织的要点笔记，保留所有决定、任务和数字。"
            "只输出要点列表。"
        ),
        "chunk_user": "会议分段（第 {i}/{n} 段）：\n\n{body}",
        "chunk_label": "### 第 {i} 段",
        "progress_chunk": "分块摘要 {i}/{n}",
        "progress_combine": "合并分段笔记",
        "progress_final": "生成最终纪要",
        "tpl_hint": "严格按下面的模板输出，保留全部标题与顺序：\n\n",
        "template_name": "会议模板：",
        "notes_label": "会议要点笔记：",
        "raw_label": "原始转写（供核对细节，可忽略与模板无关的部分）：",
        "final_ask": "请输出最终会议纪要。",
        "empty": "（没有转写内容可总结）",
        "section_ask": "（写作要求：{instruction}）",
        "no_info": "本节无相关内容",
    },
    "en": {
        "strict": (
            "Never output a thinking process, analysis steps, or any meta commentary. "
            "Start directly with the Markdown content.\n\n"
        ),
        "system_base": (
            "You are a professional meeting-notes assistant. Answer only from the supplied "
            "meeting transcript; never invent information that was not said, never output "
            "reasoning or explanations, and stay strictly on task. If a section genuinely has "
            "no supporting material, write \"" + "No relevant content in this section" + "\". "
            "Output Markdown directly, without wrapping it in a code block."
        ),
        "chunk_system": (
            "You are a meeting-notes assistant. Compress this part of a meeting transcript into "
            "bullet notes. Keep: the topics discussed, conclusions and decisions, action items "
            "(with owner and timing when mentioned), key numbers and proper nouns. "
            "Output bullet points only - no commentary, no meta explanation."
        ),
        "combine_system": (
            "You are a meeting-notes assistant. Below are notes from several parts of the same "
            "meeting. Merge them into one de-duplicated, topic-organised set of notes, keeping "
            "every decision, action item and number. Output bullet points only."
        ),
        "chunk_user": "Meeting segment ({i}/{n}):\n\n{body}",
        "chunk_label": "### Segment {i}",
        "progress_chunk": "Summarising chunk {i}/{n}",
        "progress_combine": "Merging chunk notes",
        "progress_final": "Writing the minutes",
        "tpl_hint": "Follow the template below exactly, keeping every heading and its order:\n\n",
        "template_name": "Meeting template: ",
        "notes_label": "Key notes:",
        "raw_label": "Raw transcript (for detail checking; ignore anything irrelevant to the template):",
        "final_ask": "Produce the final meeting minutes now.",
        "empty": "(nothing to summarise - the transcript is empty)",
        "section_ask": "(writing requirement: {instruction})",
        "no_info": "No relevant content in this section",
    },
}


def norm_lang(lang: str | None) -> str:
    lang = (lang or DEFAULT_LANG).lower()
    if lang.startswith("zh"):
        return "zh"
    if lang.startswith("en"):
        return "en"
    return DEFAULT_LANG


def _tpl_dir(lang: str) -> str:
    d = os.path.join(TPL_ROOT, norm_lang(lang))
    return d if os.path.isdir(d) else os.path.join(TPL_ROOT, DEFAULT_LANG)


def _read_tpl(lang: str, tid: str) -> dict:
    path = os.path.join(_tpl_dir(lang), f"{tid}.json")
    if not os.path.isfile(path):
        path = os.path.join(_tpl_dir(lang), "standard_meeting.json")
    if not os.path.isfile(path):
        path = os.path.join(TPL_ROOT, DEFAULT_LANG, "standard_meeting.json")
    return json.loads(open(path, encoding="utf-8").read())


def list_templates(lang: str | None = DEFAULT_LANG) -> list[dict]:
    """列出某个语言的模板；模板不存在时回落到中文。"""
    out = []
    for d in (_tpl_dir(lang), os.path.join(TPL_ROOT, DEFAULT_LANG)):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            tid = fn[:-5]
            if any(t["id"] == tid for t in out):
                continue
            try:
                data = json.loads(open(os.path.join(d, fn), encoding="utf-8").read())
            except Exception:
                continue
            out.append({
                "id": tid,
                "lang": data.get("lang", norm_lang(lang)),
                "name": data.get("name", tid),
                "description": data.get("description", ""),
                "sections": [s["title"] for s in data.get("sections", [])],
            })
    return out


def load_template(tid: str, lang: str | None = DEFAULT_LANG) -> dict:
    return _read_tpl(lang, tid)


def _sections_markdown(tpl: dict, lang: str) -> str:
    P = PROMPTS[norm_lang(lang)]
    lines = []
    for i, sec in enumerate(tpl.get("sections", []), 1):
        lines.append(f"## {i}. {sec['title']}")
        if sec.get("instruction"):
            lines.append(P["section_ask"].format(instruction=sec["instruction"]))
        lines.append("")
    return "\n".join(lines)


def chunk_text(text: str, max_chars: int = 3000, overlap: int = 200) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    chunks, i = [], 0
    while i < len(text):
        end = min(i + max_chars, len(text))
        if end < len(text):
            cut = max(text.rfind("\n", i, end), text.rfind("。", i, end), text.rfind(". ", i, end))
            if cut > i + max_chars // 2:
                end = cut + 1
        chunks.append(text[i:end])
        if end >= len(text):
            break
        i = max(end - overlap, i + 1)
    return chunks


class Summarizer:
    def __init__(self, llm, log=print):
        self.llm = llm
        self.log = log

    def summarize(self, transcript: str, template_id: str = "standard_meeting",
                  progress=None, lang: str | None = None) -> str:
        tpl = load_template(template_id, lang)
        tpl_lang = norm_lang(tpl.get("lang") or lang)
        P = PROMPTS[tpl_lang]
        if not transcript.strip():
            return P["empty"]

        chunks = chunk_text(transcript)
        self.log(f"[summary] {len(transcript)} chars -> {len(chunks)} chunk(s) lang={tpl_lang}")

        if len(chunks) == 1:
            notes = transcript
        else:
            notes_parts = []
            for idx, ch in enumerate(chunks, 1):
                if progress:
                    progress(P["progress_chunk"].format(i=idx, n=len(chunks)))
                part = self.llm.generate(
                    P["strict"] + P["chunk_system"],
                    P["chunk_user"].format(i=idx, n=len(chunks), body=ch),
                    max_tokens=700,
                    context_size=8192,
                )
                notes_parts.append(P["chunk_label"].format(i=idx) + "\n" + part)
            if progress:
                progress(P["progress_combine"])
            joined = "\n\n".join(notes_parts)
            if len(chunks) > 3:
                notes = self.llm.generate(
                    P["strict"] + P["combine_system"], joined, max_tokens=1400, context_size=16384
                )
            else:
                notes = joined

        if progress:
            progress(P["progress_final"])
        system = (
            P["strict"] + P["system_base"] + "\n\n" + P["tpl_hint"]
            + _sections_markdown(tpl, tpl_lang)
        )
        user = (
            P["template_name"] + tpl.get("name", template_id) + "\n\n"
            + P["notes_label"] + "\n" + notes.strip()[:12000] + "\n\n"
            + P["raw_label"] + "\n" + transcript.strip()[:8000] + "\n\n"
            + P["final_ask"]
        )
        return self.llm.generate(system, user, max_tokens=1800, context_size=32768)

    def translate(self, text: str, target: str = "English") -> str:
        return self.llm.generate(
            "You are a professional translator. Translate the meeting minutes into "
            f"{target}, preserving Markdown structure and headings.",
            text[:12000],
            max_tokens=1800,
            context_size=32768,
        )
"""The assistant's knowledge: the platform's own documentation, searchable.

The guides are split into sections at their headings and ranked with BM25.
They are read from disk and re-indexed when a file changes, so the assistant
never drifts from the documentation it quotes. Rendering is a small Markdown
subset, escaped before anything is interpreted: the text is ours, but the
answer is HTML in someone's browser, and that is not the place to trust it.
"""

from __future__ import annotations

import html
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.urls import reverse
from django.utils.text import slugify

#: (key, title, path relative to BASE_DIR). The framework reference comes first:
#: it defines IRGF itself (https://irgframework.org/); the guides after it say
#: how this platform implements the framework and how the platform works.
DOCUMENTS = [
    ("framework", "IRGF framework (irgframework.org)", "docs/irgf-framework-reference.md"),
    ("irgf", "AI Control Tower and the IRGF", "docs/irgf-alignment.md"),
    ("guide", "Technical Guide", "docs/technical-guide.md"),
    ("readme", "Product overview", "README.md"),
]
#: Headings whose sections are navigation, not content.
SKIP_TITLES = {"contents"}
HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
STOPWORDS = frozenset(
    "a an and are as at be by can do does for from has have how i in is it its me my of on or our "
    "that the their them then there these this to was we what when where which who why will with "
    "you your about into than so if not no any all also".split()
)
#: IRGF codes and common phrasings, expanded into the words the guides use.
EXPANSIONS = {
    "g1": "provisional classification gate g1",
    "g2": "confirmed countersigned classification gate g2",
    "g3": "deployment authorization gate g3 s7",
    "g4": "material change gate g4 s9",
    "s2": "risk classification tier scoring s2",
    "s3": "agent card s3",
    "s6": "assurance evaluation s6",
    "s7": "deployment authorization g3",
    "s8": "monitoring owner s8",
    "s9": "change g4",
    "s10": "retired inactive s10",
    "m4": "scoring dimensions",
    "m5": "tiering tier matrix",
    "m6": "countersignature",
    "m10": "eight link chain",
    "m12": "control plane compared",
    "m13": "drift records",
    "m15": "decision rights",
    "m16": "decision rights",
    "m17": "exception exceptions",
    "killswitch": "kill switch suspend",
    "finops": "cost budget spend",
    "a2a": "agent to agent contracts",
    "mcp": "mcp gateway tool",
}


def tokenize(text: str) -> list[str]:
    text = text.lower()
    # "gate 3", "stage 7", "module 17" -> g3, s7, m17; "kill-switch" -> killswitch.
    text = re.sub(r"\bgate\s+(\d)\b", r"g\1", text)
    text = re.sub(r"\bstage\s+(\d{1,2})\b", r"s\1", text)
    text = re.sub(r"\bmodule\s+(\d{1,2})\b", r"m\1", text)
    text = text.replace("kill-switch", "killswitch").replace("kill switch", "killswitch")
    words = re.findall(r"[a-z0-9]+", text)
    return [stem(word) for word in words if word not in STOPWORDS and len(word) > 1]


def stem(word: str) -> str:
    """Just enough stemming that "authorizations" finds "authorization"."""
    if re.fullmatch(r"[a-z]\d{1,2}", word):  # an IRGF code: leave it alone
        return word
    for suffix in ("ations", "ation", "ings", "ing", "ies", "es", "ed", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def expand(tokens: list[str]) -> list[str]:
    extra = []
    for token in tokens:
        if token in EXPANSIONS:
            extra.extend(tokenize(EXPANSIONS[token]))
    return tokens + extra


@dataclass
class Section:
    id: str
    doc: str
    doc_title: str
    title: str
    trail: list[str]
    level: int
    body: str
    tokens: Counter = field(default_factory=Counter, repr=False)
    length: int = 0

    @property
    def url(self) -> str:
        return reverse("assistant:section", args=[self.id])

    @property
    def breadcrumb(self) -> str:
        return " › ".join(self.trail)


def parse(key: str, title: str, text: str) -> list[Section]:
    sections: list[Section] = []
    trail: list[str] = []
    current: Section | None = None
    lines: list[str] = []
    in_code = False
    seen: Counter = Counter()

    def close():
        if current is not None:
            current.body = "\n".join(lines).strip()
            sections.append(current)

    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        match = None if in_code else HEADING.match(line)
        if match:
            close()
            lines = []
            level = len(match.group(1))
            heading = re.sub(r"[*`]", "", match.group(2)).strip()
            trail = trail[: max(level - 2, 0)] + [heading] if level > 1 else [heading]
            base = f"{key}-{slugify('-'.join(trail))}"[:120]
            seen[base] += 1
            current = Section(
                id=base if seen[base] == 1 else f"{base}-{seen[base]}",
                doc=key, doc_title=title, title=heading, trail=list(trail), level=level, body="",
            )
            continue
        lines.append(line)
    close()

    kept = []
    for section in sections:
        if section.title.lower() in SKIP_TITLES or len(section.body) < 40:
            continue
        words = tokenize(section.body)
        heading_words = tokenize(" ".join(section.trail))
        # A heading says what a section is about more reliably than any one line in it.
        section.tokens = Counter(words) + Counter(heading_words * 3)
        section.length = sum(section.tokens.values())
        kept.append(section)
    return kept


class KnowledgeBase:
    """The documentation, split into sections and ranked with BM25."""

    k1 = 1.4
    b = 0.75

    def __init__(self, documents=DOCUMENTS, base_dir=None):
        self.documents = documents
        self.base_dir = Path(base_dir or settings.BASE_DIR)
        self._lock = threading.Lock()
        self._stamp = None
        self.sections: list[Section] = []
        self.by_id: dict[str, Section] = {}
        self.df: Counter = Counter()
        self.avg_length = 1.0

    def _paths(self):
        return [(key, title, self.base_dir / rel) for key, title, rel in self.documents]

    def _current_stamp(self):
        return tuple(path.stat().st_mtime if path.exists() else 0 for _, _, path in self._paths())

    def ensure_loaded(self) -> None:
        stamp = self._current_stamp()
        if stamp == self._stamp:
            return
        with self._lock:
            if stamp == self._stamp:
                return
            sections = []
            for key, title, path in self._paths():
                if path.exists():
                    sections.extend(parse(key, title, path.read_text(encoding="utf-8-sig")))
            df = Counter()
            for section in sections:
                df.update(section.tokens.keys())
            self.sections = sections
            self.by_id = {section.id: section for section in sections}
            self.df = df
            self.avg_length = (sum(s.length for s in sections) / len(sections)) if sections else 1.0
            self._stamp = stamp

    def get(self, section_id: str) -> Section | None:
        self.ensure_loaded()
        return self.by_id.get(section_id)

    def search(self, query: str, limit: int = 5, prefer: str = "", docs=None) -> list[tuple[float, Section]]:
        """Sections ranked for `query`; `docs` limits the search to those document keys."""
        self.ensure_loaded()
        terms = expand(tokenize(query))
        if not terms or not self.sections:
            return []
        total = len(self.sections)
        weights = Counter(terms)
        scored = []
        for section in self.sections:
            if docs and section.doc not in docs:
                continue
            score = 0.0
            for term, weight in weights.items():
                frequency = section.tokens.get(term)
                if not frequency:
                    continue
                idf = math.log(1 + (total - self.df[term] + 0.5) / (self.df[term] + 0.5))
                norm = frequency + self.k1 * (1 - self.b + self.b * section.length / self.avg_length)
                score += weight * idf * frequency * (self.k1 + 1) / norm
            if score and prefer and section.doc == prefer:
                score *= 1.25
            if score and section.level == 1:
                score *= 0.6  # a document's introduction mentions everything and answers little
            if score:
                scored.append((score, section))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[:limit]


_knowledge: KnowledgeBase | None = None


def knowledge() -> KnowledgeBase:
    global _knowledge
    if _knowledge is None:
        _knowledge = KnowledgeBase()
    _knowledge.ensure_loaded()
    return _knowledge


# -- Extracting an answer ------------------------------------------------------------


def blocks(markdown: str) -> list[str]:
    """Paragraphs, lists, tables and code blocks, each kept whole."""
    out, current, in_code = [], [], False
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            current.append(line)
            in_code = not in_code
            if not in_code:
                out.append("\n".join(current))
                current = []
            continue
        if in_code:
            current.append(line)
            continue
        if not line.strip():
            if current:
                out.append("\n".join(current))
                current = []
            continue
        if line.strip().startswith("!["):  # screenshots are not served to the widget
            continue
        current.append(line)
    if current:
        out.append("\n".join(current))
    return [block for block in out if block.strip() and block.strip() != "---"]


def extract(section: Section, query: str, budget: int = 1500) -> str:
    """The parts of a section that answer the question, in their original order."""
    terms = set(expand(tokenize(query)))
    candidates = blocks(section.body)
    if not candidates:
        return ""
    ranked = []
    for index, block in enumerate(candidates):
        words = tokenize(block)
        hits = sum(1 for word in words if word in terms)
        # The opening paragraph usually defines the subject; give it a head start.
        ranked.append((hits + (1.5 if index == 0 else 0) - len(block) / 4000, index, block))
    ranked.sort(reverse=True)
    chosen, used = [], 0
    for score, index, block in ranked:
        if score <= 0 and chosen:
            break
        if used and used + len(block) > budget:
            continue
        chosen.append((index, block))
        used += len(block)
        if used >= budget:
            break
    # A block ending "...on five dimensions:" introduces the next one; never
    # show the introduction without what it introduces.
    picked = {index for index, _ in chosen}
    for index, block in list(chosen):
        if block.rstrip().endswith(":") and index + 1 < len(candidates) and index + 1 not in picked:
            chosen.append((index + 1, candidates[index + 1]))
            picked.add(index + 1)
    # ...and an introduction to something not shown (a screenshot) says nothing.
    chosen = [
        (index, block) for index, block in chosen
        if not block.rstrip().endswith(":") or index + 1 in picked
    ]
    return "\n\n".join(block for _, block in sorted(chosen))


# -- Rendering -------------------------------------------------------------------------


def _inline(text: str, link_for) -> str:
    text = html.escape(text, quote=False)
    code_spans: list[str] = []

    def keep_code(match):
        code_spans.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(code_spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", keep_code, text)

    def link(match):
        label, target = match.group(1), html.unescape(match.group(2))
        href = link_for(target)
        if not href:
            return label
        external = href.startswith("http")
        attrs = ' target="_blank" rel="noopener"' if external else ""
        return f'<a href="{html.escape(href)}"{attrs}>{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"&lt;(https?://[^\s&]+)&gt;", lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: code_spans[int(m.group(1))], text)


def default_link_for(target: str) -> str:
    if target.startswith(("http://", "https://")):
        return target
    # Links between the guides point at a heading: find the section it names.
    anchor = target.split("#", 1)[1] if "#" in target else ""
    if anchor:
        base = knowledge()
        wanted = slugify(anchor.replace("-", " "))
        for section in base.sections:
            if slugify(section.title) == wanted:
                return section.url
    return ""


def render(markdown: str, link_for=default_link_for) -> str:
    out = []
    for block in blocks(markdown):
        lines = block.splitlines()
        first = lines[0].strip()
        if first.startswith("```"):
            code = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:])
            out.append(f"<pre><code>{html.escape(code)}</code></pre>")
        elif first.startswith("|") and len(lines) >= 2 and re.match(r"^\|?[\s:|-]+\|?$", lines[1].strip()):
            rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines if line.strip()]
            head, body = rows[0], rows[2:]
            # A grid of short values (a tier matrix) reads only if it does not wrap.
            compact = all(len(cell) <= 14 for row in body for cell in row)
            table = [f"<table class=\"table table-sm ct-assistant-table{' is-compact' if compact else ''}\"><thead><tr>"]
            table += [f"<th>{_inline(cell, link_for)}</th>" for cell in head]
            table.append("</tr></thead><tbody>")
            for row in body:
                table.append("<tr>" + "".join(f"<td>{_inline(cell, link_for)}</td>" for cell in row) + "</tr>")
            table.append("</tbody></table>")
            out.append("<div class=\"table-responsive\">" + "".join(table) + "</div>")
        elif re.match(r"^(#{1,6})\s", first):
            out.append(f"<p class=\"fw-semibold mb-1\">{_inline(first.lstrip('#').strip(), link_for)}</p>")
        elif re.match(r"^([-*]|\d+\.)\s", first):
            ordered = bool(re.match(r"^\d+\.", first))
            items, current = [], []
            for line in lines:
                if re.match(r"^\s{0,1}([-*]|\d+\.)\s", line):
                    if current:
                        items.append(" ".join(current))
                    current = [re.sub(r"^\s*([-*]|\d+\.)\s+", "", line)]
                else:
                    current.append(line.strip())
            if current:
                items.append(" ".join(current))
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(item, link_for)}</li>" for item in items) + f"</{tag}>")
        else:
            out.append(f"<p>{_inline(' '.join(line.strip() for line in lines), link_for)}</p>")
    return "\n".join(out)

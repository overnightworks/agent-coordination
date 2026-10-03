"""The board page: one simple yes/no card per open expectation line.

Layout and colours follow the frozen picture `docs/board/mockup.html` (the
simple card: question, example, picture, Ja, Nein, note, undo). Every text
from the decision source is HTML-escaped, and a picture is only ever embedded
as an `<img>` data URI: a browser never runs script inside an image, and the
page's Content-Security-Policy lets only the page's own nonce'd script run.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from html import escape

from .ports import ExpectationLine, Outcome

HTML_CONTENT_TYPE = "text/html; charset=utf-8"
UNDO_SECONDS = 8
NOTHING_OPEN = "Nichts offen"
NOTE_MAX_LENGTH = 280

_STYLE = """
:root{--bg:#F3F2F6;--surface:#FFFFFF;--line:#E1DEE8;--line-strong:#C9C4D4;--fg:#17141F;
--fg-2:#4D4759;--fg-3:#857E92;--accent:#8A2A63;--accent-fg:#FFFFFF;--crit:#C42F2F;
--crit-soft:#FBE7E7;--warn:#9A6514;--warn-soft:#F8EEDA;
--shadow:0 18px 50px -24px rgba(23,20,31,.35);--r:12px;
--font-body:'Instrument Sans',ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#0F0E13;--surface:#18161E;--line:#2A2733;
--line-strong:#3B3746;--fg:#ECEAF1;--fg-2:#B0AABC;--fg-3:#7A7487;--accent:#E98BC0;
--accent-fg:#160D13;--crit:#F07070;--crit-soft:#331719;--warn:#E2AE55;--warn-soft:#2A2113;
--shadow:0 20px 60px -20px rgba(0,0,0,.8);color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 var(--font-body)}
main{max-width:720px;margin:0 auto;padding:28px clamp(16px,3vw,44px) 96px}
h1{font-size:22px;margin:0 0 20px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
box-shadow:var(--shadow);padding:20px;margin:0 0 20px}
.card .item{font-size:13px;color:var(--fg-3);margin:0 0 6px}
.card .question{font-size:19px;font-weight:600;margin:0 0 8px}
.card .text{color:var(--fg-2);margin:0 0 8px}
.card .example{color:var(--fg-2);border-left:3px solid var(--line-strong);padding-left:12px;
margin:0 0 12px}
.card img{display:block;max-width:100%;max-height:60vh;object-fit:contain;
border:1px solid var(--line);border-radius:8px;margin:0 0 12px}
.card textarea{width:100%;font:inherit;padding:8px;border:1px solid var(--line-strong);
border-radius:8px;background:var(--surface);color:var(--fg);margin:0 0 12px;resize:vertical}
.choices{display:flex;gap:12px}
.choices button{padding:8px 22px;border-radius:999px;border:1px solid var(--line-strong);
background:var(--surface);color:var(--fg);font:inherit;font-weight:600;cursor:pointer}
.choices button:hover{border-color:var(--fg)}
.choices button:disabled{opacity:.4;cursor:default}
.status{margin:12px 0 0;padding:8px 12px;border-radius:8px}
.status.failed{background:var(--crit-soft);color:var(--crit)}
.status.already_ruled{background:var(--warn-soft);color:var(--warn)}
.empty,.unavailable{text-align:center;color:var(--fg-2);padding:48px 0;font-size:19px}
.unavailable{color:var(--crit)}
.toasts{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);display:flex;
flex-direction:column;gap:8px;max-width:calc(100% - 32px)}
.toast{background:var(--fg);color:var(--bg);border-radius:999px;padding:10px 10px 10px 18px;
display:flex;gap:14px;align-items:center;box-shadow:var(--shadow);font-size:14px}
.toast .msg{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.toast button{background:var(--bg);color:var(--fg);font:inherit;font-weight:600;
padding:6px 14px;border-radius:999px;border:0;cursor:pointer}
"""

_SCRIPT = """
const token = new URLSearchParams(location.search).get('t');
const undoMilliseconds = Number(document.body.dataset.undoSeconds) * 1000;

function showStatus(card, kind, sentence) {
  const status = card.querySelector('.status');
  status.className = 'status ' + kind;
  status.textContent = sentence;
  status.hidden = false;
}

function showEmptyWhenNothingLeft() {
  if (!document.querySelector('.card')) {
    document.querySelector('.empty').hidden = false;
  }
}

function setChoicesEnabled(card, enabled) {
  card.querySelectorAll('[data-outcome]').forEach(button => { button.disabled = !enabled; });
}

async function send(card, outcome) {
  const note = card.querySelector('textarea').value.trim();
  let answer;
  try {
    const response = await fetch('/rule', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token, item: Number(card.dataset.item),
                            line: Number(card.dataset.line),
                            fingerprint: card.dataset.fingerprint, outcome, note: note || null}),
    });
    answer = response.ok ? await response.json()
                         : {status: 'failed', message: await response.text()};
  } catch (error) {
    answer = {status: 'failed', message: 'Das Board ist nicht erreichbar.'};
  }
  if (answer.status === 'ruled') {
    card.remove();
    showEmptyWhenNothingLeft();
    return;
  }
  card.hidden = false;
  if (answer.status === 'already_ruled') {
    setChoicesEnabled(card, false);
    showStatus(card, 'already_ruled',
               'Schon entschieden: diese Zeile wurde inzwischen woanders entschieden.');
    return;
  }
  setChoicesEnabled(card, true);
  showStatus(card, 'failed', 'Nicht gespeichert, nichts ist entschieden: '
                             + (answer.message || 'unbekannter Fehler'));
}

function choose(card, button) {
  const outcome = button.dataset.outcome;
  card.hidden = true;
  setChoicesEnabled(card, false);
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.setAttribute('role', 'status');
  const message = document.createElement('span');
  message.className = 'msg';
  message.textContent = button.textContent + ': ' + card.querySelector('.question').textContent;
  const undo = document.createElement('button');
  undo.textContent = 'Rückgängig';
  toast.append(message, undo);
  document.querySelector('.toasts').append(toast);
  const timer = setTimeout(() => { toast.remove(); send(card, outcome); }, undoMilliseconds);
  undo.addEventListener('click', () => {
    clearTimeout(timer);
    toast.remove();
    card.hidden = false;
    setChoicesEnabled(card, true);
  });
}

document.querySelectorAll('.card').forEach(card => {
  card.querySelectorAll('[data-outcome]').forEach(button => {
    button.addEventListener('click', () => choose(card, button));
  });
});
"""


def content_security_policy(nonce: str | None) -> str:
    """Only images from data URIs; script and style only from this page's nonce."""
    script_and_style = (
        f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; " if nonce is not None else ""
    )
    return (
        f"default-src 'none'; {script_and_style}img-src data:; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def render_board(open_lines: Sequence[ExpectationLine], nonce: str) -> str:
    cards = "".join(_card(line) for line in open_lines)
    empty_hidden = " hidden" if open_lines else ""
    body = (
        f"<main><h1>Entscheiden</h1>{cards}"
        f'<p class="empty"{empty_hidden}>{NOTHING_OPEN}</p></main>'
        '<div class="toasts" aria-live="polite"></div>'
        f'<script nonce="{nonce}">{_SCRIPT}</script>'
    )
    return _document(body, nonce, body_attributes=f' data-undo-seconds="{UNDO_SECONDS}"')


def render_unavailable(sentence: str, nonce: str) -> str:
    body = (
        '<main><h1>Entscheiden</h1><p class="unavailable">'
        f"aco konnte nicht gelesen werden: {escape(sentence)}</p></main>"
    )
    return _document(body, nonce, body_attributes="")


def _document(body: str, nonce: str, *, body_attributes: str) -> str:
    return (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Board</title><style nonce="{nonce}">{_STYLE}</style></head>'
        f"<body{body_attributes}>{body}</body></html>"
    )


def _card(line: ExpectationLine) -> str:
    heading = line.question if line.question is not None else line.text
    sentence = f'<p class="text">{escape(line.text)}</p>' if line.question is not None else ""
    example = f'<p class="example">{escape(line.example)}</p>' if line.example is not None else ""
    picture = _picture(line.picture) if line.picture is not None else ""
    buttons = "".join(
        f'<button type="button" data-outcome="{outcome.value}">{label}</button>'
        for outcome, label in ((Outcome.YES, "Ja"), (Outcome.NO, "Nein"))
    )
    return (
        f'<article class="card" data-item="{line.item}" data-line="{line.index}" '
        f'data-fingerprint="{line.fingerprint}">'
        f'<p class="item">#{line.item} · {escape(line.item_title)} · Zeile {line.index}</p>'
        f'<p class="question">{escape(heading)}</p>{sentence}{example}{picture}'
        f'<textarea rows="2" maxlength="{NOTE_MAX_LENGTH}" '
        'placeholder="Notiz (optional)" aria-label="Notiz"></textarea>'
        f'<div class="choices">{buttons}</div><p class="status" role="alert" hidden></p>'
        "</article>"
    )


def _picture(svg: str) -> str:
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f'<img alt="Bild zur Frage" src="data:image/svg+xml;base64,{encoded}">'

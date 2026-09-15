"""Responde automaticamente mensagens recebidas.

Faz polling no messages.db (gravado pelo bridge) e responde via REST /api/send.
BOT_ALLOWED: numeros permitidos separados por virgula (vazio = responde todo mundo).
"""
import os
import sqlite3
import time

import requests

from whatsapp import MESSAGES_DB_PATH, send_message

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("BOT_MODEL", "@preset/whatsapp-responde")  # o preset guarda o system prompt

ALLOWED = set(filter(None, os.environ.get("BOT_ALLOWED", "").split(",")))
PREFIX = "🤖 [Automação] "
DEBOUNCE = float(os.environ.get("BOT_DEBOUNCE", "5"))  # segundos de silencio antes de responder
SESSION_GAP = float(os.environ.get("BOT_SESSION_GAP", "6")) * 3600  # horas de silencio = conversa nova
SESSION_DB_PATH = os.path.join(os.path.dirname(MESSAGES_DB_PATH), "whatsapp.db")


def my_number() -> str:
    jid = sqlite3.connect(SESSION_DB_PATH, timeout=10).execute("SELECT jid FROM whatsmeow_device").fetchone()[0]
    return jid.split(":")[0].split("@")[0]


def wants_reply(chat: str, content: str) -> bool:
    """Privado: sempre. Grupo: so quando marcam a conta (@numero aparece no texto)."""
    return not chat.endswith("@g.us") or f"@{ME}" in content


def phone_of(sender: str) -> str:
    """Remetente pode vir como LID; traduz para numero pela tabela do whatsmeow."""
    row = sqlite3.connect(SESSION_DB_PATH, timeout=10).execute(
        "SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (sender,)
    ).fetchone()
    return row[0] if row else sender


def history(db, chat: str, limit: int = 10):
    """Ultimas mensagens do chat no formato do chat completions (mais antiga primeiro).

    Corta no primeiro silencio maior que SESSION_GAP: assunto de outro dia nao entra no contexto.
    A ultima mensagem recebe a data/hora atual, para o modelo entender "hoje", "amanha", "sabado".
    """
    rows = db.execute(
        "SELECT is_from_me, content, CAST(strftime('%s', timestamp) AS INTEGER) FROM messages "
        "WHERE chat_jid = ? AND content != '' ORDER BY timestamp DESC LIMIT ?",
        (chat, limit),
    ).fetchall()
    session = []
    for i, (me, c, ts) in enumerate(rows):  # do mais novo para o mais antigo
        if i and rows[i - 1][2] - ts > SESSION_GAP:
            break
        session.append({"role": "assistant" if me else "user", "content": c})
    session.reverse()
    if session:
        session[-1]["content"] = f"[Agora: {time.strftime('%Y-%m-%d %H:%M (%A)')}]\n" + session[-1]["content"]
    return session


def reply(messages) -> str:
    if not OPENROUTER_KEY:
        return "Recebi: " + messages[-1]["content"]
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
        json={"model": MODEL, "messages": messages},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def new_messages(db, after_rowid):
    return db.execute(
        "SELECT rowid, chat_jid, sender, content, strftime('%s', timestamp) FROM messages "
        "WHERE rowid > ? AND is_from_me = 0 AND content != '' ORDER BY rowid",
        (after_rowid,),
    ).fetchall()


def main():
    db = sqlite3.connect(MESSAGES_DB_PATH, timeout=10)
    last = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]
    start = time.time()
    global ME
    ME = my_number()
    print(f"bot: escutando a partir de rowid {last}, allowed={ALLOWED or 'todos'}, eu={ME}", flush=True)
    pending = {}  # chat -> (hora da ultima msg recebida, textos acumulados)
    while True:
        for rowid, chat, sender, content, ts in new_messages(db, last):
            last = rowid
            if int(ts) < start or (ALLOWED and phone_of(sender) not in ALLOWED) or not wants_reply(chat, content):
                continue  # historico antigo, remetente nao permitido, ou grupo sem mencao
            pending.setdefault(chat, [0, []])
            pending[chat][0] = time.time()
            pending[chat][1].append(content)
        # so responde quando o chat fica DEBOUNCE segundos sem msg nova: junta "oi" / "tudo bem?" / "..."
        for chat, (t, texts) in list(pending.items()):
            if time.time() - t < DEBOUNCE:
                continue
            del pending[chat]
            try:
                answer = reply(history(db, chat))  # historico ja contem todas as msgs acumuladas
            except Exception as e:  # modelo free fora do ar / rate limit: pula, nao derruba o bot
                print(f"{chat} <- {texts!r} -> erro no modelo: {e}", flush=True)
                continue
            ok, msg = send_message(chat, PREFIX + answer)
            print(f"{chat} <- {texts!r} -> {answer!r} ({ok} {msg})", flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()

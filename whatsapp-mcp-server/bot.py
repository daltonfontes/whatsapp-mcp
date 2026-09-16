"""Responde automaticamente mensagens recebidas.

Faz polling no messages.db (gravado pelo bridge) e responde via REST /api/send.
Config por conta na tabela bot_accounts (criada aqui). Conta nova nasce com os defaults do env:
BOT_MODEL, BOT_SYSTEM_PROMPT e BOT_ALLOWED (numeros separados por virgula, vazio = responde todo mundo).

Agentes (tabela bot_agents, editada pelo painel): cada um tem nome, descricao, prompt e modelo opcional.
Com agentes cadastrados, uma chamada extra ao modelo escolhe pelo nome qual responde; o prompt do agente
entra depois do prompt da conta (que segue valendo como base). Nenhum bate = so o prompt da conta.

Comandos: mensagens enviadas do celular da propria conta. A confirmacao chega no seu chat com voce mesmo.
  /pausar             para de responder no chat onde foi enviado
  /voltar             volta a responder nesse chat
  /pausar 5511999...  para de responder para esse numero (de qualquer chat)
  /voltar 5511999...  volta a responder para esse numero
  /pausar tudo        para a conta inteira
  /voltar tudo        religa a conta
"""
import os
import sqlite3
import time

import requests

from whatsapp import MESSAGES_DB_PATH, send_message

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DEFAULTS = {  # usados so na primeira vez que uma conta aparece; depois vale a tabela
    "model": os.environ.get("BOT_MODEL", "@preset/whatsapp-responde"),
    "system_prompt": os.environ.get("BOT_SYSTEM_PROMPT", ""),
    "allowed": os.environ.get("BOT_ALLOWED", ""),
}
PREFIX = "🤖 [Automação] "
DEBOUNCE = float(os.environ.get("BOT_DEBOUNCE", "5"))  # segundos de silencio antes de responder
SESSION_GAP = float(os.environ.get("BOT_SESSION_GAP", "6")) * 3600  # horas de silencio = conversa nova
SESSION_DB_PATH = os.path.join(os.path.dirname(MESSAGES_DB_PATH), "whatsapp.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_accounts (
    account_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    model TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    allowed TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bot_paused_chats (
    account_id TEXT,
    chat_jid TEXT,
    PRIMARY KEY (account_id, chat_jid)
);
CREATE TABLE IF NOT EXISTS bot_agents (
    account_id TEXT,
    name TEXT,
    description TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account_id, name)
);
"""


def config(db, account: str) -> dict:
    """Config da conta; cria com os defaults do env na primeira mensagem vista."""
    db.execute(
        "INSERT OR IGNORE INTO bot_accounts (account_id, model, system_prompt, allowed) VALUES (?, ?, ?, ?)",
        (account, DEFAULTS["model"], DEFAULTS["system_prompt"], DEFAULTS["allowed"]),
    )
    db.commit()
    enabled, model, prompt, allowed = db.execute(
        "SELECT enabled, model, system_prompt, allowed FROM bot_accounts WHERE account_id = ?", (account,)
    ).fetchone()
    return {"enabled": bool(enabled), "model": model, "system_prompt": prompt, "allowed": set(filter(None, allowed.split(",")))}


def is_paused(db, account: str, chat: str) -> bool:
    return db.execute("SELECT 1 FROM bot_paused_chats WHERE account_id = ? AND chat_jid = ?", (account, chat)).fetchone() is not None


def lid_of(number: str) -> str | None:
    """Numero -> LID pela tabela do whatsmeow. Quase todo chat privado hoje chega como <lid>@lid."""
    row = sqlite3.connect(SESSION_DB_PATH, timeout=10).execute(
        "SELECT lid FROM whatsmeow_lid_map WHERE pn = ?", (number,)
    ).fetchone()
    return row[0] if row else None


def chat_jids(number: str) -> list[str]:
    """Os JIDs pelos quais o chat com esse numero pode aparecer no messages.db."""
    jids = [f"{number}@s.whatsapp.net"]
    if lid := lid_of(number):
        jids.append(f"{lid}@lid")
    return jids


def handle_command(db, account: str, chat: str, text: str) -> str | None:
    """Comando enviado pelo dono da conta. Devolve a confirmacao, ou None se nao e comando."""
    parts = text.strip().lower().split()
    if not parts or parts[0] not in ("/pausar", "/voltar"):
        return None
    pause = parts[0] == "/pausar"
    target = " ".join(parts[1:])
    if target == "tudo":
        config(db, account)  # garante a linha
        db.execute("UPDATE bot_accounts SET enabled = ? WHERE account_id = ?", (0 if pause else 1, account))
        msg = "bot pausado em todos os chats" if pause else "bot religado em todos os chats"
    else:
        chats, label = [chat], chat.split("@")[0]
        if target:
            label = "".join(filter(str.isdigit, target))
            chats = chat_jids(label)
        for c in chats:
            if pause:
                db.execute("INSERT OR IGNORE INTO bot_paused_chats VALUES (?, ?)", (account, c))
            else:
                db.execute("DELETE FROM bot_paused_chats WHERE account_id = ? AND chat_jid = ?", (account, c))
        msg = f"bot {'pausado' if pause else 'de volta'} no chat {label}"
    db.commit()
    return msg


def wants_reply(chat: str, content: str, me: str) -> bool:
    """Privado: sempre. Grupo: so quando marcam a conta (@numero aparece no texto)."""
    return not chat.endswith("@g.us") or f"@{me}" in content


def phone_of(sender: str) -> str:
    """Remetente pode vir como LID; traduz para numero pela tabela do whatsmeow."""
    row = sqlite3.connect(SESSION_DB_PATH, timeout=10).execute(
        "SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (sender,)
    ).fetchone()
    return row[0] if row else sender


def history(db, account: str, chat: str, limit: int = 10):
    """Ultimas mensagens do chat no formato do chat completions (mais antiga primeiro).

    Corta no primeiro silencio maior que SESSION_GAP: assunto de outro dia nao entra no contexto.
    Comandos (/...) ficam de fora e o PREFIX das respostas do bot e removido, senao o modelo o copia.
    A ultima mensagem recebe a data/hora atual, para o modelo entender "hoje", "amanha", "sabado".
    """
    rows = db.execute(
        "SELECT is_from_me, content, CAST(strftime('%s', timestamp) AS INTEGER) FROM messages "
        "WHERE account_id = ? AND chat_jid = ? AND content != '' AND content NOT LIKE '/%' "
        "ORDER BY timestamp DESC LIMIT ?",
        (account, chat, limit),
    ).fetchall()
    session = []
    for i, (me, c, ts) in enumerate(rows):  # do mais novo para o mais antigo
        if i and rows[i - 1][2] - ts > SESSION_GAP:
            break
        session.append({"role": "assistant" if me else "user", "content": c.removeprefix(PREFIX)})
    session.reverse()
    if session:
        session[-1]["content"] = f"[Agora: {time.strftime('%Y-%m-%d %H:%M (%A)')}]\n" + session[-1]["content"]
    return session


def agents(db, account: str) -> list[dict]:
    return [
        dict(zip(("name", "description", "system_prompt", "model"), row))
        for row in db.execute(
            "SELECT name, description, system_prompt, model FROM bot_agents WHERE account_id = ? ORDER BY rowid", (account,)
        )
    ]


def chat(model: str, messages) -> str:
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
        json={"model": model, "messages": messages},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def route(cfg: dict, agents: list[dict], messages) -> dict | None:
    """Escolhe o agente lendo as descricoes; None se nenhum serve ou se o roteador falhar.

    ponytail: roteia a cada resposta (uma chamada extra). Se o agente ficar trocando no meio da
    conversa ou o rate limit do modelo free apertar, guardar o agente escolhido por chat.
    """
    if not agents:
        return None
    menu = "\n".join(f"- {a['name']}: {a['description']}" for a in agents)
    ask = [{"role": "system", "content": f"Escolha qual agente deve responder a conversa.\nAgentes:\n{menu}\n\n"
            "Responda somente com o nome do agente, ou 'nenhum'."}] + messages[-4:]
    try:
        choice = chat(cfg["model"], ask).lower()
    except Exception as e:
        print(f"roteador falhou, usando o prompt da conta: {e}", flush=True)
        return None
    return next((a for a in agents if a["name"].lower() in choice), None)


def reply(cfg: dict, agents: list[dict], messages) -> tuple[str, str]:
    """Devolve (resposta, nome do agente que respondeu ou '')."""
    if not OPENROUTER_KEY:
        return "Recebi: " + messages[-1]["content"], ""
    agent = route(cfg, agents, messages) or {"name": "", "system_prompt": "", "model": ""}
    prompt = "\n\n".join(filter(None, (cfg["system_prompt"], agent["system_prompt"])))
    if prompt:
        messages = [{"role": "system", "content": prompt}] + messages
    return chat(agent["model"] or cfg["model"], messages), agent["name"]


def new_messages(db, after_rowid):
    return db.execute(
        "SELECT rowid, account_id, chat_jid, sender, content, is_from_me, strftime('%s', timestamp) FROM messages "
        "WHERE rowid > ? AND content != '' ORDER BY rowid",
        (after_rowid,),
    ).fetchall()


def main():
    db = sqlite3.connect(MESSAGES_DB_PATH, timeout=10)
    db.executescript(SCHEMA)
    last = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]
    start = time.time()
    print(f"bot: escutando a partir de rowid {last}", flush=True)
    pending = {}  # (conta, chat) -> (hora da ultima msg recebida, textos acumulados)
    while True:
        for rowid, account, chat, sender, content, from_me, ts in new_messages(db, last):
            last = rowid
            if int(ts) < start:
                continue  # historico antigo
            if from_me:
                if msg := handle_command(db, account, chat, content):
                    send_message(f"{account}@s.whatsapp.net", PREFIX + msg, account)
                    print(f"[{account}] comando {content!r}: {msg}", flush=True)
                continue
            cfg = config(db, account)
            if (cfg["allowed"] and phone_of(sender) not in cfg["allowed"]) or not wants_reply(chat, content, account):
                continue  # remetente nao permitido, ou grupo sem mencao
            pending.setdefault((account, chat), [0, []])
            pending[(account, chat)][0] = time.time()
            pending[(account, chat)][1].append(content)
        # so responde quando o chat fica DEBOUNCE segundos sem msg nova: junta "oi" / "tudo bem?" / "..."
        for (account, chat), (t, texts) in list(pending.items()):
            if time.time() - t < DEBOUNCE:
                continue
            del pending[(account, chat)]
            cfg = config(db, account)  # checado na hora de responder: /pausar durante o debounce ja vale
            if not cfg["enabled"] or is_paused(db, account, chat):
                print(f"[{account}] {chat} <- {texts!r} -> pausado", flush=True)
                continue
            try:
                answer, who = reply(cfg, agents(db, account), history(db, account, chat))  # historico ja tem as msgs acumuladas
            except Exception as e:  # modelo free fora do ar / rate limit: pula, nao derruba o bot
                body = getattr(getattr(e, "response", None), "text", "")[:300]  # o OpenRouter explica o erro no corpo
                print(f"[{account}] {chat} <- {texts!r} -> erro no modelo: {e} {body}", flush=True)
                continue
            ok, msg = send_message(chat, PREFIX + answer, account)
            print(f"[{account}] {chat} <- {texts!r} -> [{who or 'conta'}] {answer!r} ({ok} {msg})", flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()

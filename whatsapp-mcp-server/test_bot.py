"""python test_bot.py: comandos de pausa e config por conta, em memoria."""
import sqlite3

import bot


def test_commands():
    db = sqlite3.connect(":memory:")
    db.executescript(bot.SCHEMA)
    a, c = "5511", "5522@s.whatsapp.net"
    assert bot.handle_command(db, a, c, "oi, tudo bem?") is None
    assert bot.config(db, a)["enabled"] and not bot.is_paused(db, a, c)

    assert bot.handle_command(db, a, c, "/pausar")
    assert bot.is_paused(db, a, c)
    assert bot.handle_command(db, a, c, " /Voltar ")
    assert not bot.is_paused(db, a, c)

    bot.lid_of = lambda n: "999" if n == "5522" else None  # sem whatsapp.db no teste
    assert bot.handle_command(db, a, f"{a}@s.whatsapp.net", "/pausar +55 22")  # de outro chat, mirando o numero
    assert bot.is_paused(db, a, c) and bot.is_paused(db, a, "999@lid")  # pausa o chat por numero e por LID
    assert bot.handle_command(db, a, f"{a}@s.whatsapp.net", "/voltar 5522")
    assert not bot.is_paused(db, a, c) and not bot.is_paused(db, a, "999@lid")

    assert bot.handle_command(db, a, c, "/pausar tudo")
    assert not bot.config(db, a)["enabled"]
    assert bot.handle_command(db, a, c, "/voltar tudo")
    assert bot.config(db, a)["enabled"]
    assert bot.is_paused(db, "outra", c) is False  # pausa e por conta


def test_route():
    db = sqlite3.connect(":memory:")
    db.executescript(bot.SCHEMA)
    a = "5511"
    db.execute("INSERT INTO bot_agents VALUES (?, 'Vendas', 'precos e pedidos', 'Feche a venda.', '')", (a,))
    db.execute("INSERT INTO bot_agents VALUES (?, 'Suporte', 'defeitos e trocas', 'Resolva o problema.', 'x/forte')", (a,))
    cfg = {"model": "x/base", "system_prompt": "Voce e a loja."}
    calls = []
    bot.OPENROUTER_KEY = "k"
    bot.chat = lambda model, msgs: calls.append((model, msgs)) or ("suporte" if len(calls) == 1 else "ok")

    answer, who = bot.reply(cfg, bot.agents(db, a), [{"role": "user", "content": "quebrou"}])
    assert (answer, who) == ("ok", "Suporte")
    assert calls[0][0] == "x/base" and "- Vendas: precos e pedidos" in calls[0][1][0]["content"]  # roteador: modelo da conta, menu
    assert calls[1][0] == "x/forte" and calls[1][1][0]["content"] == "Voce e a loja.\n\nResolva o problema."  # agente: seu modelo, prompt empilhado

    calls.clear()
    bot.chat = lambda model, msgs: calls.append(model) or ("nenhum" if len(calls) == 1 else "ok")
    assert bot.reply(cfg, bot.agents(db, a), [{"role": "user", "content": "oi"}]) == ("ok", "")  # nenhum bate = prompt da conta
    calls.clear()
    bot.chat = lambda model, msgs: calls.append(model) or "ok"
    assert bot.reply(cfg, [],[{"role": "user", "content": "oi"}]) == ("ok", "") and calls == ["x/base"]  # sem agentes = 1 chamada


def test_restart_catchup():
    db = sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE messages (account_id, chat_jid, content, timestamp, is_from_me);")
    a, c = "5511", "5522@s.whatsapp.net"
    assert bot.resume_from(db, 0) == 0  # banco vazio
    db.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", [
        (a, c, "ontem", "2024-01-01 10:00:00", 0),
        (a, c, "resposta", "2024-01-01 10:01:00", 1),
        (a, c, "chegou no restart", "2024-01-02 10:00:00", 0),
    ])
    cutoff = 1704189600  # 2024-01-02 10:00:00 UTC
    assert bot.resume_from(db, cutoff) == 2  # reprocessa so a que chegou no restart
    assert bot.resume_from(db, cutoff + 1) == 3  # nada tao novo: so daqui em diante
    assert not bot.answered(db, a, c)
    db.execute("INSERT INTO messages VALUES (?, ?, 'resp', '2024-01-02 10:00:30', 1)", (a, c))
    assert bot.answered(db, a, c)  # ultima e nossa: nao responde de novo
    assert not bot.answered(db, a, "outro@s.whatsapp.net")


if __name__ == "__main__":
    test_commands()
    test_route()
    test_restart_catchup()
    print("ok")

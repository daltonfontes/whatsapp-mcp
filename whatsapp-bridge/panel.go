package main

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"net/http"
	"sort"
	"strconv"
	"sync"
	"time"

	"go.mau.fi/whatsmeow/types"
)

// Panel: static page at / plus the bot-config and chat endpoints it needs.
// The bot tables are shared with whatsapp-mcp-server/bot.py, which reads them on every reply.

//go:embed panel.html
var panelHTML []byte

type BotConfig struct {
	Exists       bool   `json:"exists"` // false = bot will seed this row from its env defaults
	Enabled      bool   `json:"enabled"`
	Model        string `json:"model"`
	SystemPrompt string `json:"system_prompt"`
	Allowed      string `json:"allowed"`
}

type ChatRow struct {
	JID             string    `json:"jid"`
	Phone           string    `json:"phone"` // resolved from the LID map for @lid chats
	Name            string    `json:"name"`  // as seen on WhatsApp
	ContactName     string    `json:"contact_name"`
	Notes           string    `json:"notes"`
	LastMessageTime time.Time `json:"last_message_time"`
	LastMessage     string    `json:"last_message"`
	LastFromMe      bool      `json:"last_from_me"`
	Paused          bool      `json:"paused"`
}

type Contact struct {
	Name  string `json:"name"`
	Notes string `json:"notes"`
}

func (store *MessageStore) SetContact(account, chatJID string, c Contact) error {
	_, err := store.db.Exec(
		"INSERT OR REPLACE INTO contacts (account_id, jid, name, notes) VALUES (?, ?, ?, ?)",
		account, chatJID, c.Name, c.Notes,
	)
	return err
}

type MessageRow struct {
	ID        string    `json:"id"`
	Sender    string    `json:"sender"`
	Content   string    `json:"content"`
	Time      time.Time `json:"time"`
	FromMe    bool      `json:"from_me"`
	MediaType string    `json:"media_type"`
}

func (store *MessageStore) GetBotConfig(account string) (BotConfig, error) {
	c := BotConfig{Enabled: true}
	err := store.db.QueryRow(
		"SELECT enabled, model, system_prompt, allowed FROM bot_accounts WHERE account_id = ?", account,
	).Scan(&c.Enabled, &c.Model, &c.SystemPrompt, &c.Allowed)
	if err == sql.ErrNoRows {
		return c, nil
	}
	c.Exists = true
	return c, err
}

func (store *MessageStore) SetBotConfig(account string, c BotConfig) error {
	_, err := store.db.Exec(
		"INSERT OR REPLACE INTO bot_accounts (account_id, enabled, model, system_prompt, allowed) VALUES (?, ?, ?, ?, ?)",
		account, c.Enabled, c.Model, c.SystemPrompt, c.Allowed,
	)
	return err
}

func (store *MessageStore) SetPaused(account, chatJID string, paused bool) error {
	q := "DELETE FROM bot_paused_chats WHERE account_id = ? AND chat_jid = ?"
	if paused {
		q = "INSERT OR IGNORE INTO bot_paused_chats (account_id, chat_jid) VALUES (?, ?)"
	}
	_, err := store.db.Exec(q, account, chatJID)
	return err
}

func (store *MessageStore) ListChats(account string, limit int) ([]ChatRow, error) {
	rows, err := store.db.Query(`
		SELECT c.jid, COALESCE(c.name, ''), COALESCE(k.name, ''), COALESCE(k.notes, ''), c.last_message_time,
			COALESCE(m.content, ''), COALESCE(m.is_from_me, 0),
			EXISTS(SELECT 1 FROM bot_paused_chats p WHERE p.account_id = c.account_id AND p.chat_jid = c.jid)
		FROM chats c
		LEFT JOIN contacts k ON k.account_id = c.account_id AND k.jid = c.jid
		LEFT JOIN messages m ON m.rowid = (SELECT rowid FROM messages WHERE account_id = c.account_id AND chat_jid = c.jid ORDER BY timestamp DESC LIMIT 1)
		WHERE c.account_id = ? ORDER BY c.last_message_time DESC LIMIT ?`, account, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ChatRow{}
	for rows.Next() {
		var c ChatRow
		if err := rows.Scan(&c.JID, &c.Name, &c.ContactName, &c.Notes, &c.LastMessageTime, &c.LastMessage, &c.LastFromMe, &c.Paused); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

func (store *MessageStore) ListMessages(account, chatJID string, limit int) ([]MessageRow, error) {
	rows, err := store.db.Query(`
		SELECT id, sender, content, timestamp, is_from_me, COALESCE(media_type, '') FROM messages
		WHERE account_id = ? AND chat_jid = ? ORDER BY timestamp DESC LIMIT ?`, account, chatJID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []MessageRow{}
	for rows.Next() {
		var m MessageRow
		if err := rows.Scan(&m.ID, &m.Sender, &m.Content, &m.Time, &m.FromMe, &m.MediaType); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

type Model struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Context int    `json:"context_length"`
}

var modelsCache struct {
	sync.Mutex
	at   time.Time
	list []Model
}

// freeModels lists OpenRouter models with zero prompt and completion price, cached for an hour.
func freeModels() ([]Model, error) {
	modelsCache.Lock()
	defer modelsCache.Unlock()
	if modelsCache.list != nil && time.Since(modelsCache.at) < time.Hour {
		return modelsCache.list, nil
	}
	client := http.Client{Timeout: 15 * time.Second}
	resp, err := client.Get("https://openrouter.ai/api/v1/models")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var body struct {
		Data []struct {
			ID            string `json:"id"`
			Name          string `json:"name"`
			ContextLength int    `json:"context_length"`
			Pricing       struct {
				Prompt     string `json:"prompt"`
				Completion string `json:"completion"`
			} `json:"pricing"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	list := []Model{}
	for _, m := range body.Data {
		if m.Pricing.Prompt == "0" && m.Pricing.Completion == "0" {
			list = append(list, Model{m.ID, m.Name, m.ContextLength})
		}
	}
	sort.Slice(list, func(i, j int) bool { return list[i].Name < list[j].Name })
	modelsCache.at, modelsCache.list = time.Now(), list
	return list, nil
}

func limitParam(r *http.Request, def int) int {
	if n, err := strconv.Atoi(r.URL.Query().Get("limit")); err == nil && n > 0 && n <= 500 {
		return n
	}
	return def
}

// resolvePhones fills ChatRow.Phone for @lid chats using the account's LID map.
func resolvePhones(bridge *Bridge, account string, chats []ChatRow) {
	client, err := bridge.Client(account)
	if err != nil {
		return
	}
	for i, c := range chats {
		jid, err := types.ParseJID(c.JID)
		if err != nil {
			continue
		}
		switch jid.Server {
		case types.DefaultUserServer:
			chats[i].Phone = jid.User
		case types.HiddenUserServer:
			if pn, err := client.Store.LIDs.GetPNForLID(context.Background(), jid); err == nil && !pn.IsEmpty() {
				chats[i].Phone = pn.User
			}
		}
	}
}

func registerPanel(mux *http.ServeMux, bridge *Bridge) {
	store := bridge.store
	fail := func(w http.ResponseWriter, err error) {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
	}

	mux.HandleFunc("GET /api/models", func(w http.ResponseWriter, r *http.Request) {
		list, err := freeModels()
		if err != nil {
			fail(w, err)
			return
		}
		writeJSON(w, http.StatusOK, list)
	})

	mux.HandleFunc("GET /api/accounts/{id}/bot", func(w http.ResponseWriter, r *http.Request) {
		c, err := store.GetBotConfig(r.PathValue("id"))
		if err != nil {
			fail(w, err)
			return
		}
		writeJSON(w, http.StatusOK, c)
	})

	mux.HandleFunc("PUT /api/accounts/{id}/bot", func(w http.ResponseWriter, r *http.Request) {
		var c BotConfig
		if err := json.NewDecoder(r.Body).Decode(&c); err != nil || c.Model == "" {
			http.Error(w, "enabled, model (required), system_prompt and allowed expected", http.StatusBadRequest)
			return
		}
		if err := store.SetBotConfig(r.PathValue("id"), c); err != nil {
			fail(w, err)
			return
		}
		c.Exists = true
		writeJSON(w, http.StatusOK, c)
	})

	mux.HandleFunc("GET /api/accounts/{id}/chats", func(w http.ResponseWriter, r *http.Request) {
		chats, err := store.ListChats(r.PathValue("id"), limitParam(r, 50))
		if err != nil {
			fail(w, err)
			return
		}
		resolvePhones(bridge, r.PathValue("id"), chats)
		writeJSON(w, http.StatusOK, chats)
	})

	mux.HandleFunc("GET /api/accounts/{id}/chats/{jid}/messages", func(w http.ResponseWriter, r *http.Request) {
		msgs, err := store.ListMessages(r.PathValue("id"), r.PathValue("jid"), limitParam(r, 50))
		if err != nil {
			fail(w, err)
			return
		}
		writeJSON(w, http.StatusOK, msgs)
	})

	mux.HandleFunc("PUT /api/accounts/{id}/chats/{jid}/contact", func(w http.ResponseWriter, r *http.Request) {
		var c Contact
		if err := json.NewDecoder(r.Body).Decode(&c); err != nil {
			http.Error(w, "name and notes expected", http.StatusBadRequest)
			return
		}
		if err := store.SetContact(r.PathValue("id"), r.PathValue("jid"), c); err != nil {
			fail(w, err)
			return
		}
		writeJSON(w, http.StatusOK, c)
	})

	for _, m := range []struct {
		method string
		paused bool
	}{{"PUT", true}, {"DELETE", false}} {
		paused := m.paused
		mux.HandleFunc(m.method+" /api/accounts/{id}/chats/{jid}/paused", func(w http.ResponseWriter, r *http.Request) {
			if err := store.SetPaused(r.PathValue("id"), r.PathValue("jid"), paused); err != nil {
				fail(w, err)
				return
			}
			writeJSON(w, http.StatusOK, map[string]bool{"paused": paused})
		})
	}
}

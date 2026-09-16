package main

import (
	"database/sql"
	"path/filepath"
	"testing"
	"time"
)

// Old single-account schema must be rebuilt in place, keeping rows, and the same chat JID
// must then be storable under two accounts.
func TestMigrateOldSchema(t *testing.T) {
	path := filepath.Join(t.TempDir(), "messages.db")
	db, err := sql.Open("sqlite3", "file:"+path+"?_foreign_keys=on")
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(`
		CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
		CREATE TABLE messages (id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP, is_from_me BOOLEAN,
			media_type TEXT, filename TEXT, url TEXT, media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
			PRIMARY KEY (id, chat_jid), FOREIGN KEY (chat_jid) REFERENCES chats(jid));
		INSERT INTO chats VALUES ('1@s.whatsapp.net', 'A', '2024-01-01 00:00:00');
		INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES ('m1', '1@s.whatsapp.net', '1', 'hi', '2024-01-01 00:00:00', 0);
	`)
	if err != nil {
		t.Fatal(err)
	}
	db.Close()

	store, err := NewMessageStore(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AdoptOrphans("5511"); err != nil {
		t.Fatal(err)
	}
	var acc, content string
	if err := store.db.QueryRow("SELECT account_id, content FROM messages WHERE id = 'm1'").Scan(&acc, &content); err != nil {
		t.Fatal(err)
	}
	if acc != "5511" || content != "hi" {
		t.Fatalf("migrated row = %q %q", acc, content)
	}
	store.Close()

	// Reopening is a no-op migration.
	store, err = NewMessageStore(path)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	for _, a := range []string{"5511", "5522"} {
		if err := store.StoreChat(a, "g@g.us", "G", time.Now()); err != nil {
			t.Fatal(err)
		}
		if err := store.StoreMessage(a, "x", "g@g.us", "9", "yo", time.Now(), false, "", "", "", nil, nil, nil, 0); err != nil {
			t.Fatal(err)
		}
	}
	var n int
	store.db.QueryRow("SELECT COUNT(*) FROM chats WHERE jid = 'g@g.us'").Scan(&n)
	if n != 2 {
		t.Fatalf("expected 2 chats for same jid, got %d", n)
	}
}

func TestAgentsRoundTrip(t *testing.T) {
	store, err := NewMessageStore(filepath.Join(t.TempDir(), "messages.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	want := []Agent{{Name: "Vendas", Description: "precos"}, {Name: "Suporte", SystemPrompt: "ajude", Model: "x/y"}}
	if err := store.SetAgents("5511", want); err != nil {
		t.Fatal(err)
	}
	if err := store.SetAgents("5511", want[1:]); err != nil { // replace, not append
		t.Fatal(err)
	}
	got, err := store.ListAgents("5511")
	if err != nil || len(got) != 1 || got[0] != want[1] {
		t.Fatalf("got %+v, %v", got, err)
	}
	if err := store.SetAgents("5511", []Agent{{Name: "a"}, {Name: "a"}}); err == nil {
		t.Fatal("duplicate names must fail")
	}
	if got, _ := store.ListAgents("5511"); len(got) != 1 { // failed replace rolled back
		t.Fatalf("rollback lost, got %+v", got)
	}
}

package main

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"os"
	"sync"
	"time"

	"github.com/mdp/qrterminal"
	"rsc.io/qr"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// Bridge holds one whatsmeow client per paired account. Account id = phone number of the device.
type Bridge struct {
	container *sqlstore.Container
	store     *MessageStore
	logger    waLog.Logger

	mu      sync.Mutex
	clients map[string]*whatsmeow.Client // account id -> client
	logins  map[string]*Login            // login id -> QR pairing in progress
}

// Login is the state of a QR pairing, polled by GET /api/logins/{id}.
type Login struct {
	Status  string `json:"status"` // pending | success | timeout | error
	Account string `json:"account,omitempty"`
	QR      string `json:"qr,omitempty"` // data:image/png;base64,... while pending
	Error   string `json:"error,omitempty"`
}

type AccountInfo struct {
	ID        string `json:"id"`
	Connected bool   `json:"connected"`
}

func NewBridge(container *sqlstore.Container, store *MessageStore, logger waLog.Logger) *Bridge {
	return &Bridge{container: container, store: store, logger: logger,
		clients: map[string]*whatsmeow.Client{}, logins: map[string]*Login{}}
}

func accountOf(client *whatsmeow.Client) string {
	if client.Store.ID == nil {
		return ""
	}
	return client.Store.ID.User
}

func (b *Bridge) newClient(device *store.Device) *whatsmeow.Client {
	client := whatsmeow.NewClient(device, b.logger)
	client.AddEventHandler(func(evt interface{}) {
		switch v := evt.(type) {
		case *events.Message:
			handleMessage(client, b.store, v, b.logger)
		case *events.HistorySync:
			handleHistorySync(client, b.store, v, b.logger)
		case *events.Connected:
			b.logger.Infof("[%s] connected", accountOf(client))
		case *events.LoggedOut:
			b.logger.Warnf("[%s] logged out from phone, removing account", accountOf(client))
			b.remove(accountOf(client))
		}
	})
	return client
}

// Start connects every device already paired in the store.
func (b *Bridge) Start(ctx context.Context) error {
	devices, err := b.container.GetAllDevices(ctx)
	if err != nil {
		return err
	}
	for _, d := range devices {
		if d.ID == nil {
			continue
		}
		client := b.newClient(d)
		if err := client.Connect(); err != nil {
			b.logger.Errorf("[%s] connect failed: %v", d.ID.User, err)
			continue
		}
		b.mu.Lock()
		b.clients[d.ID.User] = client
		b.mu.Unlock()
	}
	return nil
}

// StartLogin creates a fresh device and begins QR pairing. The QR is exposed via Login;
// printQR also draws it on stdout (useful when reading container logs).
func (b *Bridge) StartLogin(printQR bool) (string, error) {
	client := b.newClient(b.container.NewDevice())
	qrChan, err := client.GetQRChannel(context.Background())
	if err != nil {
		return "", err
	}
	if err := client.Connect(); err != nil {
		return "", err
	}
	buf := make([]byte, 8)
	rand.Read(buf)
	id := hex.EncodeToString(buf)
	login := &Login{Status: "pending"}
	b.mu.Lock()
	b.logins[id] = login
	b.mu.Unlock()

	go func() {
		for evt := range qrChan {
			b.mu.Lock()
			switch evt.Event {
			case "code":
				if img, err := qr.Encode(evt.Code, qr.L); err == nil {
					login.QR = "data:image/png;base64," + base64.StdEncoding.EncodeToString(img.PNG())
				}
				if printQR {
					fmt.Printf("\nScan this QR code with WhatsApp (login %s):\n", id)
					qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
				}
			case "success":
				login.Status, login.Account, login.QR = "success", accountOf(client), ""
				b.clients[login.Account] = client
				b.logger.Infof("[%s] paired", login.Account)
			default: // "timeout" or "err-*"
				login.Status, login.QR = evt.Event, ""
				if evt.Error != nil {
					login.Error = evt.Error.Error()
				}
				client.Disconnect()
			}
			b.mu.Unlock()
		}
	}()
	// ponytail: pending logins are memory only; forgotten after 10 minutes.
	time.AfterFunc(10*time.Minute, func() {
		b.mu.Lock()
		delete(b.logins, id)
		b.mu.Unlock()
	})
	return id, nil
}

func (b *Bridge) Login(id string) (Login, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	l, ok := b.logins[id]
	if !ok {
		return Login{}, false
	}
	return *l, true
}

// Client returns the client for an account. Empty account is allowed when exactly one exists.
func (b *Bridge) Client(account string) (*whatsmeow.Client, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if account == "" {
		if len(b.clients) != 1 {
			return nil, fmt.Errorf("account is required (%d accounts registered)", len(b.clients))
		}
		for _, c := range b.clients {
			return c, nil
		}
	}
	c, ok := b.clients[account]
	if !ok {
		return nil, fmt.Errorf("unknown account %q", account)
	}
	return c, nil
}

func (b *Bridge) Accounts() []AccountInfo {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := []AccountInfo{}
	for id, c := range b.clients {
		out = append(out, AccountInfo{ID: id, Connected: c.IsConnected()})
	}
	return out
}

// Logout unpairs the account on the phone side and drops it.
func (b *Bridge) Logout(account string) error {
	client, err := b.Client(account)
	if err != nil {
		return err
	}
	if err := client.Logout(context.Background()); err != nil {
		return err
	}
	b.remove(accountOf(client))
	return nil
}

func (b *Bridge) remove(account string) {
	b.mu.Lock()
	delete(b.clients, account)
	b.mu.Unlock()
}

func (b *Bridge) Shutdown() {
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, c := range b.clients {
		c.Disconnect()
	}
}

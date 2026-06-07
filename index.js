const express = require("express");
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason
} = require("@whiskeysockets/baileys");

const { Pool } = require("pg");

const app = express();
app.use(express.json());

// ================= DATABASE =================
const db = new Pool({
    connectionString: process.env.DATABASE_URL,
    ssl: { rejectUnauthorized: false }
});

// ================= STATE =================
let sock;
let isConnected = false;
let qrCache = null;

// ================= CREATE TABLE =================
async function initDB() {
    await db.query(`
        CREATE TABLE IF NOT EXISTS wa_session (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    `);
}
initDB();

// ================= LOAD SESSION =================
async function loadSession() {
    const res = await db.query(
        "SELECT value FROM wa_session WHERE key='creds'"
    );

    if (res.rows.length) {
        try {
            const creds = JSON.parse(res.rows[0].value);
            return creds;
        } catch (e) {
            return null;
        }
    }
    return null;
}

// ================= SAVE SESSION =================
async function saveSession(creds) {
    await db.query(`
        INSERT INTO wa_session(key, value)
        VALUES ('creds', $1)
        ON CONFLICT (key)
        DO UPDATE SET value = $1
    `, [JSON.stringify(creds)]);
}

// ================= START WHATSAPP =================
async function start() {

    const creds = await loadSession();

    const { state, saveCreds } = await useMultiFileAuthState("./auth_tmp");

    // استبدال creds إذا موجودة
    if (creds) {
        state.creds = creds;
    }

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
        browser: ["Render", "Chrome", "120"]
    });

    sock.ev.on("creds.update", async () => {
        await saveCreds();
        await saveSession(state.creds);
    });

    sock.ev.on("connection.update", async (update) => {
        const { connection, qr, lastDisconnect } = update;

        // ================= QR =================
        if (qr) {
            qrCache = qr;

            await db.query(`
                INSERT INTO wa_session(key, value)
                VALUES ('qr', $1)
                ON CONFLICT (key)
                DO UPDATE SET value = $1
            `, [qr]);
        }

        // ================= CONNECTED =================
        if (connection === "open") {
            isConnected = true;
            qrCache = null;

            await db.query(`
                UPDATE wa_session
                SET value='connected'
                WHERE key='status'
            `);

            console.log("🔥 WhatsApp Connected");
        }

        // ================= CLOSE =================
        if (connection === "close") {
            isConnected = false;

            const code = lastDisconnect?.error?.output?.statusCode;

            if (code !== DisconnectReason.loggedOut) {
                setTimeout(start, 5000);
            }
        }
    });
}

// ================= SEND MESSAGE =================
app.post("/send", async (req, res) => {
    try {
        const { groupId, text } = req.body;

        if (!sock || !isConnected) {
            return res.status(500).json({ error: "not connected" });
        }

        await sock.sendMessage(groupId, { text });

        res.json({ ok: true });

    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// ================= STATUS =================
app.get("/status", async (req, res) => {
    res.json({
        connected: isConnected,
        hasQR: qrCache ? true : false
    });
});

// ================= QR PAGE =================
app.get("/qr", (req, res) => {
    if (isConnected) return res.send("✅ Connected");

    if (!qrCache) return res.send("⏳ QR loading...");

    res.send(`
        <html>
        <body style="background:#111;color:#fff;text-align:center;padding-top:50px">
            <h2>WhatsApp QR</h2>
            <img width="300"
                 src="https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=${qrCache}" />
        </body>
        </html>
    `);
});

// ================= START SERVER =================
const PORT = process.env.PORT || 10000;

app.listen(PORT, () => {
    console.log("🚀 Server running on", PORT);
    start();
});

const express = require("express");
const { Pool } = require("pg");
const qrcode = require("qrcode");
const pino = require("pino");

const {
    default: makeWASocket,
    DisconnectReason,
    useMultiFileAuthState
} = require("@whiskeysockets/baileys");

const app = express();
app.use(express.json());

// ================= DB =================
const db = new Pool({
    connectionString: process.env.DATABASE_URL,
    ssl: { rejectUnauthorized: false }
});

// ================= STATE =================
let sock;
let isConnected = false;
let qrCode = null;

// ================= INIT DB =================
async function initDB() {
    await db.query(`
        CREATE TABLE IF NOT EXISTS wa_session (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    `);

    console.log("✅ DB Ready");
}

// ================= SAVE SESSION =================
async function saveSession(key, value) {
    await db.query(
        `INSERT INTO wa_session(key, value)
         VALUES($1,$2)
         ON CONFLICT (key)
         DO UPDATE SET value = EXCLUDED.value`,
        [key, JSON.stringify(value)]
    );
}

// ================= LOAD SESSION =================
async function loadSession(key) {
    const res = await db.query(
        `SELECT value FROM wa_session WHERE key=$1`,
        [key]
    );

    if (res.rows.length) {
        return JSON.parse(res.rows[0].value);
    }

    return null;
}

// ================= WHATSAPP START =================
async function startBot() {
    await initDB();

    const savedCreds = await loadSession("creds");

    const { state, saveCreds } = await useMultiFileAuthState("auth");

    sock = makeWASocket({
        auth: savedCreds || state,
        logger: pino({ level: "silent" }),
        browser: ["Render Bot", "Chrome", "120"]
    });

    sock.ev.on("creds.update", async (creds) => {
        saveCreds(creds);
        await saveSession("creds", creds);
    });

    sock.ev.on("connection.update", (update) => {
        const { connection, qr, lastDisconnect } = update;

        if (qr) {
            qrCode = qr;
            console.log("📌 QR updated");
        }

        if (connection === "open") {
            isConnected = true;
            qrCode = null;
            console.log("✅ WhatsApp Connected");
        }

        if (connection === "close") {
            isConnected = false;

            const status = lastDisconnect?.error?.output?.statusCode;

            if (status !== DisconnectReason.loggedOut) {
                console.log("🔄 Reconnecting...");
                startBot();
            } else {
                console.log("❌ Logged out");
            }
        }
    });
}

startBot();

// ================= ROUTES =================
app.get("/", (req, res) => {
    res.send("🚀 WA Bridge Running");
});

app.get("/status", (req, res) => {
    res.json({
        connected: isConnected,
        hasQR: !!qrCode
    });
});

app.get("/qr", async (req, res) => {
    if (!qrCode) return res.send("⏳ No QR yet");

    const img = await qrcode.toDataURL(qrCode);

    res.send(`
        <div style="text-align:center;margin-top:50px">
            <h2>Scan QR</h2>
            <img src="${img}" />
        </div>
    `);
});

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

// ================= START SERVER =================
const PORT = process.env.PORT || 10000;

app.listen(PORT, () => {
    console.log("🚀 Server running on", PORT);
});

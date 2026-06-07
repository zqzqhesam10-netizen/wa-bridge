const express = require("express");
const qrcode = require("qrcode");
const pino = require("pino");

const {
    default: makeWASocket,
    DisconnectReason,
    useMultiFileAuthState
} = require("@whiskeysockets/baileys");

const app = express();
app.use(express.json());

// ================= STATE =================
let sock;
let isConnected = false;
let qrCode = null;

// ================= DB (OPTIONAL SAFE) =================
let dbEnabled = false;
let db;

async function initDB() {
    try {
        const { Pool } = require("pg");

        db = new Pool({
            connectionString: process.env.DATABASE_URL,
            ssl: { rejectUnauthorized: false }
        });

        await db.query(`
            CREATE TABLE IF NOT EXISTS wa_session (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        `);

        dbEnabled = true;
        console.log("✅ DB Connected");
    } catch (e) {
        console.log("⚠️ DB Disabled:", e.message);
    }
}

// ================= WHATSAPP =================
async function startBot() {
    await initDB();

    const { state, saveCreds } =
        await useMultiFileAuthState("auth_info");

    sock = makeWASocket({
        auth: state,
        logger: pino({ level: "silent" }),
        browser: ["Render Bot", "Chrome", "120"]
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
        const { connection, qr, lastDisconnect } = update;

        if (qr) {
            qrCode = qr;
            console.log("📌 QR updated");
        }

        if (connection === "open") {
            isConnected = true;
            qrCode = null;
            console.log("✅ Connected");
        }

        if (connection === "close") {
            isConnected = false;

            const code = lastDisconnect?.error?.output?.statusCode;

            if (code !== DisconnectReason.loggedOut) {
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
        hasQR: !!qrCode,
        db: dbEnabled
    });
});

app.get("/groups", async (req, res) => {
    const groups = await sock.groupFetchAllParticipating();

    const result = Object.keys(groups).map(id => ({
        id,
        name: groups[id].subject
    }));

    res.json(result);
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
            return res.status(500).json({
                error: "not connected"
            });
        }

        await sock.sendMessage(groupId, { text });

        res.json({ ok: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// ================= SERVER =================
const PORT = process.env.PORT || 10000;

app.listen(PORT, () => {
    console.log("🚀 Server running on", PORT);
});

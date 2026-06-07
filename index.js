const express = require("express");
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason
} = require("@whiskeysockets/baileys");

const { Pool } = require("pg");

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

// ================= INIT DB =================
async function initDB() {
    await db.query(`
        CREATE TABLE IF NOT EXISTS system_state (
            id SERIAL PRIMARY KEY,
            qr TEXT,
            connected BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT NOW()
        );
    `);
}
initDB();

// ================= START WHATSAPP =================
async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("./auth");

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
        browser: ["Render", "Chrome", "120"]
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {
        const { connection, qr } = update;

        // ================= QR =================
        if (qr) {
            await db.query(`
                INSERT INTO system_state (id, qr, connected)
                VALUES (1, $1, false)
                ON CONFLICT (id)
                DO UPDATE SET qr = $1, connected = false, updated_at = NOW()
            `, [qr]);
        }

        // ================= CONNECTED =================
        if (connection === "open") {
            isConnected = true;

            await db.query(`
                UPDATE system_state
                SET connected = true, qr = NULL, updated_at = NOW()
                WHERE id = 1
            `);

            console.log("✅ WhatsApp Connected");
        }

        // ================= CLOSE =================
        if (connection === "close") {
            isConnected = false;

            const code = update.lastDisconnect?.error?.output?.statusCode;

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

// ================= GET STATUS =================
app.get("/status", async (req, res) => {
    const result = await db.query("SELECT * FROM system_state WHERE id=1");

    res.json({
        connected: isConnected,
        hasQR: result.rows[0]?.qr ? true : false
    });
});

// ================= GET QR =================
app.get("/qr", async (req, res) => {
    const result = await db.query("SELECT qr FROM system_state WHERE id=1");

    if (!result.rows.length || !result.rows[0].qr) {
        return res.send("<h3>QR not ready</h3>");
    }

    const qr = result.rows[0].qr;

    res.send(`
        <html>
        <body style="text-align:center;background:#111;color:#fff;padding-top:50px">
            <h2>WhatsApp QR Login</h2>
            <img width="300"
                 src="https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=${qr}" />
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

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

// ================= DB =================
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
        await useMultiFileAuthState(process.env.AUTH_DIR || "auth_info");

    sock = makeWASocket({
        auth: state,
        logger: pino({ level: "silent" }),
        browser: ["Ubuntu", "Chrome", "120.0.0"]
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {

        const { connection, qr, lastDisconnect } = update;

        if (qr) {
            qrCode = qr;
            console.log("📌 QR Updated");
        }

        if (connection === "open") {
            isConnected = true;
            qrCode = null;
            console.log("✅ WhatsApp Connected");
        }

        if (connection === "close") {

            isConnected = false;

            const code =
                lastDisconnect?.error?.output?.statusCode;

            console.log("❌ Connection closed:", code);

            if (code !== DisconnectReason.loggedOut) {

                console.log("🔄 Reconnecting in 10s...");

                setTimeout(() => {
                    startBot();
                }, 10000);

            } else {

                console.log("❌ Logged Out");
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

    try {

        const groups =
            await sock.groupFetchAllParticipating();

        const result =
            Object.keys(groups).map(id => ({
                id,
                name: groups[id].subject
            }));

        res.json(result);

    } catch (e) {

        res.status(500).json({
            error: e.message
        });
    }
});

app.get("/qr", async (req, res) => {

    if (!qrCode) {
        return res.send("⏳ No QR yet");
    }

    const img =
        await qrcode.toDataURL(qrCode);

    res.send(`
        <div style="text-align:center;margin-top:50px">
            <h2>Scan QR</h2>
            <img src="${img}" />
        </div>
    `);
});

// ================= SEND TEXT / IMAGE =================

app.post("/send", async (req, res) => {
    const { groupId, image, caption } = req.body || {};

    if (!sock || !isConnected) {
        console.log("SEND REJECTED: WhatsApp is not connected");
        return res.status(503).json({ ok: false, error: "WhatsApp is not connected" });
    }
    if (typeof groupId !== "string" || !groupId.endsWith("@g.us")) {
        return res.status(400).json({ ok: false, error: "groupId must be a WhatsApp group JID ending in @g.us" });
    }
    if (typeof image !== "string" || !/^https?:\/\//i.test(image)) {
        return res.status(400).json({ ok: false, error: "image must be an absolute HTTP(S) URL" });
    }

    try {
        console.log(`SEND REQUEST: group=${groupId} image_host=${new URL(image).host}`);
        const sentMessage = await sock.sendMessage(groupId, {
            image: { url: image },
            caption: caption || ""
        });
        const messageId = sentMessage?.key?.id || null;
        const remoteJid = sentMessage?.key?.remoteJid || groupId;
        console.log(`SEND ACCEPTED: group=${groupId} message_id=${messageId || "unknown"} remote_jid=${remoteJid}`);
        return res.json({ ok: true, messageId, remoteJid });
    } catch (e) {
        console.error("SEND FAILED:", e);
        return res.status(500).json({ ok: false, error: e.message || "WhatsApp send failed" });
    }
});

// ================= FLASK HELPER PROXY =================
// Keep the existing Flask helper endpoints on the same public app URL.
// The Flask process listens only on loopback inside this container.
const FLASK_INTERNAL_PORT = process.env.FLASK_INTERNAL_PORT || "5001";
async function proxyToFlask(req, res) {
    try {
        const target = `http://127.0.0.1:${FLASK_INTERNAL_PORT}${req.originalUrl}`;
        const upstream = await fetch(target, { method: req.method });
        const body = await upstream.text();
        const contentType = upstream.headers.get("content-type");
        if (contentType) res.set("Content-Type", contentType);
        return res.status(upstream.status).send(body);
    } catch (error) {
        console.error("Flask helper proxy failed:", error.message);
        return res.status(502).json({
            status: "error",
            message: "Flask helper is temporarily unavailable"
        });
    }
}

app.get("/api/check_updates", proxyToFlask);
app.get("/api/clear_messages", proxyToFlask);
app.get("/api/send_test", proxyToFlask);

// ================= SERVER =================

const PORT =
    process.env.PORT || 10000;

app.listen(PORT, "0.0.0.0", () => {
    console.log("🚀 Server running on", PORT);
});

// ================= SAFETY =================

process.on("uncaughtException", (err) => {
    console.log("UNCAUGHT:", err);
});

process.on("unhandledRejection", (err) => {
    console.log("REJECTION:", err);
});

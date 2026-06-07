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
        await useMultiFileAuthState("auth_info");

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
    try {

        const {
            groupId,
            image,
            caption
        } = req.body;

        await sock.sendMessage(groupId, {
            image: {
                url: image
            },
            caption: caption || ""
        });

        res.json({
            ok: true
        });

    } catch (e) {

        console.log(e);

        res.status(500).json({
            error: e.message
        });
    }
});

// ================= SERVER =================

const PORT =
    process.env.PORT || 10000;

app.listen(PORT, () => {
    console.log("🚀 Server running on", PORT);
});

// ================= SAFETY =================

process.on("uncaughtException", (err) => {
    console.log("UNCAUGHT:", err);
});

process.on("unhandledRejection", (err) => {
    console.log("REJECTION:", err);
});

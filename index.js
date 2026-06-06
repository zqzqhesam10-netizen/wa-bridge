const express = require("express");
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason
} = require("@whiskeysockets/baileys");

const qrcode = require("qrcode");
const pino = require("pino");

const app = express();
app.use(express.json());

let sock;
let qrCodeData = "";
let isConnected = false;

// ================= START WHATSAPP =================
async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("/tmp/auth_info");

    sock = makeWASocket({
        auth: state,
        logger: pino({ level: "silent" }),
        browser: ["Render Server", "Chrome", "120.0.0"]
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
        const { connection, qr, lastDisconnect } = update;

        if (qr) {
            qrCodeData = qr;
            console.log("📌 QR updated");
        }

        if (connection === "open") {
            console.log("✅ CONNECTED");
            isConnected = true;
            qrCodeData = "";
        }

        if (connection === "close") {
            isConnected = false;

            const code = lastDisconnect?.error?.output?.statusCode;
            console.log("❌ Closed:", code);

            if (code !== DisconnectReason.loggedOut) {
                setTimeout(start, 5000);
            }
        }
    });
}

// ================= QR PAGE =================
app.get("/qr", async (req, res) => {
    if (isConnected) {
        return res.send("<h1 style='color:green;text-align:center'>✅ تم الاتصال بالفعل</h1>");
    }

    if (!qrCodeData) {
        return res.send("<h1 style='text-align:center'>⏳ جاري تجهيز QR...</h1>");
    }

    const img = await qrcode.toDataURL(qrCodeData);

    res.send(`
        <html>
        <head>
            <title>WhatsApp QR</title>
        </head>
        <body style="background:#111;color:white;text-align:center;padding-top:50px">
            <h2>📱 امسح QR للربط</h2>
            <img src="${img}" style="width:300px;border:5px solid #25D366;border-radius:12px"/>
        </body>
        </html>
    `);
});

// ================= STATUS =================
app.get("/status", (req, res) => {
    res.json({
        connected: isConnected,
        hasQR: !!qrCodeData
    });
});

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

// ================= START SERVER =================
const PORT = process.env.PORT || 10000;

app.listen(PORT, () => {
    console.log("🚀 Server running on", PORT);
    start();
});

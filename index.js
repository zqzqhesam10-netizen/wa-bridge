const { default: makeWASocket, useMultiFileAuthState, DisconnectReason } = require("@whiskeysockets/baileys");
const pino = require("pino");
const express = require("express");
const qrcode = require("qrcode");

const app = express();

let qrCodeData = "";
let isConnected = false;

// ================= STATUS =================
app.get("/status", (req, res) => {
    res.json({
        connected: isConnected,
        hasQR: !!qrCodeData
    });
});

// ================= QR PAGE =================
app.get("/qr", async (req, res) => {
    if (isConnected) return res.send("<h1>✅ تم الاتصال بالفعل</h1>");
    if (!qrCodeData) return res.send("<h1>⏳ جاري تجهيز QR...</h1>");

    const qrImage = await qrcode.toDataURL(qrCodeData);

    res.send(`
        <div style="text-align:center; padding-top:50px; font-family:Arial">
            <h2>📱 امسح كود الواتساب</h2>
            <img src="${qrImage}" style="width:280px;border:5px solid #25D366;border-radius:12px"/>
        </div>
    `);
});

// ================= START SERVER =================
const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
    console.log("🚀 Server running on", PORT);
});

// ================= WHATSAPP CONNECTION =================
async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("./auth_info");

    const sock = makeWASocket({
        auth: state,
        logger: pino({ level: "silent" }),
        browser: ["WhatsApp Server", "Chrome", "125.0.0"]
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            qrCodeData = qr;
            console.log("📌 QR updated");
        }

        if (connection === "open") {
            console.log("✅ Connected");
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

start();

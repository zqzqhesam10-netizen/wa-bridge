const { default: makeWASocket, useMultiFileAuthState, DisconnectReason } = require("@whiskeysockets/baileys");
const pino = require("pino");
const express = require("express");
const qrcode = require("qrcode");
const app = express();

let qrCodeData = "";
let isConnected = false;

// 1. خادم الويب للعرض
app.get("/qr", async (req, res) => {
    if (isConnected) return res.send("<h1>✅ تم الاتصال بالفعل</h1>");
    if (!qrCodeData) return res.send("<h1>⏳ جاري تجهيز الكود، حدث الصفحة...</h1>");
    
    const qrImage = await qrcode.toDataURL(qrCodeData);
    res.send(`
        <div style="text-align:center; padding-top:50px;">
            <h1>امسح كود الواتساب</h1>
            <img src="${qrImage}" />
        </div>
    `);
});

app.listen(process.env.PORT || 3000);

// 2. خادم الواتساب
async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("/tmp/auth_info");

    const sock = makeWASocket({
        auth: state,
        logger: pino({ level: "silent" }),
        browser: ["WhatsApp Server", "Chrome", "125.0.0"]
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
        const { connection, lastDisconnect, qr } = update;
        if (qr) qrCodeData = qr;
        if (connection === "open") isConnected = true;
        if (connection === "close") {
            isConnected = false;
            if (lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut) {
                setTimeout(start, 5000);
            }
        }
    });
}

start();

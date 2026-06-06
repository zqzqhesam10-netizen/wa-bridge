const express = require("express");
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason } = require("@whiskeysockets/baileys");
const qrcode = require("qrcode");

const app = express();
app.use(express.json());

let sock;
let qrCodeImage = null;

async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("auth");

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false
    });

    sock.ev.on("connection.update", async (update) => {
        const { connection, qr } = update;

        // 🔥 هنا نحول QR إلى صورة
        if (qr) {
            qrCodeImage = await qrcode.toDataURL(qr);
            console.log("QR Updated");
        }

        if (connection === "open") {
            console.log("✅ WhatsApp Connected");
            qrCodeImage = null;
        }

        if (connection === "close") {
            console.log("❌ Connection closed, reconnecting...");
            start();
        }
    });

    sock.ev.on("creds.update", saveCreds);

    console.log("🚀 WhatsApp Bridge Running");
}

start();


// 🔥 عرض QR في المتصفح
app.get("/qr", (req, res) => {
    if (!qrCodeImage) {
        return res.send("<h3>QR not ready yet... refresh</h3>");
    }

    res.send(`
        <html>
        <body style="text-align:center;font-family:Arial">
            <h2>Scan QR with WhatsApp</h2>
            <img src="${qrCodeImage}" />
        </body>
        </html>
    `);
});


// 🔥 إرسال للمجموعة
app.post("/send", async (req, res) => {
    try {
        const { image, caption } = req.body;

        await sock.sendMessage(process.env.GROUP_ID, {
            image: { url: image },
            caption: caption
        });

        res.json({ status: "sent" });

    } catch (e) {
        console.log(e);
        res.status(500).json({ error: "failed" });
    }
});


// 🔥 ping لـ UptimeRobot
app.get("/ping", (req, res) => {
    res.send("OK");
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log("Server running on port", PORT);
});

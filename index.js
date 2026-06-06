const express = require("express");
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason
} = require("@whiskeysockets/baileys");

const qrcode = require("qrcode");

const app = express();
app.use(express.json());

let sock;
let qrCodeImage = null;

// ================= START WHATSAPP =================
async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("auth");

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
        browser: ["Render Bot", "Chrome", "1.0.0"]
    });

    // حفظ الجلسة (مهم جداً)
    sock.ev.on("creds.update", saveCreds);

    // تحديث الاتصال + QR
    sock.ev.on("connection.update", async (update) => {
        const { connection, qr, lastDisconnect } = update;

        // QR توليد
        if (qr) {
            qrCodeImage = await qrcode.toDataURL(qr);
            console.log("📌 QR Updated");
        }

        // اتصال ناجح
        if (connection === "open") {
            console.log("✅ WhatsApp Connected");
            qrCodeImage = null;
        }

        // انقطاع الاتصال
        if (connection === "close") {
            const reason = lastDisconnect?.error?.output?.statusCode;

            console.log("❌ Connection closed. Reason:", reason);

            // إذا تسجيل خروج أو خطأ قوي → إعادة تسجيل
            if (reason === DisconnectReason.loggedOut) {
                console.log("⚠️ Logged out - deleting session...");
                process.exit(1);
            } else {
                console.log("🔁 Reconnecting...");
                setTimeout(start, 3000);
            }
        }
    });

    console.log("🚀 WhatsApp Bridge Running");
}

start();

// ================= ROUTES =================

// الصفحة الرئيسية
app.get("/", (req, res) => {
    res.send("WhatsApp Bridge Running ✅");
});

// QR عرض
app.get("/qr", (req, res) => {
    if (!qrCodeImage) {
        return res.send("<h3>QR not ready or already connected ✅</h3>");
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

// إرسال للمجموعة
app.post("/send", async (req, res) => {
    try {
        const { image, caption } = req.body;

        if (!sock) {
            return res.status(400).json({ error: "WhatsApp not connected yet" });
        }

        await sock.sendMessage(process.env.GROUP_ID, {
            image: { url: image },
            caption: caption || ""
        });

        res.json({ status: "sent" });

    } catch (e) {
        console.log("SEND ERROR:", e);
        res.status(500).json({ error: "failed" });
    }
});

// Ping
app.get("/ping", (req, res) => {
    res.send("OK");
});

// ================= START SERVER =================
const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
    console.log("Server running on port", PORT);
});

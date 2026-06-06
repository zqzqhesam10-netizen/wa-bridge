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
let isConnected = false;
let restarting = false;

// ================= START WHATSAPP =================
async function start() {
    if (restarting) return;
    restarting = true;

    const { state, saveCreds } = await useMultiFileAuthState("auth");

    sock = makeWASocket({
        auth: state,

        // 🔥 أهم تعديل للاستقرار
        printQRInTerminal: false,
        browser: ["Ubuntu", "Chrome", "120.0.0"],
        syncFullHistory: false,
        markOnlineOnConnect: false,
        defaultQueryTimeoutMs: 60000
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {
        const { connection, qr, lastDisconnect } = update;

        if (qr) {
            qrCodeImage = await qrcode.toDataURL(qr);
            console.log("📌 QR Updated");
        }

        if (connection === "open") {
            console.log("✅ WhatsApp Connected");
            qrCodeImage = null;
            isConnected = true;
            restarting = false;
        }

        if (connection === "close") {
            isConnected = false;
            restarting = false;

            const code = lastDisconnect?.error?.output?.statusCode;

            console.log("❌ Connection closed:", code);

            // 🔥 أهم تعديل هنا
            if (code === DisconnectReason.loggedOut) {
                console.log("🚨 Logged out - delete auth folder");
                process.exit(1);
            }

            // ❗️ لا تعيد تشغيل سريع (يسبب 405)
            console.log("🔁 Reconnecting in 15 seconds...");
            setTimeout(() => {
                start();
            }, 15000);
        }
    });

    console.log("🚀 WhatsApp Bridge Running");
}

start();

// ================= SAFE SEND =================
async function sendToGroup(image, caption) {
    if (!sock || !isConnected) {
        throw new Error("WhatsApp not connected");
    }

    return await sock.sendMessage(process.env.GROUP_ID, {
        image: { url: image },
        caption: caption || ""
    });
}

// ================= ROUTES =================

app.get("/", (req, res) => {
    res.send("WhatsApp Bridge Running ✅");
});

app.get("/qr", (req, res) => {
    if (!qrCodeImage) {
        return res.send("<h3>Connected or QR not ready</h3>");
    }

    res.send(`
        <html>
        <body style="text-align:center;font-family:Arial">
            <h2>Scan QR</h2>
            <img src="${qrCodeImage}" />
        </body>
        </html>
    `);
});

// 🔥 SEND API
app.post("/send", async (req, res) => {
    try {
        const { image, caption } = req.body;

        if (!image) {
            return res.status(400).json({ error: "missing image" });
        }

        await sendToGroup(image, caption);

        res.json({ status: "sent" });

    } catch (e) {
        console.log("SEND ERROR:", e.message);
        res.status(500).json({ error: e.message });
    }
});

// ================= STABILITY PING =================
app.get("/ping", (req, res) => {
    res.send("OK");
});

// ================= CRASH PROTECTION =================
process.on("uncaughtException", (err) => {
    console.log("❌ Crash:", err);
});

process.on("unhandledRejection", (err) => {
    console.log("❌ Promise Error:", err);
});

// ================= START SERVER =================
const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
    console.log("Server running on port", PORT);
});

const express = require("express");
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason
} = require("@whiskeysockets/baileys");

const qrcode = require("qrcode");
const fs = require("fs");

const app = express();
app.use(express.json());

let sock;
let qrCodeImage = null;
let isConnected = false;

// ================= START =================
async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("auth");

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
        browser: ["Chrome (Linux)", "Chrome", "120.0.0"],
        keepAliveIntervalMs: 25000
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {
        const { connection, qr, lastDisconnect } = update;

        // توليد QR
        if (qr) {
            qrCodeImage = await qrcode.toDataURL(qr);
        }

        // اتصال ناجح
        if (connection === "open") {
            console.log("✅ WhatsApp Connected");
            qrCodeImage = null;
            isConnected = true;
        }

        // انقطاع الاتصال
        if (connection === "close") {
            isConnected = false;

            const code = lastDisconnect?.error?.output?.statusCode;
            console.log("❌ Closed:", code);

            if (code === DisconnectReason.loggedOut) {
                console.log("🚫 Logged out - manual login required");
                return;
            }

            setTimeout(() => {
                console.log("🔄 Reconnecting...");
                start();
            }, 5000);
        }
    });
}

// ================= SEND (جاهز) =================
app.post("/send", async (req, res) => {
    try {
        const { groupId, image, caption } = req.body;

        if (!sock || !isConnected) {
            return res.status(500).json({ error: "not connected" });
        }

        await sock.sendMessage(groupId, {
            image: { url: image },
            caption: caption || ""
        });

        res.json({ ok: true });

    } catch (e) {
        console.log("ERROR:", e);
        res.status(500).json({ error: e.message });
    }
});

// ================= STATUS =================
app.get("/status", (req, res) => {
    res.json({ connected: isConnected });
});

// ================= QR IMAGE (FIXED) =================
app.get("/qr-image", (req, res) => {
    if (!qrCodeImage) return res.status(404).send("QR not ready");

    const img = qrCodeImage.replace("data:image/png;base64,", "");
    const buffer = Buffer.from(img, "base64");

    res.writeHead(200, {
        "Content-Type": "image/png",
        "Content-Length": buffer.length
    });

    res.end(buffer);
});

// ================= QR PAGE =================
app.get("/qr", (req, res) => {
    res.send(`
<!DOCTYPE html>
<html>
<head>
    <title>WhatsApp QR</title>
    <meta charset="UTF-8">
    <style>
        body {
            background: #111;
            color: white;
            text-align: center;
            font-family: Arial;
            padding-top: 60px;
        }
        .box {
            background: #222;
            padding: 20px;
            display: inline-block;
            border-radius: 12px;
        }
        img {
            margin-top: 20px;
            width: 300px;
            border: 5px solid #25D366;
            border-radius: 12px;
        }
    </style>
</head>
<body>

<div class="box">
    <h2>📱 WhatsApp QR Login</h2>
    <p>امسح الكود لتسجيل الدخول</p>
    <img id="qr" src="/qr-image" />
</div>

<script>
setInterval(() => {
    document.getElementById("qr").src = "/qr-image?t=" + Date.now();
}, 3000);
</script>

</body>
</html>
    `);
});

// ================= SERVER START =================
const PORT = process.env.PORT || 10000;

app.listen(PORT, () => {
    console.log("🚀 Server running on", PORT);
    start();
});

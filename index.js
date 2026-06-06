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

// ================= START WHATSAPP =================
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

        if (qr) {
            qrCodeImage = await qrcode.toDataURL(qr);
        }

        if (connection === "open") {
            console.log("✅ Connected");
            qrCodeImage = null;
            isConnected = true;
        }

        if (connection === "close") {
            isConnected = false;

            const code = lastDisconnect?.error?.output?.statusCode;
            console.log("❌ Closed:", code);

            if (code === DisconnectReason.loggedOut) {
                console.log("🚫 Logged out");
                return;
            }

            setTimeout(() => start(), 5000);
        }
    });
}

// ================= STATUS =================
app.get("/status", (req, res) => {
    res.json({ connected: isConnected });
});

// ================= QR IMAGE (FIXED + SAFE) =================
app.get("/qr-image", (req, res) => {
    res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");

    if (!qrCodeImage) {
        // صورة شفافة صغيرة بدل كسر الصورة
        const empty = Buffer.from(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7W2h0AAAAASUVORK5CYII=",
            "base64"
        );

        res.writeHead(200, { "Content-Type": "image/png" });
        return res.end(empty);
    }

    const img = qrCodeImage.replace("data:image/png;base64,", "");
    const buffer = Buffer.from(img, "base64");

    res.writeHead(200, { "Content-Type": "image/png" });
    res.end(buffer);
});

// ================= QR PAGE (PRO UI) =================
app.get("/qr", (req, res) => {
    res.send(`
<!DOCTYPE html>
<html>
<head>
    <title>WhatsApp QR</title>
    <meta charset="UTF-8">
    <style>
        body {
            background: #0f0f0f;
            color: white;
            text-align: center;
            font-family: Arial;
            padding-top: 60px;
        }

        .box {
            background: #1e1e1e;
            padding: 25px;
            display: inline-block;
            border-radius: 14px;
            box-shadow: 0 0 20px rgba(37, 211, 102, 0.2);
        }

        img {
            margin-top: 20px;
            width: 280px;
            height: 280px;
            border: 4px solid #25D366;
            border-radius: 12px;
            background: white;
        }

        .status {
            margin-top: 10px;
            font-size: 14px;
            color: #25D366;
        }
    </style>
</head>
<body>

<div class="box">
    <h2>📱 WhatsApp QR Login</h2>
    <p>امسح الكود لتسجيل الدخول</p>

    <img id="qr" src="/qr-image?t=1" />

    <div class="status" id="status">Checking...</div>
</div>

<script>
function refreshQR() {
    document.getElementById("qr").src = "/qr-image?t=" + Date.now();
}

async function checkStatus() {
    try {
        const res = await fetch("/status");
        const data = await res.json();

        document.getElementById("status").innerText =
            data.connected ? "🟢 Connected" : "🟡 Waiting for QR";
    } catch (e) {
        document.getElementById("status").innerText = "⚠️ Error";
    }
}

setInterval(refreshQR, 2500);
setInterval(checkStatus, 2000);
</script>

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
